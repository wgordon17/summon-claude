"""Integration tests for SlackChatProvider against real Slack API.

Channel lifecycle (create, invite, set_topic, archive) is exercised
transitively by the shared test_channel fixture in conftest.py.
Tests here focus on messaging operations and error handling.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.slack]


class TestMessaging:
    """Test SlackChatProvider messaging operations."""

    async def test_post_message(self, slack_provider, test_channel):
        ref = await slack_provider.post_message(test_channel, "Hello from integration test")
        assert ref.channel_id == test_channel
        assert ref.ts

    async def test_post_message_with_thread(self, slack_provider, test_channel, slack_harness):
        parent = await slack_provider.post_message(test_channel, "Parent message")
        reply = await slack_provider.post_message(test_channel, "Thread reply", thread_ts=parent.ts)
        assert reply.ts != parent.ts
        replies = await slack_harness.client.conversations_replies(
            channel=test_channel, ts=parent.ts
        )
        reply_timestamps = [m["ts"] for m in replies["messages"]]
        assert reply.ts in reply_timestamps

    async def test_update_message(self, slack_provider, test_channel, slack_harness):
        ref = await slack_provider.post_message(test_channel, "Original text")
        await slack_provider.update_message(test_channel, ref.ts, "Updated text")
        history = await slack_harness.client.conversations_history(
            channel=test_channel, latest=ref.ts, inclusive=True, limit=1
        )
        assert history["messages"][0]["text"] == "Updated text"

    async def test_add_reaction(self, slack_provider, test_channel, slack_harness):
        ref = await slack_provider.post_message(test_channel, "React to this")
        await slack_provider.add_reaction(test_channel, ref.ts, "eyes")
        reactions = await slack_harness.client.reactions_get(channel=test_channel, timestamp=ref.ts)
        reaction_names = [r["name"] for r in reactions["message"]["reactions"]]
        assert "eyes" in reaction_names

    async def test_upload_file(self, slack_provider, test_channel, slack_harness):
        await slack_provider.upload_file(
            test_channel, "test content", "test.txt", title="Test File"
        )
        has_file = False
        for _ in range(3):
            await asyncio.sleep(1)
            history = await slack_harness.client.conversations_history(
                channel=test_channel, limit=5
            )
            has_file = any(
                m.get("files") or m.get("subtype") == "file_share" for m in history["messages"]
            )
            if has_file:
                break
        assert has_file

    async def test_post_ephemeral(self, slack_provider, test_channel, slack_harness):
        bot_id = await slack_harness.resolve_bot_user_id()
        await slack_provider.post_ephemeral(test_channel, bot_id, "Ephemeral test")


class TestErrorHandling:
    """Test graceful error handling in SlackChatProvider."""

    async def test_archive_nonexistent_channel(self, slack_provider):
        await slack_provider.archive_channel("C000NONEXISTENT")

    async def test_invite_self_raises(self, slack_provider, test_channel, slack_harness):
        """Provider is transparent — cant_invite_self propagates to caller."""
        bot_id = await slack_harness.resolve_bot_user_id()
        with pytest.raises(Exception, match="cant_invite_self"):
            await slack_provider.invite_user(test_channel, bot_id)

    async def test_add_reaction_duplicate(self, slack_provider, test_channel):
        ref = await slack_provider.post_message(test_channel, "Duplicate reaction test")
        await slack_provider.add_reaction(test_channel, ref.ts, "thumbsup")
        await slack_provider.add_reaction(test_channel, ref.ts, "thumbsup")
