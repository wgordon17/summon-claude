"""Integration tests for Slack channel lifecycle operations.

Tests the raw Slack API behaviors that channel reuse and archived channel
recovery depend on: archive, unarchive, conversations_info is_archived flag,
join after unarchive, and creating channels with names reclaimed from
archived channels.
"""

from __future__ import annotations

import time

import pytest

from tests.integration.conftest import _channels_to_cleanup

pytestmark = [pytest.mark.slack]


class TestArchiveUnarchive:
    """Verify archive/unarchive round-trip works via real Slack API."""

    async def test_archive_then_info_shows_archived(self, slack_harness, fresh_channel):
        """conversations_info returns is_archived=True after archiving."""
        await slack_harness.client.conversations_archive(channel=fresh_channel)
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        assert info["channel"]["is_archived"] is True

    async def test_unarchive_restores_channel(self, slack_harness, fresh_channel):
        """Unarchiving a channel makes it active again."""
        await slack_harness.client.conversations_archive(channel=fresh_channel)
        await slack_harness.client.conversations_unarchive(channel=fresh_channel)
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        assert info["channel"]["is_archived"] is False

    async def test_post_message_after_unarchive(self, slack_harness, fresh_channel):
        """Bot can post to an unarchived private channel (already a member as creator).

        Private channels don't support conversations_join — the bot is already
        a member since it created the channel. Verify the channel is usable
        by posting a message after an archive/unarchive cycle.
        """
        await slack_harness.client.conversations_archive(channel=fresh_channel)
        await slack_harness.client.conversations_unarchive(channel=fresh_channel)
        resp = await slack_harness.client.chat_postMessage(
            channel=fresh_channel, text="Post-unarchive message"
        )
        assert resp["ok"] is True
        assert resp["message"]["text"] == "Post-unarchive message"


class TestChannelNameReclaim:
    """Verify channel name reuse after archiving (key to replacement flow)."""

    async def test_create_channel_with_archived_name_fails_for_private(self, slack_harness):
        """Creating a private channel with an archived private channel's name fails.

        Slack does NOT release names of archived private channels — you get
        name_taken. This validates why _handle_archived_channel falls back to
        the -resumed suffix when the same-name creation fails.
        """
        name = f"reclaim-integ-{int(time.time())}"[:80]
        resp = await slack_harness.client.conversations_create(name=name, is_private=True)
        original_id = resp["channel"]["id"]
        _channels_to_cleanup.append(original_id)

        await slack_harness.client.conversations_archive(channel=original_id)

        # Attempting same name → name_taken (private channels don't release names)
        with pytest.raises(Exception, match="name_taken"):
            await slack_harness.client.conversations_create(name=name, is_private=True)

        # But a suffixed name works (validates the -resumed fallback)
        resp2 = await slack_harness.client.conversations_create(
            name=f"{name}-resumed", is_private=True
        )
        replacement_id = resp2["channel"]["id"]
        _channels_to_cleanup.append(replacement_id)
        assert replacement_id != original_id

        await slack_harness.cleanup_channels([original_id, replacement_id])

    async def test_create_channel_name_taken_by_active(self, slack_harness):
        """Creating a channel whose name is taken by an active channel fails.

        This validates that the retry logic in _create_channel and the
        -resumed suffix fallback in _handle_archived_channel are necessary.
        """
        name = f"taken-integ-{int(time.time())}"[:80]
        resp = await slack_harness.client.conversations_create(name=name, is_private=True)
        active_id = resp["channel"]["id"]
        _channels_to_cleanup.append(active_id)

        with pytest.raises(Exception, match="name_taken"):
            await slack_harness.client.conversations_create(name=name, is_private=True)

        await slack_harness.cleanup_channels([active_id])


class TestConversationsInfo:
    """Verify conversations_info returns expected fields for channel reuse."""

    async def test_info_returns_channel_name(self, slack_harness, fresh_channel):
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        assert "name" in info["channel"]
        assert isinstance(info["channel"]["name"], str)
        assert len(info["channel"]["name"]) > 0

    async def test_info_active_channel_not_archived(self, slack_harness, fresh_channel):
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        assert info["channel"]["is_archived"] is False

    async def test_info_invalid_channel_raises(self, slack_harness):
        """conversations_info with a bogus channel ID raises an error.

        This validates the fallback path in _reuse_channel where info
        lookup fails and we create a new channel.
        """
        with pytest.raises(Exception, match="channel_not_found"):
            await slack_harness.client.conversations_info(channel="C0000INVALID")


class TestConversationsJoin:
    """Verify conversations_join behavior for channel reuse.

    Private channels don't support conversations_join — the API returns
    method_not_supported_for_channel_type. The bot is already a member
    since it created the channel. _reuse_channel handles this gracefully
    with a try/except.
    """

    async def test_join_private_channel_raises_not_supported(self, slack_harness, fresh_channel):
        """conversations_join on private channel → method_not_supported_for_channel_type.

        This is expected behavior — private channels require explicit invite.
        The _reuse_channel code wraps this in try/except as a debug log.
        """
        with pytest.raises(Exception, match="method_not_supported_for_channel_type"):
            await slack_harness.client.conversations_join(channel=fresh_channel)

    async def test_bot_is_already_member_of_created_channel(self, slack_harness, fresh_channel):
        """Bot can post to its own private channel without needing to join."""
        resp = await slack_harness.client.chat_postMessage(
            channel=fresh_channel, text="Already a member"
        )
        assert resp["ok"] is True


class TestChannelTopic:
    """Verify topic operations work on reused channels."""

    async def test_set_topic_on_fresh_channel(self, slack_harness, fresh_channel):
        topic = "model=opus | cwd=/tmp | branch=main"
        await slack_harness.client.conversations_setTopic(channel=fresh_channel, topic=topic)
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        assert info["channel"]["topic"]["value"] == topic

    async def test_update_topic_on_reused_channel(self, slack_harness, fresh_channel):
        """Setting topic multiple times works (simulates resume updating topic)."""
        await slack_harness.client.conversations_setTopic(channel=fresh_channel, topic="Topic v1")
        await slack_harness.client.conversations_setTopic(channel=fresh_channel, topic="Topic v2")
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        assert info["channel"]["topic"]["value"] == "Topic v2"
