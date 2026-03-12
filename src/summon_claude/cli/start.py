"""Start command logic for CLI."""

from __future__ import annotations

import click

from summon_claude.cli import daemon_client
from summon_claude.config import SummonConfig
from summon_claude.daemon import start_daemon
from summon_claude.sessions.registry import CURRENT_SCHEMA_VERSION, SessionRegistry
from summon_claude.sessions.session import SessionOptions


async def async_start(
    config: SummonConfig,
    cwd: str,
    name: str,
    model: str | None,
    resume: str | None,
) -> str:
    """Orchestrate daemon startup, session creation, and return the short_code."""
    options = SessionOptions(
        cwd=cwd,
        name=name,
        model=model or config.default_model,
        resume=resume,
    )

    # Phase 0: Run DB migration if needed (fast, ~10ms)
    async with SessionRegistry() as reg:
        if reg.migrated_from is not None and reg.migrated_from < CURRENT_SCHEMA_VERSION:
            click.echo(
                f"Database schema upgraded: v{reg.migrated_from} → v{CURRENT_SCHEMA_VERSION}",
                err=True,
            )

    # Phase 1: Ensure daemon is running (auto-start if not)
    try:
        start_daemon(config)
    except Exception as e:
        click.echo(f"Error starting daemon: {e}", err=True)
        raise SystemExit(1) from e

    # Phase 2: Send create_session to daemon; daemon generates session_id + auth
    try:
        short_code = await daemon_client.create_session(options)
    except Exception as e:
        click.echo(f"Error communicating with daemon: {e}", err=True)
        raise SystemExit(1) from e

    return short_code
