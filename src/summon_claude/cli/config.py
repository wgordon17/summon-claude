"""CLI config subcommands: show, path, edit, set."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import subprocess
import sys

import click

from summon_claude.config import get_config_file, get_data_dir
from summon_claude.sessions.registry import (
    CURRENT_SCHEMA_VERSION,
    SessionRegistry,
    _get_schema_version,
)

logger = logging.getLogger(__name__)

_SETTABLE_KEYS = frozenset(
    {
        "SUMMON_SLACK_BOT_TOKEN",
        "SUMMON_SLACK_APP_TOKEN",
        "SUMMON_SLACK_SIGNING_SECRET",
        "SUMMON_DEFAULT_MODEL",
        "SUMMON_CHANNEL_PREFIX",
        "SUMMON_PERMISSION_DEBOUNCE_MS",
        "SUMMON_MAX_INLINE_CHARS",
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
                version = await _get_schema_version(db)
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

    return all_pass
