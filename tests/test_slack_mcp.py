"""Tests for summon_claude.slack.mcp — simplified MCP tools using SlackClient."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from summon_claude.slack.client import HistoryResult, SlackClient
from summon_claude.slack.mcp import create_summon_mcp_server, create_summon_mcp_tools


def _channels(*ids: str) -> Callable[[], Awaitable[set[str]]]:
    """Create an async channel resolver for testing."""

    async def _resolver() -> set[str]:
        return set(ids)

    return _resolver


def make_mock_client() -> SlackClient:
    """Create a SlackClient with mocked web client."""
    web = MagicMock()
    web.chat_postMessage = AsyncMock(return_value={"channel": "C123", "ts": "1.0"})
    web.chat_postEphemeral = AsyncMock(return_value={})
    web.chat_update = AsyncMock(return_value={})
    web.reactions_add = AsyncMock(return_value={})
    web.files_upload_v2 = AsyncMock(return_value={})
    web.conversations_setTopic = AsyncMock(return_value={})
    web.conversations_history = AsyncMock(
        return_value={
            "messages": [
                {"ts": "3.0", "user": "U123", "text": "hello", "reply_count": 0},
                {"ts": "2.0", "user": "U456", "text": "world", "reply_count": 2},
                {"ts": "1.0", "user": "U123", "text": "first"},
            ],
            "has_more": False,
        }
    )
    web.conversations_replies = AsyncMock(
        return_value={
            "messages": [
                {"ts": "2.0", "user": "U456", "text": "world"},
                {"ts": "2.1", "user": "U123", "text": "reply 1"},
                {"ts": "2.2", "user": "U789", "text": "reply 2"},
            ],
            "has_more": False,
        }
    )
    client = SlackClient(web, "C123")
    return client


@pytest.fixture
def mock_client() -> SlackClient:
    return make_mock_client()


@pytest.fixture
def tools(mock_client) -> dict:
    return {t.name: t for t in create_summon_mcp_tools(mock_client, _channels("C123"))}


@pytest.fixture
def reading_tools(mock_client) -> dict:
    return {t.name: t for t in create_summon_mcp_tools(mock_client, _channels("C123"))}


class TestUploadFile:
    async def test_happy_path(self, tools, mock_client):
        result = await tools["slack_upload_file"].handler(
            {"content": "hello", "filename": "test.txt", "title": "Test"}
        )
        assert "Uploaded" in result["content"][0]["text"]
        mock_client._web.files_upload_v2.assert_called_once()

    async def test_size_limit_returns_error(self, tools):
        result = await tools["slack_upload_file"].handler(
            {"content": "x" * (11 * 1024 * 1024), "filename": "big.txt", "title": "Big"}
        )
        assert result["is_error"] is True
        assert "10 MB" in result["content"][0]["text"]

    async def test_slack_api_error(self, tools, mock_client):
        mock_client._web.files_upload_v2.side_effect = Exception("API error")
        result = await tools["slack_upload_file"].handler(
            {"content": "data", "filename": "f.txt", "title": "T"}
        )
        assert result["is_error"] is True

    async def test_posts_to_main_channel(self, tools, mock_client):
        """BEHAVIOR CHANGE: upload posts to main channel, not active thread."""
        await tools["slack_upload_file"].handler(
            {"content": "data", "filename": "f.txt", "title": "T"}
        )
        call_kwargs = mock_client._web.files_upload_v2.call_args.kwargs
        # No thread_ts in the call — main channel
        assert call_kwargs.get("thread_ts") is None


class TestCreateThread:
    async def test_happy_path(self, tools, mock_client):
        result = await tools["slack_create_thread"].handler(
            {"parent_ts": "1234567890.123456", "text": "reply"}
        )
        assert "Thread reply posted" in result["content"][0]["text"]
        mock_client._web.chat_postMessage.assert_called_once()

    async def test_invalid_parent_ts(self, tools):
        result = await tools["slack_create_thread"].handler(
            {"parent_ts": "invalid", "text": "reply"}
        )
        assert result["is_error"] is True
        assert "parent_ts" in result["content"][0]["text"]

    async def test_slack_api_error(self, tools, mock_client):
        mock_client._web.chat_postMessage.side_effect = Exception("API error")
        result = await tools["slack_create_thread"].handler(
            {"parent_ts": "1234567890.123456", "text": "reply"}
        )
        assert result["is_error"] is True

    async def test_uses_thread_ts(self, tools, mock_client):
        await tools["slack_create_thread"].handler({"parent_ts": "9999.001", "text": "reply"})
        call_kwargs = mock_client._web.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "9999.001"


class TestReact:
    async def test_happy_path(self, tools, mock_client):
        result = await tools["slack_react"].handler(
            {"timestamp": "1234567890.123456", "emoji": "thumbsup"}
        )
        assert "thumbsup" in result["content"][0]["text"]
        mock_client._web.reactions_add.assert_called_once()

    async def test_strips_colons(self, tools, mock_client):
        result = await tools["slack_react"].handler(
            {"timestamp": "1234567890.123456", "emoji": ":thumbsup:"}
        )
        assert "thumbsup" in result["content"][0]["text"]

    async def test_invalid_emoji(self, tools):
        result = await tools["slack_react"].handler(
            {"timestamp": "1234567890.123456", "emoji": "bad-emoji!"}
        )
        assert result["is_error"] is True

    async def test_invalid_timestamp(self, tools):
        result = await tools["slack_react"].handler({"timestamp": "invalid", "emoji": "thumbsup"})
        assert result["is_error"] is True

    async def test_slack_api_error_swallowed_by_client(self, tools, mock_client):
        """SlackClient.react() swallows API errors — tool still reports success."""
        mock_client._web.reactions_add.side_effect = Exception("API error")
        result = await tools["slack_react"].handler(
            {"timestamp": "1234567890.123456", "emoji": "thumbsup"}
        )
        # react() swallows errors internally; tool returns success
        assert "thumbsup" in result["content"][0]["text"]


class TestPostSnippet:
    async def test_happy_path(self, tools, mock_client):
        result = await tools["slack_post_snippet"].handler(
            {"code": "print('hi')", "language": "python", "title": "Example"}
        )
        assert "Code snippet posted" in result["content"][0]["text"]
        mock_client._web.chat_postMessage.assert_called_once()

    async def test_slack_api_error(self, tools, mock_client):
        mock_client._web.chat_postMessage.side_effect = Exception("API error")
        result = await tools["slack_post_snippet"].handler(
            {"code": "x", "language": "py", "title": "T"}
        )
        assert result["is_error"] is True

    async def test_posts_to_main_channel(self, tools, mock_client):
        """BEHAVIOR CHANGE: snippet posts to main channel, not active thread."""
        await tools["slack_post_snippet"].handler(
            {"code": "x = 1", "language": "python", "title": "Test"}
        )
        call_kwargs = mock_client._web.chat_postMessage.call_args.kwargs
        # No thread_ts — main channel
        assert call_kwargs.get("thread_ts") is None


class TestPostSnippetSanitization:
    async def test_title_with_mrkdwn_chars_sanitized(self, tools, mock_client):
        result = await tools["slack_post_snippet"].handler(
            {"code": "x = 1", "language": "python", "title": "*bold*\ninjected"}
        )
        assert "Code snippet posted" in result["content"][0]["text"]
        call_kwargs = mock_client._web.chat_postMessage.call_args.kwargs
        blocks = call_kwargs.get("blocks")
        mrkdwn_text = blocks[0]["text"]["text"]
        assert "\n*bold*" not in mrkdwn_text
        assert "injected" in mrkdwn_text

    async def test_lang_with_backticks_sanitized(self, tools, mock_client):
        result = await tools["slack_post_snippet"].handler(
            {"code": "x = 1", "language": "python```\nfake", "title": "Test"}
        )
        assert "Code snippet posted" in result["content"][0]["text"]
        call_kwargs = mock_client._web.chat_postMessage.call_args.kwargs
        blocks = call_kwargs.get("blocks")
        mrkdwn_text = blocks[0]["text"]["text"]
        assert "```\nfake" not in mrkdwn_text


class TestUpdateMessage:
    async def test_happy_path(self, tools, mock_client):
        result = await tools["slack_update_message"].handler(
            {"ts": "1234567890.123456", "text": "updated text"}
        )
        assert "Message updated" in result["content"][0]["text"]
        mock_client._web.chat_update.assert_called_once()

    async def test_invalid_ts(self, tools):
        result = await tools["slack_update_message"].handler(
            {"ts": "invalid", "text": "updated text"}
        )
        assert result["is_error"] is True
        assert "ts" in result["content"][0]["text"].lower()

    async def test_other_channel_denied(self, mock_client):
        scoped_tools = {t.name: t for t in create_summon_mcp_tools(mock_client, _channels("C123"))}
        result = await scoped_tools["slack_update_message"].handler(
            {"ts": "1234567890.123456", "text": "updated", "channel": "C_FORBIDDEN"}
        )
        assert result["is_error"] is True
        assert "denied" in result["content"][0]["text"].lower()

    async def test_slack_api_error(self, tools, mock_client):
        mock_client._web.chat_update.side_effect = Exception("API error")
        result = await tools["slack_update_message"].handler(
            {"ts": "1234567890.123456", "text": "updated"}
        )
        assert result["is_error"] is True


class TestMCPServerCreation:
    def test_returns_valid_config(self, mock_client):
        config = create_summon_mcp_server(mock_client, _channels("C123"))
        assert config["name"] == "summon-slack"
        assert config["type"] == "sdk"
        assert config["instance"] is not None


class TestFetchHistory:
    async def test_default_channel(self, mock_client):
        result = await mock_client.fetch_history()
        assert isinstance(result, HistoryResult)
        assert len(result.messages) == 3
        mock_client._web.conversations_history.assert_called_once()
        call_kwargs = mock_client._web.conversations_history.call_args.kwargs
        assert call_kwargs["channel"] == "C123"

    async def test_custom_channel(self, mock_client):
        await mock_client.fetch_history(channel="C999")
        call_kwargs = mock_client._web.conversations_history.call_args.kwargs
        assert call_kwargs["channel"] == "C999"

    async def test_with_oldest(self, mock_client):
        await mock_client.fetch_history(oldest="1234567890.000000")
        call_kwargs = mock_client._web.conversations_history.call_args.kwargs
        assert call_kwargs["oldest"] == "1234567890.000000"

    async def test_oldest_omitted_when_none(self, mock_client):
        await mock_client.fetch_history()
        call_kwargs = mock_client._web.conversations_history.call_args.kwargs
        assert "oldest" not in call_kwargs

    async def test_has_more_true(self, mock_client):
        mock_client._web.conversations_history = AsyncMock(
            return_value={"messages": [{"ts": "1.0"}], "has_more": True}
        )
        result = await mock_client.fetch_history()
        assert result.has_more is True

    async def test_has_more_false_when_absent(self, mock_client):
        mock_client._web.conversations_history = AsyncMock(
            return_value={"messages": [{"ts": "1.0"}]}
        )
        result = await mock_client.fetch_history()
        assert result.has_more is False


class TestFetchThreadReplies:
    async def test_default_channel(self, mock_client):
        result = await mock_client.fetch_thread_replies("2.0")
        assert isinstance(result, HistoryResult)
        assert len(result.messages) == 3
        call_kwargs = mock_client._web.conversations_replies.call_args.kwargs
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["ts"] == "2.0"

    async def test_custom_channel(self, mock_client):
        await mock_client.fetch_thread_replies("2.0", channel="C999")
        call_kwargs = mock_client._web.conversations_replies.call_args.kwargs
        assert call_kwargs["channel"] == "C999"


class TestFetchContext:
    async def test_merges_and_deduplicates(self, mock_client):
        mock_client._web.conversations_history = AsyncMock(
            side_effect=[
                {
                    "messages": [
                        {"ts": "2.0", "user": "U1", "text": "target"},
                        {"ts": "1.0", "user": "U2", "text": "before"},
                    ]
                },
                {"messages": [{"ts": "3.0", "user": "U3", "text": "after"}]},
            ]
        )
        result = await mock_client.fetch_context("2.0")
        assert result["target_ts"] == "2.0"
        tss = [m["ts"] for m in result["messages"]]
        assert tss == ["1.0", "2.0", "3.0"]

    async def test_includes_thread_when_replies(self, mock_client):
        mock_client._web.conversations_history = AsyncMock(
            side_effect=[
                {"messages": [{"ts": "2.0", "reply_count": 3}]},
                {"messages": []},
            ]
        )
        mock_client._web.conversations_replies = AsyncMock(
            return_value={
                "messages": [{"ts": "2.0"}, {"ts": "2.1"}, {"ts": "2.2"}],
                "has_more": False,
            }
        )
        result = await mock_client.fetch_context("2.0")
        assert result["thread"] is not None
        assert len(result["thread"]) == 3
        # Verify fetch_context requests up to 200 thread replies
        call_kwargs = mock_client._web.conversations_replies.call_args.kwargs
        assert call_kwargs["limit"] == 200

    async def test_no_thread_when_no_replies(self, mock_client):
        mock_client._web.conversations_history = AsyncMock(
            side_effect=[
                {"messages": [{"ts": "2.0", "reply_count": 0}]},
                {"messages": []},
            ]
        )
        result = await mock_client.fetch_context("2.0")
        assert result["thread"] is None


class TestReadHistoryTool:
    async def test_happy_path_summary(self, reading_tools, mock_client):
        result = await reading_tools["slack_read_history"].handler({"limit": 10})
        assert not result.get("is_error")
        text = result["content"][0]["text"]
        assert "[3.0]" in text

    async def test_happy_path_raw(self, reading_tools, mock_client):
        result = await reading_tools["slack_read_history"].handler({"format": "raw"})
        import json

        parsed = json.loads(result["content"][0]["text"])
        assert isinstance(parsed, list)

    async def test_limit_clamped_high(self, reading_tools, mock_client):
        await reading_tools["slack_read_history"].handler({"limit": 999})
        call_kwargs = mock_client._web.conversations_history.call_args.kwargs
        assert call_kwargs["limit"] == 200

    async def test_channel_enforcement_rejects(self, reading_tools):
        result = await reading_tools["slack_read_history"].handler({"channel": "C_FORBIDDEN"})
        assert result["is_error"] is True
        assert "denied" in result["content"][0]["text"].lower()
        assert "C_FORBIDDEN" not in result["content"][0]["text"]

    async def test_default_channel(self, reading_tools, mock_client):
        await reading_tools["slack_read_history"].handler({})
        call_kwargs = mock_client._web.conversations_history.call_args.kwargs
        assert call_kwargs["channel"] == "C123"

    async def test_invalid_format_rejected(self, reading_tools):
        result = await reading_tools["slack_read_history"].handler({"format": "detailed"})
        assert result["is_error"] is True
        assert "invalid format" in result["content"][0]["text"].lower()


class TestFetchThreadTool:
    async def test_happy_path(self, reading_tools, mock_client):
        result = await reading_tools["slack_fetch_thread"].handler({"parent_ts": "2.0"})
        assert not result.get("is_error")

    async def test_invalid_parent_ts(self, reading_tools):
        result = await reading_tools["slack_fetch_thread"].handler({"parent_ts": "invalid"})
        assert result["is_error"] is True

    async def test_channel_enforcement(self, reading_tools):
        result = await reading_tools["slack_fetch_thread"].handler(
            {"parent_ts": "2.0", "channel": "C_FORBIDDEN"}
        )
        assert result["is_error"] is True

    async def test_invalid_format_rejected(self, reading_tools):
        result = await reading_tools["slack_fetch_thread"].handler(
            {"parent_ts": "2.0", "format": "verbose"}
        )
        assert result["is_error"] is True
        assert "invalid format" in result["content"][0]["text"].lower()


class TestGetContextTool:
    async def test_with_url(self, reading_tools, mock_client):
        mock_client._web.conversations_history = AsyncMock(
            side_effect=[
                {"messages": [{"ts": "1234567890.123456", "user": "U1", "text": "hi"}]},
                {"messages": []},
            ]
        )
        result = await reading_tools["slack_get_context"].handler(
            {"url": "https://test.slack.com/archives/C123/p1234567890123456"}
        )
        assert not result.get("is_error")

    async def test_threaded_url(self, reading_tools, mock_client):
        result = await reading_tools["slack_get_context"].handler(
            {
                "url": "https://test.slack.com/archives/C123/p1234567890123456"
                "?thread_ts=1234567890.000000&cid=C123"
            }
        )
        assert not result.get("is_error")
        mock_client._web.conversations_replies.assert_called()

    async def test_with_channel_and_ts(self, reading_tools, mock_client):
        mock_client._web.conversations_history = AsyncMock(
            side_effect=[
                {"messages": [{"ts": "2.0", "user": "U1", "text": "target"}]},
                {"messages": []},
            ]
        )
        result = await reading_tools["slack_get_context"].handler(
            {"channel": "C123", "message_ts": "2.0"}
        )
        assert not result.get("is_error")

    async def test_invalid_url(self, reading_tools):
        result = await reading_tools["slack_get_context"].handler(
            {"url": "https://not-slack.com/foo"}
        )
        assert result["is_error"] is True

    async def test_channel_enforcement_on_url(self, reading_tools):
        result = await reading_tools["slack_get_context"].handler(
            {"url": "https://test.slack.com/archives/C_FORBIDDEN/p1234567890123456"}
        )
        assert result["is_error"] is True

    async def test_invalid_message_ts_manual(self, reading_tools):
        result = await reading_tools["slack_get_context"].handler(
            {"channel": "C123", "message_ts": "not-a-timestamp"}
        )
        assert result["is_error"] is True
        assert "message_ts" in result["content"][0]["text"].lower()

    async def test_invalid_format_rejected(self, reading_tools):
        result = await reading_tools["slack_get_context"].handler(
            {"channel": "C123", "message_ts": "2.0", "format": "xml"}
        )
        assert result["is_error"] is True
        assert "invalid format" in result["content"][0]["text"].lower()

    async def test_invalid_thread_ts_in_url(self, reading_tools):
        result = await reading_tools["slack_get_context"].handler(
            {"url": "https://test.slack.com/archives/C123/p1234567890123456?thread_ts=bad&cid=C123"}
        )
        assert result["is_error"] is True
        assert "thread_ts" in result["content"][0]["text"].lower()


class TestChannelEnforcement:
    async def test_custom_allowed_channels(self, mock_client):
        custom_tools = {
            t.name: t for t in create_summon_mcp_tools(mock_client, _channels("C123", "C456"))
        }
        result = await custom_tools["slack_read_history"].handler({"channel": "C456"})
        assert not result.get("is_error")

    async def test_default_is_session_channel(self, mock_client):
        default_tools = {t.name: t for t in create_summon_mcp_tools(mock_client, _channels("C123"))}
        result = await default_tools["slack_read_history"].handler({"channel": "C123"})
        assert not result.get("is_error")

    async def test_denied_does_not_leak_id(self, mock_client):
        tools = {t.name: t for t in create_summon_mcp_tools(mock_client, _channels("C123"))}
        result = await tools["slack_read_history"].handler({"channel": "C_SECRET"})
        assert result["is_error"] is True
        assert "C_SECRET" not in result["content"][0]["text"]


class TestMessageFormatting:
    def test_summary_format(self):
        from summon_claude.slack.mcp import _format_messages

        msgs = [{"ts": "1.0", "user": "U1", "text": "hello"}]
        result = _format_messages(msgs, "summary")
        assert "[1.0]" in result[0]["text"]
        assert "<U1>" in result[0]["text"]

    def test_raw_format(self):
        import json

        from summon_claude.slack.mcp import _format_messages

        msgs = [{"ts": "1.0", "text": "hello"}]
        result = _format_messages(msgs, "raw")
        parsed = json.loads(result[0]["text"])
        assert isinstance(parsed, list)

    def test_empty_messages(self):
        from summon_claude.slack.mcp import _format_messages

        result = _format_messages([], "summary")
        assert "No messages found" in result[0]["text"]

    def test_summary_includes_truncation_indicator(self):
        from summon_claude.slack.mcp import _format_message_summary

        msg = {"ts": "1.0", "user": "U1", "text": "x" * 600}
        result = _format_message_summary(msg)
        assert result.endswith("…")

        short_msg = {"ts": "1.0", "user": "U1", "text": "hello"}
        short_result = _format_message_summary(short_msg)
        assert not short_result.endswith("…")

    def test_noise_filtered_in_summary(self):
        from summon_claude.slack.mcp import _format_messages

        msgs = [
            {"ts": "1.0", "user": "U1", "text": "real message"},
            {"ts": "2.0", "subtype": "channel_join", "text": "joined"},
        ]
        result = _format_messages(msgs, "summary")
        assert "joined" not in result[0]["text"]
        assert "real message" in result[0]["text"]

    def test_noise_preserved_in_raw(self):
        from summon_claude.slack.mcp import _format_messages

        msgs = [{"ts": "1.0", "subtype": "channel_join", "text": "joined"}]
        result = _format_messages(msgs, "raw")
        assert "channel_join" in result[0]["text"]

    def test_has_more_pagination_note(self):
        from summon_claude.slack.mcp import _format_messages

        msgs = [{"ts": "1.0", "user": "U1", "text": "hi"}]
        result = _format_messages(msgs, "summary", has_more=True)
        assert "more messages available" in result[0]["text"]

    def test_no_pagination_note_when_false(self):
        from summon_claude.slack.mcp import _format_messages

        msgs = [{"ts": "1.0", "user": "U1", "text": "hi"}]
        result = _format_messages(msgs, "summary", has_more=False)
        assert "more messages available" not in result[0]["text"]

    def test_raw_truncation_produces_valid_json(self):
        import json

        from summon_claude.slack.mcp import _RAW_MAX_BYTES, _format_messages

        msgs = [
            {"ts": "1.0", "text": "x" * (_RAW_MAX_BYTES + 1000)},
            {"ts": "2.0", "text": "should not appear"},
        ]
        result = _format_messages(msgs, "raw")
        text = result[0]["text"]
        # Should have a count note since not all messages fit
        assert "messages total" in text
        # The JSON portion (before the count note) should be valid
        json_part = text.split("\n(")[0]
        parsed = json.loads(json_part)
        assert isinstance(parsed, list)


class TestAISummarization:
    """Tests for format='ai' which spawns a Haiku SDK session."""

    async def test_read_history_ai_format(self, reading_tools, mock_client):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "summon_claude.slack.mcp._ai_summarize",
                AsyncMock(return_value="AI summary of conversation"),
            )
            result = await reading_tools["slack_read_history"].handler({"format": "ai"})
        assert not result.get("is_error")
        assert "AI summary of conversation" in result["content"][0]["text"]

    async def test_fetch_thread_ai_format(self, reading_tools, mock_client):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "summon_claude.slack.mcp._ai_summarize",
                AsyncMock(return_value="Thread summary"),
            )
            result = await reading_tools["slack_fetch_thread"].handler(
                {"parent_ts": "2.0", "format": "ai"}
            )
        assert not result.get("is_error")
        assert "Thread summary" in result["content"][0]["text"]

    async def test_get_context_ai_format_with_url(self, reading_tools, mock_client):
        mock_client._web.conversations_history = AsyncMock(
            side_effect=[
                {"messages": [{"ts": "1234567890.123456", "user": "U1", "text": "hi"}]},
                {"messages": []},
            ]
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "summon_claude.slack.mcp._ai_summarize",
                AsyncMock(return_value="Context summary"),
            )
            result = await reading_tools["slack_get_context"].handler(
                {
                    "url": "https://test.slack.com/archives/C123/p1234567890123456",
                    "format": "ai",
                }
            )
        assert not result.get("is_error")
        assert "Context summary" in result["content"][0]["text"]

    async def test_get_context_ai_format_threaded_url(self, reading_tools, mock_client):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "summon_claude.slack.mcp._ai_summarize",
                AsyncMock(return_value="Threaded context summary"),
            )
            result = await reading_tools["slack_get_context"].handler(
                {
                    "url": "https://test.slack.com/archives/C123/p1234567890123456"
                    "?thread_ts=1234567890.000000&cid=C123",
                    "format": "ai",
                }
            )
        assert not result.get("is_error")
        assert "Threaded context summary" in result["content"][0]["text"]

    async def test_ai_fallback_on_error(self, reading_tools, mock_client):
        """When AI summarization fails, falls back to summary format."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "summon_claude.slack.mcp._ai_summarize",
                AsyncMock(side_effect=RuntimeError("SDK unavailable")),
            )
            result = await reading_tools["slack_read_history"].handler({"format": "ai"})
        assert not result.get("is_error")
        # Falls back to summary format — should have timestamp markers
        assert "[3.0]" in result["content"][0]["text"]

    async def test_ai_has_more_pagination_note(self, reading_tools, mock_client):
        mock_client._web.conversations_history = AsyncMock(
            return_value={"messages": [{"ts": "1.0", "user": "U1", "text": "hi"}], "has_more": True}
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "summon_claude.slack.mcp._ai_summarize",
                AsyncMock(return_value="Summary"),
            )
            result = await reading_tools["slack_read_history"].handler({"format": "ai"})
        assert "more messages available" in result["content"][0]["text"]

    async def test_ai_passes_cwd_to_summarize(self, mock_client):
        """Verify cwd is threaded from create_summon_mcp_tools to _ai_summarize."""
        tools_with_cwd = {
            t.name: t
            for t in create_summon_mcp_tools(mock_client, _channels("C123"), cwd="/project/dir")
        }
        with pytest.MonkeyPatch.context() as mp:
            mock_summarize = AsyncMock(return_value="summary")
            mp.setattr("summon_claude.slack.mcp._ai_summarize", mock_summarize)
            await tools_with_cwd["slack_read_history"].handler({"format": "ai"})
        mock_summarize.assert_called_once()
        assert mock_summarize.call_args.kwargs["cwd"] == "/project/dir"


