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
        session_id="triage-5555",
        pid=os.getpid(),
        cwd="/home/user/proj",
        name="gh-triage",
        parent_session_id="parent-1111",
        authenticated_user_id="U_OWNER",
    )
    await registry.update_status(
        "triage-5555",
        "active",
        slack_channel_id="C500",
        slack_channel_name="summon-gh-triage",
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

    async def test_gh_triage_auto_detection(self, populated_registry, tmp_path):
        """QA-002: session_start with name='gh-triage' auto-applies triage instructions."""
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
        mock_ipc = AsyncMock(return_value="triage-session-id")

        from summon_claude.config import SummonConfig

        config = SummonConfig(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            slack_signing_secret="abc123",
            github_triage_stale_pr_hours=48,
        )

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
                config=config,
            )
        }

        result = await local_tools["session_start"].handler({"name": "gh-triage"})
        assert not result.get("is_error"), result

        # Verify the SessionOptions passed to IPC has system_prompt_append set
        call_args = mock_ipc.call_args
        options = call_args[0][0]  # first positional arg
        assert options.system_prompt_append is not None
        assert "GitHub triage agent" in options.system_prompt_append
        assert "48" in options.system_prompt_append  # stale_pr_hours from config
        assert options.extra_disallowed_tools is not None
        assert "Bash" in options.extra_disallowed_tools

    async def test_jira_triage_auto_detection(self, populated_registry, tmp_path):
        """QA-002: session_start with name='jira-triage' auto-applies Jira instructions."""
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
        mock_ipc = AsyncMock(return_value="jira-triage-id")

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
                triage_jira_cloud_id="cloud-abc-123",
            )
        }

        result = await local_tools["session_start"].handler({"name": "jira-triage"})
        assert not result.get("is_error"), result

        call_args = mock_ipc.call_args
        options = call_args[0][0]
        assert options.system_prompt_append is not None
        assert "Jira triage agent" in options.system_prompt_append
        assert "cloud-abc-123" in options.system_prompt_append
        assert options.extra_disallowed_tools is not None

    async def test_jira_triage_none_cloud_id(self, populated_registry, tmp_path):
        """jira-triage with triage_jira_cloud_id=None passes empty cloudId to template."""
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
        mock_ipc = AsyncMock(return_value="jira-triage-no-cloud")

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
                triage_jira_cloud_id=None,
            )
        }

        result = await local_tools["session_start"].handler({"name": "jira-triage"})
        assert not result.get("is_error"), result

        call_args = mock_ipc.call_args
        options = call_args[0][0]
        assert options.system_prompt_append is not None
        assert "Jira triage agent" in options.system_prompt_append
        # cloudId placeholder replaced with empty string (not "None")
        assert "None" not in options.system_prompt_append
        assert options.extra_disallowed_tools is not None

    async def test_jira_triage_uses_project_jql(self, populated_registry, tmp_path):
        """jira-triage auto-detection pulls JQL from parent session's project."""
        from summon_claude.sessions.auth import SpawnAuth

        # Register a project with jira_jql set
        project_id = await populated_registry.add_project("jira-proj", str(tmp_path))
        await populated_registry.update_project(project_id, jira_jql="priority = High")

        # Set parent-1111 as a PM session belonging to that project
        await populated_registry.update_status(
            "parent-1111",
            "active",
            project_id=project_id,
        )

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
        mock_ipc = AsyncMock(return_value="jira-triage-proj-id")

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
                triage_jira_cloud_id="cloud-abc-123",
            )
        }

        result = await local_tools["session_start"].handler({"name": "jira-triage"})
        assert not result.get("is_error"), result

        call_args = mock_ipc.call_args
        options = call_args[0][0]
        assert options.system_prompt_append is not None
        assert "priority = High" in options.system_prompt_append

    async def test_triage_auto_detection_overrides_explicit_prompt(
        self, populated_registry, tmp_path
    ):
        """Triage auto-detection takes precedence over PM-provided system_prompt."""
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
        mock_ipc = AsyncMock(return_value="override-id")

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

        result = await local_tools["session_start"].handler(
            {"name": "gh-triage", "system_prompt": "Custom instructions here"}
        )
        assert not result.get("is_error"), result

        call_args = mock_ipc.call_args
        options = call_args[0][0]
        # Auto-detected triage instructions should override the explicit prompt
        assert "GitHub triage agent" in options.system_prompt_append
        assert "Custom instructions here" not in options.system_prompt_append

    async def test_non_triage_name_no_auto_detection(self, populated_registry, tmp_path):
        """Normal session names must NOT trigger triage auto-detection."""
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
        mock_ipc = AsyncMock(return_value="normal-id")

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

        result = await local_tools["session_start"].handler({"name": "my-task"})
        assert not result.get("is_error"), result

        call_args = mock_ipc.call_args
        options = call_args[0][0]
        assert options.system_prompt_append is None
        assert options.extra_disallowed_tools is None

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
        mock_reg.count_active_children = AsyncMock(side_effect=RuntimeError("DB locked"))

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
        original_count_active = populated_registry.count_active_children
        original_get_session = populated_registry.get_session

        active_children = [
            {"status": "active", "session_id": f"c{i}", "session_name": f"s{i}"}
            for i in range(MAX_SPAWN_CHILDREN_PM)
        ]

        async def patched_count_active(sid):
            return MAX_SPAWN_CHILDREN_PM

        async def patched_list_children(sid, limit=50):
            return active_children

        async def patched_get_session(sid):
            return {"session_id": sid, "project_id": "proj-test"}

        populated_registry.count_active_children = patched_count_active
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
            populated_registry.count_active_children = original_count_active
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
        assert len(children) == 2
        child_ids = {c["session_id"] for c in children}
        assert "child-2222" in child_ids
        assert "triage-5555" in child_ids

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


