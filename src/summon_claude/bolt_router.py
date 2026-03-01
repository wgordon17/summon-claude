"""Single shared Bolt instance for the daemon architecture.

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
from collections.abc import Callable
from typing import TYPE_CHECKING

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from summon_claude.providers.slack import SlackChatProvider
from summon_claude.rate_limiter import RateLimiter
from summon_claude.socket_health import SocketHealthMonitor

if TYPE_CHECKING:
    from summon_claude.config import SummonConfig
    from summon_claude.event_dispatcher import EventDispatcher

logger = logging.getLogger(__name__)

# Matches ask_user_* action IDs (same pattern as session.py)
_ASK_USER_PATTERN = re.compile(r"ask_user_\d+_.+")

_HEALTH_CHECK_INTERVAL_S = 10.0
_MAX_RECONNECT_ATTEMPTS = 10


class BoltRouter:
    """Owns the single Bolt ``AsyncApp`` and routes events to ``EventDispatcher``.

    All handler registration happens in ``_register_handlers()``, which is
    called both at construction time and after a ``reconnect()``.
    """

    def __init__(self, config: SummonConfig, dispatcher: EventDispatcher) -> None:
        self._config = config
        self._dispatcher = dispatcher
        self._rate_limiter = RateLimiter()

        # Shared web client — stays alive across reconnects
        self._client = AsyncWebClient(token=config.slack_bot_token)
        self.provider = SlackChatProvider(self._client)

        # Set by start()
        self._app: AsyncApp | None = None
        self._socket_handler: AsyncSocketModeHandler | None = None
        self.bot_user_id: str | None = None

        # Health monitor — created by start_health_monitor()
        self._health_monitor: SocketHealthMonitor | None = None
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
        resp = await self._client.auth_test()
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
        """Create a ``SocketHealthMonitor`` and launch it as an asyncio task.

        On successful reconnection the health monitor's failure counter is
        reset via ``reconnect()``.  On exhaustion (5 failed attempts):

        1. Post a disconnect notice to every active session channel.
        2. Invoke ``shutdown_callback`` if set.

        Returns the created task so the caller (daemon) can cancel it on clean
        shutdown.
        """
        if self._socket_handler is None:
            raise RuntimeError("start() must be called before start_health_monitor()")
        self._health_monitor = SocketHealthMonitor(
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
        """Called by SocketHealthMonitor when all reconnect attempts are exhausted."""
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
            await self.provider.post_message(
                channel_id,
                ":x: *Slack connection lost permanently.*\n"
                f"The daemon could not reconnect after {_MAX_RECONNECT_ATTEMPTS} attempts.\n"
                "All sessions are terminating. Restart with `summon start`.",
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
