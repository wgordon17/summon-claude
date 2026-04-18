"""Empirical tests proving get_data_dir/get_config_dir test isolation.

These tests prove that the _isolate_data_dir fixture comprehensively intercepts
ALL callers of get_data_dir() and get_config_dir(), regardless of import style,
with zero per-module maintenance.

The isolation mechanism:
  - Patches summon_claude.config._xdg_dir → all calls to get_data_dir/get_config_dir
    that delegate to _xdg_dir are redirected to temp dirs.
  - Patches summon_claude.config.get_local_root → returns None (global mode), so
    local-mode shortcuts never apply.
  - Because get_data_dir() and get_config_dir() call _xdg_dir() by name (not by
    binding), and Python resolves that name from config's module globals at call time,
    patching summon_claude.config._xdg_dir intercepts ALL callers regardless of
    how they imported get_data_dir/get_config_dir.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

import summon_claude.cli
import summon_claude.cli.config
import summon_claude.cli.model_cache
import summon_claude.cli.reset
import summon_claude.cli.session
import summon_claude.daemon
import summon_claude.diagnostics
import summon_claude.github_auth
import summon_claude.sessions.manager
import summon_claude.sessions.registry
import summon_claude.sessions.session
from summon_claude.config import ConfigOption, get_config_dir, get_data_dir

# ---------------------------------------------------------------------------
# Guard tests for isolation infrastructure contracts
# ---------------------------------------------------------------------------


def test_dotenv_settings_source_has_env_file_attribute():
    """Guard: pydantic-settings DotEnvSettingsSource must expose env_file.

    SummonConfig.settings_customise_sources relies on
    ``getattr(dotenv_settings, 'env_file', None)`` to detect when
    ``_env_file=None`` was passed.  If pydantic-settings renames or removes
    the attribute, the test-mode bypass breaks silently.
    """
    from pydantic_settings import BaseSettings, DotEnvSettingsSource

    src = DotEnvSettingsSource(BaseSettings, env_file=None)
    assert hasattr(src, "env_file"), (
        "DotEnvSettingsSource no longer has env_file attribute — "
        "update SummonConfig.settings_customise_sources"
    )
    assert src.env_file is None


def test_resolve_help_hint_string():
    """resolve_help_hint() returns a str help_hint as-is."""
    opt = ConfigOption(
        field_name="test",
        env_key="TEST",
        group="g",
        label="l",
        help_text="h",
        input_type="text",
        help_hint="static hint",
    )
    assert opt.resolve_help_hint() == "static hint"


def test_resolve_help_hint_callable():
    """resolve_help_hint() calls a callable help_hint and returns the result."""
    opt = ConfigOption(
        field_name="test",
        env_key="TEST",
        group="g",
        label="l",
        help_text="h",
        input_type="text",
        help_hint=lambda: "lazy hint",
    )
    assert opt.resolve_help_hint() == "lazy hint"


def test_resolve_help_hint_none():
    """resolve_help_hint() returns None when help_hint is None."""
    opt = ConfigOption(
        field_name="test",
        env_key="TEST",
        group="g",
        label="l",
        help_text="h",
        input_type="text",
    )
    assert opt.resolve_help_hint() is None


# ---------------------------------------------------------------------------
# Prove get_data_dir() / get_config_dir() return temp paths during tests
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("data_dir_isolation")
def test_data_dir_is_isolated():
    """Prove that during test execution, get_data_dir() and get_config_dir()
    return isolated temp paths (not the real XDG/home directories).

    The _isolate_data_dir fixture (session autouse) patches _xdg_dir and
    get_local_root so all callers get temp paths.  This test verifies the
    expected isolation is in effect by:
    1. Calling get_data_dir() and checking it differs from the real XDG path.
    2. Calling get_config_dir() and checking it differs from the real XDG path.

    If this test fails, it typically means one of:
    - import-time evaluation was reintroduced into config.py
    - _isolate_data_dir fixture coverage gap for a new module
    """
    # Ensure global mode (no local root) so we go through _xdg_dir path
    # _reset_install_mode (autouse) already cleared caches and deleted VIRTUAL_ENV
    data = get_data_dir()
    config = get_config_dir()

    real_data = Path.home() / ".local" / "share" / "summon"
    real_config = Path.home() / ".config" / "summon"

    # Must differ from real defaults (isolation is active)
    assert data != real_data, (
        f"get_data_dir() is NOT isolated: returns real XDG path {data!r}. "
        "Check _isolate_data_dir fixture and import-time evaluations in config.py."
    )
    assert config != real_config, (
        f"get_config_dir() is NOT isolated: returns real XDG path {config!r}. "
        "Check _isolate_data_dir fixture and import-time evaluations in config.py."
    )


# ---------------------------------------------------------------------------
# ALL module-level bindings resolve to the same isolated path
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("data_dir_isolation")
class TestModuleBindingsAreCovered:
    """All static 'from summon_claude.config import get_data_dir' bindings must
    resolve to the same isolated temp dir as the canonical get_data_dir()."""

    def _canonical_data(self) -> Path:
        return get_data_dir()

    def _canonical_config(self) -> Path:
        return get_config_dir()

    # ---- get_data_dir sites ----

    def test_sessions_session_get_data_dir(self):
        mod = sys.modules["summon_claude.sessions.session"]
        assert mod.get_data_dir() == self._canonical_data(), (
            "summon_claude.sessions.session.get_data_dir() is not isolated"
        )

    def test_cli_session_get_data_dir(self):
        mod = sys.modules["summon_claude.cli.session"]
        assert mod.get_data_dir() == self._canonical_data(), (
            "summon_claude.cli.session.get_data_dir() is not isolated"
        )

    def test_cli_config_get_data_dir(self):
        mod = sys.modules["summon_claude.cli.config"]
        assert mod.get_data_dir() == self._canonical_data(), (
            "summon_claude.cli.config.get_data_dir() is not isolated"
        )

    def test_daemon_get_data_dir(self):
        mod = sys.modules["summon_claude.daemon"]
        assert mod.get_data_dir() == self._canonical_data(), (
            "summon_claude.daemon.get_data_dir() is not isolated"
        )

    def test_cli_init_get_data_dir(self):
        import summon_claude.cli as cli_mod

        assert cli_mod.get_data_dir() == self._canonical_data(), (
            "summon_claude.cli.get_data_dir() is not isolated"
        )

    def test_cli_model_cache_get_data_dir(self):
        mod = sys.modules["summon_claude.cli.model_cache"]
        assert mod.get_data_dir() == self._canonical_data(), (
            "summon_claude.cli.model_cache.get_data_dir() is not isolated"
        )

    def test_cli_reset_get_data_dir(self):
        mod = sys.modules["summon_claude.cli.reset"]
        assert mod.get_data_dir() == self._canonical_data(), (
            "summon_claude.cli.reset.get_data_dir() is not isolated"
        )

    def test_sessions_manager_get_data_dir(self):
        mod = sys.modules["summon_claude.sessions.manager"]
        assert mod.get_data_dir() == self._canonical_data(), (
            "summon_claude.sessions.manager.get_data_dir() is not isolated"
        )

    def test_diagnostics_get_data_dir(self):
        mod = sys.modules["summon_claude.diagnostics"]
        assert mod.get_data_dir() == self._canonical_data(), (
            "summon_claude.diagnostics.get_data_dir() is not isolated"
        )

    def test_sessions_registry_get_data_dir(self):
        mod = sys.modules["summon_claude.sessions.registry"]
        assert mod.get_data_dir() == self._canonical_data(), (
            "summon_claude.sessions.registry.get_data_dir() is not isolated"
        )

    # ---- get_config_dir sites ----

    def test_cli_reset_get_config_dir(self):
        mod = sys.modules["summon_claude.cli.reset"]
        assert mod.get_config_dir() == self._canonical_config(), (
            "summon_claude.cli.reset.get_config_dir() is not isolated"
        )

    def test_diagnostics_get_config_dir(self):
        mod = sys.modules["summon_claude.diagnostics"]
        assert mod.get_config_dir() == self._canonical_config(), (
            "summon_claude.diagnostics.get_config_dir() is not isolated"
        )

    def test_github_auth_get_config_dir(self):
        mod = sys.modules["summon_claude.github_auth"]
        assert mod.get_config_dir() == self._canonical_config(), (
            "summon_claude.github_auth.get_config_dir() is not isolated"
        )


# ---------------------------------------------------------------------------
# Auto-discovery: find ALL module-level import sites and verify coverage
# ---------------------------------------------------------------------------

_SRC_ROOT = Path(__file__).parent.parent / "src"
_PACKAGE_ROOT = _SRC_ROOT / "summon_claude"


def _find_import_sites() -> list[tuple[str, int, str]]:
    """AST-scan all .py files for module-level imports of get_data_dir/get_config_dir.

    Returns (module_dotpath, lineno, name) tuples.  Only top-level statements
    are checked (``ast.iter_child_nodes`` on the module body), so inline imports
    inside function bodies are excluded — those are covered automatically by the
    source-level ``_xdg_dir`` patch via LEGB resolution.
    """
    targets = {"get_data_dir", "get_config_dir"}
    sites: list[tuple[str, int, str]] = []

    for py_file in sorted(_PACKAGE_ROOT.rglob("*.py")):
        rel = py_file.relative_to(_SRC_ROOT)
        module_path = ".".join(rel.with_suffix("").parts)
        if module_path.endswith(".__init__"):
            module_path = module_path[: -len(".__init__")]

        try:
            tree = ast.parse(py_file.read_text(), filename=str(py_file))
        except SyntaxError:
            continue

        # Only top-level statements — inline imports inside function bodies
        # are excluded (they pick up the _xdg_dir source patch automatically).
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "summon_claude.config":
                continue
            for name_alias in node.names:
                if name_alias.name in targets:
                    sites.append((module_path, node.lineno, name_alias.name))

    return sites


@pytest.mark.xdist_group("data_dir_isolation")
@pytest.mark.parametrize("info", _find_import_sites(), ids=lambda t: f"{t[0]}:{t[1]}:{t[2]}")
def test_import_site_is_isolated(info):
    """Auto-discovered import site: calling the function returns the isolated path.

    This test parameterizes over ALL module-level import sites discovered by AST
    scan.  A new module that imports get_data_dir from summon_claude.config will
    automatically appear here and be verified — zero conftest changes needed.
    """
    module_dotpath, lineno, func_name = info

    canonical = get_data_dir() if func_name == "get_data_dir" else get_config_dir()

    mod = importlib.import_module(module_dotpath)
    fn = getattr(mod, func_name, None)
    if fn is None:
        pytest.skip(f"{module_dotpath} does not expose {func_name} as a module attribute")

    result = fn()
    assert result == canonical, (
        f"{module_dotpath}.{func_name}() returned {result!r}, "
        f"expected isolated path {canonical!r}. "
        f"Import at line {lineno} is not covered by _isolate_data_dir."
    )
