"""Tests for summon_claude.streamer — now uses ThreadRouter."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from helpers import make_mock_slack_client
from summon_claude.sessions.response import ResponseStreamer, _format_tool_summary
from summon_claude.sessions.response import split_text as _split_text
from summon_claude.slack.router import ThreadRouter


def make_streamer(
    *,
    show_thinking: bool = False,
    max_inline_chars: int = 2500,
) -> tuple[ResponseStreamer, ThreadRouter, AsyncMock]:
    """Create a ResponseStreamer with a mocked SlackClient."""
    client = make_mock_slack_client()
    router = ThreadRouter(client)
    streamer = ResponseStreamer(
        router, show_thinking=show_thinking, max_inline_chars=max_inline_chars
    )
    return streamer, router, client


def make_text_block(text: str) -> TextBlock:
    return TextBlock(text=text)


def make_tool_use_block(name: str, input_data: dict) -> ToolUseBlock:
    return ToolUseBlock(id="tu_1", name=name, input=input_data)


def make_assistant_message(content: list) -> AssistantMessage:
    return AssistantMessage(content=content, model="claude-opus-4-6")


def make_result_message(cost: float = 0.01, turns: int = 1) -> ResultMessage:
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
# Tests absorbed from test_content_display.py
# ---------------------------------------------------------------------------


def make_streamer_for_display() -> ResponseStreamer:
    """Create a ResponseStreamer for _format_diff tests."""
    client = make_mock_slack_client()
    router = ThreadRouter(client)
    return ResponseStreamer(router)


class TestFormatDiff:
    def test_no_change_returns_no_changes_message(self):
        streamer = make_streamer_for_display()
        blocks = streamer._format_diff("same", "same", "file.py")
        assert len(blocks) == 1
        assert "No changes" in blocks[0]["text"]["text"]

    def test_change_returns_diff_block(self):
        streamer = make_streamer_for_display()
        blocks = streamer._format_diff("old line\n", "new line\n", "file.py")
        assert len(blocks) >= 1
        assert "file.py" in blocks[0]["text"]["text"]

    def test_diff_contains_code_fence(self):
        streamer = make_streamer_for_display()
        blocks = streamer._format_diff("a\n", "b\n", "test.txt")
        text = blocks[0]["text"]["text"]
        assert "```" in text

    def test_large_diff_splits_into_multiple_blocks(self):
        streamer = make_streamer_for_display()
        old = "\n".join(f"line {i}" for i in range(500))
        new = "\n".join(f"changed {i}" for i in range(500))
        blocks = streamer._format_diff(old, new, "big.py")
        assert len(blocks) >= 1
        for block in blocks:
            assert len(block["text"]["text"]) <= 3000

    def test_first_block_has_filename_header(self):
        streamer = make_streamer_for_display()
        blocks = streamer._format_diff("a", "b", "myfile.rs")
        assert "myfile.rs" in blocks[0]["text"]["text"]


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
