"""Integration tests for Socket Mode connection lifecycle.

Verifies connect, disconnect, reconnect, and health monitor integration
using real Slack Socket Mode WebSocket connections.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from summon_claude.slack.bolt import _HealthMonitor
from tests.integration.conftest import EventConsumer, SlackTestHarness

pytestmark = [
    pytest.mark.slack,
    pytest.mark.xdist_group("slack_socket"),
    pytest.mark.asyncio(loop_scope="module"),
]


# ---------------------------------------------------------------------------
# Module-scoped fixtures
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
    """Module-scoped test channel for socket mode tests."""
    channel_id = await slack_harness.create_test_channel(prefix="socket")
    yield channel_id


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
        await asyncio.wait_for(consumer.start(), timeout=25.0)
    except TimeoutError:
        pytest.skip("Socket Mode connection timed out")
    except Exception as exc:
        await consumer.stop()
        pytest.skip(f"Socket Mode connection failed: {exc}")

    yield consumer
    await consumer.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSocketConnect:
    async def test_socket_connect(self, event_consumer, slack_harness, test_channel):
        """Verify event_consumer fixture successfully connects and events flow."""
        assert event_consumer._handler is not None

        nonce = f"connect-{secrets.token_hex(6)}"
        await slack_harness.client.chat_postMessage(channel=test_channel, text=nonce)

        event = await event_consumer.wait_for_event(
            lambda e: e.get("type") == "message" and nonce in e.get("text", ""),
            timeout=10.0,
        )
        assert nonce in event.get("text", "")


class TestSocketDisconnect:
    async def test_socket_disconnect_clean(self, slack_harness):
        """A stopped consumer's socket client reports disconnected."""
        consumer = EventConsumer(
            bot_token=slack_harness.bot_token,
            app_token=slack_harness.app_token,
            signing_secret=slack_harness.signing_secret,
        )
        await asyncio.wait_for(consumer.start(), timeout=15.0)
        try:
            assert consumer._handler is not None
        finally:
            await consumer.stop()

        assert not await consumer._handler.client.is_connected()


class TestSocketReconnect:
    async def test_socket_reconnect_after_close(self, slack_harness, test_channel):
        """A consumer can be stopped and restarted; events flow after restart."""
        consumer = EventConsumer(
            bot_token=slack_harness.bot_token,
            app_token=slack_harness.app_token,
            signing_secret=slack_harness.signing_secret,
        )
        await asyncio.wait_for(consumer.start(), timeout=15.0)
        await consumer.stop()

        await asyncio.wait_for(consumer.start(), timeout=25.0)
        try:
            nonce = f"reconnect-{secrets.token_hex(6)}"
            await slack_harness.client.chat_postMessage(channel=test_channel, text=nonce)
            event = await consumer.wait_for_event(
                lambda e: e.get("type") == "message" and nonce in e.get("text", ""),
                timeout=15.0,
            )
            assert nonce in event.get("text", "")
        finally:
            await consumer.stop()


class TestHealthMonitor:
    async def test_health_monitor_healthy(self, event_consumer):
        """_HealthMonitor reports healthy when the socket is connected."""
        assert event_consumer._handler is not None
        on_reconnect_needed = AsyncMock()
        on_exhausted = AsyncMock()

        monitor = _HealthMonitor(
            socket_handler=event_consumer._handler,
            on_reconnect_needed=on_reconnect_needed,
            on_exhausted=on_exhausted,
            check_interval=1.0,
            max_reconnect_attempts=3,
            event_probe=None,
        )

        result = await monitor._is_healthy()

        assert result is True
        on_reconnect_needed.assert_not_called()
        on_exhausted.assert_not_called()

    async def test_health_monitor_detects_disconnect(self, slack_harness):
        """_HealthMonitor reports unhealthy after socket is closed."""
        consumer = EventConsumer(
            bot_token=slack_harness.bot_token,
            app_token=slack_harness.app_token,
            signing_secret=slack_harness.signing_secret,
        )
        await asyncio.wait_for(consumer.start(), timeout=15.0)
        assert consumer._handler is not None
        on_reconnect_needed = AsyncMock()
        on_exhausted = AsyncMock()

        monitor = _HealthMonitor(
            socket_handler=consumer._handler,
            on_reconnect_needed=on_reconnect_needed,
            on_exhausted=on_exhausted,
            check_interval=1.0,
            max_reconnect_attempts=3,
            event_probe=None,
        )

        try:
            await consumer.stop()
            result = await monitor._is_healthy()
        finally:
            await consumer.stop()

        assert result is False
