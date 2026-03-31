# pyright: reportFunctionMemberAccess=false
"""CLI auth subcommands: unified auth group with provider subgroups.

Consolidates all authentication commands under `summon auth`:
  - summon auth status          (unified status of all providers)
  - summon auth github login    (GitHub OAuth device flow)
  - summon auth github logout   (remove GitHub token)
  - summon auth google login    (Google Workspace OAuth)
  - summon auth google status   (check Google creds)
  - summon auth slack login     (external Slack browser auth)
  - summon auth slack logout    (remove Slack auth state)
  - summon auth slack status    (show Slack auth status)
  - summon auth slack channels  (update monitored channels)
"""

from __future__ import annotations

import asyncio
import re

import click

from summon_claude.cli.config import (
    _check_github_status,
    github_auth_cmd,
    github_logout,
)
from summon_claude.cli.google_auth import (
    _check_google_status,
    google_auth,
    google_setup,
    google_status,
)
from summon_claude.cli.slack_auth import (
    _check_existing_slack_auth,
    slack_auth,
    slack_channels,
    slack_remove,
    slack_status,
)
from summon_claude.config import get_workspace_config_path

# ---------------------------------------------------------------------------
# summon auth
# ---------------------------------------------------------------------------


@click.group("auth")
def cmd_auth() -> None:
    """Manage authentication for external services."""


@cmd_auth.command("status")
@click.pass_context
def auth_status(ctx: click.Context) -> None:
    """Show authentication status for all configured providers."""
    quiet = ctx.obj.get("quiet", False) if ctx.obj else False

    any_configured = False

    # GitHub
    github_result = _check_github_status(prefix="  ", quiet=quiet)
    if github_result is not None:
        any_configured = True

    # Google
    google_result = _check_google_status(prefix="  ", quiet=quiet)
    if google_result is not None:
        any_configured = True

    # External Slack
    workspace_config_path = get_workspace_config_path()
    if workspace_config_path.exists():
        any_configured = True
        if not quiet:
            import json  # noqa: PLC0415

            try:
                workspace = json.loads(workspace_config_path.read_text())
                url = re.sub(r"[^\x20-\x7e]", "", workspace.get("url", "unknown"))
            except (json.JSONDecodeError, OSError, AttributeError):
                click.echo(
                    "  [FAIL] Slack: workspace config is corrupted"
                    " (re-run `summon auth slack login`)"
                )
                url = None
            if url is not None:
                existing = _check_existing_slack_auth()
                if existing:
                    click.echo(f"  [PASS] Slack: authenticated ({url}, {existing['age']})")
                else:
                    click.echo(f"  [FAIL] Slack: auth expired or missing ({url})")
    elif not quiet:
        click.echo("  [INFO] Slack: not configured (run `summon auth slack login`)")

    if not any_configured and not quiet:
        click.echo()
        click.echo("No authentication configured. Available providers:")
        click.echo("  summon auth github login    Authenticate with GitHub")
        click.echo("  summon auth google setup    Set up Google OAuth credentials")
        click.echo("  summon auth google login    Authenticate with Google Workspace")
        click.echo("  summon auth slack login     Authenticate with external Slack")


# ---------------------------------------------------------------------------
# summon auth github
# ---------------------------------------------------------------------------


@cmd_auth.group("github")
def auth_github() -> None:
    """GitHub authentication for MCP tools."""


@auth_github.command("login")
def auth_github_login() -> None:
    """Authenticate with GitHub using the device flow."""
    try:
        asyncio.run(github_auth_cmd())
    except KeyboardInterrupt:
        click.echo("\nAuthentication cancelled.", err=True)


@auth_github.command("logout")
def auth_github_logout() -> None:
    """Remove stored GitHub authentication."""
    github_logout()


# ---------------------------------------------------------------------------
# summon auth google
# ---------------------------------------------------------------------------


@cmd_auth.group("google")
def auth_google() -> None:
    """Google Workspace authentication for scribe monitoring."""


@auth_google.command("setup")
def auth_google_setup() -> None:
    """Interactive guided setup for Google OAuth credentials."""
    try:
        google_setup()
    except KeyboardInterrupt:
        click.echo("\nSetup cancelled.", err=True)


@auth_google.command("login")
def auth_google_login() -> None:
    """Authenticate with Google Workspace."""
    google_auth()


@auth_google.command("status")
def auth_google_status() -> None:
    """Check Google Workspace authentication status."""
    google_status()


# ---------------------------------------------------------------------------
# summon auth slack
# ---------------------------------------------------------------------------


@cmd_auth.group("slack")
def auth_slack() -> None:
    """External Slack workspace authentication for scribe monitoring."""


@auth_slack.command("login")
@click.argument("workspace")
def auth_slack_login(workspace: str) -> None:
    """Authenticate with an external Slack workspace.

    WORKSPACE can be a name (myteam), enterprise (acme.enterprise),
    or full URL (https://myteam.slack.com).
    """
    slack_auth(workspace)


@auth_slack.command("logout")
def auth_slack_logout() -> None:
    """Remove external Slack workspace auth state."""
    slack_remove()


@auth_slack.command("status")
def auth_slack_status() -> None:
    """Show external Slack workspace auth status."""
    slack_status()


@auth_slack.command("channels")
@click.option("--refresh", is_flag=True, help="Re-fetch channels from Slack")
def auth_slack_channels(refresh: bool) -> None:
    """Update monitored channel selection (no re-auth needed)."""
    slack_channels(refresh=refresh)
