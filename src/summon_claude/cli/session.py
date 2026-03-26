"""Session subcommand logic for CLI."""

from __future__ import annotations

import logging
import pathlib
import re
import shutil
import time

import click
from slack_sdk.web.async_client import AsyncWebClient

from summon_claude.cli import daemon_client
from summon_claude.cli.formatting import (
    echo,
    format_json,
    format_uptime,
    print_session_detail,
    print_session_table,
)
from summon_claude.cli.helpers import print_local_daemon_hint, resolve_or_pick
from summon_claude.cli.interactive import (
    LOG_PICKER_HEADER,
    format_log_option,
    format_session_option,
    interactive_multi_select,
    interactive_select,
    is_interactive,
)
from summon_claude.config import SummonConfig, get_data_dir
from summon_claude.daemon import is_daemon_running
from summon_claude.sessions.registry import SessionRegistry
from summon_claude.slack.client import ZZZ_PREFIX, make_zzz_name

logger = logging.getLogger(__name__)


async def async_session_list(
    ctx: click.Context,
    show_all: bool,
    output: str,
    *,
    name: str | None = None,
) -> None:
    # Print daemon status header (table mode only)
    if output == "table" and not ctx.obj.get("quiet"):
        if is_daemon_running():
            # Query daemon for live status
            try:
                status = await daemon_client.get_status()
                pid = status.get("pid", "?")
                uptime_s = status.get("uptime", 0)
                uptime_str = format_uptime(uptime_s)
                click.echo(f"Daemon: running (pid {pid}, uptime {uptime_str})")
            except Exception as e:
                click.echo(f"Daemon: running (status unavailable: {e})")
        else:
            click.echo("Daemon: not running")
            print_local_daemon_hint()

    async with SessionRegistry() as registry:
        if show_all:
            sessions = await registry.list_all(limit=50)
        else:
            sessions = await registry.list_active()
        if name:
            sessions = [s for s in sessions if s.get("session_name") == name]
        if not sessions:
            echo("No sessions found." if show_all else "No active sessions.", ctx)
            return
        if output == "json":
            click.echo(format_json(sessions))
        else:
            print_session_table(sessions, show_id=show_all)


async def session_info_impl(ctx: click.Context, session: str, output: str) -> None:
    """Resolve and show session detail."""
    record = await resolve_or_pick(session, ctx)
    if not record:
        return
    if output == "json":
        click.echo(format_json(record))
    else:
        print_session_detail(record)


async def session_logs_impl(ctx: click.Context, session: str | None, tail: int) -> None:
    """View logs with interactive picker."""
    log_dir = get_data_dir() / "logs"
    if not log_dir.exists():
        click.echo("No log files found.")
        return

    if session is None:
        log_files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not log_files:
            click.echo("No log files found.")
            return

        if not is_interactive(ctx):
            # Non-interactive: list and exit (preserve existing behavior)
            click.echo("Available session logs:")
            for lf in log_files:
                click.echo(f"  {lf.stem}")
            return

        # Build picker: daemon.log first, then by mtime
        ordered_paths: list[pathlib.Path] = []
        daemon_log = log_dir / "daemon.log"
        if daemon_log.exists():
            ordered_paths.append(daemon_log)
            log_files = [f for f in log_files if f.name != "daemon.log"]
        ordered_paths.extend(log_files)

        # Cross-reference with session registry for rich labels
        session_lookup: dict[str, dict] = {}
        try:
            async with SessionRegistry() as registry:
                for s in await registry.list_all(limit=500):
                    session_lookup[s["session_id"]] = s
        except Exception as exc:
            logger.debug("Could not load session metadata for log picker: %s", exc)

        options = [format_log_option(p, session_lookup.get(p.stem)) for p in ordered_paths]
        title = f"Select log file:\n  {LOG_PICKER_HEADER}"
        result = interactive_select(options, title, ctx)
        if result is None:
            click.echo("No log selected.")
            return
        log_file = ordered_paths[result[1]]
        lines = log_file.read_text().splitlines()
        _page_lines(lines[-tail:])
        return

    # Resolve partial ID, name, or channel name to full session_id
    resolved_id = session
    if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", session):
        session_record = await resolve_or_pick(session, ctx)
        if not session_record:
            return
        resolved_id = session_record["session_id"]

    log_file = log_dir / f"{resolved_id}.log"
    if not log_file.exists():
        click.echo(f"No log file found for session: {resolved_id[:8]}")
        return

    lines = log_file.read_text().splitlines()
    _page_lines(lines[-tail:])


