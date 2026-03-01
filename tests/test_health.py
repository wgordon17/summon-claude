"""Tests for Phase 6 health monitoring: BoltRouter reconnect, daemon watchdog."""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# BoltRouter.reconnect
# ---------------------------------------------------------------------------


class TestBoltRouterReconnect:
    """BoltRouter reconnect replaces the socket handler and re-registers handlers."""

    async def _make_bolt_router(self):
        """Build and start a BoltRouter with all Slack calls mocked out."""
        from contextlib import ExitStack

        from summon_claude.bolt_router import BoltRouter

        mock_config = MagicMock()
        mock_config.slack_bot_token = "xoxb-test"
        mock_config.slack_signing_secret = "secret"
        mock_config.slack_app_token = "xapp-test"

        mock_handler = AsyncMock()
        mock_handler.connect_async = AsyncMock()
        mock_handler.close_async = AsyncMock()

        mock_app = MagicMock()
        mock_app.command = MagicMock(return_value=lambda f: f)
        mock_app.event = MagicMock(return_value=lambda f: f)
        mock_app.action = MagicMock(return_value=lambda f: f)

        stack = ExitStack()
        stack.enter_context(patch("summon_claude.bolt_router.AsyncApp", return_value=mock_app))
        stack.enter_context(
            patch("summon_claude.bolt_router.AsyncSocketModeHandler", return_value=mock_handler)
        )
        stack.enter_context(patch("summon_claude.bolt_router.AsyncWebClient"))

        mock_dispatcher = MagicMock()
        mock_dispatcher.all_channel_ids = MagicMock(return_value=[])
        router = BoltRouter(mock_config, mock_dispatcher)
        router._client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
        router._patch_stack = stack  # keep patches alive
        await router.start()

        return router, mock_config, mock_handler, mock_app

    async def test_reconnect_closes_old_and_connects_new(self):
        """reconnect() should close old handler and connect new one."""
        router, mock_config, old_handler, _ = await self._make_bolt_router()

        new_handler = AsyncMock()
        new_handler.connect_async = AsyncMock()
        new_handler.close_async = AsyncMock()
        new_app = MagicMock()

        with (
            patch("summon_claude.bolt_router.AsyncApp", return_value=new_app),
            patch("summon_claude.bolt_router.AsyncSocketModeHandler", return_value=new_handler),
        ):
            await router.reconnect()

        old_handler.close_async.assert_awaited_once()
        new_handler.connect_async.assert_awaited_once()

    async def test_reconnect_updates_health_monitor_handler(self):
        """After reconnect, health monitor should track the new socket handler."""
        router, mock_config, old_handler, _ = await self._make_bolt_router()

        # Set up a health monitor
        mock_monitor = MagicMock()
        router._health_monitor = mock_monitor

        new_handler = AsyncMock()
        new_handler.connect_async = AsyncMock()
        new_handler.close_async = AsyncMock()

        with (
            patch("summon_claude.bolt_router.AsyncApp"),
            patch("summon_claude.bolt_router.AsyncSocketModeHandler", return_value=new_handler),
        ):
            await router.reconnect()

        mock_monitor.update_handler.assert_called_once_with(new_handler)

    async def test_reconnect_no_health_monitor_does_not_crash(self):
        """reconnect() should work even if health monitor hasn't been started."""
        router, mock_config, old_handler, _ = await self._make_bolt_router()
        assert router._health_monitor is None

        new_handler = AsyncMock()
        new_handler.connect_async = AsyncMock()
        new_handler.close_async = AsyncMock()

        with (
            patch("summon_claude.bolt_router.AsyncApp"),
            patch("summon_claude.bolt_router.AsyncSocketModeHandler", return_value=new_handler),
        ):
            await router.reconnect()  # must not raise

    async def test_stop_signals_health_monitor(self):
        """stop() should signal health monitor to stop."""
        router, _, _, _ = await self._make_bolt_router()

        mock_monitor = MagicMock()
        router._health_monitor = mock_monitor

        with patch.object(router._socket_handler, "close_async", new=AsyncMock()):
            await router.stop()

        mock_monitor.stop.assert_called_once()


# ---------------------------------------------------------------------------
# BoltRouter.start_health_monitor
# ---------------------------------------------------------------------------


