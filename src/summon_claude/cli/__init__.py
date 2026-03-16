"""CLI entry point for summon-claude."""

# pyright: reportFunctionMemberAccess=false, reportArgumentType=false, reportReturnType=false, reportCallIssue=false
# click decorators: https://github.com/pallets/click/issues/2255
# slack_sdk doesn't ship type stubs; pydantic-settings metaclass gaps

# Naming conventions:
# - Top-level click commands: cmd_X
# - Group subcommands: group_subcommand (+ _cmd suffix for import shadowing)
# - Async helpers: extracted to cli/<module>.py

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import secrets
import sys
import threading

import click

from summon_claude import __version__
from summon_claude.cli.config import config_check, config_edit, config_path, config_set, config_show
from summon_claude.cli.db import async_db_purge, async_db_reset, async_db_status, async_db_vacuum
from summon_claude.cli.formatting import echo
from summon_claude.cli.session import (
    async_session_cleanup,
    async_session_list,
    session_info_impl,
    session_logs_impl,
)
from summon_claude.cli.start import async_start
from summon_claude.cli.stop import async_stop
from summon_claude.config import SummonConfig, get_config_dir, get_config_file, get_data_dir
from summon_claude.sessions import registry as _registry

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
@click.option(
    "--effort",
    type=click.Choice(["low", "medium", "high", "max"]),
    default=None,
    help="Effort level (default: high, or SUMMON_DEFAULT_EFFORT)",
)
@click.pass_context
def cmd_start(
    ctx: click.Context,
    cwd: str | None,
    resume: str | None,
    name: str | None,
    model: str | None,
    effort: str | None,
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
    if not pathlib.Path(resolved_cwd).is_dir():
        click.echo(f"Error: working directory does not exist: {resolved_cwd}", err=True)
        sys.exit(1)
    base_name = pathlib.Path(resolved_cwd).name

    # Auto-generated names retry on collision; explicit names fail immediately.
    max_attempts = 1 if name else 3
    short_code = ""
    for attempt in range(max_attempts):
        resolved_name = name or f"{base_name}-{secrets.token_hex(3)}"
        try:
            short_code = asyncio.run(
                async_start(config, resolved_cwd, resolved_name, model, effort, resume)
            )
            break
        except ValueError as e:
            if attempt < max_attempts - 1:
                continue
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

    _print_auth_banner(short_code)

    # Show update notification if available (after banner)
    update_thread.join(timeout=4.0)
    if update_result and not ctx.obj.get("quiet"):
        click.echo(update_result[0], err=True)


# ---------------------------------------------------------------------------
# Top-level stop command
# ---------------------------------------------------------------------------


@cli.command("stop")
@click.argument("session", metavar="SESSION", required=False, default=None)
@click.option(
    "--all",
    "-a",
    "stop_all",
    is_flag=True,
    default=False,
    help="Stop all active sessions",
)
@click.pass_context
def cmd_stop(ctx: click.Context, session: str | None, stop_all: bool) -> None:
    """Stop a session (by name or ID) or all sessions via the daemon."""
    asyncio.run(async_stop(ctx, session, stop_all))


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
@click.option("--name", default=None, help="Filter sessions by name")
@click.option(
    "-o",
    "--output",
    type=click.Choice(["json", "table"]),
    default="table",
    help="Output format",
)
@click.pass_context
def session_list(ctx: click.Context, show_all: bool, name: str | None, output: str) -> None:
    """List sessions. Shows active sessions by default; use --all for all recent."""
    asyncio.run(async_session_list(ctx, show_all, output, name=name))


@cmd_session.command("info")
@click.argument("session", metavar="SESSION")
@click.option(
    "-o",
    "--output",
    type=click.Choice(["json", "table"]),
    default="table",
    help="Output format",
)
@click.pass_context
def session_info(ctx: click.Context, session: str, output: str) -> None:
    """Show detailed information for a session (by name or ID)."""
    asyncio.run(session_info_impl(ctx, session, output))


@cmd_session.command("logs")
@click.argument("session", metavar="SESSION", required=False, default=None)
@click.option("--tail", "-n", default=50, help="Number of lines to show (default: 50)")
@click.pass_context
def session_logs(ctx: click.Context, session: str | None, tail: int) -> None:
    """Show session logs. Pass a session name or ID, or list available logs."""
    asyncio.run(session_logs_impl(ctx, session, tail))


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
    asyncio.run(async_session_cleanup(ctx, archive=archive))


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
# db command group
# ---------------------------------------------------------------------------


@cli.group("db")
def cmd_db() -> None:
    """Database maintenance commands."""


@cmd_db.command("status")
@click.pass_context
def db_status(ctx: click.Context) -> None:
    """Show schema version, integrity, and row counts."""
    asyncio.run(async_db_status(ctx))


@cmd_db.command("reset")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt")
@click.pass_context
def db_reset(ctx: click.Context, yes: bool) -> None:
    """Delete and recreate the registry database."""
    db_path = _registry.default_db_path()

    if not yes:
        click.confirm(
            f"This will permanently delete all session history and recreate an empty database"
            f" at {db_path.name}. Continue?",
            abort=True,
        )

    # Delete existing DB and WAL/SHM files
    for suffix in ("", "-wal", "-shm"):
        (db_path.parent / (db_path.name + suffix)).unlink(missing_ok=True)

    asyncio.run(async_db_reset(db_path, ctx))


@cmd_db.command("vacuum")
@click.pass_context
def db_vacuum(ctx: click.Context) -> None:
    """Compact the database and check integrity."""
    db_path = _registry.default_db_path()
    if not db_path.exists():
        click.echo(f"Database not found: {db_path}", err=True)
        ctx.exit(1)
        return

    size_before = db_path.stat().st_size
    asyncio.run(async_db_vacuum(db_path, ctx))
    size_after = db_path.stat().st_size

    echo(f"Size: {size_before:,} → {size_after:,} bytes", ctx)


@cmd_db.command("purge")
@click.option(
    "--older-than",
    default=30,
    type=click.IntRange(min=1),
    show_default=True,
    help="Purge records older than N days",
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt")
@click.pass_context
def db_purge(ctx: click.Context, older_than: int, yes: bool) -> None:
    """Purge old sessions, audit logs, and expired auth tokens."""
    if not yes:
        msg = f"Purge all completed/errored records older than {older_than} days?"
        click.confirm(msg, abort=True)
    asyncio.run(async_db_purge(older_than, ctx))


def main() -> None:
    cli()
