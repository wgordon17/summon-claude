"""Schema migrations for the session registry.

All schema changes after the v1 baseline live here as migration functions.
Fresh databases create the v1 baseline DDL, then run all migrations — no
schema change is ever duplicated between _connect() and a migration.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 18


# ---------------------------------------------------------------------------
# Migration functions
# ---------------------------------------------------------------------------


async def _migrate_1_to_2(db: aiosqlite.Connection) -> None:
    """Add parent_session_id and authenticated_user_id to sessions table."""
    for col in ("parent_session_id TEXT", "authenticated_user_id TEXT"):
        try:
            await db.execute(f"ALTER TABLE sessions ADD COLUMN {col}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
            logger.debug("Column %s already exists, skipping", col)


async def _migrate_2_to_3(db: aiosqlite.Connection) -> None:
    """Create workflow_defaults table."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_defaults (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            instructions TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )


async def _migrate_3_to_4(db: aiosqlite.Connection) -> None:
    """Add partial unique index on active session names."""
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_active_session_name "
        "ON sessions (session_name) "
        "WHERE session_name IS NOT NULL AND status IN ('pending_auth', 'active')"
    )


async def _migrate_4_to_5(db: aiosqlite.Connection) -> None:
    """Add canvas_id and canvas_markdown to sessions table."""
    for col in ("canvas_id TEXT", "canvas_markdown TEXT"):
        try:
            await db.execute(f"ALTER TABLE sessions ADD COLUMN {col}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
            logger.debug("Column %s already exists, skipping", col)


async def _migrate_5_to_6(db: aiosqlite.Connection) -> None:
    """Add index on parent_session_id for list_children queries."""
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_parent_session_id ON sessions (parent_session_id)"
    )


async def _migrate_6_to_7(db: aiosqlite.Connection) -> None:
    """Add context_pct column for tracking context window usage."""
    # SQLite lacks IF NOT EXISTS for ALTER TABLE ADD COLUMN
    with contextlib.suppress(sqlite3.OperationalError):
        await db.execute("ALTER TABLE sessions ADD COLUMN context_pct REAL")


