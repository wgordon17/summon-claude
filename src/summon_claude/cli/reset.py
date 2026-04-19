"""Reset command logic for CLI."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import click

from summon_claude.cli.interactive import is_interactive
from summon_claude.config import get_config_dir, get_data_dir
from summon_claude.daemon import is_daemon_running
from summon_claude.sessions.session import is_pm_session_name


class _IpcError(Exception):
    """Raised when IPC communication with the daemon fails."""


async def _check_running_sessions() -> tuple[bool, bool, bool]:
    """Return (daemon_running, has_adhoc, has_project) based on live daemon sessions.

    Raises:
        _IpcError: if the daemon is running but IPC communication fails.
    """
    if not is_daemon_running():
        return (False, False, False)

    from summon_claude.cli import daemon_client  # noqa: PLC0415

    try:
        sessions = await daemon_client.list_sessions()
    except Exception as exc:
        raise _IpcError(str(exc)) from exc

    has_adhoc = any(
        not session.get("project_id") and not is_pm_session_name(session.get("session_name", ""))
        for session in sessions
    )
    has_project = any(
        session.get("project_id") or is_pm_session_name(session.get("session_name", ""))
        for session in sessions
    )
    return (True, has_adhoc, has_project)


async def _refuse_if_running() -> bool:
    """Check for running daemon/sessions and print guidance if found.

    Returns True if the caller should abort (daemon running, sessions detected,
    or IPC failed), False if it is safe to proceed.
    """
    try:
        daemon_running, has_adhoc, has_project = await _check_running_sessions()
    except _IpcError:
        click.echo(
            "Could not determine session status. Ensure the daemon is stopped before resetting."
        )
        return True

    if has_adhoc:
        click.echo("Active sessions detected. Run 'summon stop --all' first.")
    if has_project:
        click.echo("Project sessions detected. Run 'summon project down' first.")
    if has_adhoc or has_project:
        return True

    # Daemon running with zero sessions — still unsafe to delete data dir
    if daemon_running:
        click.echo(
            "The summon daemon is still running. Wait a moment for it to shut down, then retry."
        )
        return True

    return False


async def _reset_directory(  # noqa: PLR0913
    ctx: click.Context,
    get_dir: Callable[[], Path],
    label: str,
    warning: str,
    success: str,
    *,
    force: bool = False,
) -> None:
    """Shared reset logic for data and config directories."""
    if not is_interactive(ctx):
        click.echo("Reset requires interactive mode.")
        raise SystemExit(1)

    if await _refuse_if_running():
        raise SystemExit(1)

    target = get_dir()
    if not target.exists():
        click.echo(f"Nothing to reset — {label} directory does not exist.")
        return

    # Safety checks BEFORE prompting — refuse early if the path is unsafe.
    # --force bypasses these but still requires interactive confirmation below.
    if target.is_symlink() and not force:
        click.echo(
            f"Error: {label} directory is a symlink. Refusing to delete (add --force to bypass)."
        )
        raise SystemExit(1)
    resolved = target.resolve()
    home = Path.home().resolve()
    if not resolved.is_relative_to(home) and not force:
        click.echo(
            f"Error: {label} directory resolves outside home directory ({resolved}). "
            "Refusing to delete (add --force to bypass)."
        )
        raise SystemExit(1)

    # Hard floor — never delete shallow paths regardless of --force.
    # Catches symlinks to / or top-level dirs like /etc, /home, /Users.
    if len(resolved.parts) < 3:
        click.echo(f"Error: resolved path is too shallow ({resolved}). Refusing to delete.")
        raise SystemExit(1)

    if force:
        click.echo("Safety checks bypassed (--force).")
    click.echo(warning.format(path=resolved))
    click.confirm("Are you SURE?" if force else "Continue?", default=False, abort=True)

    try:
        shutil.rmtree(resolved)
    except OSError as exc:
        click.echo(f"Failed to delete {label} directory: {exc}")
        raise SystemExit(1) from exc
    click.echo(success)


async def async_reset_data(ctx: click.Context, *, force: bool = False) -> None:
    """Delete all runtime data (database, logs, etc.) after confirmation."""
    await _reset_directory(
        ctx,
        get_data_dir,
        "data",
        "This will delete all runtime data at {path} including the session "
        "database, project registrations, logs, and daemon state.",
        "Data cleared. Run 'summon start' to begin a new session.",
        force=force,
    )


async def async_reset_config(ctx: click.Context, *, force: bool = False) -> None:
    """Delete all configuration (Slack tokens, Google/Jira OAuth credentials) after confirmation."""
    await _reset_directory(
        ctx,
        get_config_dir,
        "config",
        "This will delete all configuration at {path} including "
        "Slack tokens, Google OAuth credentials, Jira OAuth credentials, "
        "and external Slack auth state.",
        "Configuration cleared. Run 'summon hooks uninstall' to remove the Claude Code"
        " hook bridge, then 'summon init' to reconfigure.",
        force=force,
    )
