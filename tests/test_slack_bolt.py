"""Tests for summon_claude.slack.bolt — BoltRouter, _HealthMonitor, _RateLimiter."""

from __future__ import annotations

import asyncio
import signal
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_test_config

from summon_claude.config import SummonConfig
from summon_claude.event_dispatcher import EventDispatcher
from summon_claude.slack.bolt import (
    BoltRouter,
    DiagnosticResult,
    EventProbe,
    _HealthMonitor,
    _RateLimiter,
)


def _mock_app() -> MagicMock:
    app = MagicMock()
    app.command = MagicMock(return_value=lambda f: f)
    app.event = MagicMock(return_value=lambda f: f)
    app.action = MagicMock(return_value=lambda f: f)
    return app


def _make_router(config: SummonConfig | None = None, dispatcher=None):
    """Return a BoltRouter with all Slack SDK constructors patched out."""
    cfg = config or make_test_config()
    if dispatcher is None:
        dispatcher = MagicMock()
        dispatcher.dispatch_message = AsyncMock()
        dispatcher.dispatch_reaction = AsyncMock()
        dispatcher.dispatch_action = AsyncMock()
        dispatcher.dispatch_command = AsyncMock()
        dispatcher.all_channel_ids = MagicMock(return_value=[])

    stack = ExitStack()
    patched_app_cls = stack.enter_context(patch("summon_claude.slack.bolt.AsyncApp"))
    stack.enter_context(patch("summon_claude.slack.bolt.AsyncWebClient"))
    patched_handler_cls = stack.enter_context(
        patch("summon_claude.slack.bolt.AsyncSocketModeHandler")
    )

    mock_a = _mock_app()
    patched_app_cls.return_value = mock_a

    mock_h = AsyncMock()
    mock_h.connect_async = AsyncMock()
    mock_h.close_async = AsyncMock()
    patched_handler_cls.return_value = mock_h

    router = BoltRouter(cfg, dispatcher)
    router._patch_stack = stack  # type: ignore[attr-defined]
    router._mock_app_factory = mock_a  # type: ignore[attr-defined]
    router._mock_handler_factory = mock_h  # type: ignore[attr-defined]
    router._mock_dispatcher = dispatcher  # type: ignore[attr-defined]

    router.web_client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
    return router


async def _make_started_router(config: SummonConfig | None = None):
    router = _make_router(config)
    await router.start()
    return router


# ---------------------------------------------------------------------------
# _RateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_first_request_allowed(self):
        rl = _RateLimiter(cooldown_seconds=2.0)
        assert rl.check("user1") is True

    def test_second_request_within_cooldown_denied(self):
        rl = _RateLimiter(cooldown_seconds=2.0)
        rl.check("user1")
        assert rl.check("user1") is False

    def test_different_keys_are_independent(self):
        rl = _RateLimiter(cooldown_seconds=2.0)
        rl.check("user1")
        assert rl.check("user2") is True

    def test_cleanup_removes_old_entries(self):
        import time

        rl = _RateLimiter(cooldown_seconds=2.0)
        rl._last_attempt["old-user"] = time.monotonic() - 400
        rl.check("user1")
        rl._cleanup()
        assert "old-user" not in rl._last_attempt
        assert "user1" in rl._last_attempt


# ---------------------------------------------------------------------------
# _HealthMonitor
# ---------------------------------------------------------------------------


