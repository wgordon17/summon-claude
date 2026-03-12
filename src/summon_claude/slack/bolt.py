"""BoltRouter — single shared Bolt instance for the daemon architecture.

``BoltRouter`` owns exactly one ``AsyncApp`` + ``AsyncSocketModeHandler`` pair
for the lifetime of the daemon.  All Slack events are received here and
dispatched to ``EventDispatcher``, which routes them to the correct session.

Lifecycle
---------
1. ``BoltRouter.__init__`` — creates ``AsyncWebClient`` and takes an
   ``EventDispatcher`` reference for event and command routing.
2. ``start()`` — builds the Bolt app, registers handlers, and connects the
   socket handler (calls ``connect_async``).
3. ``stop()`` — gracefully closes the socket handler.
4. ``reconnect()`` — creates a fresh ``AsyncApp`` + handler, re-registers all
   Bolt handlers, and reconnects.  Used by the health-monitor when the socket
   drops.
5. ``start_health_monitor()`` — starts the daemon-level socket health monitor
   task.  On exhaustion, posts to all session channels and calls the registered
   shutdown callback.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.http_retry.builtin_async_handlers import (
    AsyncRateLimitErrorRetryHandler,
    AsyncServerErrorRetryHandler,
)
from slack_sdk.web.async_client import AsyncWebClient

if TYPE_CHECKING:
    from summon_claude.config import SummonConfig
    from summon_claude.event_dispatcher import EventDispatcher

logger = logging.getLogger(__name__)

# Matches ask_user_* action IDs (same pattern as session.py)
_ASK_USER_PATTERN = re.compile(r"ask_user_\d+_.+")

_HEALTH_CHECK_INTERVAL_S = 10.0
_MAX_RECONNECT_ATTEMPTS = 10


# ---------------------------------------------------------------------------
# _RateLimiter — inlined from rate_limiter.py
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Simple per-key rate limiter with automatic cleanup.

    Safe for single-threaded asyncio (no await inside check/cleanup).
    """

    _CLEANUP_EVERY = 100

    def __init__(self, cooldown_seconds: float = 2.0) -> None:
        self._cooldown = cooldown_seconds
        self._last_attempt: dict[str, float] = {}
        self._call_count = 0

    def check(self, key: str) -> bool:
        """Return True if the request is allowed."""
        now = time.monotonic()
        self._call_count += 1
        if self._call_count >= self._CLEANUP_EVERY:
            self._call_count = 0
            self._cleanup()
        last = self._last_attempt.get(key, 0.0)
        if now - last < self._cooldown:
            return False
        self._last_attempt[key] = now
        return True

    def _cleanup(self) -> None:
        """Remove entries older than 5 minutes."""
        now = time.monotonic()
        self._last_attempt = {k: v for k, v in self._last_attempt.items() if now - v < 300.0}


# ---------------------------------------------------------------------------
# _HealthMonitor — inlined from socket_health.py
# ---------------------------------------------------------------------------


class _HealthMonitor:
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
        logger.debug("_HealthMonitor: handler updated, failure counter reset")

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
            logger.debug("_HealthMonitor: health check exception: %s", e)
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


# ---------------------------------------------------------------------------
# BoltRouter
# ---------------------------------------------------------------------------


