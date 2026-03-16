"""Stop command logic for CLI."""

from __future__ import annotations

import click

from summon_claude.cli import daemon_client
from summon_claude.cli.helpers import resolve_or_pick, stop_and_report
from summon_claude.cli.interactive import format_session_option, interactive_select, is_interactive
from summon_claude.daemon import is_daemon_running


async def async_stop(ctx: click.Context, session: str | None, stop_all: bool) -> None:
    if not is_daemon_running():
        click.echo("Daemon is not running.")
        return

    try:
        if not session and not stop_all:
            if not is_interactive(ctx):
                click.echo("Provide a session name/ID or --all.", err=True)
                ctx.exit(1)
                return
            try:
                active = await daemon_client.list_sessions()
            except Exception as exc:
                click.echo(f"Error: {exc}", err=True)
                ctx.exit(1)
                return
            if not active:
                click.echo("No active sessions.")
                return
            if len(active) == 1:
                match = active[0]
                resolved_id = match["session_id"]
                label = match.get("session_name") or resolved_id[:8]
                click.echo(f"Auto-selecting {label} ({resolved_id[:8]})")
            else:
                options = [format_session_option(s) for s in active]
                result = interactive_select(options, "Select session to stop:", ctx)
                if result is None:
                    click.echo("No session selected.")
                    return
                resolved_id = active[result[1]]["session_id"]
            await stop_and_report(resolved_id)
            return

        if stop_all:
            results = await daemon_client.stop_all_sessions()
            if not results:
                click.echo("No active sessions.")
                return
            for sid, found in results:
                click.echo(f"Stop requested for {sid}: {'sent' if found else 'not found'}")
        else:
            resolved = await resolve_or_pick(session, ctx)  # type: ignore[arg-type]
            if not resolved:
                ctx.exit(1)
                return
            await stop_and_report(resolved["session_id"], suggest_cleanup=True)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        ctx.exit(1)
