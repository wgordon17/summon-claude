"""CLI entry point for summon-claude."""

# pyright: reportFunctionMemberAccess=false, reportArgumentType=false, reportReturnType=false, reportCallIssue=false
# click decorators: https://github.com/pallets/click/issues/2255
# slack_sdk doesn't ship type stubs; pydantic-settings metaclass gaps

# Naming conventions:
# - Top-level click commands: cmd_X
# - Group subcommands: group_subcommand (+ _cmd suffix for import shadowing)
# - Async helpers mirroring a click command: _async_<click_function_name>
# - Utility helpers: verb-noun descriptive names (e.g., _ensure_daemon)

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import re
import sys
import threading
from datetime import datetime

import click
from slack_sdk.web.async_client import AsyncWebClient

from summon_claude import __version__
from summon_claude.cli import daemon_client
from summon_claude.cli.config import config_check, config_edit, config_path, config_set, config_show
from summon_claude.cli.interactive import (
    format_log_option,
    format_session_option,
    interactive_multi_select,
    interactive_select,
    is_interactive,
)
from summon_claude.config import SummonConfig, get_config_dir, get_config_file, get_data_dir
from summon_claude.daemon import is_daemon_running, start_daemon
from summon_claude.sessions.registry import SessionRegistry
from summon_claude.sessions.session import SessionOptions

logger = logging.getLogger(__name__)

_BANNER_WIDTH = 50


def _print_auth_banner(short_code: str) -> None:
    """Print the auth code to the terminal before daemonization."""
    border = "=" * _BANNER_WIDTH
    click.echo(f"\n{border}")
    click.echo(f"  SUMMON CODE: {short_code}")
    click.echo(f"  Type in Slack: /summon {short_code}")
    click.echo("  Expires in 5 minutes")
    click.echo(f"{border}\n")


