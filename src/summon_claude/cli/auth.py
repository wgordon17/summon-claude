# pyright: reportFunctionMemberAccess=false
"""CLI auth subcommands: unified auth group with provider subgroups.

Consolidates all authentication commands under `summon auth`:
  - summon auth status          (unified status of all providers)
  - summon auth github login    (GitHub OAuth device flow)
  - summon auth github logout   (remove GitHub token)
  - summon auth google login    (Google Workspace OAuth)
  - summon auth google status   (check Google creds)
  - summon auth jira login      (Jira OAuth 2.1 with PKCE + DCR)
  - summon auth jira logout     (remove Jira credentials)
  - summon auth jira status     (check Jira auth status)
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
def auth_status(ctx: click.Context) -> None:  # noqa: PLR0912
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

    # Jira
    from summon_claude.jira_auth import (  # noqa: PLC0415
        check_jira_status,
        jira_credentials_exist,
    )

    if jira_credentials_exist():
        any_configured = True
        jira_err = check_jira_status()
        if jira_err is None:
            if not quiet:
                click.echo("  [PASS] Jira: authenticated")
        elif not quiet:
            click.echo(f"  [FAIL] Jira: {jira_err}")
    elif not quiet:
        click.echo("  [INFO] Jira: not configured (run `summon auth jira login`)")

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
        click.echo("  summon auth jira login      Authenticate with Jira")
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
@click.option("--account", default=None, help="Account label (e.g., personal, work)")
def auth_google_setup(account: str | None) -> None:
    """Interactive guided setup for Google OAuth credentials."""
    try:
        google_setup(account=account)
    except KeyboardInterrupt:
        click.echo("\nSetup cancelled.", err=True)


@auth_google.command("login")
@click.option("--account", default=None, help="Account label (e.g., personal, work)")
def auth_google_login(account: str | None) -> None:
    """Authenticate with Google Workspace."""
    google_auth(account=account)


@auth_google.command("status")
@click.option("--account", default=None, help="Account label (e.g., personal, work)")
def auth_google_status(account: str | None) -> None:
    """Check Google Workspace authentication status."""
    google_status(account=account)


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


# ---------------------------------------------------------------------------
# summon auth jira
# ---------------------------------------------------------------------------


_SAFE_HOSTNAME_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$"
)


def _normalize_site(site: str) -> str:
    """Normalize a site input to a bare hostname (e.g. 'myorg' → 'myorg.atlassian.net').

    Raises ``click.BadParameter`` if the result is not a valid hostname.
    """
    s = site.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    # Strip any path component (e.g. "myorg.atlassian.net/wiki" → "myorg.atlassian.net")
    s = s.split("/")[0]
    if "." not in s:
        s = f"{s}.atlassian.net"
    if not _SAFE_HOSTNAME_RE.match(s):
        raise click.BadParameter(f"'{site}' does not look like a valid hostname")
    return s


def _extract_site_host(url: str) -> str:
    """Extract hostname from an API-returned URL, returning '' on failure.

    Unlike ``_normalize_site``, this never raises — it is used on API response
    data (``discover_cloud_sites``), not user input.
    """
    from urllib.parse import urlparse  # noqa: PLC0415

    if not url:
        return ""
    hostname = urlparse(url).hostname
    return hostname or ""


@cmd_auth.group("jira")
def auth_jira() -> None:
    """Jira authentication (OAuth 2.1 with PKCE + DCR)."""


@auth_jira.command("login")
@click.option(
    "--site",
    default=None,
    help="Atlassian site (e.g. 'myorg' or 'myorg.atlassian.net'). "
    "Resolves to a cloud UUID via API discovery when possible.",
)
def auth_jira_login(site: str | None) -> None:  # noqa: PLR0912, PLR0915
    """Authenticate with Jira via OAuth 2.1."""
    import sys  # noqa: PLC0415

    from summon_claude.jira_auth import (  # noqa: PLC0415
        discover_cloud_sites,
        load_jira_token,
        save_jira_token,
        start_auth_flow,
        try_refresh_only,
    )

    # Try refresh first — skip browser if refresh token is still valid
    # Idempotent: fresh DCR + PKCE flow, overwrites existing token.json
    if asyncio.run(try_refresh_only()):
        token = load_jira_token()
        if token:
            site_name = token.get("cloud_name", "Unknown")
            click.echo(f"Jira credentials refreshed successfully. Connected to {site_name}")
            return

    click.echo("Starting Jira OAuth flow — a browser window will open.")
    try:
        token_data = asyncio.run(start_auth_flow())
    except TimeoutError as e:
        click.echo(f"Timed out: {e}", err=True)
        sys.exit(1)
    except RuntimeError as e:
        click.echo(f"Authentication failed: {e}", err=True)
        sys.exit(1)

    # Resolve cloud site: always discover via API first (to get the UUID),
    # then use --site as a filter or manual prompt as a last resort.
    access_token = token_data.get("access_token", "")
    sites = asyncio.run(discover_cloud_sites(access_token))

    if site:
        # --site narrows the discovery results by hostname match
        site_host = _normalize_site(site)
        matched = [s for s in sites if _extract_site_host(s.get("url", "")) == site_host]
        if matched:
            token_data["cloud_id"] = matched[0]["id"]
            token_data["cloud_name"] = matched[0].get("name", "")
        else:
            # Fallback: store hostname (not UUID) with a warning
            if sites:
                click.echo(
                    f"Warning: --site '{site}' did not match any discovered site. "
                    f"Available: {', '.join(s.get('name', '') for s in sites)}",
                    err=True,
                )
            else:
                click.echo(
                    f"Warning: site discovery unavailable — storing '{site_host}' as cloud_id. "
                    "MCP tools may require a UUID; re-run login without --site if issues arise.",
                    err=True,
                )
            token_data["cloud_id"] = site_host
            token_data["cloud_name"] = site_host.split(".")[0]
    elif sites:
        if len(sites) == 1:
            chosen = sites[0]
        else:
            click.echo("Multiple Atlassian cloud sites found:")
            for i, s in enumerate(sites, 1):
                click.echo(f"  {i}. {s.get('name', '')} ({s.get('url', '')})")
            idx = click.prompt("Select a site", type=click.IntRange(1, len(sites)), default=1)
            chosen = sites[idx - 1]
        token_data["cloud_id"] = chosen["id"]
        token_data["cloud_name"] = chosen.get("name", "")
    else:
        org = click.prompt("Enter your Atlassian org name (e.g. 'myorg')")
        site_host = _normalize_site(org)
        click.echo(
            f"Warning: storing '{site_host}' as cloud_id — MCP tools may require a UUID. "
            "Re-run login if issues arise.",
            err=True,
        )
        token_data["cloud_id"] = site_host
        token_data["cloud_name"] = org.strip()

    save_jira_token(token_data)
    site_label = token_data.get("cloud_name", token_data["cloud_id"])
    click.echo(f"Jira authenticated (site: {site_label}).")


@auth_jira.command("logout")
def auth_jira_logout() -> None:
    """Remove stored Jira OAuth credentials."""
    from summon_claude.jira_auth import jira_credentials_exist, logout  # noqa: PLC0415

    if not jira_credentials_exist():
        click.echo("No Jira credentials to remove.")
        return
    logout()
    click.echo("Jira credentials removed.")


@auth_jira.command("status")
def auth_jira_status() -> None:
    """Check Jira authentication status."""
    from summon_claude.jira_auth import check_jira_status  # noqa: PLC0415

    err = check_jira_status()
    if err is None:
        click.echo("Jira: authenticated")
    else:
        click.echo(f"Jira: {err}")
