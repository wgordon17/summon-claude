"""Tests for summon_claude.permissions — now uses ThreadRouter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from conftest import make_test_config

from helpers import make_mock_slack_client
from summon_claude.sessions.permissions import (
    _GITHUB_MCP_AUTO_APPROVE,
    _GITHUB_MCP_AUTO_APPROVE_PREFIXES,
    _GITHUB_MCP_REQUIRE_APPROVAL,
    _JIRA_MCP_AUTO_APPROVE_EXACT,
    _JIRA_MCP_AUTO_APPROVE_PREFIXES,
    _JIRA_MCP_HARD_DENY,
    _JIRA_MCP_PREFIX,
    _SUMMON_MCP_AUTO_APPROVE_PREFIXES,
    _WRITE_GATED_TOOLS,
    ApprovalBridge,
    ApprovalInfo,
    PendingRequest,
    PermissionHandler,
    _build_diff_preview_blocks,
    _format_request_summary,
)
from summon_claude.slack.router import ThreadRouter


def make_config(debounce_ms=10):
    """Build a minimal SummonConfig with fast debounce for tests."""
    return make_test_config(permission_debounce_ms=debounce_ms)


def make_handler(debounce_ms=10, authenticated_user_id="U_TEST", bridge=None):
    """Create a PermissionHandler with a mocked ThreadRouter.

    Sets _in_containment=True so tests exercise the Slack approval flow for
    write tools without hitting the write gate. Write gate behavior is
    tested in test_permissions_write_gate.py.
    """
    client = make_mock_slack_client()
    router = ThreadRouter(client)
    config = make_config(debounce_ms=debounce_ms)
    handler = PermissionHandler(
        router, config, authenticated_user_id=authenticated_user_id, bridge=bridge
    )
    # Bypass write gate — these tests exercise HITL batching, not the gate
    handler._check_write_gate = AsyncMock(return_value=None)
    return handler, client, router


def _interactive_auto_approve(handler):
    """Return a side effect for post_interactive that auto-approves all pending batches."""

    async def side_effect(*_args, **_kwargs):
        async def do():
            await asyncio.sleep(0.05)
            for batch_id in list(handler._batch.events.keys()):
                handler._batch.decisions[batch_id] = True
                handler._batch.events[batch_id].set()

        asyncio.create_task(do())
        return MagicMock(ts="mock_ts")

    return side_effect


def _interactive_auto_deny(handler):
    """Return a side effect for post_interactive that auto-denies all pending batches."""

    async def side_effect(*_args, **_kwargs):
        async def do():
            await asyncio.sleep(0.05)
            for batch_id in list(handler._batch.events.keys()):
                handler._batch.decisions[batch_id] = False
                handler._batch.events[batch_id].set()

        asyncio.create_task(do())
        return MagicMock(ts="mock_ts")

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
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Bash", {"command": "echo hi"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_write_is_not_auto_approved(self):
        handler, provider, _ = make_handler()
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Write", {"file_path": "/tmp/f.txt"}, None)
        assert isinstance(result, PermissionResultAllow)


class TestApprovalFlow:
    async def test_approval_returns_allow(self):
        handler, provider, _ = make_handler(debounce_ms=10)
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Bash", {"command": "rm -rf /"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_denial_returns_deny(self):
        handler, provider, _ = make_handler(debounce_ms=10)
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_deny(handler))
        result = await handler.handle("Bash", {"command": "dangerous"}, None)
        assert isinstance(result, PermissionResultDeny)

    async def test_approval_message_posted_interactive(self):
        handler, provider, _ = make_handler(debounce_ms=10)
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        await handler.handle("Edit", {"path": "/tmp/f.py"}, None)
        assert provider.post_interactive.call_count >= 1


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

    async def test_hitl_labels_do_not_embed_user_id(self):
        """HITL labels use constants — no <@user_id> mention to avoid Slack pings."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        batch_id = "test-batch-label"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["CustomTool"]
        handler._batch.tool_inputs[batch_id] = [{"key": "val"}]
        handler._batch.message_ts[batch_id] = "1234.5678"

        await handler.handle_action(
            value=f"deny:{batch_id}",
            user_id="U_TEST",
        )

        fut = bridge.create_future("CustomTool")
        assert fut.done()
        info = fut.result()
        assert info.label == "user-denied"
        assert "<@" not in info.label
        assert info.is_denial is True


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


class TestDiffPreviewBlocks:
    """Tests for diff preview blocks in approval messages."""

    def test_edit_produces_diff_block(self):
        req = PendingRequest(
            request_id="r1",
            tool_name="Edit",
            input_data={
                "path": "/src/main.py",
                "old_string": "old line\n",
                "new_string": "new line\n",
            },
        )
        blocks = _build_diff_preview_blocks([req])
        assert len(blocks) == 1
        assert blocks[0]["type"] == "markdown"
        assert "```diff" in blocks[0]["text"]
        assert "-old line" in blocks[0]["text"]
        assert "+new line" in blocks[0]["text"]

    def test_str_replace_editor_produces_diff_block(self):
        req = PendingRequest(
            request_id="r1",
            tool_name="str_replace_editor",
            input_data={
                "path": "/src/main.py",
                "old_string": "before\n",
                "new_string": "after\n",
            },
        )
        blocks = _build_diff_preview_blocks([req])
        assert len(blocks) == 1
        assert "-before" in blocks[0]["text"]
        assert "+after" in blocks[0]["text"]

    def test_write_produces_preview_block(self):
        req = PendingRequest(
            request_id="r1",
            tool_name="Write",
            input_data={"file_path": "/src/new.py", "content": "print('hello')\n"},
        )
        blocks = _build_diff_preview_blocks([req])
        assert len(blocks) == 1
        assert "New file: /src/new.py" in blocks[0]["text"]
        assert "print('hello')" in blocks[0]["text"]

    def test_write_shows_full_content(self):
        content = "\n".join(f"line {i}" for i in range(50))
        req = PendingRequest(
            request_id="r1",
            tool_name="Write",
            input_data={"file_path": "/src/big.py", "content": content},
        )
        blocks = _build_diff_preview_blocks([req])
        assert len(blocks) == 1
        text = blocks[0]["text"]
        assert "line 0" in text
        assert "line 49" in text

    def test_bash_produces_no_block(self):
        req = PendingRequest(
            request_id="r1",
            tool_name="Bash",
            input_data={"command": "git status"},
        )
        blocks = _build_diff_preview_blocks([req])
        assert blocks == []

    def test_empty_edit_produces_no_block(self):
        req = PendingRequest(
            request_id="r1",
            tool_name="Edit",
            input_data={"path": "/src/main.py", "old_string": "same\n", "new_string": "same\n"},
        )
        blocks = _build_diff_preview_blocks([req])
        assert blocks == []

    def test_write_empty_content_produces_no_block(self):
        req = PendingRequest(
            request_id="r1",
            tool_name="Write",
            input_data={"file_path": "/src/new.py", "content": ""},
        )
        blocks = _build_diff_preview_blocks([req])
        assert blocks == []

    def test_write_empty_path_produces_no_block(self):
        req = PendingRequest(
            request_id="r1",
            tool_name="Write",
            input_data={"file_path": "", "content": "hello"},
        )
        blocks = _build_diff_preview_blocks([req])
        assert blocks == []

    def test_mixed_batch_combines_previews(self):
        edit_req = PendingRequest(
            request_id="r1",
            tool_name="Edit",
            input_data={
                "path": "/src/a.py",
                "old_string": "old\n",
                "new_string": "new\n",
            },
        )
        bash_req = PendingRequest(
            request_id="r2",
            tool_name="Bash",
            input_data={"command": "ls"},
        )
        blocks = _build_diff_preview_blocks([edit_req, bash_req])
        assert len(blocks) == 1
        assert "-old" in blocks[0]["text"]

    def test_multiple_edits_combined(self):
        req1 = PendingRequest(
            request_id="r1",
            tool_name="Edit",
            input_data={
                "path": "/src/a.py",
                "old_string": "aaa\n",
                "new_string": "bbb\n",
            },
        )
        req2 = PendingRequest(
            request_id="r2",
            tool_name="Edit",
            input_data={
                "path": "/src/b.py",
                "old_string": "ccc\n",
                "new_string": "ddd\n",
            },
        )
        blocks = _build_diff_preview_blocks([req1, req2])
        assert len(blocks) == 1
        text = blocks[0]["text"]
        assert "src/a.py" in text
        assert "src/b.py" in text
        assert "-aaa" in text
        assert "-ccc" in text

    def test_backtick_fence_escaped(self):
        req = PendingRequest(
            request_id="r1",
            tool_name="Edit",
            input_data={
                "path": "/src/main.py",
                "old_string": "before ```code``` after\n",
                "new_string": "replaced\n",
            },
        )
        blocks = _build_diff_preview_blocks([req])
        assert len(blocks) == 1
        assert "```" not in blocks[0]["text"].split("```diff\n", 1)[1].rsplit("\n```", 1)[0]

    def test_large_diff_truncated(self):
        req = PendingRequest(
            request_id="r1",
            tool_name="Edit",
            input_data={
                "path": "/src/big.py",
                "old_string": ("a" * 40 + "\n") * 200,
                "new_string": ("b" * 40 + "\n") * 200,
            },
        )
        blocks = _build_diff_preview_blocks([req])
        assert len(blocks) == 1
        assert "truncated" in blocks[0]["text"]