def _setup_logging(verbose: bool = False) -> None:
    """Configure logging. Idempotent — safe to call multiple times."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")

    # Add console handler only if one doesn't exist yet
    has_console = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )
    if not has_console:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG if verbose else logging.WARNING)
        console.setFormatter(fmt)
        root.addHandler(console)

    if not verbose:
        logging.getLogger("asyncio").setLevel(logging.WARNING)


def _echo(msg: str, ctx: click.Context, err: bool = False) -> None:
    if err or not ctx.obj.get("quiet"):
        click.echo(msg, err=err)


def _format_json(data: list[dict] | dict) -> str:
    return json.dumps(data, indent=2, default=str)


class AliasedGroup(click.Group):
    """Click group with command alias support."""

    _ALIASES: dict[str, str] = {"s": "session"}

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        rv = click.Group.get_command(self, ctx, cmd_name)
        if rv is not None:
            return rv
        canonical = self._ALIASES.get(cmd_name)
        if canonical:
            return click.Group.get_command(self, ctx, canonical)
        return None

    def resolve_command(self, ctx: click.Context, args: list[str]) -> tuple:
        cmd_name = args[0] if args else ""
        canonical = self._ALIASES.get(cmd_name, cmd_name)
        return super().resolve_command(ctx, [canonical, *args[1:]])


@click.group(cls=AliasedGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="summon")
@click.option(
    "-v", "--verbose", is_eager=True, is_flag=True, default=False, help="Enable verbose logging"
)
@click.option("-q", "--quiet", is_flag=True, default=False, help="Suppress non-essential output")
@click.option("--no-color", is_flag=True, default=False, help="Disable colored output")
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Override config file path",
)
@click.option("--no-interactive", is_flag=True, default=False, help="Disable interactive prompts")
@click.pass_context
def cli(
    ctx: click.Context,
    verbose: bool,
    quiet: bool,
    no_color: bool,
    config_path: str | None,
    no_interactive: bool,
) -> None:
    """Bridge Claude Code sessions to Slack channels."""
    if verbose and quiet:
        raise click.UsageError("--verbose and --quiet are mutually exclusive")

    _setup_logging(verbose)

    if no_color or os.environ.get("NO_COLOR", ""):
        ctx.color = False

    ctx.ensure_object(dict)
    ctx.obj["quiet"] = quiet
    ctx.obj["config_path"] = config_path
    ctx.obj["no_interactive"] = no_interactive


@cli.command("version")
@click.option(
    "-o",
    "--output",
    type=click.Choice(["json", "table"]),
    default="table",
    help="Output format",
)
@click.pass_context
def cmd_version(ctx: click.Context, output: str) -> None:
    """Show extended version and environment information."""
    config_file = get_config_file(ctx.obj.get("config_path"))
    data_dir = get_data_dir()
    db_path = data_dir / "registry.db"

    info = {
        "version": __version__,
        "python": sys.version,
        "platform": sys.platform,
        "config_file": str(config_file),
        "data_dir": str(data_dir),
        "db_path": str(db_path),
    }

    if output == "json":
        click.echo(json.dumps(info, indent=2))
    else:
        click.echo(f"summon, version {__version__}")
        click.echo(f"Python:      {sys.version}")
        click.echo(f"Platform:    {sys.platform}")
        click.echo(f"Config file: {config_file}")
        click.echo(f"Data dir:    {data_dir}")
        click.echo(f"DB path:     {db_path}")


@cli.command("start")
@click.option(
    "--cwd", default=None, help="Working directory for Claude (default: current directory)"
)
@click.option(
    "--resume",
    metavar="SESSION_ID",
    default=None,
    help="Resume an existing Claude Code session by ID",
)
@click.option("--name", default=None, help="Session name (used for Slack channel naming)")
@click.option("--model", default=None, help="Model override (default: from config)")
@click.pass_context
def cmd_start(
    ctx: click.Context,
    cwd: str | None,
    resume: str | None,
    name: str | None,
    model: str | None,
) -> None:
    """Start a new summon session (thin client — delegates to the daemon)."""
    import shutil  # noqa: PLC0415

    if not shutil.which("claude"):
        click.echo("Error: Claude CLI not found in PATH.", err=True)
        click.echo("", err=True)
        click.echo("Install Claude Code: https://claude.ai/code", err=True)
        raise SystemExit(1)

    config_path_override = ctx.obj.get("config_path") if ctx.obj else None
    try:
        config = SummonConfig.from_file(config_path_override)
        config.validate()
    except Exception as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)

    # Background update check (non-blocking)
    update_result: list[str] = []

    def _bg_update_check() -> None:
        from summon_claude.cli.update_check import (  # noqa: PLC0415
            check_for_update,
            format_update_message,
        )

        info = check_for_update()
        if info:
            update_result.append(format_update_message(info))

    update_thread = threading.Thread(target=_bg_update_check, daemon=True)
    update_thread.start()

    resolved_cwd = str(pathlib.Path(cwd).resolve()) if cwd else str(pathlib.Path.cwd())
    resolved_name = name or pathlib.Path(resolved_cwd).name

    try:
        asyncio.run(_async_cmd_start(config, resolved_cwd, resolved_name, model, resume))
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    # Show update notification if available (after banner)
    update_thread.join(timeout=4.0)
    if update_result and not ctx.obj.get("quiet"):
        click.echo(update_result[0], err=True)


async def _async_cmd_start(
    config: SummonConfig,
    cwd: str,
    name: str,
    model: str | None,
    resume: str | None,
) -> None:
    """Orchestrate daemon startup, session creation, and auth banner display."""
    options = SessionOptions(
        cwd=cwd,
        name=name,
        model=model or config.default_model,
        resume=resume,
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

    # Phase 3: Print auth banner so user can authenticate via Slack
    _print_auth_banner(short_code)


# ---------------------------------------------------------------------------
# Top-level stop command
# ---------------------------------------------------------------------------


@cli.command("stop")
@click.argument("session_id", required=False, default=None)
@click.option(
    "--all",
    "-a",
    "stop_all",
    is_flag=True,
    default=False,
    help="Stop all active sessions",
)
@click.pass_context
def cmd_stop(ctx: click.Context, session_id: str | None, stop_all: bool) -> None:
    """Stop a session (or all sessions) via the daemon."""
    asyncio.run(_async_cmd_stop(ctx, session_id, stop_all))


async def _resolve_session(identifier: str) -> tuple[dict | None, list[dict]]:
    """Look up a session by ID prefix or channel name (async registry query)."""
    async with SessionRegistry() as registry:
        return await registry.resolve_session(identifier)


def _resolve_session_or_exit(identifier: str, ctx: click.Context) -> dict | None:
    """Resolve a session identifier, with interactive disambiguation.

    Returns the resolved session dict, or ``None`` if not found.
    """
    session, matches = asyncio.run(_resolve_session(identifier))
    if not session:
        if matches:
            session = _pick_session(identifier, matches, ctx)
        else:
            click.echo(f"Session not found: {identifier}")
            return None
    return session


def _pick_session(identifier: str, matches: list[dict], ctx: click.Context) -> dict | None:
    """Interactively disambiguate when a session identifier matches multiple sessions."""
    options = [format_session_option(m) for m in matches]
    result = interactive_select(options, f"'{identifier}' matches {len(matches)} sessions:", ctx)
    if result is None:
        click.echo("No session selected.")
        return None
    return matches[result[1]]


async def _async_cmd_stop(ctx: click.Context, session_id: str | None, stop_all: bool) -> None:
    if not is_daemon_running():
        click.echo("Daemon is not running.")
        return

    try:
        if not session_id and not stop_all:
            if not is_interactive(ctx):
                click.echo("Provide SESSION_ID or --all.", err=True)
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
                session = active[0]
                resolved_id = session["session_id"]
                label = session.get("session_name") or resolved_id[:8]
                click.echo(f"Auto-selecting {label} ({resolved_id[:8]})")
            else:
                options = [format_session_option(s) for s in active]
                result = interactive_select(options, "Select session to stop:", ctx)
                if result is None:
                    click.echo("No session selected.")
                    return
                resolved_id = active[result[1]]["session_id"]
            found = await daemon_client.stop_session(resolved_id)
            if found:
                click.echo(f"Stop requested for session {resolved_id[:8]}")
            else:
                click.echo(f"Session {resolved_id[:8]} not owned by running daemon.")
            return

        if stop_all:
            results = await daemon_client.stop_all_sessions()
            if not results:
                click.echo("No active sessions.")
                return
            for sid, found in results:
                click.echo(f"Stop requested for {sid}: {'sent' if found else 'not found'}")
        else:
            session, matches = await _resolve_session(session_id)  # type: ignore[arg-type]
            if not session:
                if matches:
                    session = _pick_session(session_id, matches, ctx)
                else:
                    click.echo(f"Session not found: {session_id}")
                    ctx.exit(1)
                    return
            if not session:
                ctx.exit(1)
                return

            resolved_id = session["session_id"]
            found = await daemon_client.stop_session(resolved_id)
            if found:
                click.echo(f"Stop requested for session {resolved_id[:8]}")
            else:
                click.echo(
                    f"Session {resolved_id[:8]} not owned by running daemon."
                    " Run 'summon session cleanup' to clear stale sessions."
                )
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        ctx.exit(1)


# ---------------------------------------------------------------------------
# session command group (alias: s)
# ---------------------------------------------------------------------------


@cli.group("session")
def cmd_session() -> None:
    """Manage summon sessions."""


@cmd_session.command("list")
@click.option(
    "--all",
    "-a",
    "show_all",
    is_flag=True,
    default=False,
    help="Show all recent sessions (not just active)",
)
@click.option(
    "-o",
    "--output",
    type=click.Choice(["json", "table"]),
    default="table",
    help="Output format",
)
@click.pass_context
def session_list(ctx: click.Context, show_all: bool, output: str) -> None:
    """List sessions. Shows active sessions by default; use --all for all recent."""
    asyncio.run(_async_session_list(ctx, show_all, output))


async def _async_session_list(ctx: click.Context, show_all: bool, output: str) -> None:
    # Print daemon status header (table mode only)
    if output == "table" and not ctx.obj.get("quiet"):
        if is_daemon_running():
            # Query daemon for live status
            try:
                status = await daemon_client.get_status()
                pid = status.get("pid", "?")
                uptime_s = status.get("uptime", 0)
                uptime_str = _format_uptime(uptime_s)
                click.echo(f"Daemon: running (pid {pid}, uptime {uptime_str})")
            except Exception as e:
                click.echo(f"Daemon: running (status unavailable: {e})")
        else:
            click.echo("Daemon: not running")

    async with SessionRegistry() as registry:
        if show_all:
            sessions = await registry.list_all(limit=50)
        else:
            sessions = await registry.list_active()
        if not sessions:
            _echo("No sessions found." if show_all else "No active sessions.", ctx)
            return
        if output == "json":
            click.echo(_format_json(sessions))
        else:
            _print_session_table(sessions)


@cmd_session.command("info")
@click.argument("session_id")
@click.option(
    "-o",
    "--output",
    type=click.Choice(["json", "table"]),
    default="table",
    help="Output format",
)
@click.pass_context
def session_info(ctx: click.Context, session_id: str, output: str) -> None:
    """Show detailed information for a specific session."""
    session = _resolve_session_or_exit(session_id, ctx)
    if not session:
        return
    if output == "json":
        click.echo(_format_json(session))
    else:
        _print_session_detail(session)


@cmd_session.command("logs")
@click.argument("session_id", required=False, default=None)
@click.option("--tail", "-n", default=50, help="Number of lines to show (default: 50)")
@click.pass_context
def session_logs(ctx: click.Context, session_id: str | None, tail: int) -> None:
    """Show session logs. Pass SESSION_ID for a specific session, or list available logs."""
    log_dir = get_data_dir() / "logs"
    if not log_dir.exists():
        click.echo("No log files found.")
        return

    if session_id is None:
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

        if len(ordered_paths) == 1:
            log_file = ordered_paths[0]
        else:
            options = [format_log_option(p) for p in ordered_paths]
            result = interactive_select(options, "Select log file:", ctx)
            if result is None:
                click.echo("No log selected.")
                return
            log_file = ordered_paths[result[1]]
        lines = log_file.read_text().splitlines()
        for line in lines[-tail:]:
            click.echo(line)
        return

    # Resolve partial ID or channel name to full session_id
    resolved_id = session_id
    if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", session_id):
        session_record = _resolve_session_or_exit(session_id, ctx)
        if session_record is None:
            return
        resolved_id = session_record["session_id"]

    log_file = log_dir / f"{resolved_id}.log"
    if not log_file.exists():
        click.echo(f"No log file found for session: {resolved_id[:8]}")
        return

    lines = log_file.read_text().splitlines()
    for line in lines[-tail:]:
        click.echo(line)


@cmd_session.command("cleanup")
@click.option(
    "--archive",
    is_flag=True,
    default=False,
    help="Archive Slack channels of stale sessions (channels are preserved by default)",
)
@click.pass_context
def session_cleanup(ctx: click.Context, archive: bool) -> None:
    """Mark sessions with dead processes as errored."""
    asyncio.run(_async_session_cleanup(ctx, archive=archive))


async def _async_session_cleanup(ctx: click.Context, *, archive: bool = False) -> None:
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

        if not stale:
            _echo("No stale sessions found.", ctx)
            return

        # Try to construct a Slack client for archiving channels (best-effort, only if --archive)
        slack_client = None
        if archive:
            try:
                config_path: str | None = ctx.obj.get("config_path") if ctx.obj else None
                config = SummonConfig.from_file(config_path)
                slack_client = AsyncWebClient(token=config.slack_bot_token)
            except Exception as e:
                logger.debug("Could not initialize Slack client for cleanup: %s", e)

        for session in stale:
            session_id = session["session_id"]
            channel_id = session.get("slack_channel_id")

            if session_id in pid_stale_ids:
                reason = f"Owner process (pid {session['pid']}) no longer running"
            else:
                reason = "Not tracked by running daemon (orphaned)"

            # Archive the Slack channel only if --archive flag was passed
            if archive and slack_client and channel_id:
                try:
                    await slack_client.conversations_archive(channel=channel_id)
                    logger.info("Archived channel %s for stale session %s", channel_id, session_id)
                except Exception as e:
                    logger.debug("Could not archive channel %s: %s", channel_id, e)

            await registry.mark_stale(session_id, reason)

        click.echo(f"Cleaned up {len(stale)} stale session(s).")


# ---------------------------------------------------------------------------
# Top-level commands: init, config
# ---------------------------------------------------------------------------


@cli.command("init")
@click.pass_context
def cmd_init(ctx: click.Context) -> None:
    """Interactive setup wizard for summon-claude configuration."""
    click.echo("Setting up summon-claude configuration...")
    click.echo()

    bot_token = click.prompt("  Slack Bot Token (xoxb-...)", hide_input=True)
    while not bot_token.startswith("xoxb-"):
        click.echo("  Error: Bot token must start with 'xoxb-'")
        bot_token = click.prompt("  Slack Bot Token (xoxb-...)", hide_input=True)

    app_token = click.prompt("  Slack App Token (xapp-...)", hide_input=True)
    while not app_token.startswith("xapp-"):
        click.echo("  Error: App token must start with 'xapp-'")
        app_token = click.prompt("  Slack App Token (xapp-...)", hide_input=True)

    signing_secret = click.prompt("  Slack Signing Secret", hide_input=True)

    config_path_override = ctx.obj.get("config_path") if ctx.obj else None
    if config_path_override:
        config_file = pathlib.Path(config_path_override)
        config_file.parent.mkdir(parents=True, exist_ok=True)
    else:
        config_dir = get_config_dir()
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = get_config_file()

    lines = [
        f"SUMMON_SLACK_BOT_TOKEN={bot_token}",
        f"SUMMON_SLACK_APP_TOKEN={app_token}",
        f"SUMMON_SLACK_SIGNING_SECRET={signing_secret}",
    ]
    config_file.write_text("\n".join(lines) + "\n")
    # Restrict config file to owner-only access (0600)
    with contextlib.suppress(OSError):
        config_file.chmod(0o600)

    click.echo()
    click.echo(f"Configuration saved to {config_file}")


@cli.group("config")
def cmd_config() -> None:
    """Manage summon-claude configuration."""


@cmd_config.command("show")
@click.pass_context
def config_show_cmd(ctx: click.Context) -> None:
    """Show current configuration (tokens masked)."""
    config_path_override = ctx.obj.get("config_path") if ctx.obj else None
    config_show(config_path_override)


@cmd_config.command("path")
@click.pass_context
def config_path_cmd(ctx: click.Context) -> None:
    """Print the config file path."""
    config_path_override = ctx.obj.get("config_path") if ctx.obj else None
    config_path(config_path_override)


@cmd_config.command("check")
@click.pass_context
def config_check_cmd(ctx: click.Context) -> None:
    """Validate configuration and check connectivity."""
    quiet = ctx.obj.get("quiet", False) if ctx.obj else False
    config_path_override = ctx.obj.get("config_path") if ctx.obj else None
    all_pass = config_check(quiet=quiet, config_path=config_path_override)
    if not all_pass:
        sys.exit(1)


@cmd_config.command("edit")
@click.pass_context
def config_edit_cmd(ctx: click.Context) -> None:
    """Open config file in $EDITOR."""
    config_path_override = ctx.obj.get("config_path") if ctx.obj else None
    config_edit(config_path_override)


@cmd_config.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_set_cmd(ctx: click.Context, key: str, value: str) -> None:
    """Set a configuration value (e.g. SUMMON_SLACK_BOT_TOKEN)."""
    config_path_override = ctx.obj.get("config_path") if ctx.obj else None
    config_set(key, value, config_path_override)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _print_session_table(sessions: list[dict]) -> None:
    """Print a compact table of sessions."""
    if not sessions:
        return

    headers = ["ID", "STATUS", "NAME", "CHANNEL", "CWD"]
    rows: list[list[str]] = []
    for s in sessions:
        session_id = s.get("session_id", "")
        # Show first 8 chars of UUID — enough to be unique and passable to `stop`
        short_id = session_id[:8] if session_id else "-"
        rows.append(
            [
                short_id,
                s.get("status", "?"),
                s.get("session_name") or "-",
                s.get("slack_channel_name") or "-",
                s.get("cwd", ""),
            ]
        )

    # Fixed-width for all columns except CWD (last), which wraps freely
    fixed = headers[:-1]
    col_widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(fixed)]
    prefix_fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    click.echo(f"{prefix_fmt.format(*fixed)}  {headers[-1]}")
    click.echo("  ".join("-" * w for w in col_widths) + "  " + "-" * len(headers[-1]))
    for row in rows:
        click.echo(f"{prefix_fmt.format(*row[:-1])}  {row[-1]}")


def _print_session_detail(session: dict) -> None:
    """Print detailed info for a single session."""
    fields = [
        ("Session ID", session.get("session_id", "")),
        ("Status", session.get("status", "")),
        ("Name", session.get("session_name") or "-"),
        ("PID", str(session.get("pid", ""))),
        ("CWD", session.get("cwd", "")),
        ("Model", session.get("model") or "-"),
        ("Channel ID", session.get("slack_channel_id") or "-"),
        ("Channel", session.get("slack_channel_name") or "-"),
        ("Claude Session", session.get("claude_session_id") or "-"),
        ("Started", _format_ts(session.get("started_at"))),
        ("Authenticated", _format_ts(session.get("authenticated_at"))),
        ("Last Activity", _format_ts(session.get("last_activity_at"))),
        ("Ended", _format_ts(session.get("ended_at"))),
        ("Turns", str(session.get("total_turns", 0))),
        ("Total Cost", f"${session.get('total_cost_usd', 0.0) or 0.0:.4f}"),
    ]
    if session.get("error_message"):
        fields.append(("Error", session["error_message"]))

    max_key = max(len(k) for k, _ in fields)
    for key, val in fields:
        click.echo(f"  {key.ljust(max_key)} : {val}")


def _format_ts(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts


def _format_uptime(seconds: float) -> str:
    """Format a duration in seconds as a human-readable uptime string."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def main() -> None:
    cli()