class TestHealthMonitor:
    def _make_monitor(self, connected=True, max_attempts=3, interval=0.05):
        mock_handler = MagicMock()
        mock_handler.client = MagicMock()
        mock_handler.client.is_connected = AsyncMock(return_value=connected)
        on_reconnect = AsyncMock()
        on_exhausted = AsyncMock()
        monitor = _HealthMonitor(
            socket_handler=mock_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            check_interval=interval,
            max_reconnect_attempts=max_attempts,
        )
        return monitor, on_reconnect, on_exhausted

    async def test_healthy_connection_no_action(self):
        monitor, on_reconnect, on_exhausted = self._make_monitor(connected=True)
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.1)
        monitor.stop()
        await task
        on_reconnect.assert_not_called()
        on_exhausted.assert_not_called()

    async def test_unhealthy_triggers_reconnect(self):
        monitor, on_reconnect, on_exhausted = self._make_monitor(connected=False)
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.1)
        monitor.stop()
        await task
        assert on_reconnect.call_count >= 1
        on_exhausted.assert_not_called()

    async def test_max_attempts_exhausted(self):
        monitor, on_reconnect, on_exhausted = self._make_monitor(connected=False, max_attempts=2)
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.3)
        await asyncio.wait_for(task, timeout=1.0)
        assert on_reconnect.call_count >= 2
        on_exhausted.assert_called_once()

    async def test_stop_ends_loop(self):
        monitor, _, _ = self._make_monitor(connected=True)
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.05)
        monitor.stop()
        await asyncio.wait_for(task, timeout=1.0)
        assert task.done()

    async def test_update_handler_switches_client(self):
        old_handler = MagicMock()
        old_handler.client = MagicMock()
        old_handler.client.is_connected = AsyncMock(return_value=False)
        new_handler = MagicMock()
        new_handler.client = MagicMock()
        new_handler.client.is_connected = AsyncMock(return_value=True)
        on_reconnect = AsyncMock()
        on_exhausted = AsyncMock()
        monitor = _HealthMonitor(
            socket_handler=old_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            check_interval=0.05,
            max_reconnect_attempts=3,
        )
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.1)
        monitor.update_handler(new_handler)
        assert monitor._socket_handler is new_handler
        assert monitor._consecutive_failures == 0
        await asyncio.sleep(0.1)
        monitor.stop()
        await task
        on_exhausted.assert_not_called()


# ---------------------------------------------------------------------------
# BoltRouter init + lifecycle
# ---------------------------------------------------------------------------


class TestBoltRouterInit:
    def test_web_client_exposed(self):
        router = _make_router()
        assert router.web_client is not None

    def test_no_provider_attribute(self):
        router = _make_router()
        assert not hasattr(router, "provider")

    def test_dispatcher_stored(self):
        mock_dispatcher = MagicMock()
        router = _make_router(dispatcher=mock_dispatcher)
        assert router._dispatcher is mock_dispatcher

    def test_app_is_none_before_start(self):
        router = _make_router()
        assert router._app is None

    async def test_handlers_registered_at_start(self):
        router = _make_router()
        await router.start()
        app = router._app
        assert app is not None
        app.command.assert_called_with("/summon")
        assert app.event.call_count >= 2
        assert app.action.call_count >= 3


class TestBoltRouterLifecycle:
    async def test_start_calls_connect_async(self):
        router = _make_router()
        await router.start()
        router._socket_handler.connect_async.assert_awaited_once()

    async def test_stop_calls_close_async(self):
        router = _make_router()
        await router.start()
        router._socket_handler.close_async.reset_mock()
        await router.stop()
        router._socket_handler.close_async.assert_awaited_once()

    async def test_stop_tolerates_close_error(self):
        router = _make_router()
        await router.start()
        router._socket_handler.close_async.side_effect = RuntimeError("already closed")
        await router.stop()  # must not raise

    async def test_stop_before_start_is_safe(self):
        router = _make_router()
        await router.stop()  # must not raise


class TestBoltRouterReconnect:
    async def test_reconnect_creates_new_app(self):
        cfg = make_test_config()
        mock_a1, mock_a2 = _mock_app(), _mock_app()
        mock_h1, mock_h2 = AsyncMock(), AsyncMock()
        mock_dispatcher = MagicMock()

        with (
            patch("summon_claude.slack.bolt.AsyncApp") as patched_app_cls,
            patch("summon_claude.slack.bolt.AsyncWebClient"),
            patch("summon_claude.slack.bolt.AsyncSocketModeHandler") as patched_handler_cls,
        ):
            patched_app_cls.side_effect = [mock_a1, mock_a2]
            patched_handler_cls.side_effect = [mock_h1, mock_h2]

            router = BoltRouter(cfg, mock_dispatcher)
            router.web_client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
            await router.start()
            assert router._app is mock_a1

            await router.reconnect()

            mock_h1.close_async.assert_awaited_once()
            assert router._app is mock_a2
            mock_h2.connect_async.assert_awaited_once()

    async def test_reconnect_updates_health_monitor(self):
        router = _make_router()
        await router.start()
        mock_monitor = MagicMock()
        router._health_monitor = mock_monitor

        new_h = AsyncMock()
        new_h.connect_async = AsyncMock()
        new_h.close_async = AsyncMock()

        with (
            patch("summon_claude.slack.bolt.AsyncApp"),
            patch("summon_claude.slack.bolt.AsyncSocketModeHandler", return_value=new_h),
        ):
            await router.reconnect()

        mock_monitor.update_handler.assert_called_once_with(new_h)

    async def test_reconnect_no_health_monitor_no_crash(self):
        router = _make_router()
        await router.start()
        assert router._health_monitor is None
        new_h = AsyncMock()
        new_h.connect_async = AsyncMock()
        with (
            patch("summon_claude.slack.bolt.AsyncApp"),
            patch("summon_claude.slack.bolt.AsyncSocketModeHandler", return_value=new_h),
        ):
            await router.reconnect()  # must not raise


