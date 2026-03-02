"""Tests for summon_claude.slack.router — ThreadRouter (thread management only)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from summon_claude.slack.client import MessageRef, SlackClient
from summon_claude.slack.router import ThreadRouter


def make_mock_client(ts: str = "1234567890.123456") -> tuple[SlackClient, MagicMock]:
    """Create a mocked SlackClient bound to C123."""
    web = MagicMock()
    web.chat_postMessage = AsyncMock(return_value={"channel": "C123", "ts": ts})
    web.chat_postEphemeral = AsyncMock(return_value={})
    web.chat_update = AsyncMock(return_value={})
    web.reactions_add = AsyncMock(return_value={})
    web.files_upload_v2 = AsyncMock(return_value={})
    web.conversations_setTopic = AsyncMock(return_value={})
    client = SlackClient(web, "C123")
    return client, web


class TestThreadRouterInit:
    def test_init_with_slack_client(self):
        client, _ = make_mock_client()
        router = ThreadRouter(client)
        assert router.client.channel_id == "C123"

    def test_init_no_active_thread(self):
        client, _ = make_mock_client()
        router = ThreadRouter(client)
        assert router.active_thread_ts is None
        assert router.active_thread_ref is None

    def test_init_no_subagent_threads(self):
        client, _ = make_mock_client()
        router = ThreadRouter(client)
        assert router.subagent_threads == {}

    def test_client_is_public(self):
        client, _ = make_mock_client()
        router = ThreadRouter(client)
        assert hasattr(router, "client")
        assert router.client is client


class TestThreadRouterActiveThread:
    def test_set_active_thread(self):
        client, _ = make_mock_client()
        router = ThreadRouter(client)
        ref = MessageRef(channel_id="C123", ts="9999.0")
        router.set_active_thread("9999.0", ref)
        assert router.active_thread_ts == "9999.0"
        assert router.active_thread_ref == ref


class TestThreadRouterPostToMain:
    async def test_post_to_main_no_thread_ts(self):
        client, web = make_mock_client()
        router = ThreadRouter(client)
        await router.post_to_main("Hello world")
        call_kwargs = web.chat_postMessage.call_args.kwargs
        assert call_kwargs.get("thread_ts") is None

    async def test_post_to_main_returns_message_ref(self):
        client, _ = make_mock_client()
        router = ThreadRouter(client)
        ref = await router.post_to_main("text")
        assert isinstance(ref, MessageRef)
        assert ref.ts == "1234567890.123456"

    async def test_post_to_main_with_blocks(self):
        client, web = make_mock_client()
        router = ThreadRouter(client)
        blocks = [{"type": "divider"}]
        await router.post_to_main("text", blocks=blocks)
        call_kwargs = web.chat_postMessage.call_args.kwargs
        assert call_kwargs["blocks"] == blocks


class TestThreadRouterPostToActiveThread:
    async def test_post_to_active_thread_with_active_thread(self):
        client, web = make_mock_client()
        router = ThreadRouter(client)
        ref = MessageRef(channel_id="C123", ts="1234567890.123456")
        router.set_active_thread("1234567890.123456", ref)
        await router.post_to_active_thread("Reply in thread")
        call_kwargs = web.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "1234567890.123456"

    async def test_post_to_active_thread_falls_back_to_main(self):
        client, web = make_mock_client()
        router = ThreadRouter(client)
        await router.post_to_active_thread("Text")
        call_kwargs = web.chat_postMessage.call_args.kwargs
        assert call_kwargs.get("thread_ts") is None

    async def test_post_to_active_thread_returns_message_ref(self):
        client, _ = make_mock_client()
        router = ThreadRouter(client)
        ref = MessageRef(channel_id="C123", ts="1234567890.123456")
        router.set_active_thread("1234567890.123456", ref)
        result = await router.post_to_active_thread("Text")
        assert isinstance(result, MessageRef)


class TestThreadRouterPostToSubagentThread:
    async def test_post_to_subagent_thread_with_matching_id(self):
        client, web = make_mock_client()
        router = ThreadRouter(client)
        await router.start_subagent_thread("task_123", "Running analysis")
        await router.post_to_subagent_thread("task_123", "Subagent response")
        call_kwargs = web.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "1234567890.123456"

    async def test_post_to_subagent_thread_falls_back_to_active(self):
        client, web = make_mock_client()
        router = ThreadRouter(client)
        ref = MessageRef(channel_id="C123", ts="1234567890.123456")
        router.set_active_thread("1234567890.123456", ref)
        await router.post_to_subagent_thread("unknown_id", "Text")
        call_kwargs = web.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "1234567890.123456"


class TestThreadRouterStartSubagentThread:
    async def test_start_subagent_thread_creates_message(self):
        client, web = make_mock_client()
        router = ThreadRouter(client)
        ts = await router.start_subagent_thread("task_123", "Running analysis")
        assert ts == "1234567890.123456"
        text = web.chat_postMessage.call_args.kwargs["text"]
        assert "Subagent" in text
        assert "Running analysis" in text

    async def test_start_subagent_thread_tracks_by_tool_id(self):
        client, _ = make_mock_client()
        router = ThreadRouter(client)
        ts = await router.start_subagent_thread("task_123", "Description")
        assert router.subagent_threads["task_123"] == ts

    async def test_start_subagent_thread_evicts_when_over_limit(self):
        client, _ = make_mock_client()
        router = ThreadRouter(client)
        # Fill to the limit
        for i in range(100):
            router.subagent_threads[f"task_{i}"] = f"ts_{i}"
        # Adding one more should trigger eviction
        await router.start_subagent_thread("task_new", "New")
        assert len(router.subagent_threads) <= 51  # 50 remaining + 1 new


class TestThreadRouterUploadToActiveThread:
    async def test_upload_to_active_thread(self):
        client, web = make_mock_client()
        router = ThreadRouter(client)
        ref = MessageRef(channel_id="C123", ts="1234567890.123456")
        router.set_active_thread("1234567890.123456", ref)
        await router.upload_to_active_thread("content", "file.txt")
        web.files_upload_v2.assert_called_once()
        call_kwargs = web.files_upload_v2.call_args.kwargs
        assert call_kwargs["content"] == "content"
        assert call_kwargs["filename"] == "file.txt"
        assert call_kwargs["thread_ts"] == "1234567890.123456"
