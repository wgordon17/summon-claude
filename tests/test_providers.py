"""Tests for provider abstraction and SlackChatProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from summon_claude.providers.base import ChannelRef, ChatProvider, MessageRef
from summon_claude.providers.slack import SlackChatProvider


def make_mock_slack_client():
    """Create a mocked AsyncWebClient."""
    client = AsyncMock()
    return client


def make_slack_provider(client=None):
    """Create a SlackChatProvider with a mocked client."""
    if client is None:
        client = make_mock_slack_client()
    return SlackChatProvider(client)


class TestDataclasses:
    """Smoke tests for frozen dataclasses."""

    def test_message_ref_is_frozen(self):
        ref = MessageRef(channel_id="C123", ts="1234567890.123456")
        with pytest.raises(AttributeError):
            ref.channel_id = "C456"

    def test_channel_ref_is_frozen(self):
        ref = ChannelRef(channel_id="C_NEW", name="test-channel")
        with pytest.raises(AttributeError):
            ref.channel_id = "C_OTHER"


class TestChatProviderProtocol:
    """ChatProvider protocol tests."""

    def test_slack_provider_satisfies_protocol(self):
        """SlackChatProvider should implement ChatProvider protocol."""
        provider = make_slack_provider()
        assert isinstance(provider, ChatProvider)


class TestSlackChatProviderPostMessage:
    """SlackChatProvider.post_message tests."""

    async def test_post_message_basic(self):
        """post_message should delegate to client.chat_postMessage."""
        client = make_mock_slack_client()
        client.chat_postMessage = AsyncMock(
            return_value={"channel": "C123", "ts": "1234567890.123456"}
        )
        provider = SlackChatProvider(client)

        ref = await provider.post_message("C123", "Hello world")

        assert ref.channel_id == "C123"
        assert ref.ts == "1234567890.123456"
        client.chat_postMessage.assert_called_once()
        call_kwargs = client.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["text"] == "Hello world"

    async def test_post_message_with_blocks(self):
        """post_message should pass blocks to client."""
        client = make_mock_slack_client()
        client.chat_postMessage = AsyncMock(
            return_value={"channel": "C123", "ts": "1234567890.123456"}
        )
        provider = SlackChatProvider(client)

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "test"}}]
        await provider.post_message("C123", "text", blocks=blocks)

        call_kwargs = client.chat_postMessage.call_args[1]
        assert call_kwargs["blocks"] == blocks

    async def test_post_message_with_thread_ts(self):
        """post_message should pass thread_ts to client."""
        client = make_mock_slack_client()
        client.chat_postMessage = AsyncMock(
            return_value={"channel": "C123", "ts": "1234567890.123456"}
        )
        provider = SlackChatProvider(client)

        await provider.post_message("C123", "reply", thread_ts="1234567890.111111")

        call_kwargs = client.chat_postMessage.call_args[1]
        assert call_kwargs["thread_ts"] == "1234567890.111111"

    async def test_post_message_with_reply_broadcast(self):
        """post_message should pass reply_broadcast to client."""
        client = make_mock_slack_client()
        client.chat_postMessage = AsyncMock(
            return_value={"channel": "C123", "ts": "1234567890.123456"}
        )
        provider = SlackChatProvider(client)

        await provider.post_message(
            "C123", "reply", thread_ts="1234567890.111111", reply_broadcast=True
        )

        call_kwargs = client.chat_postMessage.call_args[1]
        assert call_kwargs.get("reply_broadcast") is True

    async def test_post_message_without_optional_params(self):
        """post_message should omit optional params when not provided."""
        client = make_mock_slack_client()
        client.chat_postMessage = AsyncMock(
            return_value={"channel": "C123", "ts": "1234567890.123456"}
        )
        provider = SlackChatProvider(client)

        await provider.post_message("C123", "text")

        call_kwargs = client.chat_postMessage.call_args[1]
        assert "blocks" not in call_kwargs
        assert "thread_ts" not in call_kwargs
        assert "reply_broadcast" not in call_kwargs


class TestSlackChatProviderUpdateMessage:
    """SlackChatProvider.update_message tests."""

    async def test_update_message_basic(self):
        """update_message should delegate to client.chat_update."""
        client = make_mock_slack_client()
        client.chat_update = AsyncMock(return_value={"ok": True})
        provider = SlackChatProvider(client)

        await provider.update_message("C123", "1234567890.123456", "Updated text")

        client.chat_update.assert_called_once()
        call_kwargs = client.chat_update.call_args[1]
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["ts"] == "1234567890.123456"
        assert call_kwargs["text"] == "Updated text"

    async def test_update_message_with_blocks(self):
        """update_message should pass blocks to client."""
        client = make_mock_slack_client()
        client.chat_update = AsyncMock(return_value={"ok": True})
        provider = SlackChatProvider(client)

        blocks = [{"type": "divider"}]
        await provider.update_message("C123", "1234567890.123456", "text", blocks=blocks)

        call_kwargs = client.chat_update.call_args[1]
        assert call_kwargs["blocks"] == blocks


class TestSlackChatProviderAddReaction:
    """SlackChatProvider.add_reaction tests."""

    async def test_add_reaction_basic(self):
        """add_reaction should delegate to client.reactions_add."""
        client = make_mock_slack_client()
        client.reactions_add = AsyncMock(return_value={"ok": True})
        provider = SlackChatProvider(client)

        await provider.add_reaction("C123", "1234567890.123456", "thumbsup")

        client.reactions_add.assert_called_once()
        call_kwargs = client.reactions_add.call_args[1]
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["timestamp"] == "1234567890.123456"
        assert call_kwargs["name"] == "thumbsup"

    async def test_add_reaction_strips_colons(self):
        """add_reaction should strip colons from emoji."""
        client = make_mock_slack_client()
        client.reactions_add = AsyncMock(return_value={"ok": True})
        provider = SlackChatProvider(client)

        await provider.add_reaction("C123", "1234567890.123456", ":thumbsup:")

        call_kwargs = client.reactions_add.call_args[1]
        assert call_kwargs["name"] == "thumbsup"

    async def test_add_reaction_catches_exceptions(self):
        """add_reaction should catch exceptions gracefully."""
        client = make_mock_slack_client()
        client.reactions_add = AsyncMock(side_effect=Exception("Rate limited"))
        provider = SlackChatProvider(client)

        # Should not raise
        await provider.add_reaction("C123", "1234567890.123456", ":thumbsup:")


class TestSlackChatProviderUploadFile:
    """SlackChatProvider.upload_file tests."""

    async def test_upload_file_basic(self):
        """upload_file should delegate to client.files_upload_v2."""
        client = make_mock_slack_client()
        client.files_upload_v2 = AsyncMock(return_value={"ok": True})
        provider = SlackChatProvider(client)

        await provider.upload_file("C123", "file content", "test.txt")

        client.files_upload_v2.assert_called_once()
        call_kwargs = client.files_upload_v2.call_args[1]
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["content"] == "file content"
        assert call_kwargs["filename"] == "test.txt"
        assert call_kwargs["title"] == "test.txt"

    async def test_upload_file_with_custom_title(self):
        """upload_file should use custom title if provided."""
        client = make_mock_slack_client()
        client.files_upload_v2 = AsyncMock(return_value={"ok": True})
        provider = SlackChatProvider(client)

        await provider.upload_file("C123", "content", "test.txt", title="Custom Title")

        call_kwargs = client.files_upload_v2.call_args[1]
        assert call_kwargs["title"] == "Custom Title"

    async def test_upload_file_with_thread_ts(self):
        """upload_file should pass thread_ts to client."""
        client = make_mock_slack_client()
        client.files_upload_v2 = AsyncMock(return_value={"ok": True})
        provider = SlackChatProvider(client)

        await provider.upload_file("C123", "content", "test.txt", thread_ts="1234567890.111111")

        call_kwargs = client.files_upload_v2.call_args[1]
        assert call_kwargs["thread_ts"] == "1234567890.111111"


class TestSlackChatProviderCreateChannel:
    """SlackChatProvider.create_channel tests."""

    async def test_create_channel_public(self):
        """create_channel should create a public channel."""
        client = make_mock_slack_client()
        client.conversations_create = AsyncMock(
            return_value={"channel": {"id": "C_NEW", "name": "test-channel"}}
        )
        provider = SlackChatProvider(client)

        ref = await provider.create_channel("test-channel", is_private=False)

        assert ref.channel_id == "C_NEW"
        assert ref.name == "test-channel"
        call_kwargs = client.conversations_create.call_args[1]
        assert call_kwargs["name"] == "test-channel"
        assert call_kwargs["is_private"] is False

    async def test_create_channel_private(self):
        """create_channel should create a private channel."""
        client = make_mock_slack_client()
        client.conversations_create = AsyncMock(
            return_value={"channel": {"id": "C_NEW", "name": "test-channel"}}
        )
        provider = SlackChatProvider(client)

        await provider.create_channel("test-channel", is_private=True)

        call_kwargs = client.conversations_create.call_args[1]
        assert call_kwargs["is_private"] is True

    async def test_create_channel_handles_missing_name(self):
        """create_channel should handle responses without 'name' field."""
        client = make_mock_slack_client()
        client.conversations_create = AsyncMock(return_value={"channel": {"id": "C_NEW"}})
        provider = SlackChatProvider(client)

        ref = await provider.create_channel("test-channel")

        assert ref.channel_id == "C_NEW"
        assert ref.name == "test-channel"


class TestSlackChatProviderArchiveChannel:
    """SlackChatProvider.archive_channel tests."""

    async def test_archive_channel_basic(self):
        """archive_channel should delegate to client.conversations_archive."""
        client = make_mock_slack_client()
        client.conversations_archive = AsyncMock(return_value={"ok": True})
        provider = SlackChatProvider(client)

        await provider.archive_channel("C_OLD")

        client.conversations_archive.assert_called_once()
        call_kwargs = client.conversations_archive.call_args[1]
        assert call_kwargs["channel"] == "C_OLD"

    async def test_archive_channel_catches_exceptions(self):
        """archive_channel should catch exceptions gracefully."""
        client = make_mock_slack_client()
        client.conversations_archive = AsyncMock(side_effect=Exception("Not found"))
        provider = SlackChatProvider(client)

        # Should not raise
        await provider.archive_channel("C_OLD")