class TestSessionClear:
    """Tests for the session_clear MCP tool."""

    @pytest.fixture
    def clear_tools(self, populated_registry: SessionRegistry) -> tuple:
        mock_clear = AsyncMock(
            return_value={"type": "session_cleared", "session_id": "triage-5555"},
        )
        tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                scheduler=make_scheduler(),
                is_pm=True,
                _ipc_clear_session=mock_clear,
            )
        }
        return tools, mock_clear

    async def test_clears_triage_child(self, clear_tools):
        tools, mock_clear = clear_tools
        result = await tools["session_clear"].handler({"session_id": "triage-5555"})
        assert not result.get("is_error")
        assert "Context cleared" in result["content"][0]["text"]
        mock_clear.assert_awaited_once_with("triage-5555")

    async def test_rejects_missing_id(self, clear_tools):
        tools, mock_clear = clear_tools
        result = await tools["session_clear"].handler({})
        assert result.get("is_error")
        mock_clear.assert_not_awaited()

    async def test_rejects_self(self, clear_tools):
        tools, _ = clear_tools
        result = await tools["session_clear"].handler({"session_id": "parent-1111"})
        assert result.get("is_error")
        assert "cannot clear your own session" in result["content"][0]["text"]

    async def test_rejects_non_child(self, clear_tools):
        tools, mock_clear = clear_tools
        result = await tools["session_clear"].handler({"session_id": "done-4444"})
        assert result.get("is_error")
        assert "can only clear sessions you spawned" in result["content"][0]["text"]
        mock_clear.assert_not_awaited()

    async def test_other_user_session_returns_not_found(self, clear_tools):
        """Other user's session returns 'not found' — no existence leak."""
        tools, mock_clear = clear_tools
        result = await tools["session_clear"].handler({"session_id": "other-3333"})
        assert result.get("is_error")
        assert "not found" in result["content"][0]["text"]
        mock_clear.assert_not_awaited()

    async def test_rejects_inactive(self, populated_registry, clear_tools):
        await populated_registry.update_status("triage-5555", "completed")
        tools, mock_clear = clear_tools
        result = await tools["session_clear"].handler({"session_id": "triage-5555"})
        assert result.get("is_error")
        assert "Can only clear active sessions" in result["content"][0]["text"]

    async def test_returns_error_when_ipc_raises(self, populated_registry):
        """Exception raised by _ipc_clear_session is caught and returned as error."""
        mock_clear = AsyncMock(side_effect=RuntimeError("connection refused"))
        tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                scheduler=make_scheduler(),
                is_pm=True,
                _ipc_clear_session=mock_clear,
            )
        }
        result = await tools["session_clear"].handler({"session_id": "triage-5555"})
        assert result.get("is_error")
        assert "connection refused" in result["content"][0]["text"]

    async def test_gpm_does_not_have_session_clear(self, populated_registry):
        """SEC: GPM must not have session_clear tool at all."""
        tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="gpm-9999",
                authenticated_user_id="U_OWNER",
                channel_id="C_GPM",
                cwd="/home/user/proj",
                scheduler=make_scheduler(),
                is_pm=True,
                is_global_pm=True,
            )
        }
        assert "session_clear" not in tools

    async def test_non_triage_session_clear_rejected(self, populated_registry):
        """SEC: PM cannot clear non-triage sessions."""
        mock_clear = AsyncMock(return_value={"type": "session_cleared", "session_id": "child-2222"})
        tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="parent-1111",  # parent of child-2222
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/home/user/proj",
                scheduler=make_scheduler(),
                is_pm=True,
                _ipc_clear_session=mock_clear,
            )
        }
        # child-2222 has name "child-session" — not a triage name
        result = await tools["session_clear"].handler({"session_id": "child-2222"})
        assert result.get("is_error")
        assert "triage sessions" in result["content"][0]["text"]
        mock_clear.assert_not_awaited()


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

    def test_gpm_tool_count(self, populated_registry):
        tools = create_summon_cli_mcp_tools(
            populated_registry,
            "gpm-sid",
            "uid",
            "cid",
            "/tmp",
            is_pm=True,
            is_global_pm=True,
            scheduler=make_scheduler(),
        )
        # 8 base + session_stop, session_log_status, session_resume, session_message
        # = 4 PM (no session_start, no session_clear for GPM)
        # + get_workflow_instructions = 1 GPM-only
        # session_status_update excluded (no pm_status_ts)
        assert len(tools) == 13

    def test_pm_tool_count(self, populated_registry):
        tools = create_summon_cli_mcp_tools(
            populated_registry,
            "pm-sid",
            "uid",
            "cid",
            "/tmp",
            is_pm=True,
            is_global_pm=False,
            scheduler=make_scheduler(),
        )
        # 8 base + session_start, session_stop, session_log_status,
        # session_resume, session_message, session_clear = 6 PM tools
        # session_status_update excluded (no pm_status_ts / no _web_client)
        assert len(tools) == 14


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


