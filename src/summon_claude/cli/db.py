"""Database maintenance command logic for CLI."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import click

from summon_claude.cli.formatting import echo
from summon_claude.sessions.registry import (
    CURRENT_SCHEMA_VERSION,
    SessionRegistry,
    _get_schema_version,
)


async def async_db_status(ctx: click.Context) -> None:
    previous: int | None = None
    version = 0
    integrity = "unknown"
    sessions_count = 0
    audit_count = 0
    spawn_count = 0
    async with SessionRegistry() as reg:
        previous = reg.migrated_from
        db = reg.db
        version = await _get_schema_version(db)
        async with db.execute("PRAGMA integrity_check") as cursor:
            row = await cursor.fetchone()
            integrity = row[0] if row else "unknown"
        async with db.execute("SELECT COUNT(*) FROM sessions") as cur:
            row = await cur.fetchone()
            sessions_count = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM audit_log") as cur:
            row = await cur.fetchone()
            audit_count = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM spawn_tokens") as cur:
            row = await cur.fetchone()
            spawn_count = row[0] if row else 0

    if previous is not None and previous < CURRENT_SCHEMA_VERSION:
        echo(f"Migrated schema from version {previous} → {version}", ctx)
    else:
        echo(f"Schema version: {version} (current)", ctx)
    echo(f"Integrity: {integrity}", ctx)
    echo(f"Sessions: {sessions_count}, Audit log: {audit_count}, Spawn tokens: {spawn_count}", ctx)


async def async_db_reset(db_path: pathlib.Path, ctx: click.Context) -> None:
    async with SessionRegistry(db_path=db_path):
        pass
    echo(f"Database recreated at {db_path}", ctx)
    echo(f"Schema version: {CURRENT_SCHEMA_VERSION}", ctx)


async def async_db_vacuum(db_path: pathlib.Path, ctx: click.Context) -> None:
    integrity = "unknown"
    async with SessionRegistry(db_path=db_path) as reg:
        db = reg.db
        async with db.execute("PRAGMA integrity_check") as cursor:
            result = await cursor.fetchone()
            integrity = result[0] if result else "unknown"
        await db.execute("VACUUM")
    echo(f"Integrity: {integrity}", ctx)


async def async_db_purge(older_than: int, ctx: click.Context) -> None:
    cutoff = (datetime.now(UTC) - timedelta(days=older_than)).isoformat()

    sessions_deleted = 0
    audit_deleted = 0
    tokens_deleted = 0
    spawn_deleted = 0
    async with SessionRegistry() as reg:
        db = reg.db
        await db.execute("BEGIN")
        try:
            async with db.execute(
                "DELETE FROM sessions WHERE started_at < ? AND status IN ('completed', 'errored')",
                (cutoff,),
            ) as cur:
                sessions_deleted = cur.rowcount
            async with db.execute(
                "DELETE FROM audit_log WHERE timestamp < ?",
                (cutoff,),
            ) as cur:
                audit_deleted = cur.rowcount
            async with db.execute(
                "DELETE FROM pending_auth_tokens WHERE expires_at < ?",
                (cutoff,),
            ) as cur:
                tokens_deleted = cur.rowcount
            async with db.execute(
                "DELETE FROM spawn_tokens WHERE expires_at < ?",
                (cutoff,),
            ) as cur:
                spawn_deleted = cur.rowcount
            await db.execute("COMMIT")
        except Exception:
            await db.execute("ROLLBACK")
            raise

    echo(f"Purged records older than {older_than} days (before {cutoff[:10]}):", ctx)
    echo(f"  Sessions:     {sessions_deleted}", ctx)
    echo(f"  Audit log:    {audit_deleted}", ctx)
    echo(f"  Auth tokens:  {tokens_deleted}", ctx)
    echo(f"  Spawn tokens: {spawn_deleted}", ctx)
