"""Schema migrations for the session registry.

All schema changes after the v1 baseline live here as migration functions.
Fresh databases create the v1 baseline DDL, then run all migrations — no
schema change is ever duplicated between _connect() and a migration.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 6


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


# Mapping from version N to the coroutine that migrates N → N+1.
# Migration 0→1 is a no-op: the baseline DDL in _connect() produces schema v1.
_MIGRATIONS: dict[int, Any] = {
    0: None,  # baseline — no-op
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
    3: _migrate_3_to_4,
    4: _migrate_4_to_5,
    5: _migrate_5_to_6,
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
