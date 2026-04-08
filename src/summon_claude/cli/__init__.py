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
import shutil
import sys
import threading

import click
import pydantic

from summon_claude import __version__
from summon_claude.cli.auth import cmd_auth
from summon_claude.cli.config import (
    config_check,
    config_edit,
    config_path,
    config_set,
    config_show,
)
from summon_claude.cli.db import async_db_purge, async_db_status, async_db_vacuum
from summon_claude.cli.doctor import async_doctor
from summon_claude.cli.formatting import _mask_secret, echo
from summon_claude.cli.hooks import (
    async_clear_hooks,
    async_set_hooks,
    async_show_hooks,
    install_hooks,
    run_post_worktree_cli,
    uninstall_hooks,
)
from summon_claude.cli.project import (
    async_project_add,
    async_project_list,
    async_project_remove,
    async_project_update,
    async_workflow_clear,
    async_workflow_set,
    async_workflow_show,
    launch_project_managers,
    stop_project_managers,
)
from summon_claude.cli.reset import async_reset_config, async_reset_data
from summon_claude.cli.session import (
    async_session_cleanup,
    async_session_list,
    session_info_impl,
    session_logs_impl,
)
from summon_claude.cli.start import async_start
from summon_claude.cli.stop import async_stop
from summon_claude.config import SummonConfig, get_config_file, get_data_dir, is_local_install
from summon_claude.daemon import start_daemon
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

    _ALIASES: dict[str, str] = {"s": "session", "p": "project"}

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
    ctx.obj["verbose"] = verbose
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
        click.echo("Run `summon init` to set up your configuration.", err=True)
        sys.exit(1)

    # Background update check (non-blocking)
    update_result: list[str] = []

    def _bg_update_check() -> None:
        from summon_claude.cli.update_check import (  # noqa: PLC0415
            check_for_update,
            format_update_message,
        )

        info = check_for_update(no_update_check=config.no_update_check)
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
# auth command group — summon auth <provider> <action>
# ---------------------------------------------------------------------------

cli.add_command(cmd_auth)


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
# project command group (alias: p)
# ---------------------------------------------------------------------------


_MAX_JQL_LEN = 500


@cli.group("project")
def cmd_project() -> None:
    """Manage summon projects."""


@cmd_project.command("add")
@click.argument("name")
@click.argument("directory", default=".", type=click.Path())
@click.option("--jql", default=None, help="JQL filter for Jira issue triage (optional).")
@click.pass_context
def project_add(ctx: click.Context, name: str, directory: str, jql: str | None) -> None:
    """Register a project directory for PM agent management."""
    if jql and len(jql) > _MAX_JQL_LEN:
        raise click.BadParameter(f"JQL filter too long (max {_MAX_JQL_LEN} chars)")
    project_id = asyncio.run(async_project_add(name, directory, jira_jql=jql))
    if not ctx.obj.get("quiet"):
        click.echo(f"Project {name!r} registered (id: {project_id[:8]}...)")
        click.echo("Run 'summon project up' to start a PM agent for this project.")


@cmd_project.command("remove")
@click.argument("name_or_id")
@click.pass_context
def project_remove(ctx: click.Context, name_or_id: str) -> None:
    """Remove a registered project."""
    asyncio.run(async_project_remove(name_or_id))
    if not ctx.obj.get("quiet"):
        click.echo(f"Project {name_or_id!r} removed.")