def _page_lines(lines: list[str]) -> None:
    """Output lines, using the system pager only when output exceeds terminal height."""
    if not lines:
        click.echo("(empty log file)")
        return
    text = "\n".join(lines) + "\n"
    terminal_height = shutil.get_terminal_size().lines
    if len(lines) > terminal_height - 2:
        click.echo_via_pager(text)
    else:
        click.echo(text, nl=False)


async def async_session_cleanup(ctx: click.Context, *, archive: bool = False) -> None:
    async with SessionRegistry() as registry:
        stale = await registry.list_stale()
        pid_stale_ids = {s["session_id"] for s in stale}

        # Cross-reference with daemon: sessions in SQLite as "active" but
        # not tracked by the running daemon are also stale.
        if is_daemon_running():
            try:
                daemon_sessions = await daemon_client.list_sessions()
                daemon_sids = {s["session_id"] for s in daemon_sessions}
                active = await registry.list_active()
                for session in active:
                    sid = session["session_id"]
                    if sid not in daemon_sids and sid not in pid_stale_ids:
                        stale.append(session)
            except Exception as e:
                logger.debug("Could not cross-reference daemon sessions: %s", e)

        # Interactive: let user select which sessions to clean
        if stale and is_interactive(ctx) and not archive:
            options = []
            for session in stale:
                if session["session_id"] in pid_stale_ids:
                    reason = f"pid {session.get('pid', '?')} dead"
                else:
                    reason = "orphaned"
                options.append(format_session_option(session, annotation=reason))

            try:
                selected = interactive_multi_select(
                    options,
                    "Select sessions to clean up (space=toggle, enter=confirm):",
                    ctx,
                )
            except KeyboardInterrupt:
                click.echo("\nCleanup cancelled.")
                return

            if not selected:
                click.echo("No sessions selected.")
                return

            selected_indices = {idx for _, idx in selected}
            stale = [s for i, s in enumerate(stale) if i in selected_indices]

        if stale:
            # Best-effort Slack client for archiving/zzz-renaming channels
            slack_client = None
            try:
                config_path: str | None = ctx.obj.get("config_path") if ctx.obj else None
                config = SummonConfig.from_file(config_path)
                slack_client = AsyncWebClient(token=config.slack_bot_token)
            except Exception as e:
                logger.debug("Could not initialize Slack client for cleanup: %s", e)

            for session in stale:
                session_id = session["session_id"]
                channel_id = session.get("slack_channel_id")
                channel_name = session.get("slack_channel_name", "")

                if session_id in pid_stale_ids:
                    reason = f"Owner process (pid {session['pid']}) no longer running"
                else:
                    reason = "Not tracked by running daemon (orphaned)"

                # Archive the Slack channel only if --archive flag was passed
                if archive and slack_client and channel_id:
                    try:
                        await slack_client.conversations_archive(channel=channel_id)
                        logger.info(
                            "Archived channel %s for session %s",
                            channel_id,
                            session_id,
                        )
                    except Exception as e:
                        logger.debug("Could not archive channel %s: %s", channel_id, e)
                elif (
                    slack_client
                    and channel_id
                    and channel_name
                    and not channel_name.startswith(ZZZ_PREFIX)
                ):
                    # Rename stale channel with zzz- prefix
                    zzz_name = make_zzz_name(channel_name)
                    try:
                        await slack_client.conversations_rename(channel=channel_id, name=zzz_name)
                        logger.info(
                            "zzz-rename: #%s → #%s for stale session %s",
                            channel_name,
                            zzz_name,
                            session_id[:8],
                        )
                    except Exception as e:
                        logger.debug("Could not zzz-rename channel %s: %s", channel_id, e)

                await registry.mark_stale(session_id, reason)

            click.echo(f"Cleaned up {len(stale)} stale session(s).")
        else:
            echo("No stale sessions found.", ctx)

        # Clean up orphan log files (no matching session in registry)
        log_dir = get_data_dir() / "logs"
        if log_dir.exists():
            all_session_ids = {s["session_id"] for s in await registry.list_all(limit=10_000)}
            removed = 0
            recent_cutoff = time.time() - 60
            for log_file in log_dir.glob("*.log"):
                if log_file.name == "daemon.log":
                    continue
                if log_file.stem not in all_session_ids:
                    try:
                        if log_file.stat().st_mtime < recent_cutoff:
                            log_file.unlink()
                            removed += 1
                    except OSError:
                        pass
            # Remove rotated daemon logs older than 7 days
            week_ago = time.time() - 7 * 86400
            for rotated in log_dir.glob("daemon.log.*"):
                try:
                    if rotated.stat().st_mtime < week_ago:
                        rotated.unlink()
                        removed += 1
                except OSError:
                    pass
            if removed:
                click.echo(f"Removed {removed} orphan log file(s).")
