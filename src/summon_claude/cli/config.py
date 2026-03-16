"""CLI config subcommands: show, path, edit, set."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import click

from summon_claude.config import (
    find_workspace_mcp_bin,
    get_config_file,
    get_data_dir,
    get_google_credentials_dir,
    google_mcp_env,
)
from summon_claude.sessions.migrations import CURRENT_SCHEMA_VERSION, get_schema_version
from summon_claude.sessions.registry import SessionRegistry

logger = logging.getLogger(__name__)

_SETTABLE_KEYS = frozenset(
    {
        "SUMMON_SLACK_BOT_TOKEN",
        "SUMMON_SLACK_APP_TOKEN",
        "SUMMON_SLACK_SIGNING_SECRET",
        "SUMMON_DEFAULT_MODEL",
        "SUMMON_DEFAULT_EFFORT",
        "SUMMON_CHANNEL_PREFIX",
        "SUMMON_PERMISSION_DEBOUNCE_MS",
        "SUMMON_MAX_INLINE_CHARS",
        # Scribe agent
        "SUMMON_SCRIBE_ENABLED",
        "SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES",
        "SUMMON_SCRIBE_CWD",
        "SUMMON_SCRIBE_MODEL",
        "SUMMON_SCRIBE_IMPORTANCE_KEYWORDS",
        "SUMMON_SCRIBE_QUIET_HOURS",
        "SUMMON_SCRIBE_GOOGLE_SERVICES",
        "SUMMON_SCRIBE_SLACK_ENABLED",
        "SUMMON_SCRIBE_SLACK_BROWSER",
        "SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS",
    }
)


def _require_config_file(override: str | None = None):
    """Return the config file Path if it exists, else print a hint and return None."""
    config_file = get_config_file(override)
    if not config_file.exists():
        click.echo(f"No config file found at {config_file}")
        click.echo("Run `summon init` to create one.")
        return None
    return config_file


def config_path(override: str | None = None) -> None:
    click.echo(str(get_config_file(override)))


def config_show(override: str | None = None) -> None:
    config_file = _require_config_file(override)
    if config_file is None:
        return

    secret_keys = {
        "SUMMON_SLACK_BOT_TOKEN",
        "SUMMON_SLACK_APP_TOKEN",
        "SUMMON_SLACK_SIGNING_SECRET",
    }

    for raw_line in config_file.read_text().splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            k, _, v = stripped.partition("=")
            k = k.strip()
            v = v.strip()
            if k in secret_keys:
                click.echo(f"{k}={'configured' if v else 'missing'}")
            else:
                click.echo(f"{k}={v}")
        else:
            click.echo(raw_line)


def config_edit(override: str | None = None) -> None:
    config_file = _require_config_file(override)
    if config_file is None:
        return

    editor = os.environ.get("EDITOR", "vi")
    try:
        subprocess.run([editor, str(config_file)], check=False)  # noqa: S603
    except FileNotFoundError:
        click.echo(f"Editor '{editor}' not found. Set $EDITOR to your preferred editor.", err=True)
        sys.exit(1)


def config_set(key: str, value: str, override: str | None = None) -> None:
    key = key.strip().upper()
    if key not in _SETTABLE_KEYS:
        click.echo(f"Unknown config key: {key!r}", err=True)
        click.echo(f"Valid keys: {', '.join(sorted(_SETTABLE_KEYS))}", err=True)
        sys.exit(1)

    # Strip newlines to prevent injection into the .env format
    value = value.replace("\n", "").replace("\r", "")

    config_file = get_config_file(override)
    config_file.parent.mkdir(parents=True, exist_ok=True)

    # Read existing lines
    lines = config_file.read_text().splitlines() if config_file.exists() else []

    updated = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped == key:
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f"{key}={value}")

    config_file.write_text("\n".join(new_lines) + "\n")
    with contextlib.suppress(OSError):
        config_file.chmod(0o600)
    click.echo(f"Set {key} in {config_file}")


def _ensure_google_client_secrets() -> dict[str, str]:
    """Ensure Google OAuth client credentials are available.

    Checks env vars first, then prompts interactively.  Returns env
    dict with ``GOOGLE_OAUTH_CLIENT_ID`` and ``GOOGLE_OAUTH_CLIENT_SECRET``.
    """
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")

    if client_id and client_secret:
        click.echo("Using Google OAuth credentials from environment.")
        return {"GOOGLE_OAUTH_CLIENT_ID": client_id, "GOOGLE_OAUTH_CLIENT_SECRET": client_secret}

    # Check if we saved them previously in summon's config
    secrets_file = get_google_credentials_dir() / "client_env"
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            if line.startswith("GOOGLE_OAUTH_CLIENT_ID="):
                client_id = line.split("=", 1)[1].strip()
            elif line.startswith("GOOGLE_OAUTH_CLIENT_SECRET="):
                client_secret = line.split("=", 1)[1].strip()
        if client_id and client_secret:
            return {
                "GOOGLE_OAUTH_CLIENT_ID": client_id,
                "GOOGLE_OAUTH_CLIENT_SECRET": client_secret,
            }

    # Interactive prompt
    click.echo("Google OAuth client credentials are required.")
    click.echo("Get these from https://console.cloud.google.com/apis/credentials")
    click.echo("  1. Create or select a project")
    click.echo("  2. Enable Gmail, Calendar, and Drive APIs")
    click.echo("  3. Create an OAuth 2.0 Client ID (Desktop app type)")
    click.echo("  4. Download the JSON file or copy the Client ID + Secret")
    click.echo()
    response = click.prompt(
        "Path to client_secret.json (or paste Client ID)", default="", show_default=False
    )
    if not response:
        click.echo("Client credentials are required.", err=True)
        sys.exit(1)

    import json  # noqa: PLC0415

    json_path = Path(response.strip()).expanduser()
    if json_path.suffix == ".json" or json_path.exists():
        # User provided a JSON file path
        if not json_path.exists():
            click.echo(f"File not found: {json_path}", err=True)
            sys.exit(1)
        try:
            data = json.loads(json_path.read_text())
            # Google's format nests under "installed" or "web"
            inner = data.get("installed") or data.get("web") or data
            client_id = inner["client_id"]
            client_secret = inner["client_secret"]
        except (json.JSONDecodeError, KeyError) as e:
            click.echo(f"Invalid client_secret.json: {e}", err=True)
            sys.exit(1)
        # Copy the JSON to our credentials dir for workspace-mcp
        dest = secrets_file.parent / "client_secret.json"
        secrets_file.parent.mkdir(parents=True, exist_ok=True)
        import shutil  # noqa: PLC0415

        shutil.copy2(str(json_path), str(dest))
        with contextlib.suppress(OSError):
            dest.chmod(0o600)
        click.echo(f"Copied {json_path.name} to {dest}")
    else:
        # User pasted a Client ID directly
        client_id = response.strip()
        client_secret = click.prompt("Google OAuth Client Secret", default="", show_default=False)
        if not client_secret:
            click.echo("Client Secret is required.", err=True)
            sys.exit(1)

    # Persist env-style for future runs
    secrets_file.parent.mkdir(parents=True, exist_ok=True)
    secrets_file.write_text(
        f"GOOGLE_OAUTH_CLIENT_ID={client_id}\nGOOGLE_OAUTH_CLIENT_SECRET={client_secret}\n"
    )
    with contextlib.suppress(OSError):
        secrets_file.chmod(0o600)
    click.echo(f"Saved credentials to {secrets_file}")

    return {"GOOGLE_OAUTH_CLIENT_ID": client_id, "GOOGLE_OAUTH_CLIENT_SECRET": client_secret}


def google_auth() -> None:
    """Interactive Google Workspace authentication.

    Prompts for OAuth client credentials if not configured, then runs the
    workspace-mcp OAuth flow which opens a browser for authorization.
    Credentials are stored under summon's XDG data directory.
    """
    bin_path = find_workspace_mcp_bin()
    if not bin_path.exists():
        click.echo(
            "Google Workspace support requires the 'google' extra: "
            "uv pip install summon-claude[google]",
            err=True,
        )
        sys.exit(1)

    # Ensure client credentials and build env for subprocess.
    # Set LOG_LEVEL=WARNING to suppress workspace-mcp's INFO output.
    client_env = _ensure_google_client_secrets()
    env = {**os.environ, **client_env, **google_mcp_env(), "LOG_LEVEL": "WARNING"}

    click.echo("Starting Google OAuth flow — a browser window will open for authorization.")

    # workspace-mcp's ``start_google_auth`` tool initiates the OAuth flow.
    try:
        subprocess.run(  # noqa: S603
            [str(bin_path), "--single-user", "--cli", "start_google_auth"],
            check=True,
            stdout=subprocess.DEVNULL,
            env=env,
        )
        click.echo("Google Workspace authenticated successfully.")
        click.echo(f"Credentials stored in {get_google_credentials_dir()}")
    except subprocess.CalledProcessError:
        click.echo("Google auth flow did not complete.", err=True)
        click.echo("Run `summon config google-status` to check auth state.")
        sys.exit(1)


def _check_google_status(*, prefix: str = "", quiet: bool = False) -> bool | None:
    """Check Google Workspace authentication status.

    Returns True if valid, False if credentials exist but are broken,
    or None if Google isn't configured (not an error, just absent).
    """
    try:
        from auth.credential_store import LocalDirectoryCredentialStore  # noqa: PLC0415
        from auth.google_auth import has_required_scopes  # noqa: PLC0415
        from auth.scopes import get_scopes_for_tools  # noqa: PLC0415
    except ImportError:
        if not quiet:
            click.echo(f"{prefix}Google: not installed (install summon-claude[google])")
        return None

    creds_dir = get_google_credentials_dir()
    if not creds_dir.exists():
        if not quiet:
            click.echo(f"{prefix}Google: not configured (run `summon config google-auth`)")
        return None

    store = LocalDirectoryCredentialStore(str(creds_dir))
    users = store.list_users()
    if not users:
        if not quiet:
            click.echo(f"{prefix}Google: no credentials found")
        return None

    all_ok = True
    for user in users:
        cred = store.get_credential(user)
        if not cred:
            click.echo(f"{prefix}Google: invalid credential file ({user})")
            all_ok = False
            continue

        if cred.valid:
            status = "valid"
        elif cred.expired and cred.refresh_token:
            status = "expired (will refresh on next use)"
        else:
            click.echo(f"{prefix}Google: invalid — re-run `summon config google-auth` ({user})")
            all_ok = False
            continue

        # Scope validation: check granted scopes against configured services
        granted = set(cred.scopes or [])
        required = set(get_scopes_for_tools(["gmail", "calendar", "drive"]))
        if granted and not has_required_scopes(granted, required):
            missing = required - granted
            click.echo(f"{prefix}Google: {status} but missing scopes ({user})")
            if not quiet:
                click.echo(f"{prefix}  Missing: {', '.join(sorted(missing)[:3])}...")
                click.echo(f"{prefix}  Re-run `summon config google-auth` to grant scopes")
            all_ok = False
        elif not quiet:
            click.echo(f"{prefix}Google: {status} ({user})")

    return all_ok


def google_status() -> None:
    """Check Google Workspace authentication status (CLI entry point)."""
    _check_google_status()


_REQUIRED_KEYS = (
    "SUMMON_SLACK_BOT_TOKEN",
    "SUMMON_SLACK_APP_TOKEN",
    "SUMMON_SLACK_SIGNING_SECRET",
)


def config_check(quiet: bool = False, config_path: str | None = None) -> bool:
    """Check config validity. Returns True if all checks pass."""
    config_file = get_config_file(config_path)
    all_pass = True

    # Parse the config file into a dict
    values: dict[str, str] = {}
    if config_file.exists():
        for raw_line in config_file.read_text().splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in stripped:
                k, _, v = stripped.partition("=")
                values[k.strip()] = v.strip()

    # Required keys
    for key in _REQUIRED_KEYS:
        present = bool(values.get(key))
        if present:
            if not quiet:
                click.echo(f"  [PASS] {key} is set")
        else:
            click.echo(f"  [FAIL] {key} is missing")
            all_pass = False

    # Token format
    bot_token = values.get("SUMMON_SLACK_BOT_TOKEN", "")
    app_token = values.get("SUMMON_SLACK_APP_TOKEN", "")
    signing_secret = values.get("SUMMON_SLACK_SIGNING_SECRET", "")

    if bot_token:
        if bot_token.startswith("xoxb-"):
            if not quiet:
                click.echo("  [PASS] Bot token format is valid (xoxb-)")
        else:
            click.echo("  [FAIL] Bot token must start with 'xoxb-'")
            all_pass = False

    if app_token:
        if app_token.startswith("xapp-"):
            if not quiet:
                click.echo("  [PASS] App token format is valid (xapp-)")
        else:
            click.echo("  [FAIL] App token must start with 'xapp-'")
            all_pass = False

    if signing_secret:
        if re.match(r"^[0-9a-f]+$", signing_secret):
            if not quiet:
                click.echo("  [PASS] Signing secret format looks valid (hex)")
        else:
            click.echo("  [FAIL] Signing secret should be a hex string")
            all_pass = False

    # DB writable
    db_path = get_data_dir() / "registry.db"
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.touch()
        if os.access(db_path, os.W_OK):
            if not quiet:
                click.echo(f"  [PASS] DB path is writable: {db_path}")
        else:
            click.echo(f"  [FAIL] DB path is not writable: {db_path}")
            all_pass = False
    except OSError as e:
        click.echo(f"  [FAIL] DB path error: {e}")
        all_pass = False

    # Schema version, integrity, and row counts
    try:

        async def _check_db() -> tuple[int, str, int, int]:
            version = 0
            integrity = "unknown"
            sessions = 0
            audit = 0
            reg = SessionRegistry(db_path=db_path)
            async with reg:
                db = reg.db
                version = await get_schema_version(db)
                async with db.execute("PRAGMA integrity_check") as cursor:
                    row = await cursor.fetchone()
                    integrity = row[0] if row else "unknown"
                async with db.execute("SELECT COUNT(*) FROM sessions") as cur:
                    row = await cur.fetchone()
                    sessions = row[0] if row else 0
                async with db.execute("SELECT COUNT(*) FROM audit_log") as cur:
                    row = await cur.fetchone()
                    audit = row[0] if row else 0
            return version, integrity, sessions, audit

        version, integrity, sessions_count, audit_count = asyncio.run(_check_db())

        # Schema version
        if version == CURRENT_SCHEMA_VERSION:
            if not quiet:
                click.echo(f"  [PASS] Schema version {version} (current)")
        elif version > CURRENT_SCHEMA_VERSION:
            click.echo(
                f"  [WARN] Schema version {version} is ahead of this release"
                f" (expects {CURRENT_SCHEMA_VERSION}) — upgrade summon-claude"
            )
        else:
            # Should not happen — _connect() auto-migrates
            click.echo(f"  [FAIL] Schema version {version} (expected {CURRENT_SCHEMA_VERSION})")
            all_pass = False

        # Integrity
        if integrity == "ok":
            if not quiet:
                click.echo("  [PASS] Database integrity OK")
        else:
            click.echo(f"  [FAIL] Database integrity error: {integrity}")
            all_pass = False

        # Row counts (informational only)
        if not quiet:
            click.echo(f"  [INFO] Sessions: {sessions_count}, Audit log: {audit_count}")

    except Exception:
        logger.debug("Database validation error", exc_info=True)
        click.echo("  [FAIL] Database validation error")
        all_pass = False

    # Slack API reachable (optional, best-effort)
    if bot_token.startswith("xoxb-"):
        try:
            from slack_sdk import WebClient  # noqa: PLC0415

            client = WebClient(token=bot_token)
            resp = client.auth_test()
            if resp["ok"]:
                if not quiet:
                    click.echo(f"  [PASS] Slack API reachable (team: {resp.get('team')})")
            else:
                click.echo(f"  [FAIL] Slack API auth.test failed: {resp.get('error')}")
                all_pass = False
        except Exception as e:
            click.echo(f"  [WARN] Slack API check skipped: {e}")

    # Google Workspace (optional, only if credentials exist)
    google_result = _check_google_status(prefix="  ", quiet=quiet)
    if google_result is not None:
        # Credentials exist — report pass/fail
        if google_result:
            if not quiet:
                click.echo("  [PASS] Google Workspace credentials valid")
        else:
            click.echo("  [FAIL] Google Workspace credentials have issues")
            all_pass = False
    elif not quiet:
        click.echo("  [INFO] Google Workspace: not configured (optional)")

    return all_pass