@cmd_project.command("list")
@click.option(
    "-o",
    "--output",
    type=click.Choice(["json", "table"]),
    default="table",
    help="Output format",
)
@click.pass_context
def project_list(ctx: click.Context, output: str) -> None:
    """List all registered projects."""
    projects = asyncio.run(async_project_list())
    if output == "json":
        click.echo(json.dumps(projects, indent=2))
        return
    if not projects:
        click.echo("No projects registered.")
        return
    name_w, dir_w = 20, 40
    click.echo(f"{'NAME':<{name_w}} {'DIRECTORY':<{dir_w}} {'PM':<10} {'ID'}")
    click.echo("-" * (name_w + dir_w + 10 + 10))
    for p in projects:
        name = p.get("name", "")
        directory = p.get("directory", "")
        pid = p.get("project_id", "")[:8]
        # Truncate name with suffix ellipsis, directory with prefix ellipsis
        if len(name) > name_w:
            name = name[: name_w - 1] + "\u2026"
        if len(directory) > dir_w:
            directory = "\u2026" + directory[-(dir_w - 1) :]
        # PM status: running > errored > completed > -
        if p.get("pm_running"):
            pm_status = "running"
        elif p.get("last_pm_status") == "errored":
            pm_status = "errored"
        elif p.get("last_pm_status") == "completed":
            pm_status = "stopped"
        elif p.get("last_pm_status") == "pending_auth":
            pm_status = "auth…"
        else:
            pm_status = "-"
        click.echo(f"{name:<{name_w}} {directory:<{dir_w}} {pm_status:<10} {pid}...")
        # Show last error inline if the PM errored
        if pm_status == "errored" and p.get("last_pm_error"):
            err = p["last_pm_error"]
            if len(err) > name_w + dir_w:
                err = err[: name_w + dir_w - 1] + "\u2026"
            click.echo(f"  └ {err}", err=True)
        # Show JQL filter if set
        if p.get("jira_jql"):
            click.echo(f"  └ JQL: {p['jira_jql']}")


@cmd_project.command("up")
@click.pass_context
def project_up(ctx: click.Context) -> None:
    """Start PM agents for all registered projects that don't have one running."""
    if not shutil.which("claude"):
        click.echo("Error: Claude CLI not found in PATH.", err=True)
        raise SystemExit(1)

    config_path_override = ctx.obj.get("config_path") if ctx.obj else None
    try:
        config = SummonConfig.from_file(config_path_override)
        config.validate()
    except Exception as e:
        click.echo(f"Configuration error: {e}", err=True)
        raise SystemExit(1) from e

    try:
        start_daemon(config)
    except Exception as e:
        click.echo(f"Error starting daemon: {e}", err=True)
        raise SystemExit(1) from e

    try:
        asyncio.run(launch_project_managers())
    except click.ClickException:
        raise
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from e


@cmd_project.command("down")
@click.argument("name", required=False, default=None)
@click.pass_context
def project_down(ctx: click.Context, name: str | None) -> None:
    """Stop PM sessions for registered projects.

    If NAME is given, stop only that project's sessions. Otherwise stop all.
    """
    try:
        asyncio.run(stop_project_managers(name=name))
    except click.ClickException:
        raise
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from e


@cmd_project.group("workflow")
def project_workflow() -> None:
    """Manage PM workflow instructions."""


@project_workflow.command("show")
@click.argument("project_name", required=False, default=None)
@click.option(
    "--raw",
    is_flag=True,
    default=False,
    help="Show raw template without expanding $INCLUDE_GLOBAL",
)
def workflow_show(project_name: str | None, raw: bool) -> None:
    """Show workflow instructions. Without PROJECT_NAME, shows global defaults."""
    asyncio.run(async_workflow_show(project_name, raw=raw))


@project_workflow.command("set")
@click.argument("project_name", required=False, default=None)
def workflow_set(project_name: str | None) -> None:
    """Set workflow instructions via $EDITOR. Without PROJECT_NAME, sets global defaults."""
    asyncio.run(async_workflow_set(project_name))


@project_workflow.command("clear")
@click.argument("project_name", required=False, default=None)
def workflow_clear(project_name: str | None) -> None:
    """Clear workflow instructions. Without PROJECT_NAME, clears global defaults."""
    asyncio.run(async_workflow_clear(project_name))