# ---------------------------------------------------------------------------
# BoltRouter handlers
# ---------------------------------------------------------------------------


class TestSummonCommandHandler:
    async def _invoke_summon(self, router, command: dict) -> dict:
        registered_fn = None

        def capture_command(path):
            def decorator(fn):
                nonlocal registered_fn
                if path == "/summon":
                    registered_fn = fn
                return fn

            return decorator

        mock_a = MagicMock()
        mock_a.command = capture_command
        mock_a.event = MagicMock(return_value=lambda f: f)
        mock_a.action = MagicMock(return_value=lambda f: f)
        router._register_handlers(mock_a)

        assert registered_fn is not None
        ack = AsyncMock()
        respond = AsyncMock()
        await registered_fn(ack=ack, command=command, respond=respond)
        return {"ack": ack, "respond": respond}

    async def test_summon_acks_immediately(self):
        router = _make_router()
        result = await self._invoke_summon(router, {"user_id": "U001", "text": "abc123"})
        result["ack"].assert_awaited_once()

    async def test_summon_empty_text_responds_with_usage(self):
        router = _make_router()
        result = await self._invoke_summon(router, {"user_id": "U001", "text": ""})
        text = result["respond"].call_args.kwargs.get("text", "")
        assert "Usage" in text

    async def test_summon_delegates_to_dispatcher(self):
        router = _make_router()
        result = await self._invoke_summon(router, {"user_id": "U001", "text": "mycode"})
        router._mock_dispatcher.dispatch_command.assert_awaited_once_with(
            user_id="U001",
            code="mycode",
            respond=result["respond"],
        )

    async def test_summon_rate_limited_gets_cooldown_message(self):
        router = _make_router()
        router._rate_limiter.check("U001")
        result = await self._invoke_summon(router, {"user_id": "U001", "text": "code"})
        text = result["respond"].call_args.kwargs.get("text", "")
        assert "wait" in text.lower()


class TestMessageRouting:
    def _extract_event_handler(self, router, event_type: str):
        captured: dict = {}

        def capture_event(ev_type):
            def decorator(fn):
                captured[ev_type] = fn
                return fn

            return decorator

        mock_a = MagicMock()
        mock_a.command = MagicMock(return_value=lambda f: f)
        mock_a.event = capture_event
        mock_a.action = MagicMock(return_value=lambda f: f)
        router._register_handlers(mock_a)
        return captured.get(event_type)

    async def test_message_event_routes_to_dispatcher(self):
        router = _make_router()
        handler = self._extract_event_handler(router, "message")
        event = {"type": "message", "channel": "C001", "text": "hello"}
        await handler(event=event, say=AsyncMock())
        router._mock_dispatcher.dispatch_message.assert_awaited_once_with(event)

    async def test_reaction_added_routes_to_dispatcher(self):
        router = _make_router()
        handler = self._extract_event_handler(router, "reaction_added")
        event = {"type": "reaction_added", "item": {"channel": "C001"}}
        await handler(event=event)
        router._mock_dispatcher.dispatch_reaction.assert_awaited_once_with(event)


# ---------------------------------------------------------------------------
# BoltRouter.start_health_monitor
# ---------------------------------------------------------------------------


