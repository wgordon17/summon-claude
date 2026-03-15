"""Tests for summon_claude.summon_cli_mcp — session lifecycle MCP tools."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from summon_claude.sessions.registry import SessionRegistry
from summon_claude.summon_cli_mcp import (
    _SENSITIVE_FIELDS,
    create_summon_cli_mcp_server,
    create_summon_cli_mcp_tools,
)


@pytest.fixture
async def registry(temp_db_path: Path) -> SessionRegistry:
    reg = SessionRegistry(db_path=temp_db_path)
    async with reg:
        yield reg


@pytest.fixture
async def populated_registry(registry: SessionRegistry) -> SessionRegistry:
    """Registry with sample sessions for testing."""
    await registry.register(
        session_id="parent-1111",
        pid=os.getpid(),
        cwd="/home/user/proj",
        name="parent-session",
        model="claude-sonnet-4-20250514",
        authenticated_user_id="U_OWNER",
    )
    await registry.update_status(
        "parent-1111",
        "active",
        slack_channel_id="C100",
        slack_channel_name="summon-parent",
        authenticated_user_id="U_OWNER",
    )

    await registry.register(
        session_id="child-2222",
        pid=os.getpid(),
        cwd="/home/user/proj",
        name="child-session",
        parent_session_id="parent-1111",
        authenticated_user_id="U_OWNER",
    )
    await registry.update_status(
        "child-2222",
        "active",
        slack_channel_id="C200",
        slack_channel_name="summon-child",
        authenticated_user_id="U_OWNER",
    )

    await registry.register(
        session_id="other-3333",
        pid=os.getpid(),
        cwd="/home/other/proj",
        name="other-session",
        authenticated_user_id="U_OTHER",
    )
    await registry.update_status("other-3333", "active", authenticated_user_id="U_OTHER")

    await registry.register(
        session_id="done-4444",
        pid=99999,
        cwd="/home/user/old",
        name="done-session",
        authenticated_user_id="U_OWNER",
    )
    await registry.update_status("done-4444", "completed")

    return registry


@pytest.fixture
def tools(populated_registry: SessionRegistry) -> dict:
    return {
        t.name: t
        for t in create_summon_cli_mcp_tools(
            registry=populated_registry,
            session_id="parent-1111",
            authenticated_user_id="U_OWNER",
            channel_id="C100",
            cwd="/home/user/proj",
        )
    }


class TestSessionList:
    async def test_active_filter(self, tools):
        result = await tools["session_list"].handler({"filter": "active"})
        assert not result.get("is_error")
        text = result["content"][0]["text"]
        assert "parent" in text
        assert "child" in text
        assert "other" in text
        # completed session should not appear
        assert "done-sess" not in text

    async def test_all_filter(self, tools):
        result = await tools["session_list"].handler({"filter": "all"})
        assert not result.get("is_error")
        text = result["content"][0]["text"]
        # all sessions including completed
        assert "done-sess" in text

    async def test_mine_filter(self, tools):
        result = await tools["session_list"].handler({"filter": "mine"})
        assert not result.get("is_error")
        text = result["content"][0]["text"]
        assert "child" in text
        # parent itself and other user's sessions should not appear
        assert "parent-sess" not in text
        assert "other-sess" not in text

    async def test_invalid_filter(self, tools):
        result = await tools["session_list"].handler({"filter": "invalid"})
        assert result["is_error"] is True

    async def test_default_is_active(self, tools):
        result = await tools["session_list"].handler({})
        assert not result.get("is_error")
        text = result["content"][0]["text"]
        assert "done-sess" not in text

    async def test_excludes_sensitive_fields(self, tools):
        result = await tools["session_list"].handler({"filter": "all"})
        text = result["content"][0]["text"]
        for field in _SENSITIVE_FIELDS:
            assert f"{field}=" not in text


class TestSessionInfo:
    async def test_found(self, tools):
        result = await tools["session_info"].handler({"session_id": "parent-1111"})
        assert not result.get("is_error")
        text = result["content"][0]["text"]
        assert "parent-1111" in text
        assert "active" in text

    async def test_not_found(self, tools):
        result = await tools["session_info"].handler({"session_id": "nonexistent"})
        assert result["is_error"] is True
        assert "not found" in result["content"][0]["text"]

    async def test_excludes_sensitive_fields(self, tools):
        result = await tools["session_info"].handler({"session_id": "parent-1111"})
        text = result["content"][0]["text"]
        for field in _SENSITIVE_FIELDS:
            assert f"{field}:" not in text

    async def test_missing_session_id(self, tools):
        result = await tools["session_info"].handler({})
        assert result["is_error"] is True


class TestSessionStart:
    async def test_invalid_name_empty(self, tools):
        result = await tools["session_start"].handler({"name": ""})
        assert result["is_error"] is True
        assert "name" in result["content"][0]["text"].lower()

    async def test_invalid_name_uppercase(self, tools):
        result = await tools["session_start"].handler({"name": "BadName"})
        assert result["is_error"] is True

    async def test_invalid_name_too_long(self, tools):
        result = await tools["session_start"].handler({"name": "a" * 21})
        assert result["is_error"] is True

    async def test_invalid_cwd(self, tools):
        result = await tools["session_start"].handler(
            {"name": "test-session", "cwd": "/nonexistent/path/12345"}
        )
        assert result["is_error"] is True
        assert "does not exist" in result["content"][0]["text"]

    async def test_cwd_breakout_rejected(self, tools, tmp_path):
        """CWD outside the calling session's directory is rejected."""
        # tools fixture has cwd="/home/user/proj"; tmp_path is NOT under it
        result = await tools["session_start"].handler(
            {"name": "test-session", "cwd": str(tmp_path)}
        )
        assert result["is_error"] is True
        assert "must be within" in result["content"][0]["text"]

    async def test_cwd_parent_dir_rejected(self, populated_registry, tmp_path):
        """CWD that is a parent of the session's directory is rejected."""
        # Build a real parent/child dir so the parent passes is_dir()
        # but fails the ancestor check (parent is ABOVE the session's CWD).
        parent = tmp_path / "parent"
        parent.mkdir()
        child = parent / "child"
        child.mkdir()

        tools_child = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd=str(child),  # session lives in child/
            )
        }
        # Try to spawn in parent/ — exists, but is ABOVE session's CWD
        result = await tools_child["session_start"].handler(
            {"name": "test-session", "cwd": str(parent)}
        )
        assert result["is_error"] is True
        assert "must be within" in result["content"][0]["text"]

    async def test_cwd_subdirectory_allowed(self, populated_registry, tmp_path):
        """CWD that is a subdirectory of the session's directory is allowed."""
        from summon_claude.sessions.auth import SpawnAuth

        # Create a subdir under a controlled parent so Path.is_dir() passes
        parent = tmp_path / "proj"
        parent.mkdir()
        subdir = parent / "sub"
        subdir.mkdir()

        # Build tools with cwd=parent so subdir is a valid descendant
        tools_sub = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd=str(parent),
            )
        }

        mock_spawn = AsyncMock(
            return_value=SpawnAuth(
                token="tok123",
                parent_session_id="parent-1111",
                parent_channel_id="C100",
                target_user_id="U_OWNER",
                cwd=str(subdir),
                spawn_source="session",
                expires_at=None,
            )
        )
        mock_ipc = AsyncMock(return_value="new-session-id")

        with (
            patch("summon_claude.sessions.auth.generate_spawn_token", mock_spawn),
            patch("summon_claude.cli.daemon_client.create_session_with_spawn_token", mock_ipc),
        ):
            result = await tools_sub["session_start"].handler(
                {"name": "test-sub", "cwd": str(subdir)}
            )
        assert not result.get("is_error"), result

    async def test_cwd_symlink_escape_rejected(self, populated_registry, tmp_path):
        """Symlink pointing outside caller's CWD is rejected after resolution."""
        parent = tmp_path / "proj"
        parent.mkdir()
        escape_target = tmp_path / "escape"
        escape_target.mkdir()
        link = parent / "sneaky"
        link.symlink_to(escape_target)

        tools_link = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd=str(parent),
            )
        }

        result = await tools_link["session_start"].handler(
            {"name": "test-escape", "cwd": str(link)}
        )
        assert result["is_error"] is True
        assert "must be within" in result["content"][0]["text"]

    async def test_creates_via_daemon_ipc(self, populated_registry, tmp_path):
        from summon_claude.sessions.auth import SpawnAuth

        # Build tools with tmp_path as CWD so the default CWD passes the
        # ancestor check (no explicit cwd arg -> uses the session's own CWD).
        local_tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd=str(tmp_path),
            )
        }

        mock_spawn = AsyncMock(
            return_value=SpawnAuth(
                token="tok123",
                parent_session_id="parent-1111",
                parent_channel_id="C100",
                target_user_id="U_OWNER",
                cwd=str(tmp_path),
                spawn_source="session",
                expires_at=None,
            )
        )
        mock_ipc = AsyncMock(return_value="new-session-id")

        with (
            patch("summon_claude.sessions.auth.generate_spawn_token", mock_spawn),
            patch("summon_claude.cli.daemon_client.create_session_with_spawn_token", mock_ipc),
        ):
            result = await local_tools["session_start"].handler({"name": "test-session"})
        assert not result.get("is_error"), result
        assert "new-session-id" in result["content"][0]["text"]


