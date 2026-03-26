"""Tests for summon_claude.permissions — now uses ThreadRouter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from helpers import make_mock_slack_client
from summon_claude.config import SummonConfig
from summon_claude.sessions.permissions import (
    _GITHUB_MCP_AUTO_APPROVE,
    _GITHUB_MCP_AUTO_APPROVE_PREFIXES,
    _GITHUB_MCP_REQUIRE_APPROVAL,
    PendingRequest,
    PermissionHandler,
    _format_request_summary,
)
from summon_claude.slack.router import ThreadRouter


def make_config(debounce_ms=10):
    """Build a minimal SummonConfig with fast debounce for tests."""
    return SummonConfig.model_validate(
        {
            "slack_bot_token": "xoxb-t",
            "slack_app_token": "xapp-t",
            "slack_signing_secret": "abcdef",
            "permission_debounce_ms": debounce_ms,
        }
    )


def make_handler(debounce_ms=10, authenticated_user_id="U_TEST"):
    """Create a PermissionHandler with a mocked ThreadRouter."""
    client = make_mock_slack_client()
    router = ThreadRouter(client)
    config = make_config(debounce_ms=debounce_ms)
    handler = PermissionHandler(router, config, authenticated_user_id=authenticated_user_id)
    return handler, client, router


def _ephemeral_auto_approve(handler):
    """Return a side effect for post_ephemeral that auto-approves all pending batches."""

    async def side_effect(*_args, **_kwargs):
        async def do():
            await asyncio.sleep(0.05)
            for batch_id in list(handler._batch.events.keys()):
                handler._batch.decisions[batch_id] = True
                handler._batch.events[batch_id].set()

        asyncio.create_task(do())

    return side_effect


def _ephemeral_auto_deny(handler):
    """Return a side effect for post_ephemeral that auto-denies all pending batches."""

    async def side_effect(*_args, **_kwargs):
        async def do():
            await asyncio.sleep(0.05)
            for batch_id in list(handler._batch.events.keys()):
                handler._batch.decisions[batch_id] = False
                handler._batch.events[batch_id].set()

        asyncio.create_task(do())

    return side_effect


class TestAutoApprove:
    async def test_read_is_auto_approved(self):
        handler, _, _ = make_handler()
        result = await handler.handle("Read", {"file_path": "/tmp/f"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_grep_is_auto_approved(self):
        handler, _, _ = make_handler()
        result = await handler.handle("Grep", {"pattern": "foo"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_glob_is_auto_approved(self):
        handler, _, _ = make_handler()
        result = await handler.handle("Glob", {"pattern": "**/*.py"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_web_search_is_auto_approved(self):
        handler, _, _ = make_handler()
        result = await handler.handle("WebSearch", {"query": "python"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_web_fetch_is_auto_approved(self):
        handler, _, _ = make_handler()
        result = await handler.handle("WebFetch", {"url": "https://example.com"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_cat_is_auto_approved(self):
        handler, _, _ = make_handler()
        result = await handler.handle("Cat", {}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_lsp_is_auto_approved(self):
        handler, _, _ = make_handler()
        result = await handler.handle("LSP", {}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_bash_is_not_auto_approved(self):
        handler, provider, _ = make_handler()
        provider.post_ephemeral = AsyncMock(side_effect=_ephemeral_auto_approve(handler))
        result = await handler.handle("Bash", {"command": "echo hi"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_write_is_not_auto_approved(self):
        handler, provider, _ = make_handler()
        provider.post_ephemeral = AsyncMock(side_effect=_ephemeral_auto_approve(handler))
        result = await handler.handle("Write", {"file_path": "/tmp/f.txt"}, None)
        assert isinstance(result, PermissionResultAllow)


class TestApprovalFlow:
    async def test_approval_returns_allow(self):
        handler, provider, _ = make_handler(debounce_ms=10)
        provider.post_ephemeral = AsyncMock(side_effect=_ephemeral_auto_approve(handler))
        result = await handler.handle("Bash", {"command": "rm -rf /"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_denial_returns_deny(self):
        handler, provider, _ = make_handler(debounce_ms=10)
        provider.post_ephemeral = AsyncMock(side_effect=_ephemeral_auto_deny(handler))
        result = await handler.handle("Bash", {"command": "dangerous"}, None)
        assert isinstance(result, PermissionResultDeny)

    async def test_approval_message_posted_ephemeral(self):
        handler, provider, _ = make_handler(debounce_ms=10)
        provider.post_ephemeral = AsyncMock(side_effect=_ephemeral_auto_approve(handler))
        await handler.handle("Edit", {"path": "/tmp/f.py"}, None)
        assert provider.post_ephemeral.call_count >= 1


class TestHandleAction:
    async def test_approve_action_sets_decision(self):
        handler, provider, _ = make_handler()
        batch_id = "test-batch-123"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event

        await handler.handle_action(
            value=f"approve:{batch_id}",
            user_id="U_TEST",
        )

        assert handler._batch.decisions.get(batch_id) is True
        assert event.is_set()

    async def test_deny_action_sets_decision(self):
        handler, provider, _ = make_handler()
        batch_id = "test-batch-456"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event

        await handler.handle_action(
            value=f"deny:{batch_id}",
            user_id="U_TEST",
        )

        assert handler._batch.decisions.get(batch_id) is False
        assert event.is_set()

    async def test_action_posts_to_turn_thread_not_update(self):
        """Ephemeral messages can't be updated — confirmation goes to turn thread."""
        handler, provider, _ = make_handler()
        batch_id = "test-batch-upd"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event

        await handler.handle_action(
            value=f"approve:{batch_id}",
            user_id="U_TEST",
        )

        # Should NOT update the ephemeral message
        provider.update.assert_not_called()

    async def test_unknown_action_value_ignored(self):
        handler, provider, _ = make_handler()
        await handler.handle_action(
            value="unknown_value",
            user_id="U_TEST",
        )

    async def test_unauthorized_user_rejected(self):
        """Actions from non-authenticated users should be ignored."""
        handler, provider, _ = make_handler(authenticated_user_id="U_OWNER")
        batch_id = "test-batch-auth"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event

        await handler.handle_action(
            value=f"approve:{batch_id}",
            user_id="U_INTRUDER",
        )

        # Decision should NOT have been set
        assert batch_id not in handler._batch.decisions
        assert not event.is_set()