async def _migrate_7_to_8(db: aiosqlite.Connection) -> None:
    """Create projects table and add project_id column to sessions table."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            directory TEXT NOT NULL,
            channel_prefix TEXT NOT NULL,
            pm_channel_id TEXT,
            workflow_instructions TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    try:
        await db.execute("ALTER TABLE sessions ADD COLUMN project_id TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise
        logger.debug("Column project_id already exists, skipping")


async def _migrate_8_to_9(db: aiosqlite.Connection) -> None:
    """Add unique index on channel_prefix in projects table."""
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_channel_prefix ON projects (channel_prefix)"
    )


async def _migrate_9_to_10(db: aiosqlite.Connection) -> None:
    """Create channels table, add effort column, migrate canvas data, drop redundant columns."""
    # Add effort column to sessions table
    with contextlib.suppress(sqlite3.OperationalError):
        await db.execute("ALTER TABLE sessions ADD COLUMN effort TEXT")

    # Create channels table
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            channel_name TEXT NOT NULL,
            claude_session_id TEXT,
            canvas_id TEXT,
            canvas_markdown TEXT,
            cwd TEXT NOT NULL,
            authenticated_user_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_channels_name ON channels(channel_name)")

    # Migrate existing data from sessions to channels (latest session per channel)
    await db.execute(
        """
        INSERT OR IGNORE INTO channels
            (channel_id, channel_name, claude_session_id, canvas_id,
             canvas_markdown, cwd, authenticated_user_id, created_at, updated_at)
        SELECT s.slack_channel_id, s.slack_channel_name, s.claude_session_id,
               s.canvas_id, s.canvas_markdown, s.cwd, s.authenticated_user_id,
               s.started_at, s.started_at
        FROM sessions s
        WHERE s.slack_channel_id IS NOT NULL
          AND s.slack_channel_name IS NOT NULL
          AND s.rowid = (
            SELECT s2.rowid FROM sessions s2
            WHERE s2.slack_channel_id = s.slack_channel_id
            ORDER BY s2.started_at DESC LIMIT 1
          )
        """
    )

    # Drop canvas columns from sessions — now channel-only.
    # Data was just copied above; no code writes these on sessions anymore.
    for col in ("canvas_id", "canvas_markdown"):
        with contextlib.suppress(sqlite3.OperationalError):
            await db.execute(f"ALTER TABLE sessions DROP COLUMN {col}")


async def _migrate_10_to_11(db: aiosqlite.Connection) -> None:
    """Create session_tasks table for structured task tracking."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS session_tasks (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            priority TEXT NOT NULL DEFAULT 'medium',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_session_tasks_session
        ON session_tasks(session_id)
    """)


async def _migrate_11_to_12(db: aiosqlite.Connection) -> None:
    """Add hooks column to workflow_defaults and projects tables."""
    for table in ("workflow_defaults", "projects"):
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN hooks TEXT DEFAULT NULL")
        except sqlite3.OperationalError as e:
            err = str(e).lower()
            if "duplicate column name" not in err and "no such table" not in err:
                raise
            logger.debug("Column hooks on %s already exists or table absent, skipping", table)


async def _migrate_12_to_13(db: aiosqlite.Connection) -> None:
    """Add index on authenticated_user_id + status for channel scoping queries.

    Covers get_all_active_channels (Global PM) and get_child_channels (Project PM).
    Without this index, both queries do a full table scan on every MCP tool call.
    """
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_auth_user_status "
        "ON sessions (authenticated_user_id, status, slack_channel_id)"
    )


async def _migrate_13_to_14(db: aiosqlite.Connection) -> None:
    """Make projects.workflow_instructions nullable (NULL = use global, '' = explicit clear).

    SQLite cannot change column constraints with ALTER TABLE, so we use the
    copy-drop-rename pattern. sessions.project_id references projects but has
    no REFERENCES constraint, so PRAGMA foreign_keys does not fire.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS projects_new (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            directory TEXT NOT NULL,
            channel_prefix TEXT NOT NULL,
            pm_channel_id TEXT,
            workflow_instructions TEXT DEFAULT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            hooks TEXT DEFAULT NULL
        )
        """
    )
    # Copy existing rows; convert empty-string workflow to NULL so existing
    # projects without explicit instructions fall back to global defaults.
    await db.execute(
        """
        INSERT INTO projects_new
            (project_id, name, directory, channel_prefix, pm_channel_id,
             workflow_instructions, created_at, hooks)
        SELECT project_id, name, directory, channel_prefix, pm_channel_id,
               NULLIF(workflow_instructions, ''),
               created_at, hooks
        FROM projects
        """
    )
    await db.execute("DROP TABLE projects")
    await db.execute("ALTER TABLE projects_new RENAME TO projects")
    # Recreate indexes that existed on the old table.
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_channel_prefix ON projects (channel_prefix)"
    )
    # Also create the parent_status index here (not only in 12→13) so that
    # databases already at version 13 from PR #65's original migration still
    # get this index.  IF NOT EXISTS makes it safe if 12→13 already created it.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_parent_status "
        "ON sessions (parent_session_id, status)"
    )


async def _migrate_14_to_15(db: aiosqlite.Connection) -> None:
    """Create scheduled_jobs table for cron job persistence."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_jobs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            cron_expr TEXT NOT NULL,
            prompt TEXT NOT NULL,
            recurring INTEGER NOT NULL DEFAULT 1,
            max_lifetime_s INTEGER NOT NULL DEFAULT 86400,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_session
        ON scheduled_jobs(session_id)
    """)


async def _migrate_15_to_16(db: aiosqlite.Connection) -> None:
    """Add jira_jql column to projects table for per-project Jira issue filter.

    Stored on the projects table (not a separate table) because JQL is the only
    per-project Jira config field. PM sessions read this via registry.get_project()
    to build the triage prompt via build_pm_scan_prompt(). Set via CLI:
    ``summon project add --jql`` or ``summon project update --jql``.
    """
    try:
        await db.execute("ALTER TABLE projects ADD COLUMN jira_jql TEXT DEFAULT NULL")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise
        logger.debug("Column jira_jql already exists, skipping")


async def _migrate_16_to_17(db: aiosqlite.Connection) -> None:
    """Add auto_mode_rules column to projects table for per-project classifier rules."""
    try:
        await db.execute("ALTER TABLE projects ADD COLUMN auto_mode_rules TEXT DEFAULT NULL")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise
        logger.debug("Column auto_mode_rules already exists, skipping")


async def _migrate_17_to_18(db: aiosqlite.Connection) -> None:
    """Add bug hunter columns to projects and sessions tables."""
    project_cols = [
        "bug_hunter_enabled INTEGER DEFAULT 0",
        "bug_hunter_scan_interval_minutes INTEGER DEFAULT 60",
        "bug_hunter_network_allowlist TEXT DEFAULT NULL",
        "bug_hunter_secrets TEXT DEFAULT NULL",
    ]
    for col in project_cols:
        try:
            await db.execute(f"ALTER TABLE projects ADD COLUMN {col}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise

    try:
        await db.execute("ALTER TABLE sessions ADD COLUMN vm_id TEXT DEFAULT NULL")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc):
            raise

    # One bug hunter per project — enforced at DB level
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bug_hunter_active "
        "ON sessions(project_id) "
        "WHERE session_name = 'bug-hunter' AND status IN ('active', 'pending_auth')"
    )


# Mapping from version N to the coroutine that migrates N → N+1.
# Migration 0→1 is a no-op: the baseline DDL in _connect() produces schema v1.
_MIGRATIONS: dict[int, Any] = {
    0: None,  # baseline — no-op
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
    3: _migrate_3_to_4,
    4: _migrate_4_to_5,
    5: _migrate_5_to_6,
    6: _migrate_6_to_7,
    7: _migrate_7_to_8,
    8: _migrate_8_to_9,
    9: _migrate_9_to_10,
    10: _migrate_10_to_11,
    11: _migrate_11_to_12,
    12: _migrate_12_to_13,
    13: _migrate_13_to_14,
    14: _migrate_14_to_15,
    15: _migrate_15_to_16,
    16: _migrate_16_to_17,
    17: _migrate_17_to_18,
}


# ---------------------------------------------------------------------------
# Version queries & migration runner
# ---------------------------------------------------------------------------


async def get_schema_version(db: aiosqlite.Connection) -> int:
    """Return the current schema version, or 0 if the version table is empty."""
    async with db.execute("SELECT version FROM schema_version WHERE id = 1") as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def run_migrations(db: aiosqlite.Connection) -> int:
    """Apply any pending schema migrations and update the version row.

    Returns the schema version that was in place before migration ran.
    """
    # Use BEGIN IMMEDIATE to prevent concurrent migration races across processes.
    await db.execute("BEGIN IMMEDIATE")
    try:
        current = await get_schema_version(db)

        if current >= CURRENT_SCHEMA_VERSION:
            if current > CURRENT_SCHEMA_VERSION:
                logger.warning(
                    "DB schema v%d is newer than this code (v%d). "
                    "Likely from a different branch or newer install. "
                    "Continuing with existing schema.",
                    current,
                    CURRENT_SCHEMA_VERSION,
                )
            await db.execute("COMMIT")
            return current

        for version in range(current, CURRENT_SCHEMA_VERSION):
            migration = _MIGRATIONS.get(version)
            if migration is not None:
                await migration(db)
            logger.info("Applied DB migration %d → %d", version, version + 1)

        # Upsert the version row (PK id=1 enforces single row)
        await db.execute(
            "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)",
            (CURRENT_SCHEMA_VERSION,),
        )
        await db.execute("COMMIT")
        return current
    except Exception:
        await db.execute("ROLLBACK")
        raise
