"""Guard tests: documented env vars match SummonConfig fields."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.docs.conftest import REPO_ROOT, parse_env_var_refs

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

# Defaults that docs render differently than Python repr.
# Cross-ref: _DEFAULT_OVERRIDES in scripts/generate_env_docs.py covers the same set.
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


def test_env_var_types_match_docs(
    docs_dir: Path,
    summon_config_fields: dict[str, object],
) -> None:
    """Types and defaults in environment-variables.md match SummonConfig."""
    from pydantic_core import PydanticUndefined as _PydanticUndefined

    from scripts.generate_env_docs import _derive_type
    from summon_claude.config import CONFIG_OPTIONS

    ref_doc = docs_dir / "reference" / "environment-variables.md"
    assert ref_doc.exists(), f"Reference doc not found: {ref_doc}"

    content = ref_doc.read_text(encoding="utf-8")
    doc_rows = _parse_env_var_tables(content)
    opt_by_key = {o.env_key: o for o in CONFIG_OPTIONS}

    mismatches: list[str] = []

    for var, field_info in summon_config_fields.items():
        if var not in doc_rows:
            continue  # covered by test_all_config_fields_are_documented

        doc_type = doc_rows[var]["type"]
        doc_default = doc_rows[var]["default"]

        if doc_type is None:
            continue  # malformed table row, skip

        # --- type check ---
        opt = opt_by_key.get(var)
        if opt is None:
            continue
        expected_type_prefix = _derive_type(opt, field_info)

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
        if py_default is _PydanticUndefined:
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


# ---------------------------------------------------------------------------
# Test 4 — generated env-var tables must match current doc content
# ---------------------------------------------------------------------------


def test_env_var_content_matches_generated() -> None:
    """Generated env-var tables must match current doc content."""
    from scripts.generate_env_docs import generate, get_group_tables

    doc_path = REPO_ROOT / "docs" / "reference" / "environment-variables.md"
    content = doc_path.read_text(encoding="utf-8")
    tables = get_group_tables()
    updated = generate(content, tables)
    assert content == updated, (
        "environment-variables.md is stale — run `make docs-generate` to regenerate"
    )


# ---------------------------------------------------------------------------
# Test 5 — input_type='secret' and repr=False must always agree
# ---------------------------------------------------------------------------


def test_secret_fields_input_type_repr_agree() -> None:
    """input_type='secret' and repr=False must always agree."""
    from summon_claude.config import CONFIG_OPTIONS, SummonConfig

    secret_by_input_type = {opt.env_key for opt in CONFIG_OPTIONS if opt.input_type == "secret"}
    secret_by_repr: set[str] = set()
    prefix = SummonConfig.model_config.get("env_prefix", "SUMMON_")
    for field_name, field_info in SummonConfig.model_fields.items():
        env_key = f"{prefix}{field_name.upper()}"
        if not field_info.repr:
            secret_by_repr.add(env_key)

    input_type_only = secret_by_input_type - secret_by_repr
    repr_only = secret_by_repr - secret_by_input_type

    errors = []
    if input_type_only:
        errors.append(f"input_type='secret' but repr=True: {sorted(input_type_only)}")
    if repr_only:
        errors.append(f"repr=False but input_type!='secret': {sorted(repr_only)}")
    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# Test 6 — FUZZY_DEFAULTS and _DEFAULT_OVERRIDES must cover the same set
# ---------------------------------------------------------------------------


def test_fuzzy_defaults_match_default_overrides() -> None:
    """FUZZY_DEFAULTS keys must match _DEFAULT_OVERRIDES keys exactly."""
    from scripts.generate_env_docs import _DEFAULT_OVERRIDES

    fuzzy_keys = set(FUZZY_DEFAULTS.keys())
    override_keys = set(_DEFAULT_OVERRIDES.keys())

    only_in_fuzzy = fuzzy_keys - override_keys
    only_in_overrides = override_keys - fuzzy_keys

    errors = []
    if only_in_fuzzy:
        errors.append(f"In FUZZY_DEFAULTS but not _DEFAULT_OVERRIDES: {sorted(only_in_fuzzy)}")
    if only_in_overrides:
        errors.append(f"In _DEFAULT_OVERRIDES but not FUZZY_DEFAULTS: {sorted(only_in_overrides)}")
    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# Test 7 — help_text with backticks must render clean in CLI (no raw backticks)
# ---------------------------------------------------------------------------


def test_help_text_backticks_stripped_for_cli() -> None:
    """help_text values containing backticks must be safe for CLI display.

    The CLI init wizard strips backticks via hint.replace('`', '').
    This test ensures that pattern produces readable output for all help_text
    values that contain backticks.
    """
    from summon_claude.config import CONFIG_OPTIONS

    for opt in CONFIG_OPTIONS:
        hint = opt.resolve_help_hint() or opt.help_text
        if not hint:
            continue
        cleaned = hint.replace("`", "")
        assert cleaned.strip(), (
            f"{opt.env_key}: hint becomes empty after backtick removal: {hint!r}"
        )


# ---------------------------------------------------------------------------
# Test 8 — _derive_type produces correct mappings
# ---------------------------------------------------------------------------


def test_derive_type_mappings() -> None:
    """_derive_type must produce correct doc type strings for all CONFIG_OPTIONS."""
    from scripts.generate_env_docs import _derive_type
    from summon_claude.config import CONFIG_OPTIONS, SummonConfig

    for opt in CONFIG_OPTIONS:
        field_info = SummonConfig.model_fields.get(opt.field_name)
        if field_info is None:
            continue
        result = _derive_type(opt, field_info)
        assert isinstance(result, str), f"{opt.env_key}: _derive_type returned non-string"
        assert result, f"{opt.env_key}: _derive_type returned empty string"
        if opt.input_type == "secret":
            assert result == "secret", f"{opt.env_key}: secret field should derive type 'secret'"
        elif opt.choices:
            assert result.startswith("choice:"), (
                f"{opt.env_key}: field with choices should derive type starting with 'choice:'"
            )


# ---------------------------------------------------------------------------
# Test 9 — _derive_default produces correct mappings
# ---------------------------------------------------------------------------


def test_derive_default_mappings() -> None:
    """_derive_default must return expected types for all CONFIG_OPTIONS."""
    from pydantic_core import PydanticUndefined

    from scripts.generate_env_docs import _derive_default
    from summon_claude.config import CONFIG_OPTIONS, SummonConfig

    for opt in CONFIG_OPTIONS:
        field_info = SummonConfig.model_fields.get(opt.field_name)
        if field_info is None:
            continue
        result = _derive_default(opt, field_info)
        if opt.input_type == "secret":
            assert result is None, f"{opt.env_key}: secret field must return None (3-col table)"
        elif field_info.default is PydanticUndefined:
            assert result is None, (
                f"{opt.env_key}: required non-secret field must return None, got {result!r}"
            )
        else:
            assert isinstance(result, str) and result, (
                f"{opt.env_key}: _derive_default must return a non-empty string, got {result!r}"
            )


# ---------------------------------------------------------------------------
# Test 10 — None-default fields must have _DEFAULT_OVERRIDES entries
# ---------------------------------------------------------------------------


def test_none_defaults_have_overrides() -> None:
    """Every ConfigOption with a None Pydantic default must have a _DEFAULT_OVERRIDES entry.

    Without an override, _derive_default falls through to the generic
    ``_(empty)_`` rendering, which is wrong for fields that mean "auto-detect"
    or "inherits from another setting".  This test forces the developer to
    choose the correct human-readable default text when adding a new field
    with default=None.
    """
    from pydantic_core import PydanticUndefined

    from scripts.generate_env_docs import _DEFAULT_OVERRIDES
    from summon_claude.config import CONFIG_OPTIONS, SummonConfig

    missing: list[str] = []
    for opt in CONFIG_OPTIONS:
        field_info = SummonConfig.model_fields.get(opt.field_name)
        if field_info is None:
            continue
        if field_info.default is PydanticUndefined:
            continue  # required field — _derive_default returns None
        if field_info.default is not None:
            continue  # has a real default — _derive_default handles it
        # default is None — must have an override to render correctly
        if opt.env_key not in _DEFAULT_OVERRIDES:
            missing.append(opt.env_key)

    assert not missing, (
        f"ConfigOptions with default=None but no _DEFAULT_OVERRIDES entry: {sorted(missing)}. "
        f"Add an entry to _DEFAULT_OVERRIDES in scripts/generate_env_docs.py "
        f"(e.g. '_(auto-detect)_' or '_(empty)_')."
    )

    # Reverse: every _DEFAULT_OVERRIDES key must be a real ConfigOption
    config_keys = {opt.env_key for opt in CONFIG_OPTIONS}
    phantom = set(_DEFAULT_OVERRIDES.keys()) - config_keys
    assert not phantom, (
        f"_DEFAULT_OVERRIDES entries for non-existent ConfigOptions: {sorted(phantom)}. "
        f"Remove stale entries from _DEFAULT_OVERRIDES."
    )
