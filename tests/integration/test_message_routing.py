"""Integration tests for ThreadRouter message routing."""

from __future__ import annotations

import asyncio

import pytest

from summon_claude.slack.router import ThreadRouter

pytestmark = [pytest.mark.slack]


class TestThreadRouter:
    """Test ThreadRouter message routing against real Slack."""

    async def test_post_to_main(self, thread_router, test_channel, slack_harness):
        ref = await thread_router.post_to_main("Main channel message")
        assert ref.channel_id == test_channel
        assert ref.ts
        # Verify not threaded
        history = await slack_harness.client.conversations_history(channel=test_channel, limit=1)
        msg = history["messages"][0]
        assert "thread_ts" not in msg or msg["thread_ts"] == msg["ts"]

    async def test_post_to_active_thread(
        self, thread_router, test_channel, slack_harness, slack_client
    ):
        """Post to active thread after setting it."""
        from summon_claude.slack.client import MessageRef

        # Create a message to become the active thread
        ref = await slack_client.post("\U0001f527 Turn 1: Processing...")
        thread_router.set_active_thread(ref.ts, ref)

        reply = await thread_router.post_to_active_thread("Turn thread message")
        assert reply.ts != ref.ts
        # Verify threaded under turn
        replies = await slack_harness.client.conversations_replies(channel=test_channel, ts=ref.ts)
        reply_timestamps = [m["ts"] for m in replies["messages"]]
        assert reply.ts in reply_timestamps

    async def test_active_thread_isolation(
        self, thread_router, test_channel, slack_harness, slack_client
    ):
        """Messages from different turns should be in separate threads."""
        ref1 = await slack_client.post("\U0001f527 Turn 1: Processing...")
        thread_router.set_active_thread(ref1.ts, ref1)
        msg1 = await thread_router.post_to_active_thread("Turn 1 message")

        ref2 = await slack_client.post("\U0001f527 Turn 2: Processing...")
        thread_router.set_active_thread(ref2.ts, ref2)
        msg2 = await thread_router.post_to_active_thread("Turn 2 message")

        assert ref1.ts != ref2.ts

        # Verify turn 1 message is in turn 1 thread
        replies1 = await slack_harness.client.conversations_replies(
            channel=test_channel, ts=ref1.ts
        )
        ts_list_1 = [m["ts"] for m in replies1["messages"]]
        assert msg1.ts in ts_list_1
        assert msg2.ts not in ts_list_1

        # Verify turn 2 message is in turn 2 thread
        replies2 = await slack_harness.client.conversations_replies(
            channel=test_channel, ts=ref2.ts
        )
        ts_list_2 = [m["ts"] for m in replies2["messages"]]
        assert msg2.ts in ts_list_2
        assert msg1.ts not in ts_list_2

    async def test_upload_to_active_thread(
        self, thread_router, test_channel, slack_harness, slack_client
    ):
        """File uploads should go to the active thread."""
        ref = await slack_client.post("\U0001f527 Turn 1: Processing...")
        thread_router.set_active_thread(ref.ts, ref)

        await thread_router.upload_to_active_thread(
            "file content here", "test-upload.txt", title="Test Upload"
        )
        # Retry to wait for Slack file indexing
        has_file = False
        for _ in range(3):
            await asyncio.sleep(1)
            replies = await slack_harness.client.conversations_replies(
                channel=test_channel, ts=ref.ts
            )
            # Look for file in thread replies
            has_file = any(
                m.get("files") or m.get("subtype") == "file_share" for m in replies["messages"]
            )
            if has_file:
                break
        assert has_file

    async def test_subagent_thread(self, thread_router, test_channel, slack_harness):
        """Subagent messages should be in their own thread."""
        subagent_ts = await thread_router.start_subagent_thread("tool_123", "Running analysis")
        ref = await thread_router.post_to_subagent_thread("tool_123", "Subagent result")
        replies = await slack_harness.client.conversations_replies(
            channel=test_channel, ts=subagent_ts
        )
        reply_timestamps = [m["ts"] for m in replies["messages"]]
        assert ref.ts in reply_timestamps


class TestConclusionMention:
    """Test conclusion @-mention behavior."""

    async def test_conclusion_with_user_mention(self, thread_router, test_channel, slack_harness):
        """Conclusion text should include @-mention when user_id is set."""
        bot_id = await slack_harness.resolve_bot_user_id()

        # Simulate what ResponseStreamer._flush_conclusion_to_main does
        conclusion_text = f"<@{bot_id}> Here is the conclusion"
        ref = await thread_router.post_to_main(conclusion_text)

        history = await slack_harness.client.conversations_history(
            channel=test_channel, latest=ref.ts, inclusive=True, limit=1
        )
        msg_text = history["messages"][0]["text"]
        assert f"<@{bot_id}>" in msg_text

    async def test_conclusion_without_user_mention(
        self, thread_router, test_channel, slack_harness
    ):
        """Conclusion text without user_id should not have @-mention."""
        conclusion_text = "Here is the conclusion without mention"
        ref = await thread_router.post_to_main(conclusion_text)

        history = await slack_harness.client.conversations_history(
            channel=test_channel, latest=ref.ts, inclusive=True, limit=1
        )
        msg_text = history["messages"][0]["text"]
        assert "<@" not in msg_text
