"""Shared fixtures for summon-claude tests."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import summon_claude.github_auth
from summon_claude.config import SummonConfig
from summon_claude.sessions.registry import SessionRegistry
from summon_claude.sessions.scheduler import SessionScheduler


@pytest.fixture(autouse=True, scope="session")
def _strip_claudecode():
    """Remove CLAUDECODE env var so SDK subprocesses don't detect nesting.

    Prevents Claude Code's nesting guard from blocking SDK subprocess spawns
    when tests are run from within a Claude Code session.
    """
    old = os.environ.pop("CLAUDECODE", None)
    yield
    if old is not None:
        os.environ["CLAUDECODE"] = old


@pytest.fixture(autouse=True, scope="session")
def _isolate_summon_env():
    """Strip SUMMON_* env vars (except SUMMON_TEST_*) for the entire test session.

    Prevents local config (SUMMON_DEFAULT_MODEL, SUMMON_CHANNEL_PREFIX, etc.)
    from leaking into SummonConfig construction via pydantic-settings env reading.
    SUMMON_TEST_* vars are preserved for integration tests.
    """
    keys = [k for k in os.environ if k.startswith("SUMMON_") and not k.startswith("SUMMON_TEST_")]
    saved = {k: os.environ.pop(k) for k in keys}
    yield
    os.environ.update(saved)


@pytest.fixture(autouse=True, scope="session")
def _isolate_registry_db(tmp_path_factory):
    """Prevent tests from writing to the real registry.db."""
    db_dir = tmp_path_factory.mktemp("db")
    with patch(
        "summon_claude.sessions.registry.default_db_path",
        return_value=db_dir / "registry.db",
    ):
        yield


@pytest.fixture(autouse=True, scope="session")
def _isolate_data_dir(tmp_path_factory):
    """Prevent tests from writing log files or other data to the real data dir."""
    data_dir = tmp_path_factory.mktemp("data")
    config_dir = tmp_path_factory.mktemp("config")
    (data_dir / "logs").mkdir()
    with (
        # Per-module patches for static 'from summon_claude.config import X' bindings.
        # Source-level patches are intentionally omitted: test modules that import
        # get_config_dir/get_data_dir directly (e.g. test_cli_reset.py) would see
        # inconsistent results if we patched the source but not their local binding.
        patch("summon_claude.sessions.session.get_data_dir", return_value=data_dir),
        patch("summon_claude.cli.session.get_data_dir", return_value=data_dir),
        patch("summon_claude.cli.config.get_data_dir", return_value=data_dir),
        patch("summon_claude.daemon.get_data_dir", return_value=data_dir),
        patch("summon_claude.cli.__init__.get_data_dir", return_value=data_dir),
        patch("summon_claude.cli.model_cache.get_data_dir", return_value=data_dir),
        patch("summon_claude.cli.reset.get_data_dir", return_value=data_dir),
        patch("summon_claude.sessions.manager.get_data_dir", return_value=data_dir),
        patch("summon_claude.diagnostics.get_data_dir", return_value=data_dir),
        # sessions.registry is covered by _isolate_registry_db (patches default_db_path
        # directly); add as defense-in-depth for any direct get_data_dir callers.
        patch("summon_claude.sessions.registry.get_data_dir", return_value=data_dir),
        # get_config_dir per-module patches
        patch("summon_claude.cli.reset.get_config_dir", return_value=config_dir),
        patch("summon_claude.diagnostics.get_config_dir", return_value=config_dir),
        patch("summon_claude.github_auth.get_config_dir", return_value=config_dir),
        # jira_auth.get_config_dir is an inline import inside a function body —
        # picks up the source-level patch automatically, no separate patch needed.
    ):
        yield


@pytest.fixture(autouse=True)
def _cleanup_root_logger_handlers():
    """Remove any QueueHandlers leaked onto the root logger by tests."""
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            if isinstance(handler, logging.handlers.QueueHandler):
                handler.close()


@pytest.fixture(autouse=True)
def _reset_install_mode(monkeypatch):
    """Clear local install detection cache and force global mode for all tests.

    Without this, ``uv run pytest`` sets VIRTUAL_ENV and the repo has
    pyproject.toml, so every test would detect local mode.
    """
    from summon_claude.config import (
        _detect_install_mode,
        _find_project_root,
        _get_git_main_repo_root,
    )
    from summon_claude.diagnostics import _config_dir_str, _data_dir_str

    _detect_install_mode.cache_clear()
    _find_project_root.cache_clear()
    _get_git_main_repo_root.cache_clear()
    _data_dir_str.cache_clear()
    _config_dir_str.cache_clear()
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("SUMMON_LOCAL", raising=False)
    yield
    # Import fresh in case importlib.reload() created a new function object
    from summon_claude.config import _detect_install_mode as fresh
    from summon_claude.config import _find_project_root as fresh_root
    from summon_claude.config import _get_git_main_repo_root as fresh_git
    from summon_claude.diagnostics import _config_dir_str as fresh_config
    from summon_claude.diagnostics import _data_dir_str as fresh_data

    fresh.cache_clear()
    if hasattr(fresh_root, "cache_clear"):
        fresh_root.cache_clear()
    fresh_git.cache_clear()
    fresh_data.cache_clear()
    fresh_config.cache_clear()


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Provide a temporary SQLite database path."""
    return tmp_path / "test.db"