class TestAutoApproveList:
    def test_list_files_approved(self):
        from summon_claude.sessions.permissions import _AUTO_APPROVE_TOOLS

        assert "ListFiles" in _AUTO_APPROVE_TOOLS

    def test_get_symbols_overview_approved(self):
        from summon_claude.sessions.permissions import _AUTO_APPROVE_TOOLS

        assert "GetSymbolsOverview" in _AUTO_APPROVE_TOOLS

    def test_find_symbol_approved(self):
        from summon_claude.sessions.permissions import _AUTO_APPROVE_TOOLS

        assert "FindSymbol" in _AUTO_APPROVE_TOOLS

    def test_bash_not_approved(self):
        from summon_claude.sessions.permissions import _AUTO_APPROVE_TOOLS

        assert "Bash" not in _AUTO_APPROVE_TOOLS

    def test_edit_not_approved(self):
        from summon_claude.sessions.permissions import _AUTO_APPROVE_TOOLS

        assert "Edit" not in _AUTO_APPROVE_TOOLS


class TestFormatRequestSummary:
    def test_bash_includes_command(self):
        req = PendingRequest(
            request_id="r1",
            tool_name="Bash",
            input_data={"command": "git status"},
        )
        summary = _format_request_summary(req)
        assert "git status" in summary

    def test_bash_truncates_long_command(self):
        req = PendingRequest(
            request_id="r2",
            tool_name="Bash",
            input_data={"command": "x" * 200},
        )
        summary = _format_request_summary(req)
        assert len(summary) < 200  # should be truncated

    def test_write_includes_path(self):
        req = PendingRequest(
            request_id="r3",
            tool_name="Write",
            input_data={"file_path": "/tmp/output.txt"},
        )
        summary = _format_request_summary(req)
        assert "/tmp/output.txt" in summary

    def test_edit_includes_path(self):
        req = PendingRequest(
            request_id="r4",
            tool_name="Edit",
            input_data={"path": "/src/main.py"},
        )
        summary = _format_request_summary(req)
        assert "/src/main.py" in summary

    def test_notebook_edit_includes_path(self):
        req = PendingRequest(
            request_id="r5",
            tool_name="NotebookEdit",
            input_data={"notebook_path": "/notebooks/analysis.ipynb"},
        )
        summary = _format_request_summary(req)
        assert "analysis.ipynb" in summary

    def test_generic_tool_returns_string_with_params(self):
        req = PendingRequest(
            request_id="r6",
            tool_name="CustomTool",
            input_data={"key": "value"},
        )
        summary = _format_request_summary(req)
        assert isinstance(summary, str)
        assert "CustomTool" in summary


