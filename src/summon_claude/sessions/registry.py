"""SQLite-backed session registry for cross-process session visibility."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from summon_claude.config import get_data_dir
from summon_claude.sessions.migrations import (
    CURRENT_SCHEMA_VERSION,
    run_migrations,
)

logger = logging.getLogger(__name__)

# Shared spawn-child limits — used by both session.py and summon_cli_mcp.py
MAX_SPAWN_CHILDREN = 5  # regular sessions (!summon start)
MAX_SPAWN_CHILDREN_PM = 15  # PM sessions (shared pool: MCP + !summon start)
MAX_SPAWN_DEPTH = 2  # max nesting: root → child → grandchild (3 sessions deep)

_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    status TEXT NOT NULL,
    session_name TEXT,
    cwd TEXT NOT NULL,
    slack_channel_id TEXT,
    slack_channel_name TEXT,
    model TEXT,
    claude_session_id TEXT,
    started_at TEXT NOT NULL,
    authenticated_at TEXT,
    ended_at TEXT,
    last_activity_at TEXT,
    total_cost_usd REAL DEFAULT 0.0,
    total_turns INTEGER DEFAULT 0,
    error_message TEXT
)
"""

_CREATE_PENDING_AUTH_TOKENS = """
CREATE TABLE IF NOT EXISTS pending_auth_tokens (
    short_code TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    session_id TEXT,
    user_id TEXT,
    details TEXT
)
"""

_CREATE_SPAWN_TOKENS = """
CREATE TABLE IF NOT EXISTS spawn_tokens (
    token TEXT PRIMARY KEY,
    parent_session_id TEXT,
    parent_channel_id TEXT,
    target_user_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    spawn_source TEXT NOT NULL DEFAULT 'session',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
)
"""

_CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
)
"""

_MAX_FAILED_ATTEMPTS = 5


def _now() -> str:
    return datetime.now(UTC).isoformat()


def slugify_for_channel(text: str) -> str:
    """Convert text to a Slack-safe channel name slug (lowercase, alphanumeric + hyphens)."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\-]", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def default_db_path() -> Path:
    """Determine the default DB path using XDG data dir, with migration from old path."""
    new_path = get_data_dir() / "registry.db"
    old_path = Path.home() / ".summon" / "registry.db"

    if old_path.exists() and not new_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_path), str(new_path))
        logger.info("Migrated registry from %s to %s", old_path, new_path)

    return new_path


