"""CLI commands for external Slack workspace authentication."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import click

from summon_claude.config import (
    get_browser_auth_dir,
    get_config_file,
    get_workspace_config_path,
)

if TYPE_CHECKING:
    from summon_claude.slack_browser import SlackAuthResult


def _pick_channels(channels: list[dict[str, str]] | None) -> str:
    """Interactive channel picker. Returns comma-separated channel IDs.

    Reusable by both ``auth slack login`` and ``auth slack channels`` commands.
    Includes an empty-selection guard: if the user confirms with nothing
    selected (likely pressed Enter instead of Space), offers a retry.
    """
    if not channels:
        click.echo()
        click.echo("Could not detect channels from sidebar.")
        click.echo("To monitor specific channels, set their IDs in config:")
        click.echo("  summon config set SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS C01ABC,C02DEF")
        return ""

    click.echo()
    n = len(channels)
    click.echo(f"Found {n} sidebar channels (muted excluded).")
    click.echo("DMs and @mentions are always captured.")
    click.echo("Select channels for monitoring all messages (all messages, not just mentions).")

    if not sys.stdin.isatty():
        return _pick_channels_noninteractive(channels)

    import pick  # noqa: PLC0415

    # Build grouped display with non-selectable section headers
    display_options: list[str] = []
    channel_indices: list[int] = []
    current_section = ""
    for ch_idx, ch in enumerate(channels):
        section = ch.get("section", "Channels")
        if section != current_section:
            current_section = section
            display_options.append(f"── {section} ──")
            channel_indices.append(-1)
        display_options.append(f"  #{ch['name']}")
        channel_indices.append(ch_idx)

    pick_options = [pick.Option(opt, enabled=not opt.startswith("──")) for opt in display_options]
    title = (
        "Select channels to monitor\n"
        "  SPACE = toggle    ENTER = confirm\n"
        "DMs and @mentions are always captured "
        "— this is for monitoring all messages."
    )

    try:
        while True:
            selected_raw = pick.pick(
                pick_options,
                title,
                multiselect=True,
                min_selection_count=0,
                indicator=">",
            )
            selected_ch_indices = [
                channel_indices[int(s[1])]  # type: ignore[index]
                for s in selected_raw
                if channel_indices[int(s[1])] >= 0  # type: ignore[index]
            ]
            if selected_ch_indices:
                selected = [channels[i] for i in selected_ch_indices]
                result = ",".join(ch["id"] for ch in selected)
                names = ", ".join(f"#{ch['name']}" for ch in selected)
                click.echo(f"Selected: {names}")
                return result

            # Empty selection guard — user likely pressed Enter
            # instead of Space
            if not click.confirm(
                "No channels selected (use SPACE to toggle). Try again?",
                default=True,
            ):
                return ""
    except (KeyboardInterrupt, EOFError):
        click.echo("Skipped channel selection.")
        return ""


def _pick_channels_noninteractive(channels: list[dict[str, str]]) -> str:
    """Non-interactive fallback: numbered list with comma input."""
    for i, ch in enumerate(channels, 1):
        click.echo(f"  {i}) #{ch['name']}  ({ch['id']})")
    click.echo()
    selection = click.prompt(
        "Enter channel numbers (comma-separated, or Enter to skip)",
        default="",
    )
    if not selection.strip():
        return ""
    indices: list[int] = []
    for token in selection.split(","):
        stripped = token.strip()
        if stripped.isdigit():
            idx = int(stripped) - 1
            if 0 <= idx < len(channels):
                indices.append(idx)
    if not indices:
        return ""
    selected = [channels[i] for i in indices]
    result = ",".join(ch["id"] for ch in selected)
    names = ", ".join(f"#{ch['name']}" for ch in selected)
    click.echo(f"Selected: {names}")
    return result


def _save_monitored_channels(monitored_channels: str) -> None:
    """Save monitored channel IDs to the config file."""
    if not monitored_channels:
        return

    config_file = get_config_file()
    config_file.parent.mkdir(parents=True, exist_ok=True)
    lines = config_file.read_text().splitlines() if config_file.exists() else []
    key = "SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS"
    new_line = f"{key}={monitored_channels}"
    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = new_line
            updated = True
            break
    if not updated:
        lines.append(new_line)
    config_file.write_text("\n".join(lines) + "\n")
    click.echo(f"Monitored channels saved to {config_file}")


def _normalize_workspace(workspace: str) -> str:
    """Normalize a workspace name or URL to a full Slack URL.

    Accepts:
      - Full URL: ``https://myteam.slack.com`` → as-is
      - Bare name: ``myteam`` → ``https://myteam.slack.com``
      - Enterprise: ``acme.enterprise`` → ``https://acme.enterprise.slack.com``

    Raises ``SystemExit`` for explicit ``http://`` URLs (insecure).
    """
    # Reject explicit http:// — must use https
    if workspace.startswith("http://"):
        click.echo("Slack requires HTTPS. Use https:// or just the workspace name.", err=True)
        sys.exit(1)

    # Already a full URL
    if workspace.startswith("https://"):
        parsed = urlparse(workspace)
        if "@" in (parsed.netloc or ""):
            click.echo("Workspace URL must not contain credentials.", err=True)
            sys.exit(1)
        return workspace.rstrip("/")

    # Bare name or domain — strip trailing slashes
    workspace = workspace.rstrip("/")

    # Already has .slack.com
    if workspace.endswith(".slack.com"):
        return f"https://{workspace}"

    # Bare workspace name — append .slack.com
    return f"https://{workspace}.slack.com"


def _check_existing_slack_auth() -> dict[str, str] | None:
    """Check if valid Slack browser auth already exists.

    Reads the saved Playwright state file and checks the ``d`` cookie (Slack's
    primary auth cookie). Returns a dict with status info if credentials exist
    and appear valid, or ``None`` if missing/expired.
    """
    import datetime  # noqa: PLC0415
    import time  # noqa: PLC0415

    workspace_config_path = get_workspace_config_path()
    if not workspace_config_path.exists():
        return None

    try:
        workspace = json.loads(workspace_config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    state_path = Path(workspace.get("auth_state_path", ""))

    # [SEC] Validate path exists and is within expected directory (mirrors slack_remove guard)
    expected_dir = get_browser_auth_dir()
    if not state_path.is_file() or (
        state_path.name and not state_path.resolve().is_relative_to(expected_dir.resolve())
    ):
        return None

    # Check the primary auth cookie ("d") for expiry.
    # The d cookie is Slack's long-lived session cookie (~1 year).
    d_cookie = _find_slack_d_cookie(state_path)
    if not d_cookie:
        return None

    expires = d_cookie.get("expires", -1)
    if isinstance(expires, (int, float)) and 0 < expires < time.time():
        return None  # Primary auth cookie expired

    # Build status info
    mtime = state_path.stat().st_mtime
    saved_dt = datetime.datetime.fromtimestamp(mtime, tz=datetime.UTC)
    age = datetime.datetime.now(tz=datetime.UTC) - saved_dt
    if age.days > 0:
        age_str = f"{age.days}d ago"
    else:
        hours = age.seconds // 3600
        age_str = f"{hours}h ago" if hours > 0 else "just now"

    return {
        "saved": saved_dt.strftime("%Y-%m-%d %H:%M UTC"),
        "age": age_str,
        "user_id": workspace.get("user_id", ""),
        "url": workspace.get("url", ""),
    }


def _find_slack_d_cookie(state_path: Path) -> dict | None:
    """Find Slack's ``d`` auth cookie in a Playwright state file."""
    try:
        state = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    cookies = state.get("cookies", [])
    return next((c for c in cookies if c.get("name") == "d"), None)