class TestPermissionEphemeral:
    """Tests for ephemeral permission posting."""

    async def test_permissions_use_ephemeral(self):
        """Permission requests should go through post_ephemeral."""
        handler, provider, router = make_handler(authenticated_user_id="U_TEST")
        provider.post_ephemeral = AsyncMock(side_effect=_ephemeral_auto_approve(handler))
        await handler.handle("Edit", {"path": "/tmp/f.py"}, None)
        provider.post_ephemeral.assert_called()

    async def test_handle_action_posts_to_turn_thread(self):
        """handle_action should post confirmation to turn thread, not update."""
        handler, provider, router = make_handler()
        batch_id = "test-batch-123"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event

        await handler.handle_action(
            value=f"approve:{batch_id}",
            user_id="U_TEST",
        )

        assert handler._batch.decisions.get(batch_id) is True
        provider.update.assert_not_called()

    async def test_authenticated_user_id_set(self):
        """PermissionHandler should store authenticated_user_id."""
        handler, _, _ = make_handler(authenticated_user_id="U_CUSTOM")
        assert handler._authenticated_user_id == "U_CUSTOM"

    async def test_authenticated_user_id_default(self):
        """PermissionHandler should default authenticated_user_id to empty string."""
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        config = make_config()
        handler = PermissionHandler(router, config)
        assert handler._authenticated_user_id == ""

    async def test_permission_ping_goes_to_main_channel(self):
        """Permission notification ping should go to main channel, not thread."""
        handler, provider, router = make_handler(authenticated_user_id="U_PING")
        provider.post_ephemeral = AsyncMock(side_effect=_ephemeral_auto_approve(handler))
        provider.post = AsyncMock(return_value=AsyncMock(ts="1234"))

        await handler.handle("Edit", {"path": "/tmp/f.py"}, None)

        # post() should have been called with the permission ping (no thread_ts)
        ping_calls = [
            c
            for c in provider.post.call_args_list
            if "Permission needed" in str(c) and "U_PING" in str(c)
        ]
        assert len(ping_calls) == 1
        # Main channel post = no thread_ts kwarg
        call_kwargs = ping_calls[0].kwargs
        assert "thread_ts" not in call_kwargs or call_kwargs.get("thread_ts") is None


