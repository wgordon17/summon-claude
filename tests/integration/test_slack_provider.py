"""Integration tests for SlackChatProvider against real Slack API."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.slack]


class TestChannelOperations:
    """Test SlackChatProvider channel operations."""

    async def test_create_channel(self, slack_provider, slack_harness):
        import time

        name = f"test-create-{int(time.time())}"[:80]
        ref = await slack_provider.create_channel(name)
        assert ref.channel_id
        assert ref.name
        await slack_harness.cleanup_channels([ref.channel_id])

    async def test_create_channel_private(self, slack_provider, slack_harness):
        import time

        name = f"test-priv-{int(time.time())}"[:80]
        ref = await slack_provider.create_channel(name, is_private=True)
        assert ref.channel_id
        # Verify private via conversations_info
        info = await slack_harness.client.conversations_info(channel=ref.channel_id)
        assert info["channel"]["is_private"] is True
        await slack_harness.cleanup_channels([ref.channel_id])

    async def test_invite_user(self, slack_provider, test_channel, slack_harness):
        bot_id = await slack_harness.resolve_bot_user_id()
        # Bot created the channel so it's already a member. Slack returns
        # cant_invite_self — verify the provider passes the call through
        # (it doesn't swallow invite errors, unlike archive_channel).
        with pytest.raises(Exception, match="cant_invite_self"):
            await slack_provider.invite_user(test_channel, bot_id)

    async def test_archive_channel(self, slack_provider, slack_harness):
        import time

        name = f"test-arch-{int(time.time())}"[:80]
        ref = await slack_provider.create_channel(name)
        await slack_provider.archive_channel(ref.channel_id)
        # Verify archived
        info = await slack_harness.client.conversations_info(channel=ref.channel_id)
        assert info["channel"]["is_archived"] is True

    async def test_set_topic(self, slack_provider, test_channel, slack_harness):
        topic = "Integration test topic"
        await slack_provider.set_topic(test_channel, topic)
        info = await slack_harness.client.conversations_info(channel=test_channel)
        assert info["channel"]["topic"]["value"] == topic


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
        # Verify threading via conversations_replies
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
        await slack_provider.add_reaction(test_channel, ref.ts, "thumbsup")
        # Verify via conversations_history (doesn't require reactions:read scope)
        history = await slack_harness.client.conversations_history(
            channel=test_channel, latest=ref.ts, inclusive=True, limit=1
        )
        msg = history["messages"][0]
        reaction_names = [r["name"] for r in msg.get("reactions", [])]
        assert "thumbsup" in reaction_names

    async def test_upload_file(self, slack_provider, test_channel, slack_harness):
        await slack_provider.upload_file(
            test_channel, "test content", "test.txt", title="Test File"
        )
        # Retry to wait for Slack file indexing instead of a fixed delay
        has_file = False
        for _ in range(3):
            await asyncio.sleep(1)
            history = await slack_harness.client.conversations_history(
                channel=test_channel, limit=5
            )
            # File messages may appear as subtype=file_share or have files array
            has_file = any(
                m.get("files") or m.get("subtype") == "file_share" for m in history["messages"]
            )
            if has_file:
                break
        assert has_file

    async def test_post_ephemeral(self, slack_provider, test_channel, slack_harness):
        bot_id = await slack_harness.resolve_bot_user_id()
        # Ephemeral messages can't be verified via API — just check no error
        await slack_provider.post_ephemeral(test_channel, bot_id, "Ephemeral test")


class TestErrorHandling:
    """Test graceful error handling in SlackChatProvider."""

    async def test_archive_nonexistent_channel(self, slack_provider):
        # Provider swallows archive errors
        await slack_provider.archive_channel("C000NONEXISTENT")

    async def test_add_reaction_duplicate(self, slack_provider, test_channel):
        ref = await slack_provider.post_message(test_channel, "Duplicate reaction test")
        await slack_provider.add_reaction(test_channel, ref.ts, "thumbsup")
        # Second reaction should not raise (provider swallows duplicates)
        await slack_provider.add_reaction(test_channel, ref.ts, "thumbsup")
