"""Tests for summon_claude.slack.bolt — BoltRouter, _HealthMonitor, _RateLimiter."""

from __future__ import annotations

import asyncio
import signal
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.config import SummonConfig
from summon_claude.slack.bolt import BoltRouter, _HealthMonitor, _RateLimiter


def make_config(**overrides) -> SummonConfig:
    defaults = {
        "slack_bot_token": "xoxb-test-token",
        "slack_app_token": "xapp-test-token",
        "slack_signing_secret": "test-secret",
    }
    defaults.update(overrides)
    return SummonConfig.model_validate(defaults)


def _mock_app() -> MagicMock:
    app = MagicMock()
    app.command = MagicMock(return_value=lambda f: f)
    app.event = MagicMock(return_value=lambda f: f)
    app.action = MagicMock(return_value=lambda f: f)
    return app


def _make_router(config: SummonConfig | None = None, dispatcher=None):
    """Return a BoltRouter with all Slack SDK constructors patched out."""
    cfg = config or make_config()
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

    router._client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
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
        rl._cleanup(max_age=300.0)
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

    async def test_mark_healthy_resets_counter(self):
        monitor, _, on_exhausted = self._make_monitor(connected=False, max_attempts=5)
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.15)
        monitor.mark_healthy()
        monitor._socket_handler.client.is_connected = AsyncMock(return_value=True)
        await asyncio.sleep(0.15)
        monitor.stop()
        await task
        on_exhausted.assert_not_called()

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
        assert router.web_client is router._client

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
        cfg = make_config()
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
            router._client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
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
        router._client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
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
        router._client.chat_postMessage = AsyncMock(return_value={"ok": True})

        await router._on_reconnect_exhausted()
        if router._exhausted_notice_task is not None:
            await router._exhausted_notice_task

        assert router._client.chat_postMessage.await_count == 2

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