def slack_auth(workspace: str) -> None:
    """Interactive Slack workspace authentication via Playwright.

    Opens a real browser window for the user to log in. Saves auth state
    to ``get_browser_auth_dir()``.

    Accepts a workspace name (``myteam``), enterprise name (``acme.enterprise``),
    or full URL (``https://myteam.slack.com``).
    """
    workspace_url = _normalize_workspace(workspace)
    parsed = urlparse(workspace_url)
    if not (parsed.netloc.endswith(".slack.com") or parsed.netloc == "slack.com"):
        click.echo(
            f"Cannot resolve workspace {workspace!r} to a Slack URL. "
            "Expected a name like 'myteam' or URL like https://myteam.slack.com",
            err=True,
        )
        sys.exit(1)

    try:
        from summon_claude.slack_browser import interactive_slack_auth  # noqa: PLC0415
    except ImportError:
        click.echo(
            "External Slack support requires the 'slack-browser' extra: "
            "uv pip install summon-claude[slack-browser]",
            err=True,
        )
        sys.exit(1)

    # Check for existing valid credentials before launching browser
    existing = _check_existing_slack_auth()
    if existing:
        click.echo(f"Slack auth already configured for {existing['url']}")
        click.echo(f"  Saved: {existing['saved']} ({existing['age']})")
        if existing["user_id"]:
            click.echo(f"  User:  {existing['user_id']}")
        if not click.confirm("Re-authenticate?", default=False):
            return

    browser_type = os.environ.get("SUMMON_SCRIBE_SLACK_BROWSER", "chrome")

    click.echo(f"Opening {browser_type} browser for Slack authentication at {workspace_url}")
    click.echo("Complete the login in the browser window.")
    click.echo("The browser will close automatically after detecting your session.")
    click.echo("WARNING: Auth state contains session cookies — treat stored files as secrets.")

    try:
        result = asyncio.run(interactive_slack_auth(workspace_url, browser_type))
    except Exception as e:
        click.echo(f"Slack login failed: {e}", err=True)
        sys.exit(1)

    effective_url = result.resolved_url or workspace_url

    identity = result.user_id or "unknown"
    click.echo(f"Slack authenticated as {identity}.")
    click.echo(f"Credentials stored in {result.state_file.parent}")
    click.echo(f"  Team ID:  {result.team_id or 'not detected'}")
    click.echo(f"  Channels: {len(result.channels) if result.channels else 0} found")
    if result.resolved_url and result.resolved_url.rstrip("/") != workspace_url.rstrip("/"):
        click.echo(f"  Resolved: {result.resolved_url} (redirected from {workspace_url})")

    _save_workspace_config(result, effective_url, browser_type)
    click.echo()
    click.echo("Slack monitoring will be available on next project start.")


