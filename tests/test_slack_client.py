"""Tests for summon_claude.slack.client — SlackClient and helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from summon_claude.slack.client import (
    MessageRef,
    SlackClient,
    sanitize_for_mrkdwn,
)


class TestMessageRef:
    def test_frozen(self):
        from dataclasses import FrozenInstanceError

        ref = MessageRef(channel_id="C123", ts="1234567890.123")
        with pytest.raises(FrozenInstanceError):
            ref.channel_id = "other"  # type: ignore[misc]

    def test_equality(self):
        a = MessageRef(channel_id="C123", ts="1.0")
        b = MessageRef(channel_id="C123", ts="1.0")
        assert a == b

    def test_fields(self):
        ref = MessageRef(channel_id="C456", ts="9.9")
        assert ref.channel_id == "C456"
        assert ref.ts == "9.9"


class TestSanitizeForMrkdwn:
    def test_removes_newlines(self):
        assert sanitize_for_mrkdwn("hello\nworld") == "hello world"

    def test_removes_carriage_returns(self):
        assert sanitize_for_mrkdwn("hello\rworld") == "hello world"

    def test_replaces_backticks_with_single_quote(self):
        assert sanitize_for_mrkdwn("he said `foo`") == "he said 'foo'"

    def test_removes_asterisks(self):
        assert sanitize_for_mrkdwn("**bold**") == "bold"

    def test_truncates_at_max_len(self):
        text = "a" * 200
        result = sanitize_for_mrkdwn(text, max_len=50)
        assert len(result) == 50

    def test_default_max_len_100(self):
        text = "b" * 150
        result = sanitize_for_mrkdwn(text)
        assert len(result) == 100

    def test_short_text_unchanged(self):
        assert sanitize_for_mrkdwn("hello") == "hello"

    def test_empty_string(self):
        assert sanitize_for_mrkdwn("") == ""


class TestSlackClient:
    def _make_client(self) -> tuple[SlackClient, MagicMock]:
        web = MagicMock()
        web.chat_postMessage = AsyncMock(return_value={"channel": "C123", "ts": "1.0"})
        web.chat_postEphemeral = AsyncMock(return_value={})
        web.chat_update = AsyncMock(return_value={})
        web.reactions_add = AsyncMock(return_value={})
        web.reactions_remove = AsyncMock(return_value={})
        web.files_upload_v2 = AsyncMock(return_value={})
        web.conversations_setTopic = AsyncMock(return_value={})
        web.assistant_threads_setStatus = AsyncMock(return_value={})
        client = SlackClient(web, "C123")
        return client, web

    def test_channel_id_attribute(self):
        web = MagicMock()
        client = SlackClient(web, "C456")
        assert client.channel_id == "C456"

    def test_web_is_private(self):
        web = MagicMock()
        client = SlackClient(web, "C456")
        assert not hasattr(client, "web")
        assert hasattr(client, "_web")

    async def test_post_returns_message_ref(self):
        client, web = self._make_client()
        ref = await client.post("hello")
        assert ref == MessageRef(channel_id="C123", ts="1.0")

    async def test_post_preserves_formatting(self):
        client, web = self._make_client()
        await client.post("hello\nworld")
        call_kwargs = web.chat_postMessage.call_args.kwargs
        assert call_kwargs["text"] == "hello\nworld"

    async def test_post_with_thread_ts(self):
        client, web = self._make_client()
        await client.post("hello", thread_ts="9999.0")
        call_kwargs = web.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "9999.0"

    async def test_post_with_blocks(self):
        client, web = self._make_client()
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
        await client.post("hello", blocks=blocks)
        call_kwargs = web.chat_postMessage.call_args.kwargs
        assert call_kwargs["blocks"] == blocks

    async def test_post_ephemeral_calls_api(self):
        client, web = self._make_client()
        await client.post_ephemeral("U123", "only for you")
        web.chat_postEphemeral.assert_called_once()
        call_kwargs = web.chat_postEphemeral.call_args.kwargs
        assert call_kwargs["user"] == "U123"
        assert call_kwargs["channel"] == "C123"

    async def test_post_ephemeral_swallows_errors(self):
        client, web = self._make_client()
        web.chat_postEphemeral.side_effect = Exception("api error")
        # Should not raise
        await client.post_ephemeral("U123", "text")

    async def test_update_calls_api(self):
        client, web = self._make_client()
        await client.update("1.0", "updated text")
        web.chat_update.assert_called_once()
        call_kwargs = web.chat_update.call_args.kwargs
        assert call_kwargs["ts"] == "1.0"
        assert call_kwargs["text"] == "updated text"
        assert call_kwargs["channel"] == "C123"

    async def test_react_calls_api(self):
        client, web = self._make_client()
        await client.react("1.0", "white_check_mark")
        web.reactions_add.assert_called_once()
        call_kwargs = web.reactions_add.call_args.kwargs
        assert call_kwargs["name"] == "white_check_mark"
        assert call_kwargs["timestamp"] == "1.0"

    async def test_react_strips_colons(self):
        client, web = self._make_client()
        await client.react("1.0", ":thumbsup:")
        call_kwargs = web.reactions_add.call_args.kwargs
        assert call_kwargs["name"] == "thumbsup"

    async def test_react_swallows_errors(self):
        client, web = self._make_client()
        web.reactions_add.side_effect = Exception("api error")
        # Should not raise
        await client.react("1.0", "thumbsup")

    async def test_upload_calls_api(self):
        client, web = self._make_client()
        await client.upload("file content", "test.py")
        web.files_upload_v2.assert_called_once()
        call_kwargs = web.files_upload_v2.call_args.kwargs
        assert call_kwargs["content"] == "file content"
        assert call_kwargs["filename"] == "test.py"
        assert call_kwargs["channel"] == "C123"

    async def test_upload_with_thread_ts(self):
        client, web = self._make_client()
        await client.upload("content", "file.txt", thread_ts="9999.0")
        call_kwargs = web.files_upload_v2.call_args.kwargs
        assert call_kwargs["thread_ts"] == "9999.0"

    async def test_upload_uses_filename_as_title_when_no_title(self):
        client, web = self._make_client()
        await client.upload("content", "test.py")
        call_kwargs = web.files_upload_v2.call_args.kwargs
        assert call_kwargs["title"] == "test.py"

    async def test_upload_custom_title(self):
        client, web = self._make_client()
        await client.upload("content", "test.py", title="My Script")
        call_kwargs = web.files_upload_v2.call_args.kwargs
        assert call_kwargs["title"] == "My Script"

    async def test_set_topic_calls_api(self):
        client, web = self._make_client()
        await client.set_topic("my topic")
        web.conversations_setTopic.assert_called_once()
        call_kwargs = web.conversations_setTopic.call_args.kwargs
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["topic"] == "my topic"

    async def test_unreact_calls_api(self):
        client, web = self._make_client()
        await client.unreact("1.0", "white_check_mark")
        web.reactions_remove.assert_called_once()
        call_kwargs = web.reactions_remove.call_args.kwargs
        assert call_kwargs["name"] == "white_check_mark"
        assert call_kwargs["timestamp"] == "1.0"
        assert call_kwargs["channel"] == "C123"

    async def test_unreact_strips_colons(self):
        client, web = self._make_client()
        await client.unreact("1.0", ":thumbsup:")
        call_kwargs = web.reactions_remove.call_args.kwargs
        assert call_kwargs["name"] == "thumbsup"

    async def test_unreact_swallows_errors(self):
        client, web = self._make_client()
        web.reactions_remove.side_effect = Exception("api error")
        # Should not raise
        await client.unreact("1.0", "thumbsup")

    async def test_set_thread_status_calls_api(self):
        client, web = self._make_client()
        await client.set_thread_status("1.0", "Thinking...")
        web.assistant_threads_setStatus.assert_called_once()
        call_kwargs = web.assistant_threads_setStatus.call_args.kwargs
        assert call_kwargs["channel_id"] == "C123"
        assert call_kwargs["thread_ts"] == "1.0"
        assert call_kwargs["status"] == "Thinking..."

    async def test_set_thread_status_clear(self):
        client, web = self._make_client()
        await client.set_thread_status("1.0", "")
        call_kwargs = web.assistant_threads_setStatus.call_args.kwargs
        assert call_kwargs["status"] == ""

    async def test_set_thread_status_swallows_errors(self):
        client, web = self._make_client()
        web.assistant_threads_setStatus.side_effect = Exception("api error")
        # Should not raise
        await client.set_thread_status("1.0", "Thinking...")
