"""CLI entry point for summon-claude."""

# pyright: reportFunctionMemberAccess=false, reportArgumentType=false, reportReturnType=false, reportCallIssue=false
# click decorators: https://github.com/pallets/click/issues/2255
# slack_sdk doesn't ship type stubs; pydantic-settings metaclass gaps

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
import re
import signal
import sys
import uuid
from datetime import UTC, datetime

import click
import daemon
from slack_sdk.web.async_client import AsyncWebClient

from summon_claude.channel_manager import ChannelManager
from summon_claude.cli_config import config_edit, config_path, config_set, config_show
from summon_claude.config import SummonConfig, get_config_dir, get_config_file, get_data_dir
from summon_claude.providers.slack import SlackChatProvider
from summon_claude.registry import SessionRegistry
from summon_claude.session import SessionOptions, SummonSession

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


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=level,
    )
    # Silence noisy libraries unless verbose
    if not verbose:
        logging.getLogger("slack_bolt").setLevel(logging.ERROR)
        logging.getLogger("slack_sdk").setLevel(logging.ERROR)
        logging.getLogger("asyncio").setLevel(logging.ERROR)


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


@click.group(cls=AliasedGroup)
@click.option(
    "-v", "--verbose", is_eager=True, is_flag=True, default=False, help="Enable verbose logging"
)
def cli(verbose: bool) -> None:
    """Bridge Claude Code sessions to Slack channels."""
    _setup_logging(verbose)


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
@click.option(
    "--background",
    "-b",
    is_flag=True,
    default=False,
    help="Run session as a background daemon process",
)
def cmd_start(
    cwd: str | None,
    resume: str | None,
    name: str | None,
    model: str | None,
    background: bool,
) -> None:
    """Start a new summon session."""
    try:
        config = SummonConfig()
        config.validate()
    except Exception as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)

    resolved_cwd = str(pathlib.Path(cwd).resolve()) if cwd else str(pathlib.Path.cwd())
    session_id = str(uuid.uuid4())
    resolved_name = name or pathlib.Path(resolved_cwd).name

    session = SummonSession(
        config=config,
        options=SessionOptions(
            session_id=session_id,
            cwd=resolved_cwd,
            name=resolved_name,
            model=model or config.default_model,
            resume=resume,
        ),
    )

    # Phase 1: Register + generate auth token (foreground, pre-fork)
    try:
        short_code = asyncio.run(session.prepare_auth())
    except Exception as e:
        logger.exception("Failed to prepare session: %s", e)
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    _print_auth_banner(short_code)

    # Phase 2: Daemonize if requested (after banner is shown)
    daemon_ctx: daemon.DaemonContext | contextlib.nullcontext[None] = contextlib.nullcontext()
    if background:
        if sys.platform == "win32":
            click.echo("Background mode is not supported on Windows.", err=True)
            sys.exit(1)

        log_dir = get_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{session_id}.log"
        click.echo(f"Session started in background. Log: {log_file}")

        log_fh = log_file.open("a")
        daemon_ctx = daemon.DaemonContext(
            working_directory=resolved_cwd,
            umask=0o022,
            stdout=log_fh,
            stderr=log_fh,
            signal_map={signal.SIGHUP: signal.SIG_IGN},
        )

    # Phase 3: Run session (bolt + auth wait + message loop)
    with daemon_ctx:
        try:
            success = asyncio.run(session.start())
            if not success:
                sys.exit(1)
        except KeyboardInterrupt:
            click.echo("\nInterrupted.")
        except Exception as e:
            logger.exception("Session error: %s", e)
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)


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
def session_list(show_all: bool) -> None:
    """List sessions. Shows active sessions by default; use --all for all recent."""
    asyncio.run(_async_session_list(show_all))


async def _async_session_list(show_all: bool) -> None:
    async with SessionRegistry() as registry:
        if show_all:
            sessions = await registry.list_all(limit=50)
        else:
            sessions = await registry.list_active()
        if not sessions:
            click.echo("No sessions found." if show_all else "No active sessions.")
            return
        _print_session_table(sessions)


@cmd_session.command("info")
@click.argument("session_id")
def session_info(session_id: str) -> None:
    """Show detailed information for a specific session."""
    asyncio.run(_async_session_info(session_id))


async def _async_session_info(session_id: str) -> None:
    async with SessionRegistry() as registry:
        session = await registry.get_session(session_id)
        if not session:
            click.echo(f"Session not found: {session_id}")
            return
        _print_session_detail(session)


@cmd_session.command("stop")
@click.argument("session_id")
def session_stop(session_id: str) -> None:
    """Send SIGTERM to a running session process."""
    asyncio.run(_async_stop(session_id))


