"""Bug hunter memory volume management — host-side helpers."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MEMORY_FILES = ("SETUP.md", "PATTERNS.md", "FINDINGS.md", "SUPPRESSIONS.md", "SCAN_LOG.md")


def initialize_bug_hunter_memory(path: Path) -> None:
    """Create the memory directory and template files if they don't exist."""
    path.mkdir(parents=True, exist_ok=True)
    for filename in _MEMORY_FILES:
        filepath = path / filename
        if not filepath.exists():
            filepath.write_text("")
    logger.info("Bug hunter memory initialized at %s", path)


def get_last_scan_hash(path: Path) -> str | None:
    """Read the most recent git hash from SCAN_LOG.md.

    Returns None if the file is empty or doesn't exist.
    Scan log format: ISO_TIMESTAMP GIT_HASH FILES_SCANNED FINDINGS_COUNT DURATION
    """
    scan_log = path / "SCAN_LOG.md"
    if not scan_log.exists():
        return None
    content = scan_log.read_text().strip()
    if not content:
        return None
    # Last non-empty line, second field is the git hash
    last_line = content.splitlines()[-1].strip()
    parts = last_line.split()
    if len(parts) >= 2:
        return parts[1]
    return None
