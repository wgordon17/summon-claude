"""Stop command logic for CLI."""

from __future__ import annotations

import logging
from typing import Any

import click

from summon_claude.cli import daemon_client
from summon_claude.cli.helpers import print_local_daemon_hint, resolve_or_pick, stop_and_report
from summon_claude.cli.interactive import format_session_option, interactive_select, is_interactive
from summon_claude.daemon import is_daemon_running
from summon_claude.sessions.registry import SessionRegistry

logger = logging.getLogger(__name__)


async def _check_pm_stop(session: dict[str, Any], ctx: click.Context) -> bool:
    """If *session* is a PM, warn about orphaned children. Returns True to proceed."""
    project_id = session.get("project_id")
    sname = session.get("session_name", "")
    if not project_id or "-pm-" not in sname:
        return True

    siblings: list[dict[str, Any]] = []
    async with SessionRegistry() as registry:
        siblings = await registry.get_project_sessions(project_id)
    active_children = [
        s
        for s in siblings
        if s["session_id"] != session["session_id"]
        and s.get("status") in ("pending_auth", "active")
        and "-pm-" not in (s.get("session_name") or "")
    ]
    if not active_children:
        return True

    n = len(active_children)
    click.echo(
        f"Warning: this PM has {n} running subsession{'s' if n != 1 else ''}. "
        "Stopping it will orphan them."
    )
    if not is_interactive(ctx):
        click.echo("Use 'summon project down' for a clean cascading shutdown.", err=True)
        return False
    return click.confirm("Continue?", default=False)


async def _notify_pm_of_child_stop(session: dict[str, Any]) -> None:
    """Best-effort: post a notification to the PM's channel when a child session is stopped."""
    project_id = session.get("project_id")
    sname = session.get("session_name", "")
    if not project_id or "-pm-" in sname:
        return  # not a child session

    project: dict[str, Any] | None = None
    async with SessionRegistry() as registry:
        project = await registry.get_project(project_id)
    if not project:
        return
    pm_channel_id = project.get("pm_channel_id")
    if not pm_channel_id:
        return

    label = sname or session.get("session_id", "?")[:8]
    try:
        from slack_sdk.web.async_client import AsyncWebClient  # noqa: PLC0415

        from summon_claude.config import SummonConfig  # noqa: PLC0415

        config = SummonConfig.from_file()
        web = AsyncWebClient(token=config.slack_bot_token)
        await web.chat_postMessage(
            channel=pm_channel_id,
            text=f":information_source: Subsession *{label}* was stopped by the user via CLI.",
        )
    except Exception:
        logger.debug("Failed to notify PM channel %s about child stop", pm_channel_id)


async def async_stop(ctx: click.Context, session: str | None, stop_all: bool) -> None:
    if not is_daemon_running():
        click.echo("Daemon is not running.")
        print_local_daemon_hint()
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
            # Enrich sparse daemon dict with full registry data for PM-awareness
            picked: dict[str, Any] | None = None
            async with SessionRegistry() as registry:
                picked = await registry.get_session(resolved_id)
            if picked and not await _check_pm_stop(picked, ctx):
                return
            await stop_and_report(resolved_id)
            if picked:
                await _notify_pm_of_child_stop(picked)
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
            if not await _check_pm_stop(resolved, ctx):
                ctx.exit(1)
                return
            await stop_and_report(resolved["session_id"], suggest_cleanup=True)
            await _notify_pm_of_child_stop(resolved)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        ctx.exit(1)