async def _async_stop(session_id: str) -> None:
    async with SessionRegistry() as registry:
        session = await registry.get_session(session_id)
        if not session:
            click.echo(f"Session not found: {session_id}")
            return
        if session["status"] not in ("pending_auth", "active"):
            click.echo(f"Session {session_id} is not active (status: {session['status']})")
            return

        pid = session["pid"]

        # Verify the PID belongs to the current user before signaling
        if not _pid_owned_by_current_user(pid):
            click.echo(f"Process {pid} is not owned by the current user — refusing to signal")
            return

        try:
            os.kill(pid, signal.SIGTERM)
            click.echo(f"Sent SIGTERM to session {session_id} (pid {pid})")
            await registry.log_event(
                "session_stopped",
                session_id=session_id,
                details={"pid": pid, "stopped_by": "cli"},
            )
        except ProcessLookupError:
            click.echo(f"Process {pid} not found — session may have already ended")
            await registry.update_status(
                session_id, "errored", error_message="Process not found at stop time"
            )
        except PermissionError:
            click.echo(f"Permission denied to send signal to pid {pid}")


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
def session_cleanup() -> None:
    """Mark sessions with dead processes as errored."""
    asyncio.run(_async_cleanup())


async def _async_cleanup() -> None:
    async with SessionRegistry() as registry:
        stale = await registry.list_stale()
        if not stale:
            click.echo("No stale sessions found.")
            return

        # Try to construct a Slack client for archiving channels (best-effort)
        slack_client = None
        try:
            config = SummonConfig()
            slack_client = AsyncWebClient(token=config.slack_bot_token)
        except Exception as e:
            logger.debug("Could not initialize Slack client for cleanup: %s", e)

        for session in stale:
            session_id = session["session_id"]
            channel_id = session.get("slack_channel_id")
            pid = session["pid"]

            # Archive the Slack channel if we have a client and channel ID
            if slack_client and channel_id:
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
def cmd_init() -> None:
    """Interactive setup wizard for summon-claude configuration."""
    click.echo("Setting up summon-claude configuration...")
    click.echo()

    bot_token = click.prompt("  Slack Bot Token (xoxb-...)")
    while not bot_token.startswith("xoxb-"):
        click.echo("  Error: Bot token must start with 'xoxb-'")
        bot_token = click.prompt("  Slack Bot Token (xoxb-...)")

    app_token = click.prompt("  Slack App Token (xapp-...)")
    while not app_token.startswith("xapp-"):
        click.echo("  Error: App token must start with 'xapp-'")
        app_token = click.prompt("  Slack App Token (xapp-...)")

    signing_secret = click.prompt("  Slack Signing Secret")
    allowed_users = click.prompt("  Allowed User IDs (comma-separated)")

    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = get_config_file()

    lines = [
        f"SUMMON_SLACK_BOT_TOKEN={bot_token}",
        f"SUMMON_SLACK_APP_TOKEN={app_token}",
        f"SUMMON_SLACK_SIGNING_SECRET={signing_secret}",
        f"SUMMON_ALLOWED_USER_IDS={allowed_users}",
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
def config_show_cmd() -> None:
    """Show current configuration (tokens masked)."""
    config_show()


@cmd_config.command("path")
def config_path_cmd() -> None:
    """Print the config file path."""
    config_path()


@cmd_config.command("edit")
def config_edit_cmd() -> None:
    """Open config file in $EDITOR."""
    config_edit()


@cmd_config.command("set")
@click.argument("key")
@click.argument("value")
def config_set_cmd(key: str, value: str) -> None:
    """Set a configuration value (e.g. SUMMON_SLACK_BOT_TOKEN)."""
    config_set(key, value)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _print_session_table(sessions: list[dict]) -> None:
    """Print a compact table of sessions."""
    if not sessions:
        return

    headers = ["STATUS", "NAME", "CHANNEL", "CWD", "TURNS", "COST"]
    rows: list[list[str]] = []
    for s in sessions:
        rows.append(
            [
                s.get("status", "?"),
                s.get("session_name") or "-",
                s.get("slack_channel_name") or "-",
                _truncate(s.get("cwd", ""), 30),
                str(s.get("total_turns", 0)),
                f"${s.get('total_cost_usd', 0.0) or 0.0:.4f}",
            ]
        )

    col_widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    click.echo(fmt.format(*headers))
    click.echo("  ".join("-" * w for w in col_widths))
    for row in rows:
        click.echo(fmt.format(*row))


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


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else "..." + s[-(n - 3) :]


def _pid_uid_from_proc(pid: int) -> int | None:
    """Read the real UID of *pid* from /proc (Linux only)."""
    stat_file = pathlib.Path(f"/proc/{pid}/status")
    if not stat_file.exists():
        return None
    for line in stat_file.read_text().splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    return None


def _pid_owned_by_current_user(pid: int) -> bool:
    """Return True if the process with the given PID is owned by the current user."""
    try:
        import psutil  # type: ignore[import]  # optional dependency  # noqa: PLC0415

        proc = psutil.Process(pid)
        return proc.uids().real == os.getuid()
    except Exception as e:
        logger.debug("psutil PID ownership check failed: %s", e)

    # psutil not available or process gone; fall back to /proc on Linux
    try:
        uid = _pid_uid_from_proc(pid)
        if uid is not None:
            return uid == os.getuid()
    except Exception as e:
        logger.debug("/proc PID ownership check failed: %s", e)

    # macOS/BSD fallback: check process exists via os.kill(pid, 0).
    # Since only the current user registers sessions in the registry,
    # existence is sufficient to confirm ownership on non-Linux systems.
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by another user
        return False


def main() -> None:
    cli()
