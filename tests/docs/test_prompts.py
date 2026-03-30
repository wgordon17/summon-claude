"""Guard tests: prompt documentation vs source prompt constants.

Validates that ``docs/reference/prompts.md`` matches the verbatim prompt
text defined in ``src/summon_claude/sessions/prompts/``.

When these tests fail, regenerate the doc::

    uv run python scripts/generate_prompt_docs.py
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.docs

_PROMPTS_DOC = "reference/prompts.md"

# Matches  <!-- prompt:NAME -->\n```text\n...CONTENT...```\n<!-- /prompt:NAME -->
_PROMPT_BLOCK_RE = re.compile(
    r"<!-- prompt:(\S+) -->\n```text\n(.*?)```\n<!-- /prompt:\1 -->",
    re.DOTALL,
)

# ---------------------------------------------------------------------------
# Source extraction — canonical mapping from marker name to prompt text.
#
# This must stay in sync with scripts/generate_prompt_docs.py.  The guard
# tests below will catch drift between *this* mapping and the doc file.
# ---------------------------------------------------------------------------


def _get_source_prompts() -> dict[str, str]:
    """Extract all prompt texts from source, keyed by doc marker name."""
    from summon_claude.sessions.prompts import (
        build_global_pm_scan_prompt,
        build_pm_scan_prompt,
        build_scribe_scan_prompt,
    )
    from summon_claude.sessions.prompts.global_pm import _GLOBAL_PM_SYSTEM_PROMPT_APPEND
    from summon_claude.sessions.prompts.pm import (
        _PM_SYSTEM_PROMPT_APPEND,
        _REVIEWER_SYSTEM_PROMPT_TEMPLATE,
    )
    from summon_claude.sessions.prompts.scribe import _SCRIBE_SYSTEM_PROMPT_APPEND
    from summon_claude.sessions.prompts.shared import (
        _CANVAS_PROMPT_SECTION,
        _COMPACT_PROMPT,
        _OVERFLOW_RECOVERY_PROMPT,
        _SCHEDULING_PROMPT_SECTION,
    )

    # Scribe system: resolve internal template vars, keep user-facing ones.
    scribe_system = _SCRIBE_SYSTEM_PROMPT_APPEND.replace(
        "{google_section}",
        "Your domain: Gmail, Google Calendar, Google Drive \u2014 "
        "watch every inbox, every calendar event, every shared document.\n\n",
    ).replace(
        "{external_slack_section}",
        "Your domain: External Slack channels, DMs, and @mentions \u2014 "
        "every message in your monitored workspaces passes through your watch.\n\n",
    )

    return {
        "pm-system": _PM_SYSTEM_PROMPT_APPEND,
        "pm-scan": build_pm_scan_prompt(github_enabled=True),
        "global-pm-system": _GLOBAL_PM_SYSTEM_PROMPT_APPEND,
        "global-pm-scan": build_global_pm_scan_prompt(),
        "scribe-system": scribe_system,
        "scribe-scan": build_scribe_scan_prompt(
            nonce="{nonce}",
            google_enabled=True,
            slack_enabled=True,
            user_mention="{user_mention}",
            importance_keywords="{importance_keywords}",
            quiet_hours="{quiet_hours}",
        ),
        "reviewer-system": _REVIEWER_SYSTEM_PROMPT_TEMPLATE,
        "compact": _COMPACT_PROMPT,
        "overflow-recovery": _OVERFLOW_RECOVERY_PROMPT,
        "canvas": _CANVAS_PROMPT_SECTION,
        "scheduling": _SCHEDULING_PROMPT_SECTION,
    }


def _parse_doc_prompts(docs_dir: Path) -> dict[str, str]:
    """Parse prompt blocks from the documentation file."""
    doc_path = docs_dir / _PROMPTS_DOC
    assert doc_path.exists(), f"Prompts doc not found: {doc_path}"
    content = doc_path.read_text(encoding="utf-8")
    return dict(_PROMPT_BLOCK_RE.findall(content))


# ---------------------------------------------------------------------------
# Test 1 — no undocumented prompts
# ---------------------------------------------------------------------------


def test_all_source_prompts_are_documented(docs_dir: Path) -> None:
    """Every source prompt must have a corresponding doc marker."""
    source = _get_source_prompts()
    doc = _parse_doc_prompts(docs_dir)

    missing = set(source) - set(doc)
    if missing:
        print(f"\nPrompts in source but not documented: {sorted(missing)}")

    assert not missing, f"Source prompts missing from docs: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Test 2 — no fabricated markers
# ---------------------------------------------------------------------------


def test_no_fabricated_prompt_markers(docs_dir: Path) -> None:
    """Every doc marker must correspond to a source prompt."""
    source = _get_source_prompts()
    doc = _parse_doc_prompts(docs_dir)

    fabricated = set(doc) - set(source)
    if fabricated:
        print(f"\nDoc markers with no source prompt: {sorted(fabricated)}")

    assert not fabricated, f"Documentation has markers with no source: {sorted(fabricated)}"


# ---------------------------------------------------------------------------
# Test 3 — verbatim content match
# ---------------------------------------------------------------------------


def test_prompt_content_matches_source(docs_dir: Path) -> None:
    """Documented prompt text must match the source verbatim."""
    source = _get_source_prompts()
    doc = _parse_doc_prompts(docs_dir)

    mismatches: list[str] = []
    for marker in sorted(set(source) & set(doc)):
        source_text = source[marker].strip()
        doc_text = doc[marker].strip()
        if source_text != doc_text:
            source_lines = source_text.splitlines()
            doc_lines = doc_text.splitlines()
            for i, (s, d) in enumerate(zip(source_lines, doc_lines, strict=False)):
                if s != d:
                    mismatches.append(
                        f"  {marker}: first diff at line {i + 1}:\n"
                        f"    source: {s!r}\n"
                        f"    doc:    {d!r}"
                    )
                    break
            else:
                mismatches.append(
                    f"  {marker}: line count differs "
                    f"(source={len(source_lines)}, doc={len(doc_lines)})"
                )

    if mismatches:
        print("\nPrompt content mismatches:\n" + "\n".join(mismatches))
        print("\nRun `uv run python scripts/generate_prompt_docs.py` to regenerate.")

    assert not mismatches, (
        "Prompt docs are out of date. "
        "Run: uv run python scripts/generate_prompt_docs.py\n" + "\n".join(mismatches)
    )
