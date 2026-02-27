"""Tests for ThreadRouter routing logic."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from helpers import make_mock_provider
from summon_claude.providers.base import MessageRef
from summon_claude.thread_router import ThreadRouter


class TestThreadRouterInit:
    """ThreadRouter initialization tests."""

    def test_init_sets_channel_id(self):
        """ThreadRouter should store the channel_id."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")
        assert router.channel_id == "C123"

    def test_init_no_current_turn(self):
        """ThreadRouter should start with no current turn."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")
        # Access private state for testing initialization
        assert router._current_turn_ts is None
        assert router._current_turn_number == 0


class TestThreadRouterStartTurn:
    """ThreadRouter.start_turn tests."""

    async def test_start_turn_creates_message(self):
        """start_turn should create a turn thread starter message."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        ts = await router.start_turn(1)

        assert ts == "1234567890.123456"
        provider.post_message.assert_called_once()
        call_args = provider.post_message.call_args
        assert call_args[0][0] == "C123"
        assert "Turn 1" in call_args[0][1]

    async def test_start_turn_resets_tool_count(self):
        """start_turn should reset tool call count."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        # Record some tool calls
        router.record_tool_call("Read", {"file_path": "/src/main.py"})
        router.record_tool_call("Edit", {"path": "/src/main.py"})

        # Start a new turn
        await router.start_turn(2)

        # Tool count should be reset
        assert router._tool_call_count == 0

    async def test_start_turn_resets_files_touched(self):
        """start_turn should reset files touched."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        # Record some file touches
        router.record_tool_call("Read", {"file_path": "/src/main.py"})

        # Start a new turn
        await router.start_turn(2)

        # Files should be cleared
        assert router._files_touched == []

    async def test_start_turn_increments_turn_number(self):
        """start_turn should set the turn number."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.start_turn(5)

        assert router._current_turn_number == 5


class TestThreadRouterUpdateTurnSummary:
    """ThreadRouter.update_turn_summary tests."""

    async def test_update_turn_summary_updates_message(self):
        """update_turn_summary should update the turn starter message."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.start_turn(1)
        await router.update_turn_summary("2 tool calls · config.py · +1 more")

        provider.update_message.assert_called_once()
        call_args = provider.update_message.call_args
        assert call_args[0][0] == "C123"
        assert "Turn 1" in call_args[0][2]
        assert "2 tool calls · config.py · +1 more" in call_args[0][2]

    async def test_update_turn_summary_no_op_when_no_turn(self):
        """update_turn_summary should not crash if no turn exists."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        # Should not raise
        await router.update_turn_summary("summary")

        provider.update_message.assert_not_called()


class TestThreadRouterStartSubagentThread:
    """ThreadRouter.start_subagent_thread tests."""

    async def test_start_subagent_thread_creates_message(self):
        """start_subagent_thread should create a subagent thread."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        ts = await router.start_subagent_thread("task_123", "Running analysis")

        assert ts == "1234567890.123456"
        provider.post_message.assert_called_once()
        call_args = provider.post_message.call_args
        assert "Subagent" in call_args[0][1]
        assert "Running analysis" in call_args[0][1]

    async def test_start_subagent_thread_tracks_by_tool_id(self):
        """start_subagent_thread should track thread by tool_use_id."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        ts = await router.start_subagent_thread("task_123", "Description")

        assert router._subagent_threads["task_123"] == ts


class TestThreadRouterPostToMain:
    """ThreadRouter.post_to_main tests."""

    async def test_post_to_main_calls_provider(self):
        """post_to_main should post without thread_ts."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.post_to_main("Hello world")

        provider.post_message.assert_called_once()
        call_args = provider.post_message.call_args
        assert call_args[0][0] == "C123"
        assert call_args[0][1] == "Hello world"
        # Should not have thread_ts
        assert call_args[1].get("thread_ts") is None

    async def test_post_to_main_with_blocks(self):
        """post_to_main should pass blocks to provider."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        blocks = [{"type": "divider"}]
        await router.post_to_main("text", blocks=blocks)

        call_args = provider.post_message.call_args
        assert call_args[1]["blocks"] == blocks


class TestThreadRouterPostToTurnThread:
    """ThreadRouter.post_to_turn_thread tests."""

    async def test_post_to_turn_thread_with_active_turn(self):
        """post_to_turn_thread should post to turn thread when active."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.start_turn(1)
        await router.post_to_turn_thread("Reply in thread")

        call_args = provider.post_message.call_args
        assert call_args[1]["thread_ts"] == "1234567890.123456"

    async def test_post_to_turn_thread_falls_back_to_main(self):
        """post_to_turn_thread should fall back to main when no turn."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.post_to_turn_thread("Text")

        call_args = provider.post_message.call_args
        assert call_args[1].get("thread_ts") is None

    async def test_post_to_turn_thread_returns_message_ref(self):
        """post_to_turn_thread should return MessageRef."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.start_turn(1)
        ref = await router.post_to_turn_thread("Text")

        assert isinstance(ref, MessageRef)
        assert ref.ts == "1234567890.123456"