class TestPermissionInteractive:
    """Tests for interactive permission posting."""

    async def test_permissions_use_interactive(self):
        """Permission requests should go through post_interactive."""
        handler, provider, router = make_handler(authenticated_user_id="U_TEST")
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        await handler.handle("Edit", {"path": "/tmp/f.py"}, None)
        provider.post_interactive.assert_called()

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

    def test_authenticated_user_id_is_required(self):
        """PermissionHandler must require authenticated_user_id (no default)."""
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        config = make_config()
        with pytest.raises(TypeError, match="authenticated_user_id"):
            PermissionHandler(router, config)  # type: ignore[call-arg]

    async def test_no_separate_ping_for_normal_messages(self):
        """Normal messages trigger notifications — no separate ping needed."""
        handler, provider, _ = make_handler(authenticated_user_id="U_PING")
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        provider.post = AsyncMock(return_value=MagicMock(ts="1234"))

        await handler.handle("Edit", {"path": "/tmp/f.py"}, None)

        # No ping calls — post_interactive already generates notifications
        ping_calls = [c for c in provider.post.call_args_list if "Permission needed" in str(c)]
        assert len(ping_calls) == 0


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
        provider.post_interactive.assert_not_called()

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
        provider.post_interactive.assert_not_called()

    async def test_suggestion_ask_falls_through_to_slack(self):
        """When suggestion.behavior='ask', fall through to Slack approval flow."""
        from unittest.mock import MagicMock

        handler, provider, _ = make_handler()
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))

        suggestion = MagicMock()
        suggestion.behavior = "ask"
        context = MagicMock()
        context.suggestions = [suggestion]

        result = await handler.handle("Bash", {"command": "test"}, context)

        assert isinstance(result, PermissionResultAllow)
        provider.post_interactive.assert_called()

    async def test_no_suggestion_uses_auto_approve_fallback(self):
        """When context=None, still use _AUTO_APPROVE_TOOLS fallback."""
        handler, provider, _ = make_handler()

        result = await handler.handle("Read", {"file_path": "/tmp/f"}, None)

        assert isinstance(result, PermissionResultAllow)
        provider.post_interactive.assert_not_called()


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
        provider.post_interactive.assert_not_called()


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
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle(tool_name, {}, None)
        assert isinstance(result, PermissionResultAllow)
        provider.post_interactive.assert_called()

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
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))

        suggestion = MagicMock()
        suggestion.behavior = "allow"
        context = MagicMock()
        context.suggestions = [suggestion]

        result = await handler.handle(tool_name, {}, context)
        assert isinstance(result, PermissionResultAllow)
        provider.post_interactive.assert_called()


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