class TestSessionStop:
    async def test_not_found(self, tools):
        result = await tools["session_stop"].handler({"session_id": "nonexistent"})
        assert result["is_error"] is True
        assert "not found" in result["content"][0]["text"]

    async def test_already_ended(self, tools):
        result = await tools["session_stop"].handler({"session_id": "done-4444"})
        assert result["is_error"] is True
        assert "completed" in result["content"][0]["text"]

    async def test_self_stop_rejected(self, tools):
        result = await tools["session_stop"].handler({"session_id": "parent-1111"})
        assert result["is_error"] is True
        assert "own session" in result["content"][0]["text"]

    async def test_wrong_user_rejected(self, tools):
        result = await tools["session_stop"].handler({"session_id": "other-3333"})
        assert result["is_error"] is True
        assert "different user" in result["content"][0]["text"]

    async def test_missing_session_id(self, tools):
        result = await tools["session_stop"].handler({})
        assert result["is_error"] is True

    async def test_stops_via_daemon_ipc(self, tools):
        mock_ipc = AsyncMock(return_value=True)
        with patch("summon_claude.cli.daemon_client.stop_session", mock_ipc):
            result = await tools["session_stop"].handler({"session_id": "child-2222"})
        assert not result.get("is_error")
        assert "Stopped" in result["content"][0]["text"]
        mock_ipc.assert_called_once_with("child-2222")


class TestListChildren:
    async def test_returns_children(self, populated_registry):
        children = await populated_registry.list_children("parent-1111")
        assert len(children) == 1
        assert children[0]["session_id"] == "child-2222"

    async def test_no_children(self, populated_registry):
        children = await populated_registry.list_children("other-3333")
        assert children == []


class TestMCPServerCreation:
    def test_returns_valid_config(self, populated_registry):
        config = create_summon_cli_mcp_server(populated_registry, "sid", "uid", "cid", "/tmp")
        assert config["name"] == "summon-cli"
        assert config["type"] == "sdk"

    def test_tool_count(self, populated_registry):
        tools = create_summon_cli_mcp_tools(populated_registry, "sid", "uid", "cid", "/tmp")
        assert len(tools) == 4
