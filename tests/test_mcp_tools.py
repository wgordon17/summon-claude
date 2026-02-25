"""Tests for summon_claude.mcp_tools — invoke actual tool handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from helpers import make_mock_provider
from summon_claude.mcp_tools import create_summon_mcp_tools
from summon_claude.thread_router import ThreadRouter


@pytest.fixture
def mock_provider():
    return make_mock_provider()


@pytest.fixture
def thread_router(mock_provider):
    router = ThreadRouter(mock_provider, "C123")
    router.upload_to_turn_thread = AsyncMock()
    router.add_reaction = AsyncMock()
    router.post_to_turn_thread = AsyncMock()
    return router


@pytest.fixture
def tools(thread_router):
    return {t.name: t for t in create_summon_mcp_tools(thread_router)}


class TestUploadFile:
    async def test_happy_path(self, tools, thread_router):
        result = await tools["slack_upload_file"].handler(
            {"content": "hello", "filename": "test.txt", "title": "Test"}
        )
        assert "Uploaded" in result["content"][0]["text"]
        thread_router.upload_to_turn_thread.assert_called_once()

    async def test_size_limit_returns_error(self, tools):
        result = await tools["slack_upload_file"].handler(
            {"content": "x" * (11 * 1024 * 1024), "filename": "big.txt", "title": "Big"}
        )
        assert result["is_error"] is True
        assert "10 MB" in result["content"][0]["text"]

    async def test_slack_api_error(self, tools, thread_router):
        thread_router.upload_to_turn_thread.side_effect = Exception("API error")
        result = await tools["slack_upload_file"].handler(
            {"content": "data", "filename": "f.txt", "title": "T"}
        )
        assert result["is_error"] is True


class TestCreateThread:
    async def test_happy_path(self, tools, mock_provider):
        result = await tools["slack_create_thread"].handler(
            {"parent_ts": "1234567890.123456", "text": "reply"}
        )
        assert "Thread reply posted" in result["content"][0]["text"]
        mock_provider.post_message.assert_called_once()

    async def test_invalid_parent_ts(self, tools):
        result = await tools["slack_create_thread"].handler(
            {"parent_ts": "invalid", "text": "reply"}
        )
        assert result["is_error"] is True
        assert "parent_ts" in result["content"][0]["text"]

    async def test_slack_api_error(self, tools, mock_provider):
        mock_provider.post_message.side_effect = Exception("API error")
        result = await tools["slack_create_thread"].handler(
            {"parent_ts": "1234567890.123456", "text": "reply"}
        )
        assert result["is_error"] is True


class TestReact:
    async def test_happy_path(self, tools, thread_router):
        result = await tools["slack_react"].handler(
            {"timestamp": "1234567890.123456", "emoji": "thumbsup"}
        )
        assert "thumbsup" in result["content"][0]["text"]
        thread_router.add_reaction.assert_called_once()

    async def test_strips_colons(self, tools, thread_router):
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

    async def test_slack_api_error(self, tools, thread_router):
        thread_router.add_reaction.side_effect = Exception("API error")
        result = await tools["slack_react"].handler(
            {"timestamp": "1234567890.123456", "emoji": "thumbsup"}
        )
        assert result["is_error"] is True


class TestPostSnippet:
    async def test_happy_path(self, tools, thread_router):
        result = await tools["slack_post_snippet"].handler(
            {"code": "print('hi')", "language": "python", "title": "Example"}
        )
        assert "Code snippet posted" in result["content"][0]["text"]
        thread_router.post_to_turn_thread.assert_called_once()

    async def test_slack_api_error(self, tools, thread_router):
        thread_router.post_to_turn_thread.side_effect = Exception("API error")
        result = await tools["slack_post_snippet"].handler(
            {"code": "x", "language": "py", "title": "T"}
        )
        assert result["is_error"] is True


class TestPostSnippetSanitization:
    async def test_title_with_mrkdwn_chars_sanitized(self, tools, thread_router):
        result = await tools["slack_post_snippet"].handler(
            {"code": "x = 1", "language": "python", "title": "*bold*\ninjected"}
        )
        assert "Code snippet posted" in result["content"][0]["text"]
        call_args = thread_router.post_to_turn_thread.call_args
        blocks = call_args.kwargs.get("blocks") or call_args[1].get("blocks")
        mrkdwn_text = blocks[0]["text"]["text"]
        assert "\n*bold*" not in mrkdwn_text
        assert "injected" in mrkdwn_text  # text preserved, just formatting stripped

    async def test_lang_with_backticks_sanitized(self, tools, thread_router):
        result = await tools["slack_post_snippet"].handler(
            {"code": "x = 1", "language": "python```\nfake", "title": "Test"}
        )
        assert "Code snippet posted" in result["content"][0]["text"]
        call_args = thread_router.post_to_turn_thread.call_args
        blocks = call_args.kwargs.get("blocks") or call_args[1].get("blocks")
        mrkdwn_text = blocks[0]["text"]["text"]
        assert "```\nfake" not in mrkdwn_text


class TestMCPServerCreation:
    def test_returns_valid_config(self, thread_router):
        from summon_claude.mcp_tools import create_summon_mcp_server

        config = create_summon_mcp_server(thread_router)
        assert config["name"] == "summon-slack"
        assert config["type"] == "sdk"
        assert config["instance"] is not None
