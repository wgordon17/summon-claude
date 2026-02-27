"""Integration tests for ThreadRouter and ResponseStreamer message routing."""

from __future__ import annotations

import asyncio

import pytest

from summon_claude.thread_router import ThreadRouter

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

    async def test_post_to_turn_thread(self, thread_router, test_channel, slack_harness):
        turn_ts = await thread_router.start_turn(1)
        ref = await thread_router.post_to_turn_thread("Turn thread message")
        assert ref.ts != turn_ts
        # Verify threaded under turn
        replies = await slack_harness.client.conversations_replies(channel=test_channel, ts=turn_ts)
        reply_timestamps = [m["ts"] for m in replies["messages"]]
        assert ref.ts in reply_timestamps

    async def test_post_permission_ephemeral(self, thread_router, slack_harness):
        bot_id = await slack_harness.resolve_bot_user_id()
        # Ephemeral — just verify no error
        await thread_router.post_permission_ephemeral(
            bot_id,
            "Permission prompt",
            [{"type": "section", "text": {"type": "mrkdwn", "text": "Approve?"}}],
        )

    async def test_turn_thread_isolation(self, thread_router, test_channel, slack_harness):
        """Messages from different turns should be in separate threads."""
        turn1_ts = await thread_router.start_turn(1)
        ref1 = await thread_router.post_to_turn_thread("Turn 1 message")

        turn2_ts = await thread_router.start_turn(2)
        ref2 = await thread_router.post_to_turn_thread("Turn 2 message")

        assert turn1_ts != turn2_ts

        # Verify turn 1 message is in turn 1 thread
        replies1 = await slack_harness.client.conversations_replies(
            channel=test_channel, ts=turn1_ts
        )
        ts_list_1 = [m["ts"] for m in replies1["messages"]]
        assert ref1.ts in ts_list_1
        assert ref2.ts not in ts_list_1

        # Verify turn 2 message is in turn 2 thread
        replies2 = await slack_harness.client.conversations_replies(
            channel=test_channel, ts=turn2_ts
        )
        ts_list_2 = [m["ts"] for m in replies2["messages"]]
        assert ref2.ts in ts_list_2
        assert ref1.ts not in ts_list_2

    async def test_upload_to_turn_thread(self, thread_router, test_channel, slack_harness):
        """File uploads should go to the turn thread."""
        turn_ts = await thread_router.start_turn(1)
        await thread_router.upload_to_turn_thread(
            "file content here", "test-upload.txt", title="Test Upload"
        )
        # Retry to wait for Slack file indexing
        has_file = False
        for _ in range(3):
            await asyncio.sleep(1)
            replies = await slack_harness.client.conversations_replies(
                channel=test_channel, ts=turn_ts
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
    """Test ResponseStreamer conclusion @-mention behavior.

    These tests verify the mention formatting logic without requiring
    a full Claude SDK message stream. We test the ThreadRouter routing
    that the streamer uses for conclusion text.
    """

    async def test_conclusion_with_user_mention(self, slack_provider, test_channel, slack_harness):
        """Conclusion text should include @-mention when user_id is set."""
        bot_id = await slack_harness.resolve_bot_user_id()
        router = ThreadRouter(slack_provider, test_channel)

        # Simulate what ResponseStreamer._flush_conclusion_to_main does
        conclusion_text = f"<@{bot_id}> Here is the conclusion"
        ref = await router.post_to_main(conclusion_text)

        history = await slack_harness.client.conversations_history(
            channel=test_channel, latest=ref.ts, inclusive=True, limit=1
        )
        msg_text = history["messages"][0]["text"]
        assert f"<@{bot_id}>" in msg_text

    async def test_conclusion_without_user_mention(
        self, slack_provider, test_channel, slack_harness
    ):
        """Conclusion text without user_id should not have @-mention."""
        router = ThreadRouter(slack_provider, test_channel)
        conclusion_text = "Here is the conclusion without mention"
        ref = await router.post_to_main(conclusion_text)

        history = await slack_harness.client.conversations_history(
            channel=test_channel, latest=ref.ts, inclusive=True, limit=1
        )
        msg_text = history["messages"][0]["text"]
        assert "<@" not in msg_text

    async def test_turn_summary_update(self, thread_router, test_channel, slack_harness):
        """Turn starter message should be updatable with summary."""
        turn_ts = await thread_router.start_turn(1)
        thread_router.record_tool_call("Read", {"file_path": "/tmp/test.py"})
        thread_router.record_tool_call("Edit", {"file_path": "/tmp/other.py"})
        summary = thread_router.generate_turn_summary()
        await thread_router.update_turn_summary(summary)

        history = await slack_harness.client.conversations_history(
            channel=test_channel, latest=turn_ts, inclusive=True, limit=1
        )
        msg_text = history["messages"][0]["text"]
        assert "2 tool calls" in msg_text
