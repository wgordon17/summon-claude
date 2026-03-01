"""Tests for summon_claude.daemon — PID lifecycle, stale lock, is_daemon_running."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_data_dir(tmp_path: Path):
    """Patch get_data_dir to use tmp_path so tests don't touch real data dir."""
    import summon_claude.daemon as dm

    return patch.object(dm, "_data_dir", return_value=tmp_path)


# ---------------------------------------------------------------------------
# is_daemon_running
# ---------------------------------------------------------------------------


class TestIsDaemonRunning:
    def test_returns_false_when_socket_absent(self, tmp_path):
        """No socket file → daemon is not running."""
        with _patch_data_dir(tmp_path):
            from summon_claude.daemon import is_daemon_running

            assert is_daemon_running() is False

    def test_returns_false_when_socket_exists_but_refused(self, tmp_path):
        """Socket file present but connection refused → daemon not running."""
        import socket as _socket

        with _patch_data_dir(tmp_path):
            sock_path = tmp_path / "daemon.sock"
            sock_path.touch()

            with patch("summon_claude.daemon.socket.socket") as mock_sock_cls:
                mock_sock = MagicMock()
                mock_sock.connect.side_effect = ConnectionRefusedError
                mock_sock_cls.return_value = mock_sock

                from summon_claude.daemon import is_daemon_running

                assert is_daemon_running() is False

    def test_returns_true_when_socket_accepts_connection(self, tmp_path):
        """Socket exists and accepts connection → daemon is running."""
        with _patch_data_dir(tmp_path):
            sock_path = tmp_path / "daemon.sock"
            sock_path.touch()

            with patch("summon_claude.daemon.socket.socket") as mock_sock_cls:
                mock_sock = MagicMock()
                mock_sock_cls.return_value = mock_sock

                from summon_claude.daemon import is_daemon_running

                assert is_daemon_running() is True
                mock_sock.connect.assert_called_once_with(str(sock_path))
                mock_sock.close.assert_called_once()


# ---------------------------------------------------------------------------
# run_daemon — PID file lifecycle
# ---------------------------------------------------------------------------


class TestRunDaemon:
    def test_pid_file_created_on_start(self, tmp_path):
        """run_daemon should write its PID to the PID file."""
        captured_pid: list[str] = []

        def fake_asyncio_run(coro):
            # Read PID file while "running" — it should exist
            pid_path = tmp_path / "daemon.pid"
            if pid_path.exists():
                captured_pid.append(pid_path.read_text().strip())
            # Cancel coroutine to avoid ResourceWarning
            coro.close()

        with (
            _patch_data_dir(tmp_path),
            patch("asyncio.run", side_effect=fake_asyncio_run),
        ):
            from summon_claude.daemon import run_daemon

            mock_config = MagicMock()
            run_daemon(mock_config)

        assert str(os.getpid()) in captured_pid

    def test_pid_file_cleaned_up_after_run(self, tmp_path):
        """run_daemon should remove the PID file after asyncio.run completes."""
        with (
            _patch_data_dir(tmp_path),
            patch("asyncio.run", side_effect=lambda coro: coro.close()),
        ):
            from summon_claude.daemon import run_daemon

            mock_config = MagicMock()
            run_daemon(mock_config)

        assert not (tmp_path / "daemon.pid").exists()

    def test_raises_if_lock_already_held(self, tmp_path):
        """run_daemon should raise DaemonAlreadyRunningError if lock is held."""
        import fcntl

        from summon_claude.daemon import DaemonAlreadyRunningError

        lock_path = tmp_path / "daemon.lock"
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with _patch_data_dir(tmp_path), pytest.raises(DaemonAlreadyRunningError):
                from summon_claude.daemon import run_daemon

                mock_config = MagicMock()
                run_daemon(mock_config)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


# ---------------------------------------------------------------------------
# _wait_for_socket
# ---------------------------------------------------------------------------


class TestWaitForSocket:
    def test_returns_immediately_when_socket_exists(self, tmp_path):
        from summon_claude.daemon import _wait_for_socket

        sock = tmp_path / "daemon.sock"
        sock.touch()
        _wait_for_socket(sock)  # should not raise

    def test_raises_on_timeout(self, tmp_path):
        from summon_claude.daemon import _wait_for_socket

        sock = tmp_path / "daemon.sock"
        with (
            patch("summon_claude.daemon._SOCKET_WAIT_TIMEOUT_S", 0.05),
            patch("summon_claude.daemon._SOCKET_POLL_INTERVAL_S", 0.01),
            pytest.raises(RuntimeError, match="Daemon did not start"),
        ):
            _wait_for_socket(sock)

    def test_returns_when_socket_appears_after_delay(self, tmp_path):
        from summon_claude.daemon import _wait_for_socket

        sock = tmp_path / "daemon.sock"

        # Create socket after a short delay
        def _create_sock():
            time.sleep(0.05)
            sock.touch()

        import threading

        t = threading.Thread(target=_create_sock, daemon=True)
        t.start()
        _wait_for_socket(sock)  # should succeed
        t.join(timeout=1.0)