@cmd_project.command("update")
@click.argument("name_or_id")
@click.option("--jql", default=None, help='JQL filter for Jira triage. Pass "" to clear.')
@click.pass_context
def project_update(ctx: click.Context, name_or_id: str, jql: str | None) -> None:
    """Update a project's configuration.

    NAME_OR_ID can be the project name or project ID prefix.
    Pass --jql "" to clear the Jira JQL filter.
    """
    if jql is None:
        raise click.UsageError("No fields to update. Use --jql to set a JQL filter.")
    if jql and len(jql) > _MAX_JQL_LEN:
        raise click.BadParameter(f"JQL filter too long (max {_MAX_JQL_LEN} chars)")
    # Empty string clears the field; non-empty sets it.
    asyncio.run(async_project_update(name_or_id, jira_jql=jql or None))
    if not ctx.obj.get("quiet"):
        if jql:
            click.echo(f"Project {name_or_id!r} updated: JQL filter set.")
        else:
            click.echo(f"Project {name_or_id!r} updated: JQL filter cleared.")


# ---------------------------------------------------------------------------
# Top-level commands: init, config
# ---------------------------------------------------------------------------


@cli.command("init")
@click.pass_context
def cmd_init(ctx: click.Context) -> None:
    """Interactive setup wizard for summon-claude configuration."""
    from summon_claude.cli.preflight import check_claude_cli  # noqa: PLC0415
    from summon_claude.config import CONFIG_OPTIONS, _is_truthy, get_config_default  # noqa: PLC0415

    # Preflight check
    cli_status = check_claude_cli()
    if not cli_status.found:
        click.echo("Warning: Claude CLI not found in PATH.", err=True)
        click.echo("Install from https://claude.ai/code before running sessions.", err=True)
        click.echo()
    elif cli_status.version:
        click.echo(f"Claude CLI: {cli_status.version}")

    click.echo("Setting up summon-claude configuration...")
    click.echo()

    config_path_override = ctx.obj.get("config_path") if ctx.obj else None
    config_file = pathlib.Path(config_path_override) if config_path_override else get_config_file()

    # Load existing config values for pre-filling
    from summon_claude.cli.config import parse_env_file  # noqa: PLC0415

    existing = parse_env_file(config_file)
    if existing:
        click.echo(f"  Existing config found at {config_file}")
        click.echo("  Press Enter to keep current values.\n")

    # Opportunistically refresh model cache via throwaway SDK session
    if cli_status.found:
        from summon_claude.cli.model_cache import (  # noqa: PLC0415
            cache_sdk_models,
            load_cached_models,
            query_sdk_models,
        )

        cached = load_cached_models()
        if cached is None:
            click.echo("  Discovering available models...", nl=False)
            sdk_result = asyncio.run(query_sdk_models(cli_version=cli_status.version))
            if sdk_result is not None:
                sdk_models, sdk_cli_version = sdk_result
                cache_sdk_models(sdk_models, sdk_cli_version)
                if sdk_models:
                    click.echo(" done")
                else:
                    click.echo(" skipped (no models returned)")
            else:
                click.echo(" skipped (CLI not authenticated)")

    # Collect values via prompts
    collected: dict[str, str] = {}
    current_group = ""
    show_advanced: bool | None = None  # None = not yet asked

    for opt in CONFIG_OPTIONS:
        # Build the in-progress config dict for visibility predicates
        in_progress = {**existing, **collected}

        # Check visibility
        if opt.visible is not None and not opt.visible(in_progress):
            continue

        # Gate advanced options behind a single prompt
        if opt.advanced and show_advanced is None:
            click.echo()
            show_advanced = click.confirm("  Configure advanced settings?", default=False)
            if not show_advanced:
                break

        # Print group header on group change
        if opt.group != current_group:
            current_group = opt.group
            click.echo(click.style(f"  {opt.group}", bold=True))

        current_value = existing.get(opt.env_key)
        default = get_config_default(opt)

        # Show contextual guidance — help_hint preferred, help_text as fallback
        hint = opt.help_hint or opt.help_text
        if hint:
            click.echo(click.style(f"    {hint}", dim=True))
        # Show env var name for cross-reference with docs
        click.echo(click.style(f"    ({opt.env_key})", dim=True))

        if opt.input_type == "secret":
            # Build prompt label with format hint if available
            fmt_suffix = ""
            if opt.format_hint:
                fmt_suffix = click.style(f" [format: {opt.format_hint}]", dim=True)

            if current_value:
                raw = click.prompt(
                    f"    {opt.label} [configured — Enter to keep]{fmt_suffix}",
                    default="",
                    show_default=False,
                    hide_input=True,
                )
                if raw:
                    value = raw
                    click.echo(click.style(f"    Value received: {_mask_secret(value)}", dim=True))
                else:
                    value = current_value
                    click.echo(click.style("    Kept existing value", dim=True))
            elif opt.required:
                value = click.prompt(f"    {opt.label}{fmt_suffix}", hide_input=True)
                if opt.validate_fn:
                    err = opt.validate_fn(value)
                    while err:
                        click.echo(f"    Error: {err}")
                        value = click.prompt(f"    {opt.label}{fmt_suffix}", hide_input=True)
                        err = opt.validate_fn(value)
                click.echo(click.style(f"    Value received: {_mask_secret(value)}", dim=True))
            else:
                # Optional secret — empty input accepted (skip)
                value = click.prompt(
                    f"    {opt.label} (optional, Enter to skip){fmt_suffix}",
                    default="",
                    show_default=False,
                    hide_input=True,
                )
                if value and opt.validate_fn:
                    err = opt.validate_fn(value)
                    while err:
                        click.echo(f"    Error: {err}")
                        value = click.prompt(
                            f"    {opt.label}{fmt_suffix}",
                            default="",
                            show_default=False,
                            hide_input=True,
                        )
                        if not value:
                            break
                        err = opt.validate_fn(value)
                if value:
                    click.echo(click.style(f"    Value received: {_mask_secret(value)}", dim=True))
                else:
                    click.echo(click.style("    Skipped", dim=True))

        elif opt.input_type == "choice":
            if opt.choices_fn:
                choices = opt.choices_fn()
            elif opt.choices:
                choices = list(opt.choices)
            else:
                choices = []
            prompt_default = current_value or (str(default) if default is not None else "")
            # Sentinel-aware default handling for model fields.
            if not prompt_default:
                # Fresh install with no current value: map None → "default (auto)"
                if "default (auto)" in choices:
                    prompt_default = "default (auto)"
                elif choices:
                    prompt_default = choices[0]
            elif choices and prompt_default not in choices:
                # Existing custom model not in choices: insert it before "other"
                insert_idx = choices.index("other") if "other" in choices else len(choices)
                choices = [*choices[:insert_idx], prompt_default, *choices[insert_idx:]]
            value = click.prompt(
                f"    {opt.label}",
                type=click.Choice(choices, case_sensitive=False) if choices else None,
                default=prompt_default or None,
                show_default=True,
            )
            # Sentinel post-processing
            if value == "default (auto)":
                value = ""
            elif value == "other":
                raw_custom = click.prompt(f"    {opt.label} (custom)", default="")
                value = raw_custom if raw_custom else (current_value or "")
            # Soft-validate if validate_fn present (warn-only; return value always None)
            if value and opt.validate_fn:
                opt.validate_fn(value)

        elif opt.input_type == "flag":
            current_bool = False
            if current_value:
                current_bool = _is_truthy(current_value)
            elif default is not None:
                current_bool = bool(default)
            value = "true" if click.confirm(f"    {opt.label}?", default=current_bool) else "false"

        elif opt.input_type == "int":
            prompt_default = current_value or (str(default) if default is not None else None)
            raw = click.prompt(
                f"    {opt.label}", default=prompt_default, show_default=True, type=int
            )
            value = str(raw)
            if value and opt.validate_fn:
                err = opt.validate_fn(value)
                while err:
                    click.echo(f"    Error: {err}")
                    if hint:
                        click.echo(click.style(f"    {hint}", dim=True))
                    raw = click.prompt(
                        f"    {opt.label}",
                        default=prompt_default,
                        show_default=True,
                        type=int,
                    )
                    value = str(raw)
                    if not value:
                        break
                    err = opt.validate_fn(value)

        else:  # text
            prompt_default = current_value or (str(default) if default is not None else "") or ""
            value = click.prompt(
                f"    {opt.label}", default=prompt_default, show_default=bool(prompt_default)
            )
            if value and opt.validate_fn:
                err = opt.validate_fn(value)
                while err:
                    click.echo(f"    Error: {err}")
                    if hint:
                        click.echo(click.style(f"    {hint}", dim=True))
                    value = click.prompt(
                        f"    {opt.label}",
                        default=prompt_default,
                        show_default=bool(prompt_default),
                    )
                    if not value:
                        break
                    err = opt.validate_fn(value)

        # Only store non-default values (keep config file clean)
        if isinstance(default, bool):
            default_str = str(default).lower()
        else:
            default_str = str(default) if default is not None else ""
        if opt.required or value != default_str:
            collected[opt.env_key] = value

    # Validate before writing — construct SummonConfig to catch bad combinations
    try:
        SummonConfig(
            **{
                opt.field_name: collected[opt.env_key]
                for opt in CONFIG_OPTIONS
                if opt.env_key in collected
            }
        )
    except pydantic.ValidationError as e:
        click.echo("\nValidation error:", err=True)
        for err in e.errors():
            field = ".".join(str(loc) for loc in err["loc"])
            click.echo(f"  {field}: {err['msg']}", err=True)
        click.echo("Config file NOT written. Fix the errors and re-run `summon init`.", err=True)
        raise SystemExit(1) from e
    except Exception as e:
        click.echo(f"\nUnexpected error: {e}", err=True)
        click.echo("Config file NOT written. Fix the errors and re-run `summon init`.", err=True)
        raise SystemExit(1) from e

    # Write config file — merge existing values for hidden options to prevent data loss,
    # then strip newlines to prevent injection into .env format
    config_file.parent.mkdir(parents=True, exist_ok=True)
    merged = {**existing, **collected}
    sanitized = {k: v.replace("\n", "").replace("\r", "") for k, v in merged.items()}
    output_lines = [f"{k}={v}" for k, v in sanitized.items()]
    config_file.write_text("\n".join(output_lines) + "\n")
    with contextlib.suppress(OSError):
        config_file.chmod(0o600)

    click.echo()
    click.echo(f"Configuration saved to {config_file}")
    if is_local_install():
        # Defense-in-depth: write .gitignore inside .summon/ to prevent accidental commits
        summon_dir = get_data_dir()
        summon_dir.mkdir(parents=True, exist_ok=True)
        gitignore = summon_dir / ".gitignore"
        if not gitignore.exists():
            with contextlib.suppress(OSError):
                gitignore.write_text("*\n")
        click.echo("Note: Add .summon/ to your project's .gitignore to avoid committing secrets.")
    click.echo()

    # Auto-run config check — validates connectivity and shows feature inventory
    from summon_claude.cli.config import config_check  # noqa: PLC0415

    config_check(config_path=config_path_override)


