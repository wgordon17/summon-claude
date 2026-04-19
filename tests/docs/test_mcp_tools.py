"""Guard tests: MCP tool documentation vs source tool definitions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.docs.conftest import parse_mcp_tool_refs

pytestmark = pytest.mark.docs

_MCP_TOOLS_DOC = "reference/mcp-tools.md"

# Regex matching ### or #### heading with a backtick-wrapped tool name
_TOOL_HEADING_RE = re.compile(r"^#{3,4}\s+`([a-zA-Z][a-zA-Z0-9_]+)`", re.MULTILINE)

# Regex for a param table row — handles both 4-column and 5-column (with Default) tables.
# Columns: | `param` | type | Required | [Default |] Description |
# Required is always the 3rd data column.
_PARAM_ROW_RE = re.compile(r"^\|\s*`([\w]+)`\s*\|[^|]*\|\s*(Yes|No)\s*\|", re.MULTILINE)

# Server prefixes for count-mapping


def _normalize_schema(raw: dict) -> tuple[set[str], set[str] | None]:
    """Normalize the two input_schema formats.

    Format 1 (standard JSON Schema): {'type': 'object', 'properties': {...}, 'required': [...]}
    Format 2 (raw annotation map): {'param': <class 'str'>, ...} — values are Python types.

    Returns (property_names, required_names).
    required_names is None when the format doesn't encode optionality (raw annotation map).
    """
    if "properties" in raw or "type" in raw:
        # Standard JSON Schema
        props = set(raw.get("properties", {}).keys())
        required = set(raw.get("required", []))
        return props, required
    # Raw annotation map: keys are param names, values are Python types or dicts
    props = set(raw.keys())
    return props, None  # required info unavailable


def _parse_doc(docs_dir: Path) -> str:
    doc_path = docs_dir / _MCP_TOOLS_DOC
    assert doc_path.exists(), f"MCP tools doc not found: {doc_path}"
    return doc_path.read_text(encoding="utf-8")


def _parse_param_table(tool_name: str, content: str) -> tuple[set[str], set[str]]:
    """Return (all_param_names, required_param_names) from the docs table for tool_name.

    Returns empty sets if the tool has no parameter table ("No parameters." text).
    """
    # Find the heading for this tool
    pattern = re.compile(
        r"^#{3,4}\s+`" + re.escape(tool_name) + r"`.*?(?=^#{2,4}\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    if not match:
        return set(), set()

    section = match.group(0)

    # Check for explicit "no parameters" marker
    if re.search(r"No parameters\.", section, re.IGNORECASE):
        return set(), set()

    # Find the parameter table — it must have a header row with "Parameter"
    table_header = re.search(r"^\|\s*`?Parameter`?\s*\|", section, re.MULTILINE | re.IGNORECASE)
    if not table_header:
        return set(), set()

    all_params: set[str] = set()
    required_params: set[str] = set()

    for row_match in _PARAM_ROW_RE.finditer(section):
        param_name = row_match.group(1)
        is_required = row_match.group(2) == "Yes"
        all_params.add(param_name)
        if is_required:
            required_params.add(param_name)

    return all_params, required_params


# ---------------------------------------------------------------------------
# Test 1
# ---------------------------------------------------------------------------


def test_documented_mcp_tools_exist(docs_dir: Path, mcp_tool_names: set[str]) -> None:
    """Every tool name in the MCP docs must exist in the source tool registry."""
    content = _parse_doc(docs_dir)
    documented = parse_mcp_tool_refs(content)

    fabricated = documented - mcp_tool_names
    if fabricated:
        print(f"\nFabricated tool names (in docs but not in source): {sorted(fabricated)}")

    assert not fabricated, (
        f"Documentation references tools that don't exist in source: {sorted(fabricated)}"
    )


# ---------------------------------------------------------------------------
# Test 2
# ---------------------------------------------------------------------------

INTERNAL_TOOLS: frozenset[str] = frozenset()


def test_all_mcp_tools_are_documented(docs_dir: Path, mcp_tool_names: set[str]) -> None:
    """Every registered MCP tool must appear in the docs (minus internal-only tools)."""
    content = _parse_doc(docs_dir)
    documented = parse_mcp_tool_refs(content)

    public_tools = mcp_tool_names - INTERNAL_TOOLS
    undocumented = public_tools - documented
    if undocumented:
        print(f"\nUndocumented tools (in source but not in docs): {sorted(undocumented)}")

    assert not undocumented, f"Source tools are missing from documentation: {sorted(undocumented)}"


# ---------------------------------------------------------------------------
# Test 3
# ---------------------------------------------------------------------------


def test_mcp_tool_parameters_match(docs_dir: Path, mcp_tool_schemas: dict[str, dict]) -> None:
    """Documented parameter tables must match the source tool input schemas."""
    content = _parse_doc(docs_dir)
    documented_tools = parse_mcp_tool_refs(content)

    mismatches: list[str] = []

    for tool_name in sorted(documented_tools):
        if tool_name not in mcp_tool_schemas:
            # Already caught by test_documented_mcp_tools_exist
            continue

        source_properties, source_required = _normalize_schema(mcp_tool_schemas[tool_name])

        doc_params, doc_required = _parse_param_table(tool_name, content)

        extra_in_docs = doc_params - source_properties
        missing_from_docs = source_properties - doc_params
        # Only compare required when source schema encodes optionality (standard JSON Schema)
        required_mismatch: set[str] = set()
        if source_required is not None:
            required_mismatch = doc_required.symmetric_difference(source_required)

        if extra_in_docs or missing_from_docs or required_mismatch:
            lines = [f"  {tool_name}:"]
            if extra_in_docs:
                lines.append(f"    params in docs but not source: {sorted(extra_in_docs)}")
            if missing_from_docs:
                lines.append(f"    params in source but not docs: {sorted(missing_from_docs)}")
            if required_mismatch:
                lines.append(
                    f"    required mismatch — docs says required={sorted(doc_required)}, "
                    f"source says required={sorted(source_required or [])}"
                )
            mismatches.extend(lines)

    if mismatches:
        print("\nParameter mismatches:\n" + "\n".join(mismatches))

    assert not mismatches, "MCP tool parameter tables don't match source schemas:\n" + "\n".join(
        mismatches
    )


# ---------------------------------------------------------------------------
# Test 4
# ---------------------------------------------------------------------------


def test_mcp_tool_counts_match(docs_dir: Path, mcp_tool_names: set[str]) -> None:
    """Tool counts in the summary table and per-section headings must match source."""
    content = _parse_doc(docs_dir)

    # --- Parse summary table ---
    # Matches: | `summon-slack` | ... | 8 tools — ... |
    summary_re = re.compile(r"^\|\s*`(summon-[\w-]+)`\s*\|[^|]*\|\s*(\d+)\s+tools", re.MULTILINE)
    summary_counts: dict[str, int] = {}
    for m in summary_re.finditer(content):
        summary_counts[m.group(1)] = int(m.group(2))

    # --- Parse per-section heading counts ---
    # Split doc into ## sections by server
    section_re = re.compile(r"^## (summon-[\w-]+)", re.MULTILINE)
    section_matches = list(section_re.finditer(content))

    heading_counts: dict[str, int] = {}
    for i, sec_match in enumerate(section_matches):
        server_name = sec_match.group(1)
        start = sec_match.start()
        end = section_matches[i + 1].start() if i + 1 < len(section_matches) else len(content)
        section_text = content[start:end]
        tool_headings = _TOOL_HEADING_RE.findall(section_text)
        heading_counts[server_name] = len(tool_headings)

    # --- Count from source ---
    from scripts.generate_mcp_docs import _SERVER_PREFIXES, _tools_for_server

    source_counts: dict[str, int] = {
        server: len(_tools_for_server(server, mcp_tool_names)) for server in _SERVER_PREFIXES
    }

    count_errors: list[str] = []
    all_servers = set(_SERVER_PREFIXES.keys()) | set(summary_counts) | set(heading_counts)

    for server in sorted(all_servers):
        h_count = heading_counts.get(server, 0)
        s_count = source_counts.get(server, 0)
        sum_count = summary_counts.get(server)

        if h_count != s_count:
            count_errors.append(f"  {server}: heading count={h_count} != source count={s_count}")
        if sum_count is not None and sum_count != s_count:
            count_errors.append(
                f"  {server}: summary table count={sum_count} != source count={s_count}"
            )

    if count_errors:
        print("\nCount mismatches:\n" + "\n".join(count_errors))
        print(f"\n  Summary table: {summary_counts}")
        print(f"  Heading counts: {heading_counts}")
        print(f"  Source counts: {source_counts}")

    assert not count_errors, "MCP tool counts don't match:\n" + "\n".join(count_errors)


# ---------------------------------------------------------------------------
# Test 5
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_generated_sections_match() -> None:
    """mcp-tools.md generated sections must be up to date with source tools."""
    from scripts.generate_mcp_docs import generate, get_generated_sections

    doc_path = _REPO_ROOT / "docs" / "reference" / "mcp-tools.md"
    content = doc_path.read_text(encoding="utf-8")
    sections = get_generated_sections()
    updated = generate(content, sections)
    assert content == updated, "mcp-tools.md is stale — run `make docs-mcp` to regenerate"