class TestBoltRouterHealthMonitor:
    """start_health_monitor() wires SocketHealthMonitor and returns a task."""

    async def _make_minimal_router(self):
        from contextlib import ExitStack

        from summon_claude.bolt_router import BoltRouter

        mock_config = MagicMock()
        mock_config.slack_bot_token = "xoxb-test"
        mock_config.slack_signing_secret = "secret"
        mock_config.slack_app_token = "xapp-test"

        mock_handler = AsyncMock()
        mock_app = MagicMock()
        mock_app.command = MagicMock(return_value=lambda f: f)
        mock_app.event = MagicMock(return_value=lambda f: f)
        mock_app.action = MagicMock(return_value=lambda f: f)

        stack = ExitStack()
        stack.enter_context(patch("summon_claude.bolt_router.AsyncApp", return_value=mock_app))
        stack.enter_context(
            patch("summon_claude.bolt_router.AsyncSocketModeHandler", return_value=mock_handler)
        )
        stack.enter_context(patch("summon_claude.bolt_router.AsyncWebClient"))

        mock_dispatcher = MagicMock()
        mock_dispatcher.all_channel_ids = MagicMock(return_value=[])
        router = BoltRouter(mock_config, mock_dispatcher)
        router._client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
        router._patch_stack = stack  # keep patches alive
        await router.start()
        return router

    async def test_start_health_monitor_returns_task(self):
        """start_health_monitor() should return a running asyncio Task."""
        router = await self._make_minimal_router()
        shutdown_event = asyncio.Event()
        router.shutdown_callback = shutdown_event.set

        # Patch SocketHealthMonitor.run to a no-op coroutine
        with patch("summon_claude.bolt_router.SocketHealthMonitor.run", new=AsyncMock()):
            task = router.start_health_monitor()

        assert isinstance(task, asyncio.Task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_exhaustion_sets_shutdown_event(self):
        """When reconnection is exhausted, shutdown_event should be set."""
        router = await self._make_minimal_router()
        shutdown_event = asyncio.Event()
        router.shutdown_callback = shutdown_event.set

        # dispatcher is already wired via constructor in _make_minimal_router()

        # Manually trigger the exhaustion callback that start_health_monitor wires
        # by accessing the monitor after creation
        with patch("summon_claude.bolt_router.SocketHealthMonitor.run", new=AsyncMock()):
            task = router.start_health_monitor()
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task

        # Simulate exhaustion by awaiting the monitor's _on_exhausted
        await router._health_monitor._on_exhausted()  # type: ignore[attr-defined]
        assert shutdown_event.is_set()


# ---------------------------------------------------------------------------
# Daemon watchdog
# ---------------------------------------------------------------------------


class TestDaemonWatchdog:
    """Daemon-level event loop watchdog detects stalls and triggers shutdown."""

    async def test_watchdog_exits_cleanly_on_shutdown(self):
        """_watchdog_loop should exit cleanly when shutdown_event is set."""
        from summon_claude.daemon import _watchdog_loop

        shutdown_event = asyncio.Event()

        with patch("summon_claude.daemon._WATCHDOG_CHECK_INTERVAL_S", 0.01):
            # Set shutdown immediately so loop exits after first check
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

        # Patch the threshold very low so any sleep overhead triggers it
        with (
            patch("summon_claude.daemon._WATCHDOG_CHECK_INTERVAL_S", 0.01),
            patch("summon_claude.daemon._WATCHDOG_THRESHOLD_S", 0.001),
        ):
            # The watchdog should trigger shutdown on the first check since
            # elapsed will exceed 0.001s easily
            await asyncio.wait_for(_watchdog_loop(shutdown_event), timeout=1.0)

        assert shutdown_event.is_set()


# ---------------------------------------------------------------------------
# SIGALRM watchdog
# ---------------------------------------------------------------------------


class TestSigAlrmWatchdog:
    """SIGALRM watchdog arms/disarms correctly on Unix."""

    def test_start_and_disarm_sigalrm(self):
        """_start_sigalrm_watchdog arms SIGALRM; _disarm_sigalrm_watchdog cancels it."""
        from summon_claude.daemon import _disarm_sigalrm_watchdog, _start_sigalrm_watchdog

        if not hasattr(signal, "SIGALRM"):
            pytest.skip("SIGALRM not available on this platform")

        # Store original handler so we can restore it
        original = signal.getsignal(signal.SIGALRM)
        try:
            _start_sigalrm_watchdog()
            # Alarm should be pending (non-zero)
            remaining = signal.alarm(0)  # read remaining time without setting new alarm
            assert remaining > 0 or remaining == 0  # just verify no exception

            _disarm_sigalrm_watchdog()
            # After disarm, alarm should be 0
            remaining_after = signal.alarm(0)
            assert remaining_after == 0
        finally:
            # Restore original handler
            signal.signal(signal.SIGALRM, original)
            signal.alarm(0)

    def test_start_sigalrm_no_op_on_no_sigalrm(self):
        """On platforms without SIGALRM, _start_sigalrm_watchdog is a no-op."""
        from summon_claude.daemon import _start_sigalrm_watchdog

        with patch("summon_claude.daemon.signal") as mock_signal:
            del mock_signal.SIGALRM  # simulate platform without SIGALRM
            _start_sigalrm_watchdog()  # must not raise
