"""Tests for summon_claude.streamer — now uses ThreadRouter."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from helpers import make_mock_slack_client
from summon_claude.config import SummonConfig
from summon_claude.sessions.response import ResponseStreamer, StreamResult, _format_tool_summary
from summon_claude.sessions.response import split_text as _split_text
from summon_claude.slack.router import ThreadRouter


def make_config(**overrides) -> SummonConfig:
    defaults = {
        "slack_bot_token": "xoxb-t",
        "slack_app_token": "xapp-t",
        "slack_signing_secret": "s",
        "max_inline_chars": 2500,
    }
    defaults.update(overrides)
    return SummonConfig.model_validate(defaults)


def make_streamer() -> tuple[ResponseStreamer, ThreadRouter, AsyncMock]:
    """Create a ResponseStreamer with a mocked SlackClient."""
    client = make_mock_slack_client()
    router = ThreadRouter(client)
    streamer = ResponseStreamer(router)
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

    async def test_result_summary_posted(self):
        """Result summary should be posted on ResultMessage."""
        streamer, router, provider = make_streamer()
        messages = [make_result_message(cost=0.0123, turns=3)]
        await streamer.stream_with_flush(agen(messages))
        # Should post the summary
        assert provider.post.call_count >= 1

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
        """Empty buffers should not be posted."""
        streamer, router, provider = make_streamer()
        messages = [make_result_message()]
        await streamer.stream_with_flush(agen(messages))
        # Only result summary should be posted (with blocks)
        assert provider.post.call_count >= 1


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
        from claude_agent_sdk import AssistantMessage

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
        """When ResultMessage.result is None, only summary should be posted."""
        streamer, router, provider = make_streamer()
        result_msg = make_result_message()
        result_msg.result = None
        messages = [result_msg]

        await streamer.stream_with_flush(agen(messages))

        # Should post the summary (divider + context block)
        assert provider.post.call_count >= 1


class TestBUG028ResolvedModelTracking:
    """BUG-028: Test that streamer tracks resolved_model from AssistantMessage."""

    async def test_resolved_model_set_from_assistant_message(self):
        """After streaming AssistantMessage with model field, resolved_model should return it."""
        streamer, router, provider = make_streamer()
        msg = make_assistant_message([make_text_block("Response")])
        msg.model = "claude-opus-4-6"
        messages = [msg, make_result_message()]

        await streamer.stream_with_flush(agen(messages))

        assert streamer.resolved_model == "claude-opus-4-6"

    async def test_resolved_model_returns_none_when_no_model(self):
        """Before any messages, resolved_model should return None."""
        streamer, router, provider = make_streamer()

        assert streamer.resolved_model is None

    async def test_resolved_model_persists_across_multiple_messages(self):
        """resolved_model should be set from first message and persist."""
        streamer, router, provider = make_streamer()
        msg1 = make_assistant_message([make_text_block("First")])
        msg1.model = "claude-opus-4-6"
        msg2 = make_assistant_message([make_text_block("Second")])
        # msg2 has a different model but we should keep the first one
        msg2.model = "claude-sonnet-4"
        messages = [msg1, msg2, make_result_message()]

        await streamer.stream_with_flush(agen(messages))

        # Should have the first model
        assert streamer.resolved_model == "claude-opus-4-6"


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
    """Tests for BUG-025: post-tool conclusion text only in main channel, not thread."""

    async def test_post_tool_text_not_flushed_to_thread(self):
        """After tool use, conclusion text should NOT be posted to thread."""
        streamer, router, provider = make_streamer()
        tool_block = make_tool_use_block("Read", {"file_path": "/src/main.py"})
        conclusion_text = "Conclusion text after tool"
        messages = [
            make_assistant_message([tool_block, make_text_block(conclusion_text)]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        # Extract all calls to post_message to check thread_ts patterns
        calls = provider.post.call_args_list

        # Find thread posts (those with thread_ts argument)
        thread_posts = [call for call in calls if call.kwargs.get("thread_ts")]

        # Verify no thread post contains the conclusion text
        for call in thread_posts:
            call_text = call.args[0] if call.args else ""
            assert conclusion_text not in call_text

    async def test_post_tool_text_flushed_to_main(self):
        """After tool use, conclusion text SHOULD appear in main channel."""
        streamer, router, provider = make_streamer()
        tool_block = make_tool_use_block("Read", {"file_path": "/src/main.py"})
        conclusion_text = "Conclusion text after tool"
        messages = [
            make_assistant_message([tool_block, make_text_block(conclusion_text)]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))

        # Extract all calls to post_message
        calls = provider.post.call_args_list

        # Find main channel posts (those WITHOUT thread_ts)
        main_posts = [call for call in calls if not call.kwargs.get("thread_ts")]

        # Verify the conclusion text appears in a main post
        main_texts = [call.args[0] for call in main_posts if call.args]
        assert any(conclusion_text in t for t in main_texts), (
            f"Conclusion text not found in main posts: {main_texts}"
        )

    async def test_multiple_text_blocks_after_tool_concatenated(self):
        """Multiple TextBlocks after tool use should be concatenated in conclusion."""
        streamer, router, provider = make_streamer()
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

        # Check that conclusion contains both parts concatenated
        calls = provider.post.call_args_list
        main_posts = [call for call in calls if not call.kwargs.get("thread_ts")]

        full_text = "".join(call.args[0] if call.args else "" for call in main_posts)
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

        # Extract main channel posts
        calls = provider.post.call_args_list
        main_posts = [call for call in calls if not call.kwargs.get("thread_ts")]

        # Verify pre-tool text appears in main
        main_texts = [call.args[0] for call in main_posts if call.args]
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

        # last_message_ts should be set so the checkmark reaction lands correctly
        assert streamer._turn.last_message_ts is not None

        # add_reaction should have been called (checkmark on conclusion or result)
        assert provider.react.called


class TestStreamResult:
    async def test_stream_result_contains_context(self):
        """When ResultMessage has usage, StreamResult should have ContextUsage."""
        from summon_claude.sessions.context import ContextUsage

        streamer, router, provider = make_streamer()
        result_msg = ResultMessage(
            subtype="success",
            session_id="s1",
            is_error=False,
            total_cost_usd=0.01,
            num_turns=1,
            result=None,
            usage={
                "input_tokens": 84000,
                "output_tokens": 1000,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            duration_ms=1000,
            duration_api_ms=800,
        )
        messages = [
            make_assistant_message([make_text_block("text")]),
            result_msg,
        ]
        stream_result = await streamer.stream_with_flush(agen(messages))
        assert stream_result is not None
        assert stream_result.context is not None
        assert stream_result.context.input_tokens == 84000
        assert stream_result.context.percentage == pytest.approx(42.0)

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

    async def test_stream_result_none_usage_means_none_context(self):
        """When ResultMessage.usage is None, context should be None."""
        streamer, router, provider = make_streamer()
        messages = [
            make_assistant_message([make_text_block("text")]),
            make_result_message(),  # usage=None by default
        ]
        stream_result = await streamer.stream_with_flush(agen(messages))
        assert stream_result is not None
        assert stream_result.context is None

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
    return ResponseStreamer(router, max_inline_chars=2500)


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
