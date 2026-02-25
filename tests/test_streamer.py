"""Tests for summon_claude.streamer — now uses ThreadRouter."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from helpers import make_mock_provider
from summon_claude.config import SummonConfig
from summon_claude.content_display import _split_text
from summon_claude.streamer import ResponseStreamer, _format_tool_summary
from summon_claude.thread_router import ThreadRouter


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
    """Create a ResponseStreamer with a mocked provider."""
    provider = make_mock_provider()
    router = ThreadRouter(provider, "C123")
    streamer = ResponseStreamer(router)
    return streamer, router, provider


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
        provider.post_message.assert_called()

    async def test_returns_result_message(self):
        streamer, router, provider = make_streamer()
        result_msg = make_result_message(cost=0.05)
        messages = [
            make_assistant_message([make_text_block("text")]),
            result_msg,
        ]
        result = await streamer.stream_with_flush(agen(messages))
        assert result is result_msg

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
        assert provider.post_message.call_count >= 1

    async def test_result_summary_posted(self):
        """Result summary should be posted on ResultMessage."""
        streamer, router, provider = make_streamer()
        messages = [make_result_message(cost=0.0123, turns=3)]
        await streamer.stream_with_flush(agen(messages))
        # Should post the summary
        assert provider.post_message.call_count >= 1

    async def test_long_text_triggers_new_message(self):
        """Long text should trigger multiple messages."""
        streamer, router, provider = make_streamer()
        long_text = "x" * 3000
        messages = [
            make_assistant_message([make_text_block(long_text)]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))
        assert provider.post_message.call_count >= 1

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
        assert provider.post_message.call_count >= 2

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
        assert provider.post_message.call_count >= 2

    async def test_empty_buffer_not_posted(self):
        """Empty buffers should not be posted."""
        streamer, router, provider = make_streamer()
        messages = [make_result_message()]
        await streamer.stream_with_flush(agen(messages))
        # Only result summary should be posted (with blocks)
        assert provider.post_message.call_count >= 1


class TestResponseStreamerStreamWithFlush:
    async def test_stream_with_flush_returns_result(self):
        streamer, router, provider = make_streamer()
        result_msg = make_result_message()
        messages = [
            make_assistant_message([make_text_block("hello")]),
            result_msg,
        ]
        result = await streamer.stream_with_flush(agen(messages))
        assert result is result_msg

    async def test_stream_with_flush_posts_messages(self):
        streamer, router, provider = make_streamer()
        messages = [
            make_assistant_message([make_text_block("response text")]),
            make_result_message(),
        ]
        await streamer.stream_with_flush(agen(messages))
        assert provider.post_message.call_count >= 1


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
        assert "tu_1" in router._subagent_threads

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
        provider.post_message.assert_called()


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
        calls = provider.post_message.call_args_list

        # Find thread posts (those with thread_ts argument)
        thread_posts = [call for call in calls if call.kwargs.get("thread_ts")]

        # Verify no thread post contains the conclusion text
        for call in thread_posts:
            call_text = call.args[1] if len(call.args) > 1 else ""
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
        calls = provider.post_message.call_args_list

        # Find main channel posts (those WITHOUT thread_ts)
        main_posts = [call for call in calls if not call.kwargs.get("thread_ts")]

        # Verify the conclusion text appears in a main post
        main_texts = [call.args[1] for call in main_posts if len(call.args) > 1]
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
        calls = provider.post_message.call_args_list
        main_posts = [call for call in calls if not call.kwargs.get("thread_ts")]

        full_text = "".join(call.args[1] if len(call.args) > 1 else "" for call in main_posts)
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
        calls = provider.post_message.call_args_list
        main_posts = [call for call in calls if not call.kwargs.get("thread_ts")]

        # Verify pre-tool text appears in main
        main_texts = [call.args[1] for call in main_posts if len(call.args) > 1]
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
        assert provider.add_reaction.called