class TestGetWorkflowInstructions:
    """Tests for the get_workflow_instructions MCP tool (GPM-only)."""

    @staticmethod
    def _make_gpm_tools(registry) -> dict:
        """Build a GPM tools dict with standard test parameters."""
        return {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=registry,
                session_id="gpm-test",
                authenticated_user_id="U_OWNER",
                channel_id="C_GPM",
                cwd="/tmp",
                is_pm=True,
                is_global_pm=True,
                scheduler=make_scheduler(),
            )
        }

    @pytest.fixture
    async def gpm_tools_with_workflows(self, populated_registry):
        """Create GPM tools with workflow data pre-configured."""
        project_id = await populated_registry.add_project("test-proj", "/tmp/test-proj")
        await populated_registry.set_workflow_defaults("Always run tests.")
        await populated_registry.set_project_workflow(project_id, "Use TDD for this project.")

        tools = self._make_gpm_tools(populated_registry)
        return tools, project_id

    async def test_global_defaults(self, gpm_tools_with_workflows):
        tools, _ = gpm_tools_with_workflows
        result = await tools["get_workflow_instructions"].handler({})
        assert not result.get("is_error")
        text = result["content"][0]["text"]
        assert "global default" in text.lower()
        assert "Always run tests." in text

    async def test_project_override(self, gpm_tools_with_workflows):
        tools, _ = gpm_tools_with_workflows
        result = await tools["get_workflow_instructions"].handler({"project": "test-proj"})
        assert not result.get("is_error")
        text = result["content"][0]["text"]
        assert "project-specific" in text.lower()
        assert "Use TDD for this project." in text
        # Global defaults must NOT bleed through when project has its own instructions
        assert "Always run tests." not in text

    async def test_project_fallback_to_global(self, populated_registry):
        """Project with no override falls back to global."""
        await populated_registry.add_project("fallback-proj", "/tmp/fb-proj")
        await populated_registry.set_workflow_defaults("Global rules.")
        tools = self._make_gpm_tools(populated_registry)
        result = await tools["get_workflow_instructions"].handler({"project": "fallback-proj"})
        assert not result.get("is_error")
        text = result["content"][0]["text"]
        assert "global default" in text.lower()
        assert "Global rules." in text

    async def test_project_not_found(self, gpm_tools_with_workflows):
        tools, _ = gpm_tools_with_workflows
        result = await tools["get_workflow_instructions"].handler({"project": "nonexistent"})
        assert result.get("is_error") is True
        assert "not found" in result["content"][0]["text"]

    async def test_none_configured(self, populated_registry):
        """Returns 'no workflow instructions' when nothing is set."""
        tools = self._make_gpm_tools(populated_registry)
        result = await tools["get_workflow_instructions"].handler({})
        assert not result.get("is_error")
        assert "No workflow instructions configured." in result["content"][0]["text"]

    async def test_not_available_to_regular_pm(self, populated_registry):
        """Tool is NOT registered for regular PM sessions."""
        tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="pm-test",
                authenticated_user_id="U_OWNER",
                channel_id="C_PM",
                cwd="/tmp",
                is_pm=True,
                is_global_pm=False,
                scheduler=make_scheduler(),
            )
        }
        assert "get_workflow_instructions" not in tools

    async def test_not_available_to_regular_session(self, populated_registry):
        """Tool is NOT registered for regular (non-PM) sessions."""
        tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=populated_registry,
                session_id="regular-test",
                authenticated_user_id="U_OWNER",
                channel_id="C_REG",
                cwd="/tmp",
                is_pm=False,
                is_global_pm=False,
                scheduler=make_scheduler(),
            )
        }
        assert "get_workflow_instructions" not in tools

    async def test_include_global_token_expansion(self, populated_registry):
        """$INCLUDE_GLOBAL token in project instructions is expanded."""
        project_id = await populated_registry.add_project("token-proj", "/tmp/token-proj")
        await populated_registry.set_workflow_defaults("Global rules.")
        await populated_registry.set_project_workflow(
            project_id, "Before.\n$INCLUDE_GLOBAL\nAfter."
        )
        tools = self._make_gpm_tools(populated_registry)
        result = await tools["get_workflow_instructions"].handler({"project": "token-proj"})
        assert not result.get("is_error")
        text = result["content"][0]["text"]
        assert "Global rules." in text
        assert "Before." in text
        assert "After." in text
        assert "$INCLUDE_GLOBAL" not in text
        assert "project-specific" in text.lower()

    async def test_registry_error_returns_error(self, populated_registry):
        """Registry exception is caught and returned as error."""
        mock_registry = AsyncMock()
        mock_registry.get_workflow_defaults = AsyncMock(
            side_effect=RuntimeError("DB connection lost")
        )
        tools = self._make_gpm_tools(mock_registry)
        result = await tools["get_workflow_instructions"].handler({})
        assert result.get("is_error") is True
        text = result["content"][0]["text"]
        assert "Error retrieving workflow instructions:" in text
        assert "DB connection lost" in text

    async def test_get_workflow_instructions_strips_markdown_images(self, populated_registry):
        """Workflow instructions must strip markdown images via validate_agent_output."""
        await populated_registry.set_workflow_defaults(
            "Rules: ![stolen](https://evil.com/steal) must not leak."
        )
        tools = self._make_gpm_tools(populated_registry)
        result = await tools["get_workflow_instructions"].handler({})
        assert not result.get("is_error")
        text = result["content"][0]["text"]
        assert "![stolen]" not in text
        assert "[image removed by security filter]" in text

    async def test_get_project_error_returns_error(self, populated_registry):
        """get_project() raising is caught and returned as error."""
        mock_registry = AsyncMock()
        mock_registry.get_project = AsyncMock(side_effect=RuntimeError("DB gone"))
        tools = self._make_gpm_tools(mock_registry)
        result = await tools["get_workflow_instructions"].handler({"project": "some-proj"})
        assert result.get("is_error") is True
        text = result["content"][0]["text"]
        assert "Error retrieving workflow instructions:" in text
        assert "DB gone" in text

    async def test_get_effective_workflow_error_returns_error(self, populated_registry):
        """get_effective_workflow() raising is caught and returned as error."""
        mock_registry = AsyncMock()
        mock_registry.get_project = AsyncMock(
            return_value={"project_id": "proj-uuid-123", "workflow_instructions": "some text"}
        )
        mock_registry.get_effective_workflow = AsyncMock(
            side_effect=RuntimeError("Workflow DB gone")
        )
        tools = self._make_gpm_tools(mock_registry)
        result = await tools["get_workflow_instructions"].handler({"project": "some-proj"})
        assert result.get("is_error") is True
        text = result["content"][0]["text"]
        assert "Error retrieving workflow instructions:" in text
        assert "Workflow DB gone" in text

    async def test_project_lookup_by_uuid(self, populated_registry):
        """Handler accepts project_id (UUID) in addition to project name."""
        project_id = await populated_registry.add_project("uuid-proj", "/tmp/uuid-proj")
        await populated_registry.set_project_workflow(project_id, "UUID-based lookup works.")
        tools = self._make_gpm_tools(populated_registry)
        result = await tools["get_workflow_instructions"].handler({"project": project_id})
        assert not result.get("is_error")
        text = result["content"][0]["text"]
        assert "UUID-based lookup works." in text
        assert "project-specific" in text.lower()

    async def test_whitespace_project_uses_global_defaults(self, populated_registry):
        """Whitespace-only project string is normalized to None, returning global defaults."""
        await populated_registry.set_workflow_defaults("Global WS rule.")
        tools = self._make_gpm_tools(populated_registry)
        for value in ("   ", ""):
            result = await tools["get_workflow_instructions"].handler({"project": value})
            assert not result.get("is_error"), f"Expected success for project={value!r}"
            text = result["content"][0]["text"]
            assert "global default" in text.lower()
            assert "Global WS rule." in text

    async def test_source_prefix_format(self, gpm_tools_with_workflows):
        """Response text starts with [Source: ...] prefix (exact format)."""
        tools, _ = gpm_tools_with_workflows
        result = await tools["get_workflow_instructions"].handler({})
        text = result["content"][0]["text"]
        assert text.startswith("[Source: global default]\n\n")

        result = await tools["get_workflow_instructions"].handler({"project": "test-proj"})
        text = result["content"][0]["text"]
        assert text.startswith("[Source: project-specific]\n\n")


def test_is_global_pm_requires_is_pm(populated_registry):
    """is_global_pm=True with is_pm=False must raise ValueError."""
    with pytest.raises(ValueError, match="is_global_pm requires is_pm=True"):
        create_summon_cli_mcp_tools(
            registry=populated_registry,
            session_id="gpm-test",
            authenticated_user_id="U_OWNER",
            channel_id="C_GPM",
            cwd="/tmp",
            is_pm=False,
            is_global_pm=True,
            scheduler=make_scheduler(),
        )
