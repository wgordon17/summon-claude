"""CLI entry point for summon-claude."""

# pyright: reportFunctionMemberAccess=false, reportArgumentType=false, reportReturnType=false, reportCallIssue=false
# click decorators: https://github.com/pallets/click/issues/2255
# slack_sdk doesn't ship type stubs; pydantic-settings metaclass gaps

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
from datetime import UTC, datetime

import click
from slack_sdk.web.async_client import AsyncWebClient

from summon_claude import __version__, daemon_client
from summon_claude.channel_manager import ChannelManager
from summon_claude.cli_config import config_check, config_edit, config_path, config_set, config_show
from summon_claude.config import SummonConfig, get_config_dir, get_config_file, get_data_dir
from summon_claude.daemon_client import DaemonError
from summon_claude.providers.slack import SlackChatProvider
from summon_claude.registry import SessionRegistry
from summon_claude.session import SessionOptions

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


def _setup_logging(verbose: bool = False, log_file: pathlib.Path | None = None) -> None:
    """Configure logging. Idempotent — safe to call multiple times.

    First call (from cli()): sets up console handler only.
    Second call (from cmd_start()): adds file handler for session diagnostics.
    """
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

    # Add file handler if requested and not already attached
    if log_file:
        has_file = any(isinstance(h, logging.FileHandler) for h in root.handlers)
        if not has_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_file)
            fh.setLevel(logging.DEBUG if verbose else logging.INFO)
            fh.setFormatter(fmt)
            root.addHandler(fh)

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
@click.pass_context
def cli(
    ctx: click.Context,
    verbose: bool,
    quiet: bool,
    no_color: bool,
    config_path: str | None,
) -> None:
    """Bridge Claude Code sessions to Slack channels."""
    if verbose and quiet:
        raise click.UsageError("--verbose and --quiet are mutually exclusive")

    _setup_logging(verbose)

    if no_color or os.environ.get("NO_COLOR", ""):
        ctx.color = False

    ctx.ensure_object(dict)
    ctx.obj["quiet"] = quiet
    ctx.obj["verbose"] = verbose
    ctx.obj["no_color"] = no_color or bool(os.environ.get("NO_COLOR", ""))
    ctx.obj["config_path"] = config_path


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
        from summon_claude.update_check import (  # noqa: PLC0415
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

    options = SessionOptions(
        session_id="",  # assigned by daemon
        cwd=resolved_cwd,
        name=resolved_name,
        model=model or config.default_model,
        resume=resume,
    )

    # Phase 1: Ensure daemon is running (auto-start if not)
    try:
        _ensure_daemon(config)
    except Exception as e:
        click.echo(f"Error starting daemon: {e}", err=True)
        sys.exit(1)

    # Phase 2: Send create_session to daemon; daemon generates session_id + auth
    try:
        _session_id, short_code = asyncio.run(_create_session_in_daemon(options))
    except Exception as e:
        click.echo(f"Error communicating with daemon: {e}", err=True)
        sys.exit(1)

    # Phase 3: Print auth banner so user can authenticate via Slack
    _print_auth_banner(short_code)

    # Show update notification if available (after banner)
    update_thread.join(timeout=4.0)
    if update_result and not ctx.obj.get("quiet"):
        click.echo(update_result[0], err=True)


def _ensure_daemon(config: SummonConfig) -> None:
    """Start the daemon if it is not already running."""
    from summon_claude.daemon import is_daemon_running, start_daemon  # noqa: PLC0415

    if not is_daemon_running():
        logger.debug("Daemon not running — starting")
        start_daemon(config)
    else:
        logger.debug("Daemon already running")


async def _create_session_in_daemon(options: SessionOptions) -> tuple[str, str]:
    """Send a create_session request to the daemon.

    Returns ``(session_id, short_code)`` assigned by the daemon.
    """
    return await daemon_client.create_session(
        options={
            "session_id": "",  # daemon assigns real ID
            "cwd": options.cwd,
            "name": options.name,
            "model": options.model,
            "resume": options.resume,
        },
    )


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
    if not session_id and not stop_all:
        click.echo("Provide SESSION_ID or --all.", err=True)
        ctx.exit(1)
    asyncio.run(_async_cmd_stop(ctx, session_id, stop_all))


async def _async_cmd_stop(ctx: click.Context, session_id: str | None, stop_all: bool) -> None:
    from summon_claude.daemon import is_daemon_running  # noqa: PLC0415

    if not is_daemon_running():
        click.echo("Daemon is not running.")
        return

    try:
        if stop_all:
            results = await daemon_client.stop_all_sessions()
            if not results:
                click.echo("No active sessions.")
                return
            for sid, found in results:
                click.echo(f"Stop requested for {sid}: {'sent' if found else 'not found'}")
        else:
            found = await daemon_client.stop_session(session_id)  # type: ignore[arg-type]
            if found:
                click.echo(f"Stop requested for session {session_id}")
            else:
                click.echo(f"Session {session_id} not found in daemon")
    except (DaemonError, Exception) as exc:
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
    from summon_claude.daemon import is_daemon_running  # noqa: PLC0415

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
    asyncio.run(_async_session_info(session_id, output))


async def _async_session_info(session_id: str, output: str) -> None:
    async with SessionRegistry() as registry:
        session = await registry.get_session(session_id)
        if not session:
            click.echo(f"Session not found: {session_id}")
            return
        if output == "json":
            click.echo(_format_json(session))
        else:
            _print_session_detail(session)


@cmd_session.command("logs")
@click.argument("session_id", required=False, default=None)
@click.option("--tail", "-n", default=50, help="Number of lines to show (default: 50)")
def session_logs(session_id: str | None, tail: int) -> None:
    """Show session logs. Pass SESSION_ID for a specific session, or list available logs."""
    log_dir = get_data_dir() / "logs"
    if not log_dir.exists():
        click.echo("No log files found.")
        return

    if session_id is None:
        # List available log files
        log_files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not log_files:
            click.echo("No log files found.")
            return
        click.echo("Available session logs:")
        for lf in log_files:
            click.echo(f"  {lf.stem}")
        return

    if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", session_id):
        click.echo("Error: Invalid session ID format", err=True)
        raise SystemExit(1)

    log_file = log_dir / f"{session_id}.log"
    if not log_file.exists():
        click.echo(f"No log file found for session: {session_id}")
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
    asyncio.run(_async_cleanup(ctx, archive=archive))


async def _async_cleanup(ctx: click.Context, *, archive: bool = False) -> None:
    async with SessionRegistry() as registry:
        stale = await registry.list_stale()
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
            pid = session["pid"]

            # Archive the Slack channel only if --archive flag was passed
            if archive and slack_client and channel_id:
                try:
                    provider = SlackChatProvider(slack_client)
                    channel_manager = ChannelManager(provider, "summon")
                    await channel_manager.archive_session_channel(channel_id)
                    logger.info("Archived channel %s for stale session %s", channel_id, session_id)
                except Exception as e:
                    logger.debug("Could not archive channel %s: %s", channel_id, e)

            await registry.update_status(
                session_id,
                "errored",
                error_message=f"Process {pid} no longer running",
                ended_at=datetime.now(UTC).isoformat(),
            )

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

    headers = ["STATUS", "NAME", "CHANNEL", "CWD"]
    rows: list[list[str]] = []
    for s in sessions:
        rows.append(
            [
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