class TestBoltRouterHealthMonitor:
    async def _make_minimal_router(self):
        mock_config = MagicMock()
        mock_config.slack_bot_token = "xoxb-test"
        mock_config.slack_signing_secret = "secret"
        mock_config.slack_app_token = "xapp-test"

        mock_handler = AsyncMock()
        mock_app = _mock_app()

        stack = ExitStack()
        stack.enter_context(patch("summon_claude.slack.bolt.AsyncApp", return_value=mock_app))
        stack.enter_context(
            patch("summon_claude.slack.bolt.AsyncSocketModeHandler", return_value=mock_handler)
        )
        stack.enter_context(patch("summon_claude.slack.bolt.AsyncWebClient"))

        mock_dispatcher = MagicMock()
        mock_dispatcher.all_channel_ids = MagicMock(return_value=[])
        router = BoltRouter(mock_config, mock_dispatcher)
        router.web_client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
        router._patch_stack = stack
        await router.start()
        return router

    async def test_start_health_monitor_returns_task(self):
        router = await self._make_minimal_router()
        shutdown_event = asyncio.Event()
        router.shutdown_callback = shutdown_event.set

        with patch("summon_claude.slack.bolt._HealthMonitor.run", new=AsyncMock()):
            task = router.start_health_monitor()

        assert isinstance(task, asyncio.Task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_exhaustion_triggers_shutdown_callback(self):
        router = await self._make_minimal_router()
        shutdown_event = asyncio.Event()
        router.shutdown_callback = shutdown_event.set

        with patch("summon_claude.slack.bolt._HealthMonitor.run", new=AsyncMock()):
            task = router.start_health_monitor()
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task

        await router._health_monitor._on_exhausted()
        assert shutdown_event.is_set()

    async def test_exhaustion_posts_to_all_channels(self):
        router = _make_router()
        router.shutdown_callback = MagicMock()
        router._mock_dispatcher.all_channel_ids.return_value = ["C001", "C002"]
        router.web_client.chat_postMessage = AsyncMock(return_value={"ok": True})

        await router._on_reconnect_exhausted()
        if router._exhausted_notice_task is not None:
            await router._exhausted_notice_task

        assert router.web_client.chat_postMessage.await_count == 2

    async def test_no_channels_does_not_crash(self):
        router = _make_router()
        router.shutdown_callback = MagicMock()
        router._mock_dispatcher.all_channel_ids.return_value = []
        await router._on_reconnect_exhausted()  # must not raise

    async def test_warns_when_no_callback(self):
        router = _make_router()
        assert router.shutdown_callback is None
        await router._on_reconnect_exhausted()  # must not raise


# ---------------------------------------------------------------------------
# EventDispatcher.has_active_sessions
# ---------------------------------------------------------------------------


class TestHasActiveSessions:
    def test_empty_dispatcher_returns_false(self):
        dispatcher = EventDispatcher()
        assert dispatcher.has_active_sessions() is False

    def test_dispatcher_with_sessions_returns_true(self):
        dispatcher = EventDispatcher()
        handle = MagicMock()
        dispatcher.register("C001", handle)
        assert dispatcher.has_active_sessions() is True

    def test_dispatcher_after_unregister_returns_false(self):
        dispatcher = EventDispatcher()
        handle = MagicMock()
        dispatcher.register("C001", handle)
        dispatcher.unregister("C001")
        assert dispatcher.has_active_sessions() is False


# ---------------------------------------------------------------------------
# _HealthMonitor — EventProbe integration
# ---------------------------------------------------------------------------


class TestHealthMonitorWithProbe:
    def _make_monitor_with_probe(
        self,
        probe: EventProbe | None = None,
        dispatcher_has_sessions: bool = True,
        connected: bool = True,
    ):
        mock_handler = MagicMock()
        mock_handler.client = MagicMock()
        mock_handler.client.is_connected = AsyncMock(return_value=connected)
        on_reconnect = AsyncMock()
        on_exhausted = AsyncMock()
        mock_dispatcher = MagicMock()
        mock_dispatcher.has_active_sessions = MagicMock(return_value=dispatcher_has_sessions)
        monitor = _HealthMonitor(
            socket_handler=mock_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            check_interval=0.05,
            max_reconnect_attempts=3,
            event_probe=probe,
            dispatcher=mock_dispatcher,
        )
        return monitor, on_reconnect, on_exhausted

    async def test_skips_probe_when_no_sessions(self):
        mock_probe = MagicMock(spec=EventProbe)
        mock_probe.run_probe = AsyncMock()
        monitor, _, _ = self._make_monitor_with_probe(
            probe=mock_probe, dispatcher_has_sessions=False
        )
        result = await monitor._is_healthy()
        assert result is True
        mock_probe.run_probe.assert_not_awaited()

    async def test_runs_probe_when_sessions_active(self):
        mock_probe = MagicMock(spec=EventProbe)
        mock_probe.run_probe = AsyncMock(
            return_value=DiagnosticResult(healthy=True, reason="healthy", details="OK")
        )
        monitor, _, _ = self._make_monitor_with_probe(probe=mock_probe)
        result = await monitor._is_healthy()
        assert result is True
        mock_probe.run_probe.assert_awaited_once()

    async def test_probe_failure_marks_unhealthy(self):
        mock_probe = MagicMock(spec=EventProbe)
        mock_probe.run_probe = AsyncMock(
            return_value=DiagnosticResult(
                healthy=False,
                reason="events_disabled",
                details="Events not delivered.",
            )
        )
        monitor, _, _ = self._make_monitor_with_probe(probe=mock_probe)
        result = await monitor._is_healthy()
        assert result is False
        assert monitor._last_diagnostic is not None
        assert monitor._last_diagnostic.reason == "events_disabled"

    async def test_link_disabled_triggers_immediate_exhaustion(self):
        on_exhausted = AsyncMock()
        mock_handler = MagicMock()
        mock_handler.client.is_connected = AsyncMock(return_value=True)
        on_reconnect = AsyncMock()
        monitor = _HealthMonitor(
            socket_handler=mock_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            max_reconnect_attempts=3,
        )
        monitor._last_diagnostic = DiagnosticResult(
            healthy=False,
            reason="socket_disabled",
            details="Socket Mode was disabled.",
        )
        await monitor._handle_unhealthy()
        on_exhausted.assert_awaited_once()
        on_reconnect.assert_not_awaited()

    async def test_events_disabled_requires_3_consecutive_failures(self):
        on_exhausted = AsyncMock()
        mock_handler = MagicMock()
        mock_handler.client.is_connected = AsyncMock(return_value=True)
        on_reconnect = AsyncMock()
        monitor = _HealthMonitor(
            socket_handler=mock_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            max_reconnect_attempts=3,
        )
        monitor._last_diagnostic = DiagnosticResult(
            healthy=False, reason="events_disabled", details="Events not delivered."
        )
        await monitor._handle_unhealthy()
        on_exhausted.assert_not_awaited()
        await monitor._handle_unhealthy()
        on_exhausted.assert_not_awaited()
        await monitor._handle_unhealthy()
        on_exhausted.assert_awaited_once()

    async def test_probe_cancelled_during_reconnect_returns_healthy(self):
        mock_probe = MagicMock(spec=EventProbe)
        mock_probe.run_probe = AsyncMock(
            return_value=DiagnosticResult(
                healthy=True, reason="cancelled", details="Probe cancelled."
            )
        )
        monitor, _, _ = self._make_monitor_with_probe(probe=mock_probe)
        result = await monitor._is_healthy()
        assert result is True

    async def test_probe_exception_returns_healthy(self):
        """Probe exception should not mark socket as unhealthy."""
        mock_probe = MagicMock(spec=EventProbe)
        mock_probe.run_probe = AsyncMock(side_effect=RuntimeError("probe crash"))
        monitor, _, _ = self._make_monitor_with_probe(probe=mock_probe)
        result = await monitor._is_healthy()
        assert result is True

    async def test_slack_down_triggers_reconnect_not_exhaustion(self):
        """slack_down should use reconnect logic, not immediate exhaustion."""
        on_exhausted = AsyncMock()
        on_reconnect = AsyncMock()
        mock_handler = MagicMock()
        mock_handler.client.is_connected = AsyncMock(return_value=True)
        monitor = _HealthMonitor(
            socket_handler=mock_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            max_reconnect_attempts=3,
        )
        monitor._last_diagnostic = DiagnosticResult(
            healthy=False,
            reason="slack_down",
            details="Slack unreachable.",
        )
        await monitor._handle_unhealthy()
        on_reconnect.assert_awaited_once()
        on_exhausted.assert_not_awaited()
        assert monitor._consecutive_probe_failures == 0

    async def test_socket_disconnect_clears_stale_diagnostic(self):
        """Socket disconnect must clear stale probe diagnostic to prevent misclassification."""
        mock_probe = MagicMock(spec=EventProbe)
        mock_probe.run_probe = AsyncMock(
            return_value=DiagnosticResult(
                healthy=False, reason="events_disabled", details="Events not delivered."
            )
        )
        monitor, on_reconnect, on_exhausted = self._make_monitor_with_probe(probe=mock_probe)

        # First: probe fails → _last_diagnostic set
        result = await monitor._is_healthy()
        assert result is False
        assert monitor._last_diagnostic is not None
        assert monitor._last_diagnostic.reason == "events_disabled"

        # Now: socket disconnects → _last_diagnostic must be cleared
        monitor._socket_handler.client.is_connected = AsyncMock(return_value=False)
        result = await monitor._is_healthy()
        assert result is False
        assert monitor._last_diagnostic is None  # cleared by socket disconnect

        # _handle_unhealthy should use socket reconnect path, not probe failure path
        await monitor._handle_unhealthy()
        on_reconnect.assert_awaited_once()  # socket reconnect, not probe exhaustion


# ---------------------------------------------------------------------------
# event_failure_callback
# ---------------------------------------------------------------------------


class TestEventFailureCallback:
    async def test_event_failure_callback_fires_on_event_pipeline_failure(self):
        """event_failure_callback fires when diagnostic is events_disabled."""
        cfg = make_test_config()
        mock_dispatcher = MagicMock()
        mock_dispatcher.all_channel_ids = MagicMock(return_value=[])

        with (
            patch("summon_claude.slack.bolt.AsyncApp") as patched_app_cls,
            patch("summon_claude.slack.bolt.AsyncWebClient"),
            patch("summon_claude.slack.bolt.AsyncSocketModeHandler") as patched_handler_cls,
        ):
            patched_app_cls.return_value = _mock_app()
            mock_handler = AsyncMock()
            mock_handler.client = MagicMock()
            mock_handler.client.on_message_listeners = []
            patched_handler_cls.return_value = mock_handler

            router = BoltRouter(cfg, mock_dispatcher)
            router.web_client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})

            mock_probe = MagicMock(spec=EventProbe)
            mock_probe.setup_anchor = AsyncMock()
            mock_probe.on_ws_message = AsyncMock()

            with patch("summon_claude.slack.bolt.EventProbe", return_value=mock_probe):
                await router.start()

            # Set up health monitor with a diagnostic
            router._health_monitor = MagicMock()
            router._health_monitor.last_diagnostic = DiagnosticResult(
                healthy=False,
                reason="events_disabled",
                details="Events not delivered.",
            )

            callback = MagicMock()
            router.event_failure_callback = callback
            router.shutdown_callback = MagicMock()

            await router._on_reconnect_exhausted()

            callback.assert_called_once()

    async def test_event_failure_callback_not_fired_on_slack_down(self):
        """event_failure_callback should NOT fire when diagnostic is slack_down."""
        cfg = make_test_config()
        mock_dispatcher = MagicMock()
        mock_dispatcher.all_channel_ids = MagicMock(return_value=[])

        with (
            patch("summon_claude.slack.bolt.AsyncApp") as patched_app_cls,
            patch("summon_claude.slack.bolt.AsyncWebClient"),
            patch("summon_claude.slack.bolt.AsyncSocketModeHandler") as patched_handler_cls,
        ):
            patched_app_cls.return_value = _mock_app()
            mock_handler = AsyncMock()
            mock_handler.client = MagicMock()
            mock_handler.client.on_message_listeners = []
            patched_handler_cls.return_value = mock_handler

            router = BoltRouter(cfg, mock_dispatcher)
            router.web_client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})

            mock_probe = MagicMock(spec=EventProbe)
            mock_probe.setup_anchor = AsyncMock()
            mock_probe.on_ws_message = AsyncMock()

            with patch("summon_claude.slack.bolt.EventProbe", return_value=mock_probe):
                await router.start()

            router._health_monitor = MagicMock()
            router._health_monitor.last_diagnostic = DiagnosticResult(
                healthy=False,
                reason="slack_down",
                details="Slack unreachable.",
            )

            callback = MagicMock()
            router.event_failure_callback = callback
            router.shutdown_callback = MagicMock()

            await router._on_reconnect_exhausted()

            callback.assert_not_called()


