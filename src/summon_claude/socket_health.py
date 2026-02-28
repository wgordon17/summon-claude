"""Socket health monitor — detects dead Slack WebSocket connections and triggers reconnection."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

logger = logging.getLogger(__name__)


class SocketHealthMonitor:
    """Monitors slack-sdk socket client health and triggers reconnection."""

    def __init__(
        self,
        socket_handler: AsyncSocketModeHandler,
        on_reconnect_needed: Callable[[], Awaitable[None]],
        on_exhausted: Callable[[], Awaitable[None]],
        check_interval: float = 10.0,
        max_reconnect_attempts: int = 5,
    ) -> None:
        self._socket_handler = socket_handler
        self._on_reconnect_needed = on_reconnect_needed
        self._on_exhausted = on_exhausted
        self._check_interval = check_interval
        self._max_reconnect_attempts = max_reconnect_attempts
        self._consecutive_failures = 0
        self._stop_event = asyncio.Event()

    def update_handler(self, socket_handler: AsyncSocketModeHandler) -> None:
        """Switch to a new socket handler after reconnection."""
        self._socket_handler = socket_handler
        self._consecutive_failures = 0
        logger.debug("SocketHealthMonitor: handler updated, failure counter reset")

    def mark_healthy(self) -> None:
        """Called when a message is successfully received; resets failure counter."""
        self._consecutive_failures = 0

    def stop(self) -> None:
        """Signal the monitoring loop to stop."""
        self._stop_event.set()

    async def run(self) -> None:
        """Main monitoring loop. Runs as an asyncio task."""
        while not self._stop_event.is_set():
            await asyncio.sleep(self._check_interval)
            if not self._stop_event.is_set() and not await self._is_healthy():
                await self._handle_unhealthy()

    async def _is_healthy(self) -> bool:
        """Check if the socket client is connected and responsive."""
        try:
            client = self._socket_handler.client
            return await client.is_connected()
        except Exception as e:
            logger.debug("SocketHealthMonitor: health check exception: %s", e)
            return False

    async def _handle_unhealthy(self) -> None:
        """Attempt recovery when socket is unhealthy."""
        self._consecutive_failures += 1
        if self._consecutive_failures <= self._max_reconnect_attempts:
            logger.warning(
                "Socket unhealthy (attempt %d/%d), triggering reconnect",
                self._consecutive_failures,
                self._max_reconnect_attempts,
            )
            try:
                await self._on_reconnect_needed()
            except Exception as e:
                logger.error("Reconnect callback raised: %s", e)
        else:
            logger.error(
                "Socket reconnection failed after %d attempts — signalling exhaustion",
                self._max_reconnect_attempts,
            )
            self._stop_event.set()
            await self._on_exhausted()
