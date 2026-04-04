"""Guard tests: documented env vars match SummonConfig fields."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.docs.conftest import parse_env_var_refs

pytestmark = pytest.mark.docs

# ---------------------------------------------------------------------------
# Env vars that appear in docs but are NOT SummonConfig fields
# ---------------------------------------------------------------------------

_SUMMON_TEST_PREFIX = "SUMMON_TEST_"

# Env vars read directly via os.environ (not SummonConfig fields)
_NON_CONFIG_VARS = frozenset({"SUMMON_GITHUB_PAT"})

# Files that legitimately reference removed/historical env vars
_EXCLUDED_FILES = {"changelog.md"}


def _is_non_config(var: str) -> bool:
    """Filter out vars that are not SummonConfig fields."""
    return var.startswith(_SUMMON_TEST_PREFIX) or var in _NON_CONFIG_VARS


# ---------------------------------------------------------------------------
# Test 1 — every documented SUMMON_* var must exist in SummonConfig
# ---------------------------------------------------------------------------


def test_documented_env_vars_exist_in_config(
    all_md_files: list[Path],
    summon_config_fields: dict[str, object],
) -> None:
    """All SUMMON_* vars mentioned in docs must be real SummonConfig fields."""
    all_documented: dict[str, list[Path]] = {}
    for md_path in all_md_files:
        if md_path.name in _EXCLUDED_FILES:
            continue
        content = md_path.read_text(encoding="utf-8")
        for var in parse_env_var_refs(content):
            all_documented.setdefault(var, []).append(md_path)

    fabricated = {
        var: paths
        for var, paths in all_documented.items()
        if not _is_non_config(var) and var not in summon_config_fields
    }

    if fabricated:
        lines = ["Documented SUMMON_* vars not found in SummonConfig:"]
        for var, paths in sorted(fabricated.items()):
            for p in paths:
                lines.append(f"  {var}  ({p})")
        pytest.fail("\n".join(lines))


# ---------------------------------------------------------------------------
# Test 2 — every SummonConfig field must appear in the reference doc
# ---------------------------------------------------------------------------

INTENTIONALLY_UNDOCUMENTED: frozenset[str] = frozenset()


def test_all_config_fields_are_documented(
    docs_dir: Path,
    summon_config_fields: dict[str, object],
) -> None:
    """Every SummonConfig field must appear in docs/reference/environment-variables.md."""
    ref_doc = docs_dir / "reference" / "environment-variables.md"
    assert ref_doc.exists(), f"Reference doc not found: {ref_doc}"

    content = ref_doc.read_text(encoding="utf-8")
    documented = parse_env_var_refs(content)

    undocumented = {
        var
        for var in summon_config_fields
        if var not in documented and var not in INTENTIONALLY_UNDOCUMENTED
    }

    if undocumented:
        lines = ["SummonConfig fields missing from environment-variables.md:"]
        for var in sorted(undocumented):
            lines.append(f"  {var}")
        pytest.fail("\n".join(lines))


# ---------------------------------------------------------------------------
# Test 3 — types and defaults in the doc table match SummonConfig metadata
# ---------------------------------------------------------------------------

# Mapping from Python type to the doc-table "Type" column value prefix
_TYPE_MAP: dict[str, str] = {
    "str": "text",
    "int": "integer",
    "bool": "boolean",
}

# Defaults that docs render differently than Python repr
FUZZY_DEFAULTS: dict[str, str | None] = {
    # None / missing-value fields rendered as descriptive prose in docs
    "SUMMON_SCRIBE_ENABLED": None,  # auto-detect (None → bool at model_validator)
    "SUMMON_SCRIBE_GOOGLE_ENABLED": None,  # auto-detect (None → bool at model_validator)
    "SUMMON_SCRIBE_SLACK_ENABLED": None,  # auto-detect (None → bool at model_validator)
    "SUMMON_DEFAULT_MODEL": None,
    "SUMMON_SCRIBE_CWD": None,
    "SUMMON_SCRIBE_MODEL": None,
    # Credentials — no default row in docs (3-column table)
    "SUMMON_SLACK_BOT_TOKEN": None,
    "SUMMON_SLACK_APP_TOKEN": None,
    "SUMMON_SLACK_SIGNING_SECRET": None,
    # Empty-string defaults rendered as _(empty)_ in docs
    "SUMMON_SCRIBE_IMPORTANCE_KEYWORDS": None,
    "SUMMON_SCRIBE_QUIET_HOURS": None,
    "SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS": None,
    # Global PM — None defaults rendered as descriptive prose
    "SUMMON_GLOBAL_PM_CWD": None,
    "SUMMON_GLOBAL_PM_MODEL": None,
}

# Matches a markdown table row: | cell | cell | ...
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$")


def _parse_env_var_tables(content: str) -> dict[str, dict[str, str | None]]:
    """Parse all markdown tables in content; return {env_var: {type, default}}."""
    result: dict[str, dict[str, str | None]] = {}
    for line in content.splitlines():
        m = _TABLE_ROW_RE.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if not cells:
            continue
        # Skip separator rows (--- cells)
        if all(re.fullmatch(r"-+", c) for c in cells if c):
            continue
        # Extract the env var name from the first cell (backtick-wrapped)
        first = cells[0]
        var_match = re.search(r"`(SUMMON_[A-Z_]+)`", first)
        if not var_match:
            continue
        var = var_match.group(1)

        if len(cells) == 3:
            # | Config Key | Type | Description |
            result[var] = {"type": cells[1], "default": None}
        elif len(cells) >= 4:
            # | Config Key | Type | Default | Description |
            result[var] = {"type": cells[1], "default": cells[2]}

    return result


def test_env_var_types_match_docs(  # noqa: PLR0912
    docs_dir: Path,
    summon_config_fields: dict[str, object],
) -> None:
    """Types and defaults in environment-variables.md match SummonConfig."""
    ref_doc = docs_dir / "reference" / "environment-variables.md"
    assert ref_doc.exists(), f"Reference doc not found: {ref_doc}"

    content = ref_doc.read_text(encoding="utf-8")
    doc_rows = _parse_env_var_tables(content)

    mismatches: list[str] = []

    for var, field_info in summon_config_fields.items():
        if var not in doc_rows:
            continue  # covered by test_all_config_fields_are_documented

        doc_type = doc_rows[var]["type"]
        doc_default = doc_rows[var]["default"]

        if doc_type is None:
            continue  # malformed table row, skip

        # --- type check ---
        is_secret = not field_info.repr
        if is_secret:
            expected_type_prefix = "secret"
        else:
            ann = field_info.annotation
            # Handle Optional[X] / X | None — strip None from union args
            if hasattr(ann, "__args__"):
                non_none = [a for a in ann.__args__ if a is not type(None)]
                py_type = non_none[0].__name__ if non_none else "str"
            else:
                py_type = getattr(ann, "__name__", str(ann))
            expected_type_prefix = _TYPE_MAP.get(py_type, py_type)

        # "choice:" in docs is a valid rendering for str fields with constrained values
        type_ok = doc_type.startswith(expected_type_prefix) or (
            expected_type_prefix == "text" and doc_type.startswith("choice")
        )
        if not type_ok:
            mismatches.append(
                f"{var}: expected type prefix '{expected_type_prefix}', got '{doc_type}'"
            )

        # --- default check ---
        if var in FUZZY_DEFAULTS:
            continue  # known non-standard doc rendering, skip

        py_default = field_info.default
        # PydanticUndefined — no default, 3-col table row, skip
        if str(py_default) == "PydanticUndefined":
            continue

        if doc_default is not None:
            # Compare rendered default; handle backtick-wrapped values
            rendered = doc_default.strip("`").strip()
            expected = str(py_default).lower() if isinstance(py_default, bool) else str(py_default)
            if rendered != expected:
                mismatches.append(f"{var}: expected default '{expected}', got '{rendered}'")

    if mismatches:
        detail = "\n".join(f"  {m}" for m in mismatches)
        pytest.fail(f"Type/default mismatches in environment-variables.md:\n{detail}")
