"""Tests for summon_claude.streamer — now uses ThreadRouter."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import RateLimitInfo

from helpers import make_mock_slack_client
from summon_claude.sessions.permissions import ApprovalBridge, ApprovalInfo
from summon_claude.sessions.response import (
    ResponseStreamer,
    _format_tool_result,
    _format_tool_summary,
    _sanitize_approval_reason,
)
from summon_claude.sessions.response import split_text as _split_text
from summon_claude.slack.router import ThreadRouter


def make_streamer(
    *,
    show_thinking: bool = False,
    max_inline_chars: int = 2500,
    team_id: str | None = None,
    user_id: str | None = None,
) -> tuple[ResponseStreamer, ThreadRouter, AsyncMock]:
    """Create a ResponseStreamer with a mocked SlackClient."""
    client = make_mock_slack_client()
    router = ThreadRouter(client)
    streamer = ResponseStreamer(
        router,
        show_thinking=show_thinking,
        max_inline_chars=max_inline_chars,
        user_id=user_id,
        team_id=team_id,
    )
    return streamer, router, client


def make_text_block(text: str) -> TextBlock:
    return TextBlock(text=text)


def make_tool_use_block(name: str, input_data: dict, tool_use_id: str = "tu_1") -> ToolUseBlock:
    return ToolUseBlock(id=tool_use_id, name=name, input=input_data)


def make_assistant_message(content: list) -> AssistantMessage:
    return AssistantMessage(content=content, model="claude-opus-4-6")


def make_result_message(
    cost: float = 0.01, turns: int = 1, errors: list[str] | None = None
) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        session_id="sess-1",
        is_error=False,
        total_cost_usd=cost,
        num_turns=turns,
        result=None,
        usage=None,
        duration_ms=1000,
        duration_api_ms=800,
        errors=errors,
    )


async def agen(items: list):
    """Async generator from a list."""
    for item in items:
        yield item


class TestResponseStreamerStream:
    async def test_text_message_posted_to_main(self):
        """Text before any tool use should be posted to main channel."""
        streamer, router, provider = make_streamer()
        messages = [
            make_assistant_message([make_text_block("Hello!")]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))
        # Text should be posted to main channel
        provider.post.assert_called()

    async def test_returns_result_message(self):
        streamer, router, provider = make_streamer()
        result_msg = make_result_message(cost=0.05)
        messages = [
            make_assistant_message([make_text_block("text")]),
            result_msg,
        ]
        result = await streamer.stream_with_flush(agen(messages))
        assert result is not None
        assert result.result is result_msg

    async def test_returns_none_when_no_result(self):
        streamer, router, provider = make_streamer()
        messages = [make_assistant_message([make_text_block("text")])]
        result = await streamer.stream_with_flush(agen(messages))
        assert result is None

    async def test_tool_use_block_posted_to_thread(self):
        """Tool use blocks should be posted to turn thread."""
        streamer, router, provider = make_streamer()
        tool_block = make_tool_use_block("Bash", {"command": "git status"})
        messages = [
            make_assistant_message([tool_block]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))
        # Should have posted at least one message
        assert provider.post.call_count >= 1

    async def test_result_only_no_streamer_post(self):
        """ResultMessage with no text produces no streamer post (footer comes from session)."""
        streamer, router, provider = make_streamer()
        messages = [make_result_message(cost=0.0123, turns=3)]
        await streamer.stream_with_flush(agen(messages))
        assert provider.post.call_count == 0

    async def test_long_text_triggers_new_message(self):
        """Long text should trigger multiple messages."""
        streamer, router, provider = make_streamer()
        long_text = "x" * 3000
        messages = [
            make_assistant_message([make_text_block(long_text)]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))
        assert provider.post.call_count >= 1

    async def test_text_after_tool_use_goes_to_main_on_result(self):
        """Text after tool use should be buffered and flushed to main on ResultMessage."""
        streamer, router, provider = make_streamer()
        tool_block = make_tool_use_block("Read", {"file_path": "/src/main.py"})
        messages = [
            make_assistant_message([tool_block, make_text_block("Conclusion text")]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))
        # Should have posted multiple messages (tool + conclusion)
        assert provider.post.call_count >= 2

    async def test_text_before_tool_goes_to_main(self):
        """Text before tool use should go to main channel."""
        streamer, router, provider = make_streamer()
        tool_block = make_tool_use_block("Read", {"file_path": "/src/main.py"})
        messages = [
            make_assistant_message([make_text_block("Analyzing..."), tool_block]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))
        # Should post text to main and tool to thread
        assert provider.post.call_count >= 2

    async def test_empty_buffer_not_posted(self):
        """Empty buffers should not produce any streamer post."""
        streamer, router, provider = make_streamer()
        messages = [make_result_message()]
        await streamer.stream_with_flush(agen(messages))
        assert provider.post.call_count == 0


class TestResponseStreamerStreamWithFlush:
    async def test_stream_with_flush_returns_result(self):
        streamer, router, provider = make_streamer()
        result_msg = make_result_message()
        messages = [
            make_assistant_message([make_text_block("hello")]),
            result_msg,
        ]
        result = await streamer.stream_with_flush(agen(messages))
        assert result is not None
        assert result.result is result_msg

    async def test_stream_with_flush_posts_messages(self):
        streamer, router, provider = make_streamer()
        messages = [
            make_assistant_message([make_text_block("response text")]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))
        assert provider.post.call_count >= 1

    async def test_rate_limit_event_passes_through(self, caplog):
        """RateLimitEvent between AssistantMessage and ResultMessage does not abort stream."""
        rate_limit_info = RateLimitInfo(
            status="allowed_warning",
            resets_at=9999999999,
            rate_limit_type="five_hour",
            utilization=0.85,
            overage_status=None,
            overage_resets_at=None,
        )
        rate_event = RateLimitEvent(
            rate_limit_info=rate_limit_info, uuid="rl-1", session_id="sess-1"
        )
        result_msg = make_result_message()
        streamer, router, provider = make_streamer()
        messages = [
            make_assistant_message([make_text_block("Hello!")]),
            rate_event,
            result_msg,
        ]
        with caplog.at_level(logging.INFO, logger="summon_claude.sessions.response"):
            result = await streamer.stream_with_flush(agen(messages))
        assert result is not None
        assert result.result is result_msg
        provider.post.assert_called()
        assert "Rate limit event received" in caplog.text

    async def test_result_message_errors_logs_warning(self, caplog):
        """ResultMessage.errors triggers a WARNING log and stream still completes."""
        streamer, router, provider = make_streamer()
        result_msg = make_result_message(errors=["API error: quota exceeded"])
        messages = [result_msg]
        with caplog.at_level(logging.WARNING, logger="summon_claude.sessions.response"):
            result = await streamer.stream_with_flush(agen(messages))
        assert result is not None
        assert result.result is result_msg
        assert "SDK ResultMessage errors" in caplog.text

    async def test_result_message_errors_redacts_secrets(self, caplog):
        """Secrets in ResultMessage.errors are redacted before logging."""
        secret = "sk-ant-api03-SUPER-SECRET-KEY-abc123"
        streamer, router, provider = make_streamer()
        result_msg = make_result_message(errors=[f"Auth failed: {secret}"])
        messages = [result_msg]
        with caplog.at_level(logging.WARNING, logger="summon_claude.sessions.response"):
            await streamer.stream_with_flush(agen(messages))
        assert secret not in caplog.text
        assert "[REDACTED]" in caplog.text


class TestResponseStreamerSubagentThreads:
    async def test_task_tool_use_creates_subagent_thread(self):
        """Task tool use should trigger start_subagent_thread."""
        streamer, router, provider = make_streamer()
        task_block = make_tool_use_block(
            "Task",
            {
                "description": "Analyze the codebase",
                "prompt": "Find security issues",
            },
        )
        messages = [
            make_assistant_message([task_block]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))
        # Should have created a subagent thread
        assert "tu_1" in router.subagent_threads

    async def test_text_with_parent_tool_use_id_goes_to_subagent(self):
        """Text from subagent should go to subagent thread."""
        streamer, router, provider = make_streamer()
        # Create a subagent thread first
        await router.start_subagent_thread("task_123", "Running analysis")

        # Now stream a response with parent_tool_use_id
        msg = AssistantMessage(content=[make_text_block("Subagent response")], model="test")
        msg.parent_tool_use_id = "task_123"

        messages = [msg, make_result_message()]
        await streamer.stream_with_flush(agen(messages))

        # Should have posted to subagent thread
        provider.post.assert_called()

    async def test_task_tool_use_populates_pending_agent_verifications(self):
        """Task ToolUseBlock adds input to _pending_agent_verifications."""
        streamer, router, provider = make_streamer()
        task_input = {"description": "Analyze security", "prompt": "Find issues"}
        task_block = make_tool_use_block("Task", task_input, tool_use_id="tu_task_1")
        msg = make_assistant_message([task_block])
        await streamer._handle_assistant_message(msg)

        assert "tu_task_1" in streamer._pending_agent_verifications
        assert streamer._pending_agent_verifications["tu_task_1"] == task_input

    async def test_task_notification_completed_triggers_callback(self):
        """Completed TaskNotificationMessage pops entry and fires on_subagent_return."""
        from claude_agent_sdk import TaskNotificationMessage

        callback = AsyncMock()
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, on_subagent_return=callback)

        task_input = {"prompt": "Find security issues"}
        streamer._pending_agent_verifications["tu_task_1"] = task_input

        notif = TaskNotificationMessage(
            subtype="task_notification",
            data={},
            task_id="task-abc",
            status="completed",
            output_file="",
            summary="All done",
            uuid="uuid-1",
            session_id="sess-1",
            tool_use_id="tu_task_1",
        )
        messages = [notif, make_result_message()]
        await streamer.stream_with_flush(agen(messages))
        await asyncio.sleep(0.05)

        assert "tu_task_1" not in streamer._pending_agent_verifications
        callback.assert_called_once_with(task_input, "All done")

    async def test_task_notification_non_completed_pops_without_callback(self):
        """Failed TaskNotificationMessage pops entry but does NOT fire callback."""
        from claude_agent_sdk import TaskNotificationMessage

        callback = AsyncMock()
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, on_subagent_return=callback)

        streamer._pending_agent_verifications["tu_task_2"] = {"prompt": "Do something"}

        notif = TaskNotificationMessage(
            subtype="task_notification",
            data={},
            task_id="task-def",
            status="failed",
            output_file="",
            summary="",
            uuid="uuid-2",
            session_id="sess-1",
            tool_use_id="tu_task_2",
        )
        messages = [notif, make_result_message()]
        await streamer.stream_with_flush(agen(messages))
        await asyncio.sleep(0.05)

        assert "tu_task_2" not in streamer._pending_agent_verifications
        callback.assert_not_called()

    async def test_task_notification_unknown_tool_use_id_ignored(self):
        """TaskNotificationMessage with unknown tool_use_id is silently ignored."""
        from claude_agent_sdk import TaskNotificationMessage

        callback = AsyncMock()
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, on_subagent_return=callback)

        notif = TaskNotificationMessage(
            subtype="task_notification",
            data={},
            task_id="task-xyz",
            status="completed",
            output_file="",
            summary="summary text",
            uuid="uuid-3",
            session_id="sess-1",
            tool_use_id="tu_unknown",
        )
        messages = [notif, make_result_message()]
        await streamer.stream_with_flush(agen(messages))
        await asyncio.sleep(0.05)

        callback.assert_not_called()
        assert streamer._pending_agent_verifications == {}

    async def test_pending_agent_verification_resolves_across_turns(self):
        """Entry added in turn 1 resolves when TaskNotificationMessage arrives in turn 2."""
        from claude_agent_sdk import TaskNotificationMessage

        callback = AsyncMock()
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, on_subagent_return=callback)

        task_input = {"prompt": "Do something long-running"}

        # Turn 1: Task ToolUseBlock is seen — entry added, no notification arrives
        task_block = make_tool_use_block("Task", task_input, tool_use_id="tu_cross")
        turn1_messages = [
            make_assistant_message([task_block]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(turn1_messages))

        # Entry must persist across the turn boundary
        assert "tu_cross" in streamer._pending_agent_verifications

        # Turn 2: TaskNotificationMessage arrives for the same tool_use_id
        notif = TaskNotificationMessage(
            subtype="task_notification",
            data={},
            task_id="task-cross",
            status="completed",
            output_file="",
            summary="Cross-turn done",
            uuid="uuid-cross",
            session_id="sess-1",
            tool_use_id="tu_cross",
        )
        turn2_messages = [notif, make_result_message()]
        await streamer.stream_with_flush(agen(turn2_messages))
        await asyncio.sleep(0.05)

        # Callback must fire and entry must be consumed
        assert "tu_cross" not in streamer._pending_agent_verifications
        callback.assert_called_once_with(task_input, "Cross-turn done")

    async def test_aborted_turn_cleans_up_pending_verifications(self):
        """Task entries added during an aborted turn (no ResultMessage) are cleaned up."""
        callback = AsyncMock()
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, on_subagent_return=callback)

        task_input = {"prompt": "will be aborted"}
        task_block = make_tool_use_block("Task", task_input, tool_use_id="tu_abort")

        async def abort_after_task():
            yield make_assistant_message([task_block])
            # No ResultMessage — stream ends without completing

        await streamer.stream_with_flush(abort_after_task())

        # Entry should be cleaned up since no ResultMessage was received
        assert "tu_abort" not in streamer._pending_agent_verifications
        callback.assert_not_called()


class TestFormatToolSummary:
    def test_bash_shows_command(self):
        summary = _format_tool_summary("Bash", {"command": "git status"})
        assert "git status" in summary

    def test_bash_truncates_long_command(self):
        long_cmd = "x" * 200
        summary = _format_tool_summary("Bash", {"command": long_cmd})
        assert len(summary) < 150
        assert "..." in summary

    def test_read_shows_path(self):
        summary = _format_tool_summary("Read", {"file_path": "/src/main.py"})
        assert "/src/main.py" in summary

    def test_write_shows_path(self):
        summary = _format_tool_summary("Write", {"file_path": "/out/file.txt"})
        assert "/out/file.txt" in summary

    def test_edit_shows_path(self):
        summary = _format_tool_summary("Edit", {"path": "/foo/bar.py"})
        assert "/foo/bar.py" in summary

    def test_glob_shows_pattern(self):
        summary = _format_tool_summary("Glob", {"pattern": "**/*.py"})
        assert "**/*.py" in summary

    def test_web_search_shows_query(self):
        summary = _format_tool_summary("WebSearch", {"query": "python asyncio"})
        assert "python asyncio" in summary

    def test_web_fetch_shows_url(self):
        summary = _format_tool_summary("WebFetch", {"url": "https://example.com"})
        assert "example.com" in summary

    def test_unknown_tool_returns_string(self):
        summary = _format_tool_summary("CustomTool", {"key": "value"})
        assert isinstance(summary, str)

    def test_empty_input_returns_empty_or_string(self):
        summary = _format_tool_summary("Bash", {})
        assert isinstance(summary, str)


class TestBUG029ResultMessageWithOutput:
    """BUG-029: Test that ResultMessage.result is posted to main channel."""

    async def test_result_message_with_output_posted_to_main(self):
        """When ResultMessage.result is non-empty string, post_to_main should be called with it."""
        streamer, router, provider = make_streamer()
        result_msg = make_result_message()
        result_msg.result = "some output text"
        messages = [result_msg]

        await streamer.stream_with_flush(agen(messages))

        # The result is posted via post_to_main, verify it was called at least once
        assert provider.post.call_count >= 1

    async def test_result_message_without_output_no_extra_post(self):
        """When ResultMessage.result is None, streamer should not post anything."""
        streamer, router, provider = make_streamer()
        result_msg = make_result_message()
        result_msg.result = None
        messages = [result_msg]

        await streamer.stream_with_flush(agen(messages))

        assert provider.post.call_count == 0


class TestTextOnlyResponseNoDuplicate:
    """Text-only responses should NOT post result.result when buffer was already flushed."""

    async def test_text_only_response_not_duplicated(self):
        """When text is already flushed to main via buffer, result.result should be skipped."""
        streamer, router, provider = make_streamer()
        result_msg = make_result_message()
        result_msg.result = "Hello!"
        messages = [
            make_assistant_message([make_text_block("Hello!")]),
            result_msg,
        ]

        await streamer.stream_with_flush(agen(messages))

        # Collect all post_to_main text args (excluding blocks-only calls)
        posted_texts = [
            call.args[0] if call.args else call.kwargs.get("text", "")
            for call in provider.post.call_args_list
        ]
        # "Hello!" should appear only once (from buffer flush), not twice
        hello_count = sum(1 for t in posted_texts if "Hello!" in t)
        assert hello_count == 1, (
            f"Expected 'Hello!' posted once, got {hello_count}. Posts: {posted_texts}"
        )

    async def test_result_still_posted_when_no_text_blocks(self):
        """ResultMessage.result should still be posted if no text was buffered."""
        streamer, router, provider = make_streamer()
        result_msg = make_result_message()
        result_msg.result = "some output"
        messages = [result_msg]

        await streamer.stream_with_flush(agen(messages))

        posted_texts = [
            call.args[0] if call.args else call.kwargs.get("text", "")
            for call in provider.post.call_args_list
        ]
        assert any("some output" in t for t in posted_texts), (
            f"Expected 'some output' to be posted. Posts: {posted_texts}"
        )


class TestBUG028ResolvedModelTracking:
    """BUG-028: Test that streamer tracks resolved_model from AssistantMessage.

    The resolved_model property was removed from ResponseStreamer. Model info
    is now only available via StreamResult.model (returned from stream_with_flush).
    These tests verify model tracking through the StreamResult API.
    """

    async def test_resolved_model_set_from_assistant_message(self):
        """After streaming AssistantMessage with model, StreamResult.model returns it."""
        streamer, router, provider = make_streamer()
        msg = make_assistant_message([make_text_block("Response")])
        msg.model = "claude-opus-4-6"
        messages = [msg, make_result_message()]

        result = await streamer.stream_with_flush(agen(messages))

        assert result is not None
        assert result.model == "claude-opus-4-6"

    async def test_resolved_model_none_before_streaming(self):
        """Before any messages, _turn.resolved_model should be None."""
        streamer, router, provider = make_streamer()

        assert streamer._turn.resolved_model is None

    async def test_resolved_model_persists_across_multiple_messages(self):
        """resolved_model should be set from first message and persist in StreamResult."""
        streamer, router, provider = make_streamer()
        msg1 = make_assistant_message([make_text_block("First")])
        msg1.model = "claude-opus-4-6"
        msg2 = make_assistant_message([make_text_block("Second")])
        # msg2 has a different model but we should keep the first one
        msg2.model = "claude-sonnet-4"
        messages = [msg1, msg2, make_result_message()]

        result = await streamer.stream_with_flush(agen(messages))

        # Should have the first model
        assert result is not None
        assert result.model == "claude-opus-4-6"


class TestSplitText:
    def test_short_text_not_split(self):
        chunks = _split_text("hello", 3000)
        assert chunks == ["hello"]

    def test_long_text_split(self):
        text = "line\n" * 1000
        chunks = _split_text(text, 3000)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 3000

    def test_no_newline_split_at_limit(self):
        text = "x" * 6000
        chunks = _split_text(text, 3000)
        assert len(chunks) == 2

    def test_exactly_at_limit(self):
        text = "x" * 3000
        chunks = _split_text(text, 3000)
        assert len(chunks) == 1


class TestBug025ResponseDuplication:
    """Tests for post-tool text routing: eager to thread + conclusion to main."""

    async def test_post_tool_text_eager_to_thread_and_conclusion_to_main(self):
        """Post-tool text should appear in thread (eager) AND main (conclusion)."""
        streamer, router, provider = make_streamer()
        await streamer.start_turn(1, user_snippet="test")
        provider.post.reset_mock()

        tool_block = make_tool_use_block("Read", {"file_path": "/src/main.py"})
        conclusion_text = "Conclusion text after tool"
        messages = [
            make_assistant_message([tool_block, make_text_block(conclusion_text)]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        calls = provider.post.call_args_list
        thread_posts = [c for c in calls if c.kwargs.get("thread_ts")]
        main_posts = [c for c in calls if not c.kwargs.get("thread_ts")]

        thread_texts = [c.args[0] for c in thread_posts if c.args]
        main_texts = [c.args[0] for c in main_posts if c.args]

        # Eager: posted to thread
        assert any(conclusion_text in t for t in thread_texts), (
            f"Conclusion text not found in thread posts: {thread_texts}"
        )
        # Conclusion: posted to main
        assert any(conclusion_text in t for t in main_texts), (
            f"Conclusion text not found in main posts: {main_texts}"
        )

    async def test_conclusion_not_duplicated_in_main(self):
        """Conclusion text should appear exactly once in main channel."""
        streamer, router, provider = make_streamer()
        await streamer.start_turn(1, user_snippet="test")
        provider.post.reset_mock()

        tool_block = make_tool_use_block("Read", {"file_path": "/src/main.py"})
        conclusion_text = "Unique conclusion"
        messages = [
            make_assistant_message([tool_block, make_text_block(conclusion_text)]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        main_posts = [c for c in provider.post.call_args_list if not c.kwargs.get("thread_ts")]
        main_texts = [c.args[0] for c in main_posts if c.args]
        count = sum(1 for t in main_texts if conclusion_text in t)
        assert count == 1, f"Expected conclusion once in main, got {count}: {main_texts}"

    async def test_multiple_text_blocks_after_tool_concatenated(self):
        """Multiple TextBlocks after tool use should be concatenated in conclusion."""
        streamer, router, provider = make_streamer()
        await streamer.start_turn(1, user_snippet="test")
        provider.post.reset_mock()

        tool_block = make_tool_use_block("Read", {"file_path": "/src/main.py"})
        messages = [
            make_assistant_message(
                [
                    tool_block,
                    make_text_block("Part 1"),
                    make_text_block("Part 2"),
                ]
            ),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        main_posts = [c for c in provider.post.call_args_list if not c.kwargs.get("thread_ts")]
        full_text = "".join(c.args[0] if c.args else "" for c in main_posts)
        assert "Part 1" in full_text
        assert "Part 2" in full_text

    async def test_pre_tool_text_still_goes_to_main(self):
        """Text BEFORE tool use should still go to main channel (regression test)."""
        streamer, router, provider = make_streamer()
        pre_tool_text = "Analyzing..."
        tool_block = make_tool_use_block("Read", {"file_path": "/src/main.py"})
        messages = [
            make_assistant_message([make_text_block(pre_tool_text), tool_block]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        calls = provider.post.call_args_list
        main_posts = [c for c in calls if not c.kwargs.get("thread_ts")]
        main_texts = [c.args[0] for c in main_posts if c.args]
        assert any(pre_tool_text in t for t in main_texts), (
            f"Pre-tool text not found in main posts: {main_texts}"
        )

    async def test_last_message_ts_tracks_conclusion(self):
        """After conclusion is posted to main, last_message_ts should be updated."""
        streamer, router, provider = make_streamer()
        tool_block = make_tool_use_block("Read", {"file_path": "/src/main.py"})
        conclusion_text = "Conclusion after tool"
        messages = [
            make_assistant_message([tool_block, make_text_block(conclusion_text)]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        # last_message_ts should be set for tracking
        assert streamer._turn.last_message_ts is not None

        # Checkmark reaction is now on the user's message (in session.py),
        # not on the bot's message (streamer no longer reacts)
        assert not provider.react.called


class TestStreamResult:
    async def test_stream_result_model_captured(self):
        """Model from AssistantMessage should appear in StreamResult."""
        streamer, router, provider = make_streamer()
        messages = [
            make_assistant_message([make_text_block("text")]),
            make_result_message(),
        ]
        stream_result = await streamer.stream_with_flush(agen(messages))
        assert stream_result is not None
        assert stream_result.model == "claude-opus-4-6"

    async def test_stream_result_none_when_no_result(self):
        """When no ResultMessage, stream_with_flush returns None."""
        streamer, router, provider = make_streamer()
        messages = [make_assistant_message([make_text_block("text")])]
        result = await streamer.stream_with_flush(agen(messages))
        assert result is None

    async def test_stream_result_result_attribute_matches_message(self):
        """StreamResult.result should be the exact ResultMessage object."""
        streamer, router, provider = make_streamer()
        result_msg = make_result_message(cost=0.02)
        messages = [
            make_assistant_message([make_text_block("hello")]),
            result_msg,
        ]
        stream_result = await streamer.stream_with_flush(agen(messages))
        assert stream_result is not None
        assert stream_result.result is result_msg

    async def test_stream_result_model_none_when_not_set(self):
        """When AssistantMessage has no model, StreamResult.model should be None."""
        streamer, router, provider = make_streamer()
        # Create AssistantMessage without setting model
        msg_no_model = AssistantMessage(content=[make_text_block("text")], model=None)
        messages = [
            msg_no_model,
            make_result_message(),
        ]
        stream_result = await streamer.stream_with_flush(agen(messages))
        assert stream_result is not None
        assert stream_result.model is None


class TestResponseStreamerUserPing:
    """Tests for user ID ping in conclusion text."""

    async def test_conclusion_ping_with_user_id(self):
        """Conclusion text should be prefixed with <@user_id> ping when user_id is set."""
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, user_id="U_TESTUSER")

        tool_block = make_tool_use_block("Read", {"file_path": "/src/main.py"})
        messages = [
            make_assistant_message([tool_block, make_text_block("Here is the analysis.")]),
            make_result_message(),
        ]

        await streamer.stream_with_flush(agen(messages))

        # Find the call that posts conclusion text (should be after tool use)
        found_ping = False
        for call in client.post.call_args_list:
            text = call.args[0] if call.args else ""
            if "analysis" in text and "<@U_TESTUSER>" in text:
                found_ping = True
                break
        assert found_ping, "Conclusion text should contain user ping <@U_TESTUSER>"

    async def test_conclusion_no_ping_without_user_id(self):
        """Conclusion text should not be modified when user_id is None."""
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, user_id=None)

        tool_block = make_tool_use_block("Read", {"file_path": "/src/main.py"})
        messages = [
            make_assistant_message([tool_block, make_text_block("Here is the analysis.")]),
            make_result_message(),
        ]

        await streamer.stream_with_flush(agen(messages))

        # Check that conclusion text doesn't have a ping prefix
        for call in client.post.call_args_list:
            text = call.args[0] if call.args else ""
            if "analysis" in text:
                # Should not start with a user ping
                assert not text.startswith("<@"), (
                    "Conclusion without user_id should not have ping prefix"
                )
                assert text.startswith("Here"), "Conclusion should start with original text"


# ---------------------------------------------------------------------------
# Tests for diff upload behavior (replaces old _format_diff tests)
# ---------------------------------------------------------------------------


class TestResolveUploadThread:
    def test_returns_active_thread(self):
        streamer, router, _client = make_streamer()
        router.active_thread_ts = "active_ts"
        assert streamer._resolve_upload_thread(None) == "active_ts"

    def test_returns_subagent_thread_when_registered(self):
        streamer, router, _client = make_streamer()
        router.active_thread_ts = "active_ts"
        router.subagent_threads["task_abc"] = "subagent_ts"
        assert streamer._resolve_upload_thread("task_abc") == "subagent_ts"

    def test_falls_back_to_active_when_parent_unknown(self):
        streamer, router, _client = make_streamer()
        router.active_thread_ts = "fallback_ts"
        assert streamer._resolve_upload_thread("unknown_parent") == "fallback_ts"

    def test_raises_when_no_active_thread(self):
        streamer, _router, _client = make_streamer()
        with pytest.raises(RuntimeError, match="No active thread"):
            streamer._resolve_upload_thread(None)


class TestUploadDiff:
    async def test_no_change_posts_notice(self):
        streamer, router, client = make_streamer()
        router.active_thread_ts = "thread_1"
        await streamer._upload_diff("same", "same", "file.py", "thread_1")
        # Should post a "No changes" message via router with mrkdwn conversion
        assert client.post.call_count >= 1
        text = client.post.call_args.args[0]
        # Source is markdown *italic* — router converts to mrkdwn _italic_
        assert "_No changes" in text
        assert "file.py" in text

    async def test_change_uploads_diff_file(self):
        streamer, router, client = make_streamer()
        await streamer._upload_diff("old\n", "new\n", "/src/file.py", "thread_1")
        client.upload.assert_called_once()
        call_kwargs = client.upload.call_args.kwargs
        assert call_kwargs["snippet_type"] == "diff"
        assert call_kwargs["thread_ts"] == "thread_1"
        assert "file.py.diff" in client.upload.call_args.args[1]

    async def test_upload_failure_falls_back_to_inline(self):
        streamer, router, client = make_streamer()
        client.upload.side_effect = Exception("API error")
        router.active_thread_ts = "thread_1"
        # Should not raise — falls back to inline posting via router
        await streamer._upload_diff("old\n", "new\n", "file.py", "thread_1")
        assert client.post.call_count >= 1
        # Router converts markdown **Edit:** to mrkdwn *Edit:*
        text = client.post.call_args.args[0]
        assert "*Edit:*" in text

    async def test_edit_tool_triggers_diff_upload(self):
        streamer, router, client = make_streamer()
        router.active_thread_ts = "thread_1"
        edit_block = make_tool_use_block(
            "Edit",
            {"path": "/src/main.py", "old_string": "old\n", "new_string": "new\n"},
        )
        msg = make_assistant_message([edit_block])
        await streamer._handle_assistant_message(msg)
        result_block = ToolResultBlock(tool_use_id="tu_1", content="OK")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        # Give fire-and-forget task a moment
        await asyncio.sleep(0.05)
        client.upload.assert_called_once()
        call_kwargs = client.upload.call_args.kwargs
        assert call_kwargs["snippet_type"] == "diff"

    async def test_write_tool_triggers_content_upload(self):
        streamer, router, client = make_streamer()
        router.active_thread_ts = "thread_1"
        write_block = make_tool_use_block(
            "Write",
            {"file_path": "/src/output.py", "content": "print('hello')"},
        )
        msg = make_assistant_message([write_block])
        await streamer._handle_assistant_message(msg)
        result_block = ToolResultBlock(tool_use_id="tu_1", content="OK")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)
        client.upload.assert_called_once()
        assert "output.py" in client.upload.call_args.args[1]
        assert client.upload.call_args.kwargs["snippet_type"] == "python"

    async def test_write_md_renders_markdown_blocks(self):
        streamer, router, client = make_streamer()
        router.active_thread_ts = "thread_1"
        md_content = "# Hello\n\n**World**"
        write_block = make_tool_use_block(
            "Write",
            {"file_path": "/src/README.md", "content": md_content},
        )
        msg = make_assistant_message([write_block])
        await streamer._handle_assistant_message(msg)
        result_block = ToolResultBlock(tool_use_id="tu_1", content="OK")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)
        # .md files should NOT trigger upload — they use markdown blocks
        client.upload.assert_not_called()
        # Find the markdown block post
        md_blocks = []
        for c in client.post.call_args_list:
            for b in c.kwargs.get("blocks") or []:
                if b.get("type") == "markdown":
                    md_blocks.append(b)
        assert md_blocks, "Expected at least one type: markdown block"
        # Block content must be raw markdown — NOT mrkdwn-converted
        assert md_blocks[0]["text"] == md_content
        # text param (notification fallback) must also be raw (no conversion)
        md_call = next(
            c
            for c in client.post.call_args_list
            if any(b.get("type") == "markdown" for b in (c.kwargs.get("blocks") or []))
        )
        assert md_call.args[0] == md_content

    async def test_write_md_repeated_shows_update(self):
        streamer, router, client = make_streamer()
        router.active_thread_ts = "thread_1"
        write_block = make_tool_use_block(
            "Write",
            {"file_path": "/src/README.md", "content": "# V1"},
            tool_use_id="tu_1",
        )
        msg = make_assistant_message([write_block])
        await streamer._handle_assistant_message(msg)
        result_block = ToolResultBlock(tool_use_id="tu_1", content="OK")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)
        client.post.reset_mock()

        # Second write to same path
        write_block2 = make_tool_use_block(
            "Write",
            {"file_path": "/src/README.md", "content": "# V2"},
            tool_use_id="tu_2",
        )
        msg2 = make_assistant_message([write_block2])
        await streamer._handle_assistant_message(msg2)
        result_block2 = ToolResultBlock(tool_use_id="tu_2", content="OK")
        result_msg2 = make_assistant_message([result_block2])
        await streamer._handle_assistant_message(result_msg2)
        await asyncio.sleep(0.05)

        # Should show "Updated" header, not full re-render.
        # Header goes through post_to_thread: **Updated:** → *Updated:* (mrkdwn bold)
        all_texts = [c.args[0] for c in client.post.call_args_list if c.args]
        assert any("*Updated:*" in t for t in all_texts)
        # Must NOT re-render the markdown content
        all_blocks = [b for c in client.post.call_args_list for b in (c.kwargs.get("blocks") or [])]
        assert not any(b.get("type") == "markdown" for b in all_blocks)

    async def test_write_md_fallback_to_plain_text(self):
        """When type: markdown blocks fail, should fall back to plain text."""
        streamer, router, client = make_streamer()
        router.active_thread_ts = "thread_1"

        # Make post fail on markdown blocks, succeed on plain text
        call_count = 0
        original_post = client.post

        async def selective_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            blocks = kwargs.get("blocks") or []
            if any(b.get("type") == "markdown" for b in blocks):
                raise Exception("markdown blocks not supported")
            return await original_post(*args, **kwargs)

        client.post = AsyncMock(side_effect=selective_fail)

        write_block = make_tool_use_block(
            "Write",
            {"file_path": "/src/README.md", "content": "# Hello"},
        )
        msg = make_assistant_message([write_block])
        await streamer._handle_assistant_message(msg)
        result_block = ToolResultBlock(tool_use_id="tu_1", content="OK")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)

        # Should have attempted markdown block and fallen back to mrkdwn-converted text.
        # Calls: header (succeeds) + md block (fails) + mrkdwn fallback (succeeds)
        assert call_count >= 3
        # Verify the fallback call has no markdown blocks and has mrkdwn-converted text
        plain_calls = [
            c
            for c in client.post.call_args_list
            if not any(b.get("type") == "markdown" for b in (c.kwargs.get("blocks") or []))
        ]
        assert len(plain_calls) >= 2  # header + mrkdwn fallback
        # The fallback text should be mrkdwn-converted (# Hello → *Hello*)
        fallback_texts = [c.args[0] for c in plain_calls if c.args]
        # Must contain the converted heading (*Hello*), NOT the raw markdown (# Hello)
        assert any("*Hello*" in t for t in fallback_texts)
        assert not any("# Hello" in t for t in fallback_texts)


class TestSplitTextAdditional:
    """Additional split_text tests absorbed from test_content_display.py."""

    def test_no_newline_split_at_limit(self):
        text = "x" * 6000
        chunks = _split_text(text, 3000)
        assert len(chunks) == 2
        assert len(chunks[0]) == 3000
        assert len(chunks[1]) == 3000

    def test_exactly_at_limit_not_split(self):
        text = "x" * 3000
        chunks = _split_text(text, 3000)
        assert len(chunks) == 1

    def test_all_chunks_within_limit(self):
        import random

        text = "".join(random.choice("abcde\n") for _ in range(10000))
        chunks = _split_text(text, 3000)
        for chunk in chunks:
            assert len(chunk) <= 3000

    def test_even_fence_count_not_modified(self):
        """Chunks with even fence counts (closed blocks) should not be touched."""
        text = "```a```\n" * 50 + "\n" + "```b```\n" * 50
        chunks = _split_text(text, 200)
        for chunk in chunks:
            fence_count = chunk.count("```")
            assert fence_count % 2 == 0


class TestTurnHeaderSnippet:
    """Tests for turn header with user snippet."""

    async def test_start_turn_with_snippet(self):
        streamer, router, provider = make_streamer()
        ts = await streamer.start_turn(1, user_snippet="fix the auth bug")
        assert ts
        # Verify snippet is in the posted message
        post_text = provider.post.call_args.args[0]
        assert "fix the auth bug" in post_text
        assert "Turn 1" in post_text

    async def test_start_turn_strips_mrkdwn_chars(self):
        streamer, router, provider = make_streamer()
        await streamer.start_turn(1, user_snippet="**bold** _italic_ `code`")
        post_text = provider.post.call_args.args[0]
        # mrkdwn special chars should be stripped
        assert "**" not in post_text
        assert "`" not in post_text

    async def test_start_turn_without_snippet(self):
        streamer, router, provider = make_streamer()
        await streamer.start_turn(1)
        post_text = provider.post.call_args.args[0]
        assert "Processing..." in post_text

    async def test_update_turn_summary_preserves_snippet(self):
        streamer, router, provider = make_streamer()
        await streamer.start_turn(1, user_snippet="fix bug")
        provider.update.reset_mock()
        await streamer.update_turn_summary("3 tools, 45%")
        update_text = provider.update.call_args.args[1]
        assert "fix bug" in update_text
        assert "3 tools, 45%" in update_text


class TestSetStatus:
    """Tests for setStatus integration."""

    async def test_set_status_at_turn_start(self):
        streamer, router, provider = make_streamer()
        await streamer.start_turn(1, user_snippet="test")
        provider.set_thread_status.assert_called()
        # Should have set "Thinking..." status
        call_args = provider.set_thread_status.call_args
        assert call_args.args[1] == "Thinking..."

    async def test_set_status_during_tool_execution(self):
        streamer, router, provider = make_streamer()
        await streamer.start_turn(1)
        provider.set_thread_status.reset_mock()
        tool_block = make_tool_use_block("Read", {"file_path": "/test.py"})
        msg = make_assistant_message([tool_block])
        await streamer._handle_assistant_message(msg)
        # "Running Read..." should be set AFTER the tool use post (persists during execution)
        calls = [c.args[1] for c in provider.set_thread_status.call_args_list]
        assert "Running Read..." in calls

    async def test_set_status_cleared_at_turn_end(self):
        streamer, router, provider = make_streamer()
        await streamer.start_turn(1, user_snippet="test")
        provider.set_thread_status.reset_mock()
        # Simulate a turn with a tool (so status actually changes during the turn)
        tool_block = make_tool_use_block("Read", {"file_path": "/test.py"})
        messages = [
            make_assistant_message([tool_block, make_text_block("result")]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))
        # The last setStatus call should clear with ""
        calls = [c.args[1] for c in provider.set_thread_status.call_args_list]
        assert "" in calls

    async def test_no_redundant_thinking_between_tools(self):
        """No 'Thinking...' status between tool result and next tool use."""
        streamer, router, provider = make_streamer()
        await streamer.start_turn(1)
        provider.set_thread_status.reset_mock()
        tool1 = make_tool_use_block("Read", {"file_path": "/a.py"})
        tool2 = make_tool_use_block("Edit", {"path": "/a.py"})
        msg = make_assistant_message([tool1, tool2])
        await streamer._handle_assistant_message(msg)
        calls = [c.args[1] for c in provider.set_thread_status.call_args_list]
        # Should have "Running Read..." and "Running Edit...", no "Thinking..." in between
        assert "Running Read..." in calls
        assert "Running Edit..." in calls
        assert "Thinking..." not in calls


class TestEagerIntermediateText:
    """Tests for eager intermediate text routing to thread."""

    async def test_intermediate_text_goes_to_thread(self):
        streamer, router, provider = make_streamer()
        await streamer.start_turn(1, user_snippet="test")
        provider.post.reset_mock()
        tool_block = make_tool_use_block("Read", {"file_path": "/test.py"})
        messages = [
            make_assistant_message([tool_block, make_text_block("intermediate")]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))
        # Intermediate text should be posted to thread (thread_ts is set)
        thread_calls = [c for c in provider.post.call_args_list if c.kwargs.get("thread_ts")]
        thread_texts = [c.args[0] for c in thread_calls if c.args]
        assert any("intermediate" in t for t in thread_texts)


class TestThinkingBlock:
    """Tests for ThinkingBlock display."""

    async def test_thinking_silent_when_disabled(self):
        streamer, _, client = make_streamer(show_thinking=False)
        tb = ThinkingBlock(thinking="deep thoughts", signature="sig")
        msg = AssistantMessage(content=[tb, make_text_block("result")], model="claude-opus-4-6")
        messages = [msg, make_result_message()]
        await streamer.stream_with_flush(agen(messages))
        # Thinking should NOT appear in posted blocks (where _flush_thinking routes content)
        all_blocks = []
        for c in client.post.call_args_list:
            if c.kwargs.get("blocks"):
                all_blocks.extend(c.kwargs["blocks"])
        assert not any("deep thoughts" in str(b) for b in all_blocks), (
            "Thinking content should not appear when show_thinking=False"
        )

    async def test_thinking_posted_when_enabled(self):
        streamer, _, client = make_streamer(show_thinking=True)
        tb = ThinkingBlock(thinking="deep thoughts", signature="sig")
        msg = AssistantMessage(content=[tb, make_text_block("result")], model="claude-opus-4-6")
        messages = [msg, make_result_message()]
        await streamer.stream_with_flush(agen(messages))
        all_blocks = []
        for c in client.post.call_args_list:
            if c.kwargs.get("blocks"):
                all_blocks.extend(c.kwargs["blocks"])
        assert any("deep thoughts" in str(b) for b in all_blocks), (
            "Thinking content should appear in posted blocks"
        )

    async def test_thinking_sets_status_deeply(self):
        streamer, _, client = make_streamer(show_thinking=False)
        await streamer.start_turn(1)
        client.set_thread_status.reset_mock()
        tb = ThinkingBlock(thinking="thinking...", signature="sig")
        msg = AssistantMessage(content=[tb], model="claude-opus-4-6")
        await streamer._handle_assistant_message(msg)
        calls = [c.args[1] for c in client.set_thread_status.call_args_list]
        assert "Thinking deeply..." in calls, (
            "ThinkingBlock should set status to 'Thinking deeply...'"
        )

    async def test_thinking_splits_long_content(self):
        """Thinking content near context element limit should split, not truncate."""
        streamer, _, client = make_streamer(show_thinking=True, max_inline_chars=5000)
        # 4000 chars — exceeds single context element (3000) but under max_inline_chars
        long_thinking = "x" * 4000
        tb = ThinkingBlock(thinking=long_thinking, signature="sig")
        msg = AssistantMessage(content=[tb, make_text_block("result")], model="claude-opus-4-6")
        messages = [msg, make_result_message()]
        await streamer.stream_with_flush(agen(messages))

        thinking_posts = [
            c
            for c in client.post.call_args_list
            if c.kwargs.get("blocks")
            and any("thought_balloon" in str(b) for b in c.kwargs["blocks"])
        ]
        assert len(thinking_posts) >= 2, (
            f"Expected multiple thinking posts for split, got {len(thinking_posts)}"
        )

    async def test_thinking_blocks_accumulate(self):
        """Multiple ThinkingBlocks before a TextBlock flush as one combined message."""
        streamer, _, client = make_streamer(show_thinking=True)
        tb1 = ThinkingBlock(thinking="first thought", signature="sig1")
        tb2 = ThinkingBlock(thinking="second thought", signature="sig2")
        msg = AssistantMessage(
            content=[tb1, tb2, make_text_block("result")], model="claude-opus-4-6"
        )
        messages = [msg, make_result_message()]

        real_flush = streamer._flush_thinking
        with patch.object(streamer, "_flush_thinking", wraps=real_flush) as patched:
            await streamer.stream_with_flush(agen(messages))
            assert patched.call_count >= 1, "_flush_thinking should be called before TextBlock"

        # Both contents should appear in a single thinking post (concatenated)
        thinking_posts = [
            c
            for c in client.post.call_args_list
            if c.kwargs.get("blocks")
            and any("thought_balloon" in str(b) for b in c.kwargs["blocks"])
        ]
        assert len(thinking_posts) == 1, (
            f"Expected single combined thinking post, got {len(thinking_posts)}"
        )
        combined = str(thinking_posts[0].kwargs["blocks"])
        assert "first thought" in combined, "First thought missing from combined post"
        assert "second thought" in combined, "Second thought missing from combined post"

    async def test_thinking_flush_on_tool_use(self):
        """Accumulated thinking flushes when a ToolUseBlock arrives, not just TextBlock."""
        streamer, _, client = make_streamer(show_thinking=True)
        tb = ThinkingBlock(thinking="pre-tool thought", signature="sig")
        tool = make_tool_use_block("Read", {"file_path": "/a.py"})
        msg = AssistantMessage(content=[tb, tool], model="claude-opus-4-6")
        messages = [msg, make_result_message()]

        real_flush = streamer._flush_thinking
        with patch.object(streamer, "_flush_thinking", wraps=real_flush) as patched:
            await streamer.stream_with_flush(agen(messages))
            assert patched.call_count >= 1, "_flush_thinking should be triggered by ToolUseBlock"

        all_blocks = []
        for c in client.post.call_args_list:
            if c.kwargs.get("blocks"):
                all_blocks.extend(c.kwargs["blocks"])
        assert any("pre-tool thought" in str(b) for b in all_blocks), (
            "Thinking content should appear in posted blocks"
        )

    async def test_subagent_thinking(self):
        """ThinkingBlock with parent_tool_use_id still accumulates to turn buffer.

        _handle_thinking_block does not inspect parent_tool_use_id — thinking is
        always a turn-level concept, not scoped to a subagent thread.
        """
        streamer, _, client = make_streamer(show_thinking=True)
        tb = ThinkingBlock(thinking="subagent thought", signature="sig")
        msg = AssistantMessage(
            content=[tb, make_text_block("subagent result")],
            model="claude-opus-4-6",
            parent_tool_use_id="tu_subagent",
        )
        messages = [msg, make_result_message()]
        await streamer.stream_with_flush(agen(messages))

        all_blocks = []
        for c in client.post.call_args_list:
            if c.kwargs.get("blocks"):
                all_blocks.extend(c.kwargs["blocks"])
        assert any("subagent thought" in str(b) for b in all_blocks), (
            "Thinking with parent_tool_use_id should still post to active thread"
        )


class TestPostTurnFooter:
    """Tests for post_turn_footer method."""

    async def test_footer_posts_to_main(self):
        streamer, router, provider = make_streamer()
        await streamer.post_turn_footer(":checkered_flag: $0.0100 \u00b7 42% context")
        assert provider.post.call_count == 1
        call = provider.post.call_args
        assert "0.0100" in call.args[0]
        assert call.kwargs.get("blocks")

    async def test_footer_contains_divider(self):
        streamer, router, provider = make_streamer()
        await streamer.post_turn_footer(":checkered_flag: $0.01")
        blocks = provider.post.call_args.kwargs["blocks"]
        assert any(b["type"] == "divider" for b in blocks)


class TestFileChangeCallback:
    async def test_edit_fires_callback(self):
        changes = []

        async def on_change(change):
            changes.append(change)

        client = make_mock_slack_client()
        router = ThreadRouter(client)
        router.active_thread_ts = "thread_1"
        streamer = ResponseStreamer(router, on_file_change=on_change)
        streamer._current_turn_number = 1

        edit_block = make_tool_use_block(
            "Edit",
            {"path": "/src/main.py", "old_string": "old\n", "new_string": "new\n"},
        )
        msg = make_assistant_message([edit_block])
        await streamer._handle_assistant_message(msg)
        result_block = ToolResultBlock(tool_use_id="tu_1", content="OK")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)

        assert len(changes) == 1
        assert changes[0].path == "/src/main.py"
        assert changes[0].change_type == "modified"
        assert changes[0].additions == 1
        assert changes[0].deletions == 1
        assert changes[0].turn_number == 1

    async def test_write_fires_callback_as_created(self):
        changes = []

        async def on_change(change):
            changes.append(change)

        client = make_mock_slack_client()
        router = ThreadRouter(client)
        router.active_thread_ts = "thread_1"
        streamer = ResponseStreamer(router, on_file_change=on_change)
        streamer._current_turn_number = 2

        write_block = make_tool_use_block(
            "Write",
            {"file_path": "/src/new_file.py", "content": "line1\nline2\n"},
        )
        msg = make_assistant_message([write_block])
        await streamer._handle_assistant_message(msg)
        result_block = ToolResultBlock(tool_use_id="tu_1", content="OK")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)

        assert len(changes) == 1
        assert changes[0].path == "/src/new_file.py"
        assert changes[0].change_type == "created"
        assert changes[0].additions == 2

    async def test_no_callback_when_none(self):
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        router.active_thread_ts = "thread_1"
        streamer = ResponseStreamer(router, on_file_change=None)

        edit_block = make_tool_use_block(
            "Edit",
            {"path": "/src/main.py", "old_string": "old\n", "new_string": "new\n"},
        )
        msg = make_assistant_message([edit_block])
        # Should not raise
        await streamer._handle_assistant_message(msg)
        result_block = ToolResultBlock(tool_use_id="tu_1", content="OK")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)


class TestWorktreeDetectionCallback:
    """Tests for EnterWorktree detection in ResponseStreamer."""

    async def test_enter_worktree_triggers_callback_on_success(self):
        """Callback fires on successful ToolResultBlock, not on ToolUseBlock."""
        callback = AsyncMock()
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, on_worktree_entered=callback)

        use_block = make_tool_use_block("EnterWorktree", {"name": "test-wt"})
        use_msg = make_assistant_message([use_block])
        await streamer._handle_assistant_message(use_msg)
        await asyncio.sleep(0.05)
        # Not yet — callback should NOT fire on ToolUseBlock
        callback.assert_not_called()

        # Now simulate the successful result
        result_block = ToolResultBlock(tool_use_id=use_block.id, content="Worktree created")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)
        callback.assert_called_once_with("test-wt", "")

    async def test_enter_worktree_does_not_trigger_on_error(self):
        """Callback must NOT fire when EnterWorktree fails."""
        callback = AsyncMock()
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, on_worktree_entered=callback)

        use_block = make_tool_use_block("EnterWorktree", {"name": "bad-wt"})
        use_msg = make_assistant_message([use_block])
        await streamer._handle_assistant_message(use_msg)

        # Error result
        result_block = ToolResultBlock(
            tool_use_id=use_block.id, content="Permission denied", is_error=True
        )
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)
        callback.assert_not_called()

    async def test_other_tools_do_not_trigger_callback(self):
        """Non-EnterWorktree tools should NOT fire the callback."""
        callback = AsyncMock()
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, on_worktree_entered=callback)

        use_block = make_tool_use_block("Read", {"file_path": "/f"})
        use_msg = make_assistant_message([use_block])
        await streamer._handle_assistant_message(use_msg)

        result_block = ToolResultBlock(tool_use_id=use_block.id, content="file contents")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)
        callback.assert_not_called()

    async def test_enter_worktree_empty_name_passes_empty_string(self):
        """EnterWorktree with missing name key should pass empty string to callback."""
        callback = AsyncMock()
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, on_worktree_entered=callback)

        use_block = make_tool_use_block("EnterWorktree", {})  # no name key
        use_msg = make_assistant_message([use_block])
        await streamer._handle_assistant_message(use_msg)

        result_block = ToolResultBlock(tool_use_id=use_block.id, content="OK")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)
        callback.assert_called_once_with("", "")

    async def test_no_callback_no_error(self):
        """EnterWorktree without callback configured should not raise."""
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router)  # no on_worktree_entered

        use_block = make_tool_use_block("EnterWorktree", {"name": "test"})
        use_msg = make_assistant_message([use_block])
        await streamer._handle_assistant_message(use_msg)

        result_block = ToolResultBlock(tool_use_id=use_block.id, content="OK")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)
        # No error = pass

    async def test_enter_worktree_path_triggers_callback_on_success(self):
        """Path-only EnterWorktree passes ('', path) to callback on success."""
        callback = AsyncMock()
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, on_worktree_entered=callback)

        wt_path = "/project/.claude/worktrees/feat"
        use_block = make_tool_use_block("EnterWorktree", {"path": wt_path})
        use_msg = make_assistant_message([use_block])
        await streamer._handle_assistant_message(use_msg)
        await asyncio.sleep(0.05)
        callback.assert_not_called()

        result_block = ToolResultBlock(tool_use_id=use_block.id, content="Worktree entered")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)
        callback.assert_called_once_with("", wt_path)

    async def test_enter_worktree_both_name_and_path_uses_name(self):
        """When both name and path are present, name wins and path is cleared."""
        callback = AsyncMock()
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, on_worktree_entered=callback)

        use_block = make_tool_use_block(
            "EnterWorktree", {"name": "feat", "path": "/project/.claude/worktrees/feat"}
        )
        use_msg = make_assistant_message([use_block])
        await streamer._handle_assistant_message(use_msg)

        result_block = ToolResultBlock(tool_use_id=use_block.id, content="OK")
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)
        # Name wins — path is cleared to ""
        callback.assert_called_once_with("feat", "")

    async def test_enter_worktree_path_error_does_not_trigger_callback(self):
        """Path-only EnterWorktree failure must NOT fire the callback."""
        callback = AsyncMock()
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, on_worktree_entered=callback)

        wt_path = "/project/.claude/worktrees/feat"
        use_block = make_tool_use_block("EnterWorktree", {"path": wt_path})
        use_msg = make_assistant_message([use_block])
        await streamer._handle_assistant_message(use_msg)

        result_block = ToolResultBlock(
            tool_use_id=use_block.id, content="No worktree at path", is_error=True
        )
        result_msg = make_assistant_message([result_block])
        await streamer._handle_assistant_message(result_msg)
        await asyncio.sleep(0.05)
        callback.assert_not_called()


class TestFormatToolResult:
    """Tests for _format_tool_result."""

    def test_success_string_content_shows_check_mark(self):
        block = ToolResultBlock(tool_use_id="tu_1", content="some output", is_error=False)
        text, blocks = _format_tool_result(block)
        assert text == "Tool result"
        assert len(blocks) == 1
        assert ":white_check_mark:" in blocks[0]["elements"][0]["text"]
        assert ":x:" not in blocks[0]["elements"][0]["text"]

    def test_none_is_error_treated_as_success(self):
        block = ToolResultBlock(tool_use_id="tu_1", content="some output", is_error=None)
        text, blocks = _format_tool_result(block)
        assert ":white_check_mark:" in blocks[0]["elements"][0]["text"]

    def test_error_string_content_shows_x_emoji(self):
        block = ToolResultBlock(tool_use_id="tu_1", content="something went wrong", is_error=True)
        text, blocks = _format_tool_result(block)
        assert text == "Tool result"
        assert len(blocks) == 1
        element_text = blocks[0]["elements"][0]["text"]
        assert ":x:" in element_text
        assert ":white_check_mark:" not in element_text
        assert "Tool error:" in element_text
        assert "something went wrong" in element_text

    def test_error_content_is_redacted(self):
        secret = "sk-ant-abc123secret"
        block = ToolResultBlock(tool_use_id="tu_1", content=f"auth failed: {secret}", is_error=True)
        text, blocks = _format_tool_result(block)
        element_text = blocks[0]["elements"][0]["text"]
        assert secret not in element_text

    def test_error_content_truncated_at_200_chars(self):
        long_error = "x" * 250
        block = ToolResultBlock(tool_use_id="tu_1", content=long_error, is_error=True)
        text, blocks = _format_tool_result(block)
        element_text = blocks[0]["elements"][0]["text"]
        assert element_text.endswith("...")
        # prefix + 200 chars + "..."
        assert len(element_text) < 230

    def test_empty_content_returns_empty(self):
        block = ToolResultBlock(tool_use_id="tu_1", content="", is_error=False)
        text, blocks = _format_tool_result(block)
        assert text == ""
        assert blocks == []

    def test_error_non_string_content_shows_generic_error(self):
        block = ToolResultBlock(
            tool_use_id="tu_1", content=[{"type": "text", "text": "err"}], is_error=True
        )
        text, blocks = _format_tool_result(block)
        element_text = blocks[0]["elements"][0]["text"]
        assert ":x:" in element_text
        assert "Tool error" in element_text

    def test_success_non_string_content_shows_completed(self):
        block = ToolResultBlock(
            tool_use_id="tu_1", content=[{"type": "text", "text": "ok"}], is_error=False
        )
        text, blocks = _format_tool_result(block)
        element_text = blocks[0]["elements"][0]["text"]
        assert ":white_check_mark:" in element_text
        assert "Tool completed" in element_text


class TestToolNameTracking:
    """Tests for tool_use_id → tool name tracking in _TurnState."""

    def test_tool_use_block_stores_name(self):
        """ToolUseBlock id→name stored in _turn.tool_names."""
        from summon_claude.sessions.response import _TurnState

        turn = _TurnState()
        assert turn.tool_names == {}
        turn.tool_names["tu_abc"] = "mcp__jira__getIssue"
        assert turn.tool_names["tu_abc"] == "mcp__jira__getIssue"

    def test_turn_state_reset_clears_tool_names(self):
        """New _TurnState resets tool_names dict."""
        from summon_claude.sessions.response import _TurnState

        turn1 = _TurnState()
        turn1.tool_names["tu_1"] = "Read"
        turn2 = _TurnState()
        assert turn2.tool_names == {}


class TestStreamerHealthTrackerIntegration:
    """Tests for ResponseStreamer + McpHealthTracker wiring."""

    async def test_error_tool_result_fires_health_tracker(self):
        """Error ToolResultBlock triggers health tracker record."""
        from summon_claude.sessions.mcp_health import McpHealthTracker

        callback = AsyncMock()
        tracker = McpHealthTracker(on_degraded=callback)
        router = MagicMock()
        router.post_to_active_thread = AsyncMock()
        streamer = ResponseStreamer(router=router, mcp_health=tracker)
        # Simulate tool use then result
        streamer._turn.tool_names["tu_1"] = "mcp__jira__getIssue"
        block = ToolResultBlock(tool_use_id="tu_1", content="HTTP 401", is_error=True)
        await streamer._handle_tool_result_block(block, parent_id=None)
        # Auth error should trigger immediate notification
        callback.assert_called_once()

    async def test_success_resets_health_tracker(self):
        """Success ToolResultBlock resets health tracker counter."""
        from summon_claude.sessions.mcp_health import McpHealthTracker

        callback = AsyncMock()
        tracker = McpHealthTracker(on_degraded=callback)
        router = MagicMock()
        router.post_to_active_thread = AsyncMock()
        streamer = ResponseStreamer(router=router, mcp_health=tracker)
        # 2 errors then success
        streamer._turn.tool_names["tu_1"] = "mcp__jira__getIssue"
        streamer._turn.tool_names["tu_2"] = "mcp__jira__getIssue"
        streamer._turn.tool_names["tu_3"] = "mcp__jira__getIssue"
        await streamer._handle_tool_result_block(
            ToolResultBlock(tool_use_id="tu_1", content="err", is_error=True), None
        )
        await streamer._handle_tool_result_block(
            ToolResultBlock(tool_use_id="tu_2", content="err", is_error=True), None
        )
        await streamer._handle_tool_result_block(
            ToolResultBlock(tool_use_id="tu_3", content="ok", is_error=False), None
        )
        assert tracker._failures.get("mcp__jira__") == 0
        callback.assert_not_called()

    async def test_error_content_truncated_to_500_for_health_tracker(self):
        """Health tracker receives at most 500 chars of error content."""
        from summon_claude.sessions.mcp_health import McpHealthTracker

        callback = AsyncMock()
        tracker = McpHealthTracker(on_degraded=callback)
        original_record = tracker.record_tool_result
        recorded_contents: list[str] = []

        async def _spy(tool_name, *, is_error=None, error_content=None):
            if error_content is not None:
                recorded_contents.append(error_content)
            await original_record(tool_name, is_error=is_error, error_content=error_content)

        tracker.record_tool_result = _spy  # type: ignore[assignment]

        router = MagicMock()
        router.post_to_active_thread = AsyncMock()
        streamer = ResponseStreamer(router=router, mcp_health=tracker)
        long_content = "HTTP 401 " + "x" * 600
        streamer._turn.tool_names["tu_1"] = "mcp__jira__getIssue"
        block = ToolResultBlock(tool_use_id="tu_1", content=long_content, is_error=True)
        await streamer._handle_tool_result_block(block, parent_id=None)
        assert len(recorded_contents) == 1
        assert len(recorded_contents[0]) == 500
        assert recorded_contents[0].startswith("HTTP 401")

    async def test_no_tracker_means_no_tracking(self):
        """When mcp_health is None, no tracking occurs."""
        router = MagicMock()
        router.post_to_active_thread = AsyncMock()
        streamer = ResponseStreamer(router=router, mcp_health=None)
        streamer._turn.tool_names["tu_1"] = "mcp__jira__getIssue"
        # Should not raise
        await streamer._handle_tool_result_block(
            ToolResultBlock(tool_use_id="tu_1", content="err", is_error=True), None
        )


class TestApprovalVisibility:
    """Tests for approval label rendering on tool use messages."""

    async def test_tool_use_with_auto_allowed_label(self):
        """Pre-resolved bridge with 'auto-allowed' renders label on tool use."""
        bridge = ApprovalBridge()
        bridge.resolve("Read", ApprovalInfo(label="auto-allowed"))
        streamer, router, client = make_streamer()
        streamer._bridge = bridge
        block = make_tool_use_block("Read", {"file_path": "/tmp/test.py"})

        await streamer._handle_tool_use_block(block, None)

        posted_blocks = client.post.call_args
        # Find the context block with the tool use text
        call_kwargs = posted_blocks[1] if len(posted_blocks) > 1 else {}
        blocks = call_kwargs.get("blocks", [])
        text = blocks[0]["elements"][0]["text"] if blocks else ""
        assert "_(auto-allowed)_" in text
        assert ":hammer_and_wrench:" in text

    async def test_tool_use_with_classifier_label_and_reason(self):
        """Classifier approval renders label with reason."""
        bridge = ApprovalBridge()
        bridge.resolve("Bash", ApprovalInfo(label="auto-mode", reason="local file edit"))
        streamer, router, client = make_streamer()
        streamer._bridge = bridge
        block = make_tool_use_block("Bash", {"command": "git status"})

        await streamer._handle_tool_use_block(block, None)

        posted_blocks = client.post.call_args
        call_kwargs = posted_blocks[1] if len(posted_blocks) > 1 else {}
        blocks = call_kwargs.get("blocks", [])
        text = blocks[0]["elements"][0]["text"] if blocks else ""
        assert "_(auto-mode: local file edit)_" in text

    async def test_denied_tool_uses_denial_emoji(self):
        """Denied tool uses :no_entry_sign: emoji instead of :hammer_and_wrench:."""
        bridge = ApprovalBridge()
        bridge.resolve("Write", ApprovalInfo(label="denied by <@U123>", is_denial=True))
        streamer, router, client = make_streamer()
        streamer._bridge = bridge
        block = make_tool_use_block("Write", {"file_path": "/tmp/test.py"})

        await streamer._handle_tool_use_block(block, None)

        posted_blocks = client.post.call_args
        call_kwargs = posted_blocks[1] if len(posted_blocks) > 1 else {}
        blocks = call_kwargs.get("blocks", [])
        text = blocks[0]["elements"][0]["text"] if blocks else ""
        assert ":no_entry_sign:" in text
        assert ":hammer_and_wrench:" not in text

    async def test_no_bridge_fallback(self):
        """Streamer without bridge posts tool use immediately with no label."""
        streamer, router, client = make_streamer()
        assert streamer._bridge is None
        block = make_tool_use_block("Read", {"file_path": "/tmp/test.py"})

        await streamer._handle_tool_use_block(block, None)

        posted_blocks = client.post.call_args
        call_kwargs = posted_blocks[1] if len(posted_blocks) > 1 else {}
        blocks = call_kwargs.get("blocks", [])
        text = blocks[0]["elements"][0]["text"] if blocks else ""
        assert ":hammer_and_wrench:" in text
        assert "_(" not in text  # No label suffix

    async def test_bridge_timeout_posts_without_label(self):
        """On bridge timeout, tool use posts without label (graceful degradation)."""
        bridge = ApprovalBridge()
        streamer, router, client = make_streamer()
        streamer._bridge = bridge
        block = make_tool_use_block("Read", {"file_path": "/tmp/test.py"})

        # Patch asyncio.wait_for to raise TimeoutError immediately
        timeout_patch = patch(
            "summon_claude.sessions.response.asyncio.wait_for",
            side_effect=asyncio.TimeoutError,
        )
        with timeout_patch:
            await streamer._handle_tool_use_block(block, None)

        posted_blocks = client.post.call_args
        call_kwargs = posted_blocks[1] if len(posted_blocks) > 1 else {}
        blocks = call_kwargs.get("blocks", [])
        text = blocks[0]["elements"][0]["text"] if blocks else ""
        assert ":hammer_and_wrench:" in text
        assert "_(" not in text  # No label

    async def test_subagent_tool_skips_bridge(self):
        """Subagent tool calls (parent_id != None) skip bridge, post immediately."""
        bridge = ApprovalBridge()
        bridge.create_future = MagicMock()
        streamer, router, client = make_streamer()
        streamer._bridge = bridge
        block = make_tool_use_block("Read", {"file_path": "/tmp/test.py"})

        # parent_id is set — subagent context
        router._subagent_threads = {"parent_123": "thread_ts"}
        await streamer._handle_tool_use_block(block, "parent_123")

        bridge.create_future.assert_not_called()

    async def test_enter_worktree_skips_bridge(self):
        """EnterWorktree bypasses can_use_tool — must skip bridge to prevent timeout hang."""
        bridge = ApprovalBridge()
        bridge.create_future = MagicMock()
        streamer, router, client = make_streamer()
        streamer._bridge = bridge
        block = ToolUseBlock(id="tu_wt", name="EnterWorktree", input={"name": "test"})

        await streamer._handle_tool_use_block(block, None)

        bridge.create_future.assert_not_called()

    async def test_exit_worktree_skips_bridge(self):
        """ExitWorktree bypasses can_use_tool — must skip bridge to prevent timeout hang."""
        bridge = ApprovalBridge()
        bridge.create_future = MagicMock()
        streamer, router, client = make_streamer()
        streamer._bridge = bridge
        block = ToolUseBlock(id="tu_exit_wt", name="ExitWorktree", input={})

        await streamer._handle_tool_use_block(block, None)

        bridge.create_future.assert_not_called()

    async def test_denied_tool_result_suppressed(self):
        """Denied tool results (is_error=True) are suppressed — no :x: Tool error."""
        bridge = ApprovalBridge()
        bridge.resolve("Write", ApprovalInfo(label="denied", is_denial=True))
        streamer, router, client = make_streamer()
        streamer._bridge = bridge
        tool_block = ToolUseBlock(id="tu_deny", name="Write", input={"file_path": "/f"})

        await streamer._handle_tool_use_block(tool_block, None)
        client.post.reset_mock()

        result_block = ToolResultBlock(
            tool_use_id="tu_deny",
            content="Denied by user in Slack",
            is_error=True,
        )
        await streamer._handle_tool_result_block(result_block, None)

        # post should NOT have been called for the denied result
        client.post.assert_not_called()

    async def test_approved_tool_result_not_suppressed(self):
        """Approved tool results are posted normally."""
        bridge = ApprovalBridge()
        bridge.resolve("Read", ApprovalInfo(label="auto-allowed"))
        streamer, router, client = make_streamer()
        streamer._bridge = bridge
        tool_block = ToolUseBlock(id="tu_ok", name="Read", input={"file_path": "/f"})

        await streamer._handle_tool_use_block(tool_block, None)
        client.post.reset_mock()

        result_block = ToolResultBlock(
            tool_use_id="tu_ok",
            content="file content here",
            is_error=False,
        )
        await streamer._handle_tool_result_block(result_block, None)

        # post SHOULD have been called for the approved result
        client.post.assert_called()

    async def test_denied_tool_success_result_not_suppressed(self):
        """Denied tool with is_error=False result is NOT suppressed — posts normally."""
        bridge = ApprovalBridge()
        bridge.resolve("Write", ApprovalInfo(label="denied", is_denial=True))
        streamer, router, client = make_streamer()
        streamer._bridge = bridge
        tool_block = ToolUseBlock(id="tu_deny_ok", name="Write", input={"file_path": "/f"})

        await streamer._handle_tool_use_block(tool_block, None)
        client.post.reset_mock()

        # Denied tool but result is NOT an error (is_error=False)
        result_block = ToolResultBlock(
            tool_use_id="tu_deny_ok",
            content="Completed successfully",
            is_error=False,
        )
        await streamer._handle_tool_result_block(result_block, None)

        # Suppression only fires for denied+is_error=True; success posts normally
        client.post.assert_called()


class TestBridgeTimeoutGuard:
    """Guard test: bridge timeout must exceed permission timeout."""

    def test_config_permission_timeout_default_is_900(self):
        """Guard: config default must stay at 15 minutes (900s)."""
        from conftest import make_test_config

        config = make_test_config()
        assert config.permission_timeout_s == 900

    def test_bridge_skip_tools_contains_builtin_bypass_tools(self):
        """Guard: _BRIDGE_SKIP_TOOLS must include tools that bypass can_use_tool."""
        from summon_claude.sessions.response import _BRIDGE_SKIP_TOOLS

        assert "EnterWorktree" in _BRIDGE_SKIP_TOOLS
        assert "ExitWorktree" in _BRIDGE_SKIP_TOOLS


class TestBridgeClearOnNewTurn:
    """Tests for bridge.clear() cancelling stale Futures on stream_with_flush."""

    async def test_stale_future_cancelled_on_second_stream_with_flush(self):
        """Futures pending from a prior turn are cancelled when stream_with_flush starts."""
        bridge = ApprovalBridge()
        streamer, router, client = make_streamer()
        streamer._bridge = bridge

        stale_fut = bridge.create_future("Write")
        assert not stale_fut.done()
        messages = [
            make_assistant_message([make_text_block("hello")]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        # The stale Future must now be cancelled
        assert stale_fut.cancelled()


class TestSanitizeApprovalReason:
    """Tests for the _sanitize_approval_reason helper."""

    def test_empty_string(self):
        assert _sanitize_approval_reason("") == ""

    def test_underscores_replaced_with_spaces(self):
        assert "_" not in _sanitize_approval_reason("safe_file_edit")

    def test_angle_brackets_stripped(self):
        result = _sanitize_approval_reason("injected <@U123> mention")
        assert "<" not in result
        assert ">" not in result

    def test_truncated_to_60_chars(self):
        long_reason = "a" * 100
        result = _sanitize_approval_reason(long_reason)
        assert len(result) <= 60

    def test_bold_and_backtick_stripped(self):
        result = _sanitize_approval_reason("*bold* and `code`")
        assert "*" not in result
        assert "`" not in result


class TestBridgeTimeoutRelationship:
    """Guard: bridge_timeout_s must exceed permission_timeout_s."""

    def test_bridge_timeout_exceeds_permission_timeout(self):
        """session.py wires bridge_timeout_s = permission_timeout_s + 60."""
        from conftest import make_test_config

        config = make_test_config()
        expected = config.permission_timeout_s + 60
        # Default matches the formula: permission_timeout_s (900) + 60 = 960
        streamer, _, _ = make_streamer()
        assert streamer._bridge_timeout_s == expected

    def test_permission_timeout_env_var_binding(self):
        """SUMMON_PERMISSION_TIMEOUT_S env var changes config value."""
        import os

        from summon_claude.config import SummonConfig

        env_patch = {
            "SUMMON_PERMISSION_TIMEOUT_S": "120",
            "SUMMON_SLACK_BOT_TOKEN": "xoxb-t",
            "SUMMON_SLACK_APP_TOKEN": "xapp-t",
            "SUMMON_SLACK_SIGNING_SECRET": "abc123",
        }
        saved = {}
        for k, v in env_patch.items():
            saved[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            config = SummonConfig(_env_file=None)
            assert config.permission_timeout_s == 120
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_permission_timeout_zero_means_indefinite(self):
        """permission_timeout_s=0 produces None timeout (indefinite)."""
        from conftest import make_test_config

        from summon_claude.sessions.permissions import PermissionHandler
        from summon_claude.slack.router import ThreadRouter

        config = make_test_config(permission_timeout_s=0)
        from helpers import make_mock_slack_client

        client = make_mock_slack_client()
        router = ThreadRouter(client)
        handler = PermissionHandler(router, config, authenticated_user_id="U_TEST")
        assert handler._timeout_s is None

    def test_permission_timeout_negative_rejected(self):
        """Negative permission_timeout_s is rejected by validator."""
        import pytest

        from summon_claude.config import SummonConfig

        with pytest.raises(Exception, match="must be >= 0"):
            SummonConfig(
                slack_bot_token="xoxb-t",
                slack_app_token="xapp-t",
                slack_signing_secret="abc123",
                permission_timeout_s=-1,
                _env_file=None,
            )


class TestPendingFileChangeLifecycle:
    async def test_failed_tool_no_file_change_or_upload(self):
        changes = []

        async def on_change(change):
            changes.append(change)

        client = make_mock_slack_client()
        router = ThreadRouter(client)
        router.active_thread_ts = "thread_1"
        streamer = ResponseStreamer(router, on_file_change=on_change)

        write_block = make_tool_use_block(
            "Write",
            {"file_path": "/src/out.py", "content": "print('hi')"},
        )
        msg = make_assistant_message([write_block])
        await streamer._handle_assistant_message(msg)

        failed_result = ToolResultBlock(tool_use_id="tu_1", content="Error", is_error=True)
        result_msg = make_assistant_message([failed_result])
        with patch.object(streamer, "_upload_write", new_callable=AsyncMock) as mock_upload:
            await streamer._handle_assistant_message(result_msg)
            await asyncio.sleep(0.05)
            mock_upload.assert_not_called()

        assert len(changes) == 0

    async def test_denied_tool_error_no_file_change_or_upload(self):
        """Denial (is_error=True + denied_tool_use_ids) hits the early-return."""
        changes = []

        async def on_change(change):
            changes.append(change)

        client = make_mock_slack_client()
        router = ThreadRouter(client)
        router.active_thread_ts = "thread_1"
        streamer = ResponseStreamer(router, on_file_change=on_change)

        write_block = make_tool_use_block(
            "Write",
            {"file_path": "/src/out.py", "content": "print('hi')"},
        )
        msg = make_assistant_message([write_block])
        await streamer._handle_assistant_message(msg)

        # Mark the tool use as denied before processing the result
        streamer._turn.denied_tool_use_ids.add("tu_1")

        denied_result = ToolResultBlock(tool_use_id="tu_1", content="Denied", is_error=True)
        result_msg = make_assistant_message([denied_result])
        with patch.object(streamer, "_upload_write", new_callable=AsyncMock) as mock_upload:
            await streamer._handle_assistant_message(result_msg)
            await asyncio.sleep(0.05)
            mock_upload.assert_not_called()

        assert len(changes) == 0

    async def test_denied_tool_success_result_not_triggered(self):
        changes = []

        async def on_change(change):
            changes.append(change)

        client = make_mock_slack_client()
        router = ThreadRouter(client)
        router.active_thread_ts = "thread_1"
        streamer = ResponseStreamer(router, on_file_change=on_change)

        write_block = make_tool_use_block(
            "Write",
            {"file_path": "/src/out.py", "content": "print('hi')"},
        )
        msg = make_assistant_message([write_block])
        await streamer._handle_assistant_message(msg)

        # Manually mark as denied even though result is is_error=False
        streamer._turn.denied_tool_use_ids.add("tu_1")

        success_result = ToolResultBlock(tool_use_id="tu_1", content="OK", is_error=False)
        result_msg = make_assistant_message([success_result])
        with patch.object(streamer, "_upload_write", new_callable=AsyncMock) as mock_upload:
            await streamer._handle_assistant_message(result_msg)
            await asyncio.sleep(0.05)
            mock_upload.assert_not_called()

        assert len(changes) == 0

    async def test_successful_edit_fires_deferred_upload(self):
        changes = []

        async def on_change(change):
            changes.append(change)

        client = make_mock_slack_client()
        router = ThreadRouter(client)
        router.active_thread_ts = "thread_1"
        streamer = ResponseStreamer(router, on_file_change=on_change)

        edit_block = make_tool_use_block(
            "Edit",
            {"path": "/src/main.py", "old_string": "old\n", "new_string": "new\n"},
        )
        msg = make_assistant_message([edit_block])
        await streamer._handle_assistant_message(msg)

        success_result = ToolResultBlock(tool_use_id="tu_1", content="OK")
        result_msg = make_assistant_message([success_result])
        with patch.object(streamer, "_upload_diff", new_callable=AsyncMock) as mock_upload_diff:
            await streamer._handle_assistant_message(result_msg)
            await asyncio.sleep(0.05)
            mock_upload_diff.assert_called_once()

        assert len(changes) == 1

    async def test_no_active_thread_skips_upload_but_fires_callback(self):
        """RuntimeError from _resolve_upload_thread silently skips upload; callback still fires."""
        changes = []

        async def on_change(change):
            changes.append(change)

        client = make_mock_slack_client()
        router = ThreadRouter(client)
        streamer = ResponseStreamer(router, on_file_change=on_change)

        edit_block = make_tool_use_block(
            "Edit",
            {"path": "/src/main.py", "old_string": "old\n", "new_string": "new\n"},
        )
        msg = make_assistant_message([edit_block])
        await streamer._handle_assistant_message(msg)

        success_result = ToolResultBlock(tool_use_id="tu_1", content="OK")
        result_msg = make_assistant_message([success_result])
        with patch.object(streamer, "_upload_diff", new_callable=AsyncMock) as mock_upload_diff:
            await streamer._handle_assistant_message(result_msg)
            await asyncio.sleep(0.05)
            mock_upload_diff.assert_not_called()

        assert len(changes) == 1

    async def test_str_replace_editor_fires_deferred_upload(self):
        """str_replace_editor tool (grouped with Edit) triggers diff upload."""
        changes = []

        async def on_change(change):
            changes.append(change)

        client = make_mock_slack_client()
        router = ThreadRouter(client)
        router.active_thread_ts = "thread_1"
        streamer = ResponseStreamer(router, on_file_change=on_change)

        edit_block = make_tool_use_block(
            "str_replace_editor",
            {"path": "/src/main.py", "old_string": "old\n", "new_string": "new\n"},
        )
        msg = make_assistant_message([edit_block])
        await streamer._handle_assistant_message(msg)

        success_result = ToolResultBlock(tool_use_id="tu_1", content="OK")
        result_msg = make_assistant_message([success_result])
        with patch.object(streamer, "_upload_diff", new_callable=AsyncMock) as mock_upload_diff:
            await streamer._handle_assistant_message(result_msg)
            await asyncio.sleep(0.05)
            mock_upload_diff.assert_called_once()

        assert len(changes) == 1

    async def test_subagent_edit_deferred_upload_targets_subagent_thread(self):
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        router.active_thread_ts = "active_ts"
        subagent_ts = await router.start_subagent_thread("task_123", "desc")
        streamer = ResponseStreamer(router)

        edit_block = make_tool_use_block(
            "Edit",
            {"path": "/src/main.py", "old_string": "old\n", "new_string": "new\n"},
        )
        tool_msg = AssistantMessage(
            content=[edit_block],
            model="claude-opus-4-6",
            parent_tool_use_id="task_123",
        )
        await streamer._handle_assistant_message(tool_msg)

        success_result = ToolResultBlock(tool_use_id="tu_1", content="OK")
        result_msg = AssistantMessage(
            content=[success_result],
            model="claude-opus-4-6",
            parent_tool_use_id="task_123",
        )
        with patch.object(streamer, "_upload_diff", new_callable=AsyncMock) as mock_upload_diff:
            await streamer._handle_assistant_message(result_msg)
            await asyncio.sleep(0.05)
            mock_upload_diff.assert_called_once()
            _, _, _, thread_ts_arg = mock_upload_diff.call_args.args
            assert thread_ts_arg == subagent_ts
            assert thread_ts_arg != "active_ts"


# ---------------------------------------------------------------------------
# comp-5: _build_turn_header_blocks (overflow menu accessory)
# ---------------------------------------------------------------------------


class TestBuildTurnHeaderBlocks:
    """Tests for the _build_turn_header_blocks helper — comp-5 overflow menus."""

    def test_returns_single_section_block(self):
        """_build_turn_header_blocks returns exactly one block of type section."""
        from summon_claude.sessions.response import _build_turn_header_blocks

        blocks = _build_turn_header_blocks("Turn 1: Processing...")
        assert len(blocks) == 1
        assert blocks[0]["type"] == "section"

    def test_section_contains_text(self):
        """The section block includes the provided header text."""
        from summon_claude.sessions.response import _build_turn_header_blocks

        blocks = _build_turn_header_blocks("Turn 42: some work")
        text_field = blocks[0]["text"]
        assert text_field["type"] == "mrkdwn"
        assert "Turn 42: some work" in text_field["text"]

    def test_accessory_is_overflow(self):
        """The section block has an overflow accessory."""
        from summon_claude.sessions.response import _build_turn_header_blocks

        blocks = _build_turn_header_blocks("header")
        accessory = blocks[0]["accessory"]
        assert accessory["type"] == "overflow"
        assert accessory["action_id"] == "turn_overflow"

    def test_overflow_has_three_options(self):
        """The overflow menu has exactly 3 options."""
        from summon_claude.sessions.response import _build_turn_header_blocks

        blocks = _build_turn_header_blocks("header")
        options = blocks[0]["accessory"]["options"]
        assert len(options) == 3

    def test_overflow_option_values(self):
        """The overflow options have the expected value strings."""
        from summon_claude.sessions.response import _build_turn_header_blocks

        blocks = _build_turn_header_blocks("header")
        values = {opt["value"] for opt in blocks[0]["accessory"]["options"]}
        assert values == {"turn_stop", "turn_copy_sid", "turn_view_cost"}

    async def test_start_turn_posts_blocks_with_overflow(self):
        """start_turn posts Block Kit with an overflow accessory."""
        streamer, router, client = make_streamer()
        client.post = AsyncMock(return_value=MagicMock(channel_id="C123", ts="111.0"))

        await streamer.start_turn(turn_number=1)

        client.post.assert_awaited_once()
        call_kwargs = client.post.call_args.kwargs
        blocks = call_kwargs.get("blocks")
        assert blocks is not None, "Expected blocks kwarg in post call"
        assert len(blocks) == 1
        assert blocks[0]["accessory"]["type"] == "overflow"

    async def test_update_turn_summary_posts_blocks_with_overflow(self):
        """update_turn_summary preserves overflow accessory in update call."""
        streamer, router, client = make_streamer()
        client.post = AsyncMock(return_value=MagicMock(channel_id="C123", ts="111.0"))
        await streamer.start_turn(turn_number=1)

        await streamer.update_turn_summary("3 tool calls")

        client.update.assert_awaited_once()
        update_kwargs = client.update.call_args.kwargs
        blocks = update_kwargs.get("blocks")
        assert blocks is not None, "Expected blocks kwarg in update call"
        assert blocks[0]["accessory"]["type"] == "overflow"


class TestHybridStreaming:
    """Tests for the chat_stream hybrid streaming integration."""

    def _make_stream_streamer(self):
        """Create a streamer with streaming enabled and a mock AsyncChatStream."""
        mock_stream = AsyncMock()
        mock_stream.append = AsyncMock()
        mock_stream.stop = AsyncMock()
        streamer, router, client = make_streamer(team_id="T123", user_id="U456")
        client.open_chat_stream = AsyncMock(return_value=mock_stream)
        return streamer, router, client, mock_stream

    async def _setup_turn(self, streamer, client):
        """Start a turn so turn_thread_ts is set."""
        client.post = AsyncMock(return_value=MagicMock(channel_id="C123", ts="turn.0"))
        await streamer.start_turn(turn_number=1)

    async def test_stream_opened_on_first_tool_use(self):
        """A chat stream is opened when the first ToolUseBlock arrives."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)

        messages = [
            make_assistant_message([make_tool_use_block("Read", {"file_path": "/a.py"})]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        client.open_chat_stream.assert_awaited_once_with("turn.0", team_id="T123", user_id="U456")

    async def test_task_update_in_progress_emitted(self):
        """TaskUpdateChunk(in_progress) is appended to the stream on ToolUseBlock."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)

        messages = [
            make_assistant_message([make_tool_use_block("Read", {"file_path": "/a.py"})]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        # Find the append call with chunks (TaskUpdateChunk)
        chunk_calls = [c for c in mock_stream.append.call_args_list if c.kwargs.get("chunks")]
        assert len(chunk_calls) >= 1
        chunk = chunk_calls[0].kwargs["chunks"][0]
        assert chunk.status == "in_progress"
        assert chunk.title == "Read"

    async def test_task_update_complete_on_success(self):
        """TaskUpdateChunk(complete) is emitted on successful ToolResultBlock."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)

        tool_result = ToolResultBlock(tool_use_id="tu_1", content="file content", is_error=False)
        messages = [
            make_assistant_message([make_tool_use_block("Read", {"file_path": "/a.py"})]),
            make_assistant_message([tool_result]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        chunk_calls = [c for c in mock_stream.append.call_args_list if c.kwargs.get("chunks")]
        assert len(chunk_calls) >= 2
        complete_chunk = chunk_calls[1].kwargs["chunks"][0]
        assert complete_chunk.status == "complete"
        assert complete_chunk.title == "Read"

    async def test_task_update_error_on_failure(self):
        """TaskUpdateChunk(error) is emitted on failed ToolResultBlock."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)

        tool_result = ToolResultBlock(tool_use_id="tu_1", content="file not found", is_error=True)
        messages = [
            make_assistant_message([make_tool_use_block("Read", {"file_path": "/a.py"})]),
            make_assistant_message([tool_result]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        chunk_calls = [c for c in mock_stream.append.call_args_list if c.kwargs.get("chunks")]
        error_chunk = chunk_calls[-1].kwargs["chunks"][0]
        assert error_chunk.status == "error"

    async def test_stream_stopped_with_summary_blocks(self):
        """Stream stop includes summary blocks with tool count and files."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)

        tool_result = ToolResultBlock(tool_use_id="tu_1", content="ok", is_error=False)
        messages = [
            make_assistant_message([make_tool_use_block("Read", {"file_path": "/src/a.py"})]),
            make_assistant_message([tool_result]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        # stop() should have been called with summary blocks
        mock_stream.stop.assert_awaited()
        stop_kwargs = mock_stream.stop.call_args.kwargs
        blocks = stop_kwargs.get("blocks")
        assert blocks is not None, "stop() should include summary blocks"
        assert blocks[0]["type"] == "context"
        summary_text = blocks[0]["elements"][0]["text"]
        assert "1 tool call" in summary_text
        assert "a.py" in summary_text

    async def test_stream_stopped_on_result(self):
        """The stream is stopped when ResultMessage arrives."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)

        messages = [
            make_assistant_message([make_tool_use_block("Read", {"file_path": "/a.py"})]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        mock_stream.stop.assert_awaited_once()

    async def test_fallback_on_stream_open_failure(self):
        """Falls back to chat_postMessage when stream open fails."""
        streamer, router, client, _ = self._make_stream_streamer()
        await self._setup_turn(streamer, client)
        client.open_chat_stream = AsyncMock(side_effect=Exception("stream_error"))
        # Reset post mock to track new calls (start_turn already called post)
        client.post = AsyncMock(return_value=MagicMock(channel_id="C123", ts="msg.1"))

        messages = [
            make_assistant_message([make_tool_use_block("Read", {"file_path": "/a.py"})]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        # Tool use context block should still be posted via chat_postMessage
        tool_post_calls = [
            c
            for c in client.post.call_args_list
            if "blocks" in (c.kwargs or {}) and any("hammer" in str(b) for b in c.kwargs["blocks"])
        ]
        assert len(tool_post_calls) >= 1, "Tool use block should be posted via chat_postMessage"

    async def test_fallback_on_stream_append_failure(self):
        """Falls back after initial success when a later append fails."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)

        # First append (in_progress) succeeds, second (complete) fails
        call_count = 0
        original_append = AsyncMock()

        async def _append_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise Exception("append_failed")
            return await original_append(**kwargs)

        mock_stream.append = AsyncMock(side_effect=_append_side_effect)

        tool_result = ToolResultBlock(tool_use_id="tu_1", content="ok", is_error=False)
        messages = [
            make_assistant_message([make_tool_use_block("Read", {"file_path": "/a.py"})]),
            make_assistant_message([tool_result]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        # Stream should have been stopped after failure
        mock_stream.stop.assert_awaited()
        # stream_failed should prevent further stream attempts
        assert streamer._turn.stream_failed is True

    async def test_flush_to_thread_uses_stream_when_active(self):
        """_flush_to_thread routes through stream.append when a stream is open."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)

        # Manually open a stream and set up thread flushing state
        streamer._turn.active_stream = mock_stream
        streamer._turn.posting_to_thread = True
        streamer._turn.buffer = "Some thread text"

        await streamer._flush_buffer()

        # Text should have gone to stream.append(markdown_text=...) not client.post
        md_calls = [c for c in mock_stream.append.call_args_list if c.kwargs.get("markdown_text")]
        assert len(md_calls) == 1
        assert md_calls[0].kwargs["markdown_text"] == "Some thread text"

    async def test_flush_to_thread_stream_redacts_secrets(self):
        """Secrets are redacted when flushing through the stream path."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)

        streamer._turn.active_stream = mock_stream
        streamer._turn.posting_to_thread = True
        streamer._turn.buffer = "Token is xoxb-secret-token-here"

        await streamer._flush_buffer()

        md_calls = [c for c in mock_stream.append.call_args_list if c.kwargs.get("markdown_text")]
        assert len(md_calls) == 1
        streamed_text = md_calls[0].kwargs["markdown_text"]
        assert "xoxb-" not in streamed_text
        assert "[REDACTED]" in streamed_text

    async def test_flush_to_thread_stream_logs_validation_warnings(self, caplog):
        """validate_agent_output warnings are logged when flushing through the stream path."""
        import logging
        from unittest.mock import patch

        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)

        streamer._turn.active_stream = mock_stream
        streamer._turn.posting_to_thread = True
        streamer._turn.buffer = "some text"

        with (
            patch(
                "summon_claude.sessions.response.validate_agent_output",
                return_value=("some text", ["warning: suspicious pattern detected"]),
            ),
            caplog.at_level(logging.WARNING, logger="summon_claude.sessions.response"),
        ):
            await streamer._flush_buffer()

        assert any("suspicious pattern detected" in record.message for record in caplog.records)

    async def test_no_stream_without_team_id(self):
        """No stream is opened when team_id is not set."""
        streamer, router, client = make_streamer(user_id="U456")
        client.post = AsyncMock(return_value=MagicMock(channel_id="C123", ts="turn.0"))
        await streamer.start_turn(turn_number=1)

        messages = [
            make_assistant_message([make_tool_use_block("Read", {"file_path": "/a.py"})]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        client.open_chat_stream.assert_not_awaited()

    async def test_no_stream_without_user_id(self):
        """No stream is opened when user_id is not set."""
        streamer, router, client = make_streamer(team_id="T123")
        client.post = AsyncMock(return_value=MagicMock(channel_id="C123", ts="turn.0"))
        await streamer.start_turn(turn_number=1)

        messages = [
            make_assistant_message([make_tool_use_block("Read", {"file_path": "/a.py"})]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        client.open_chat_stream.assert_not_awaited()

    async def test_no_stream_for_subagent_tools(self):
        """TaskUpdateChunks are not emitted for subagent tool calls (parent_id set)."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)

        # Simulate a subagent tool use (parent_tool_use_id set)
        subagent_msg = AssistantMessage(
            content=[make_tool_use_block("Read", {"file_path": "/b.py"}, tool_use_id="tu_sub")],
            model="claude-opus-4-6",
            parent_tool_use_id="parent_tu_1",
        )
        messages = [
            subagent_msg,
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        # Stream should NOT have been opened for subagent tool
        client.open_chat_stream.assert_not_awaited()

    async def test_stream_reused_across_tool_calls(self):
        """The same stream is reused for multiple tool calls in one turn."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)

        tool_result_1 = ToolResultBlock(tool_use_id="tu_1", content="ok", is_error=False)
        tool_use_2 = make_tool_use_block("Grep", {"pattern": "foo"}, tool_use_id="tu_2")
        tool_result_2 = ToolResultBlock(tool_use_id="tu_2", content="found", is_error=False)
        messages = [
            make_assistant_message([make_tool_use_block("Read", {"file_path": "/a.py"})]),
            make_assistant_message([tool_result_1]),
            make_assistant_message([tool_use_2]),
            make_assistant_message([tool_result_2]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        # open_chat_stream should only be called once
        client.open_chat_stream.assert_awaited_once()
        # But there should be 4 chunk appends (2 in_progress + 2 complete)
        chunk_calls = [c for c in mock_stream.append.call_args_list if c.kwargs.get("chunks")]
        assert len(chunk_calls) == 4

    async def test_stream_stop_failure_is_silent(self):
        """stream.stop() failure does not propagate."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)
        mock_stream.stop = AsyncMock(side_effect=Exception("stop_failed"))

        messages = [
            make_assistant_message([make_tool_use_block("Read", {"file_path": "/a.py"})]),
            make_result_message(),
        ]
        # Should not raise
        result = await streamer.stream_with_flush(agen(messages))
        assert result is not None

    async def test_denied_tool_skips_task_update_chunks(self):
        """Denied tools do not emit TaskUpdateChunk (in_progress or error)."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)
        # Simulate a turn already in progress (reset doesn't wipe these)
        streamer._turn.active_stream = mock_stream
        streamer._turn.has_seen_tool_use = True

        # Simulate denial: add tool_use_id to denied set (as bridge would)
        streamer._turn.denied_tool_use_ids.add("tu_denied")
        streamer._turn.tool_names["tu_denied"] = "Bash"

        # Directly call _handle_tool_use_block with a denied tool
        denied_block = make_tool_use_block("Bash", {"command": "rm"}, tool_use_id="tu_denied")
        await streamer._handle_tool_use_block(denied_block, parent_id=None)

        # in_progress should NOT have been emitted for the denied tool
        chunk_calls = [c for c in mock_stream.append.call_args_list if c.kwargs.get("chunks")]
        assert len(chunk_calls) == 0, (
            f"Denied tool should not emit in_progress TaskUpdateChunk, got {chunk_calls}"
        )

        # Now handle the tool result (denied + error)
        denied_result = ToolResultBlock(tool_use_id="tu_denied", content="denied", is_error=True)
        await streamer._handle_tool_result_block(denied_result, parent_id=None)

        # complete/error should NOT have been emitted either
        chunk_calls = [c for c in mock_stream.append.call_args_list if c.kwargs.get("chunks")]
        assert len(chunk_calls) == 0, (
            f"Denied tool should not emit any TaskUpdateChunks, got {chunk_calls}"
        )

    async def test_text_only_no_stream_opened(self):
        """Text-only content (no ToolUseBlock) never opens a stream — stop() not called."""
        streamer, router, client, mock_stream = self._make_stream_streamer()
        await self._setup_turn(streamer, client)

        messages = [
            make_assistant_message([make_text_block("Here is the answer.")]),
            make_result_message(),
        ]
        result = await streamer.stream_with_flush(agen(messages))

        client.open_chat_stream.assert_not_awaited()
        mock_stream.stop.assert_not_awaited()
        assert result is not None
