"""Background update checker for summon-claude."""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import NamedTuple

from packaging.version import Version

from summon_claude.config import get_update_check_path

logger = logging.getLogger(__name__)

_PYPI_URL = "https://pypi.org/pypi/summon-claude/json"
_CACHE_TTL = timedelta(hours=24)
_REQUEST_TIMEOUT = 3


class UpdateInfo(NamedTuple):
    current: str
    latest: str


def check_for_update() -> UpdateInfo | None:
    """Check PyPI for a newer version of summon-claude.

    Returns an UpdateInfo if a newer version is available, else None.
    All exceptions are caught and silently ignored (fail-open).
    """
    if os.environ.get("SUMMON_NO_UPDATE_CHECK") == "1":
        return None

    try:
        cache_path = get_update_check_path()
        latest_version = _read_cache(cache_path)

        if latest_version is None:
            latest_version = _fetch_latest_from_pypi()
            if latest_version is None:
                return None
            _write_cache(cache_path, latest_version)

        current = version("summon-claude")
        if Version(latest_version) > Version(current):
            return UpdateInfo(current=current, latest=latest_version)

    except Exception:
        logger.debug("Update check failed", exc_info=True)

    return None


def _read_cache(cache_path: Path) -> str | None:
    """Return cached latest version if cache is fresh, else None."""
    try:
        if not cache_path.exists():
            return None
        data = json.loads(cache_path.read_text())
        last_checked = datetime.fromisoformat(data["last_checked"])
        if datetime.now(UTC) - last_checked < _CACHE_TTL:
            return data["latest_version"]
    except Exception:
        logger.debug("Failed to read update cache", exc_info=True)
    return None


def _fetch_latest_from_pypi() -> str | None:
    """Query PyPI for the latest version of summon-claude."""
    try:
        with urllib.request.urlopen(_PYPI_URL, timeout=_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read(65536))  # 64KB cap; PyPI info JSON is ~5KB
            return data["info"]["version"]
    except Exception:
        logger.debug("Failed to fetch latest version from PyPI", exc_info=True)
    return None


def _write_cache(cache_path: Path, latest_version: str) -> None:
    """Write the latest version and timestamp to the cache file."""
    try:
        if cache_path.is_symlink():
            logger.debug("Refusing to write cache through symlink: %s", cache_path)
            return
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "latest_version": latest_version,
                    "last_checked": datetime.now(UTC).isoformat(),
                }
            )
        )
    except Exception:
        logger.debug("Failed to write update cache", exc_info=True)


def format_update_message(info: UpdateInfo) -> str:
    """Return a styled box announcing the available update."""
    upgrade_line = f"  Update available: {info.current} → {info.latest}"
    install_line = "  Run: uv tool upgrade summon-claude"
    disable_line = "  Disable: SUMMON_NO_UPDATE_CHECK=1"

    width = max(len(upgrade_line), len(install_line), len(disable_line)) + 2
    bar = "─" * width
    return "\n".join(
        [
            f"┌{bar}┐",
            f"│{upgrade_line.ljust(width)}│",
            f"│{install_line.ljust(width)}│",
            f"│{disable_line.ljust(width)}│",
            f"└{bar}┘",
        ]
    )
