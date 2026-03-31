"""Integration tests for Socket Mode event delivery round-trips.

Verifies the full pipeline: HTTP API call → Slack event generation →
Socket Mode WebSocket delivery → event handler.  Uses a real Socket Mode
connection via the ``EventConsumer`` fixture.

The active Socket Mode consumer also prevents Slack from auto-disabling
event subscriptions on the test app — events generated with no consumer
trigger persistent auto-disable.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets

import pytest
import pytest_asyncio

from summon_claude.slack.client import SlackClient
from tests.integration.conftest import EventConsumer, SlackTestHarness

pytestmark = [
    pytest.mark.slack,
    pytest.mark.xdist_group("slack_events"),
    pytest.mark.asyncio(loop_scope="module"),
]


# ---------------------------------------------------------------------------
# Module-scoped fixtures: all event-delivery tests share a single Socket Mode
# connection and event loop, eliminating per-test reconnection flakiness.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def slack_harness(_slack_socket_lock):
    """Module-scoped harness — skips if credentials not set."""
    if not os.environ.get("SUMMON_TEST_SLACK_BOT_TOKEN"):
        pytest.skip("SUMMON_TEST_SLACK_BOT_TOKEN not set")
    harness = SlackTestHarness()
    await harness.resolve_bot_user_id()
    yield harness


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def test_channel(slack_harness):
    """Module-scoped test channel for event delivery tests."""
    channel_id = await slack_harness.create_test_channel(prefix="events")
    user_id = await slack_harness.find_non_bot_user()
    if user_id:
        with contextlib.suppress(Exception):
            await slack_harness.client.conversations_invite(
                channel=channel_id,
                users=user_id,
            )
    await slack_harness.client.conversations_setTopic(
        channel=channel_id,
        topic="Event delivery integration tests",
    )
    yield channel_id


@pytest.fixture(scope="module")
def slack_client(slack_harness, test_channel):
    """Module-scoped SlackClient bound to event test channel."""
    return SlackClient(slack_harness.client, test_channel)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def event_consumer(slack_harness, test_channel):
    """Module-scoped Socket Mode consumer — connects once for all tests.

    A single long-lived connection eliminates per-test reconnection overhead
    and the flaky event delivery window during Socket Mode handshake. Tests
    use unique nonces in predicates, so cross-test interference is impossible.
    """
    consumer = EventConsumer(
        bot_token=slack_harness.bot_token,
        app_token=slack_harness.app_token,
        signing_secret=slack_harness.signing_secret,
    )
    try:
        await asyncio.wait_for(consumer.start(), timeout=15.0)
    except TimeoutError:
        pytest.skip("Socket Mode connection timed out (15s)")
    except Exception as exc:
        pytest.skip(f"Socket Mode connection failed: {exc}")

    canary = f"canary-{secrets.token_hex(4)}"
    await slack_harness.client.chat_postMessage(channel=test_channel, text=canary)
    try:
        await consumer.wait_for_event(
            lambda e: e.get("type") == "message" and canary in e.get("text", ""),
            timeout=10.0,
        )
    except TimeoutError:
        await consumer.stop()
        pytest.skip("Socket Mode canary failed — events not flowing")
    consumer.drain()

    yield consumer
    await consumer.stop()


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