class SessionRegistry:
    """Async SQLite session registry. Use as an async context manager."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or default_db_path()
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self.migrated_from: int | None = None

    async def __aenter__(self) -> SessionRegistry:
        await self._connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        await self._close()
        return False

    async def _connect(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path), isolation_level=None)
        # Restrict DB file to owner-only access (0600)
        try:
            self._db_path.chmod(0o600)
        except OSError as e:
            logger.debug("Could not set DB permissions: %s", e)
        self._db.row_factory = aiosqlite.Row
        # Enable WAL mode for concurrent access from multiple processes
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA journal_size_limit=67108864")
        await self._db.execute(_CREATE_SESSIONS)
        await self._db.execute(_CREATE_PENDING_AUTH_TOKENS)
        await self._db.execute(_CREATE_AUDIT_LOG)
        await self._db.execute(_CREATE_SPAWN_TOKENS)
        await self._db.execute(_CREATE_SCHEMA_VERSION)
        await self._db.commit()

        # Detect fresh DB before migrations (empty schema_version table).
        async with self._db.execute("SELECT COUNT(*) FROM schema_version") as cur:
            row = await cur.fetchone()
            is_fresh = row[0] == 0  # type: ignore[index]

        # Always run migrations — single source of truth for schema changes.
        # Fresh DBs start at version 0 (0→1 is a no-op baseline), so all
        # real migrations run inside the same BEGIN IMMEDIATE transaction.
        pre_version = await run_migrations(self._db)
        # Fresh DBs don't report as "migrated" to avoid spurious upgrade messages.
        self.migrated_from = CURRENT_SCHEMA_VERSION if is_fresh else pre_version

    async def _close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        """Return the active DB connection. Raises if not connected."""
        return self._check_connected()

    def _check_connected(self) -> aiosqlite.Connection:
        if not self._db:
            raise RuntimeError("SessionRegistry not connected. Use as async context manager.")
        return self._db

    async def is_name_active(self, name: str) -> bool:
        """Check if any active session (pending_auth/active) uses this name."""
        db = self._check_connected()
        async with db.execute(
            "SELECT 1 FROM sessions WHERE session_name = ?"
            " AND status IN ('pending_auth', 'active') LIMIT 1",
            (name,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def register(
        self,
        session_id: str,
        pid: int,
        cwd: str,
        name: str | None = None,
        model: str | None = None,
        parent_session_id: str | None = None,
        authenticated_user_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """Insert a new session with status pending_auth."""
        db = self._check_connected()
        async with self._lock:
            if name and await self.is_name_active(name):
                raise ValueError(
                    f"An active session with name {name!r} already exists. "
                    "Use --name to specify a different name."
                )
            try:
                await db.execute(
                    """
                    INSERT INTO sessions
                        (session_id, pid, status, session_name, cwd, model,
                         started_at, last_activity_at,
                         parent_session_id, authenticated_user_id, project_id)
                    VALUES (?, ?, 'pending_auth', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        pid,
                        name,
                        cwd,
                        model,
                        _now(),
                        _now(),
                        parent_session_id,
                        authenticated_user_id,
                        project_id,
                    ),
                )
            except Exception as exc:
                if "UNIQUE constraint failed: sessions.session_name" in str(exc):
                    raise ValueError(
                        f"An active session with name {name!r} already exists. "
                        "Use --name to specify a different name."
                    ) from exc
                raise
            await db.commit()

    _VALID_STATUSES: frozenset[str] = frozenset(
        {"pending_auth", "active", "completed", "errored", "suspended"}
    )

    _UPDATABLE_FIELDS: frozenset[str] = frozenset(
        {
            "slack_channel_id",
            "slack_channel_name",
            "claude_session_id",
            "authenticated_at",
            "authenticated_user_id",
            "ended_at",
            "error_message",
            "model",
            "canvas_id",
            "canvas_markdown",
            "project_id",
        }
    )

    async def update_status(self, session_id: str, status: str, **kwargs: Any) -> None:
        """Update session status and any additional fields."""
        if status not in self._VALID_STATUSES:
            raise ValueError(
                f"Invalid status {status!r}; must be one of {sorted(self._VALID_STATUSES)}"
            )
        db = self._check_connected()
        updates: dict[str, Any] = {"status": status, "last_activity_at": _now()}
        for key, val in kwargs.items():
            if key in self._UPDATABLE_FIELDS:
                updates[key] = val
            else:
                logger.warning("update_status: ignoring unknown field %r", key)

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = [*updates.values(), session_id]
        async with self._lock:
            await db.execute(f"UPDATE sessions SET {set_clause} WHERE session_id = ?", values)
            await db.commit()

    async def heartbeat(self, session_id: str) -> None:
        """Update last_activity_at for a session."""
        db = self._check_connected()
        async with self._lock:
            await db.execute(
                "UPDATE sessions SET last_activity_at = ? WHERE session_id = ?",
                (_now(), session_id),
            )
            await db.commit()

    async def record_turn(
        self, session_id: str, cost_usd: float = 0.0, context_pct: float | None = None
    ) -> None:
        """Increment turn count, accumulate cost, and update context usage."""
        db = self._check_connected()
        async with self._lock:
            if context_pct is not None:
                await db.execute(
                    """
                    UPDATE sessions
                    SET total_turns = total_turns + 1,
                        total_cost_usd = total_cost_usd + ?,
                        context_pct = ?,
                        last_activity_at = ?
                    WHERE session_id = ?
                    """,
                    (cost_usd, context_pct, _now(), session_id),
                )
            else:
                await db.execute(
                    """
                    UPDATE sessions
                    SET total_turns = total_turns + 1,
                        total_cost_usd = total_cost_usd + ?,
                        last_activity_at = ?
                    WHERE session_id = ?
                    """,
                    (cost_usd, _now(), session_id),
                )
            await db.commit()

    async def get_session(self, session_id: str) -> dict | None:
        """Fetch one session by ID."""
        db = self._check_connected()
        async with db.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def resolve_session(self, identifier: str) -> tuple[dict | None, list[dict]]:  # noqa: PLR0911
        """Resolve a session by ID prefix, session name, or channel name.

        Returns ``(session, matches)`` where *session* is the unique match
        (or ``None``) and *matches* is the list of candidates when ambiguous.
        """
        # 1. Exact session_id match
        exact = await self.get_session(identifier)
        if exact:
            return exact, [exact]

        db = self._check_connected()

        # 2. Prefix match on session_id (escape LIKE metacharacters)
        safe = identifier.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with db.execute(
            "SELECT * FROM sessions WHERE session_id LIKE ? ESCAPE '\\' ORDER BY started_at DESC",
            (f"{safe}%",),
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
            if len(rows) == 1:
                return rows[0], rows
            if len(rows) > 1:
                return None, rows

        # 3. Session name match — active wins (uniqueness-constrained)
        async with db.execute(
            "SELECT * FROM sessions WHERE session_name = ?"
            " AND status IN ('pending_auth', 'active') LIMIT 1",
            (identifier,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                d = dict(row)
                return d, [d]

        # 4. Session name match — most recent historical
        async with db.execute(
            "SELECT * FROM sessions WHERE session_name = ? ORDER BY started_at DESC LIMIT 1",
            (identifier,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                d = dict(row)
                return d, [d]

        # 5. Channel name match
        async with db.execute(
            "SELECT * FROM sessions WHERE slack_channel_name = ? ORDER BY started_at DESC LIMIT 1",
            (identifier,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                d = dict(row)
                return d, [d]
            return None, []

    async def list_active(self) -> list[dict]:
        """List all sessions with status pending_auth or active."""
        db = self._check_connected()
        async with db.execute(
            "SELECT * FROM sessions WHERE status IN ('pending_auth', 'active')"
            " ORDER BY started_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def list_all(self, limit: int = 50) -> list[dict]:
        """List recent sessions (all statuses)."""
        db = self._check_connected()
        async with db.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def list_children(self, parent_session_id: str, *, limit: int = 50) -> list[dict]:
        """List sessions spawned by a given parent session."""
        db = self._check_connected()
        async with db.execute(
            "SELECT * FROM sessions WHERE parent_session_id = ? ORDER BY started_at DESC LIMIT ?",
            (parent_session_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def compute_spawn_depth(self, session_id: str) -> int:
        """Count ancestor levels by following parent_session_id chain.

        Returns 0 for root sessions (no parent), 1 for direct children, etc.
        """
        db = self._check_connected()
        depth = 0
        current: str | None = session_id
        visited: set[str] = set()

        while current:
            if current in visited:
                logger.error("Circular parent chain detected at session %s", current)
                break
            visited.add(current)
            async with db.execute(
                "SELECT parent_session_id FROM sessions WHERE session_id = ?",
                (current,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row or not row[0]:
                break
            depth += 1
            current = row[0]

        return depth

    async def list_stale(self) -> list[dict]:
        """Return sessions with status pending_auth/active whose PIDs are dead."""
        db = self._check_connected()
        async with db.execute(
            "SELECT * FROM sessions WHERE status IN ('pending_auth', 'active')"
        ) as cursor:
            rows = await cursor.fetchall()

        stale = []
        for row in rows:
            pid = row["pid"]
            if not _pid_alive(pid):
                stale.append(dict(row))
        return stale

    async def mark_stale(self, session_id: str, reason: str) -> None:
        """Mark a single session as errored with a reason and ended_at timestamp."""
        await self.update_status(
            session_id,
            "errored",
            error_message=reason,
            ended_at=_now(),
        )

    async def cleanup_active(self, reason: str) -> list[dict]:
        """Mark all active/pending_auth sessions as errored.

        Returns the list of sessions that were cleaned up (empty if none).
        Intended for daemon startup where no sessions should be active.
        """
        active = await self.list_active()
        for session in active:
            await self.mark_stale(session["session_id"], reason)
        return active

    # --- Project methods ---

    async def add_project(self, name: str, directory: str) -> str:
        """Register a new project. Returns the generated project_id.

        Derives ``channel_prefix`` from the project name using slugification.
        Raises ``ValueError`` if the name is empty, contains no alphanumeric
        characters, or a project with the same name/channel prefix already exists.
        """
        if not name or not name.strip():
            raise ValueError("Project name must not be empty.")
        slug = slugify_for_channel(name)
        if not slug:
            raise ValueError(
                f"Project name {name!r} must contain at least one alphanumeric character."
            )
        project_id = str(uuid.uuid4())
        channel_prefix = slug[:20].rstrip("-") or "project"
        db = self._check_connected()
        async with self._lock:
            try:
                await db.execute(
                    """
                    INSERT INTO projects
                        (project_id, name, directory, channel_prefix,
                         workflow_instructions, created_at)
                    VALUES (?, ?, ?, ?, '', ?)
                    """,
                    (project_id, name, directory, channel_prefix, _now()),
                )
                await db.commit()
            except sqlite3.IntegrityError as exc:
                msg = str(exc)
                if "UNIQUE constraint failed: projects.name" in msg:
                    raise ValueError(f"A project with name {name!r} already exists.") from exc
                if "UNIQUE constraint failed: projects.channel_prefix" in msg:
                    raise ValueError(
                        f"Channel prefix {channel_prefix!r} (derived from {name!r}) "
                        "conflicts with an existing project. Use a more distinct name."
                    ) from exc
                raise
        return project_id

    async def remove_project(self, project_id_or_name: str) -> list[str]:
        """Remove a project by ID or name.

        Returns a list of session_ids that were active and need stopping.
        Raises ``ValueError`` if the project doesn't exist.
        """
        project = await self.get_project(project_id_or_name)
        if project is None:
            raise ValueError(f"No project found: {project_id_or_name!r}")

        project_id = project["project_id"]
        db = self._check_connected()
        async with self._lock:
            # Collect active session IDs inside the lock to avoid TOCTOU
            async with db.execute(
                "SELECT session_id FROM sessions WHERE project_id = ?"
                " AND status IN ('pending_auth', 'active')",
                (project_id,),
            ) as cursor:
                active_ids = [row[0] for row in await cursor.fetchall()]
            await db.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            # Clear suspended sessions — they can't be restarted without a project
            await db.execute(
                "UPDATE sessions SET status = 'completed' WHERE project_id = ?"
                " AND status = 'suspended'",
                (project_id,),
            )
            # NULL out project_id on historical sessions to avoid dangling references
            await db.execute(
                "UPDATE sessions SET project_id = NULL WHERE project_id = ?",
                (project_id,),
            )
            await db.commit()
        return active_ids

    async def list_projects(self) -> list[dict]:
        """List all projects with PM status fields.

        Only considers PM sessions (name matching ``%-pm-%``) to avoid
        pollution from child sessions that inherit the project_id.

        Each row includes:
        - ``pm_running``: 1 if an active/pending_auth PM session exists
        - ``last_pm_status``: status of the most recent PM session (or NULL)
        - ``last_pm_error``: error_message from the most recent PM session (or NULL)
        """
        db = self._check_connected()
        async with db.execute(
            "SELECT p.*,"
            "  EXISTS("
            "    SELECT 1 FROM sessions s"
            "    WHERE s.project_id = p.project_id"
            "      AND s.session_name LIKE '%-pm-%'"
            "      AND s.status IN ('pending_auth', 'active')"
            "  ) AS pm_running,"
            "  (SELECT s2.status FROM sessions s2"
            "   WHERE s2.project_id = p.project_id"
            "     AND s2.session_name LIKE '%-pm-%'"
            "   ORDER BY s2.started_at DESC LIMIT 1"
            "  ) AS last_pm_status,"
            "  (SELECT s2.error_message FROM sessions s2"
            "   WHERE s2.project_id = p.project_id"
            "     AND s2.session_name LIKE '%-pm-%'"
            "   ORDER BY s2.started_at DESC LIMIT 1"
            "  ) AS last_pm_error"
            " FROM projects p ORDER BY p.name"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_project(self, project_id_or_name: str) -> dict | None:
        """Fetch a project by ID or name. Returns None if not found."""
        db = self._check_connected()
        # Try by project_id first
        async with db.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id_or_name,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
        # Fall back to name
        async with db.execute(
            "SELECT * FROM projects WHERE name = ?", (project_id_or_name,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_project_sessions(self, project_id: str) -> list[dict]:
        """List sessions associated with a project."""
        db = self._check_connected()
        async with db.execute(
            "SELECT * FROM sessions WHERE project_id = ? ORDER BY started_at DESC",
            (project_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    _UPDATABLE_PROJECT_FIELDS: frozenset[str] = frozenset(
        {"pm_channel_id", "workflow_instructions", "channel_prefix", "directory"}
    )

    async def update_project(self, project_id: str, **kwargs: Any) -> None:
        """Update mutable project fields (pm_channel_id, workflow_instructions, etc.).

        Raises ``KeyError`` if the project_id does not exist.
        """
        updates: dict[str, Any] = {}
        for key, val in kwargs.items():
            if key in self._UPDATABLE_PROJECT_FIELDS:
                updates[key] = val
            else:
                logger.warning("update_project: ignoring unknown field %r", key)
        if not updates:
            return
        db = self._check_connected()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = [*updates.values(), project_id]
        async with self._lock:
            cursor = await db.execute(
                f"UPDATE projects SET {set_clause} WHERE project_id = ?", values
            )
            await db.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"No project with id {project_id!r}")

    # --- Canvas methods ---

    async def update_canvas(self, session_id: str, canvas_id: str, canvas_markdown: str) -> None:
        """Update the canvas ID and markdown for a session."""
        db = self._check_connected()
        async with self._lock:
            await db.execute(
                "UPDATE sessions SET canvas_id = ?, canvas_markdown = ?, last_activity_at = ?"
                " WHERE session_id = ?",
                (canvas_id, canvas_markdown, _now(), session_id),
            )
            await db.commit()

    async def get_canvas(self, session_id: str) -> tuple[str | None, str | None]:
        """Return (canvas_id, canvas_markdown) for a session."""
        db = self._check_connected()
        async with db.execute(
            "SELECT canvas_id, canvas_markdown FROM sessions WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0], row[1]
            return None, None

    async def get_canvas_by_channel(
        self, channel_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """Return (canvas_id, canvas_markdown, authenticated_user_id) for a channel."""
        db = self._check_connected()
        async with db.execute(
            "SELECT canvas_id, canvas_markdown, authenticated_user_id FROM sessions"
            " WHERE slack_channel_id = ? AND canvas_id IS NOT NULL"
            " ORDER BY started_at DESC LIMIT 1",
            (channel_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0], row[1], row[2]
            return None, None, None

    # --- Pending auth token methods ---

    async def store_pending_token(
        self,
        short_code: str,
        session_id: str,
        expires_at: str,
    ) -> None:
        """Store a pending auth token for cross-process auth verification."""
        db = self._check_connected()
        async with self._lock:
            await db.execute(
                """
                INSERT OR REPLACE INTO pending_auth_tokens
                    (short_code, session_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (short_code, session_id, _now(), expires_at),
            )
            await db.commit()

    async def _get_pending_token(self, short_code: str) -> dict | None:
        """Retrieve a pending auth token by short code (used by tests)."""
        db = self._check_connected()
        async with db.execute(
            "SELECT * FROM pending_auth_tokens WHERE short_code = ?", (short_code,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_all_pending_tokens(self) -> list[dict]:
        """Retrieve pending auth tokens for constant-time comparison.

        Returns only the columns needed for verification (short_code, expires_at,
        failed_attempts) — NOT the full token value, which is only fetched during
        atomic_consume_pending_token after verification succeeds.
        """
        db = self._check_connected()
        async with db.execute(
            "SELECT short_code, expires_at, failed_attempts FROM pending_auth_tokens"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def delete_pending_token(self, short_code: str) -> None:
        """Delete a pending auth token (after successful verification)."""
        db = self._check_connected()
        async with self._lock:
            await db.execute("DELETE FROM pending_auth_tokens WHERE short_code = ?", (short_code,))
            await db.commit()

    async def atomic_consume_pending_token(self, short_code: str, now_iso: str) -> dict | None:
        """Atomically delete and return a non-expired, not-locked-out pending auth token.

        Uses a single DELETE ... RETURNING statement so concurrent callers
        cannot both succeed for the same code (no TOCTOU race).
        Returns the row dict if the code existed and was valid, else None.
        """
        db = self._check_connected()
        async with self._lock:
            async with db.execute(
                """DELETE FROM pending_auth_tokens
                   WHERE short_code = ?
                     AND expires_at > ?
                     AND failed_attempts < ?
                   RETURNING *""",
                (short_code, now_iso, _MAX_FAILED_ATTEMPTS),
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
            return dict(row) if row else None

    async def record_failed_auth_attempt(self, short_code: str) -> None:
        """Increment failed attempt counter; invalidate token after max failures."""
        db = self._check_connected()
        async with self._lock:
            await db.execute(
                "UPDATE pending_auth_tokens"
                " SET failed_attempts = failed_attempts + 1 WHERE short_code = ?",
                (short_code,),
            )
            await db.commit()

    # --- Spawn token methods ---

    async def store_spawn_token(
        self,
        token: str,
        target_user_id: str,
        cwd: str,
        expires_at: str,
        spawn_source: str = "session",
        parent_session_id: str | None = None,
        parent_channel_id: str | None = None,
    ) -> None:
        """Store a spawn token for pre-authenticated session creation."""
        db = self._check_connected()
        async with self._lock:
            await db.execute(
                """
                INSERT INTO spawn_tokens
                    (token, parent_session_id, parent_channel_id, target_user_id,
                     cwd, spawn_source, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    parent_session_id,
                    parent_channel_id,
                    target_user_id,
                    cwd,
                    spawn_source,
                    _now(),
                    expires_at,
                ),
            )
            await db.commit()

    async def get_all_spawn_tokens(self) -> list[dict]:
        """Retrieve all spawn tokens for constant-time comparison."""
        db = self._check_connected()
        async with db.execute("SELECT * FROM spawn_tokens") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def consume_spawn_token(self, token: str, now_iso: str) -> dict | None:
        """Atomically delete and return a non-expired spawn token."""
        db = self._check_connected()
        async with self._lock:
            async with db.execute(
                """DELETE FROM spawn_tokens
                   WHERE token = ?
                     AND expires_at > ?
                   RETURNING *""",
                (token, now_iso),
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
            return dict(row) if row else None

    # --- Workflow instruction methods ---

    async def get_workflow_defaults(self) -> str:
        """Return global default workflow instructions, or empty string if unset."""
        db = self._check_connected()
        async with db.execute("SELECT instructions FROM workflow_defaults WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else ""

    async def set_workflow_defaults(self, instructions: str) -> None:
        """Set global default workflow instructions (upsert)."""
        db = self._check_connected()
        now = _now()
        async with self._lock:
            await db.execute(
                "INSERT INTO workflow_defaults (id, instructions, updated_at)"
                " VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE"
                " SET instructions = excluded.instructions,"
                " updated_at = excluded.updated_at",
                (instructions, now),
            )
            await db.commit()

    async def clear_workflow_defaults(self) -> None:
        """Remove global default workflow instructions."""
        db = self._check_connected()
        async with self._lock:
            await db.execute("DELETE FROM workflow_defaults WHERE id = 1")
            await db.commit()

    async def get_project_workflow(self, project_id: str) -> str:
        """Return per-project workflow instructions, or empty string if unset."""
        db = self._check_connected()
        async with db.execute(
            "SELECT workflow_instructions FROM projects WHERE project_id = ?",
            (project_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else ""

    async def set_project_workflow(self, project_id: str, instructions: str) -> None:
        """Set per-project workflow instructions.

        Raises ``KeyError`` if the project_id does not exist.
        """
        db = self._check_connected()
        async with self._lock:
            cursor = await db.execute(
                "UPDATE projects SET workflow_instructions = ? WHERE project_id = ?",
                (instructions, project_id),
            )
            await db.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"No project with id {project_id!r}")

    async def clear_project_workflow(self, project_id: str) -> None:
        """Reset per-project workflow instructions to empty string.

        No-op if the project does not exist.
        """
        try:
            await self.set_project_workflow(project_id, "")
        except KeyError:
            pass

    async def get_effective_workflow(self, project_id: str) -> str:
        """Return effective workflow instructions for a project.

        Returns per-project override if non-empty, otherwise global defaults.
        """
        db = self._check_connected()
        async with db.execute(
            "SELECT COALESCE("
            "  NULLIF((SELECT workflow_instructions FROM projects"
            "          WHERE project_id = ?), ''),"
            "  (SELECT instructions FROM workflow_defaults WHERE id = 1),"
            "  ''"
            ")",
            (project_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else ""

    # --- Audit log methods ---

    async def log_event(
        self,
        event_type: str,
        session_id: str | None = None,
        user_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        """Record an audit event."""
        db = self._check_connected()
        async with self._lock:
            await db.execute(
                "INSERT INTO audit_log (timestamp, event_type, session_id, user_id, details)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    _now(),
                    event_type,
                    session_id,
                    user_id,
                    json.dumps(details) if details else None,
                ),
            )
            await db.commit()


def _pid_alive(pid: int) -> bool:
    """Check if a process is alive using os.kill(pid, 0)."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