class TestAISummarizeFunction:
    """Tests for the _ai_summarize function itself."""

    async def test_extracts_text_from_sdk_response(self):
        """Successful SDK session returns extracted text."""
        from claude_agent_sdk import AssistantMessage, TextBlock

        from summon_claude.slack.mcp import _ai_summarize

        mock_block = MagicMock(spec=TextBlock)
        mock_block.text = "Summarized conversation"
        mock_msg = MagicMock(spec=AssistantMessage)
        mock_msg.content = [mock_block]

        async def mock_response():
            yield mock_msg

        mock_haiku = AsyncMock()
        mock_haiku.receive_response = mock_response

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_haiku)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "summon_claude.slack.mcp.ClaudeSDKClient",
                MagicMock(return_value=mock_ctx),
            )
            result = await _ai_summarize([{"ts": "1.0", "text": "hello"}])
        assert result == "Summarized conversation"

    async def test_restores_claudecode_env(self):
        """CLAUDECODE env var is restored after summarization."""
        import os

        from summon_claude.slack.mcp import _ai_summarize

        async def empty_response():
            return
            yield

        mock_haiku = AsyncMock()
        mock_haiku.receive_response = empty_response

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_haiku)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        os.environ["CLAUDECODE"] = "test_value"
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "summon_claude.slack.mcp.ClaudeSDKClient",
                    MagicMock(return_value=mock_ctx),
                )
                await _ai_summarize([{"ts": "1.0", "text": "hello"}])
            assert os.environ.get("CLAUDECODE") == "test_value"
        finally:
            os.environ.pop("CLAUDECODE", None)

    async def test_clears_claudecode_for_subprocess(self):
        """CLAUDECODE is removed from env before ClaudeSDKClient is created."""
        import os

        from claude_agent_sdk import ClaudeSDKClient

        from summon_claude.slack.mcp import _ai_summarize

        os.environ["CLAUDECODE"] = "original"
        captured_env: dict[str, str | None] = {}

        def spy_init(self, *args, **kwargs):
            captured_env["CLAUDECODE"] = os.environ.get("CLAUDECODE")
            raise RuntimeError("stop before subprocess")

        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(ClaudeSDKClient, "__init__", spy_init)
                with pytest.raises(RuntimeError, match="stop before subprocess"):
                    await _ai_summarize([{"ts": "1.0", "text": "hello"}])

            # Env var was cleared before SDK client was created
            assert captured_env["CLAUDECODE"] is None
            # And restored after
            assert os.environ.get("CLAUDECODE") == "original"
        finally:
            os.environ.pop("CLAUDECODE", None)


class TestToolInventory:
    def test_all_expected_tools_present(self, mock_client):
        tools = create_summon_mcp_tools(mock_client, _channels("C123"))
        names = {t.name for t in tools}
        assert "slack_upload_file" in names
        assert "slack_create_thread" in names

    def test_total_tool_count(self, mock_client):
        tools = create_summon_mcp_tools(mock_client, _channels("C123"))
        assert len(tools) == 8