# ---------------------------------------------------------------------------
# BoltRouter reconnect — WS listener re-registration
# ---------------------------------------------------------------------------


class TestBoltRouterReconnectWithProbe:
    async def test_reconnect_re_registers_ws_listener(self):
        cfg = make_test_config()
        mock_a1, mock_a2 = _mock_app(), _mock_app()
        mock_h1, mock_h2 = AsyncMock(), AsyncMock()
        mock_h1.client = MagicMock()
        mock_h1.client.on_message_listeners = []
        mock_h2.client = MagicMock()
        mock_h2.client.on_message_listeners = []
        mock_dispatcher = MagicMock()
        mock_dispatcher.all_channel_ids = MagicMock(return_value=[])

        with (
            patch("summon_claude.slack.bolt.AsyncApp") as patched_app_cls,
            patch("summon_claude.slack.bolt.AsyncWebClient"),
            patch("summon_claude.slack.bolt.AsyncSocketModeHandler") as patched_handler_cls,
        ):
            patched_app_cls.side_effect = [mock_a1, mock_a2]
            patched_handler_cls.side_effect = [mock_h1, mock_h2]

            router = BoltRouter(cfg, mock_dispatcher)
            router.web_client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})

            mock_probe = MagicMock(spec=EventProbe)
            mock_probe.setup_anchor = AsyncMock()
            mock_probe.on_ws_message = AsyncMock()
            mock_probe._probe_cancelled = False

            with patch("summon_claude.slack.bolt.EventProbe", return_value=mock_probe):
                await router.start()

            assert mock_probe.on_ws_message in mock_h1.client.on_message_listeners

            with patch("summon_claude.slack.bolt.EventProbe", return_value=mock_probe):
                await router.reconnect()

            assert mock_probe.on_ws_message in mock_h2.client.on_message_listeners


