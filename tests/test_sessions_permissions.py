"""Tests for summon_claude.permissions — now uses ThreadRouter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from helpers import make_mock_slack_client
from summon_claude.config import SummonConfig
from summon_claude.sessions.permissions import (
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
            "slack_signing_secret": "s",
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
