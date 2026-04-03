"""Guard tests for diagnostic registry coverage.

These tests scan the codebase to ensure that all MCP servers, external binaries,
and config credential fields have corresponding diagnostic checks.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from summon_claude.config import SummonConfig
from summon_claude.diagnostics import DIAGNOSTIC_REGISTRY, KNOWN_SUBSYSTEMS

# ---------------------------------------------------------------------------
# Source paths
# ---------------------------------------------------------------------------

_SRC_ROOT = Path(__file__).parent.parent / "src" / "summon_claude"
_SESSION_PY = _SRC_ROOT / "sessions" / "session.py"
_DIAGNOSTICS_PY = _SRC_ROOT / "diagnostics.py"


# ---------------------------------------------------------------------------
# Step 1: MCP registration scanner
# ---------------------------------------------------------------------------

# Explicit mapping: MCP server key in mcp_servers dict → diagnostic subsystem
# Core/internal servers that have no dedicated check are commented out.
_MCP_TO_SUBSYSTEM: dict[str, str] = {
    "github": "mcp_github",
    # Core MCP servers (always present, no diagnostic check needed):
    # "summon-slack"  — core, not an external service requiring auth check
    # "summon-canvas" — core, not an external service requiring auth check
    # "summon-cli"    — core, not an external service requiring auth check
    # "external-slack"— core/scribe feature, no dedicated subsystem
    # workspace-mcp: NOT in mcp_servers dict (conditionally built via
    # _build_google_workspace_mcp). Coverage ensured by binary scanner
    # (find_workspace_mcp_bin) in Step 2.
}

# Keys to explicitly exclude from the scanner (core/internal servers)
_MCP_EXCLUDE = frozenset(
    {"summon-slack", "summon-canvas", "summon-cli", "workspace", "external-slack", "jira"}
)


def _scan_mcp_server_keys() -> set[str]:
    """Scan session.py for mcp_servers dict key assignments."""
    source = _SESSION_PY.read_text()
    # Match patterns like: mcp_servers["github"] or mcp_servers['github']
    pattern = re.compile(r'mcp_servers\[["\']([\w\-]+)["\']\]')
    return set(pattern.findall(source))


def test_mcp_servers_have_diagnostic_checks() -> None:
    """Each non-core MCP server key must map to an entry in DIAGNOSTIC_REGISTRY."""
    mcp_keys = _scan_mcp_server_keys()
    # Filter out excluded core servers
    external_keys = mcp_keys - _MCP_EXCLUDE

    missing: list[str] = []
    for key in sorted(external_keys):
        if key not in _MCP_TO_SUBSYSTEM:
            missing.append(
                f"MCP key '{key}' not in _MCP_TO_SUBSYSTEM — add it or add to _MCP_EXCLUDE"
            )
        else:
            subsystem = _MCP_TO_SUBSYSTEM[key]
            if subsystem not in DIAGNOSTIC_REGISTRY:
                missing.append(f"MCP key '{key}' maps to '{subsystem}' not in DIAGNOSTIC_REGISTRY")

    assert not missing, "\n".join(missing)


# ---------------------------------------------------------------------------
# Step 2: External binary scanner
# ---------------------------------------------------------------------------

# Mapping: binary name or function pattern → diagnostic subsystem
_BINARY_TO_SUBSYSTEM: dict[str, str] = {
    "claude": "environment",
    "gh": "environment",
    "uv": "environment",
    "sqlite3": "environment",
}

# find_*_bin() function call patterns → subsystem
_FIND_BIN_TO_SUBSYSTEM: dict[str, str] = {
    "find_workspace_mcp_bin": "mcp_workspace",
}


def _scan_which_calls() -> set[str]:
    """Scan diagnostics.py for shutil.which('...') string arguments."""
    source = _DIAGNOSTICS_PY.read_text()
    pattern = re.compile(r'shutil\.which\(["\'](\w+)["\']\)')
    return set(pattern.findall(source))


def _scan_find_bin_calls() -> set[str]:
    """Scan diagnostics.py for find_*_bin() function calls."""
    source = _DIAGNOSTICS_PY.read_text()
    pattern = re.compile(r"\b(find_\w+_bin)\(\)")
    return set(pattern.findall(source))


def test_binary_checks_have_subsystems() -> None:
    """Every binary referenced in diagnostics.py must have a known subsystem."""
    detected_binaries = _scan_which_calls()
    missing: list[str] = []
    for binary in sorted(detected_binaries):
        if binary not in _BINARY_TO_SUBSYSTEM:
            missing.append(
                f"Binary '{binary}' found in shutil.which() calls but not in _BINARY_TO_SUBSYSTEM"
            )
        else:
            subsystem = _BINARY_TO_SUBSYSTEM[binary]
            if subsystem not in DIAGNOSTIC_REGISTRY:
                missing.append(
                    f"Binary '{binary}' maps to '{subsystem}' which is not in DIAGNOSTIC_REGISTRY"
                )
    assert not missing, "\n".join(missing)


def test_find_bin_calls_have_subsystems() -> None:
    """Every find_*_bin() call in diagnostics.py must map to a known subsystem."""
    detected_fns = _scan_find_bin_calls()
    missing: list[str] = []
    for fn in sorted(detected_fns):
        if fn not in _FIND_BIN_TO_SUBSYSTEM:
            missing.append(
                f"Function '{fn}()' found in diagnostics.py but not in _FIND_BIN_TO_SUBSYSTEM"
            )
        else:
            subsystem = _FIND_BIN_TO_SUBSYSTEM[fn]
            if subsystem not in DIAGNOSTIC_REGISTRY:
                missing.append(
                    f"Function '{fn}()' maps to '{subsystem}' which is not in DIAGNOSTIC_REGISTRY"
                )
    assert not missing, "\n".join(missing)


# ---------------------------------------------------------------------------
# Step 3: Config credential field scanner
# ---------------------------------------------------------------------------

# Explicit mapping: SummonConfig field name → diagnostic subsystem
_CREDENTIAL_TO_SUBSYSTEM: dict[str, str] = {
    "slack_bot_token": "slack",
    "slack_app_token": "slack",
    "slack_signing_secret": "slack",
    # github_auth uses file-based token storage (github_auth.load_token()),
    # not a SummonConfig field. Coverage verified via MCP scanner (Step 1).
    "scribe_enabled": "mcp_workspace",  # scribe uses workspace-mcp's creds
}

# Fields that are credential-like in name but don't warrant their own subsystem
_EXCLUDED_FIELDS = frozenset(
    {
        "scribe_slack_enabled",  # sub-feature of scribe, not an independent subsystem
        "scribe_google_enabled",  # sub-feature of scribe, covered by mcp_workspace
        "scribe_slack_browser",  # config value, not a credential
        "scribe_slack_monitored_channels",  # config value, not a credential
        "auto_classifier_enabled",  # feature flag, not a credential
    }
)

# Patterns that indicate credential-like or feature-flag fields
_CREDENTIAL_PATTERNS = re.compile(r"(_token|_pat|_secret|_enabled)$")


def test_credential_fields_have_subsystems() -> None:
    """Every credential or feature-flag field on SummonConfig must be mapped or excluded."""
    model_fields = set(SummonConfig.model_fields.keys())

    # Fields that look credential-like but aren't in mapping or exclusions
    unmapped: list[str] = []
    for field_name in sorted(model_fields):
        if not _CREDENTIAL_PATTERNS.search(field_name):
            continue
        if field_name in _CREDENTIAL_TO_SUBSYSTEM:
            # Check mapping target exists in registry
            subsystem = _CREDENTIAL_TO_SUBSYSTEM[field_name]
            if subsystem not in DIAGNOSTIC_REGISTRY:
                unmapped.append(
                    f"Field '{field_name}' maps to '{subsystem}' not in DIAGNOSTIC_REGISTRY"
                )
        elif field_name not in _EXCLUDED_FIELDS:
            unmapped.append(f"Add '{field_name}' to _CREDENTIAL_TO_SUBSYSTEM or _EXCLUDED_FIELDS")

    assert not unmapped, "\n".join(unmapped)


def test_credential_mapping_fields_exist_on_config() -> None:
    """All fields in _CREDENTIAL_TO_SUBSYSTEM must exist on SummonConfig (catches renames)."""
    model_fields = set(SummonConfig.model_fields.keys())
    missing: list[str] = []
    for field_name in sorted(_CREDENTIAL_TO_SUBSYSTEM):
        if field_name not in model_fields:
            missing.append(
                f"_CREDENTIAL_TO_SUBSYSTEM references '{field_name}' missing from SummonConfig"
            )
    assert not missing, "\n".join(missing)


# ---------------------------------------------------------------------------
# Step 4: KNOWN_SUBSYSTEMS pin test
# ---------------------------------------------------------------------------


def test_known_subsystems_matches_registry() -> None:
    """KNOWN_SUBSYSTEMS must exactly match DIAGNOSTIC_REGISTRY.keys()."""
    registry_keys = set(DIAGNOSTIC_REGISTRY.keys())
    missing_from_registry = KNOWN_SUBSYSTEMS - registry_keys
    missing_from_known = registry_keys - KNOWN_SUBSYSTEMS

    errors: list[str] = []
    if missing_from_registry:
        errors.append(
            "In KNOWN_SUBSYSTEMS but missing from"
            f" DIAGNOSTIC_REGISTRY: {sorted(missing_from_registry)}"
        )
    if missing_from_known:
        errors.append(
            "In DIAGNOSTIC_REGISTRY but missing from"
            f" KNOWN_SUBSYSTEMS: {sorted(missing_from_known)}"
        )

    assert not errors, "\n".join(errors)


def test_known_subsystems_is_frozenset() -> None:
    """KNOWN_SUBSYSTEMS must be a frozenset (type pin)."""
    assert isinstance(KNOWN_SUBSYSTEMS, frozenset)


def test_diagnostic_registry_is_dict() -> None:
    """DIAGNOSTIC_REGISTRY must be a dict."""
    assert isinstance(DIAGNOSTIC_REGISTRY, dict)


def test_all_registry_entries_implement_protocol() -> None:
    """Every entry in DIAGNOSTIC_REGISTRY must implement DiagnosticCheck protocol."""
    from summon_claude.diagnostics import DiagnosticCheck

    for name, check in DIAGNOSTIC_REGISTRY.items():
        assert isinstance(check, DiagnosticCheck), (
            f"DIAGNOSTIC_REGISTRY['{name}'] does not implement DiagnosticCheck protocol"
        )
        assert hasattr(check, "name"), f"'{name}'.name missing"
        assert hasattr(check, "description"), f"'{name}'.description missing"
        assert callable(getattr(check, "run", None)), f"'{name}'.run not callable"


# ---------------------------------------------------------------------------
# Step 5: _DB_TABLES guard test
# ---------------------------------------------------------------------------


def test_db_tables_matches_schema(tmp_path) -> None:
    """_DB_TABLES must include all tables in the live schema (bidirectional)."""
    import asyncio

    from summon_claude.diagnostics import _DB_TABLES
    from summon_claude.sessions.registry import SessionRegistry

    async def _get_tables() -> set[str]:
        # Pristine DB — no cross-test contamination from session-scoped fixture
        async with SessionRegistry(db_path=tmp_path / "schema_check.db") as reg:
            rows = await reg.db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            return {r[0] for r in rows}

    schema_tables = asyncio.run(_get_tables())
    db_tables_set = set(_DB_TABLES)

    missing = schema_tables - db_tables_set
    assert not missing, f"Tables in schema but missing from _DB_TABLES: {sorted(missing)}"

    extra = db_tables_set - schema_tables
    assert not extra, f"Tables in _DB_TABLES but missing from schema: {sorted(extra)}"