class BoltRouter:
    """Owns the single Bolt ``AsyncApp`` and routes events to ``EventDispatcher``.

    All handler registration happens in ``_register_handlers()``, which is
    called both at construction time and after a ``reconnect()``.
    """

    def __init__(self, config: SummonConfig, dispatcher: EventDispatcher) -> None:
        self._config = config
        self._dispatcher = dispatcher
        self._rate_limiter = _RateLimiter()

        # Shared web client — stays alive across reconnects
        self.web_client = AsyncWebClient(
            token=config.slack_bot_token,
            retry_handlers=[AsyncRateLimitErrorRetryHandler(), AsyncServerErrorRetryHandler()],
        )

        # Set by start()
        self._app: AsyncApp | None = None
        self._socket_handler: AsyncSocketModeHandler | None = None
        self.bot_user_id: str | None = None

        # Health monitor — created by start_health_monitor()
        self._health_monitor: _HealthMonitor | None = None
        self._exhausted_notice_task: asyncio.Task[None] | None = None
        self._health_monitor_task: asyncio.Task[None] | None = None
        self.shutdown_callback: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build the Bolt app, register handlers, and connect to Slack."""
        logger.info("BoltRouter: starting")
        self._app, self._socket_handler = self._build_app()
        self._register_handlers(self._app)
        await self._socket_handler.connect_async()
        # Only fetch bot_user_id on first start — it never changes across reconnects
        if self.bot_user_id is None:
            resp = await self.web_client.auth_test()
            self.bot_user_id = resp["user_id"]
            logger.debug("BoltRouter: bot_user_id cached as %s", self.bot_user_id)

    async def stop(self) -> None:
        """Gracefully close the Socket Mode connection and health monitor."""
        logger.info("BoltRouter: stopping")
        self.stop_health_monitor()
        await self._close_socket()

    async def reconnect(self) -> None:
        """Close the old socket and start a fresh connection.

        The health monitor survives reconnects — it is notified about the
        new handler so it resets its failure counter.
        """
        logger.info("BoltRouter: reconnecting")
        await self._close_socket()
        await self.start()
        if self._health_monitor is not None and self._socket_handler is not None:
            self._health_monitor.update_handler(self._socket_handler)
        logger.info("BoltRouter: reconnected")

    async def _close_socket(self) -> None:
        """Close the socket handler (best-effort, swallows errors)."""
        if self._socket_handler is None:
            return
        try:
            await self._socket_handler.close_async()
        except Exception as e:
            logger.debug("BoltRouter: socket close error (expected): %s", e)

    def start_health_monitor(self) -> asyncio.Task[None]:
        """Create a ``_HealthMonitor`` and launch it as an asyncio task.

        On successful reconnection the health monitor's failure counter is
        reset via ``reconnect()``.  On exhaustion (10 failed attempts):

        1. Post a disconnect notice to every active session channel.
        2. Invoke ``shutdown_callback`` if set.

        Returns the created task so the caller (daemon) can cancel it on clean
        shutdown.
        """
        if self._socket_handler is None:
            raise RuntimeError("start() must be called before start_health_monitor()")
        self._health_monitor = _HealthMonitor(
            socket_handler=self._socket_handler,
            on_reconnect_needed=self.reconnect,
            on_exhausted=self._on_reconnect_exhausted,
            check_interval=_HEALTH_CHECK_INTERVAL_S,
            max_reconnect_attempts=_MAX_RECONNECT_ATTEMPTS,
        )
        self._health_monitor_task = asyncio.create_task(
            self._health_monitor.run(), name="bolt-health-monitor"
        )
        logger.info("BoltRouter: health monitor started")
        return self._health_monitor_task

    def stop_health_monitor(self) -> None:
        """Signal the health monitor to stop and cancel its task."""
        if self._health_monitor is not None:
            self._health_monitor.stop()
        if self._health_monitor_task is not None and not self._health_monitor_task.done():
            self._health_monitor_task.cancel()
        logger.debug("BoltRouter: health monitor stop requested")

    # ------------------------------------------------------------------
    # Health monitor bound methods
    # ------------------------------------------------------------------

    async def _on_reconnect_exhausted(self) -> None:
        """Called by _HealthMonitor when all reconnect attempts are exhausted."""
        logger.error(
            "BoltRouter: socket reconnection exhausted — posting to sessions and shutting down"
        )
        # Trigger daemon shutdown via registered callback
        if self.shutdown_callback is None:
            logger.warning("BoltRouter: no shutdown callback registered — daemon will hang")
        else:
            try:
                self.shutdown_callback()
            except Exception:
                logger.exception("BoltRouter: shutdown callback raised")
        # Post disconnect notice to all active session channels (best-effort).
        # Stored in a task so notifications are awaited with a timeout before
        # the event loop exits, preventing fire-and-forget loss on fast shutdown.
        channel_ids = self._dispatcher.all_channel_ids()
        if channel_ids:

            async def _send_notices() -> None:
                notice_tasks = [
                    asyncio.create_task(self._post_exhausted_notice(cid)) for cid in channel_ids
                ]
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        asyncio.gather(*notice_tasks, return_exceptions=True),
                        timeout=5.0,
                    )

            self._exhausted_notice_task = asyncio.ensure_future(_send_notices())

    async def _post_exhausted_notice(self, channel_id: str) -> None:
        """Post a permanent disconnect notice to a single channel."""
        with contextlib.suppress(Exception):
            # Raw web_client call — accepted exception to single-output-path rule.
            # These are last-resort crash-path messages sent before SlackClient exists.
            await self.web_client.chat_postMessage(
                channel=channel_id,
                text=(
                    ":x: *Slack connection lost permanently.*\n"
                    f"The daemon could not reconnect after {_MAX_RECONNECT_ATTEMPTS} attempts.\n"
                    "All sessions are terminating. Restart with `summon start`."
                ),
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_app(self) -> tuple[AsyncApp, AsyncSocketModeHandler]:
        """Create a new ``AsyncApp`` + ``AsyncSocketModeHandler`` pair."""
        app = AsyncApp(
            token=self._config.slack_bot_token,
            signing_secret=self._config.slack_signing_secret,
        )
        handler = AsyncSocketModeHandler(app, self._config.slack_app_token)
        return app, handler

    def _register_handlers(self, app: AsyncApp) -> None:
        """Register all Bolt event/action/command handlers on *app*."""
        app.command("/summon")(self._on_summon_command)
        app.event("message")(self._on_message)
        app.event("reaction_added")(self._on_reaction_added)
        app.action("permission_approve")(self._on_dispatch_action)
        app.action("permission_deny")(self._on_dispatch_action)
        app.action(_ASK_USER_PATTERN)(self._on_dispatch_action)

    # ------------------------------------------------------------------
    # Bolt handler bound methods
    # ------------------------------------------------------------------

    async def _on_summon_command(self, ack, command, respond) -> None:
        await ack()

        user_id = command.get("user_id", "")

        if not self._rate_limiter.check(user_id):
            await respond(text="Please wait before trying again.", response_type="ephemeral")
            return

        text = command.get("text", "").strip()
        if not text:
            await respond(
                text="Usage: `/summon <code>` — enter the code shown in terminal.",
                response_type="ephemeral",
            )
            return

        await self._dispatcher.dispatch_command(user_id=user_id, code=text, respond=respond)

    async def _on_message(self, event, say) -> None:  # noqa: ARG002
        await self._dispatcher.dispatch_message(event)

    async def _on_reaction_added(self, event) -> None:
        await self._dispatcher.dispatch_reaction(event)

    async def _on_dispatch_action(self, ack, action, body) -> None:
        await ack()
        await self._dispatcher.dispatch_action(action, body)
