"""Tests for socket_health.py — socket resilience monitoring."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from summon_claude.socket_health import SocketHealthMonitor


class TestSocketHealthMonitor:
    """Test the SocketHealthMonitor class."""

    async def test_healthy_connection_no_action(self):
        """When is_connected returns True, no callbacks should be called."""
        mock_socket_handler = MagicMock()
        mock_socket_handler.client = MagicMock()
        mock_socket_handler.client.is_connected = AsyncMock(return_value=True)

        on_reconnect = AsyncMock()
        on_exhausted = AsyncMock()

        monitor = SocketHealthMonitor(
            socket_handler=mock_socket_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            check_interval=0.05,
            max_reconnect_attempts=3,
        )

        # Run one check cycle
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.1)
        monitor.stop()
        await task

        # No callbacks should have been triggered
        on_reconnect.assert_not_called()
        on_exhausted.assert_not_called()

    async def test_unhealthy_triggers_reconnect(self):
        """When is_connected returns False, on_reconnect_needed should be called."""
        mock_socket_handler = MagicMock()
        mock_socket_handler.client = MagicMock()
        mock_socket_handler.client.is_connected = AsyncMock(return_value=False)

        on_reconnect = AsyncMock()
        on_exhausted = AsyncMock()

        monitor = SocketHealthMonitor(
            socket_handler=mock_socket_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            check_interval=0.05,
            max_reconnect_attempts=3,
        )

        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.1)
        monitor.stop()
        await task

        # Reconnect should have been triggered at least once
        assert on_reconnect.call_count >= 1
        on_exhausted.assert_not_called()

    async def test_max_attempts_exhausted(self):
        """After max_reconnect_attempts failures, on_exhausted should be called."""
        mock_socket_handler = MagicMock()
        mock_socket_handler.client = MagicMock()
        mock_socket_handler.client.is_connected = AsyncMock(return_value=False)

        on_reconnect = AsyncMock()
        on_exhausted = AsyncMock()

        monitor = SocketHealthMonitor(
            socket_handler=mock_socket_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            check_interval=0.05,
            max_reconnect_attempts=2,
        )

        task = asyncio.create_task(monitor.run())
        # Wait enough time for multiple check cycles to exhaust attempts
        await asyncio.sleep(0.3)
        # Monitor should stop itself after exhaustion
        await asyncio.wait_for(task, timeout=1.0)

        # Should have tried reconnect max_reconnect_attempts times, then called on_exhausted
        assert on_reconnect.call_count >= 2
        on_exhausted.assert_called_once()

    async def test_mark_healthy_resets_counter(self):
        """mark_healthy() should reset the consecutive failure counter."""
        mock_socket_handler = MagicMock()
        mock_socket_handler.client = MagicMock()
        mock_socket_handler.client.is_connected = AsyncMock(return_value=False)

        on_reconnect = AsyncMock()
        on_exhausted = AsyncMock()

        monitor = SocketHealthMonitor(
            socket_handler=mock_socket_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            check_interval=0.05,
            max_reconnect_attempts=5,
        )

        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.15)  # Allow some failures to accumulate

        # Mark as healthy, resetting counter
        monitor.mark_healthy()

        # Now switch to healthy connection
        mock_socket_handler.client.is_connected = AsyncMock(return_value=True)

        await asyncio.sleep(0.15)
        monitor.stop()
        await task

        # Should not have exhausted because counter was reset
        on_exhausted.assert_not_called()

    async def test_update_handler_switches_client(self):
        """update_handler() should switch to new handler and reset counter."""
        old_handler = MagicMock()
        old_handler.client = MagicMock()
        old_handler.client.is_connected = AsyncMock(return_value=False)

        new_handler = MagicMock()
        new_handler.client = MagicMock()
        new_handler.client.is_connected = AsyncMock(return_value=True)

        on_reconnect = AsyncMock()
        on_exhausted = AsyncMock()

        monitor = SocketHealthMonitor(
            socket_handler=old_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            check_interval=0.05,
            max_reconnect_attempts=3,
        )

        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.1)

        # Update to new handler
        monitor.update_handler(new_handler)
        assert monitor._socket_handler is new_handler
        assert monitor._consecutive_failures == 0

        await asyncio.sleep(0.1)
        monitor.stop()
        await task

        # Should not have exhausted because new handler is healthy
        on_exhausted.assert_not_called()

    async def test_stop_ends_loop(self):
        """stop() should cause the run() loop to exit."""
        mock_socket_handler = MagicMock()
        mock_socket_handler.client = MagicMock()
        mock_socket_handler.client.is_connected = AsyncMock(return_value=True)

        on_reconnect = AsyncMock()
        on_exhausted = AsyncMock()

        monitor = SocketHealthMonitor(
            socket_handler=mock_socket_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            check_interval=0.05,
        )

        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.05)
        assert not task.done()

        monitor.stop()
        # Should exit cleanly
        await asyncio.wait_for(task, timeout=1.0)
        assert task.done()

    async def test_healthy_after_reconnect(self):
        """After reconnect brings health back, counter should stay reset."""
        mock_socket_handler = MagicMock()
        mock_socket_handler.client = MagicMock()

        # Start unhealthy, then recover after 2 failures
        connection_states = [False, False, True, True, True]
        call_count = [0]

        async def is_connected_side_effect():
            idx = min(call_count[0], len(connection_states) - 1)
            result = connection_states[idx]
            call_count[0] += 1
            return result

        mock_socket_handler.client.is_connected = AsyncMock(side_effect=is_connected_side_effect)

        on_reconnect = AsyncMock()
        on_exhausted = AsyncMock()

        monitor = SocketHealthMonitor(
            socket_handler=mock_socket_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            check_interval=0.05,
            max_reconnect_attempts=5,
        )

        task = asyncio.create_task(monitor.run())
        # Allow enough cycles to see the state changes
        await asyncio.sleep(0.3)
        monitor.stop()
        await task

        # Should have attempted reconnect but not exhausted
        assert on_reconnect.call_count >= 1
        on_exhausted.assert_not_called()

    async def test_reconnect_exception_increments_counter(self):
        """If on_reconnect_needed raises, counter should still increment."""
        mock_socket_handler = MagicMock()
        mock_socket_handler.client = MagicMock()
        mock_socket_handler.client.is_connected = AsyncMock(return_value=False)

        on_reconnect = AsyncMock(side_effect=RuntimeError("Reconnect failed"))
        on_exhausted = AsyncMock()

        monitor = SocketHealthMonitor(
            socket_handler=mock_socket_handler,
            on_reconnect_needed=on_reconnect,
            on_exhausted=on_exhausted,
            check_interval=0.05,
            max_reconnect_attempts=2,
        )

        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.3)
        # Monitor should exit after exhaustion despite exceptions
        await asyncio.wait_for(task, timeout=1.0)

        # Should have tried reconnect despite exceptions
        assert on_reconnect.call_count >= 2
        on_exhausted.assert_called_once()