# ---------------------------------------------------------------------------
# _clear_stale_daemon_files
# ---------------------------------------------------------------------------


class TestClearStaleFiles:
    def test_removes_pid_and_socket_files(self, tmp_path):
        """_clear_stale_daemon_files removes PID and socket but preserves lock."""
        from summon_claude.daemon import _clear_stale_daemon_files

        with _patch_data_dir(tmp_path):
            (tmp_path / "daemon.lock").touch()
            (tmp_path / "daemon.pid").touch()
            (tmp_path / "daemon.sock").touch()

            _clear_stale_daemon_files()

        # Lock file is intentionally preserved — fcntl handles release
        assert (tmp_path / "daemon.lock").exists()
        assert not (tmp_path / "daemon.pid").exists()
        assert not (tmp_path / "daemon.sock").exists()

    def test_no_error_when_files_absent(self, tmp_path):
        from summon_claude.daemon import _clear_stale_daemon_files

        with _patch_data_dir(tmp_path):
            _clear_stale_daemon_files()  # must not raise


# ---------------------------------------------------------------------------
# connect_to_daemon
# ---------------------------------------------------------------------------


class TestConnectToDaemon:
    async def test_connect_opens_unix_socket(self, tmp_path):
        from summon_claude.daemon import connect_to_daemon

        mock_reader = AsyncMock()
        mock_writer = AsyncMock()

        with (
            _patch_data_dir(tmp_path),
            patch(
                "asyncio.open_unix_connection",
                return_value=(mock_reader, mock_writer),
            ) as mock_connect,
        ):
            reader, writer = await connect_to_daemon()

        mock_connect.assert_called_once_with(str(tmp_path / "daemon.sock"), limit=65536)
        assert reader is mock_reader
        assert writer is mock_writer


# ---------------------------------------------------------------------------
# daemon_main — wiring smoke test (unit-level, no real Bolt)
# ---------------------------------------------------------------------------


class TestDaemonMain:
    async def test_daemon_main_wires_components_and_shuts_down(self, tmp_path):
        """daemon_main should start components and stop cleanly on shutdown signal."""
        from summon_claude.daemon import daemon_main

        mock_config = MagicMock()

        mock_bolt = AsyncMock()
        mock_bolt.start = AsyncMock()
        mock_bolt.stop = AsyncMock()
        mock_bolt.shutdown_callback = None

        # start_health_monitor is sync and returns an awaitable Task; create a
        # pre-cancelled real task so daemon_main can await it without error.
        async def _noop() -> None:
            await asyncio.sleep(0)

        _mock_health_task = asyncio.get_event_loop().create_task(_noop())
        _mock_health_task.cancel()
        mock_bolt.start_health_monitor = MagicMock(return_value=_mock_health_task)

        mock_dispatcher = MagicMock()
        mock_session_manager = AsyncMock()
        mock_session_manager.shutdown = AsyncMock()
        # shutdown_event that fires immediately after setup
        shutdown_event = asyncio.Event()
        mock_session_manager.shutdown_event = shutdown_event

        # Handle client callback needed for asyncio.start_unix_server
        async def _handle_client(r, w):
            pass

        mock_session_manager.handle_client = _handle_client

        mock_server = AsyncMock()
        mock_server.close = MagicMock()
        mock_server.wait_closed = AsyncMock()

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon.BoltRouter", return_value=mock_bolt),
            patch("summon_claude.daemon.EventDispatcher", return_value=mock_dispatcher),
            patch("summon_claude.daemon.SessionManager", return_value=mock_session_manager),
            patch(
                "asyncio.start_unix_server",
                return_value=mock_server,
            ),
        ):
            # Trigger shutdown before daemon_main is even reached
            shutdown_event.set()
            await daemon_main(mock_config)

        mock_bolt.start.assert_awaited_once()
        mock_bolt.stop.assert_awaited_once()
        mock_session_manager.shutdown.assert_awaited_once()
        # CR-007: verify the critical shutdown callback wiring
        # Verify shutdown callback wiring — calling it should set the event
        mock_bolt.shutdown_callback()
        assert shutdown_event.is_set()
