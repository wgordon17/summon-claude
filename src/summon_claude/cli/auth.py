# pyright: reportFunctionMemberAccess=false
"""CLI auth subcommands: unified auth group with provider subgroups.

Consolidates all authentication commands under `summon auth`:
  - summon auth status          (unified status of all providers)
  - summon auth github login    (GitHub OAuth device flow)
  - summon auth github logout   (remove GitHub credentials)
  - summon auth github status   (check GitHub auth status)
  - summon auth google setup    (guided Google OAuth setup)
  - summon auth google login    (Google OAuth)
  - summon auth google logout   (remove Google credentials)
  - summon auth google status   (check Google creds)
  - summon auth jira login      (Jira OAuth 2.1 with PKCE + DCR)
  - summon auth jira logout     (remove Jira credentials)
  - summon auth jira status     (check Jira auth status)
  - summon auth slack login     (external Slack browser auth)
  - summon auth slack logout    (remove Slack credentials)
  - summon auth slack status    (show Slack auth status)
  - summon auth slack channels  (update monitored channels)
"""

from __future__ import annotations

import asyncio
import json
import re

import click

from summon_claude.cli.config import (
    _check_github_status,
    _check_github_status_data,
    github_auth_cmd,
    github_logout,
)
from summon_claude.cli.formatting import (
    auth_authenticated_msg,
    auth_cancelled,
    auth_login_success,
    auth_not_configured_msg,
    auth_not_stored,
    auth_removed,
    auth_status_line,
    make_auth_status_data,
)
from summon_claude.cli.google_auth import (
    _check_google_status,
    _check_google_status_data,
    google_auth,
    google_logout,
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
# Data-returning helpers for --json output
# ---------------------------------------------------------------------------


def _check_jira_status_data() -> dict:
    """Return Jira auth status as a dict for --json output.

    Reads the token file once to extract both status and site name,
    avoiding the double-read of calling check_jira_status() + get_jira_site_name().
    """
    import time  # noqa: PLC0415

    from summon_claude.jira_auth import get_jira_token_path, jira_credentials_exist  # noqa: PLC0415

    if not jira_credentials_exist():
        return make_auth_status_data("jira", "not_configured")

    # Single read — mirrors check_jira_status() validation + get_jira_site_name() extraction
    token_path = get_jira_token_path()
    try:
        token_data = json.loads(token_path.read_text())
    except (json.JSONDecodeError, OSError):
        return make_auth_status_data("jira", "error", error="corrupt or unreadable")

    if not token_data.get("access_token"):
        return make_auth_status_data("jira", "error", error="missing access_token")

    if not token_data.get("cloud_id"):
        return make_auth_status_data("jira", "error", error="missing cloud_id")

    expires_at = token_data.get("expires_at", 0)
    if time.time() >= expires_at and not token_data.get("refresh_token"):
        return make_auth_status_data("jira", "error", error="expired without refresh_token")

    result = make_auth_status_data("jira", "authenticated")
    name = token_data.get("cloud_name")
    if name:
        site = re.sub(r"[^\x20-\x7e]", "", name)[:80]
        if site:
            result["site"] = site
    return result


def _check_slack_status_data() -> dict:
    wcp = get_workspace_config_path()
    if not wcp.exists():
        return make_auth_status_data("slack", "not_configured")
    try:
        workspace = json.loads(wcp.read_text())
        url = re.sub(r"[^\x20-\x7e]", "", workspace.get("url", "unknown"))[:200]
    except (json.JSONDecodeError, OSError, AttributeError):
        return make_auth_status_data("slack", "error", error="corrupted config")
    existing = _check_existing_slack_auth()
    if existing:
        return make_auth_status_data(
            "slack",
            "authenticated",
            workspace_url=url,
            saved_at=existing["saved_iso"],
        )
    return make_auth_status_data("slack", "error", error="expired", workspace_url=url)


def _check_jira_status(*, prefix: str = "", quiet: bool = False) -> bool | None:
    """Check Jira authentication status.

    Returns True if valid, False if broken, None if not configured.
    """
    from summon_claude.jira_auth import (  # noqa: PLC0415
        check_jira_status,
        get_jira_site_name,
        jira_credentials_exist,
    )

    if not jira_credentials_exist():
        if not quiet:
            click.echo(
                auth_status_line(
                    "Jira",
                    status="not_configured",
                    message=auth_not_configured_msg("summon auth jira login"),
                    prefix=prefix,
                )
            )
        return None

    err = check_jira_status()
    if err is not None:
        if not quiet:
            click.echo(auth_status_line("Jira", status="error", message=err, prefix=prefix))
        return False

    if not quiet:
        site = get_jira_site_name()
        detail = f"site: {site}" if site else ""
        click.echo(
            auth_status_line(
                "Jira",
                status="authenticated",
                message=auth_authenticated_msg(detail=detail) if detail else "authenticated",
                prefix=prefix,
            )
        )
    return True


# ---------------------------------------------------------------------------
# summon auth
# ---------------------------------------------------------------------------


@click.group("auth")
def cmd_auth() -> None:
    """Manage authentication for external services."""


@cmd_auth.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def auth_status(ctx: click.Context, as_json: bool) -> None:
    """Show authentication status for all configured providers."""
    if as_json:
        providers = [
            _check_github_status_data(),
            _check_google_status_data(),
            _check_jira_status_data(),
            _check_slack_status_data(),
        ]
        click.echo(json.dumps({"providers": providers}, indent=2))
        return

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
    jira_result = _check_jira_status(prefix="  ", quiet=quiet)
    if jira_result is not None:
        any_configured = True

    # External Slack
    workspace_config_path = get_workspace_config_path()
    if workspace_config_path.exists():
        any_configured = True
        if not quiet:
            existing = _check_existing_slack_auth()
            if existing:
                url = re.sub(r"[^\x20-\x7e]", "", existing.get("url", "unknown"))[:200]
                age = existing["age"]
                click.echo(
                    auth_status_line(
                        "Slack",
                        status="authenticated",
                        message=auth_authenticated_msg(
                            detail=f"workspace: {url}, saved {age}",
                        ),
                        prefix="  ",
                    )
                )
            else:
                # Config exists but auth is expired/missing — read URL for display
                try:
                    workspace = json.loads(workspace_config_path.read_text())
                    url = re.sub(r"[^\x20-\x7e]", "", workspace.get("url", "unknown"))[:200]
                except (json.JSONDecodeError, OSError, AttributeError):
                    click.echo(
                        auth_status_line(
                            "Slack",
                            status="error",
                            message="workspace config is corrupted"
                            " (re-run `summon auth slack login`)",
                            prefix="  ",
                        )
                    )
                    url = None
                if url is not None:
                    click.echo(
                        auth_status_line(
                            "Slack",
                            status="error",
                            message=f"auth expired or missing ({url})",
                            prefix="  ",
                        )
                    )
    elif not quiet:
        click.echo(
            auth_status_line(
                "Slack",
                status="not_configured",
                message=auth_not_configured_msg("summon auth slack login"),
                prefix="  ",
            )
        )

    if not any_configured and not quiet:
        click.echo()
        click.echo("No authentication configured. Available providers:")
        click.echo("  summon auth github login    Authenticate with GitHub")
        click.echo("  summon auth google setup    Set up Google OAuth credentials")
        click.echo("  summon auth google login    Authenticate with Google")
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
        click.echo(auth_cancelled(), err=True)


@auth_github.command("logout")
def auth_github_logout() -> None:
    """Remove stored GitHub authentication."""
    github_logout()


@auth_github.command("status")
def auth_github_status_cmd() -> None:
    """Check GitHub authentication status."""
    _check_github_status()


# ---------------------------------------------------------------------------
# summon auth google
# ---------------------------------------------------------------------------


@cmd_auth.group("google")
def auth_google() -> None:
    """Google authentication."""


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
    """Authenticate with Google."""
    try:
        google_auth(account=account)
    except KeyboardInterrupt:
        click.echo(auth_cancelled(), err=True)


@auth_google.command("status")
@click.option("--account", default=None, help="Account label (e.g., personal, work)")
def auth_google_status(account: str | None) -> None:
    """Check Google authentication status."""
    google_status(account=account)


@auth_google.command("logout")
@click.option("--account", default=None, help="Account label (e.g., personal, work)")
def auth_google_logout(account: str | None) -> None:
    """Remove stored Google credentials."""
    google_logout(account=account)


# ---------------------------------------------------------------------------
# summon auth slack
# ---------------------------------------------------------------------------


@cmd_auth.group("slack")
def auth_slack() -> None:
    """External Slack workspace authentication."""


@auth_slack.command("login")
@click.argument("workspace")
def auth_slack_login(workspace: str) -> None:
    """Authenticate with an external Slack workspace.

    WORKSPACE can be a name (myteam), enterprise (acme.enterprise),
    or full URL (https://myteam.slack.com).
    """
    try:
        slack_auth(workspace)
    except KeyboardInterrupt:
        click.echo(auth_cancelled(), err=True)


@auth_slack.command("logout")
def auth_slack_logout() -> None:
    """Remove stored Slack credentials."""
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
    """Jira authentication for MCP tools."""


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
        get_jira_token_path,
        load_jira_token,
        save_jira_token,
        start_auth_flow,
        try_refresh_only,
    )

    try:
        # Try refresh first — skip browser if refresh token is still valid
        # Idempotent: fresh DCR + PKCE flow, overwrites existing token.json
        if asyncio.run(try_refresh_only()):
            token = load_jira_token()
            if token:
                site_name = token.get("cloud_name", "Unknown")
                auth_login_success(
                    "Jira",
                    detail=f"site: {site_name}",
                    storage_path=get_jira_token_path().parent,
                    next_step="Jira MCP tools will be available on next session start.",
                )
                return

        click.echo("Opening browser for Jira authentication...")
        try:
            token_data = asyncio.run(start_auth_flow())
        except TimeoutError as e:
            click.echo(f"Jira authentication timed out: {e}", err=True)
            sys.exit(1)
        except RuntimeError as e:
            click.echo(f"Jira authentication failed: {e}", err=True)
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
                        f"Warning: site discovery unavailable"
                        f" — storing '{site_host}' as cloud_id."
                        " MCP tools may require a UUID;"
                        " re-run login without --site if issues arise.",
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
        auth_login_success(
            "Jira",
            detail=f"site: {site_label}",
            storage_path=get_jira_token_path().parent,
            next_step="Jira MCP tools will be available on next session start.",
        )
    except (KeyboardInterrupt, click.Abort):
        click.echo(auth_cancelled(), err=True)


@auth_jira.command("logout")
def auth_jira_logout() -> None:
    """Remove stored Jira OAuth credentials."""
    from summon_claude.jira_auth import jira_credentials_exist, logout  # noqa: PLC0415

    if not jira_credentials_exist():
        click.echo(auth_not_stored("Jira"))
        return
    logout()
    click.echo(auth_removed("Jira"))


@auth_jira.command("status")
def auth_jira_status() -> None:
    """Check Jira authentication status."""
    _check_jira_status()
