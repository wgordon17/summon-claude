"""CLI config subcommands: show, path, edit, set."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import click

from summon_claude.cli.formatting import format_tag
from summon_claude.config import (
    _BOOL_FALSE,
    _BOOL_TRUE,
    CONFIG_OPTIONS,
    find_workspace_mcp_bin,
    get_config_file,
    get_data_dir,
    get_google_credentials_dir,
)
from summon_claude.sessions.migrations import CURRENT_SCHEMA_VERSION, get_schema_version
from summon_claude.sessions.registry import SessionRegistry

logger = logging.getLogger(__name__)

# Required Slack bot scopes — must match slack-app-manifest.yaml.
# Guard test test_required_scopes_match_manifest pins this set.
_REQUIRED_SLACK_SCOPES: frozenset[str] = frozenset(
    {
        "canvases:read",
        "canvases:write",
        "channels:history",
        "channels:join",
        "channels:manage",
        "channels:read",
        "chat:write",
        "commands",
        "files:read",
        "files:write",
        "groups:history",
        "groups:read",
        "groups:write",
        "reactions:read",
        "reactions:write",
        "users:read",
    }
)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env-style config file into a dict. Returns {} if the file does not exist."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            k, _, v = stripped.partition("=")
            values[k.strip()] = v.strip()
    return values


def write_env_file(path: Path, values: dict[str, str], *, atomic: bool = False) -> None:
    """Write a dict as KEY=VALUE lines to a .env file with 0o600 permissions.

    Sanitizes newlines from all values to prevent .env injection.
    When atomic=True, writes to a temp file first then uses os.replace() for
    atomic rename (prevents partial-write corruption on power loss).
    """
    content = "\n".join(
        f"{k}={v.replace(chr(10), '').replace(chr(13), '')}" for k, v in values.items()
    )
    if content:
        content += "\n"

    path.parent.mkdir(parents=True, exist_ok=True)

    if atomic:
        old_umask = os.umask(0o177)
        try:
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=path.name + "."
            )
        finally:
            os.umask(old_umask)
        write_path = Path(tmp_name)
    else:
        tmp_fd = None
        write_path = path
    try:
        if atomic and tmp_fd is not None:
            os.fchmod(tmp_fd, 0o600)
            with os.fdopen(tmp_fd, "w") as f:
                f.write(content)
            tmp_fd = None  # fdopen owns the fd now
        else:
            old_umask = os.umask(0o177)
            try:
                fd = os.open(str(write_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            finally:
                os.umask(old_umask)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w") as f:
                    fd = -1  # fdopen owns the fd now
                    f.write(content)
            except BaseException:
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)
                raise
        if atomic:
            write_path.replace(path)
    except BaseException:
        if atomic:
            if tmp_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(tmp_fd)
            with contextlib.suppress(OSError):
                write_path.unlink()
        raise


def get_draft_path(config_file: Path) -> Path:
    """Return the draft path for a given config file (used for crash/Ctrl-C recovery)."""
    return config_file.parent / (config_file.stem + ".draft" + config_file.suffix)


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


def config_show(override: str | None = None, *, color: bool = True) -> None:
    """Show all config options with grouped display and source indicators."""
    from summon_claude.config import get_config_default  # noqa: PLC0415

    config_file = get_config_file(override)
    values = parse_env_file(config_file)
    if not config_file.exists():
        click.echo(f"No config file found at {config_file}")
        click.echo("Run `summon init` to create one.\n")

    current_group = ""
    for opt in CONFIG_OPTIONS:
        # Evaluate visibility predicate
        if opt.visible is not None and not opt.visible(values):
            # Show dim hint for hidden groups (only once per group).
            # Uses current_group so groups with at least one visible item
            # don't also print a "disabled" hint for their hidden items.
            if opt.group != current_group:
                current_group = opt.group
                hint = (
                    click.style(f"\n  {opt.group}: disabled", dim=True)
                    if color
                    else f"\n  {opt.group}: disabled"
                )
                click.echo(hint)
            continue

        # Print group header on group change
        if opt.group != current_group:
            current_group = opt.group
            if color:
                click.echo(click.style(f"\n  {opt.group}", bold=True))
            else:
                click.echo(f"\n  {opt.group}")

        # Determine value and source
        file_value = values.get(opt.env_key)
        default = get_config_default(opt)

        if opt.input_type == "secret":
            if file_value:
                display_value = "configured"
                source = "set"
            elif opt.required:
                display_value = ""
                source = "not set"
            else:
                display_value = ""
                source = "optional"
        elif file_value is not None:
            display_value = file_value
            if isinstance(default, bool):
                default_str = str(default).lower()
            else:
                default_str = str(default) if default is not None else ""
            source = "default" if file_value == default_str else "set"
        elif opt.required:
            display_value = ""
            source = "not set"
        else:
            if isinstance(default, bool):
                display_value = str(default).lower()
            else:
                display_value = str(default) if default is not None else ""
            source = "default"

        # Truncate long values to keep columns aligned
        val_col = 30
        if display_value and len(display_value) > val_col:
            display_value = display_value[: val_col - 1] + "\u2026"

        # Format output with color
        if color:
            if source == "set":
                source_label = click.style("(set)", fg="green")
            elif source == "not set":
                source_label = click.style("(not set)", fg="yellow")
            elif source == "optional":
                source_label = click.style("(optional)", dim=True)
            else:
                source_label = click.style("(default)", dim=True)
            # Pad before styling to avoid ANSI escape codes breaking alignment
            if display_value:
                val_display = f"{display_value:<{val_col}}"
            else:
                val_display = click.style(f"{'—':<{val_col}}", dim=True)
        else:
            source_label = f"({source})"
            val_display = f"{(display_value if display_value else '—'):<{val_col}}"

        click.echo(f"    {opt.env_key:<40} {val_display} {source_label}")


def config_edit(override: str | None = None) -> None:
    config_file = _require_config_file(override)
    if config_file is None:
        return

    editor = os.environ.get("EDITOR", "vi")
    try:
        subprocess.run([*shlex.split(editor), str(config_file)], check=False)  # noqa: S603
    except FileNotFoundError:
        click.echo(f"Editor '{editor}' not found. Set $EDITOR to your preferred editor.", err=True)
        sys.exit(1)


def config_set(key: str, value: str, override: str | None = None) -> None:
    key = key.strip().upper()
    valid_keys = {opt.env_key for opt in CONFIG_OPTIONS}
    if key not in valid_keys:
        click.echo(f"Unknown config key: {key!r}", err=True)
        click.echo(f"Valid keys: {', '.join(sorted(valid_keys))}", err=True)
        sys.exit(1)

    # Bool normalization for flag-type options
    option = next((opt for opt in CONFIG_OPTIONS if opt.env_key == key), None)
    if option and option.input_type == "flag":
        lower = value.lower()
        if lower in _BOOL_TRUE:
            value = "true"
        elif lower in _BOOL_FALSE:
            value = "false"
        else:
            click.echo(f"Invalid boolean value: {value!r}. Use true/false/yes/no/1/0.", err=True)
            sys.exit(1)

    # Validate choices for choice-type options (choices_fn takes precedence over static choices)
    if option and value:
        choices: list[str] = []
        if option.choices_fn:
            try:
                choices = option.choices_fn()
            except Exception as e:
                click.echo(f"Error resolving choices for {key}: {e}", err=True)
                sys.exit(1)
        elif option.choices:
            choices = list(option.choices)
        # Sentinel values ("other", "default (...)") are UI-only choices from get_model_choices().
        # Block them for options with validate_fn (model fields) — others don't use sentinels.
        _is_sentinel = value == "other" or value.startswith("default (")
        if _is_sentinel and choices and option.validate_fn:
            click.echo(
                f"Invalid value for {key}: {value!r} is a UI placeholder, not a valid value.",
                err=True,
            )
            sys.exit(1)
        # Strip sentinels so they don't appear in error messages.
        choices = [c for c in choices if c != "other" and not c.startswith("default (")]
        if choices and value not in choices and not option.validate_fn:
            click.echo(
                f"Invalid value for {key}: {value!r}. Must be one of: {', '.join(choices)}",
                err=True,
            )
            sys.exit(1)
        # else: soft-validation option (model fields) — accept value; validate_fn fires below

    # Run option validator if present
    if option and option.validate_fn and value:
        err = option.validate_fn(value)
        if err:
            click.echo(f"Invalid value for {key}: {err}", err=True)
            sys.exit(1)

    # Strip newlines to prevent injection into the .env format
    value = value.replace("\n", "").replace("\r", "")

    config_file = get_config_file(override)

    # Read existing values, update target key, write atomically at 0o600
    existing = parse_env_file(config_file)
    existing[key] = value
    write_env_file(config_file, existing, atomic=True)
    click.echo(f"Set {key} in {config_file}")


# Google OAuth setup, auth, and status moved to cli/google_auth.py


async def github_auth_cmd() -> None:
    """Interactive GitHub OAuth device flow authentication.

    Runs the device flow, prompting the user to visit GitHub and enter a code.
    Stores the resulting token for use by all sessions.
    """
    import aiohttp  # noqa: PLC0415

    from summon_claude.github_auth import GitHubAuthError, run_device_flow  # noqa: PLC0415

    def _print_device_code(user_code: str, verification_uri: str) -> None:
        # Strip ANSI escape sequences first, then non-printable chars
        safe_uri = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", verification_uri)
        safe_uri = re.sub(r"[^\x20-\x7e]", "", safe_uri)
        safe_code = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", user_code)
        safe_code = re.sub(r"[^\x20-\x7e]", "", safe_code)
        click.echo(f"Visit {safe_uri} and enter code: {safe_code}")
        click.echo("Verify the authorization page shows 'summon-claude' as the app name.")
        click.launch(safe_uri)
        click.echo("Waiting for GitHub authorization...")

    try:
        result = await run_device_flow(on_code=_print_device_code)
        login = re.sub(r"[^a-zA-Z0-9-]", "", result.login) or "unknown"
        cred_dir = result.token_path.parent
        click.echo(f"GitHub authenticated as {login}.")
        click.echo(f"Credentials stored in {cred_dir}")
        click.echo()
        click.echo("GitHub MCP tools will be available on next session start.")
    except aiohttp.ClientError as e:
        click.echo(f"Network error during GitHub auth: {e}", err=True)
        sys.exit(1)
    except GitHubAuthError as e:
        click.echo(f"GitHub authentication failed: {e}", err=True)
        sys.exit(1)


def github_logout() -> None:
    """Remove the stored GitHub OAuth token."""
    from summon_claude.github_auth import remove_token  # noqa: PLC0415

    removed = remove_token()
    if removed:
        click.echo("GitHub credentials removed.")
    else:
        click.echo("No GitHub credentials stored.")


def _check_github_status(*, prefix: str = "", quiet: bool = False) -> bool | None:
    """Check GitHub OAuth token status.

    Returns True if valid, False if broken, None if not configured.
    """
    import aiohttp  # noqa: PLC0415

    from summon_claude.github_auth import (  # noqa: PLC0415
        GitHubAuthError,
        load_token,
        validate_token,
    )

    token = load_token()
    if not token:
        if not quiet:
            click.echo(
                f"{prefix}{format_tag('INFO')} GitHub: not configured"
                " (run `summon auth github login`)"
            )
        return None

    try:
        result = asyncio.run(validate_token(token))
    except (OSError, aiohttp.ClientError, GitHubAuthError):
        if not quiet:
            click.echo(
                f"{prefix}{format_tag('WARN')} GitHub: token found"
                " (validation skipped — network error)"
            )
        return True

    if result is None:
        if not quiet:
            click.echo(
                f"{prefix}{format_tag('FAIL')} GitHub: token invalid"
                " — run `summon auth github login`"
            )
        return False

    if not quiet:
        login = re.sub(r"[^a-zA-Z0-9-]", "", result["login"]) or "unknown"
        scopes = re.sub(r"[^\x20-\x7e]", "", result["scopes"])
        click.echo(
            f"{prefix}{format_tag('PASS')} GitHub: authenticated as {login} (scopes: {scopes})"
        )
    return True


def _check_github_status_data() -> dict:
    """Return GitHub auth status as a dict for --json output.

    Never includes token values.
    """
    import aiohttp  # noqa: PLC0415

    from summon_claude.github_auth import (  # noqa: PLC0415
        GitHubAuthError,
        load_token,
        validate_token,
    )

    token = load_token()
    if not token:
        return {"provider": "github", "status": "not_configured"}

    try:
        result = asyncio.run(validate_token(token))
    except (OSError, aiohttp.ClientError, GitHubAuthError):
        return {
            "provider": "github",
            "status": "authenticated",
            "note": "validation skipped — network error",
        }

    if result is None:
        return {"provider": "github", "status": "error", "error": "token invalid"}

    login = re.sub(r"[^a-zA-Z0-9-]", "", result["login"]) or "unknown"
    scopes = re.sub(r"[^\x20-\x7e]", "", result["scopes"])
    return {"provider": "github", "status": "authenticated", "login": login, "scopes": scopes}


def get_upgrade_command() -> str:
    """Detect install method from sys.executable and return the upgrade command."""
    exe = sys.executable or ""
    if "/Cellar/" in exe or "/homebrew/" in exe:
        return "brew upgrade summon-claude"
    if "/.local/pipx/venvs/" in exe:
        return "pipx upgrade summon-claude"
    return "uv tool upgrade summon-claude"


async def _check_db(db_path: Path) -> tuple[int, str, int, int]:
    """Query DB for schema version, integrity, and row counts."""
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


async def _check_features(db_path: Path) -> tuple[bool, bool, int]:
    """Query DB for workflow, hooks, and project count."""
    has_workflow = False
    has_hooks = False
    project_count = 0
    reg = SessionRegistry(db_path=db_path)
    async with reg:
        has_workflow = bool(await reg.get_workflow_defaults())
        raw_hooks = await reg.get_raw_hooks_json(project_id=None)
        has_hooks = raw_hooks is not None
        db = reg.db
        async with db.execute("SELECT COUNT(*) FROM projects") as cur:
            row = await cur.fetchone()
            project_count = row[0] if row else 0
    return has_workflow, has_hooks, project_count


def config_check(quiet: bool = False, config_path: str | None = None) -> bool:
    """Check config validity. Returns True if all checks pass."""
    from summon_claude.cli.google_auth import _check_google_status  # noqa: PLC0415
    from summon_claude.cli.preflight import check_claude_cli  # noqa: PLC0415

    config_file = get_config_file(config_path)
    all_pass = True

    # Claude CLI preflight
    cli_status = check_claude_cli()
    if cli_status.found:
        if not quiet:
            version_str = f" ({cli_status.version})" if cli_status.version else ""
            click.echo(f"  {format_tag('PASS')} Claude CLI found{version_str}")
    else:
        click.echo(
            f"  {format_tag('FAIL')} Claude CLI not found — install from https://claude.ai/code"
        )
        all_pass = False

    # Parse the config file into a dict
    values = parse_env_file(config_file)

    # Required keys
    required_keys = [opt.env_key for opt in CONFIG_OPTIONS if opt.required]
    for key in required_keys:
        present = bool(values.get(key))
        if present:
            if not quiet:
                click.echo(f"  {format_tag('PASS')} {key} is set")
        else:
            click.echo(f"  {format_tag('FAIL')} {key} is missing")
            all_pass = False

    # Token format
    bot_token = values.get("SUMMON_SLACK_BOT_TOKEN", "")
    app_token = values.get("SUMMON_SLACK_APP_TOKEN", "")
    signing_secret = values.get("SUMMON_SLACK_SIGNING_SECRET", "")

    if bot_token:
        if bot_token.startswith("xoxb-"):
            if not quiet:
                click.echo(f"  {format_tag('PASS')} Bot token format is valid (xoxb-)")
        else:
            click.echo(f"  {format_tag('FAIL')} Bot token must start with 'xoxb-'")
            all_pass = False

    if app_token:
        if app_token.startswith("xapp-"):
            if not quiet:
                click.echo(f"  {format_tag('PASS')} App token format is valid (xapp-)")
        else:
            click.echo(f"  {format_tag('FAIL')} App token must start with 'xapp-'")
            all_pass = False

    if signing_secret:
        if re.match(r"^[0-9a-f]+$", signing_secret):
            if not quiet:
                click.echo(f"  {format_tag('PASS')} Signing secret format looks valid (hex)")
        else:
            click.echo(f"  {format_tag('FAIL')} Signing secret should be a hex string")
            all_pass = False

    # Pydantic validation — catches @field_validator rules (effort, quiet_hours,
    # google_services, channel_prefix, etc.) that individual key checks above miss.
    # Skip when required keys are missing — those failures are already reported above
    # and model_validate would just produce a duplicate cryptic error.
    required_missing = [key for key in required_keys if not bool(values.get(key))]
    if values and not required_missing:
        from summon_claude.config import SummonConfig  # noqa: PLC0415

        try:
            SummonConfig.model_validate(
                {
                    opt.field_name: values[opt.env_key]
                    for opt in CONFIG_OPTIONS
                    if opt.env_key in values
                }
            )
            if not quiet:
                click.echo(f"  {format_tag('PASS')} Config values pass validation")
        except Exception as e:
            click.echo(f"  {format_tag('FAIL')} Config validation: {e}")
            all_pass = False

    # DB writable
    db_path = get_data_dir() / "registry.db"
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.touch()
        if os.access(db_path, os.W_OK):
            if not quiet:
                click.echo(f"  {format_tag('PASS')} DB path is writable: {db_path}")
        else:
            click.echo(f"  {format_tag('FAIL')} DB path is not writable: {db_path}")
            all_pass = False
    except OSError as e:
        click.echo(f"  {format_tag('FAIL')} DB path error: {e}")
        all_pass = False

    # Schema version, integrity, and row counts
    try:
        version, integrity, sessions_count, audit_count = asyncio.run(_check_db(db_path))

        # Schema version
        if version == CURRENT_SCHEMA_VERSION:
            if not quiet:
                click.echo(f"  {format_tag('PASS')} Schema version {version} (current)")
        elif version > CURRENT_SCHEMA_VERSION:
            upgrade_cmd = get_upgrade_command()
            click.echo(
                f"  {format_tag('WARN')} Schema version {version} is ahead of this release"
                f" (expects {CURRENT_SCHEMA_VERSION}) — run `{upgrade_cmd}`"
            )
        else:
            # Should not happen — _connect() auto-migrates
            tag = format_tag("FAIL")
            click.echo(f"  {tag} Schema version {version} (expected {CURRENT_SCHEMA_VERSION})")
            all_pass = False

        # Integrity
        if integrity == "ok":
            if not quiet:
                click.echo(f"  {format_tag('PASS')} Database integrity OK")
        else:
            click.echo(f"  {format_tag('FAIL')} Database integrity error: {integrity}")
            all_pass = False

        # Row counts (informational only)
        if not quiet:
            tag = format_tag("INFO")
            click.echo(f"  {tag} Sessions: {sessions_count}, Audit log: {audit_count}")

    except Exception:
        logger.debug("Database validation error", exc_info=True)
        click.echo(f"  {format_tag('FAIL')} Database validation error")
        all_pass = False

    # Slack API reachable + scope verification (optional, best-effort)
    if bot_token.startswith("xoxb-"):
        try:
            from slack_sdk import WebClient  # noqa: PLC0415

            client = WebClient(token=bot_token)
            resp = client.auth_test()
            if resp["ok"]:
                if not quiet:
                    tag = format_tag("PASS")
                    click.echo(f"  {tag} Slack API reachable (team: {resp.get('team')})")
                # Check bot scopes via x-oauth-scopes response header.
                # Header name casing varies by HTTP library, so do a
                # case-insensitive lookup.
                headers_lower = {k.lower(): v for k, v in resp.headers.items()}
                granted_str = headers_lower.get("x-oauth-scopes", "")
                if granted_str:
                    granted = {s.strip() for s in granted_str.split(",") if s.strip()}
                    missing = _REQUIRED_SLACK_SCOPES - granted
                    if missing:
                        tag = format_tag("FAIL")
                        click.echo(
                            f"  {tag} Slack bot missing scopes: {', '.join(sorted(missing))}"
                        )
                        click.echo(
                            "  Update at: api.slack.com/apps → your app"
                            " → OAuth & Permissions → Scopes"
                        )
                        all_pass = False
                    elif not quiet:
                        tag = format_tag("PASS")
                        n = len(_REQUIRED_SLACK_SCOPES)
                        click.echo(f"  {tag} Slack bot scopes: all {n} required scopes granted")
            else:
                tag = format_tag("FAIL")
                click.echo(f"  {tag} Slack API auth.test failed: {resp.get('error')}")
                all_pass = False
        except Exception as e:
            click.echo(f"  {format_tag('WARN')} Slack API check skipped: {e}")

    # GitHub OAuth (optional, with connectivity check)
    github_result = _check_github_status(prefix="  ", quiet=quiet)
    if github_result is False:
        all_pass = False

    # Google Workspace (optional, only if credentials exist)
    google_result = _check_google_status(prefix="  ", quiet=quiet)
    if google_result is False:
        all_pass = False

    # Jira (optional, only if credentials exist)
    from summon_claude.jira_auth import (  # noqa: PLC0415
        check_jira_status,
        get_jira_site_name,
        jira_credentials_exist,
    )

    if jira_credentials_exist():
        jira_err = check_jira_status()
        if jira_err is None:
            if not quiet:
                site = get_jira_site_name()
                if site:
                    site = re.sub(r"[^\x20-\x7e]", "", site)[:80]
                site_suffix = f" (site: {site})" if site else ""
                click.echo(f"  {format_tag('PASS')} Jira: authenticated{site_suffix}")
        else:
            click.echo(f"  {format_tag('FAIL')} Jira: {jira_err}")
            all_pass = False
    elif not quiet:
        click.echo(f"  {format_tag('INFO')} Jira: not configured (optional)")

    # Optional extras availability (informational)
    if not quiet:
        from summon_claude.config import (  # noqa: PLC0415
            discover_google_accounts,
            is_extra_installed,
        )

        accounts = discover_google_accounts()
        bin_exists = find_workspace_mcp_bin().exists()
        label = (
            f"workspace-mcp (Google: {len(accounts)} account{'s' if len(accounts) != 1 else ''})"
            if accounts
            else "workspace-mcp (Google)"
        )
        gmcp_status = "installed" if bin_exists and len(accounts) > 0 else "not installed"
        click.echo(f"  {format_tag('INFO')} {label}: {gmcp_status}")

        # Playwright — check auth state, not just pip importability
        if is_extra_installed("playwright"):
            from summon_claude.cli.slack_auth import _check_existing_slack_auth  # noqa: PLC0415

            existing = _check_existing_slack_auth()
            if existing:
                click.echo(
                    f"  {format_tag('INFO')} playwright (Slack browser):"
                    f" authenticated ({existing.get('age', 'unknown')})"
                )
            else:
                click.echo(
                    f"  {format_tag('INFO')} playwright (Slack browser):"
                    " installed, not authenticated"
                )
        else:
            click.echo(f"  {format_tag('INFO')} playwright (Slack browser): not installed")

    # Event health check — only when daemon is running
    from summon_claude.daemon import is_daemon_running  # noqa: PLC0415

    if is_daemon_running():
        if not quiet:
            click.echo("  Event health: checking...", nl=False)
        try:
            from summon_claude.cli import daemon_client  # noqa: PLC0415

            result = asyncio.run(daemon_client.health_check())
            healthy = result.get("healthy")
            details = result.get("details", "")
            remediation_url = result.get("remediation_url")

            if healthy is True:
                if not quiet:
                    click.echo(f"\r  {format_tag('PASS')} Event health: OK")
            elif healthy is None:
                if not quiet:
                    click.echo(f"\r  {format_tag('INFO')} Event health: {details}")
            else:
                click.echo(f"\r  {format_tag('FAIL')} Event health: {details}")
                if remediation_url and not quiet:
                    click.echo(f"         Fix at: {remediation_url}")
                all_pass = False
        except Exception as e:
            click.echo(f"\r  {format_tag('WARN')} Event health check failed: {e}")
    elif not quiet:
        click.echo(f"  {format_tag('INFO')} Event health: skipped (daemon not running)")

    # Feature inventory — surface external flows so users know they exist
    if not quiet:
        click.echo()
        click.echo(click.style("Features:", bold=True))
        _print_feature_inventory(db_path, values)

    return all_pass


def _print_feature_inventory(db_path: Path, config_values: dict[str, str]) -> None:
    """Print discoverable status of external setup flows."""
    project_count: int | None = None
    has_workflow = False
    db_ok = False

    try:
        has_workflow, has_hooks, project_count = asyncio.run(_check_features(db_path))
        db_ok = True

        # Projects — the primary workflow
        if project_count:
            click.echo(f"  {format_tag('PASS')} Projects: {project_count} registered")
        else:
            click.echo(f"  {format_tag('INFO')} Projects: none registered (summon project add)")

        if has_workflow:
            click.echo(f"  {format_tag('PASS')} Workflow instructions: configured")
        else:
            tag = format_tag("INFO")
            click.echo(f"  {tag} Workflow instructions: not set (summon project workflow set)")

        if has_hooks:
            click.echo(f"  {format_tag('PASS')} Lifecycle hooks: configured")
        else:
            click.echo(f"  {format_tag('INFO')} Lifecycle hooks: not set (summon hooks set)")

    except Exception:
        logger.debug("Feature inventory DB error", exc_info=True)

    # Hook bridge — check settings.json for summon-owned entries
    has_bridge = False
    try:
        from summon_claude.cli.hooks import read_settings  # noqa: PLC0415

        settings = read_settings()
        hooks_section = settings.get("hooks", {})
        has_bridge = any(
            "summon-pre-worktree" in str(entry) or "summon-post-worktree" in str(entry)
            for hook_type in ("PreToolUse", "PostToolUse")
            for entry in hooks_section.get(hook_type, [])
        )
        if has_bridge:
            click.echo(f"  {format_tag('PASS')} Hook bridge: installed")
            # Warn if show_thinking is on but showThinkingSummaries not configured
            show_thinking_on = config_values.get("SUMMON_SHOW_THINKING", "").lower() in _BOOL_TRUE
            if show_thinking_on and "showThinkingSummaries" not in settings:
                click.echo(
                    f"  {format_tag('WARN')} showThinkingSummaries not set in"
                    " settings.json (re-run: summon hooks install)"
                )
        else:
            click.echo(f"  {format_tag('INFO')} Hook bridge: not installed (summon hooks install)")
    except Exception:
        logger.debug("Hook bridge check error", exc_info=True)

    # Scribe → Google auth nudge
    scribe_on = config_values.get("SUMMON_SCRIBE_ENABLED", "").lower() in _BOOL_TRUE
    has_creds = False
    if scribe_on:
        try:
            google_dir = get_google_credentials_dir()
            has_creds = google_dir.exists() and any(google_dir.iterdir())
        except OSError:
            pass
        if not has_creds:
            tag = format_tag("INFO")
            msg = "Scribe enabled but Google not configured (summon auth google setup)"
            click.echo(f"  {tag} {msg}")

    # Dynamic "Next steps" — only uncompleted items
    next_steps: list[tuple[str, str]] = []

    # Auth commands
    from summon_claude.github_auth import load_token  # noqa: PLC0415

    if not load_token():
        next_steps.append(("summon auth github login", "Authenticate with GitHub"))
    if scribe_on:
        if not has_creds:
            next_steps.append(("summon auth google setup", "Set up Google OAuth"))
        from summon_claude.cli.slack_auth import _check_existing_slack_auth  # noqa: PLC0415

        if not _check_existing_slack_auth():
            next_steps.append(("summon auth slack login", "Authenticate Slack browser"))

    # Project commands — only show when DB state is known
    if db_ok:
        if project_count == 0:
            next_steps.append(("summon project add <path>", "Register a project directory"))
            next_steps.append(("summon project up", "Start PM agents for all projects"))
        if not has_workflow:
            next_steps.append(("summon project workflow set", "Set workflow instructions"))
        if not has_bridge:
            next_steps.append(("summon hooks install", "Install Claude Code hook bridge"))
    else:
        next_steps.append(
            ("summon doctor --submit", "DB unavailable — diagnose with summon doctor")
        )

    if next_steps:
        click.echo()
        click.echo(click.style("Next steps:", bold=True))
        cmd_col = 36
        for cmd, desc in next_steps:
            click.echo(f"  {cmd:<{cmd_col}}{desc}")