# ---------------------------------------------------------------------------
# Daemon watchdog (absorbed from test_health.py)
# ---------------------------------------------------------------------------


class TestDaemonWatchdog:
    """Daemon-level event loop watchdog detects stalls and triggers shutdown."""

    async def test_watchdog_exits_cleanly_on_shutdown(self):
        """_watchdog_loop should exit cleanly when shutdown_event is set."""
        from summon_claude.daemon import _watchdog_loop

        shutdown_event = asyncio.Event()

        with patch("summon_claude.daemon._WATCHDOG_CHECK_INTERVAL_S", 0.01):

            async def _trigger_shutdown():
                await asyncio.sleep(0.02)
                shutdown_event.set()

            trigger = asyncio.create_task(_trigger_shutdown())
            await asyncio.wait_for(_watchdog_loop(shutdown_event), timeout=1.0)
            trigger.cancel()

    async def test_watchdog_detects_long_sleep(self):
        """If elapsed time greatly exceeds check interval, watchdog sets shutdown."""
        from summon_claude.daemon import _watchdog_loop

        shutdown_event = asyncio.Event()

        with (
            patch("summon_claude.daemon._WATCHDOG_CHECK_INTERVAL_S", 0.01),
            patch("summon_claude.daemon._WATCHDOG_THRESHOLD_S", 0.001),
        ):
            await asyncio.wait_for(_watchdog_loop(shutdown_event), timeout=1.0)

        assert shutdown_event.is_set()