def _save_workspace_config(
    result: SlackAuthResult,
    workspace_url: str,
    browser_type: str,
) -> None:
    """Save workspace metadata and prompt for user ID and channels."""
    # Defense-in-depth: validate URL before persisting to config
    parsed = urlparse(workspace_url)
    if not (
        parsed.scheme == "https"
        and (parsed.netloc.endswith(".slack.com") or parsed.netloc == "slack.com")
        and "@" not in parsed.netloc
    ):
        raise ValueError(f"Refusing to save invalid workspace URL: {workspace_url!r}")

    # User ID
    if result.user_id:
        click.echo(f"Auto-detected user ID: {result.user_id}")
        user_id = result.user_id
    else:
        click.echo()
        click.echo("Could not auto-detect user ID.")
        click.echo("To enable @mention detection, enter your Slack user ID for this workspace.")
        click.echo("Find it: click your profile picture → Profile → ⋮ → Copy member ID")
        user_id = click.prompt("External workspace user ID (or press Enter to skip)", default="")

    # Interactive channel selection
    monitored_channels = _pick_channels(result.channels)

    # Save workspace metadata
    workspace_config: dict[str, str] = {
        "url": workspace_url,
        "auth_state_path": str(result.state_file),
        "browser_type": browser_type,
    }
    if user_id:
        workspace_config["user_id"] = user_id
    if result.team_id:
        workspace_config["team_id"] = result.team_id
    if result.channels:
        workspace_config["channels"] = result.channels  # type: ignore[assignment]
    config_path = get_workspace_config_path()
    config_path.write_text(json.dumps(workspace_config, indent=2))
    config_path.chmod(0o600)

    _save_monitored_channels(monitored_channels)

    click.echo(f"Workspace config saved to {config_path}")
    if not user_id:
        click.echo(
            "Note: @mention detection disabled (no user ID)."
            " Re-run `summon auth slack login` to add it."
        )


def slack_status() -> None:
    """Show external Slack workspace configuration and auth status."""
    config_path = get_workspace_config_path()
    if not config_path.exists():
        click.echo("No external Slack workspace configured.")
        click.echo("Run: summon auth slack login <workspace-url>")
        return

    try:
        workspace = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        click.echo("Workspace config is corrupted. Re-run: summon auth slack login")
        return

    from summon_claude.cli.formatting import format_tag  # noqa: PLC0415

    url = workspace.get("url", "N/A")
    existing = _check_existing_slack_auth()
    if existing:
        tag = format_tag("PASS")
        click.echo(f"{tag} Slack: authenticated (workspace: {url}, saved {existing['age']})")
    else:
        click.echo(f"{format_tag('FAIL')} Slack: auth expired or missing ({url})")
    click.echo()
    click.echo(f"Workspace URL: {workspace.get('url', 'N/A')}")
    user_id = workspace.get("user_id", "")
    click.echo(f"User ID: {user_id or 'not set (re-run `summon auth slack login` to add)'}")

    state_path = Path(workspace.get("auth_state_path", ""))
    if state_path.exists():
        import datetime  # noqa: PLC0415

        mtime = datetime.datetime.fromtimestamp(state_path.stat().st_mtime, tz=datetime.UTC)
        click.echo(f"Auth state: {state_path} (saved {mtime.isoformat()})")
    else:
        click.echo("Auth state: MISSING (re-run `summon auth slack login`)")

    channels = os.environ.get("SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS", "")
    if channels:
        click.echo(f"Monitored channels: {channels}")
    else:
        click.echo("Monitored channels: none (DMs and @mentions always captured)")
    click.echo()
    click.echo("How to find IDs:")
    click.echo("  User ID: click profile picture > Profile > ... > Copy member ID")
    click.echo("  Channel ID: right-click channel > View channel details > ID at bottom")
    click.echo("  Set channels: summon config set SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS C01,C02")


