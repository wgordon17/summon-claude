"""Tests for summon_claude.slack.mcp — simplified MCP tools using SlackClient."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from summon_claude.slack.client import SlackClient
from summon_claude.slack.mcp import create_summon_mcp_server, create_summon_mcp_tools


def make_mock_client() -> SlackClient:
    """Create a SlackClient with mocked web client."""
    web = MagicMock()
    web.chat_postMessage = AsyncMock(return_value={"channel": "C123", "ts": "1.0"})
    web.chat_postEphemeral = AsyncMock(return_value={})
    web.chat_update = AsyncMock(return_value={})
    web.reactions_add = AsyncMock(return_value={})
    web.files_upload_v2 = AsyncMock(return_value={})
    web.conversations_setTopic = AsyncMock(return_value={})
    client = SlackClient(web, "C123")
    return client


@pytest.fixture
def mock_client() -> SlackClient:
    return make_mock_client()


@pytest.fixture
def tools(mock_client) -> dict:
    return {t.name: t for t in create_summon_mcp_tools(mock_client)}


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


class TestMCPServerCreation:
    def test_returns_valid_config(self, mock_client):
        config = create_summon_mcp_server(mock_client)
        assert config["name"] == "summon-slack"
        assert config["type"] == "sdk"
        assert config["instance"] is not None
