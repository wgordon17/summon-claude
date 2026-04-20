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
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.http_retry.builtin_async_handlers import (
    AsyncRateLimitErrorRetryHandler,
    AsyncServerErrorRetryHandler,
)
from slack_sdk.web.async_client import AsyncWebClient

if TYPE_CHECKING:
    from aiohttp import WSMessage

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
# DiagnosticResult + EventProbe — active event pipeline health verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiagnosticResult:
    """Result of an event pipeline health probe or diagnostic cascade."""

    healthy: bool
    reason: str  # machine-readable key
    details: str  # user-facing message
    remediation_url: str | None = None


_PROBE_REACTION = "white_check_mark"
_PROBE_CHANNEL_NAME = "summon-health-probe"
_PROBE_ANCHOR_TEXT = "Event health probe anchor (do not delete)"


class EventProbe:
    """Active event pipeline health probe using reaction round-trip verification.

    Creates a private Slack channel, posts an anchor message, and periodically
    adds a reaction to verify the full event pipeline (Socket Mode → reaction_added
    event → raw WS listener) is functioning end-to-end.
    """

    def __init__(
        self,
        web_client: AsyncWebClient,
        config: SummonConfig,
    ) -> None:
        self._web_client = web_client
        self._config = config
        self._anchor_channel_id: str | None = None
        self._anchor_ts: str | None = None
        self._event_received: asyncio.Event = asyncio.Event()
        self._last_disconnect_reason: str | None = None
        self._probe_cancelled: bool = False
        self._probe_lock: asyncio.Lock = asyncio.Lock()

    async def setup_anchor(self) -> None:
        """Create or find the private health probe channel and post an anchor message."""
        if self._anchor_channel_id is not None:
            return

        channel_id = await self._resolve_probe_channel()
        if channel_id is None:
            raise RuntimeError("EventProbe: could not find or create probe channel")

        # Post anchor message
        resp = await self._web_client.chat_postMessage(
            channel=channel_id,
            text=_PROBE_ANCHOR_TEXT,
        )
        self._anchor_channel_id = channel_id
        self._anchor_ts = resp["ts"]
        self._save_channel_cache(channel_id)
        logger.debug("EventProbe: anchor posted in %s at ts=%s", channel_id, self._anchor_ts)

    async def _resolve_probe_channel(self) -> str | None:
        """Resolve the probe channel: cached ID → create → random-suffix fallback."""
        import secrets  # noqa: PLC0415

        # 1. Try cached channel ID (1 API call to validate)
        cached_id = self._load_channel_cache()
        if cached_id is not None:
            try:
                resp = await self._web_client.conversations_info(channel=cached_id)
                ch = resp.get("channel", {})
                if ch.get("is_archived"):
                    with contextlib.suppress(Exception):
                        await self._web_client.conversations_unarchive(channel=cached_id)
                logger.debug("EventProbe: reusing cached probe channel %s", cached_id)
                return cached_id
            except Exception:
                logger.debug("EventProbe: cached channel %s is stale, creating new", cached_id)
                self._clear_channel_cache()

        # 2. Try to create the channel (canonical name first, then random suffix)
        for name in (_PROBE_CHANNEL_NAME, f"{_PROBE_CHANNEL_NAME}-{secrets.token_hex(3)}"):
            try:
                resp = await self._web_client.conversations_create(
                    name=name,
                    is_private=True,
                )
                channel_id = resp["channel"]["id"]  # type: ignore[index]
                logger.debug("EventProbe: created probe channel %s (%s)", channel_id, name)
                return channel_id
            except Exception as e:
                if "name_taken" not in str(e):
                    raise
        return None

    @staticmethod
    def _channel_cache_path() -> Path:
        from summon_claude.config import get_data_dir  # noqa: PLC0415

        return get_data_dir() / "probe-channel-id"

    @staticmethod
    def _load_channel_cache() -> str | None:
        path = EventProbe._channel_cache_path()
        try:
            content = path.read_text().strip()
            return content if content else None
        except FileNotFoundError:
            return None

    @staticmethod
    def _save_channel_cache(channel_id: str) -> None:
        path = EventProbe._channel_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(channel_id)

    @staticmethod
    def _clear_channel_cache() -> None:
        EventProbe._channel_cache_path().unlink(missing_ok=True)

    async def run_probe(self, timeout: float = 10.0) -> DiagnosticResult:  # noqa: ASYNC109
        """Run an active event probe."""
        if self._anchor_channel_id is None or self._anchor_ts is None:
            return DiagnosticResult(
                healthy=False,
                reason="unknown",
                details="Probe anchor not set up.",
            )

        async with self._probe_lock:
            return await self._run_probe_locked(timeout, self._anchor_channel_id, self._anchor_ts)

    async def _run_probe_locked(
        self, wait_seconds: float, channel_id: str, anchor_ts: str
    ) -> DiagnosticResult:
        """Probe implementation — must be called under _probe_lock."""
        self._event_received.clear()
        self._probe_cancelled = False
        self._last_disconnect_reason = None

        # Step 1: Remove existing reaction (best-effort, ignore no_reaction)
        with contextlib.suppress(Exception):
            await self._web_client.reactions_remove(
                channel=channel_id,
                timestamp=anchor_ts,
                name=_PROBE_REACTION,
            )

        # Step 2: Add reaction
        try:
            await self._web_client.reactions_add(
                channel=channel_id,
                timestamp=anchor_ts,
                name=_PROBE_REACTION,
            )
        except Exception as e:
            if "already_reacted" not in str(e):
                return DiagnosticResult(
                    healthy=False,
                    reason="unknown",
                    details=f"Failed to add reaction: {e}",
                )
            # already_reacted: remove stuck reaction and retry
            try:
                await self._web_client.reactions_remove(
                    channel=channel_id,
                    timestamp=anchor_ts,
                    name=_PROBE_REACTION,
                )
                await self._web_client.reactions_add(
                    channel=channel_id,
                    timestamp=anchor_ts,
                    name=_PROBE_REACTION,
                )
            except Exception as e2:
                if "already_reacted" in str(e2):
                    logger.warning(
                        "EventProbe: reaction irrecoverably stuck — skipping probe cycle"
                    )
                    return DiagnosticResult(
                        healthy=True,
                        reason="healthy",
                        details="Probe skipped (reaction stuck).",
                    )
                return DiagnosticResult(
                    healthy=False,
                    reason="unknown",
                    details=f"Failed to add reaction after retry: {e2}",
                )

        # Step 3: Wait for event (or cancellation via cancel_probe setting _event_received)
        try:
            await asyncio.wait_for(self._event_received.wait(), timeout=wait_seconds)
        except TimeoutError:
            if not self._probe_cancelled:
                return await self._run_diagnostic_cascade()
            # Cancelled during timeout — fall through to cancelled result below

        if self._probe_cancelled:
            return DiagnosticResult(
                healthy=True,
                reason="cancelled",
                details="Probe cancelled during reconnect.",
            )
        return DiagnosticResult(healthy=True, reason="healthy", details="Event pipeline OK.")

    async def _run_diagnostic_cascade(self) -> DiagnosticResult:
        """Sequential diagnostic checks to identify the root cause of probe failure."""
        app_url = self._config.slack_app_url

        # 1. api.test — check network/Slack reachability
        try:
            await self._web_client.api_test()
        except Exception:
            return DiagnosticResult(
                healthy=False,
                reason="slack_down",
                details="Slack API is unreachable. Check network connectivity.",
            )

        # 2. auth.test — check token validity
        try:
            await self._web_client.auth_test()
        except Exception:
            return DiagnosticResult(
                healthy=False,
                reason="token_revoked",
                details="Bot token is invalid or revoked.",
                remediation_url=f"{app_url}/oauth",
            )

        # 3. link_disabled disconnect reason (auth_test already confirmed token is valid)
        if self._last_disconnect_reason == "link_disabled":
            return DiagnosticResult(
                healthy=False,
                reason="socket_disabled",
                details="Socket Mode was disabled.",
                remediation_url=f"{app_url}/socket-mode",
            )

        # 4. Socket connected but no events
        return DiagnosticResult(
            healthy=False,
            reason="events_disabled",
            details=(
                "Socket Mode is connected but events are not being delivered."
                " Check Event Subscriptions."
            ),
            remediation_url=f"{app_url}/event-subscriptions",
        )

    async def on_ws_message(self, message: WSMessage) -> None:
        """Raw WebSocket message listener registered on SocketModeClient."""
        import aiohttp  # noqa: PLC0415

        try:
            if message.type != aiohttp.WSMsgType.TEXT:
                return
            data = json.loads(message.data)
            msg_type = data.get("type", "")

            if msg_type == "events_api" and self._anchor_channel_id is not None:
                payload = data.get("payload", {})
                event = payload.get("event", {})
                if event.get("type") == "reaction_added":
                    item = event.get("item", {})
                    if (
                        item.get("channel") == self._anchor_channel_id
                        and item.get("ts") == self._anchor_ts
                    ):
                        self._event_received.set()

            elif msg_type == "disconnect":
                reason = data.get("reason")
                if reason and isinstance(reason, str):
                    self._last_disconnect_reason = reason[:64]
                    logger.debug("EventProbe: disconnect reason=%s", reason)

        except Exception as e:
            logger.debug("EventProbe.on_ws_message: parse error (ignored): %s", e)

    def cancel_probe(self) -> None:
        """Mark any in-flight probe as cancelled (called before reconnect)."""
        self._probe_cancelled = True
        self._event_received.set()

    def reset_cancel(self) -> None:
        """Clear the cancelled flag (called after reconnect completes)."""
        self._probe_cancelled = False

    def format_alert(self, result: DiagnosticResult) -> str:
        """Format a diagnostic result as a Slack alert message string."""
        from summon_claude.slack.client import redact_secrets  # noqa: PLC0415

        lines = [f":x: *Event pipeline failure detected*\n{result.details}"]
        if result.remediation_url:
            lines.append(result.remediation_url)
        return redact_secrets("\n".join(lines))


