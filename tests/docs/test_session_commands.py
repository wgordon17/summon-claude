"""Guard tests: commands.md <-> COMMAND_ACTIONS bidirectional + content match."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.docs

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_COMMANDS_DOC = "reference/commands.md"


def _read_commands_doc(docs_dir: Path) -> str:
    doc_path = docs_dir / _COMMANDS_DOC
    assert doc_path.exists(), f"commands.md not found: {doc_path}"
    return doc_path.read_text(encoding="utf-8")


def _parse_session_commands_table(content: str) -> list[str]:
    """Extract command names from the session commands table.

    Parses the table under "## Session commands", stops at the next ## heading.
    Returns bare names (no ! prefix, first word only).
    Excludes the header row.
    """
    # Find the session commands section
    section_start = re.search(r"^## Session commands", content, re.MULTILINE)
    if not section_start:
        return []

    # Find the next ## heading after this section
    next_heading = re.search(r"^## ", content[section_start.end() :], re.MULTILINE)
    if next_heading:
        section = content[section_start.start() : section_start.end() + next_heading.start()]
    else:
        section = content[section_start.start() :]

    # Match table rows: | `!cmd ...` | ... |
    # Extract command name: strip !, take first word only
    cmd_names: list[str] = []
    table_row_re = re.compile(r"^\|\s*`!(\S+)", re.MULTILINE)
    for m in table_row_re.finditer(section):
        raw = m.group(1)
        # Strip trailing backtick or other punctuation, take first word
        name = raw.rstrip("`").split()[0].lower()
        # Skip table separator rows
        if name.startswith("-"):
            continue
        cmd_names.append(name)

    return cmd_names


def test_documented_session_commands_exist(docs_dir: Path) -> None:
    """Every command in the session commands table must exist in COMMAND_ACTIONS or aliases."""
    from summon_claude.sessions.commands import _ALIAS_LOOKUP, COMMAND_ACTIONS

    content = _read_commands_doc(docs_dir)
    cmd_names = _parse_session_commands_table(content)
    assert cmd_names, "No session commands found in the table — parser may be broken"

    missing: list[str] = []
    for name in cmd_names:
        if name not in COMMAND_ACTIONS and name not in _ALIAS_LOOKUP:
            missing.append(name)

    if missing:
        pytest.fail(
            f"Commands in session commands table not found in COMMAND_ACTIONS or _ALIAS_LOOKUP: "
            f"{sorted(missing)}"
        )


def test_all_commands_are_documented(docs_dir: Path) -> None:
    """Every key in COMMAND_ACTIONS must appear somewhere in commands.md."""
    from summon_claude.sessions.commands import COMMAND_ACTIONS

    content = _read_commands_doc(docs_dir)

    undocumented: list[str] = []
    for name in COMMAND_ACTIONS:
        # Skip plugin-registered entries (contain colon)
        if ":" in name:
            continue
        # Search for `!name` anywhere in the doc — the name may be followed by
        # arguments (e.g. `!help [COMMAND]`) or appear as a heading (### !help),
        # so we match on the bare token `!name` with a word boundary after.
        pattern = r"`!" + re.escape(name) + r"(?:`|\s|\\|\[)"
        if not re.search(pattern, content) and f"### !{name}" not in content:
            undocumented.append(name)

    if undocumented:
        pytest.fail(
            f"COMMAND_ACTIONS keys not found in commands.md (search: `!name`): "
            f"{sorted(undocumented)}"
        )


def test_generated_sections_match() -> None:
    """commands.md generated sections must be up to date with COMMAND_ACTIONS."""
    from scripts.generate_commands_docs import generate

    doc_path = _REPO_ROOT / "docs" / "reference" / "commands.md"
    content = doc_path.read_text(encoding="utf-8")
    updated = generate(content)
    assert content == updated, "commands.md is stale — run `make docs-commands` to regenerate"


# ---------------------------------------------------------------------------
# Test 4 — _classify_commands produces non-empty, disjoint categories
# ---------------------------------------------------------------------------


def test_classify_commands_categories() -> None:
    """_classify_commands must produce non-empty, disjoint categories."""
    from scripts.generate_commands_docs import _classify_commands

    passthrough, blocked_specific, cli_only = _classify_commands()

    assert passthrough, "No passthrough commands — classification may be broken"
    assert blocked_specific, "No blocked-specific commands — classification may be broken"
    assert cli_only, "No CLI-only commands — classification may be broken"

    # Names should be disjoint across categories
    pt_names = {name for name, _ in passthrough}
    bs_names = {name for name, _ in blocked_specific}
    co_names = {name for name, _ in cli_only}

    overlap_pt_bs = pt_names & bs_names
    overlap_pt_co = pt_names & co_names
    overlap_bs_co = bs_names & co_names

    errors: list[str] = []
    if overlap_pt_bs:
        errors.append(f"In both passthrough and blocked-specific: {sorted(overlap_pt_bs)}")
    if overlap_pt_co:
        errors.append(f"In both passthrough and cli-only: {sorted(overlap_pt_co)}")
    if overlap_bs_co:
        errors.append(f"In both blocked-specific and cli-only: {sorted(overlap_bs_co)}")
    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# Test 5 — _build_cli_only_list handles empty input
# ---------------------------------------------------------------------------


def test_build_cli_only_list_empty() -> None:
    """_build_cli_only_list([]) must return empty string."""
    from scripts.generate_commands_docs import _build_cli_only_list

    assert _build_cli_only_list([]) == ""
