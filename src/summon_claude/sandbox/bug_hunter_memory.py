"""Bug hunter memory volume management — host-side helpers."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MEMORY_FILES = ("PATTERNS.md", "FINDINGS.md", "SUPPRESSIONS.md", "SCAN_LOG.md")


def initialize_bug_hunter_memory(path: Path) -> None:
    """Create the memory directory and template files if they don't exist."""
    path.mkdir(parents=True, exist_ok=True)
    for filename in _MEMORY_FILES:
        filepath = path / filename
        if not filepath.exists():
            filepath.write_text("")
    logger.info("Bug hunter memory initialized at %s", path)
