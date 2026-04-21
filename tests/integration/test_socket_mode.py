"""Integration tests for Socket Mode connection lifecycle.

Verifies connect, disconnect, reconnect, and health monitor integration
using real Slack Socket Mode WebSocket connections.
"""

from __future__ import annotations

import asyncio
import secrets
from unittest.mock import AsyncMock

import pytest

from summon_claude.slack.bolt import _HealthMonitor
from tests.integration.conftest import EventConsumer

pytestmark = [
    pytest.mark.slack,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest.mark.xdist_group("slack_socket")
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


@pytest.mark.xdist_group("slack_socket")
class TestSocketDisconnect:
    async def test_socket_disconnect_clean(self, _slack_socket_lock, event_store, slack_harness):
        """A stopped consumer's socket client reports disconnected."""
        consumer = EventConsumer(
            bot_token=slack_harness.bot_token,
            app_token=slack_harness.app_token,
            signing_secret=slack_harness.signing_secret,
            event_store=event_store,
        )
        await asyncio.wait_for(consumer.start(), timeout=15.0)
        try:
            assert consumer._handler is not None
        finally:
            await consumer.stop()

        assert not await consumer._handler.client.is_connected()


@pytest.mark.xdist_group("slack_socket")
class TestSocketReconnect:
    async def test_socket_reconnect_after_close(
        self, _slack_socket_lock, event_store, slack_harness, test_channel
    ):
        """A consumer can be stopped and restarted; events flow after restart."""
        consumer = EventConsumer(
            bot_token=slack_harness.bot_token,
            app_token=slack_harness.app_token,
            signing_secret=slack_harness.signing_secret,
            event_store=event_store,
        )
        await asyncio.wait_for(consumer.start(), timeout=15.0)
        await consumer.stop()

        await asyncio.wait_for(consumer.start(), timeout=15.0)
        # Slack's routing table takes 1-3s to register a new consumer
        await asyncio.sleep(2.0)
        try:
            event_store.reset_reader()
            # Canary: confirm events are flowing after restart
            canary = f"canary-{secrets.token_hex(4)}"
            await slack_harness.client.chat_postMessage(channel=test_channel, text=canary)
            await consumer.wait_for_event(
                lambda e: e.get("type") == "message" and canary in e.get("text", ""),
                timeout=15.0,
            )
            consumer.drain()

            nonce = f"reconnect-{secrets.token_hex(6)}"
            await slack_harness.client.chat_postMessage(channel=test_channel, text=nonce)
            event = await consumer.wait_for_event(
                lambda e: e.get("type") == "message" and nonce in e.get("text", ""),
                timeout=15.0,
            )
            assert nonce in event.get("text", "")
        finally:
            await consumer.stop()


@pytest.mark.xdist_group("slack_socket")
class TestHealthMonitorConnected:
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


@pytest.mark.xdist_group("slack_socket")
class TestHealthMonitorDisconnected:
    async def test_health_monitor_detects_disconnect(
        self, _slack_socket_lock, event_store, slack_harness
    ):
        """_HealthMonitor reports unhealthy after socket is closed."""
        consumer = EventConsumer(
            bot_token=slack_harness.bot_token,
            app_token=slack_harness.app_token,
            signing_secret=slack_harness.signing_secret,
            event_store=event_store,
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