def slack_remove() -> None:
    """Remove stored Slack credentials."""
    config_path = get_workspace_config_path()
    if not config_path.exists():
        click.echo("No Slack credentials stored.")
        return

    if not click.confirm(
        "Remove Slack credentials? Reconfigure by running 'summon auth slack login' again.",
    ):
        return

    try:
        workspace = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        click.echo("Workspace config is corrupted — removing config file only.")
        with contextlib.suppress(FileNotFoundError):
            config_path.unlink()
        return
    state_path = Path(workspace.get("auth_state_path", ""))

    # [SEC] Validate path is within expected directory before unlinking
    expected_dir = get_browser_auth_dir()
    if state_path.name and (not state_path.resolve().is_relative_to(expected_dir.resolve())):
        click.echo(
            f"Auth state path {state_path} is outside expected directory — skipping removal.",
            err=True,
        )
    else:
        with contextlib.suppress(FileNotFoundError):
            state_path.unlink()

    with contextlib.suppress(FileNotFoundError):
        config_path.unlink()

    click.echo("Slack credentials removed.")


def slack_channels(*, refresh: bool = False) -> None:
    """Update monitored channel selection.

    Uses cached channel list from workspace config by default.
    With ``--refresh``, re-fetches from Slack via Playwright.
    """
    config_path = get_workspace_config_path()
    if not config_path.exists():
        click.echo("No external Slack workspace configured.")
        click.echo("Run: summon auth slack login <workspace>")
        return

    try:
        workspace = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        click.echo("Workspace config is corrupted. Re-run: summon auth slack login")
        return
    workspace_url = workspace.get("url", "")
    cached_channels = workspace.get("channels")

    channels: list[dict[str, str]] | None = None

    if not refresh and cached_channels:
        click.echo(f"Using cached channel list ({len(cached_channels)} channels).")
        click.echo("Run with --refresh to re-fetch from Slack.")
        channels = cached_channels
    else:
        channels = _fetch_channels_via_playwright(workspace)
        if channels:
            # Update cache
            workspace["channels"] = channels
            config_path.write_text(json.dumps(workspace, indent=2))
            config_path.chmod(0o600)

    if not channels:
        click.echo("Could not load channels — auth state may be expired.")
        click.echo(f"Re-run: summon auth slack login {workspace_url}")
        return

    monitored = _pick_channels(channels)
    _save_monitored_channels(monitored)


def _fetch_channels_via_playwright(
    workspace: dict,
) -> list[dict[str, str]] | None:
    """Load channels from Slack via headless Playwright."""
    workspace_url = workspace.get("url", "")
    state_path = Path(workspace.get("auth_state_path", ""))
    browser_type = workspace.get("browser_type", "chrome")

    if not state_path.is_file():
        click.echo("Auth state expired or missing.")
        click.echo(f"Re-run: summon auth slack login {workspace_url}")
        return None

    try:
        from summon_claude.slack_browser import (  # noqa: PLC0415
            _extract_channels,
            _launch_browser,
            _resolve_client_url,
        )
    except ImportError:
        click.echo(
            "External Slack support requires the 'slack-browser' extra: "
            "uv pip install summon-claude[slack-browser]",
            err=True,
        )
        return None

    click.echo(f"Loading channels from {workspace_url}...")

    async def _load() -> list[dict[str, str]]:
        from playwright.async_api import async_playwright  # noqa: PLC0415

        async with async_playwright() as p:
            browser = await _launch_browser(p, browser_type, headless=True)
            context = await browser.new_context(
                storage_state=str(state_path),
            )
            page = await context.new_page()
            nav_url = _resolve_client_url(workspace_url, state_path)
            await page.goto(nav_url, wait_until="domcontentloaded")

            try:
                await page.wait_for_url(
                    "https://*.slack.com/client/**",
                    timeout=30000,
                    wait_until="commit",
                )
            except Exception:
                await browser.close()
                return []

            with contextlib.suppress(Exception):
                await page.wait_for_selector(
                    '[data-qa^="channel_sidebar_name_"]',
                    timeout=5000,
                )

            result = await _extract_channels(page, workspace_url)
            await browser.close()
            return result

    result = asyncio.run(_load())
    return result or None
