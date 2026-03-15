"""Shared fixtures for summon-claude tests."""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from summon_claude.sessions.registry import SessionRegistry


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
    (data_dir / "logs").mkdir()
    with (
        patch("summon_claude.sessions.session.get_data_dir", return_value=data_dir),
        patch("summon_claude.cli.session.get_data_dir", return_value=data_dir),
        patch("summon_claude.cli.config.get_data_dir", return_value=data_dir),
        patch("summon_claude.daemon.get_data_dir", return_value=data_dir),
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