class TestPermissionSuggestions:
    """Test permission suggestions behavior (BUG-013)."""

    async def test_suggestion_allow_returns_allowed_without_slack(self):
        """When suggestion.behavior='allow', return PermissionResultAllow without Slack."""
        from unittest.mock import MagicMock

        handler, provider, _ = make_handler()

        suggestion = MagicMock()
        suggestion.behavior = "allow"
        context = MagicMock()
        context.suggestions = [suggestion]

        result = await handler.handle("Bash", {"command": "ls"}, context)

        assert isinstance(result, PermissionResultAllow)
        provider.post_ephemeral.assert_not_called()

    async def test_suggestion_deny_returns_denied_without_slack(self):
        """When suggestion.behavior='deny', return PermissionResultDeny without Slack."""
        from unittest.mock import MagicMock

        handler, provider, _ = make_handler()

        suggestion = MagicMock()
        suggestion.behavior = "deny"
        context = MagicMock()
        context.suggestions = [suggestion]

        result = await handler.handle("Bash", {"command": "rm -rf /"}, context)

        assert isinstance(result, PermissionResultDeny)
        provider.post_ephemeral.assert_not_called()

    async def test_suggestion_ask_falls_through_to_slack(self):
        """When suggestion.behavior='ask', fall through to Slack approval flow."""
        from unittest.mock import MagicMock

        handler, provider, _ = make_handler()
        provider.post_ephemeral = AsyncMock(side_effect=_ephemeral_auto_approve(handler))

        suggestion = MagicMock()
        suggestion.behavior = "ask"
        context = MagicMock()
        context.suggestions = [suggestion]

        result = await handler.handle("Bash", {"command": "test"}, context)

        assert isinstance(result, PermissionResultAllow)
        provider.post_ephemeral.assert_called()

    async def test_no_suggestion_uses_auto_approve_fallback(self):
        """When context=None, still use _AUTO_APPROVE_TOOLS fallback."""
        handler, provider, _ = make_handler()

        result = await handler.handle("Read", {"file_path": "/tmp/f"}, None)

        assert isinstance(result, PermissionResultAllow)
        provider.post_ephemeral.assert_not_called()


class TestGitHubMCPReadToolsAutoApproved:
    """GitHub MCP read-only tools should be auto-approved without Slack prompt."""

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__github__pull_request_read",
            "mcp__github__get_file_contents",
            "mcp__github__get_commit",
            "mcp__github__list_pull_requests",
            "mcp__github__search_code",
            "mcp__github__list_issues",
        ],
    )
    async def test_read_tool_auto_approved(self, tool_name):
        handler, provider, _ = make_handler()
        result = await handler.handle(tool_name, {}, None)
        assert isinstance(result, PermissionResultAllow)
        provider.post_ephemeral.assert_not_called()


class TestGitHubMCPToolsRequireApproval:
    """GitHub MCP tools that are visible-to-others or destructive require HITL approval."""

    @pytest.mark.parametrize(
        "tool_name",
        [
            # Visible-to-others
            "mcp__github__create_pull_request",
            "mcp__github__create_issue",
            "mcp__github__add_issue_comment",
            "mcp__github__pull_request_review_write",
            # Destructive
            "mcp__github__merge_pull_request",
            "mcp__github__create_or_update_file",
            "mcp__github__push_files",
            "mcp__github__delete_branch",
            "mcp__github__close_pull_request",
            "mcp__github__close_issue",
            "mcp__github__update_pull_request_branch",
            # Unknown (fail-closed)
            pytest.param("mcp__github__some_future_tool", id="unknown_tool_falls_through"),
        ],
    )
    async def test_requires_slack_approval(self, tool_name):
        handler, provider, _ = make_handler()
        provider.post_ephemeral = AsyncMock(side_effect=_ephemeral_auto_approve(handler))
        result = await handler.handle(tool_name, {}, None)
        assert isinstance(result, PermissionResultAllow)
        provider.post_ephemeral.assert_called()

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__github__merge_pull_request",
            "mcp__github__create_pull_request",
        ],
    )
    async def test_ignores_sdk_allow_suggestion(self, tool_name):
        """Restricted tools must require Slack approval even when SDK suggests allow."""
        handler, provider, _ = make_handler()
        provider.post_ephemeral = AsyncMock(side_effect=_ephemeral_auto_approve(handler))

        suggestion = MagicMock()
        suggestion.behavior = "allow"
        context = MagicMock()
        context.suggestions = [suggestion]

        result = await handler.handle(tool_name, {}, context)
        assert isinstance(result, PermissionResultAllow)
        provider.post_ephemeral.assert_called()


