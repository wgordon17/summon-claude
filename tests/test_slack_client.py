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

    async def test_upload_with_snippet_type(self):
        client, web = self._make_client()
        await client.upload("diff content", "test.diff", snippet_type="diff")
        call_kwargs = web.files_upload_v2.call_args.kwargs
        assert call_kwargs["snippet_type"] == "diff"

    async def test_upload_without_snippet_type_omits_key(self):
        client, web = self._make_client()
        await client.upload("content", "test.py")
        call_kwargs = web.files_upload_v2.call_args.kwargs
        assert "snippet_type" not in call_kwargs

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

    async def test_canvas_create_returns_canvas_id(self):
        client, web = self._make_client()
        web.api_call = AsyncMock(return_value={"canvas_id": "F_ABC"})
        result = await client.canvas_create("# Hello")
        assert result == "F_ABC"
        web.api_call.assert_called_once()
        call_args = web.api_call.call_args
        assert call_args[0][0] == "canvases.create"
        assert call_args[1]["json"]["channel_id"] == "C123"

    async def test_canvas_create_fallback_syncs_and_renames(self):
        """Fallback should find existing canvas, sync content, and rename."""
        client, web = self._make_client()
        call_count = 0

        async def api_call_side_effect(method, **kwargs):
            nonlocal call_count
            call_count += 1
            if method == "canvases.create":
                raise Exception("plan_limit")
            return {"ok": True}

        web.api_call = AsyncMock(side_effect=api_call_side_effect)
        web.files_list = AsyncMock(return_value={"files": [{"id": "F_EXIST"}]})
        result = await client.canvas_create("# Hello", title="My Canvas")
        assert result == "F_EXIST"
        # Should have called: canvases.create (failed), canvases.edit (sync), canvases.edit (rename)
        assert web.api_call.call_count == 3
        edit_calls = [c for c in web.api_call.call_args_list if c[0][0] == "canvases.edit"]
        assert len(edit_calls) == 2
        # First edit is content replace
        assert edit_calls[0][1]["json"]["changes"][0]["operation"] == "replace"
        # Second edit is rename
        assert edit_calls[1][1]["json"]["changes"][0]["operation"] == "rename"
        assert edit_calls[1][1]["json"]["changes"][0]["title_content"]["markdown"] == "My Canvas"

    async def test_canvas_create_fallback_no_existing(self):
        client, web = self._make_client()
        web.api_call = AsyncMock(side_effect=Exception("fail"))
        web.files_list = AsyncMock(return_value={"files": []})
        result = await client.canvas_create("# Hello")
        assert result is None

    async def test_canvas_sync_success(self):
        client, web = self._make_client()
        web.api_call = AsyncMock(return_value={"ok": True})
        ok = await client.canvas_sync("F_ABC", "# Updated")
        assert ok is True
        call_args = web.api_call.call_args
        assert call_args[0][0] == "canvases.edit"

    async def test_canvas_sync_failure_returns_false(self):
        client, web = self._make_client()
        web.api_call = AsyncMock(side_effect=Exception("rate_limited"))
        ok = await client.canvas_sync("F_ABC", "# Updated")
        assert ok is False

    async def test_canvas_rename_success(self):
        client, web = self._make_client()
        web.api_call = AsyncMock(return_value={"ok": True})
        ok = await client.canvas_rename("F_ABC", "New Title")
        assert ok is True
        call_args = web.api_call.call_args
        assert call_args[0][0] == "canvases.edit"
        change = call_args[1]["json"]["changes"][0]
        assert change["operation"] == "rename"
        assert change["title_content"]["markdown"] == "New Title"

    async def test_canvas_rename_failure_returns_false(self):
        client, web = self._make_client()
        web.api_call = AsyncMock(side_effect=Exception("not_allowed"))
        ok = await client.canvas_rename("F_ABC", "New Title")
        assert ok is False

    async def test_get_canvas_id_returns_first_file(self):
        client, web = self._make_client()
        web.files_list = AsyncMock(return_value={"files": [{"id": "F_FIRST"}]})
        result = await client.get_canvas_id()
        assert result == "F_FIRST"

    async def test_get_canvas_id_returns_none_when_empty(self):
        client, web = self._make_client()
        web.files_list = AsyncMock(return_value={"files": []})
        result = await client.get_canvas_id()
        assert result is None

    async def test_get_canvas_id_returns_none_on_error(self):
        client, web = self._make_client()
        web.files_list = AsyncMock(side_effect=Exception("api error"))
        result = await client.get_canvas_id()
        assert result is None
