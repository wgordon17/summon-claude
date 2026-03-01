"""Shared fixtures for summon-claude tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from slack_sdk.web.async_client import AsyncWebClient

from summon_claude.sessions.registry import SessionRegistry


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


@pytest.fixture
def mock_slack_client() -> AsyncMock:
    """Provide a mocked AsyncWebClient."""
    return AsyncMock(spec=AsyncWebClient)


@pytest.fixture
def monkeypatch_home(tmp_path: Path, monkeypatch) -> Path:
    """Monkeypatch Path.home() to return a temp directory."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path
