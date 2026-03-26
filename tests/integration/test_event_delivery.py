"""Integration tests for Socket Mode event delivery round-trips.

Verifies the full pipeline: HTTP API call → Slack event generation →
Socket Mode WebSocket delivery → event handler.  Uses a real Socket Mode
connection via the ``EventConsumer`` fixture.

The active Socket Mode consumer also prevents Slack from auto-disabling
event subscriptions on the test app — events generated with no consumer
trigger persistent auto-disable.
"""

from __future__ import annotations

import secrets

import pytest

pytestmark = [pytest.mark.slack, pytest.mark.xdist_group("slack_events")]


class TestMessageEvents:
    """Message events delivered via Socket Mode."""

    async def test_message_event_received(self, event_consumer, slack_client, test_channel):
        """chat.postMessage → message event arrives via Socket Mode."""
        nonce = secrets.token_hex(8)
        await slack_client.post(f"event-test-{nonce}")

        event = await event_consumer.wait_for_event(
            lambda e: e.get("type") == "message" and nonce in e.get("text", ""),
        )
        assert event["channel"] == test_channel
        assert nonce in event["text"]

    async def test_message_event_has_user_and_ts(
        self, event_consumer, slack_client, test_channel, slack_harness
    ):
        """Message event includes user, ts, and channel."""
        nonce = secrets.token_hex(8)
        ref = await slack_client.post(f"metadata-{nonce}")
        bot_user_id = await slack_harness.resolve_bot_user_id()

        event = await event_consumer.wait_for_event(
            lambda e: e.get("type") == "message" and nonce in e.get("text", ""),
        )
        assert event["user"] == bot_user_id
        assert event["ts"] == ref.ts
        assert event["channel"] == test_channel

    async def test_threaded_message_has_thread_ts(self, event_consumer, slack_client):
        """Threaded reply event includes thread_ts pointing to parent."""
        parent_nonce = secrets.token_hex(8)
        parent = await slack_client.post(f"parent-{parent_nonce}")

        # Consume parent message event first
        await event_consumer.wait_for_event(
            lambda e: e.get("type") == "message" and parent_nonce in e.get("text", ""),
        )

        reply_nonce = secrets.token_hex(8)
        await slack_client.post(f"reply-{reply_nonce}", thread_ts=parent.ts)

        event = await event_consumer.wait_for_event(
            lambda e: e.get("type") == "message" and reply_nonce in e.get("text", ""),
        )
        assert event.get("thread_ts") == parent.ts


class TestReactionEvents:
    """Reaction events delivered via Socket Mode."""

    async def test_reaction_added_event(
        self, event_consumer, slack_client, test_channel, slack_harness
    ):
        """reactions.add → reaction_added event arrives via Socket Mode."""
        nonce = secrets.token_hex(8)
        ref = await slack_client.post(f"react-target-{nonce}")

        # Consume the message event first
        await event_consumer.wait_for_event(
            lambda e: e.get("type") == "message" and nonce in e.get("text", ""),
        )

        # Use raw API — slack_client.react() swallows errors, which would
        # cause a misleading 10s timeout instead of an immediate failure.
        await slack_harness.client.reactions_add(
            channel=test_channel, name="white_check_mark", timestamp=ref.ts
        )

        event = await event_consumer.wait_for_event(
            lambda e: e.get("type") == "reaction_added" and e.get("item", {}).get("ts") == ref.ts,
        )
        assert event["reaction"] == "white_check_mark"
        assert event["item"]["channel"] == test_channel


class TestFileEvents:
    """File events delivered via Socket Mode."""

    async def test_file_shared_event(self, event_consumer, slack_client, test_channel):
        """files.upload → file_shared event arrives via Socket Mode."""
        nonce = secrets.token_hex(8)
        await slack_client.upload(f"content-{nonce}", f"test-{nonce}.txt", title=f"Test {nonce}")

        event = await event_consumer.wait_for_event(
            lambda e: e.get("type") == "file_shared" and e.get("channel_id") == test_channel,
            timeout=15.0,
        )
        file_id = event.get("file_id")
        assert isinstance(file_id, str) and file_id, f"file_shared event missing file_id: {event}"