@cli.group("config")
def cmd_config() -> None:
    """Manage summon-claude configuration."""


@cmd_config.command("show")
@click.pass_context
def config_show_cmd(ctx: click.Context) -> None:
    """Show current configuration with grouped display and source indicators."""
    config_path_override = ctx.obj.get("config_path") if ctx.obj else None
    use_color = ctx.color is not False
    config_show(config_path_override, color=use_color)


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


# ---------------------------------------------------------------------------
# reset command group
# ---------------------------------------------------------------------------


@cli.group("reset")
def cmd_reset() -> None:
    """Reset summon data or configuration to a clean state."""


@cmd_reset.command("data")
@click.option(
    "--force",
    is_flag=True,
    help="Bypass symlink/outside-home safety checks. Still requires confirmation.",
)
@click.pass_context
def reset_data(ctx: click.Context, force: bool) -> None:
    """Delete all runtime data and start fresh."""
    asyncio.run(async_reset_data(ctx, force=force))


@cmd_reset.command("config")
@click.option(
    "--force",
    is_flag=True,
    help="Bypass symlink/outside-home safety checks. Still requires confirmation.",
)
@click.pass_context
def reset_config(ctx: click.Context, force: bool) -> None:
    """Delete all configuration (Slack tokens, Google OAuth credentials)."""
    asyncio.run(async_reset_config(ctx, force=force))


