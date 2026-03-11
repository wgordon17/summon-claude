"""Session resolution and stop helpers for CLI commands."""

from __future__ import annotations

import asyncio

import click

from summon_claude.cli import daemon_client
from summon_claude.cli.interactive import format_session_option, interactive_select
from summon_claude.sessions.registry import SessionRegistry


async def resolve_session(identifier: str) -> tuple[dict | None, list[dict]]:
    """Look up a session by ID prefix or channel name (async registry query)."""
    async with SessionRegistry() as registry:
        return await registry.resolve_session(identifier)


def resolve_session_or_exit(identifier: str, ctx: click.Context) -> dict | None:
    """Resolve a session identifier, with interactive disambiguation.

    Returns the resolved session dict, or ``None`` if not found.
    """
    session, matches = asyncio.run(resolve_session(identifier))
    if not session:
        if matches:
            session = pick_session(identifier, matches, ctx)
        else:
            click.echo(f"Session not found: {identifier}")
            return None
    return session


def pick_session(identifier: str, matches: list[dict], ctx: click.Context) -> dict | None:
    """Interactively disambiguate when a session identifier matches multiple sessions."""
    options = [format_session_option(m) for m in matches]
    result = interactive_select(options, f"'{identifier}' matches {len(matches)} sessions:", ctx)
    if result is None:
        click.echo("No session selected.")
        return None
    return matches[result[1]]


async def stop_and_report(resolved_id: str, *, suggest_cleanup: bool = False) -> None:
    """Stop a session via the daemon and report the result."""
    found = await daemon_client.stop_session(resolved_id)
    if found:
        click.echo(f"Stop requested for session {resolved_id[:8]}")
    else:
        msg = f"Session {resolved_id[:8]} not owned by running daemon."
        if suggest_cleanup:
            msg += " Run 'summon session cleanup' to clear stale sessions."
        click.echo(msg)