class TestSummonMCPAutoApprove:
    """Guard + behavior tests for summon internal MCP auto-approval."""

    def test_summon_mcp_prefixes_pinned(self):
        assert _SUMMON_MCP_AUTO_APPROVE_PREFIXES == (
            "mcp__summon-cli__",
            "mcp__summon-slack__",
            "mcp__summon-canvas__",
        )

    async def test_summon_cli_tool_auto_approved(self):
        handler, _, _ = make_handler()
        result = await handler.handle("mcp__summon-cli__session_list", {}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_summon_slack_tool_auto_approved(self):
        handler, _, _ = make_handler()
        result = await handler.handle("mcp__summon-slack__slack_read_history", {}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_summon_canvas_tool_auto_approved(self):
        handler, _, _ = make_handler()
        result = await handler.handle("mcp__summon-canvas__summon_canvas_read", {}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_unknown_mcp_tool_not_auto_approved(self):
        handler, provider, _ = make_handler()
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("mcp__unknown__foo", {}, None)
        # Falls through to HITL — not auto-approved by summon prefixes
        assert isinstance(result, PermissionResultAllow)
        provider.post_interactive.assert_called_once()


class TestSessionApprovalCaching:
    """Tests for 'Approve for session' button and per-tool caching."""

    async def test_approve_session_caches_non_write_tool_by_name(self):
        """Non-write-gated tools are cached by bare tool name."""
        handler, _, _ = make_handler()
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["CustomTool"]
        handler._batch.tool_inputs[batch_id] = [{"key": "val"}]

        await handler.handle_action(
            value=f"approve_session:{batch_id}",
            user_id="U_TEST",
        )
        assert "CustomTool" in handler._session_approved_tools

    async def test_cached_tool_auto_approved(self):
        handler, provider, _ = make_handler()
        handler._session_approved_tools.add("CustomTool")
        result = await handler.handle("CustomTool", {"key": "val"}, None)
        assert isinstance(result, PermissionResultAllow)
        # Should not reach HITL
        provider.post_interactive.assert_not_called()

    async def test_github_require_approval_never_cached(self):
        handler, _, _ = make_handler()
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["mcp__github__merge_pull_request"]
        handler._batch.tool_inputs[batch_id] = [{}]

        await handler.handle_action(
            value=f"approve_session:{batch_id}",
            user_id="U_TEST",
        )
        assert "mcp__github__merge_pull_request" not in handler._session_approved_tools
        assert "mcp__github__merge_pull_request" not in handler._session_approved_tool_args

    async def test_regular_approve_does_not_cache(self):
        handler, _, _ = make_handler()
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["CustomTool"]
        handler._batch.tool_inputs[batch_id] = [{"key": "val"}]

        await handler.handle_action(
            value=f"approve:{batch_id}",
            user_id="U_TEST",
        )
        assert "CustomTool" not in handler._session_approved_tools

    async def test_session_cache_per_instance(self):
        h1, _, _ = make_handler()
        h2, _, _ = make_handler()
        h1._session_approved_tools.add("CustomTool")
        assert "CustomTool" not in h2._session_approved_tools

    async def test_approve_session_button_in_blocks(self):
        """Verify 'Approve for session' button appears in approval blocks."""
        handler, provider, _ = make_handler()
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        await handler.handle("Bash", {"command": "echo hi"}, None)
        call_kwargs = provider.post_interactive.call_args.kwargs
        blocks = call_kwargs.get("blocks", [])
        actions_block = next((b for b in blocks if b.get("type") == "actions"), None)
        assert actions_block is not None
        action_ids = {e["action_id"] for e in actions_block["elements"]}
        assert "permission_approve_session" in action_ids
        assert "permission_approve" in action_ids
        assert "permission_deny" in action_ids

    async def test_bash_never_session_cached_as_bare_tool(self):
        """Bash must not appear in _session_approved_tools (bare tool cache)."""
        handler, _, _ = make_handler()
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["Bash"]
        handler._batch.tool_inputs[batch_id] = [{"command": "git status"}]

        await handler.handle_action(
            value=f"approve_session:{batch_id}",
            user_id="U_TEST",
        )
        assert "Bash" not in handler._session_approved_tools

    async def test_handle_action_deletes_interactive_message(self):
        """handle_action should delete the interactive message after user clicks."""
        handler, provider, _ = make_handler()
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.message_ts[batch_id] = "1234.5678"
        handler._batch.tool_names[batch_id] = ["Edit"]

        await handler.handle_action(
            value=f"approve:{batch_id}",
            user_id="U_TEST",
        )
        provider.delete_message.assert_awaited_once_with("1234.5678")

    async def test_approve_session_resolves_bridge_with_session_label(self):
        """approve_session should resolve bridge with 'approved for session' label."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["Edit", "Write"]
        handler._batch.tool_inputs[batch_id] = [{"path": "/f"}, {"file_path": "/g"}]
        handler._batch.message_ts[batch_id] = "1234.5678"

        await handler.handle_action(
            value=f"approve_session:{batch_id}",
            user_id="U_TEST",
        )
        # Bridge should have resolved entries for both tools
        fut_edit = bridge.create_future("Edit")
        assert fut_edit.done()
        info_edit = fut_edit.result()
        assert "approved for session" in info_edit.label

        fut_write = bridge.create_future("Write")
        assert fut_write.done()
        info_write = fut_write.result()
        assert "approved for session" in info_write.label


class TestArgBasedCaching:
    """Tests for per-argument session caching (Bash commands, file paths, etc.)."""

    async def test_approve_session_caches_bash_command(self):
        """approve_session should cache the exact Bash command."""
        handler, _, _ = make_handler()
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["Bash"]
        handler._batch.tool_inputs[batch_id] = [{"command": "git status"}]

        await handler.handle_action(
            value=f"approve_session:{batch_id}",
            user_id="U_TEST",
        )
        assert "git status" in handler._session_approved_tool_args.get("Bash", set())

    async def test_cached_bash_command_auto_approved(self):
        """A Bash command in the arg cache should be auto-approved."""
        handler, provider, _ = make_handler()
        handler._session_approved_tool_args.setdefault("Bash", set()).add("git status")
        result = await handler.handle("Bash", {"command": "git status"}, None)
        assert isinstance(result, PermissionResultAllow)
        provider.post_interactive.assert_not_called()

    async def test_different_bash_command_still_requires_hitl(self):
        """A different Bash command should still require HITL."""
        handler, provider, _ = make_handler()
        handler._session_approved_tool_args.setdefault("Bash", set()).add("git status")
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Bash", {"command": "rm -rf /"}, None)
        assert isinstance(result, PermissionResultAllow)
        provider.post_interactive.assert_called()

    async def test_empty_bash_command_not_cached(self):
        """Empty command strings should not be cached."""
        handler, _, _ = make_handler()
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["Bash"]
        handler._batch.tool_inputs[batch_id] = [{"command": ""}]

        await handler.handle_action(
            value=f"approve_session:{batch_id}",
            user_id="U_TEST",
        )
        assert "Bash" not in handler._session_approved_tool_args

    async def test_empty_bash_command_not_matched(self):
        """An empty command should never match cached args."""
        handler, provider, _ = make_handler()
        handler._session_approved_tool_args.setdefault("Bash", set()).add("git status")
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Bash", {"command": ""}, None)
        assert isinstance(result, PermissionResultAllow)
        # Should have gone to HITL, not cache
        provider.post_interactive.assert_called()

    async def test_bash_command_exact_match_required(self):
        """Bash commands must match exactly — no prefix matching."""
        handler, provider, _ = make_handler()
        handler._session_approved_tool_args.setdefault("Bash", set()).add("git status")
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Bash", {"command": "git status --short"}, None)
        assert isinstance(result, PermissionResultAllow)
        # Different command → HITL
        provider.post_interactive.assert_called()

    async def test_mixed_batch_caches_write_tools_by_arg(self):
        """A batch with write-gated tools caches all by primary arg."""
        handler, _, _ = make_handler()
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["Edit", "Bash"]
        handler._batch.tool_inputs[batch_id] = [{"path": "/f"}, {"command": "make lint"}]

        await handler.handle_action(
            value=f"approve_session:{batch_id}",
            user_id="U_TEST",
        )
        # Both Edit and Bash use arg-based caching (write-gated)
        assert "Edit" not in handler._session_approved_tools
        assert "Bash" not in handler._session_approved_tools
        assert "/f" in handler._session_approved_tool_args.get("Edit", set())
        assert "make lint" in handler._session_approved_tool_args.get("Bash", set())

    async def test_non_write_tool_cached_by_name(self):
        """Non-write-gated tools are still cached by bare tool name."""
        handler, _, _ = make_handler()
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["CustomTool"]
        handler._batch.tool_inputs[batch_id] = [{"key": "val"}]

        await handler.handle_action(
            value=f"approve_session:{batch_id}",
            user_id="U_TEST",
        )
        assert "CustomTool" in handler._session_approved_tools

    async def test_mixed_write_and_non_write_batch(self):
        """Batch with write-gated and non-write-gated tools caches correctly."""
        handler, _, _ = make_handler()
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["Edit", "CustomTool", "Bash"]
        handler._batch.tool_inputs[batch_id] = [
            {"path": "/f"},
            {"key": "val"},
            {"command": "make lint"},
        ]

        await handler.handle_action(
            value=f"approve_session:{batch_id}",
            user_id="U_TEST",
        )
        # Write-gated: cached by arg
        assert "/f" in handler._session_approved_tool_args.get("Edit", set())
        assert "make lint" in handler._session_approved_tool_args.get("Bash", set())
        # Non-write-gated: cached by name
        assert "CustomTool" in handler._session_approved_tools
        # Write-gated tools must NOT be in bare name cache
        assert "Edit" not in handler._session_approved_tools
        assert "Bash" not in handler._session_approved_tools

    async def test_approve_session_resolves_bridge_for_write_tools(self):
        """approve_session for write tools should resolve bridge with session label."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["Bash"]
        handler._batch.tool_inputs[batch_id] = [{"command": "git status"}]
        handler._batch.message_ts[batch_id] = "1234.5678"

        await handler.handle_action(
            value=f"approve_session:{batch_id}",
            user_id="U_TEST",
        )
        fut = bridge.create_future("Bash")
        assert fut.done()
        info = fut.result()
        assert "approved for session" in info.label

    async def test_regular_approve_resolves_bridge_without_session(self):
        """Regular approve should resolve bridge with 'approved' label."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["Bash"]
        handler._batch.tool_inputs[batch_id] = [{"command": "git status"}]
        handler._batch.message_ts[batch_id] = "1234.5678"

        await handler.handle_action(
            value=f"approve:{batch_id}",
            user_id="U_TEST",
        )
        fut = bridge.create_future("Bash")
        assert fut.done()
        info = fut.result()
        assert info.label == "approved"
        assert "for session" not in info.label

    async def test_arg_cache_per_instance(self):
        """Arg caches should be per handler instance."""
        h1, _, _ = make_handler()
        h2, _, _ = make_handler()
        h1._session_approved_tool_args.setdefault("Bash", set()).add("git status")
        assert "Bash" not in h2._session_approved_tool_args

    async def test_bash_no_command_key_not_cached(self):
        """Bash with no 'command' key in input should not cache anything."""
        handler, _, _ = make_handler()
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["Bash"]
        handler._batch.tool_inputs[batch_id] = [{"description": "some desc"}]

        await handler.handle_action(
            value=f"approve_session:{batch_id}",
            user_id="U_TEST",
        )
        assert "Bash" not in handler._session_approved_tool_args

    async def test_edit_path_cached_as_arg(self):
        """Edit outside CWD should be cached by file path, not tool name."""
        handler, _, _ = make_handler()
        batch_id = "test-batch"
        event = asyncio.Event()
        handler._batch.events[batch_id] = event
        handler._batch.tool_names[batch_id] = ["Edit"]
        handler._batch.tool_inputs[batch_id] = [{"file_path": "/etc/config.ini"}]

        await handler.handle_action(
            value=f"approve_session:{batch_id}",
            user_id="U_TEST",
        )
        assert "Edit" not in handler._session_approved_tools
        assert "/etc/config.ini" in handler._session_approved_tool_args.get("Edit", set())

    async def test_cached_edit_path_auto_approved(self):
        """An Edit path in the arg cache should be auto-approved at step 2f."""
        handler, provider, _ = make_handler()
        handler._session_approved_tool_args.setdefault("Edit", set()).add("/etc/config.ini")
        result = await handler.handle("Edit", {"file_path": "/etc/config.ini"}, None)
        assert isinstance(result, PermissionResultAllow)
        provider.post_interactive.assert_not_called()

    async def test_long_bash_commands_no_truncation_collision(self):
        """Two long commands sharing first 120 chars must NOT collide in cache."""
        handler, provider, _ = make_handler()
        prefix = "x" * 120
        cmd_a = prefix + "_command_a"
        cmd_b = prefix + "_command_b"
        handler._session_approved_tool_args.setdefault("Bash", set()).add(cmd_a)
        # cmd_a should match
        result = await handler.handle("Bash", {"command": cmd_a}, None)
        assert isinstance(result, PermissionResultAllow)
        provider.post_interactive.assert_not_called()
        # cmd_b should NOT match (different full command)
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Bash", {"command": cmd_b}, None)
        assert isinstance(result, PermissionResultAllow)
        provider.post_interactive.assert_called()


class TestGoogleWorkspaceMCPReadToolsAutoApproved:
    """Google Workspace MCP read-only tools should be auto-approved without Slack prompt."""

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__workspace-default__get_gmail_message_content",
            "mcp__workspace-default__get_events",
            "mcp__workspace-default__get_drive_file_content",
            "mcp__workspace-default__list_calendars",
            "mcp__workspace-default__search_gmail_messages",
            "mcp__workspace-default__search_drive_files",
            "mcp__workspace-default__query_freebusy",
            "mcp__workspace-default__read_sheet_values",
            "mcp__workspace-default__check_drive_file_public_access",
            "mcp__workspace-default__inspect_doc_structure",
        ],
    )
    async def test_read_tool_auto_approved(self, tool_name):
        handler, provider, _ = make_handler()
        result = await handler.handle(tool_name, {}, None)
        assert isinstance(result, PermissionResultAllow)
        provider.post_interactive.assert_not_called()


class TestGoogleWorkspaceMCPWriteToolsRequireApproval:
    """Google Workspace MCP write tools require HITL approval via Slack."""

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__workspace-default__send_gmail_message",
            "mcp__workspace-default__manage_event",
            "mcp__workspace-default__create_drive_file",
            "mcp__workspace-default__create_drive_folder",
            "mcp__workspace-default__import_to_google_doc",
            "mcp__workspace-default__draft_gmail_message",
            "mcp__workspace-default__modify_gmail_message_labels",
            "mcp__workspace-default__update_drive_file",
            "mcp__workspace-default__set_drive_file_permissions",
            "mcp__workspace-default__manage_drive_access",
            "mcp__workspace-default__copy_drive_file",
            # Unknown (fail-closed: new tools default to requiring approval)
            pytest.param(
                "mcp__workspace-default__some_future_write_tool",
                id="unknown_tool_requires_approval",
            ),
        ],
    )
    async def test_requires_slack_approval(self, tool_name):
        handler, provider, _ = make_handler()
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle(tool_name, {}, None)
        assert isinstance(result, PermissionResultAllow)
        provider.post_interactive.assert_called()

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__workspace-default__send_gmail_message",
            "mcp__workspace-default__manage_event",
        ],
    )
    async def test_ignores_sdk_allow_suggestion(self, tool_name):
        """Write tools must require Slack approval even when SDK suggests allow."""
        handler, provider, _ = make_handler()
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))

        suggestion = MagicMock()
        suggestion.behavior = "allow"
        context = MagicMock()
        context.suggestions = [suggestion]

        result = await handler.handle(tool_name, {}, context)
        assert isinstance(result, PermissionResultAllow)
        provider.post_interactive.assert_called()


class TestGoogleWorkspaceMCPGuardTests:
    """Guard tests: pin prefix sets so changes aren't silently missed."""

    def test_prefix_pinned(self):
        from summon_claude.sessions.permissions import _GOOGLE_MCP_PREFIX

        assert _GOOGLE_MCP_PREFIX == "mcp__workspace-"

    def test_google_read_tool_prefixes_pinned(self):
        from summon_claude.sessions.permissions import _GOOGLE_READ_TOOL_PREFIXES

        assert _GOOGLE_READ_TOOL_PREFIXES == (
            "get_",
            "list_",
            "search_",
            "query_",
            "read_",
            "check_",
            "debug_",
            "inspect_",
        )

    def test_is_google_read_tool(self):
        from summon_claude.sessions.permissions import _is_google_read_tool

        # Read tools
        assert _is_google_read_tool("mcp__workspace-default__get_gmail_message") is True
        assert _is_google_read_tool("mcp__workspace-personal__list_calendars") is True
        assert _is_google_read_tool("mcp__workspace-work__search_drive_files") is True
        # Write tools
        assert _is_google_read_tool("mcp__workspace-default__send_gmail_message") is False
        assert _is_google_read_tool("mcp__workspace-work__manage_event") is False
        # Pathological
        assert _is_google_read_tool("mcp__workspace-personal__get_x__send_y") is True
        # Malformed
        assert _is_google_read_tool("malformed_tool_name") is False


class TestGoogleWriteToolNeverSessionCached:
    """End-to-end: Google write tools require HITL on every invocation (never session-cached)."""

    async def test_second_call_still_requires_hitl(self):
        """Calling handle() twice for a Google write tool should prompt HITL both times."""
        handler, provider, _ = make_handler()
        provider.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))

        tool = "mcp__workspace-default__send_gmail_message"

        # First call — requires HITL
        result1 = await handler.handle(tool, {}, None)
        assert isinstance(result1, PermissionResultAllow)
        assert provider.post_interactive.call_count == 1

        # Second call — must still require HITL (not session-cached)
        result2 = await handler.handle(tool, {}, None)
        assert isinstance(result2, PermissionResultAllow)
        assert provider.post_interactive.call_count == 2


class TestGoogleWriteToolCacheExclusion:
    """Defense-in-depth: _cache_session_approvals never caches Google write tools."""

    def test_cache_session_approvals_excludes_google_write_tools(self):
        """approve_session for a Google write tool must NOT populate either cache."""
        handler, _, _ = make_handler()
        batch_id = "test-batch"
        handler._batch.tool_names[batch_id] = ["mcp__workspace-default__send_gmail_message"]
        handler._batch.tool_inputs[batch_id] = [{"to": "user@example.com", "subject": "hi"}]
        handler._cache_session_approvals(batch_id)
        tool = "mcp__workspace-default__send_gmail_message"
        assert tool not in handler._session_approved_tools
        assert tool not in handler._session_approved_tool_args

    def test_cache_session_approvals_allows_google_read_tools(self):
        """approve_session for a Google read tool SHOULD populate bare-name cache."""
        handler, _, _ = make_handler()
        batch_id = "test-batch"
        handler._batch.tool_names[batch_id] = ["mcp__workspace-default__get_gmail_message"]
        handler._batch.tool_inputs[batch_id] = [{}]
        handler._cache_session_approvals(batch_id)
        assert "mcp__workspace-default__get_gmail_message" in handler._session_approved_tools


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


# ── Classifier integration ──────────────────────────────────────────────────


def _make_classifier_handler(classifier, classifier_configured=True, in_worktree=True):
    """Create a PermissionHandler with a mock classifier.

    Sets _in_worktree=True by default so classifier tests exercise the
    post-worktree flow. Pre-worktree behavior is tested separately.
    """
    client = make_mock_slack_client()
    router = ThreadRouter(client)
    config = make_config(debounce_ms=10)
    handler = PermissionHandler(
        router,
        config,
        authenticated_user_id="U_TEST",
        classifier=classifier,
        classifier_configured=classifier_configured,
    )
    if in_worktree:
        handler._in_containment = True
        handler._in_worktree = True
        handler._classifier_enabled = classifier_configured
        handler._write_access_granted = True
    return handler, client, router


class TestClassifierIntegration:
    async def test_classifier_allow_returns_allow(self):
        """Classifier allow -> PermissionResultAllow and records in _recent_approved."""
        from summon_claude.sessions.classifier import ClassifyResult

        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(return_value=ClassifyResult("allow", "safe to run"))
        handler, _, _ = _make_classifier_handler(mock_classifier)

        result = await handler.handle("Bash", {"command": "ls"}, None)
        assert isinstance(result, PermissionResultAllow)
        assert list(handler._recent_approved) == ["Bash"]

    async def test_classifier_block_returns_deny(self):
        """Classifier block -> PermissionResultDeny with generic message."""
        from summon_claude.sessions.classifier import ClassifyResult

        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(
            return_value=ClassifyResult("block", "dangerous operation")
        )
        handler, _, _ = _make_classifier_handler(mock_classifier)

        result = await handler.handle("Bash", {"command": "rm -rf /"}, None)
        assert isinstance(result, PermissionResultDeny)
        assert "Blocked by auto-mode policy" in result.message

    async def test_classifier_uncertain_falls_through(self):
        """Classifier uncertain -> falls through to SDK/HITL."""
        from summon_claude.sessions.classifier import ClassifyResult

        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(return_value=ClassifyResult("uncertain", "can't tell"))
        handler, _, _ = _make_classifier_handler(mock_classifier)

        handler._request_approval = AsyncMock(return_value=PermissionResultAllow())
        result = await handler.handle("Bash", {"command": "echo hi"}, None)
        mock_classifier.classify.assert_awaited_once()
        assert isinstance(result, PermissionResultAllow)

    async def test_classifier_not_run_pre_worktree(self):
        """Classifier does NOT run when not in worktree."""
        from summon_claude.sessions.classifier import ClassifyResult

        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(return_value=ClassifyResult("allow", "would allow"))
        handler, _, _ = _make_classifier_handler(
            mock_classifier, classifier_configured=True, in_worktree=False
        )

        # Patch _request_approval to avoid HITL hang
        handler._request_approval = AsyncMock(return_value=PermissionResultAllow())

        # Write gate will deny Bash pre-worktree — use a non-write-gated tool
        result = await handler.handle("mcp__custom__tool", {}, None)
        mock_classifier.classify.assert_not_called()
        assert isinstance(result, PermissionResultAllow)

    async def test_classifier_none_skips_classification(self):
        """No classifier -> goes straight to HITL without classifier call."""
        handler, _, _ = _make_classifier_handler(classifier=None, classifier_configured=False)

        handler._request_approval = AsyncMock(return_value=PermissionResultAllow())
        result = await handler.handle("Bash", {"command": "ls"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_github_require_approval_before_classifier(self):
        """_GITHUB_MCP_REQUIRE_APPROVAL checked before classifier."""
        from summon_claude.sessions.classifier import ClassifyResult

        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(return_value=ClassifyResult("allow", "would allow"))
        handler, _, _ = _make_classifier_handler(mock_classifier)

        restricted_tool = next(iter(_GITHUB_MCP_REQUIRE_APPROVAL))
        handler._request_approval = AsyncMock(return_value=PermissionResultAllow())
        await handler.handle(restricted_tool, {}, None)
        mock_classifier.classify.assert_not_called()

    async def test_set_classifier_enabled_false_skips(self):
        """Disabled classifier -> straight to HITL."""
        from summon_claude.sessions.classifier import ClassifyResult

        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(return_value=ClassifyResult("allow", "would allow"))
        handler, _, _ = _make_classifier_handler(mock_classifier, classifier_configured=False)

        handler._request_approval = AsyncMock(return_value=PermissionResultAllow())
        await handler.handle("Bash", {"command": "ls"}, None)
        mock_classifier.classify.assert_not_called()

    async def test_fallback_posts_notification(self):
        """Fallback exceeded -> notification posted to main channel."""
        from summon_claude.sessions.classifier import ClassifyResult

        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(
            return_value=ClassifyResult("fallback_exceeded", "too many blocks")
        )
        handler, _, router = _make_classifier_handler(mock_classifier)

        router.post_to_main = AsyncMock()
        handler._request_approval = AsyncMock(return_value=PermissionResultAllow())

        await handler.handle("Bash", {"command": "ls"}, None)

        assert handler.classifier_enabled is False
        router.post_to_main.assert_awaited_once()
        call_text = router.post_to_main.call_args[0][0]
        assert "paused" in call_text

    async def test_set_classifier_enabled_resets_counters(self):
        """Re-enabling classifier resets fallback counters."""
        mock_classifier = MagicMock()
        mock_classifier.reset_counters = MagicMock()
        handler, _, _ = _make_classifier_handler(mock_classifier)

        handler.set_classifier_enabled(True)
        mock_classifier.reset_counters.assert_called_once()

    async def test_notify_entered_worktree_enables_classifier(self, tmp_path: Path):
        """notify_entered_worktree enables classifier when configured."""
        mock_classifier = MagicMock()
        mock_classifier.reset_counters = MagicMock()
        handler, _, _ = _make_classifier_handler(
            mock_classifier, classifier_configured=True, in_worktree=False
        )
        # Set project_root so containment root can be computed (classifier
        # only activates after a valid containment root is established).
        handler._project_root = tmp_path
        wt_dir = tmp_path / ".claude" / "worktrees" / "test-wt"
        wt_dir.mkdir(parents=True)
        assert handler.classifier_enabled is False

        await handler.notify_entered_worktree("test-wt")
        assert handler.classifier_enabled is True
        mock_classifier.reset_counters.assert_called()

    async def test_notify_entered_worktree_does_not_enable_when_not_configured(
        self, tmp_path: Path
    ):
        """notify_entered_worktree does NOT enable classifier when not configured."""
        mock_classifier = MagicMock()
        handler, _, _ = _make_classifier_handler(
            mock_classifier, classifier_configured=False, in_worktree=False
        )
        # Set project_root and create worktree dir so containment_root IS set —
        # this isolates the classifier_configured=False guard as the reason
        # the classifier stays disabled, not a missing containment root.
        handler._project_root = tmp_path
        wt_dir = tmp_path / ".claude" / "worktrees" / "test-wt"
        wt_dir.mkdir(parents=True)

        await handler.notify_entered_worktree("test-wt")
        # Containment root is set (valid worktree), but classifier stays off
        # because classifier_configured=False.
        assert handler._containment_root is not None
        assert handler.classifier_enabled is False

    async def test_sdk_allow_suggestion_skipped_when_classifier_active(self):
        """Classifier active + SDK suggest allow -> HITL reached, SDK allow bypassed."""
        from summon_claude.sessions.classifier import ClassifyResult

        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(return_value=ClassifyResult("uncertain", "can't tell"))
        handler, _, _ = _make_classifier_handler(mock_classifier, classifier_configured=True)

        mock_suggestion = MagicMock()
        mock_suggestion.behavior = "allow"
        context = MagicMock()
        context.suggestions = [mock_suggestion]

        handler._request_approval = AsyncMock(return_value=PermissionResultAllow())

        # Use a non-write-gated tool to avoid write gate interference
        result = await handler.handle("mcp__custom__tool", {}, context)

        handler._request_approval.assert_awaited_once()
        assert isinstance(result, PermissionResultAllow)

    async def test_sdk_allow_suggestion_honored_when_classifier_inactive(self):
        """Classifier inactive -> SDK allow suggestion is honored (no HITL)."""
        mock_classifier = AsyncMock()
        handler, _, _ = _make_classifier_handler(
            mock_classifier, classifier_configured=False, in_worktree=False
        )

        mock_suggestion = MagicMock()
        mock_suggestion.behavior = "allow"
        context = MagicMock()
        context.suggestions = [mock_suggestion]

        handler._request_approval = AsyncMock(return_value=PermissionResultAllow())

        result = await handler.handle("mcp__custom__tool", {}, context)

        assert isinstance(result, PermissionResultAllow)
        handler._request_approval.assert_not_called()

    async def test_sdk_allow_suggestion_honored_after_fallback_exceeded(self):
        """After fallback_exceeded disables classifier, SDK allow is honored."""
        from summon_claude.sessions.classifier import ClassifyResult

        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(
            return_value=ClassifyResult("fallback_exceeded", "too many blocks")
        )
        handler, _, router = _make_classifier_handler(mock_classifier)
        router.post_to_main = AsyncMock()

        mock_suggestion = MagicMock()
        mock_suggestion.behavior = "allow"
        context = MagicMock()
        context.suggestions = [mock_suggestion]

        # First call triggers fallback — classifier_enabled becomes False
        handler._request_approval = AsyncMock(return_value=PermissionResultAllow())
        await handler.handle("mcp__custom__tool", {}, context)
        assert handler.classifier_enabled is False

        # Second call: classifier is disabled, SDK allow should be honored
        handler._request_approval.reset_mock()
        result = await handler.handle("mcp__custom__tool", {}, context)
        assert isinstance(result, PermissionResultAllow)
        handler._request_approval.assert_not_called()

    async def test_auto_on_rejected_pre_worktree(self):
        """set_classifier_enabled(True) is a no-op pre-worktree."""
        mock_classifier = MagicMock()
        handler, _, _ = _make_classifier_handler(
            mock_classifier, classifier_configured=True, in_worktree=False
        )

        handler.set_classifier_enabled(True)
        assert handler.classifier_enabled is False


class TestRecordContext:
    """Tests for PermissionHandler.record_context()."""

    def test_user_message_appended(self):
        handler, _, _ = _make_classifier_handler(classifier=None, classifier_configured=False)
        handler.record_context("user", "hello world")
        assert len(handler._context_history) == 1
        assert handler._context_history[0] == {"role": "user", "content": "hello world"}

    def test_tool_call_includes_name_and_input(self):
        handler, _, _ = _make_classifier_handler(classifier=None, classifier_configured=False)
        handler.record_context("tool_call", "", tool_name="Bash", tool_input={"command": "ls"})
        entry = handler._context_history[0]
        assert entry["role"] == "tool_call"
        assert entry["tool_name"] == "Bash"
        assert entry["tool_input"] == {"command": "ls"}

    def test_maxlen_evicts_old_entries(self):
        handler, _, _ = _make_classifier_handler(classifier=None, classifier_configured=False)
        for i in range(25):
            handler.record_context("user", f"msg {i}")
        assert len(handler._context_history) == 20
        assert handler._context_history[0]["content"] == "msg 5"

    def test_optional_fields_omitted_when_none(self):
        handler, _, _ = _make_classifier_handler(classifier=None, classifier_configured=False)
        handler.record_context("user", "test")
        entry = handler._context_history[0]
        assert "tool_name" not in entry
        assert "tool_input" not in entry

    async def test_auto_approved_tools_not_in_context_history(self):
        """Auto-approved tools (Read, Grep, etc.) should not appear in context history."""
        handler, _, _ = _make_classifier_handler(classifier=None, classifier_configured=False)
        await handler.handle("Read", {"file_path": "/tmp/test"}, None)
        assert len(handler._context_history) == 0

    async def test_recent_approved_passes_to_classifier(self):
        """Classifier receives _recent_approved list."""
        from summon_claude.sessions.classifier import ClassifyResult

        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(return_value=ClassifyResult("allow", "safe"))
        handler, _, _ = _make_classifier_handler(mock_classifier)

        # First call — empty approvals
        await handler.handle("Bash", {"command": "ls"}, None)
        first_call_approvals = mock_classifier.classify.call_args[1].get("recent_approvals", [])
        assert first_call_approvals == []

        # Second call — should include "Bash" from first approval
        await handler.handle("Bash", {"command": "pwd"}, None)
        second_call_approvals = mock_classifier.classify.call_args[1].get("recent_approvals", [])
        assert second_call_approvals == ["Bash"]


class TestClassifierFallbackTopicTransition:
    """Verify that fallback-pause produces a state change detectable by topic update logic."""

    async def test_fallback_changes_classifier_enabled_for_topic_detection(self):
        """After fallback, classifier_enabled=False triggers live_mode mismatch.

        _finalize_turn_result computes:
            live_mode = "[auto]" if classifier_enabled else "[manual]"
        Before fallback: classifier_enabled=True → live_mode="[auto]"
        After fallback:  classifier_enabled=False → live_mode="[manual]"
        The mismatch with _auto_mode_label="[auto]" triggers a topic update.
        """
        from summon_claude.sessions.classifier import ClassifyResult

        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(
            return_value=ClassifyResult("fallback_exceeded", "too many blocks")
        )
        handler, _, router = _make_classifier_handler(mock_classifier)
        router.post_to_main = AsyncMock()
        handler._request_approval = AsyncMock(return_value=PermissionResultAllow())

        # Before fallback: classifier is enabled
        assert handler.classifier_enabled is True
        assert handler.in_worktree is True

        # Trigger fallback via handle()
        await handler.handle("Bash", {"command": "ls"}, None)

        # After fallback: classifier is disabled
        assert handler.classifier_enabled is False

        # Simulate _finalize_turn_result's live_mode computation:
        # This is the exact expression from session.py
        live_mode = (
            ("[auto]" if handler.classifier_enabled else "[manual]")
            if handler.in_worktree
            else None
        )

        assert live_mode == "[manual]"
        # The _auto_mode_label would have been "[auto]" before fallback,
        # so live_mode != _auto_mode_label → topic update fires


class TestClassifierWriteGateOrdering:
    """Guard tests: write-gated tools cannot reach the classifier pre-worktree."""

    async def test_write_gated_tools_denied_before_classifier_pre_worktree(self):
        """Write-gated tools hit _check_write_gate (deny) before classifier at step 2g."""
        from summon_claude.sessions.classifier import ClassifyResult

        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(return_value=ClassifyResult("allow", "would allow"))
        handler, _, _ = _make_classifier_handler(
            mock_classifier, classifier_configured=True, in_worktree=False
        )

        for tool in _WRITE_GATED_TOOLS:
            result = await handler.handle(tool, {"command": "test", "file_path": "/tmp/x"}, None)
            assert isinstance(result, PermissionResultDeny), f"{tool} should be denied pre-worktree"

        mock_classifier.classify.assert_not_called()

    async def test_classifier_approve_short_circuits_before_sdk_allow(self):
        """Classifier-approved Bash at step 2g short-circuits before step 3 SDK allow."""
        from claude_agent_sdk import ToolPermissionContext

        from summon_claude.sessions.classifier import ClassifyResult

        mock_classifier = AsyncMock()
        mock_classifier.classify = AsyncMock(return_value=ClassifyResult("allow", "safe"))
        handler, _, _ = _make_classifier_handler(mock_classifier)

        # Mock HITL *before* the call so we can verify it wasn't reached
        handler._request_approval = AsyncMock(return_value=PermissionResultAllow())

        # Create a context with SDK allow suggestion
        mock_suggestion = MagicMock()
        mock_suggestion.behavior = "allow"
        mock_context = MagicMock(spec=ToolPermissionContext)
        mock_context.suggestions = [mock_suggestion]

        result = await handler.handle("Bash", {"command": "echo hi"}, mock_context)
        # Classifier approved at step 2g — should not reach HITL at step 4
        assert isinstance(result, PermissionResultAllow)
        mock_classifier.classify.assert_awaited_once()
        handler._request_approval.assert_not_called()


class TestJiraMCPGuardTests:
    """Guard tests: pin Jira permission sets (mirrors TestGitHubMCPGuardTests)."""

    def test_hard_deny_set_pinned(self):
        assert (
            frozenset(
                {
                    "mcp__jira__addCommentToJiraIssue",
                    "mcp__jira__addWorklogToJiraIssue",
                    "mcp__jira__createConfluenceFooterComment",
                    "mcp__jira__createConfluenceInlineComment",
                    "mcp__jira__createConfluencePage",
                    "mcp__jira__createIssueLink",
                    "mcp__jira__createJiraIssue",
                    "mcp__jira__editJiraIssue",
                    "mcp__jira__fetchAtlassian",
                    "mcp__jira__transitionJiraIssue",
                    "mcp__jira__updateConfluencePage",
                }
            )
            == _JIRA_MCP_HARD_DENY
        )

    def test_auto_approve_prefixes_pinned(self):
        assert _JIRA_MCP_AUTO_APPROVE_PREFIXES == (
            "mcp__jira__get",
            "mcp__jira__search",
            "mcp__jira__lookup",
        )

    def test_auto_approve_exact_pinned(self):
        assert frozenset({"mcp__jira__atlassianUserInfo"}) == _JIRA_MCP_AUTO_APPROVE_EXACT

    def test_no_hard_deny_matches_auto_approve_prefix(self):
        for tool in _JIRA_MCP_HARD_DENY:
            assert not tool.startswith(_JIRA_MCP_AUTO_APPROVE_PREFIXES), (
                f"Hard-deny tool '{tool}' matches an auto-approve prefix"
            )

    def test_all_prefixes_start_with_jira_prefix(self):
        for prefix in _JIRA_MCP_AUTO_APPROVE_PREFIXES:
            assert prefix.startswith(_JIRA_MCP_PREFIX)


class TestLabelConstants:
    """Guard: approval label constants must remain distinct."""

    def test_denied_labels_are_distinct(self):
        """_LABEL_DENIED and _LABEL_USER_DENIED must differ to distinguish system vs user denial."""
        from summon_claude.sessions.permissions import _LABEL_DENIED, _LABEL_USER_DENIED

        assert _LABEL_DENIED != _LABEL_USER_DENIED

    def test_sdk_denied_label_pinned(self):
        """Guard: _LABEL_SDK_DENIED value is user-visible in Slack — pin it."""
        from summon_claude.sessions.permissions import _LABEL_SDK_DENIED

        assert _LABEL_SDK_DENIED == "sdk-denied"


class TestApprovalBridge:
    """Tests for the ApprovalBridge two-sided rendezvous."""

    async def test_streamer_first_ordering(self):
        """create_future then resolve — Future resolves with expected info."""
        bridge = ApprovalBridge()
        fut = bridge.create_future("Read")
        info = ApprovalInfo(label="auto-allowed")
        bridge.resolve("Read", info)
        result = await fut
        assert result.label == "auto-allowed"
        assert result.is_denial is False

    async def test_handler_first_ordering(self):
        """resolve before create_future — returned Future is already resolved."""
        bridge = ApprovalBridge()
        info = ApprovalInfo(label="sdk-allowed")
        bridge.resolve("Read", info)
        fut = bridge.create_future("Read")
        assert fut.done()
        assert fut.result().label == "sdk-allowed"

    async def test_fifo_ordering(self):
        """Two create_future, two resolve — first goes to first, second to second."""
        bridge = ApprovalBridge()
        fut1 = bridge.create_future("Read")
        fut2 = bridge.create_future("Read")
        bridge.resolve("Read", ApprovalInfo(label="first"))
        bridge.resolve("Read", ApprovalInfo(label="second"))
        assert (await fut1).label == "first"
        assert (await fut2).label == "second"

    async def test_cleanup_on_empty(self):
        """After all Futures resolved, no leftover keys in internal dicts."""
        bridge = ApprovalBridge()
        fut = bridge.create_future("Read")
        bridge.resolve("Read", ApprovalInfo(label="done"))
        await fut
        assert "Read" not in bridge._pending
        assert "Read" not in bridge._resolved

    async def test_different_tool_names(self):
        """Futures for different tool names don't interfere."""
        bridge = ApprovalBridge()
        fut_r = bridge.create_future("Read")
        fut_w = bridge.create_future("Write")
        bridge.resolve("Write", ApprovalInfo(label="write-ok"))
        bridge.resolve("Read", ApprovalInfo(label="read-ok"))
        assert (await fut_r).label == "read-ok"
        assert (await fut_w).label == "write-ok"

    async def test_clear_cancels_pending_futures(self):
        """clear() cancels all pending Futures and empties dicts."""
        bridge = ApprovalBridge()
        fut = bridge.create_future("Read")
        bridge.clear()
        assert fut.cancelled()
        assert len(bridge._pending) == 0
        assert len(bridge._resolved) == 0

    async def test_clear_resets_resolved(self):
        """clear() removes pre-resolved entries; new create_future creates a pending Future."""
        bridge = ApprovalBridge()
        bridge.resolve("Read", ApprovalInfo(label="stale"))
        bridge.clear()
        assert len(bridge._resolved) == 0
        fut = bridge.create_future("Read")
        assert not fut.done()  # New pending Future, not the pre-resolved one

    async def test_resolve_after_clear_deposits_into_resolved(self):
        """resolve() after clear() deposits into _resolved; next create_future picks it up."""
        bridge = ApprovalBridge()
        bridge.create_future("Read")  # seed a pending entry so clear() has something to cancel
        bridge.clear()
        # Post-clear resolve goes into _resolved
        bridge.resolve("Read", ApprovalInfo(label="post-clear"))
        # Next create_future picks up the post-clear entry
        fut = bridge.create_future("Read")
        assert fut.done()
        assert fut.result().label == "post-clear"


class TestApprovalBridgeResolution:
    """Tests that _resolve_approval fires at key decision points."""

    async def test_auto_allow_resolves_bridge(self):
        """Static allowlist (Read) resolves bridge with 'auto-allowed'."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        result = await handler.handle("Read", {}, None)
        assert isinstance(result, PermissionResultAllow)
        fut = bridge.create_future("Read")
        assert fut.done()
        assert fut.result().label == "auto-allowed"

    async def test_session_cache_resolves_bridge(self):
        """Session-cached tool resolves bridge with 'session-cached'."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        handler._session_approved_tools.add("Agent")
        result = await handler.handle("Agent", {}, None)
        assert isinstance(result, PermissionResultAllow)
        fut = bridge.create_future("Agent")
        assert fut.done()
        assert fut.result().label == "session-cached"

    async def test_sdk_deny_resolves_bridge(self):
        """SDK deny resolves bridge with denial label."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        mock_ctx = MagicMock()
        mock_suggestion = MagicMock()
        mock_suggestion.behavior = "deny"
        mock_ctx.suggestions = [mock_suggestion]
        result = await handler.handle("SomeTool", {}, mock_ctx)
        assert isinstance(result, PermissionResultDeny)
        fut = bridge.create_future("SomeTool")
        assert fut.done()
        assert fut.result().is_denial is True
        assert fut.result().label == "sdk-denied"

    async def test_hitl_deny_resolves_bridge(self):
        """HITL deny via handle_action resolves bridge with denial label."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        batch_id = "test-deny"
        handler._batch.events[batch_id] = asyncio.Event()
        handler._batch.tool_names[batch_id] = ["Write"]
        handler._batch.tool_inputs[batch_id] = [{}]
        handler._batch.message_ts[batch_id] = "1234.5678"

        await handler.handle_action(value=f"deny:{batch_id}", user_id="U_TEST")
        fut = bridge.create_future("Write")
        assert fut.done()
        info = fut.result()
        assert info.is_denial is True
        assert info.label == "user-denied"

    async def test_no_bridge_does_not_error(self):
        """Handler without bridge (bridge=None) works without errors."""
        handler, _, _ = make_handler(bridge=None)
        result = await handler.handle("Read", {}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_classifier_allow_resolves_bridge(self):
        """Classifier allow resolves bridge with 'auto-mode' and reason."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        handler._classifier_enabled = True
        handler._in_worktree = True
        mock_classifier = AsyncMock()
        mock_result = MagicMock()
        mock_result.decision = "allow"
        mock_result.reason = "safe read operation"
        mock_classifier.classify = AsyncMock(return_value=mock_result)
        handler._classifier = mock_classifier
        result = await handler.handle("Agent", {}, None)
        assert isinstance(result, PermissionResultAllow)
        fut = bridge.create_future("Agent")
        assert fut.done()
        info = fut.result()
        assert info.label == "auto-mode"
        assert info.reason == "safe read operation"

    async def test_classifier_block_resolves_bridge(self):
        """Classifier block resolves bridge with 'blocked' and denial flag."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        handler._classifier_enabled = True
        handler._in_worktree = True
        mock_classifier = AsyncMock()
        mock_result = MagicMock()
        mock_result.decision = "block"
        mock_result.reason = "dangerous operation"
        mock_classifier.classify = AsyncMock(return_value=mock_result)
        handler._classifier = mock_classifier
        result = await handler.handle("Agent", {}, None)
        assert isinstance(result, PermissionResultDeny)
        fut = bridge.create_future("Agent")
        assert fut.done()
        info = fut.result()
        assert info.label == "auto-mode blocked"
        assert info.is_denial is True

    async def test_write_gate_containment_resolves_bridge(self, tmp_path):
        """Write within containment resolves bridge with 'within project'."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "src").mkdir()

        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        # Re-enable real write gate (make_handler mocks it out)
        handler._check_write_gate = PermissionHandler._check_write_gate.__get__(
            handler, PermissionHandler
        )
        handler._in_containment = True
        handler._write_access_granted = True
        handler._containment_root = project.resolve()
        file_path = str((project / "src" / "foo.py").resolve())
        result = await handler.handle("Write", {"file_path": file_path}, None)
        assert isinstance(result, PermissionResultAllow)
        fut = bridge.create_future("Write")
        assert fut.done()
        assert fut.result().label == "within project"

    async def test_write_gate_safe_dir_resolves_bridge(self, tmp_path):
        """Write to safe dir resolves bridge with 'auto-allowed'."""
        project = tmp_path / "project"
        project.mkdir()
        hack_dir = project / "hack"
        hack_dir.mkdir()

        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        handler._check_write_gate = PermissionHandler._check_write_gate.__get__(
            handler, PermissionHandler
        )
        handler._project_root = project.resolve()
        handler._safe_dirs = [str(hack_dir.resolve())]
        file_path = str((hack_dir / "notes.md").resolve())
        result = await handler.handle("Write", {"file_path": file_path}, None)
        assert isinstance(result, PermissionResultAllow)
        fut = bridge.create_future("Write")
        assert fut.done()
        assert fut.result().label == "auto-allowed"

    async def test_write_gate_sdk_deny_resolves_bridge(self):
        """SDK deny inside _check_write_gate resolves bridge with denial label."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        # make_handler stubs _check_write_gate; restore real impl to test SDK-deny path
        handler._check_write_gate = PermissionHandler._check_write_gate.__get__(
            handler, PermissionHandler
        )
        mock_suggestion = MagicMock()
        mock_suggestion.behavior = "deny"
        mock_ctx = MagicMock()
        mock_ctx.suggestions = [mock_suggestion]
        result = await handler.handle("Write", {"file_path": "/f"}, mock_ctx)
        assert isinstance(result, PermissionResultDeny)
        fut = bridge.create_future("Write")
        assert fut.done()
        info = fut.result()
        assert info.is_denial is True
        assert info.label == "sdk-denied"

    async def test_hitl_approve_resolves_bridge(self):
        """HITL approval resolves bridge with 'approved' label."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        batch_id = "test-approve"
        handler._batch.events[batch_id] = asyncio.Event()
        handler._batch.tool_names[batch_id] = ["Edit"]
        handler._batch.tool_inputs[batch_id] = [{"file_path": "/f"}]
        handler._batch.message_ts[batch_id] = "1234.5678"
        await handler.handle_action(value=f"approve:{batch_id}", user_id="U_TEST")
        fut = bridge.create_future("Edit")
        assert fut.done()
        info = fut.result()
        assert info.label == "approved"
        assert info.is_denial is False

    async def test_request_approval_timeout_resolves_bridge(self):
        """_request_approval TimeoutError path resolves bridge with denial."""
        from unittest.mock import patch

        class _ImmediateTimeout:
            async def __aenter__(self):
                raise TimeoutError

            async def __aexit__(self, *args):
                return False

        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)

        with patch(
            "summon_claude.sessions.permissions.asyncio.timeout",
            return_value=_ImmediateTimeout(),
        ):
            result = await handler.handle("CustomTool", {"key": "val"}, None)

        assert isinstance(result, PermissionResultDeny)
        fut = bridge.create_future("CustomTool")
        assert fut.done()
        info = fut.result()
        assert info.is_denial is True
        assert info.reason == "timed out"

    async def test_timeout_message_contains_display_format(self):
        """Deny message uses _timeout_display — sub-minute shows seconds."""
        from unittest.mock import patch

        class _ImmediateTimeout:
            async def __aenter__(self):
                raise TimeoutError

            async def __aexit__(self, *args):
                return False

        handler, _, _ = make_handler()
        handler._timeout_s = 45

        with patch(
            "summon_claude.sessions.permissions.asyncio.timeout",
            return_value=_ImmediateTimeout(),
        ):
            result = await handler.handle("CustomTool", {"key": "val"}, None)

        assert isinstance(result, PermissionResultDeny)
        assert "45s" in result.message

    async def test_post_approval_message_exception_resolves_bridge(self):
        """When post_interactive raises, bridge resolves with denial."""
        bridge = ApprovalBridge()
        handler, provider, _ = make_handler(bridge=bridge)
        provider.post_interactive = AsyncMock(side_effect=RuntimeError("slack down"))

        result = await handler.handle("CustomTool", {"key": "val"}, None)

        assert isinstance(result, PermissionResultDeny)
        fut = bridge.create_future("CustomTool")
        assert fut.done()
        info = fut.result()
        assert info.is_denial is True
        assert info.reason == "internal error"

    async def test_sdk_allowed_resolves_bridge(self):
        """Step 3 SDK allow resolves bridge with 'sdk-allowed' label."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)

        mock_suggestion = MagicMock()
        mock_suggestion.behavior = "allow"
        mock_ctx = MagicMock()
        mock_ctx.suggestions = [mock_suggestion]

        result = await handler.handle("CustomTool", {"key": "val"}, mock_ctx)

        assert isinstance(result, PermissionResultAllow)
        fut = bridge.create_future("CustomTool")
        assert fut.done()
        info = fut.result()
        assert info.label == "sdk-allowed"
        assert info.is_denial is False

    async def test_ask_user_question_resolves_bridge_with_answered(self):
        """AskUserQuestion success resolves bridge with 'answered' label."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        # Mock _handle_ask_user_question to return allow without Slack interaction
        handler._handle_ask_user_question = AsyncMock(return_value=PermissionResultAllow())

        result = await handler.handle("AskUserQuestion", {"questions": []}, None)

        assert isinstance(result, PermissionResultAllow)
        fut = bridge.create_future("AskUserQuestion")
        assert fut.done()
        info = fut.result()
        assert info.label == "answered"
        assert info.is_denial is False

    async def test_ask_user_question_deny_resolves_bridge_with_denied(self):
        """AskUserQuestion failure resolves bridge with denial — prevents timeout hang."""
        bridge = ApprovalBridge()
        handler, _, _ = make_handler(bridge=bridge)
        handler._handle_ask_user_question = AsyncMock(
            return_value=PermissionResultDeny(message="question timed out")
        )

        result = await handler.handle("AskUserQuestion", {"questions": []}, None)

        assert isinstance(result, PermissionResultDeny)
        fut = bridge.create_future("AskUserQuestion")
        assert fut.done()
        info = fut.result()
        assert info.label == "denied"
        assert info.reason == "question failed"
        assert info.is_denial is True

    async def test_request_and_batch_timeout_both_resolve_bridge(self):
        """Both per-request and batch timeouts deposit bridge resolve entries."""
        bridge = ApprovalBridge()
        handler, provider, _ = make_handler(bridge=bridge)
        provider.post_interactive = AsyncMock(return_value=MagicMock(ts="mock_ts"))
        handler._timeout_s = 1  # Short timeout

        result = await handler.handle("CustomTool", {"key": "val"}, None)
        assert isinstance(result, PermissionResultDeny)

        # Per-request timeout (_request_approval:906) fires first → resolve #1
        fut1 = bridge.create_future("CustomTool")
        assert fut1.done()
        assert fut1.result().is_denial is True
        assert fut1.result().reason == "timed out"

        # Yield to let batch timeout (_debounce_and_post:944) fire → resolve #2
        await asyncio.sleep(0.05)
        fut2 = bridge.create_future("CustomTool")
        assert fut2.done()
        info2 = fut2.result()
        assert info2.is_denial is True
        assert info2.reason == "timed out"

    async def test_write_gated_fallthrough_skips_sdk_allowed(self):
        """SDK-allow for write-gated tool with containment goes to HITL, not sdk-allowed."""
        bridge = ApprovalBridge()
        handler, provider, _ = make_handler(bridge=bridge)
        provider.post_interactive = AsyncMock(return_value=MagicMock(ts="mock_ts"))
        handler._timeout_s = 1  # Short timeout to avoid hanging

        # Enable containment so write gate passes but _write_gated_fallthrough is True
        handler._check_write_gate = PermissionHandler._check_write_gate.__get__(
            handler, PermissionHandler
        )
        handler._in_containment = True
        handler._write_access_granted = True

        mock_suggestion = MagicMock()
        mock_suggestion.behavior = "allow"
        mock_ctx = MagicMock()
        mock_ctx.suggestions = [mock_suggestion]

        result = await handler.handle("Bash", {"command": "git status"}, mock_ctx)

        # Should NOT be auto-allowed — should go to HITL and timeout
        assert isinstance(result, PermissionResultDeny)
        fut = bridge.create_future("Bash")
        assert fut.done()
        info = fut.result()
        # Should NOT be "sdk-allowed" — the fallthrough prevents SDK allow
        assert info.label != "sdk-allowed"
        assert info.is_denial is True


class TestTimeoutDisplay:
    """Tests for _timeout_display property."""

    @pytest.mark.parametrize(
        "timeout_s,expected",
        [
            (None, "0 minutes"),
            (30, "30s"),
            (60, "1 minute"),
            (90, "1 minute"),
            (120, "2 minutes"),
            (900, "15 minutes"),
        ],
    )
    def test_timeout_display(self, timeout_s, expected):
        handler, _, _ = make_handler()
        handler._timeout_s = timeout_s
        assert handler._timeout_display == expected
