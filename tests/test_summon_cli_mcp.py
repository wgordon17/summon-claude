"""Tests for summon_claude.summon_cli_mcp — session lifecycle MCP tools."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_scheduler

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
            scheduler=make_scheduler(),
            is_pm=True,
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
                scheduler=make_scheduler(),
                is_pm=True,
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
                scheduler=make_scheduler(),
                is_pm=True,
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
                scheduler=make_scheduler(),
                is_pm=True,
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
                scheduler=make_scheduler(),
                is_pm=True,
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
                scheduler=make_scheduler(),
                is_pm=True,
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
                scheduler=make_scheduler(),
                is_pm=True,
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
                scheduler=make_scheduler(),
                is_pm=True,
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
                scheduler=make_scheduler(),
                is_pm=True,
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
                scheduler=make_scheduler(),
                is_pm=True,
            )
        }

        result = await local_tools["session_start"].handler({"name": "should-fail"})
        assert result["is_error"] is True
        assert "spawn depth" in result["content"][0]["text"]

    def test_schema_includes_system_prompt(self, tools):
        """session_start tool schema has optional system_prompt property."""
        schema = tools["session_start"].input_schema
        assert "system_prompt" in schema["properties"]
        assert "system_prompt" not in schema.get("required", [])

    def test_schema_system_prompt_max_length(self, tools):
        """Guard test: maxLength matches MAX_PROMPT_CHARS constant."""
        from summon_claude.summon_cli_mcp import MAX_PROMPT_CHARS

        schema = tools["session_start"].input_schema
        assert schema["properties"]["system_prompt"]["maxLength"] == MAX_PROMPT_CHARS

    async def test_rejects_oversized_system_prompt(self, tools):
        """system_prompt exceeding _MAX_SYSTEM_PROMPT_CHARS returns error."""
        result = await tools["session_start"].handler(
            {"name": "test-rv", "system_prompt": "x" * 10_001}
        )
        assert result["is_error"] is True
        assert "exceeds" in result["content"][0]["text"]

    async def test_passes_system_prompt_to_options(self, populated_registry, tmp_path):
        """system_prompt arg flows through to SessionOptions.system_prompt_append."""
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
        captured_options = []

        async def capturing_ipc(options, token):
            captured_options.append(options)
            return "new-session-id"

        local_tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd=str(tmp_path),
                scheduler=make_scheduler(),
                is_pm=True,
                _generate_spawn_token=mock_spawn,
                _ipc_create_session=capturing_ipc,
            )
        }

        result = await local_tools["session_start"].handler(
            {
                "name": "test-review",
                "system_prompt": "Review PR #42 thoroughly",
            }
        )
        assert not result.get("is_error"), result
        assert len(captured_options) == 1
        assert captured_options[0].system_prompt_append == "Review PR #42 thoroughly"

    async def test_passes_initial_prompt_to_options(self, populated_registry, tmp_path):
        """initial_prompt arg flows through to SessionOptions.initial_prompt."""
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
        captured_options = []

        async def capturing_ipc(options, token):
            captured_options.append(options)
            return "new-session-id"

        local_tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd=str(tmp_path),
                scheduler=make_scheduler(),
                is_pm=True,
                _generate_spawn_token=mock_spawn,
                _ipc_create_session=capturing_ipc,
            )
        }

        result = await local_tools["session_start"].handler(
            {
                "name": "test-task",
                "initial_prompt": "Build the auth module",
            }
        )
        assert not result.get("is_error"), result
        assert len(captured_options) == 1
        assert captured_options[0].initial_prompt == "Build the auth module"

    async def test_pm_at_cap_without_queue_returns_hard_error(self, populated_registry, tmp_path):
        """PM at cap with _ipc_queue_session=None falls through to hard error."""
        original_list_children = populated_registry.list_children
        original_get_session = populated_registry.get_session

        active_children = [
            {"status": "active", "session_id": f"c{i}", "session_name": f"s{i}"}
            for i in range(MAX_SPAWN_CHILDREN_PM)
        ]

        async def patched_list_children(sid, limit=500):
            return active_children

        async def patched_get_session(sid):
            return {"session_id": sid, "project_id": "proj-test"}

        populated_registry.list_children = patched_list_children
        populated_registry.get_session = patched_get_session

        try:
            local_tools = {
                t.name: t
                for t in create_summon_cli_mcp_tools(
                    registry=populated_registry,
                    session_id="parent-1111",
                    authenticated_user_id="U_OWNER",
                    channel_id="C100",
                    cwd=str(tmp_path),
                    scheduler=make_scheduler(),
                    is_pm=True,
                    # _ipc_queue_session is NOT passed (defaults to None)
                )
            }

            result = await local_tools["session_start"].handler({"name": "blocked-task"})
        finally:
            populated_registry.list_children = original_list_children
            populated_registry.get_session = original_get_session

        assert result.get("is_error") is True
        assert "active session limit reached" in result["content"][0]["text"]


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
                scheduler=make_scheduler(),
                is_pm=True,
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
                scheduler=make_scheduler(),
                is_pm=True,
                _ipc_stop_session=mock_ipc,
            )
        }
        result = await local_tools["session_stop"].handler({"session_id": "child-2222"})
        assert not result.get("is_error")
        assert "not found in the daemon" in result["content"][0]["text"]


class TestSensitiveFields:
    def test_sensitive_fields_pinned(self):
        assert {"pid", "error_message", "authenticated_user_id"} == _SENSITIVE_FIELDS


class TestGuardConstants:
    def test_max_tasks_per_session_pinned(self):
        from summon_claude.summon_cli_mcp import _MAX_TASKS_PER_SESSION

        assert _MAX_TASKS_PER_SESSION == 100

    def test_max_cross_session_ids_pinned(self):
        from summon_claude.summon_cli_mcp import _MAX_CROSS_SESSION_IDS

        assert _MAX_CROSS_SESSION_IDS == 20


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


class TestSessionMessage:
    """Tests for the session_message MCP tool."""

    @pytest.fixture
    def msg_tools(self, populated_registry: SessionRegistry) -> dict:
        mock_send = AsyncMock(
            return_value={"type": "message_sent", "session_id": "child-2222", "channel_id": "C200"}
        )
        mock_web = AsyncMock()
        return (
            {
                t.name: t
                for t in create_summon_cli_mcp_tools(
                    registry=populated_registry,
                    session_id="parent-1111",
                    authenticated_user_id="U_OWNER",
                    channel_id="C100",
                    cwd="/home/user/proj",
                    session_name="parent-session",
                    scheduler=make_scheduler(),
                    is_pm=True,
                    _ipc_send_message=mock_send,
                    _web_client=mock_web,
                )
            },
            mock_send,
            mock_web,
        )

    async def test_sends_to_child(self, msg_tools):
        tools, mock_send, _mock_web = msg_tools
        result = await tools["session_message"].handler(
            {"session_id": "child-2222", "text": "hello child"}
        )
        assert not result.get("is_error")
        assert "Message sent" in result["content"][0]["text"]
        mock_send.assert_awaited_once()

    async def test_rejects_non_child(self, msg_tools):
        tools, mock_send, _ = msg_tools
        result = await tools["session_message"].handler(
            {"session_id": "done-4444", "text": "hello"}
        )
        assert result.get("is_error")
        assert "can only message sessions you spawned" in result["content"][0]["text"]
        mock_send.assert_not_awaited()

    async def test_rejects_self(self, msg_tools):
        tools, mock_send, _ = msg_tools
        result = await tools["session_message"].handler(
            {"session_id": "parent-1111", "text": "hello me"}
        )
        assert result.get("is_error")
        assert "cannot send a message to your own session" in result["content"][0]["text"]

    async def test_rejects_inactive_target(self, populated_registry, msg_tools):
        # Make child inactive
        await populated_registry.update_status("child-2222", "completed")
        tools, mock_send, _ = msg_tools
        result = await tools["session_message"].handler(
            {"session_id": "child-2222", "text": "hello"}
        )
        assert result.get("is_error")
        assert "Can only message active sessions" in result["content"][0]["text"]

    async def test_posts_to_slack_channel(self, msg_tools):
        tools, _mock_send, mock_web = msg_tools
        await tools["session_message"].handler({"session_id": "child-2222", "text": "hello child"})
        mock_web.chat_postMessage.assert_awaited_once()
        call_kwargs = mock_web.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C200"
        assert "hello child" in call_kwargs["text"]

    async def test_slack_post_sanitizes_mentions(self, msg_tools):
        tools, _mock_send, mock_web = msg_tools
        text = "Alert <!channel> and <!here> and <!everyone> and <@U123ABC> and <!subteam^S456>"
        await tools["session_message"].handler({"session_id": "child-2222", "text": text})
        posted = mock_web.chat_postMessage.call_args[1]["text"]
        assert "<!channel>" not in posted
        assert "<!here>" not in posted
        assert "<!everyone>" not in posted
        assert "<@U123ABC>" not in posted
        assert "<!subteam^S456>" not in posted
        assert "channel" in posted
        assert "user:U123ABC" in posted

    async def test_slack_post_sanitizes_sender_info(self, populated_registry):
        """Mentions and secrets in session_name are sanitized in the attribution line."""
        mock_send = AsyncMock(
            return_value={"type": "message_sent", "session_id": "child-2222", "channel_id": "C200"}
        )
        mock_web = AsyncMock()
        tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                session_name="<!channel> evil <@U999>",
                scheduler=make_scheduler(),
                is_pm=True,
                _ipc_send_message=mock_send,
                _web_client=mock_web,
            )
        }
        await tools["session_message"].handler({"session_id": "child-2222", "text": "hello"})
        posted = mock_web.chat_postMessage.call_args[1]["text"]
        assert "<!channel>" not in posted
        assert "<@U999>" not in posted

    async def test_slack_post_strips_markdown_images(self, msg_tools):
        """Observability post must strip markdown images via validate_agent_output."""
        tools, _mock_send, mock_web = msg_tools
        await tools["session_message"].handler(
            {"session_id": "child-2222", "text": "![stolen](https://evil.com/steal)"}
        )
        posted = mock_web.chat_postMessage.call_args[1]["text"]
        assert "![stolen]" not in posted
        assert "[image removed by security filter]" in posted

    async def test_slack_post_failure_non_fatal(self, msg_tools):
        tools, mock_send, mock_web = msg_tools
        mock_web.chat_postMessage.side_effect = Exception("Slack down")
        result = await tools["session_message"].handler(
            {"session_id": "child-2222", "text": "hello child"}
        )
        # Should still succeed (message was injected)
        assert not result.get("is_error")
        assert "Message sent" in result["content"][0]["text"]

    async def test_text_truncated(self, msg_tools):
        tools, mock_send, _ = msg_tools
        long_text = "x" * 20_000
        await tools["session_message"].handler({"session_id": "child-2222", "text": long_text})
        call_kwargs = mock_send.call_args[1]
        assert len(call_kwargs["text"]) == 10_000

    async def test_missing_session_id(self, msg_tools):
        tools, _, _ = msg_tools
        result = await tools["session_message"].handler({"text": "hello"})
        assert result.get("is_error")

    async def test_missing_text(self, msg_tools):
        tools, _, _ = msg_tools
        result = await tools["session_message"].handler({"session_id": "child-2222"})
        assert result.get("is_error")


class TestSessionResume:
    """Tests for the session_resume MCP tool."""

    @pytest.fixture
    async def resume_registry(self, registry: SessionRegistry) -> SessionRegistry:
        """Registry with parent + completed child for resume testing."""
        await registry.register(
            session_id="pm-1111",
            pid=os.getpid(),
            cwd="/home/user/proj",
            name="pm-session",
            authenticated_user_id="U_OWNER",
        )
        await registry.update_status(
            "pm-1111",
            "active",
            slack_channel_id="C100",
            authenticated_user_id="U_OWNER",
        )

        await registry.register(
            session_id="child-done",
            pid=os.getpid(),
            cwd="/home/user/proj",
            name="child-done",
            parent_session_id="pm-1111",
            authenticated_user_id="U_OWNER",
        )
        await registry.update_status(
            "child-done",
            "completed",
            slack_channel_id="C200",
            slack_channel_name="summon-child",
            authenticated_user_id="U_OWNER",
            claude_session_id="claude-xyz",
            ended_at="2026-03-18T00:00:00+00:00",
        )
        return registry

    async def test_resumes_child(self, resume_registry):
        mock_resume = AsyncMock(
            return_value={"type": "session_resumed", "session_id": "new-id", "channel_id": "C200"}
        )
        tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=resume_registry,
                session_id="pm-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                scheduler=make_scheduler(),
                is_pm=True,
                _ipc_resume_session=mock_resume,
            )
        }
        result = await tools["session_resume"].handler({"session_id": "child-done"})
        assert not result.get("is_error")
        assert "resumed" in result["content"][0]["text"]
        mock_resume.assert_awaited_once()

    async def test_rejects_active_session(self, resume_registry):
        mock_resume = AsyncMock()
        tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=resume_registry,
                session_id="pm-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                scheduler=make_scheduler(),
                is_pm=True,
                _ipc_resume_session=mock_resume,
            )
        }
        result = await tools["session_resume"].handler({"session_id": "pm-1111"})
        assert result.get("is_error")
        mock_resume.assert_not_awaited()

    async def test_rejects_non_child(self, resume_registry):
        # Register a non-child completed session
        await resume_registry.register(
            session_id="other-done",
            pid=os.getpid(),
            cwd="/tmp",
            name="other",
            authenticated_user_id="U_OWNER",
        )
        await resume_registry.update_status("other-done", "completed")

        mock_resume = AsyncMock()
        tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=resume_registry,
                session_id="pm-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                scheduler=make_scheduler(),
                is_pm=True,
                _ipc_resume_session=mock_resume,
            )
        }
        result = await tools["session_resume"].handler({"session_id": "other-done"})
        assert result.get("is_error")
        assert "can only resume sessions you spawned" in result["content"][0]["text"]

    async def test_missing_session_id(self, resume_registry):
        tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=resume_registry,
                session_id="pm-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                scheduler=make_scheduler(),
                is_pm=True,
            )
        }
        result = await tools["session_resume"].handler({})
        assert result.get("is_error")


class TestMCPServerCreation:
    def test_returns_valid_config(self, populated_registry):
        config = create_summon_cli_mcp_server(
            populated_registry, "sid", "uid", "cid", "/tmp", scheduler=make_scheduler()
        )
        assert config["name"] == "summon-cli"
        assert config["type"] == "sdk"

    def test_tool_count(self, populated_registry):
        tools = create_summon_cli_mcp_tools(
            populated_registry, "sid", "uid", "cid", "/tmp", scheduler=make_scheduler()
        )
        # session_list, session_info, cron x3, task x3 = 8 (no PM tools)
        assert len(tools) == 8


class TestSessionStatusUpdate:
    """Tests for the session_status_update MCP tool."""

    def _make_tools(self, registry, *, pm_status_ts="1234567890.123456", mock_web_client=None):
        if mock_web_client is None:
            mock_web_client = AsyncMock()
            mock_web_client.chat_update = AsyncMock(return_value={"ok": True})
        return {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                scheduler=make_scheduler(),
                is_pm=True,
                pm_status_ts=pm_status_ts,
                _web_client=mock_web_client,
            )
        }

    async def test_session_status_update_updates_pinned_message(self, populated_registry):
        mock_web_client = AsyncMock()
        mock_web_client.chat_update = AsyncMock(return_value={"ok": True})
        tools = self._make_tools(populated_registry, mock_web_client=mock_web_client)

        result = await tools["session_status_update"].handler({"summary": "All tasks running"})

        assert not result.get("is_error")
        mock_web_client.chat_update.assert_awaited_once()
        call_kwargs = mock_web_client.chat_update.call_args.kwargs
        assert call_kwargs["channel"] == "C100"
        assert call_kwargs["ts"] == "1234567890.123456"
        assert "All tasks running" in call_kwargs["text"]

    async def test_session_status_update_requires_summary(self, populated_registry):
        tools = self._make_tools(populated_registry)
        result = await tools["session_status_update"].handler({"summary": ""})
        assert result.get("is_error") is True
        assert "summary" in result["content"][0]["text"].lower()

    async def test_session_status_update_requires_summary_whitespace(self, populated_registry):
        tools = self._make_tools(populated_registry)
        result = await tools["session_status_update"].handler({"summary": "   "})
        assert result.get("is_error") is True

    async def test_session_status_update_truncates_long_input(self, populated_registry):
        mock_web_client = AsyncMock()
        mock_web_client.chat_update = AsyncMock(return_value={"ok": True})
        tools = self._make_tools(populated_registry, mock_web_client=mock_web_client)

        long_summary = "x" * 600
        result = await tools["session_status_update"].handler({"summary": long_summary})

        assert not result.get("is_error")
        call_kwargs = mock_web_client.chat_update.call_args.kwargs
        # The text in the message should contain only up to 500 chars of the summary
        # but we verify via the response text which echoes up to 100 chars
        assert "x" * 100 in result["content"][0]["text"]
        # The full 600-char summary must not appear verbatim in the posted text
        assert "x" * 501 not in call_kwargs["text"]

    async def test_session_status_update_strips_markdown_images(self, populated_registry):
        """Status update must strip markdown images via validate_agent_output."""
        mock_web_client = AsyncMock()
        mock_web_client.chat_update = AsyncMock(return_value={"ok": True})
        tools = self._make_tools(populated_registry, mock_web_client=mock_web_client)

        await tools["session_status_update"].handler(
            {"summary": "Status: ![img](https://evil.com/steal)"}
        )

        call_kwargs = mock_web_client.chat_update.call_args.kwargs
        assert "![img]" not in call_kwargs["text"]
        assert "[image removed by security filter]" in call_kwargs["text"]

    async def test_session_status_update_pm_only(self, populated_registry):
        """session_status_update must appear in PM tool list."""
        pm_tools = {
            t.name
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                scheduler=make_scheduler(),
                is_pm=True,
                pm_status_ts="1234567890.123456",
                _web_client=AsyncMock(),
            )
        }
        non_pm_tools = {
            t.name
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                scheduler=make_scheduler(),
                is_pm=False,
            )
        }
        assert "session_status_update" in pm_tools
        assert "session_status_update" not in non_pm_tools

    async def test_session_status_update_absent_without_status_ts(self, populated_registry):
        """When pm_status_ts is None, session_status_update must not be registered."""
        tools = {
            t.name
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                scheduler=make_scheduler(),
                is_pm=True,
                pm_status_ts=None,
                _web_client=AsyncMock(),
            )
        }
        assert "session_status_update" not in tools

    async def test_session_status_update_absent_without_web_client(self, populated_registry):
        """When _web_client is None, session_status_update must not be registered."""
        tools = {
            t.name
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                scheduler=make_scheduler(),
                is_pm=True,
                pm_status_ts="1234567890.123456",
                _web_client=None,
            )
        }
        assert "session_status_update" not in tools

    async def test_session_status_update_logs_audit_event(self, populated_registry):
        """session_status_update logs a pm_status_update audit event."""
        mock_web_client = AsyncMock()
        mock_web_client.chat_update = AsyncMock(return_value={"ok": True})
        # Patch log_event to verify it's called (real method needs audit_events table)
        with patch.object(populated_registry, "log_event", new_callable=AsyncMock) as mock_log:
            tools = self._make_tools(populated_registry, mock_web_client=mock_web_client)
            await tools["session_status_update"].handler({"summary": "All clear"})
            mock_log.assert_awaited_once()
            assert mock_log.call_args.args[0] == "pm_pinned_status_update"
            assert mock_log.call_args.kwargs.get("user_id") == "U_OWNER"

    async def test_session_status_update_with_details(self, populated_registry):
        """session_status_update includes details in the posted message."""
        mock_web_client = AsyncMock()
        mock_web_client.chat_update = AsyncMock(return_value={"ok": True})
        tools = self._make_tools(populated_registry, mock_web_client=mock_web_client)

        result = await tools["session_status_update"].handler(
            {"summary": "2 active", "details": "- feat-auth: 10 turns\n- fix-login: 3 turns"}
        )
        assert not result.get("is_error")
        call_kwargs = mock_web_client.chat_update.call_args.kwargs
        assert "feat-auth: 10 turns" in call_kwargs["text"]
        assert "fix-login: 3 turns" in call_kwargs["text"]

    async def test_session_status_update_truncates_details(self, populated_registry):
        """details exceeding 2000 chars are truncated."""
        mock_web_client = AsyncMock()
        mock_web_client.chat_update = AsyncMock(return_value={"ok": True})
        tools = self._make_tools(populated_registry, mock_web_client=mock_web_client)

        long_details = "d" * 2500
        result = await tools["session_status_update"].handler(
            {"summary": "Status", "details": long_details}
        )
        assert not result.get("is_error")
        text = mock_web_client.chat_update.call_args.kwargs["text"]
        assert "d" * 2001 not in text

    async def test_session_status_update_sanitizes_mentions(self, populated_registry):
        """Slack mentions in summary and details are neutralized."""
        mock_web_client = AsyncMock()
        mock_web_client.chat_update = AsyncMock(return_value={"ok": True})
        tools = self._make_tools(populated_registry, mock_web_client=mock_web_client)

        result = await tools["session_status_update"].handler(
            {
                "summary": "<!channel> alert from <@U12345>",
                "details": "<!here> check <!subteam^S123|team>",
            }
        )
        assert not result.get("is_error")
        text = mock_web_client.chat_update.call_args.kwargs["text"]
        assert "<!channel>" not in text
        assert "<@U12345>" not in text
        assert "<!here>" not in text
        assert "<!subteam" not in text

    async def test_session_status_update_chat_update_failure(self, populated_registry):
        """session_status_update returns is_error when chat_update raises."""
        mock_web_client = AsyncMock()
        mock_web_client.chat_update = AsyncMock(side_effect=Exception("Slack down"))
        tools = self._make_tools(populated_registry, mock_web_client=mock_web_client)

        result = await tools["session_status_update"].handler({"summary": "Status update"})
        assert result.get("is_error") is True
        assert "Error updating status" in result["content"][0]["text"]