# ---------------------------------------------------------------------------
# hooks command group
# ---------------------------------------------------------------------------


@cli.group("hooks")
def cmd_hooks() -> None:
    """Manage lifecycle hooks and the Claude Code hook bridge."""


@cmd_hooks.command("show")
@click.option("--project", default=None, help="Project ID to show hooks for (default: global)")
@click.pass_context
def hooks_show(ctx: click.Context, project: str | None) -> None:
    """Show configured lifecycle hooks."""
    asyncio.run(async_show_hooks(ctx, project_id=project))


@cmd_hooks.command("set")
@click.argument("hooks_json", required=False, default=None)
@click.option("--project", default=None, help="Project ID to set hooks for (default: global)")
def hooks_set(hooks_json: str | None, project: str | None) -> None:
    """Set lifecycle hooks via $EDITOR or from a JSON string.

    If HOOKS_JSON is omitted, opens $EDITOR with current hooks for editing.
    If provided, parses the JSON and stores it directly.

    Hook types: worktree_create, project_up, project_down.
    Use "$INCLUDE_GLOBAL" in per-project hooks to include global hooks.

    \b
    Examples:
      summon hooks set                                  # opens $EDITOR
      summon hooks set '{"worktree_create": ["make setup"]}'  # inline JSON
      summon hooks set --project ID '{"worktree_create": ["$INCLUDE_GLOBAL", "make setup"]}'
    """
    asyncio.run(async_set_hooks(hooks_json, project_id=project))


