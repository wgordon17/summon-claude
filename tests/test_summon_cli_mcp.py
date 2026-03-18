"""Tests for summon_claude.summon_cli_mcp — session lifecycle MCP tools."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

from summon_claude.sessions.registry import MAX_SPAWN_CHILDREN_PM, SessionRegistry
from summon_claude.summon_cli_mcp import (
    _SENSITIVE_FIELDS,
    create_summon_cli_mcp_server,
    create_summon_cli_mcp_tools,
)


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
        # other user's sessions should not appear (same-user scope guard)
        assert "other-sess" not in text
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

    async def test_includes_full_session_id(self, tools):
        result = await tools["session_list"].handler({"filter": "active"})
        text = result["content"][0]["text"]
        assert "parent-1111" in text
        assert "child-2222" in text

    async def test_other_user_sessions_hidden(self, tools):
        """Sessions owned by a different user are never visible."""
        for filter_type in ("active", "all"):
            result = await tools["session_list"].handler({"filter": filter_type})
            text = result["content"][0]["text"]
            assert "other-3333" not in text


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

    async def test_other_user_session_hidden(self, tools):
        """Other user's session returns 'not found' (no existence leak)."""
        result = await tools["session_info"].handler({"session_id": "other-3333"})
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
                cwd=str(child),
            )
        }
        result = await tools_child["session_start"].handler(
            {"name": "test-session", "cwd": str(parent)}
        )
        assert result["is_error"] is True
        assert "must be within" in result["content"][0]["text"]

    async def test_cwd_subdirectory_allowed(self, populated_registry, tmp_path):
        """CWD that is a subdirectory of the session's directory is allowed."""
        from summon_claude.sessions.auth import SpawnAuth

        parent = tmp_path / "proj"
        parent.mkdir()
        subdir = parent / "sub"
        subdir.mkdir()

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

        tools_sub = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd=str(parent),
                _generate_spawn_token=mock_spawn,
                _ipc_create_session=mock_ipc,
            )
        }

        result = await tools_sub["session_start"].handler({"name": "test-sub", "cwd": str(subdir)})
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

        local_tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd=str(tmp_path),
                _generate_spawn_token=mock_spawn,
                _ipc_create_session=mock_ipc,
            )
        }

        result = await local_tools["session_start"].handler({"name": "test-session"})
        assert not result.get("is_error"), result
        assert "new-session-id" in result["content"][0]["text"]

    async def test_active_child_cap_enforced(self, registry, tmp_path):
        """Spawning beyond MAX_SPAWN_CHILDREN_PM is rejected with details."""
        # Register a parent
        await registry.register(
            session_id="pm-parent",
            pid=os.getpid(),
            cwd=str(tmp_path),
            name="pm-parent",
            authenticated_user_id="U_PM",
        )
        await registry.update_status("pm-parent", "active", authenticated_user_id="U_PM")

        # Spawn exactly MAX_SPAWN_CHILDREN_PM active children
        for i in range(MAX_SPAWN_CHILDREN_PM):
            sid = f"child-{i:04d}"
            await registry.register(
                session_id=sid,
                pid=os.getpid(),
                cwd=str(tmp_path),
                name=f"child-{i}",
                parent_session_id="pm-parent",
                authenticated_user_id="U_PM",
            )
            await registry.update_status(sid, "active", authenticated_user_id="U_PM")

        local_tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=registry,
                session_id="pm-parent",
                authenticated_user_id="U_PM",
                channel_id="C_PM",
                cwd=str(tmp_path),
            )
        }

        result = await local_tools["session_start"].handler({"name": "one-too-many"})
        assert result["is_error"] is True
        text = result["content"][0]["text"]
        assert "limit reached" in text
        assert f"{MAX_SPAWN_CHILDREN_PM}" in text
        # Should list active session names AND full IDs so the agent can stop them
        assert "child-0" in text
        assert "child-0000" in text  # full session ID included

    async def test_completed_children_dont_count_toward_cap(self, registry, tmp_path):
        """Completed children don't count toward the active cap."""
        await registry.register(
            session_id="pm-parent-2",
            pid=os.getpid(),
            cwd=str(tmp_path),
            name="pm-parent-2",
            authenticated_user_id="U_PM2",
        )
        await registry.update_status("pm-parent-2", "active", authenticated_user_id="U_PM2")

        # Spawn _MAX children but mark all as completed
        for i in range(MAX_SPAWN_CHILDREN_PM):
            sid = f"done-{i:04d}"
            await registry.register(
                session_id=sid,
                pid=os.getpid(),
                cwd=str(tmp_path),
                name=f"done-{i}",
                parent_session_id="pm-parent-2",
                authenticated_user_id="U_PM2",
            )
            await registry.update_status(sid, "completed")

        from summon_claude.sessions.auth import SpawnAuth

        mock_spawn = AsyncMock(
            return_value=SpawnAuth(
                token="tok",
                parent_session_id="pm-parent-2",
                parent_channel_id="C_PM2",
                target_user_id="U_PM2",
                cwd=str(tmp_path),
                spawn_source="session",
                expires_at=None,
            )
        )
        mock_ipc = AsyncMock(return_value="new-id")

        local_tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=registry,
                session_id="pm-parent-2",
                authenticated_user_id="U_PM2",
                channel_id="C_PM2",
                cwd=str(tmp_path),
                _generate_spawn_token=mock_spawn,
                _ipc_create_session=mock_ipc,
            )
        }

        result = await local_tools["session_start"].handler({"name": "still-ok"})
        assert not result.get("is_error"), result

    async def test_registry_error_blocks_spawn(self, tmp_path):
        """Registry failure during cap check is fail-closed, not fail-open."""
        mock_reg = AsyncMock()
        mock_reg.list_children = AsyncMock(side_effect=RuntimeError("DB locked"))

        local_tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=mock_reg,
                session_id="pm-x",
                authenticated_user_id="U_PM",
                channel_id="C_PM",
                cwd=str(tmp_path),
            )
        }

        result = await local_tools["session_start"].handler({"name": "should-fail"})
        assert result["is_error"] is True
        assert "could not verify" in result["content"][0]["text"]

    def test_max_active_children_constant(self):
        assert MAX_SPAWN_CHILDREN_PM == 15

    async def test_depth_limit_blocks_spawn(self, tmp_path):
        """Spawning beyond MAX_SPAWN_DEPTH is rejected."""
        # Build a 3-deep chain: root -> child -> grandchild -> great-grandchild
        mock_reg = AsyncMock()
        mock_reg.compute_spawn_depth = AsyncMock(return_value=2)

        local_tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=mock_reg,
                session_id="deep-session",
                authenticated_user_id="U_DEEP",
                channel_id="C_DEEP",
                cwd=str(tmp_path),
            )
        }

        result = await local_tools["session_start"].handler({"name": "too-deep"})
        assert result["is_error"] is True
        text = result["content"][0]["text"]
        assert "depth limit" in text

    async def test_depth_check_error_blocks_spawn(self, tmp_path):
        """Depth check failure is fail-closed."""
        mock_reg = AsyncMock()
        mock_reg.compute_spawn_depth = AsyncMock(side_effect=RuntimeError("DB error"))

        local_tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=mock_reg,
                session_id="err-session",
                authenticated_user_id="U_ERR",
                channel_id="C_ERR",
                cwd=str(tmp_path),
            )
        }

        result = await local_tools["session_start"].handler({"name": "should-fail"})
        assert result["is_error"] is True
        assert "spawn depth" in result["content"][0]["text"]


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

    async def test_wrong_user_hidden(self, tools):
        """Other user's session returns 'not found' — no existence leak."""
        result = await tools["session_stop"].handler({"session_id": "other-3333"})
        assert result["is_error"] is True
        assert "not found" in result["content"][0]["text"]

    async def test_missing_session_id(self, tools):
        result = await tools["session_stop"].handler({})
        assert result["is_error"] is True

    async def test_stops_via_daemon_ipc(self, populated_registry):
        mock_ipc = AsyncMock(return_value=True)
        local_tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                _ipc_stop_session=mock_ipc,
            )
        }
        result = await local_tools["session_stop"].handler({"session_id": "child-2222"})
        assert not result.get("is_error")
        assert "Stopped" in result["content"][0]["text"]
        mock_ipc.assert_called_once_with("child-2222")

    async def test_daemon_not_found_returns_warning(self, populated_registry):
        """When daemon says session not found, return warning (not error)."""
        mock_ipc = AsyncMock(return_value=False)
        local_tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                _ipc_stop_session=mock_ipc,
            )
        }
        result = await local_tools["session_stop"].handler({"session_id": "child-2222"})
        assert not result.get("is_error")
        assert "not found in the daemon" in result["content"][0]["text"]


class TestSensitiveFields:
    def test_sensitive_fields_pinned(self):
        assert {"pid", "error_message", "authenticated_user_id"} == _SENSITIVE_FIELDS


class TestListChildren:
    async def test_returns_children(self, populated_registry):
        children = await populated_registry.list_children("parent-1111")
        assert len(children) == 1
        assert children[0]["session_id"] == "child-2222"

    async def test_no_children(self, populated_registry):
        children = await populated_registry.list_children("other-3333")
        assert children == []

    async def test_limit_respected(self, populated_registry):
        children = await populated_registry.list_children("parent-1111", limit=0)
        assert children == []


class TestMCPServerCreation:
    def test_returns_valid_config(self, populated_registry):
        config = create_summon_cli_mcp_server(populated_registry, "sid", "uid", "cid", "/tmp")
        assert config["name"] == "summon-cli"
        assert config["type"] == "sdk"

    def test_tool_count(self, populated_registry):
        tools = create_summon_cli_mcp_tools(populated_registry, "sid", "uid", "cid", "/tmp")
        assert len(tools) == 5
