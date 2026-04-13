"""Guard tests: configuration.md group list ↔ CONFIG_OPTIONS groups."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.docs

_CONFIG_DOC = "guide/configuration.md"


def test_configuration_sections_match_config_groups(docs_dir: Path) -> None:
    """Inline group list in configuration.md must match CONFIG_OPTIONS groups."""
    from summon_claude.config import CONFIG_OPTIONS

    doc_path = docs_dir / _CONFIG_DOC
    assert doc_path.exists(), f"Configuration doc not found: {doc_path}"
    content = doc_path.read_text(encoding="utf-8")

    # Parse the inline parenthetical list
    match = re.search(r"organized by section \(([^)]+)\)", content)
    assert match is not None, (
        "Could not find group list in docs/guide/configuration.md — "
        "expected pattern 'organized by section (...)'"
    )

    doc_groups = {g.strip() for g in match.group(1).split(",")}
    source_groups = {opt.group for opt in CONFIG_OPTIONS}

    # Bidirectional match
    in_doc_not_source = doc_groups - source_groups
    in_source_not_doc = source_groups - doc_groups

    errors = []
    if in_doc_not_source:
        errors.append(f"Groups in doc but not in CONFIG_OPTIONS: {sorted(in_doc_not_source)}")
    if in_source_not_doc:
        errors.append(f"Groups in CONFIG_OPTIONS but not in doc: {sorted(in_source_not_doc)}")

    assert not errors, "\n".join(errors)
