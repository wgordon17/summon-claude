"""Tests for summon_claude.permissions — now uses ThreadRouter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from helpers import make_mock_provider
from summon_claude.config import SummonConfig
from summon_claude.permissions import PendingRequest, PermissionHandler, _format_request_summary
from summon_claude.providers.base import MessageRef
from summon_claude.thread_router import ThreadRouter


def make_config(allowed_user_ids=None, debounce_ms=10):
    """Build a minimal SummonConfig with fast debounce for tests."""
    return SummonConfig.model_validate(
        {
            "slack_bot_token": "xoxb-t",
            "slack_app_token": "xapp-t",
            "slack_signing_secret": "s",
            "allowed_user_ids": allowed_user_ids or ["U_ALLOWED"],
            "permission_debounce_ms": debounce_ms,
        }
    )


def make_handler(allowed_user_ids=None, debounce_ms=10):
    """Create a PermissionHandler with a mocked ThreadRouter."""
    provider = make_mock_provider()
    router = ThreadRouter(provider, "C123")
    config = make_config(allowed_user_ids=allowed_user_ids, debounce_ms=debounce_ms)
    return PermissionHandler(router, config), provider, router


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

        async def approve_after_post(*args, **kwargs):
            async def do_approve():
                await asyncio.sleep(0.05)
                for batch_id in list(handler._batch.events.keys()):
                    handler._batch.decisions[batch_id] = True
                    handler._batch.events[batch_id].set()

            asyncio.create_task(do_approve())
            return MessageRef(channel_id="C123", ts="111.001")

        provider.post_message = AsyncMock(side_effect=approve_after_post)
        result = await handler.handle("Bash", {"command": "echo hi"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_write_is_not_auto_approved(self):
        handler, provider, _ = make_handler()

        async def approve_after_post(*args, **kwargs):
            async def do_approve():
                await asyncio.sleep(0.05)
                for batch_id in list(handler._batch.events.keys()):
                    handler._batch.decisions[batch_id] = True
                    handler._batch.events[batch_id].set()

            asyncio.create_task(do_approve())
            return MessageRef(channel_id="C123", ts="111.001")

        provider.post_message = AsyncMock(side_effect=approve_after_post)
        result = await handler.handle("Write", {"file_path": "/tmp/f.txt"}, None)
        assert isinstance(result, PermissionResultAllow)


class TestApprovalFlow:
    async def test_approval_returns_allow(self):
        handler, provider, _ = make_handler(debounce_ms=10)

        async def approve_after_post(*args, **kwargs):
            async def do_approve():
                await asyncio.sleep(0.05)
                for batch_id in list(handler._batch.events.keys()):
                    handler._batch.decisions[batch_id] = True
                    handler._batch.events[batch_id].set()

            asyncio.create_task(do_approve())
            return MessageRef(channel_id="C123", ts="111.001")

        provider.post_message = AsyncMock(side_effect=approve_after_post)
        result = await handler.handle("Bash", {"command": "rm -rf /"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_denial_returns_deny(self):
        handler, provider, _ = make_handler(debounce_ms=10)

        async def deny_after_post(*args, **kwargs):
            async def do_deny():
                await asyncio.sleep(0.05)
                for batch_id in list(handler._batch.events.keys()):
                    handler._batch.decisions[batch_id] = False
                    handler._batch.events[batch_id].set()

            asyncio.create_task(do_deny())
            return MessageRef(channel_id="C123", ts="111.001")

        provider.post_message = AsyncMock(side_effect=deny_after_post)
        result = await handler.handle("Bash", {"command": "dangerous"}, None)
        assert isinstance(result, PermissionResultDeny)

    async def test_approval_message_posted_to_channel(self):
        handler, provider, _ = make_handler(debounce_ms=10)

        async def auto_approve(*args, **kwargs):
            async def do():
                await asyncio.sleep(0.05)
                for batch_id in list(handler._batch.events.keys()):
                    handler._batch.decisions[batch_id] = True
                    handler._batch.events[batch_id].set()

            asyncio.create_task(do())
            return MessageRef(channel_id="C123", ts="111.001")

        provider.post_message = AsyncMock(side_effect=auto_approve)
        await handler.handle("Edit", {"path": "/tmp/f.py"}, None)
        assert provider.post_message.call_count >= 1


class TestHandleAction:
    async def test_approve_action_sets_decision(self):
        handler, provider, _ = make_handler()
        batch_id = "test-batch-123"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event

        await handler.handle_action(
            action_id="permission_approve",
            value=f"approve:{batch_id}",
            user_id="U_ALLOWED",
            channel_id="C123",
            message_ts="111.001",
        )

        assert handler._batch.decisions.get(batch_id) is True
        assert event.is_set()

    async def test_deny_action_sets_decision(self):
        handler, provider, _ = make_handler()
        batch_id = "test-batch-456"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event

        await handler.handle_action(
            action_id="permission_deny",
            value=f"deny:{batch_id}",
            user_id="U_ALLOWED",
            channel_id="C123",
            message_ts="111.001",
        )

        assert handler._batch.decisions.get(batch_id) is False
        assert event.is_set()

    async def test_unauthorized_user_ignored(self):
        handler, provider, _ = make_handler(allowed_user_ids=["U_ALLOWED"])
        batch_id = "test-batch-789"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event

        await handler.handle_action(
            action_id="permission_approve",
            value=f"approve:{batch_id}",
            user_id="U_UNAUTHORIZED",
            channel_id="C123",
            message_ts="111.001",
        )

        assert not event.is_set()
        assert batch_id not in handler._batch.decisions

    async def test_action_updates_message(self):
        handler, provider, _ = make_handler()
        batch_id = "test-batch-upd"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event

        await handler.handle_action(
            action_id="permission_approve",
            value=f"approve:{batch_id}",
            user_id="U_ALLOWED",
            channel_id="C123",
            message_ts="999.001",
        )

        provider.update_message.assert_called_once()
        call_kwargs = provider.update_message.call_args[0]
        assert call_kwargs[1] == "999.001"

    async def test_unknown_action_value_ignored(self):
        handler, provider, _ = make_handler()
        await handler.handle_action(
            action_id="something_else",
            value="unknown_value",
            user_id="U_ALLOWED",
            channel_id="C123",
            message_ts="111.001",
        )


class TestAutoApproveList:
    def test_list_files_approved(self):
        from summon_claude.permissions import _AUTO_APPROVE_TOOLS

        assert "ListFiles" in _AUTO_APPROVE_TOOLS

    def test_get_symbols_overview_approved(self):
        from summon_claude.permissions import _AUTO_APPROVE_TOOLS

        assert "GetSymbolsOverview" in _AUTO_APPROVE_TOOLS

    def test_find_symbol_approved(self):
        from summon_claude.permissions import _AUTO_APPROVE_TOOLS

        assert "FindSymbol" in _AUTO_APPROVE_TOOLS

    def test_bash_not_approved(self):
        from summon_claude.permissions import _AUTO_APPROVE_TOOLS

        assert "Bash" not in _AUTO_APPROVE_TOOLS

    def test_edit_not_approved(self):
        from summon_claude.permissions import _AUTO_APPROVE_TOOLS

        assert "Edit" not in _AUTO_APPROVE_TOOLS


class TestFormatRequestSummary:
    def test_bash_includes_command(self):
        req = PendingRequest(
            request_id="r1",
            tool_name="Bash",
            input_data={"command": "git status"},
            context=None,
        )
        summary = _format_request_summary(req)
        assert "git status" in summary

    def test_bash_truncates_long_command(self):
        req = PendingRequest(
            request_id="r2",
            tool_name="Bash",
            input_data={"command": "x" * 200},
            context=None,
        )
        summary = _format_request_summary(req)
        assert len(summary) < 200  # should be truncated

    def test_write_includes_path(self):
        req = PendingRequest(
            request_id="r3",
            tool_name="Write",
            input_data={"file_path": "/tmp/output.txt"},
            context=None,
        )
        summary = _format_request_summary(req)
        assert "/tmp/output.txt" in summary

    def test_edit_includes_path(self):
        req = PendingRequest(
            request_id="r4",
            tool_name="Edit",
            input_data={"path": "/src/main.py"},
            context=None,
        )
        summary = _format_request_summary(req)
        assert "/src/main.py" in summary

    def test_notebook_edit_includes_path(self):
        req = PendingRequest(
            request_id="r5",
            tool_name="NotebookEdit",
            input_data={"notebook_path": "/notebooks/analysis.ipynb"},
            context=None,
        )
        summary = _format_request_summary(req)
        assert "analysis.ipynb" in summary

    def test_generic_tool_returns_string_with_params(self):
        req = PendingRequest(
            request_id="r6",
            tool_name="CustomTool",
            input_data={"key": "value"},
            context=None,
        )
        summary = _format_request_summary(req)
        assert isinstance(summary, str)
        assert "CustomTool" in summary


class TestPermissionBroadcast:
    async def test_permission_in_thread_uses_reply_broadcast(self):
        """Permission messages in a thread should use reply_broadcast=True."""
        handler, provider, router = make_handler()

        # Start a turn to create a thread context
        await router.start_turn(1)

        async def auto_approve(*args, **kwargs):
            async def do():
                await asyncio.sleep(0.05)
                for batch_id in list(handler._batch.events.keys()):
                    handler._batch.decisions[batch_id] = True
                    handler._batch.events[batch_id].set()

            asyncio.create_task(do())
            return MessageRef(channel_id="C123", ts="111.001")

        provider.post_message = AsyncMock(side_effect=auto_approve)
        await handler.handle("Bash", {"command": "test"}, None)

        # Check that post_permission was called with reply_broadcast=True
        call_args = provider.post_message.call_args
        assert call_args[1]["reply_broadcast"] is True

    async def test_permission_includes_channel_mention(self):
        """Permission messages should include <!channel> when in a thread."""
        handler, provider, router = make_handler()

        await router.start_turn(1)

        async def auto_approve(*args, **kwargs):
            async def do():
                await asyncio.sleep(0.05)
                for batch_id in list(handler._batch.events.keys()):
                    handler._batch.decisions[batch_id] = True
                    handler._batch.events[batch_id].set()

            asyncio.create_task(do())
            return MessageRef(channel_id="C123", ts="111.001")

        provider.post_message = AsyncMock(side_effect=auto_approve)
        await handler.handle("Bash", {"command": "test"}, None)

        # Check that the message includes <!channel>
        call_args = provider.post_message.call_args
        text = call_args[0][1]
        assert text.startswith("<!channel>")


class TestPermissionSuggestions:
    """Test permission suggestions behavior (BUG-013)."""

    async def test_suggestion_allow_returns_allowed_without_slack(self):
        """When suggestion.behavior='allow', return PermissionResultAllow without Slack."""
        from unittest.mock import MagicMock

        handler, provider, _ = make_handler()

        # Create a mock context with a suggestion
        suggestion = MagicMock()
        suggestion.behavior = "allow"
        context = MagicMock()
        context.suggestions = [suggestion]

        result = await handler.handle("Bash", {"command": "ls"}, context)

        # Should return allow immediately
        assert isinstance(result, PermissionResultAllow)
        # Should NOT have posted to Slack
        provider.post_message.assert_not_called()

    async def test_suggestion_deny_returns_denied_without_slack(self):
        """When suggestion.behavior='deny', return PermissionResultDeny without Slack."""
        from unittest.mock import MagicMock

        handler, provider, _ = make_handler()

        # Create a mock context with a suggestion
        suggestion = MagicMock()
        suggestion.behavior = "deny"
        context = MagicMock()
        context.suggestions = [suggestion]

        result = await handler.handle("Bash", {"command": "rm -rf /"}, context)

        # Should return deny immediately
        assert isinstance(result, PermissionResultDeny)
        # Should NOT have posted to Slack
        provider.post_message.assert_not_called()

    async def test_suggestion_ask_falls_through_to_slack(self):
        """When suggestion.behavior='ask', fall through to Slack approval flow."""
        from unittest.mock import MagicMock

        handler, provider, _ = make_handler()

        async def approve_after_post(*args, **kwargs):
            async def do_approve():
                await asyncio.sleep(0.05)
                for batch_id in list(handler._batch.events.keys()):
                    handler._batch.decisions[batch_id] = True
                    handler._batch.events[batch_id].set()

            asyncio.create_task(do_approve())
            return MessageRef(channel_id="C123", ts="111.001")

        provider.post_message = AsyncMock(side_effect=approve_after_post)

        # Create a mock context with a suggestion
        suggestion = MagicMock()
        suggestion.behavior = "ask"
        context = MagicMock()
        context.suggestions = [suggestion]

        result = await handler.handle("Bash", {"command": "test"}, context)

        # Should have gone through Slack approval
        assert isinstance(result, PermissionResultAllow)
        provider.post_message.assert_called()

    async def test_no_suggestion_uses_auto_approve_fallback(self):
        """When context=None, still use _AUTO_APPROVE_TOOLS fallback."""
        handler, provider, _ = make_handler()

        # For a tool in _AUTO_APPROVE_TOOLS, should auto-approve even with None context
        result = await handler.handle("Read", {"file_path": "/tmp/f"}, None)

        assert isinstance(result, PermissionResultAllow)
        # Should NOT have posted to Slack
        provider.post_message.assert_not_called()
