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

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_PROMPTS_DOC = "reference/prompts.md"

# Matches  <!-- prompt:NAME -->\n```text\n...CONTENT...```\n<!-- /prompt:NAME -->
_PROMPT_BLOCK_RE = re.compile(
    r"<!-- prompt:(\S+) -->\n```text\n(.*?)```\n<!-- /prompt:\1 -->",
    re.DOTALL,
)


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
    from scripts.generate_prompt_docs import get_source_prompts

    source = get_source_prompts()
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
    from scripts.generate_prompt_docs import get_source_prompts

    source = get_source_prompts()
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
    from scripts.generate_prompt_docs import get_source_prompts

    source = get_source_prompts()
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


# ---------------------------------------------------------------------------
# Test 4 — generated sections match source
# ---------------------------------------------------------------------------


def test_generated_sections_match() -> None:
    """prompts.md generated sections must be up to date with source prompts."""
    from scripts.generate_prompt_docs import generate, get_source_prompts

    doc_path = _REPO_ROOT / "docs" / "reference" / "prompts.md"
    content = doc_path.read_text(encoding="utf-8")
    prompts = get_source_prompts()
    updated = generate(content, prompts)
    assert content == updated, "prompts.md is stale — run `make docs-prompts` to regenerate"