class TestThreadRouterPostToSubagentThread:
    """ThreadRouter.post_to_subagent_thread tests."""

    async def test_post_to_subagent_thread_with_matching_id(self):
        """post_to_subagent_thread should post to subagent thread when id matches."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.start_subagent_thread("task_123", "Description")
        await router.post_to_subagent_thread("task_123", "Subagent response")

        call_args = provider.post_message.call_args
        assert call_args[1]["thread_ts"] == "1234567890.123456"

    async def test_post_to_subagent_thread_falls_back_to_turn(self):
        """post_to_subagent_thread should fall back to turn thread when id not found."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.start_turn(1)
        await router.post_to_subagent_thread("unknown_id", "Text")

        call_args = provider.post_message.call_args
        assert call_args[1]["thread_ts"] == "1234567890.123456"


class TestThreadRouterPostPermission:
    """ThreadRouter.post_permission tests."""

    async def test_post_permission_in_thread_includes_reply_broadcast(self):
        """post_permission should include reply_broadcast=True when in thread."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.start_turn(1)
        await router.post_permission("Approve this action?", [])

        call_args = provider.post_message.call_args
        assert call_args[1]["reply_broadcast"] is True

    async def test_post_permission_in_thread_includes_channel_mention(self):
        """post_permission should prepend <!channel> when in thread."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.start_turn(1)
        await router.post_permission("Approve?", [])

        call_args = provider.post_message.call_args
        text = call_args[0][1]
        assert text.startswith("<!channel>")

    async def test_post_permission_not_in_thread_no_broadcast(self):
        """post_permission should not set reply_broadcast when no thread."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.post_permission("Approve?", [])

        call_args = provider.post_message.call_args
        assert call_args[1]["reply_broadcast"] is False

    async def test_post_permission_with_explicit_thread_ts(self):
        """post_permission should use explicit thread_ts if provided."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.post_permission("Approve?", [], thread_ts="999.001")

        call_args = provider.post_message.call_args
        assert call_args[1]["thread_ts"] == "999.001"
        assert call_args[1]["reply_broadcast"] is True