# ---------------------------------------------------------------------------
# _HealthMonitor — inlined from socket_health.py
# ---------------------------------------------------------------------------


class _HealthMonitor:
    """Monitors slack-sdk socket client health and triggers reconnection."""

    def __init__(  # noqa: PLR0913
        self,
        socket_handler: AsyncSocketModeHandler,
        on_reconnect_needed: Callable[[], Awaitable[None]],
        on_exhausted: Callable[[], Awaitable[None]],
        check_interval: float = 10.0,
        max_reconnect_attempts: int = 5,
        event_probe: EventProbe | None = None,
        dispatcher: EventDispatcher | None = None,
    ) -> None:
        self._socket_handler = socket_handler
        self._on_reconnect_needed = on_reconnect_needed
        self._on_exhausted = on_exhausted
        self._check_interval = check_interval
        self._max_reconnect_attempts = max_reconnect_attempts
        self._event_probe = event_probe
        self._dispatcher = dispatcher
        self._consecutive_failures = 0
        self._consecutive_probe_failures = 0
        self._last_diagnostic: DiagnosticResult | None = None
        self._stop_event = asyncio.Event()

    @property
    def last_diagnostic(self) -> DiagnosticResult | None:
        """Return the most recent diagnostic result from a failed probe."""
        return self._last_diagnostic

    def update_handler(self, socket_handler: AsyncSocketModeHandler) -> None:
        """Switch to a new socket handler after reconnection."""
        self._socket_handler = socket_handler
        self._consecutive_failures = 0
        self._consecutive_probe_failures = 0
        self._last_diagnostic = None
        logger.debug("_HealthMonitor: handler updated, failure counters reset")

    def stop(self) -> None:
        """Signal the monitoring loop to stop."""
        self._stop_event.set()

    async def run(self) -> None:
        """Main monitoring loop. Runs as an asyncio task."""
        while not self._stop_event.is_set():
            await asyncio.sleep(self._check_interval)
            if not self._stop_event.is_set() and not await self._is_healthy():
                await self._handle_unhealthy()

    async def _is_healthy(self) -> bool:  # noqa: PLR0911
        """Check if the socket client is connected and responsive."""
        try:
            client = self._socket_handler.client
            connected = await client.is_connected()
        except Exception as e:
            logger.debug("_HealthMonitor: health check exception: %s", e)
            self._last_diagnostic = None  # clear stale probe diagnostic
            return False

        if not connected:
            self._last_diagnostic = None  # clear stale probe diagnostic
            return False

        # Skip event probe if no sessions are active or probe is not available
        if self._event_probe is None:
            return True
        if self._dispatcher is not None and not self._dispatcher.has_active_sessions():
            return True

        # Run event probe
        try:
            result = await self._event_probe.run_probe()
        except Exception as e:
            logger.debug("_HealthMonitor: event probe exception: %s", e)
            return True  # probe error ≠ unhealthy socket

        if result.healthy:
            self._consecutive_probe_failures = 0
            self._last_diagnostic = None
            return True

        self._last_diagnostic = result
        return False

    async def _handle_unhealthy(self) -> None:
        """Attempt recovery when socket is unhealthy."""
        diagnostic = self._last_diagnostic

        # Definitive signals — skip reconnect attempts, go straight to exhaustion
        if diagnostic is not None and diagnostic.reason in ("socket_disabled", "token_revoked"):
            logger.error(
                "Socket health: definitive failure (%s) — triggering exhaustion immediately",
                diagnostic.reason,
            )
            self._stop_event.set()
            await self._on_exhausted()
            return

        # Probe-specific failures (events_disabled, unknown) — require 3 consecutive
        if diagnostic is not None and diagnostic.reason in ("events_disabled", "unknown"):
            self._consecutive_probe_failures += 1
            logger.warning(
                "Event probe failure %d/3: %s",
                self._consecutive_probe_failures,
                diagnostic.reason,
            )
            if self._consecutive_probe_failures < 3:
                return  # not yet at threshold
            logger.error(
                "Event probe failed 3 consecutive times (%s) — triggering exhaustion",
                diagnostic.reason,
            )
            self._stop_event.set()
            await self._on_exhausted()
            return

        # Socket-level failures or slack_down — use existing reconnect logic
        self._consecutive_probe_failures = 0
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
        self._event_probe: EventProbe | None = None

        # Health monitor — created by start_health_monitor()
        self._health_monitor: _HealthMonitor | None = None
        self._exhausted_notice_task: asyncio.Task[None] | None = None
        self._health_monitor_task: asyncio.Task[None] | None = None
        self.shutdown_callback: Callable[[], None] | None = None
        # Called before shutdown_callback when exhaustion is due to event pipeline failure
        self.event_failure_callback: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build the Bolt app, register handlers, and connect to Slack."""
        logger.info("BoltRouter: starting")
        self._app, self._socket_handler = self._build_app()
        self._register_handlers(self._app)
        await self._socket_handler.connect_async()
        # Only fetch bot_user_id and create EventProbe on first start
        if self.bot_user_id is None:
            resp = await self.web_client.auth_test()
            self.bot_user_id = resp["user_id"]
            logger.debug("BoltRouter: bot_user_id cached as %s", self.bot_user_id)

            # Create EventProbe — degrade gracefully if setup fails
            probe = EventProbe(web_client=self.web_client, config=self._config)
            try:
                await probe.setup_anchor()
                self._event_probe = probe
                self._socket_handler.client.on_message_listeners.append(probe.on_ws_message)
                logger.info("BoltRouter: EventProbe setup complete")
            except Exception as e:
                logger.warning("BoltRouter: EventProbe setup failed (probe disabled): %s", e)
                self._event_probe = None

    async def stop(self) -> None:
        """Gracefully close the Socket Mode connection and health monitor."""
        logger.info("BoltRouter: stopping")
        self.stop_health_monitor()
        await self._close_socket()

    @property
    def event_probe(self) -> EventProbe | None:
        """Return the EventProbe instance, or None if setup failed."""
        return self._event_probe

    async def reconnect(self) -> None:
        """Close the old socket and start a fresh connection.

        The health monitor survives reconnects — it is notified about the
        new handler so it resets its failure counter.
        """
        logger.info("BoltRouter: reconnecting")
        # Cancel any in-flight probe to avoid misdiagnosis during reconnect
        if self._event_probe is not None:
            self._event_probe.cancel_probe()
        await self._close_socket()
        await self.start()
        if self._health_monitor is not None and self._socket_handler is not None:
            self._health_monitor.update_handler(self._socket_handler)
        # Re-register WS listener on new handler and reset cancel flag
        if self._event_probe is not None and self._socket_handler is not None:
            self._event_probe.reset_cancel()
            listeners = self._socket_handler.client.on_message_listeners
            if self._event_probe.on_ws_message not in listeners:
                listeners.append(self._event_probe.on_ws_message)
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
            event_probe=self._event_probe,
            dispatcher=self._dispatcher,
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
        # If exhaustion is due to event pipeline failure (not socket/network),
        # signal for session suspension so sessions can be resumed after fixing.
        diagnostic = (
            self._health_monitor.last_diagnostic if self._health_monitor is not None else None
        )
        if (
            diagnostic is not None
            and diagnostic.reason in ("events_disabled", "unknown")
            and self.event_failure_callback is not None
        ):
            try:
                self.event_failure_callback()
            except Exception:
                logger.exception("BoltRouter: event_failure_callback raised")

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
        from summon_claude.slack.client import redact_secrets  # noqa: PLC0415

        with contextlib.suppress(Exception):
            # Build diagnostic-aware notice text
            diagnostic = (
                self._health_monitor.last_diagnostic if self._health_monitor is not None else None
            )
            if diagnostic is not None and self._event_probe is not None:
                notice_text = self._event_probe.format_alert(diagnostic)
                if diagnostic.reason in ("events_disabled", "unknown"):
                    notice_text += (
                        "\nFix the issue, then run `summon project up` to resume"
                        " project sessions or `summon start` for new sessions."
                    )
                else:
                    notice_text += "\nAll sessions are terminating. Restart with `summon start`."
            else:
                notice_text = (
                    ":x: *Slack connection lost permanently.*\n"
                    f"The daemon could not reconnect after {_MAX_RECONNECT_ATTEMPTS} attempts.\n"
                    "All sessions are terminating. Restart with `summon start`."
                )
            # Raw web_client call — pre-redacted per security constraint C3.
            await self.web_client.chat_postMessage(
                channel=channel_id,
                text=redact_secrets(notice_text),
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
        app.action("permission_approve_session")(self._on_dispatch_action)
        app.action("permission_deny")(self._on_dispatch_action)
        app.action(_ASK_USER_PATTERN)(self._on_dispatch_action)
        app.action("turn_overflow")(self._on_dispatch_action)
        app.view(re.compile(r"ask_user_other"))(self._on_view_submission)
        app.event("app_home_opened")(self._on_app_home_opened)
        app.event("file_shared")(self._on_file_shared)

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

    async def _on_view_submission(self, ack, view, body) -> None:
        await ack()
        await self._dispatcher.dispatch_view_submission(view, body)

    async def _on_app_home_opened(self, event) -> None:
        user_id: str = event.get("user", "")
        if user_id:
            await self._dispatcher.dispatch_app_home(user_id)

    async def _on_file_shared(self, event) -> None:
        await self._dispatcher.dispatch_file_shared(event)
