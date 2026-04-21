"""Integration tests for Socket Mode resilience and reconnection.

Tests reconnection edge cases by directly manipulating the underlying
Socket Mode client state — force-disconnecting, rapid cycling, and
verifying health monitor recovery behavior.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from unittest.mock import AsyncMock

import pytest

from summon_claude.slack.bolt import _HealthMonitor
from tests.integration.conftest import EventConsumer

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.slack,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest.mark.xdist_group("slack_socket")
class TestForceDisconnect:
    async def test_reconnect_after_force_disconnect(
        self, _slack_socket_lock, event_store, slack_harness, test_channel
    ):
        """Force-disconnecting via SDK and starting a new consumer delivers events."""
        consumer = EventConsumer(
            bot_token=slack_harness.bot_token,
            app_token=slack_harness.app_token,
            signing_secret=slack_harness.signing_secret,
            event_store=event_store,
        )
        await asyncio.wait_for(consumer.start(), timeout=15.0)
        assert consumer._handler is not None
        try:
            await consumer._handler.client.disconnect()
        except Exception:
            logger.debug("disconnect raised (expected in test)", exc_info=True)
        await consumer.stop()

        # Allow SDK disconnect handlers and Slack's routing table to settle
        await asyncio.sleep(2.0)

        new_consumer = EventConsumer(
            bot_token=slack_harness.bot_token,
            app_token=slack_harness.app_token,
            signing_secret=slack_harness.signing_secret,
            event_store=event_store,
        )
        await asyncio.wait_for(new_consumer.start(), timeout=15.0)
        # Slack's routing table takes 1-3s to register a new consumer
        await asyncio.sleep(2.0)
        try:
            # Reset reader so we only see events from this point forward
            event_store.reset_reader()

            # Canary: confirm events are flowing before testing
            canary = f"canary-{secrets.token_hex(4)}"
            await slack_harness.client.chat_postMessage(channel=test_channel, text=canary)
            await new_consumer.wait_for_event(
                lambda e: e.get("type") == "message" and canary in e.get("text", ""),
                timeout=15.0,
            )
            new_consumer.drain()

            nonce = f"force-disc-{secrets.token_hex(6)}"
            await slack_harness.client.chat_postMessage(channel=test_channel, text=nonce)
            event = await new_consumer.wait_for_event(
                lambda e: e.get("type") == "message" and nonce in e.get("text", ""),
                timeout=15.0,
            )
            assert nonce in event.get("text", "")
        finally:
            await new_consumer.stop()

    async def test_reconnect_preserves_channel(
        self, _slack_socket_lock, event_store, slack_harness, test_channel
    ):
        """Messages posted across disconnect/reconnect cycles all appear in history."""
        consumer = EventConsumer(
            bot_token=slack_harness.bot_token,
            app_token=slack_harness.app_token,
            signing_secret=slack_harness.signing_secret,
            event_store=event_store,
        )
        await asyncio.wait_for(consumer.start(), timeout=15.0)
        assert consumer._handler is not None

        nonce_before = f"before-{secrets.token_hex(6)}"
        await slack_harness.client.chat_postMessage(channel=test_channel, text=nonce_before)
        try:
            await consumer._handler.client.disconnect()
        except Exception:
            logger.debug("disconnect raised (expected in test)", exc_info=True)
        await consumer.stop()

        new_consumer = EventConsumer(
            bot_token=slack_harness.bot_token,
            app_token=slack_harness.app_token,
            signing_secret=slack_harness.signing_secret,
            event_store=event_store,
        )
        await asyncio.wait_for(new_consumer.start(), timeout=15.0)
        try:
            nonce_after = f"after-{secrets.token_hex(6)}"
            await slack_harness.client.chat_postMessage(channel=test_channel, text=nonce_after)

            resp = await slack_harness.client.conversations_history(channel=test_channel, limit=50)
            messages = resp.get("messages", [])
            texts = [m.get("text", "") for m in messages]

            assert any(nonce_before in t for t in texts), (
                f"pre-disconnect message missing: {nonce_before}"
            )
            assert any(nonce_after in t for t in texts), (
                f"post-reconnect message missing: {nonce_after}"
            )
        finally:
            await new_consumer.stop()


@pytest.mark.xdist_group("slack_socket")
class TestReconnectCycles:
    async def test_rapid_disconnect_reconnect_cycles(
        self, _slack_socket_lock, event_store, slack_harness
    ):
        """Three rapid disconnect/reconnect cycles all report connected after reconnect."""
        consumer = EventConsumer(
            bot_token=slack_harness.bot_token,
            app_token=slack_harness.app_token,
            signing_secret=slack_harness.signing_secret,
            event_store=event_store,
        )
        await asyncio.wait_for(consumer.start(), timeout=15.0)
        assert consumer._handler is not None
        try:
            for _ in range(3):
                # Directly call the SDK disconnect to simulate a dropped connection
                try:
                    await consumer._handler.client.disconnect()
                except Exception:
                    logger.debug("disconnect raised (expected in test)", exc_info=True)
                await asyncio.sleep(0.5)  # Allow SDK disconnect handlers to complete
                await consumer._handler.connect_async()
                assert await consumer._handler.client.is_connected()
        finally:
            await consumer.stop()


@pytest.mark.xdist_group("slack_socket")
class TestHealthMonitorRecovery:
    async def test_reconnect_exhaustion(self):
        """_HealthMonitor triggers on_exhausted after max_reconnect_attempts failures."""
        mock_handler = AsyncMock()
        mock_handler.client = AsyncMock()
        mock_handler.client.is_connected = AsyncMock(return_value=False)

        on_reconnect_needed = AsyncMock()
        on_exhausted = AsyncMock()

        monitor = _HealthMonitor(
            socket_handler=mock_handler,
            on_reconnect_needed=on_reconnect_needed,
            on_exhausted=on_exhausted,
            check_interval=1.0,
            max_reconnect_attempts=2,
            event_probe=None,
        )

        # Call _handle_unhealthy 3 times — exhaustion triggers after attempt 2+1
        for _ in range(3):
            await monitor._handle_unhealthy()

        assert on_reconnect_needed.call_count == 2
        assert on_exhausted.called

    async def test_health_monitor_reset_on_success(self):
        """update_handler resets failure counters to zero."""
        mock_handler = AsyncMock()
        mock_handler.client = AsyncMock()
        mock_handler.client.is_connected = AsyncMock(return_value=True)

        on_reconnect_needed = AsyncMock()
        on_exhausted = AsyncMock()

        monitor = _HealthMonitor(
            socket_handler=mock_handler,
            on_reconnect_needed=on_reconnect_needed,
            on_exhausted=on_exhausted,
            check_interval=1.0,
            max_reconnect_attempts=3,
            event_probe=None,
        )

        monitor._consecutive_failures = 3
        monitor._consecutive_probe_failures = 2

        new_handler = AsyncMock()
        new_handler.client = AsyncMock()
        new_handler.client.is_connected = AsyncMock(return_value=True)

        monitor.update_handler(new_handler)

        assert monitor._consecutive_failures == 0
        assert monitor._consecutive_probe_failures == 0