class TestThreadRouterRecordToolCall:
    """ThreadRouter.record_tool_call tests."""

    def test_record_tool_call_increments_count(self):
        """record_tool_call should increment tool call count."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        router.record_tool_call("Read", {"file_path": "/src/main.py"})
        assert router._tool_call_count == 1

        router.record_tool_call("Edit", {"path": "/src/config.py"})
        assert router._tool_call_count == 2

    def test_record_tool_call_extracts_file_paths(self):
        """record_tool_call should extract file paths from tool input."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        router.record_tool_call("Read", {"file_path": "/src/main.py"})
        router.record_tool_call("Edit", {"path": "/src/config.py"})

        assert "/src/main.py" in router._files_touched
        assert "/src/config.py" in router._files_touched

    def test_record_tool_call_deduplicates_files(self):
        """record_tool_call should not duplicate file paths."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        router.record_tool_call("Read", {"file_path": "/src/main.py"})
        router.record_tool_call("Read", {"file_path": "/src/main.py"})

        assert router._files_touched.count("/src/main.py") == 1

    def test_record_tool_call_ignores_non_path_keys(self):
        """record_tool_call should ignore input without path keys."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        router.record_tool_call("Bash", {"command": "git status"})

        assert len(router._files_touched) == 0

    def test_record_tool_call_extracts_command_path(self):
        """record_tool_call should extract 'command' key if it looks like a path."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        router.record_tool_call("Bash", {"command": "/usr/bin/git"})

        assert "/usr/bin/git" in router._files_touched


class TestThreadRouterGenerateTurnSummary:
    """ThreadRouter.generate_turn_summary tests."""

    def test_generate_turn_summary_includes_tool_count(self):
        """generate_turn_summary should include tool call count."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        router.record_tool_call("Read", {"file_path": "/src/main.py"})
        router.record_tool_call("Edit", {"path": "/src/config.py"})

        summary = router.generate_turn_summary()
        assert "2 tool calls" in summary

    def test_generate_turn_summary_singular_tool(self):
        """generate_turn_summary should use singular for 1 tool."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        router.record_tool_call("Read", {"file_path": "/src/main.py"})

        summary = router.generate_turn_summary()
        assert "1 tool call" in summary

    def test_generate_turn_summary_includes_file_names(self):
        """generate_turn_summary should include file names."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        router.record_tool_call("Read", {"file_path": "/src/main.py"})
        router.record_tool_call("Edit", {"path": "/src/config.py"})

        summary = router.generate_turn_summary()
        assert "main.py" in summary
        assert "config.py" in summary

    def test_generate_turn_summary_limits_files_to_3(self):
        """generate_turn_summary should show max 3 files + '+N more'."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        for i in range(5):
            router.record_tool_call("Read", {"file_path": f"/src/file{i}.py"})

        summary = router.generate_turn_summary()
        assert "+2 more" in summary

    def test_generate_turn_summary_no_tools(self):
        """generate_turn_summary should return 'Processing...' when no tools."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        summary = router.generate_turn_summary()
        assert summary == "Processing..."

    def test_generate_turn_summary_uses_separator(self):
        """generate_turn_summary should separate parts with ·."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        router.record_tool_call("Read", {"file_path": "/src/main.py"})
        router.record_tool_call("Edit", {"path": "/src/config.py"})

        summary = router.generate_turn_summary()
        assert " · " in summary


class TestThreadRouterUploadToTurnThread:
    """ThreadRouter.upload_to_turn_thread tests."""

    async def test_upload_to_turn_thread_delegates_to_provider(self):
        """upload_to_turn_thread should delegate to provider.upload_file."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.start_turn(1)
        await router.upload_to_turn_thread("file content", "output.txt")

        provider.upload_file.assert_called_once()
        call_args = provider.upload_file.call_args
        assert call_args[0][0] == "C123"
        assert call_args[0][1] == "file content"
        assert call_args[0][2] == "output.txt"
        assert call_args[1]["thread_ts"] == "1234567890.123456"

    async def test_upload_to_turn_thread_with_title(self):
        """upload_to_turn_thread should pass title to provider."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.start_turn(1)
        await router.upload_to_turn_thread("content", "file.txt", title="Custom Title")

        call_args = provider.upload_file.call_args
        assert call_args[1]["title"] == "Custom Title"


class TestThreadRouterDelegationMethods:
    """ThreadRouter delegation methods tests."""

    async def test_update_message_delegates(self):
        """update_message should delegate to provider."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.update_message("C456", "999.001", "Updated")

        provider.update_message.assert_called_once()
        call_args = provider.update_message.call_args
        assert call_args[0][0] == "C456"
        assert call_args[0][1] == "999.001"
        assert call_args[0][2] == "Updated"

    async def test_add_reaction_delegates(self):
        """add_reaction should delegate to provider."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        await router.add_reaction("C456", "999.001", ":thumbsup:")

        provider.add_reaction.assert_called_once()
        call_args = provider.add_reaction.call_args
        assert call_args[0][0] == "C456"
        assert call_args[0][1] == "999.001"
        assert call_args[0][2] == ":thumbsup:"


class TestThreadRouterPostPermissionEphemeral:
    """ThreadRouter.post_permission_ephemeral tests."""

    async def test_post_permission_ephemeral_calls_provider(self):
        """post_permission_ephemeral should call provider.post_ephemeral."""
        provider = make_mock_provider()
        router = ThreadRouter(provider, "C123")

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "Approve?"}}]
        await router.post_permission_ephemeral("U456", "Permission needed", blocks)

        provider.post_ephemeral.assert_called_once()
        call_args = provider.post_ephemeral.call_args
        assert call_args[0][0] == "C123"
        assert call_args[0][1] == "U456"
        assert call_args[0][2] == "Permission needed"
        assert call_args[1]["blocks"] == blocks