@cmd_hooks.command("clear")
@click.option("--project", default=None, help="Project ID to clear hooks for (default: global)")
def hooks_clear(project: str | None) -> None:
    """Clear lifecycle hooks (sets to NULL, falling back to global defaults)."""
    asyncio.run(async_clear_hooks(project_id=project))


@cmd_hooks.command("install")
def hooks_install() -> None:
    """Install the Claude Code hook bridge (shell wrappers + settings.json entries).

    Writes summon-pre-worktree.sh and summon-post-worktree.sh to
    ~/.claude/hooks/ and registers them in ~/.claude/settings.json
    as PreToolUse/PostToolUse handlers for EnterWorktree. Idempotent.
    """
    try:
        install_hooks()
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hooks.command("uninstall")
def hooks_uninstall() -> None:
    """Remove summon-owned hook entries from settings.json and delete shell wrappers."""
    try:
        uninstall_hooks()
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cmd_hooks.group("run", hidden=True)
def hooks_run() -> None:
    """Run internal hook bridge commands (called by shell wrappers)."""


@hooks_run.command("post-worktree")
def hooks_run_post_worktree() -> None:
    """Run worktree_create lifecycle hooks for the current directory.

    Called automatically by the post-worktree shell wrapper after
    EnterWorktree. Always exits 0 — hook failures are warnings only.
    """
    run_post_worktree_cli()


@cli.command("doctor")
@click.option(
    "--export",
    "export_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Export results as JSON to this file path",
)
@click.option(
    "--submit",
    is_flag=True,
    default=False,
    help="Submit a redacted report as a GitHub issue (requires gh CLI)",
)
@click.pass_context
def cmd_doctor(ctx: click.Context, export_path: str | None, submit: bool) -> None:
    """Run comprehensive diagnostics and display pass/fail results."""
    asyncio.run(async_doctor(ctx, export_path, submit))


def main() -> None:
    cli()
