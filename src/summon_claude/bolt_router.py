"""Single shared Bolt instance for the daemon architecture.

``BoltRouter`` owns exactly one ``AsyncApp`` + ``AsyncSocketModeHandler`` pair
for the lifetime of the daemon.  All Slack events are received here and
dispatched to ``EventDispatcher``, which routes them to the correct session.

Lifecycle
---------
1. ``BoltRouter.__init__`` — creates ``AsyncWebClient``, ``AsyncApp``, and
   ``AsyncSocketModeHandler`` then registers all Bolt handlers.
2. ``set_dispatcher`` / ``set_session_manager`` — deferred wiring called after
   both objects are constructed (breaks the circular dependency between
   BoltRouter and SessionManager).
3. ``start()`` — connects the socket handler (calls ``connect_async``).
4. ``stop()`` — gracefully closes the socket handler.
5. ``reconnect()`` — creates a fresh ``AsyncApp`` + handler, re-registers all
   Bolt handlers, and reconnects.  Used by the health-monitor when the socket
   drops.
6. ``start_health_monitor()`` — starts the daemon-level socket health monitor
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
    from summon_claude.session_manager import SessionManager

logger = logging.getLogger(__name__)

# Matches ask_user_* action IDs (same pattern as session.py)
_ASK_USER_PATTERN = re.compile(r"ask_user_\d+_.+")

_HEALTH_CHECK_INTERVAL_S = 10.0
_MAX_RECONNECT_ATTEMPTS = 5


class BoltRouter:
    """Owns the single Bolt ``AsyncApp`` and routes events to ``EventDispatcher``.

    All handler registration happens in ``_register_handlers()``, which is
    called both at construction time and after a ``reconnect()``.
    """

    def __init__(self, config: SummonConfig) -> None:
        self._config = config
        self._dispatcher: EventDispatcher | None = None
        self._session_manager: SessionManager | None = None
        self._rate_limiter = RateLimiter()

        # Shared web client — stays alive across reconnects
        self._client = AsyncWebClient(token=config.slack_bot_token)
        self._provider = SlackChatProvider(self._client)

        # Initial Bolt app + socket handler
        self._app, self._socket_handler = self._build_app()
        self._register_handlers(self._app)

        # Cached from auth_test() at start() time
        self._bot_user_id: str | None = None

        # Health monitor — created by start_health_monitor()
        self._health_monitor: SocketHealthMonitor | None = None
        self._exhausted_notice_task: asyncio.Task[None] | None = None
        self._health_monitor_task: asyncio.Task[None] | None = None
        self._shutdown_callback: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Deferred wiring
    # ------------------------------------------------------------------

    def set_dispatcher(self, dispatcher: EventDispatcher) -> None:
        """Wire the ``EventDispatcher``.  Must be called before ``start()``."""
        self._dispatcher = dispatcher

    def set_session_manager(self, session_manager: SessionManager) -> None:
        """Wire the ``SessionManager`` for ``/summon`` auth.  Must be called before ``start()``."""
        self._session_manager = session_manager

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect the Socket Mode handler to Slack."""
        logger.info("BoltRouter: connecting socket handler")
        await self._socket_handler.connect_async()
        resp = await self._client.auth_test()
        self._bot_user_id = resp["user_id"]
        logger.debug("BoltRouter: bot_user_id cached as %s", self._bot_user_id)

    async def stop(self) -> None:
        """Gracefully close the Socket Mode connection."""
        logger.info("BoltRouter: closing socket handler")
        self.stop_health_monitor()
        try:
            await self._socket_handler.close_async()
        except Exception as e:
            logger.debug("BoltRouter: socket close error (expected on dead connection): %s", e)

    async def reconnect(self) -> None:
        """Replace the current ``AsyncApp`` + handler with a fresh connection.

        Closes the old socket (best-effort), creates a new ``AsyncApp``,
        re-registers all handlers, and connects.  Notifies the health monitor
        so it resets its failure counter and tracks the new handler.
        """
        logger.info("BoltRouter: reconnecting socket")
        try:
            await self._socket_handler.close_async()
        except Exception as e:
            logger.debug("BoltRouter: old socket close error (expected): %s", e)

        self._app, self._socket_handler = self._build_app()
        self._register_handlers(self._app)
        await self._socket_handler.connect_async()

        # Inform health monitor about the new handler so it resets failure count
        if self._health_monitor is not None:
            self._health_monitor.update_handler(self._socket_handler)

        logger.info("BoltRouter: socket reconnected successfully")

    def set_shutdown_callback(self, callback: Callable[[], None]) -> None:
        """Register a zero-argument callable invoked when reconnection is exhausted.

        The daemon calls this after construction so that the health monitor can
        trigger daemon shutdown without a direct reference to the daemon.
        """
        self._shutdown_callback = callback

    def start_health_monitor(self) -> asyncio.Task[None]:
        """Create a ``SocketHealthMonitor`` and launch it as an asyncio task.

        On successful reconnection the health monitor's failure counter is
        reset via ``reconnect()``.  On exhaustion (5 failed attempts):

        1. Post a disconnect notice to every active session channel.
        2. Invoke the registered shutdown callback (``set_shutdown_callback``).

        Returns the created task so the caller (daemon) can cancel it on clean
        shutdown.
        """
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
        if self._shutdown_callback is None:
            logger.warning("BoltRouter: no shutdown callback registered — daemon will hang")
        else:
            self._shutdown_callback()
        # Post disconnect notice to all active session channels (best-effort).
        # Stored in a task so notifications are awaited with a timeout before
        # the event loop exits, preventing fire-and-forget loss on fast shutdown.
        if self._dispatcher is not None:
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
            await self._provider.post_message(
                channel_id,
                ":x: *Slack connection lost permanently.*\n"
                "The daemon could not reconnect after 5 attempts.\n"
                "All sessions are terminating. Restart with `summon start`.",
            )

    # ------------------------------------------------------------------
    # Shared provider
    # ------------------------------------------------------------------

    @property
    def provider(self) -> SlackChatProvider:
        """Return the shared ``SlackChatProvider`` (backed by the shared web client)."""
        return self._provider

    @property
    def bot_user_id(self) -> str | None:
        """Return the bot's Slack user ID, cached after ``start()`` calls ``auth_test()``."""
        return self._bot_user_id

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

        if self._session_manager is None:
            logger.warning("BoltRouter: /summon received but session_manager not set")
            await respond(
                text=":x: Service not ready. Please try again shortly.",
                response_type="ephemeral",
            )
            return

        # Delegate auth to session_manager
        await self._session_manager.handle_summon_command(
            user_id=user_id,
            code=text,
            respond=respond,
        )

    async def _on_message(self, event, say) -> None:  # noqa: ARG002
        if self._dispatcher is not None:
            await self._dispatcher.dispatch_message(event)

    async def _on_reaction_added(self, event) -> None:
        if self._dispatcher is not None:
            await self._dispatcher.dispatch_reaction(event)

    async def _on_dispatch_action(self, ack, action, body) -> None:
        await ack()
        if self._dispatcher is not None:
            await self._dispatcher.dispatch_action(action, body)
