"""Guard tests: permissions.md ↔ permission constants."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from summon_claude.sessions.permissions import (
    _AUTO_APPROVE_TOOLS,
    _GITHUB_MCP_AUTO_APPROVE,
    _GITHUB_MCP_AUTO_APPROVE_PREFIXES,
    _GITHUB_MCP_REQUIRE_APPROVAL,
    _GOOGLE_READ_TOOL_PREFIXES,
    _JIRA_MCP_AUTO_APPROVE_EXACT,
    _JIRA_MCP_AUTO_APPROVE_PREFIXES,
    _JIRA_MCP_HARD_DENY,
    _SUMMON_MCP_AUTO_APPROVE_PREFIXES,
    _WRITE_GATED_TOOLS,
)

pytestmark = pytest.mark.docs

_PERMISSIONS_DOC = "reference/permissions.md"


def _strip_prefix(name: str, prefix: str) -> str:
    """Strip MCP namespace prefix from a tool name."""
    return name[len(prefix) :] if name.startswith(prefix) else name


def _load_doc(docs_dir: Path) -> str:
    doc_path = docs_dir / _PERMISSIONS_DOC
    assert doc_path.exists(), f"Permissions doc not found: {doc_path}"
    return doc_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1: Auto-approved tools table
# ---------------------------------------------------------------------------


def test_auto_approve_tools_match(docs_dir: Path) -> None:
    """Auto-approved tools table in permissions.md must match _AUTO_APPROVE_TOOLS."""
    content = _load_doc(docs_dir)

    # Find the "Auto-approved tools" section
    section_match = re.search(
        r"## Auto-approved tools.*?(?=^---|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert section_match, "Could not find 'Auto-approved tools' section in permissions.md"
    section = section_match.group(0)

    # Extract backtick-wrapped tool names from first column of table rows.
    # Rows look like: | `Read` / `Cat` | Read file contents |
    # Split on " / " to handle multi-name cells.
    doc_tools: set[str] = set()
    row_re = re.compile(r"^\|([^|]+)\|", re.MULTILINE)
    for row_match in row_re.finditer(section):
        cell = row_match.group(1)
        # Skip header and separator rows
        if "---" in cell or "Tool" in cell:
            continue
        # Extract all backtick-wrapped names from this cell
        # The ` / ` separator is between backtick pairs, so findall
        # already returns individual names (e.g., "Read", "Cat")
        for name in re.findall(r"`([^`]+)`", cell):
            doc_tools.add(name)

    assert doc_tools, "No tools found in auto-approved table — check table format"

    missing_from_doc = _AUTO_APPROVE_TOOLS - doc_tools
    extra_in_doc = doc_tools - _AUTO_APPROVE_TOOLS

    errors: list[str] = []
    if missing_from_doc:
        errors.append(f"In _AUTO_APPROVE_TOOLS but not in doc: {sorted(missing_from_doc)}")
    if extra_in_doc:
        errors.append(f"In doc but not in _AUTO_APPROVE_TOOLS: {sorted(extra_in_doc)}")

    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# Test 2: Write-gated tools line
# ---------------------------------------------------------------------------


def test_write_gated_tools_match(docs_dir: Path) -> None:
    """Write-gated tools line in permissions.md must match _WRITE_GATED_TOOLS."""
    content = _load_doc(docs_dir)

    # Find the "Write-gated tools:" line
    line_match = re.search(r"\*\*Write-gated tools:\*\*[^\n]+", content)
    assert line_match, "Could not find '**Write-gated tools:**' line in permissions.md"
    line = line_match.group(0)

    # Extract ALL backtick-wrapped names from that line (including parenthetical)
    doc_tools = set(re.findall(r"`([^`]+)`", line))

    assert doc_tools, "No tool names found on the write-gated tools line"

    missing_from_doc = _WRITE_GATED_TOOLS - doc_tools
    extra_in_doc = doc_tools - _WRITE_GATED_TOOLS

    errors: list[str] = []
    if missing_from_doc:
        errors.append(f"In _WRITE_GATED_TOOLS but not in doc: {sorted(missing_from_doc)}")
    if extra_in_doc:
        errors.append(f"In doc but not in _WRITE_GATED_TOOLS: {sorted(extra_in_doc)}")

    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# Test 3: GitHub require-approval table
# ---------------------------------------------------------------------------


def test_github_require_approval_match(docs_dir: Path) -> None:
    """GitHub 'Always require Slack approval' table must match _GITHUB_MCP_REQUIRE_APPROVAL."""
    content = _load_doc(docs_dir)

    # Find the GitHub MCP section
    github_match = re.search(
        r"## GitHub MCP permissions.*?(?=^---|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert github_match, "Could not find 'GitHub MCP permissions' section"
    github_section = github_match.group(0)

    # Find the "Always require Slack approval" table
    table_match = re.search(
        r"\*\*Always require Slack approval\*\*.*?(?=^!|\Z)",
        github_section,
        re.MULTILINE | re.DOTALL,
    )
    assert table_match, "Could not find 'Always require Slack approval' table in GitHub section"
    table_text = table_match.group(0)

    # Extract tool names from first column of table rows
    doc_tools: set[str] = set()
    row_re = re.compile(r"^\|([^|]+)\|", re.MULTILINE)
    for row_match in row_re.finditer(table_text):
        cell = row_match.group(1).strip()
        if "---" in cell or "Tool" in cell:
            continue
        names = re.findall(r"`([^`]+)`", cell)
        for name in names:
            doc_tools.add(name)

    assert doc_tools, "No tools found in GitHub require-approval table"

    # Compare against short names (strip mcp__github__ prefix)
    expected_short = {_strip_prefix(t, "mcp__github__") for t in _GITHUB_MCP_REQUIRE_APPROVAL}

    missing_from_doc = expected_short - doc_tools
    extra_in_doc = doc_tools - expected_short

    errors: list[str] = []
    if missing_from_doc:
        errors.append(f"In _GITHUB_MCP_REQUIRE_APPROVAL but not in doc: {sorted(missing_from_doc)}")
    if extra_in_doc:
        errors.append(f"In doc but not in _GITHUB_MCP_REQUIRE_APPROVAL: {sorted(extra_in_doc)}")

    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# Test 4: Jira hard-deny table
# ---------------------------------------------------------------------------


def test_jira_hard_deny_match(docs_dir: Path) -> None:
    """Jira 'Hard-denied' table in permissions.md must match _JIRA_MCP_HARD_DENY."""
    content = _load_doc(docs_dir)

    # Find the Jira MCP section
    jira_match = re.search(
        r"## Jira MCP permissions.*?(?=^---|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert jira_match, "Could not find 'Jira MCP permissions' section"
    jira_section = jira_match.group(0)

    # Find the "Hard-denied" table
    table_match = re.search(
        r"\*\*Hard-denied.*?\*\*.*?(?=^!|\Z)",
        jira_section,
        re.MULTILINE | re.DOTALL,
    )
    assert table_match, "Could not find 'Hard-denied' table in Jira section"
    table_text = table_match.group(0)

    # Extract tool names from first column of table rows
    doc_tools: set[str] = set()
    row_re = re.compile(r"^\|([^|]+)\|", re.MULTILINE)
    for row_match in row_re.finditer(table_text):
        cell = row_match.group(1).strip()
        if "---" in cell or "Tool" in cell:
            continue
        names = re.findall(r"`([^`]+)`", cell)
        for name in names:
            doc_tools.add(name)

    assert doc_tools, "No tools found in Jira hard-deny table"

    # Compare against short names (strip mcp__jira__ prefix)
    expected_short = {_strip_prefix(t, "mcp__jira__") for t in _JIRA_MCP_HARD_DENY}

    missing_from_doc = expected_short - doc_tools
    extra_in_doc = doc_tools - expected_short

    errors: list[str] = []
    if missing_from_doc:
        errors.append(f"In _JIRA_MCP_HARD_DENY but not in doc: {sorted(missing_from_doc)}")
    if extra_in_doc:
        errors.append(f"In doc but not in _JIRA_MCP_HARD_DENY: {sorted(extra_in_doc)}")

    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# Test 5: GitHub auto-approve prose
# ---------------------------------------------------------------------------


def test_github_auto_approve_match(docs_dir: Path) -> None:
    """GitHub auto-approve prose must mention all prefix patterns and exact tool names."""
    content = _load_doc(docs_dir)

    # Find the GitHub MCP section
    github_match = re.search(
        r"## GitHub MCP permissions.*?(?=^---|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert github_match, "Could not find 'GitHub MCP permissions' section"
    github_section = github_match.group(0)

    # Find the "Auto-approved (read-only):" line
    auto_approve_line = re.search(r"\*\*Auto-approved[^*]*\*\*[^\n]+", github_section)
    assert auto_approve_line, "Could not find 'Auto-approved (read-only):' line in GitHub section"
    line = auto_approve_line.group(0)

    # Verify all short prefix patterns appear (with trailing underscore to avoid false matches)
    expected_prefixes = {
        _strip_prefix(p, "mcp__github__") for p in _GITHUB_MCP_AUTO_APPROVE_PREFIXES
    }
    missing_prefixes = [p for p in expected_prefixes if p not in line]
    assert not missing_prefixes, (
        f"GitHub auto-approve prefixes missing from doc prose: {sorted(missing_prefixes)}"
    )

    # Verify all exact tool short names appear
    expected_exact = {_strip_prefix(t, "mcp__github__") for t in _GITHUB_MCP_AUTO_APPROVE}
    missing_exact = [t for t in expected_exact if t not in line]
    assert not missing_exact, (
        f"GitHub auto-approve exact tools missing from doc prose: {sorted(missing_exact)}"
    )


# ---------------------------------------------------------------------------
# Test 6: Jira auto-approve prose
# ---------------------------------------------------------------------------


def test_jira_auto_approve_match(docs_dir: Path) -> None:
    """Jira auto-approve prose must mention all prefix patterns and exact tool names."""
    content = _load_doc(docs_dir)

    # Find the Jira MCP section
    jira_match = re.search(
        r"## Jira MCP permissions.*?(?=^---|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert jira_match, "Could not find 'Jira MCP permissions' section"
    jira_section = jira_match.group(0)

    # Find the "Auto-approved (read-only):" line
    auto_approve_line = re.search(r"\*\*Auto-approved[^*]*\*\*[^\n]+", jira_section)
    assert auto_approve_line, "Could not find 'Auto-approved (read-only):' line in Jira section"
    line = auto_approve_line.group(0)

    # Derive short prefix patterns from _JIRA_MCP_AUTO_APPROVE_PREFIXES:
    # strip mcp__jira__ prefix → {"get", "search", "lookup"}
    # The doc uses backtick-and-asterisk form like `get*`, so match against backtick patterns
    expected_prefixes = {_strip_prefix(p, "mcp__jira__") for p in _JIRA_MCP_AUTO_APPROVE_PREFIXES}
    missing_prefixes = [p for p in expected_prefixes if p not in line]
    assert not missing_prefixes, (
        f"Jira auto-approve prefixes missing from doc prose: {sorted(missing_prefixes)}"
    )

    # Verify all exact tool short names appear
    expected_exact = {_strip_prefix(t, "mcp__jira__") for t in _JIRA_MCP_AUTO_APPROVE_EXACT}
    missing_exact = [t for t in expected_exact if t not in line]
    assert not missing_exact, (
        f"Jira auto-approve exact tools missing from doc prose: {sorted(missing_exact)}"
    )


# ---------------------------------------------------------------------------
# Test 7a: Google MCP structural presence
# ---------------------------------------------------------------------------


def test_google_mcp_structural(docs_dir: Path) -> None:
    """Permissions flow table must include a row mentioning Google MCP."""
    content = _load_doc(docs_dir)

    # Find the "Permission flow (internal)" table
    flow_match = re.search(
        r"## Permission flow.*?(?=^---|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert flow_match, "Could not find 'Permission flow' section in permissions.md"
    flow_section = flow_match.group(0)

    # Check that the Google MCP section exists in the document
    assert re.search(r"Google MCP", flow_section, re.IGNORECASE), (
        "Permission flow table does not mention 'Google MCP'"
    )


# ---------------------------------------------------------------------------
# Test 7b: _GOOGLE_READ_TOOL_PREFIXES pinned
# ---------------------------------------------------------------------------


def test_google_read_tool_prefixes_pinned() -> None:
    """_GOOGLE_READ_TOOL_PREFIXES must match the expected set exactly."""
    assert set(_GOOGLE_READ_TOOL_PREFIXES) == {
        "get_",
        "list_",
        "search_",
        "query_",
        "read_",
        "check_",
        "debug_",
        "inspect_",
    }


# ---------------------------------------------------------------------------
# Test 8: Summon MCP prefixes prose
# ---------------------------------------------------------------------------


def test_summon_mcp_prefixes_match(docs_dir: Path) -> None:
    """Summon MCP auto-approve prose must mention all server names."""
    content = _load_doc(docs_dir)

    # Extract server names from _SUMMON_MCP_AUTO_APPROVE_PREFIXES:
    # strip mcp__ prefix and trailing __ suffix
    server_names = {
        p.removeprefix("mcp__").removesuffix("__") for p in _SUMMON_MCP_AUTO_APPROVE_PREFIXES
    }

    missing = [name for name in server_names if name not in content]
    assert not missing, f"Summon MCP server names missing from permissions.md: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Test 9: Permission flow table references valid constants
# ---------------------------------------------------------------------------


def test_permission_flow_table_constants(docs_dir: Path) -> None:
    """Backtick identifiers starting with _ in permission flow table must be real module attrs."""
    import summon_claude.sessions.permissions as permissions_module

    content = _load_doc(docs_dir)

    # Find the "Permission flow (internal)" table section
    flow_match = re.search(
        r"## Permission flow.*?(?=^---|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert flow_match, "Could not find 'Permission flow' section in permissions.md"
    flow_section = flow_match.group(0)

    # Extract all backtick-wrapped identifiers that start with _
    identifiers = re.findall(r"`(_[A-Z_]+)`", flow_section)

    assert identifiers, (
        "No _CONSTANT identifiers found in permission flow table — check table format"
    )

    missing: list[str] = []
    for ident in identifiers:
        if not hasattr(permissions_module, ident):
            missing.append(ident)

    assert not missing, (
        f"Permission flow table references constants not in permissions module: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# Test 10: All collection constants in permissions.py are tested
# ---------------------------------------------------------------------------

# Constants that are tested indirectly or are implementation details
_INTENTIONALLY_UNTESTED: frozenset[str] = frozenset(
    {
        "_JIRA_MCP_PREFIX",  # used internally, not a standalone permission list
        "_WRITE_TOOL_PATH_KEYS",  # implementation detail, not a permission list
    }
)


def test_all_permission_constants_are_covered() -> None:
    """Every frozenset/tuple collection constant in permissions.py must be tested."""
    import summon_claude.sessions.permissions as permissions_module

    # Find all module-level collection constants (frozenset or tuple, name starts with _)
    source_constants: set[str] = set()
    for name in dir(permissions_module):
        if not name.startswith("_") or name.startswith("__"):
            continue
        val = getattr(permissions_module, name)
        if isinstance(val, (frozenset, tuple)) and name.isupper():
            source_constants.add(name)

    # Constants tested by the tests above (imported at module level)
    tested = {
        "_AUTO_APPROVE_TOOLS",
        "_WRITE_GATED_TOOLS",
        "_GITHUB_MCP_REQUIRE_APPROVAL",
        "_GITHUB_MCP_AUTO_APPROVE",
        "_GITHUB_MCP_AUTO_APPROVE_PREFIXES",
        "_JIRA_MCP_HARD_DENY",
        "_JIRA_MCP_AUTO_APPROVE_PREFIXES",
        "_JIRA_MCP_AUTO_APPROVE_EXACT",
        "_GOOGLE_READ_TOOL_PREFIXES",
        "_SUMMON_MCP_AUTO_APPROVE_PREFIXES",
    }

    untested = source_constants - tested - _INTENTIONALLY_UNTESTED
    assert not untested, (
        f"Permission constants in permissions.py not covered by guard tests: {sorted(untested)}. "
        f"Add a test or add to _INTENTIONALLY_UNTESTED with justification."
    )