class TestGitHubMCPGuardTests:
    """Guard tests: pin permission sets so changes aren't silently missed."""

    def test_require_approval_set_pinned(self):
        assert (
            frozenset(
                [
                    "mcp__github__merge_pull_request",
                    "mcp__github__delete_branch",
                    "mcp__github__close_pull_request",
                    "mcp__github__close_issue",
                    "mcp__github__update_pull_request_branch",
                    "mcp__github__push_files",
                    "mcp__github__create_or_update_file",
                    "mcp__github__pull_request_review_write",
                    "mcp__github__create_pull_request",
                    "mcp__github__create_issue",
                    "mcp__github__add_issue_comment",
                ]
            )
            == _GITHUB_MCP_REQUIRE_APPROVAL
        )

    def test_auto_approve_set_pinned(self):
        assert (
            frozenset(
                [
                    "mcp__github__pull_request_read",
                    "mcp__github__get_file_contents",
                ]
            )
            == _GITHUB_MCP_AUTO_APPROVE
        )

    def test_require_approval_not_matched_by_auto_approve_prefixes(self):
        """No require-approval tool should match an auto-approve prefix."""
        for tool in _GITHUB_MCP_REQUIRE_APPROVAL:
            assert not tool.startswith(_GITHUB_MCP_AUTO_APPROVE_PREFIXES), (
                f"{tool} matches an auto-approve prefix"
            )


class TestIdentityVerificationFailClosed:
    """Guard tests: identity checks are fail-closed (no truthy bypass)."""

    async def test_handle_action_rejects_when_authenticated_user_empty(self):
        """handle_action should reject even if authenticated_user_id is empty string."""
        handler, _, _ = make_handler(authenticated_user_id="")
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event

        await handler.handle_action(
            value=f"approve:{batch_id}",
            user_id="U_INTRUDER",
        )

        assert batch_id not in handler._batch.decisions
        assert not event.is_set()

    async def test_handle_ask_user_action_rejects_when_authenticated_user_empty(self):
        """handle_ask_user_action should reject even if authenticated_user_id is empty."""
        handler, _, _ = make_handler(authenticated_user_id="")
        # Set up state so the request_id exists — ensures rejection is from
        # the identity check, not the "request_id not in events" early return.
        handler._ask_user.events["req-1"] = asyncio.Event()
        handler._ask_user.questions["req-1"] = [
            {"question": "Q?", "header": "H", "options": [{"label": "A", "description": ""}]}
        ]

        await handler.handle_ask_user_action(
            value="req-1|0|0",
            user_id="U_INTRUDER",
        )

        # Answer should NOT have been recorded (identity check rejected it)
        assert "req-1" not in handler._ask_user.answers or not handler._ask_user.answers.get(
            "req-1"
        )

    async def test_receive_text_input_rejects_non_owner(self):
        """receive_text_input should reject messages from non-owner users."""
        handler, _, _ = make_handler(authenticated_user_id="U_OWNER")
        handler._ask_user.pending_other = ("req-1", 0)
        handler._ask_user.questions["req-1"] = [{"question": "Q?", "header": "H", "options": []}]

        await handler.receive_text_input("hacked answer", user_id="U_INTRUDER")

        # Pending should still be set (not consumed)
        assert handler._ask_user.pending_other is not None

    async def test_receive_text_input_accepts_owner(self):
        """receive_text_input should accept messages from the session owner."""
        handler, _, _ = make_handler(authenticated_user_id="U_OWNER")
        handler._ask_user.pending_other = ("req-1", 0)
        handler._ask_user.questions["req-1"] = [{"question": "Q?", "header": "H", "options": []}]
        handler._ask_user.answers["req-1"] = {}
        handler._ask_user.events["req-1"] = asyncio.Event()

        await handler.receive_text_input("valid answer", user_id="U_OWNER")

        # Pending should be consumed
        assert handler._ask_user.pending_other is None