@pytest.fixture
async def registry(temp_db_path: Path) -> SessionRegistry:
    """Provide a SessionRegistry instance with a temp database."""
    reg = SessionRegistry(db_path=temp_db_path)
    async with reg:
        yield reg


def make_scheduler() -> SessionScheduler:
    """Create a minimal SessionScheduler for tests."""
    return SessionScheduler(asyncio.Queue(maxsize=100), asyncio.Event())


def make_test_config(**overrides) -> SummonConfig:
    """Create a SummonConfig with valid defaults, isolated from env vars and .env files.

    The session-scoped ``_isolate_summon_env`` fixture strips SUMMON_* env vars,
    but pydantic-settings also reads .env files. ``_env_file=None`` suppresses that.
    """
    defaults = {
        "slack_bot_token": "xoxb-test-token",
        "slack_app_token": "xapp-test-token",
        "slack_signing_secret": "abc123def456",
    }
    defaults.update(overrides)
    return SummonConfig(**defaults, _env_file=None)


# ---------------------------------------------------------------------------
# Shared CLI test helpers
# ---------------------------------------------------------------------------

ACTIVE_SESSION = {
    "session_id": "aaaa1111-2222-3333-4444-555566667777",
    "status": "active",
    "session_name": "my-proj",
    "slack_channel_name": "summon-my-proj-0224",
    "slack_channel_id": "C999",
    "cwd": "/home/user/my-proj",
    "pid": os.getpid(),
    "model": "claude-sonnet-4-20250514",
    "total_turns": 12,
    "total_cost_usd": 0.1234,
    "started_at": "2026-02-24T10:00:00+00:00",
    "authenticated_at": "2026-02-24T10:01:00+00:00",
    "last_activity_at": "2026-02-24T11:00:00+00:00",
    "ended_at": None,
    "claude_session_id": "claude-abc",
}

COMPLETED_SESSION = {
    "session_id": "bbbb1111-2222-3333-4444-555566667777",
    "status": "completed",
    "session_name": "old-proj",
    "slack_channel_name": "summon-old-proj-0223",
    "slack_channel_id": "C888",
    "cwd": "/home/user/old-proj",
    "pid": 99999,
    "model": "claude-sonnet-4-20250514",
    "total_turns": 5,
    "total_cost_usd": 0.05,
    "started_at": "2026-02-23T09:00:00+00:00",
    "ended_at": "2026-02-23T10:00:00+00:00",
}


def make_hooks_mock_registry(hooks: list[str]) -> MagicMock:
    """Build a mock SessionRegistry returning *hooks* from get_lifecycle_hooks_by_directory.

    The mock returns *hooks* regardless of which hook_type is queried.
    Suitable for worktree_create flow tests; add side_effect discrimination
    if tests need to distinguish hook types.
    """
    instance = AsyncMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    instance.get_lifecycle_hooks_by_directory = AsyncMock(return_value=hooks)
    return MagicMock(return_value=instance)


def mock_registry(**overrides: object) -> AsyncMock:
    """Build an AsyncMock that acts as SessionRegistry async context manager."""
    reg = AsyncMock()
    reg.list_active = AsyncMock(return_value=overrides.get("active", []))
    reg.list_all = AsyncMock(return_value=overrides.get("all", []))
    reg.get_session = AsyncMock(return_value=overrides.get("session"))
    # resolve_session returns (session, matches) tuple
    _resolve = overrides.get("resolve", overrides.get("session"))
    if _resolve is None:
        reg.resolve_session = AsyncMock(return_value=(None, []))
    elif isinstance(_resolve, list):
        # Ambiguous: multiple matches, no unique session
        reg.resolve_session = AsyncMock(return_value=(None, _resolve))
    else:
        reg.resolve_session = AsyncMock(return_value=(_resolve, [_resolve]))
    reg.list_stale = AsyncMock(return_value=overrides.get("stale", []))
    reg.mark_stale = AsyncMock()
    reg.update_status = AsyncMock()
    reg.log_event = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=reg)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _global_xdg_dir(env_var: str, default_rel: str) -> Path:
    """Replicate _xdg_dir() logic without the local-mode short-circuit."""
    xdg = os.environ.get(env_var, "").strip()
    if xdg:
        p = Path(xdg)
        if p.is_absolute():
            return p / "summon"
    candidate = Path.home() / default_rel / "summon"
    if (Path.home() / default_rel).exists():
        return candidate
    return Path.home() / ".summon"


@pytest.fixture(autouse=True, scope="session")
def _guard_no_global_xdg_writes():
    """Assert that no test writes to the real global XDG data/config directories.

    Detects net-new file/directory creation in the global summon data and
    config paths. Does NOT detect overwrites of existing files.
    """
    real_data_dir = _global_xdg_dir("XDG_DATA_HOME", ".local/share")
    real_config_dir = _global_xdg_dir("XDG_CONFIG_HOME", ".config")

    def _snapshot(p: Path) -> set[str]:
        try:
            return {e.name for e in p.iterdir()}
        except OSError:
            return set()

    before_data = _snapshot(real_data_dir)
    before_config = _snapshot(real_config_dir)
    yield
    after_data = _snapshot(real_data_dir)
    after_config = _snapshot(real_config_dir)
    new_data = after_data - before_data
    new_config = after_config - before_config
    assert not new_data, (
        f"Tests wrote to global XDG data dir {real_data_dir}: new entries {new_data}"
    )
    assert not new_config, (
        f"Tests wrote to global XDG config dir {real_config_dir}: new entries {new_config}"
    )