# ---------------------------------------------------------------------------
# SIGALRM watchdog (absorbed from test_health.py)
# ---------------------------------------------------------------------------


class TestSigAlrmWatchdog:
    """SIGALRM watchdog arms/disarms correctly on Unix."""

    def test_start_and_disarm_sigalrm(self):
        """_start_sigalrm_watchdog arms SIGALRM; _disarm_sigalrm_watchdog cancels it."""
        from summon_claude.daemon import _disarm_sigalrm_watchdog, _start_sigalrm_watchdog

        if not hasattr(signal, "SIGALRM"):
            pytest.skip("SIGALRM not available on this platform")

        original = signal.getsignal(signal.SIGALRM)
        try:
            _start_sigalrm_watchdog()
            remaining = signal.alarm(0)
            assert remaining > 0 or remaining == 0  # no exception means success

            _disarm_sigalrm_watchdog()
            remaining_after = signal.alarm(0)
            assert remaining_after == 0
        finally:
            signal.signal(signal.SIGALRM, original)
            signal.alarm(0)

    def test_start_sigalrm_no_op_on_no_sigalrm(self):
        """On platforms without SIGALRM, _start_sigalrm_watchdog is a no-op."""
        from summon_claude.daemon import _start_sigalrm_watchdog

        with patch("summon_claude.daemon.signal") as mock_signal:
            del mock_signal.SIGALRM
            _start_sigalrm_watchdog()  # must not raise
