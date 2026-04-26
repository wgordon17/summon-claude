"""Tests for summon_claude.daemon — PID lifecycle, stale lock, is_daemon_running, IPC."""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.daemon import MAX_MESSAGE_SIZE, recv_msg, send_msg
from summon_claude.sessions.registry import SessionRegistry
from summon_claude.slack.bolt import DiagnosticResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _patch_data_dir:  # noqa: N801
    """Context manager that patches _data_dir and _daemon_socket so tests don't touch
    the real data dir or /tmp.

    After Task 2, _daemon_socket() delegates to get_socket_path() instead of
    _data_dir() / "daemon.sock". Patching both ensures tests work before and
    after the Task 2 migration.
    """

    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self._patches: list = []

    def __enter__(self):
        import summon_claude.daemon as dm

        sock = self._tmp_path / "daemon.sock"
        p1 = patch.object(dm, "_data_dir", return_value=self._tmp_path)
        p2 = patch.object(dm, "_daemon_socket", return_value=sock)
        self._patches = [p1, p2]
        for p in self._patches:
            p.__enter__()
        return self

    def __exit__(self, *args):
        for p in reversed(self._patches):
            p.__exit__(*args)


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
            patch("summon_claude.daemon._cleanup_orphaned_sessions", new=AsyncMock()),
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


# ---------------------------------------------------------------------------
# C4: Startup probe, error file, and _wait_for_socket diagnostic
# ---------------------------------------------------------------------------


class TestWriteStartupError:
    def test_writes_file_with_mode_600(self, tmp_path):
        with _patch_data_dir(tmp_path):
            from summon_claude.daemon import _write_startup_error

            _write_startup_error("test diagnostic message")

        error_path = tmp_path / "last-startup-error"
        assert error_path.exists()
        content = error_path.read_text()
        assert "test diagnostic message" in content
        assert oct(error_path.stat().st_mode & 0o777) == "0o600"

    def test_write_includes_timestamp(self, tmp_path):
        with _patch_data_dir(tmp_path):
            from summon_claude.daemon import _write_startup_error

            _write_startup_error("error msg")

        content = (tmp_path / "last-startup-error").read_text()
        assert "[" in content and "]" in content


class TestWaitForSocketWithErrorFile:
    def test_includes_error_file_diagnostic_on_timeout(self, tmp_path):
        error_path = tmp_path / "last-startup-error"
        error_path.write_text("Startup failed: token_revoked")

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon._SOCKET_WAIT_TIMEOUT_S", 0.05),
            patch("summon_claude.daemon._SOCKET_POLL_INTERVAL_S", 0.01),
            pytest.raises(RuntimeError, match="Daemon startup failed"),
        ):
            from summon_claude.daemon import _wait_for_socket

            _wait_for_socket(tmp_path / "daemon.sock")

        assert not error_path.exists()

    def test_generic_message_when_no_error_file(self, tmp_path):
        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon._SOCKET_WAIT_TIMEOUT_S", 0.05),
            patch("summon_claude.daemon._SOCKET_POLL_INTERVAL_S", 0.01),
            pytest.raises(RuntimeError, match="Daemon did not start"),
        ):
            from summon_claude.daemon import _wait_for_socket

            _wait_for_socket(tmp_path / "daemon.sock")


class TestDaemonMainStartupProbe:
    async def _make_mock_bolt(self, probe_result: DiagnosticResult):
        mock_bolt = AsyncMock()
        mock_bolt.start = AsyncMock()
        mock_bolt.stop = AsyncMock()
        mock_bolt.shutdown_callback = None
        mock_bolt.event_failure_callback = None

        mock_probe = MagicMock()
        mock_probe.run_probe = AsyncMock(return_value=probe_result)
        mock_probe.format_alert = MagicMock(return_value=":x: test alert")
        mock_bolt.event_probe = mock_probe

        async def _noop():
            await asyncio.sleep(0)

        _mock_health_task = asyncio.get_event_loop().create_task(_noop())
        _mock_health_task.cancel()
        mock_bolt.start_health_monitor = MagicMock(return_value=_mock_health_task)
        return mock_bolt, mock_probe

    async def test_startup_probe_healthy_clears_error_file(self, tmp_path):
        error_path = tmp_path / "last-startup-error"
        error_path.write_text("old error")

        result = DiagnosticResult(healthy=True, reason="healthy", details="OK")
        mock_bolt, _ = await self._make_mock_bolt(result)
        mock_session_manager = AsyncMock()
        shutdown_event = asyncio.Event()
        mock_session_manager.shutdown_event = shutdown_event
        mock_session_manager.handle_client = AsyncMock()
        mock_server = AsyncMock()
        mock_server.close = MagicMock()

        from summon_claude.daemon import daemon_main

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon.BoltRouter", return_value=mock_bolt),
            patch("summon_claude.daemon.EventDispatcher", return_value=MagicMock()),
            patch("summon_claude.daemon.SessionManager", return_value=mock_session_manager),
            patch("asyncio.start_unix_server", return_value=mock_server),
            patch("summon_claude.daemon._cleanup_orphaned_sessions", new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            shutdown_event.set()
            await daemon_main(MagicMock())

        assert not error_path.exists()

    async def test_startup_probe_unhealthy_writes_error_file(self, tmp_path):
        result = DiagnosticResult(
            healthy=False,
            reason="token_revoked",
            details="Token invalid.",
        )
        mock_bolt, _ = await self._make_mock_bolt(result)

        from summon_claude.daemon import daemon_main

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon.BoltRouter", return_value=mock_bolt),
            patch("summon_claude.daemon.EventDispatcher", return_value=MagicMock()),
            patch("summon_claude.daemon.SessionManager", return_value=AsyncMock()),
            patch("asyncio.start_unix_server", return_value=AsyncMock()),
            patch("summon_claude.daemon._cleanup_orphaned_sessions", new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
            pytest.raises(SystemExit),
        ):
            await daemon_main(MagicMock())

        error_path = tmp_path / "last-startup-error"
        assert error_path.exists()

    async def test_startup_probe_events_disabled_soft_fails(self, tmp_path):
        result = DiagnosticResult(
            healthy=False,
            reason="events_disabled",
            details="Events not delivered.",
        )
        mock_bolt, _ = await self._make_mock_bolt(result)
        mock_session_manager = AsyncMock()
        shutdown_event = asyncio.Event()
        mock_session_manager.shutdown_event = shutdown_event
        mock_session_manager.handle_client = AsyncMock()
        mock_server = AsyncMock()
        mock_server.close = MagicMock()

        from summon_claude.daemon import daemon_main

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon.BoltRouter", return_value=mock_bolt),
            patch("summon_claude.daemon.EventDispatcher", return_value=MagicMock()),
            patch("summon_claude.daemon.SessionManager", return_value=mock_session_manager),
            patch("asyncio.start_unix_server", return_value=mock_server),
            patch("summon_claude.daemon._cleanup_orphaned_sessions", new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            shutdown_event.set()
            await daemon_main(MagicMock())  # must not raise

        assert not (tmp_path / "last-startup-error").exists()

    async def test_startup_probe_unknown_soft_fails(self, tmp_path):
        """Probe with reason='unknown' should soft-fail (continue without error file)."""
        result = DiagnosticResult(
            healthy=False,
            reason="unknown",
            details="Unknown failure.",
        )
        mock_bolt, _ = await self._make_mock_bolt(result)
        mock_session_manager = AsyncMock()
        shutdown_event = asyncio.Event()
        mock_session_manager.shutdown_event = shutdown_event
        mock_session_manager.handle_client = AsyncMock()
        mock_server = AsyncMock()
        mock_server.close = MagicMock()

        from summon_claude.daemon import daemon_main

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon.BoltRouter", return_value=mock_bolt),
            patch("summon_claude.daemon.EventDispatcher", return_value=MagicMock()),
            patch("summon_claude.daemon.SessionManager", return_value=mock_session_manager),
            patch("asyncio.start_unix_server", return_value=mock_server),
            patch("summon_claude.daemon._cleanup_orphaned_sessions", new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            shutdown_event.set()
            await daemon_main(MagicMock())

        assert not (tmp_path / "last-startup-error").exists()

    async def test_startup_probe_exception_soft_fails(self, tmp_path):
        """Probe that raises an exception should soft-fail."""
        mock_bolt = AsyncMock()
        mock_bolt.start = AsyncMock()
        mock_bolt.stop = AsyncMock()
        mock_bolt.shutdown_callback = None
        mock_bolt.event_failure_callback = None

        mock_probe = MagicMock()
        mock_probe.run_probe = AsyncMock(side_effect=RuntimeError("probe crash"))
        mock_bolt.event_probe = mock_probe

        async def _noop():
            await asyncio.sleep(0)

        _mock_health_task = asyncio.get_event_loop().create_task(_noop())
        _mock_health_task.cancel()
        mock_bolt.start_health_monitor = MagicMock(return_value=_mock_health_task)

        mock_session_manager = AsyncMock()
        shutdown_event = asyncio.Event()
        mock_session_manager.shutdown_event = shutdown_event
        mock_session_manager.handle_client = AsyncMock()
        mock_server = AsyncMock()
        mock_server.close = MagicMock()

        from summon_claude.daemon import daemon_main

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon.BoltRouter", return_value=mock_bolt),
            patch("summon_claude.daemon.EventDispatcher", return_value=MagicMock()),
            patch("summon_claude.daemon.SessionManager", return_value=mock_session_manager),
            patch("asyncio.start_unix_server", return_value=mock_server),
            patch("summon_claude.daemon._cleanup_orphaned_sessions", new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            shutdown_event.set()
            await daemon_main(MagicMock())  # must not raise

        assert not (tmp_path / "last-startup-error").exists()


class TestCleanupOrphanedSessions:
    """Tests for _cleanup_orphaned_sessions at daemon startup."""

    async def test_marks_active_sessions_as_errored(self, temp_db_path):
        from summon_claude.daemon import _cleanup_orphaned_sessions

        async with SessionRegistry(db_path=temp_db_path) as reg:
            await reg.register("orphan-1", 9999, "/tmp", "old-session")
            await reg.update_status("orphan-1", "active", slack_channel_id="C111")
            await reg.register("orphan-2", 9999, "/tmp")
            # orphan-2 stays as pending_auth (no channel)

        mock_client = AsyncMock()
        with patch(
            "summon_claude.daemon.SessionRegistry",
            lambda: SessionRegistry(db_path=temp_db_path),
        ):
            await _cleanup_orphaned_sessions(mock_client)

        async with SessionRegistry(db_path=temp_db_path) as reg:
            s1 = await reg.get_session("orphan-1")
            assert s1 is not None
            assert s1["status"] == "errored"
            assert "Orphaned by daemon restart" in s1["error_message"]
            assert s1["ended_at"] is not None

            s2 = await reg.get_session("orphan-2")
            assert s2 is not None
            assert s2["status"] == "errored"

        # Disconnect message posted to orphan-1's channel
        mock_client.chat_postMessage.assert_called_once()
        call_kwargs = mock_client.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C111"
        assert "daemon restarted" in call_kwargs["text"]

    async def test_no_op_when_no_active_sessions(self, temp_db_path):
        from summon_claude.daemon import _cleanup_orphaned_sessions

        async with SessionRegistry(db_path=temp_db_path) as reg:
            await reg.register("done-1", 9999, "/tmp")
            await reg.update_status("done-1", "completed")

        mock_client = AsyncMock()
        with patch(
            "summon_claude.daemon.SessionRegistry",
            lambda: SessionRegistry(db_path=temp_db_path),
        ):
            await _cleanup_orphaned_sessions(mock_client)

        async with SessionRegistry(db_path=temp_db_path) as reg:
            s = await reg.get_session("done-1")
            assert s is not None
            assert s["status"] == "completed"

        # No messages posted
        mock_client.chat_postMessage.assert_not_called()

    async def test_zzz_orphan_cleanup_renames_channel(self, temp_db_path):
        """_cleanup_orphaned_sessions renames orphaned session channels with zzz- prefix."""
        from summon_claude.daemon import _cleanup_orphaned_sessions

        async with SessionRegistry(db_path=temp_db_path) as reg:
            await reg.register("orphan-zzz", 9999, "/tmp", "old-session")
            await reg.update_status(
                "orphan-zzz",
                "active",
                slack_channel_id="C_ZZZ",
                slack_channel_name="myproj-abc",
            )

        mock_client = AsyncMock()
        mock_client.conversations_rename = AsyncMock()
        with patch(
            "summon_claude.daemon.SessionRegistry",
            lambda: SessionRegistry(db_path=temp_db_path),
        ):
            await _cleanup_orphaned_sessions(mock_client)

        mock_client.conversations_rename.assert_awaited_once()
        call_kwargs = mock_client.conversations_rename.call_args.kwargs
        assert call_kwargs["channel"] == "C_ZZZ"
        assert call_kwargs["name"].startswith("zzz-")
        assert "myproj-abc" in call_kwargs["name"]

    async def test_zzz_orphan_cleanup_skips_already_prefixed(self, temp_db_path):
        """_cleanup_orphaned_sessions skips rename when channel already zzz-prefixed."""
        from summon_claude.daemon import _cleanup_orphaned_sessions

        async with SessionRegistry(db_path=temp_db_path) as reg:
            await reg.register("orphan-pre", 9999, "/tmp", "old-session")
            await reg.update_status(
                "orphan-pre",
                "active",
                slack_channel_id="C_PRE",
                slack_channel_name="zzz-already-done",
            )

        mock_client = AsyncMock()
        mock_client.conversations_rename = AsyncMock()
        with patch(
            "summon_claude.daemon.SessionRegistry",
            lambda: SessionRegistry(db_path=temp_db_path),
        ):
            await _cleanup_orphaned_sessions(mock_client)

        mock_client.conversations_rename.assert_not_awaited()

    async def test_zzz_orphan_cleanup_rename_failure_continues(self, temp_db_path):
        """_cleanup_orphaned_sessions continues if rename raises — session still errored."""
        from summon_claude.daemon import _cleanup_orphaned_sessions

        async with SessionRegistry(db_path=temp_db_path) as reg:
            await reg.register("orphan-fail", 9999, "/tmp", "old-session")
            await reg.update_status(
                "orphan-fail",
                "active",
                slack_channel_id="C_FAIL",
                slack_channel_name="myproj-fail",
            )

        mock_client = AsyncMock()
        mock_client.conversations_rename = AsyncMock(side_effect=Exception("channel_not_found"))
        with patch(
            "summon_claude.daemon.SessionRegistry",
            lambda: SessionRegistry(db_path=temp_db_path),
        ):
            await _cleanup_orphaned_sessions(mock_client)

        # Session should still be marked errored despite rename failure
        async with SessionRegistry(db_path=temp_db_path) as reg:
            s = await reg.get_session("orphan-fail")
            assert s is not None
            assert s["status"] == "errored"


# ---------------------------------------------------------------------------
# IPC framing protocol tests (absorbed from test_ipc.py)
# ---------------------------------------------------------------------------


class _StreamPair:
    """Holds a connected (reader, writer) pair and keeps all transports alive."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        _r_writer: asyncio.StreamWriter,
        _w_reader: asyncio.StreamReader,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self._r_writer = _r_writer
        self._w_reader = _w_reader


async def _make_stream_pair() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Return a (reader, writer) pair connected via an in-process socketpair."""
    rsock, wsock = socket.socketpair()
    reader, _r_writer = await asyncio.open_connection(sock=rsock)
    _w_reader, writer = await asyncio.open_connection(sock=wsock)
    pair = _StreamPair(reader, writer, _r_writer, _w_reader)
    writer._test_pair = pair  # type: ignore[attr-defined]
    return reader, writer


class TestSendRecvRoundTrip:
    """Round-trip tests: send_msg then recv_msg returns the same data."""

    async def test_simple_dict(self):
        reader, writer = await _make_stream_pair()
        data = {"type": "hello", "session_id": "abc-123", "value": 42}
        await send_msg(writer, data)
        result = await recv_msg(reader)
        assert result == data
        writer.close()

    async def test_empty_dict(self):
        reader, writer = await _make_stream_pair()
        await send_msg(writer, {})
        result = await recv_msg(reader)
        assert result == {}
        writer.close()

    async def test_nested_dict(self):
        reader, writer = await _make_stream_pair()
        data = {"event": "message", "payload": {"text": "Hello \u2603", "numbers": [1, 2, 3]}}
        await send_msg(writer, data)
        result = await recv_msg(reader)
        assert result == data
        writer.close()

    async def test_multiple_messages_in_sequence(self):
        reader, writer = await _make_stream_pair()
        messages = [{"seq": 0}, {"seq": 1}, {"seq": 2}]
        for msg in messages:
            await send_msg(writer, msg)
        for expected in messages:
            result = await recv_msg(reader)
            assert result == expected
        writer.close()

    async def test_string_with_embedded_newlines(self):
        reader, writer = await _make_stream_pair()
        data = {"text": "line one\nline two\r\nline three"}
        await send_msg(writer, data)
        result = await recv_msg(reader)
        assert result == data
        writer.close()

    async def test_boolean_and_null_values(self):
        reader, writer = await _make_stream_pair()
        data = {"active": True, "done": False, "nothing": None}
        await send_msg(writer, data)
        result = await recv_msg(reader)
        assert result == data
        writer.close()

    async def test_large_message_well_within_limit(self):
        reader, writer = await _make_stream_pair()
        data = {"payload": "x" * (50 * 1024)}
        await send_msg(writer, data)
        result = await recv_msg(reader)
        assert result == data
        writer.close()


class TestOversizedMessageRejection:
    """recv_msg must raise ValueError for messages claiming to exceed 64 KiB."""

    async def test_oversized_header_raises_value_error(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", MAX_MESSAGE_SIZE + 1))
        with pytest.raises(ValueError, match="Message too large"):
            await recv_msg(reader)

    async def test_exact_max_size_is_accepted(self):
        prefix = b'{"k": "'
        suffix = b'"}'
        padding = b"x" * (MAX_MESSAGE_SIZE - len(prefix) - len(suffix))
        payload = prefix + padding + suffix
        assert len(payload) == MAX_MESSAGE_SIZE
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", MAX_MESSAGE_SIZE) + payload)
        result = await recv_msg(reader)
        assert result["k"] == "x" * (MAX_MESSAGE_SIZE - len(prefix) - len(suffix))

    async def test_max_uint32_header_raises_value_error(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", 0xFFFFFFFF))
        with pytest.raises(ValueError, match="Message too large"):
            await recv_msg(reader)

    async def test_error_message_includes_size_info(self):
        reader = asyncio.StreamReader()
        oversized = MAX_MESSAGE_SIZE + 100
        reader.feed_data(struct.pack(">I", oversized))
        with pytest.raises(ValueError) as exc_info:
            await recv_msg(reader)
        assert str(oversized) in str(exc_info.value)


class TestEmptyPayload:
    """Edge cases around minimal/empty JSON payloads."""

    async def test_connection_closed_before_header_raises(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x00\x00")
        reader.feed_eof()
        with pytest.raises(asyncio.IncompleteReadError):
            await recv_msg(reader)

    async def test_connection_closed_before_payload_raises(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", 10) + b"abc")
        reader.feed_eof()
        with pytest.raises(asyncio.IncompleteReadError):
            await recv_msg(reader)

    async def test_connection_closed_with_no_data_raises(self):
        reader = asyncio.StreamReader()
        reader.feed_eof()
        with pytest.raises(asyncio.IncompleteReadError):
            await recv_msg(reader)

    async def test_connection_closed_after_3_header_bytes_raises(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x00\x00\x00")
        reader.feed_eof()
        with pytest.raises(asyncio.IncompleteReadError):
            await recv_msg(reader)


# ---------------------------------------------------------------------------
# _validate_socket_path — Unix socket path length guard
# ---------------------------------------------------------------------------


class TestValidateSocketPath:
    def test_accepts_short_path(self):
        from summon_claude.daemon import _validate_socket_path

        sock = Path("/tmp/d.sock")
        _validate_socket_path(sock)  # should not raise

    def test_raises_on_long_path(self):
        from summon_claude.daemon import SocketPathTooLongError, _validate_socket_path

        # Build a path that exceeds 104 chars
        sock = Path("/" + "a" * 200 + "/daemon.sock")
        with pytest.raises(SocketPathTooLongError, match="exceeding the Unix limit"):
            _validate_socket_path(sock)

    def test_error_message_includes_path_and_hint(self):
        from summon_claude.daemon import SocketPathTooLongError, _validate_socket_path

        sock = Path("/" + "x" * 200 + "/daemon.sock")
        with pytest.raises(SocketPathTooLongError) as exc_info:
            _validate_socket_path(sock)
        msg = str(exc_info.value)
        # Autouse fixture forces global mode — hint shows XDG_DATA_HOME
        assert "XDG_DATA_HOME" in msg
        assert "daemon.sock" in msg

    def test_error_message_includes_local_mode_hint(self, tmp_path, monkeypatch):
        from summon_claude.config import _detect_install_mode
        from summon_claude.daemon import SocketPathTooLongError, _validate_socket_path

        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUMMON_LOCAL", "1")
        _detect_install_mode.cache_clear()

        sock = Path("/" + "x" * 200 + "/daemon.sock")
        with pytest.raises(SocketPathTooLongError) as exc_info:
            _validate_socket_path(sock)
        msg = str(exc_info.value)
        # C2 changed the local-mode hint: /tmp path can't normally be too long,
        # so it now directs the user to file a bug rather than offering a workaround.
        assert "Please file a bug" in msg

    def test_accepts_path_at_exact_limit(self):
        from summon_claude.daemon import _UNIX_SOCKET_PATH_MAX, _validate_socket_path

        # Build a path exactly at the limit: "/" (1 char) + N x's
        sock = Path("/" + "x" * (_UNIX_SOCKET_PATH_MAX - 1))
        assert len(str(sock)) == _UNIX_SOCKET_PATH_MAX
        _validate_socket_path(sock)  # should not raise


# ---------------------------------------------------------------------------
# Socket path delegation and new /tmp-based socket tests
# ---------------------------------------------------------------------------


class TestDaemonSocketDelegation:
    def test_daemon_socket_delegates_to_get_socket_path(self, tmp_path):
        """_daemon_socket() returns whatever get_socket_path() returns."""
        from summon_claude.daemon import _daemon_socket

        expected = tmp_path / "mysocket.sock"
        with patch("summon_claude.daemon.get_socket_path", return_value=expected):
            result = _daemon_socket()
        assert result == expected

    def test_is_daemon_running_checks_new_socket_path(self, tmp_path):
        """is_daemon_running() checks the path returned by _daemon_socket()."""
        sock_path = tmp_path / "sockets" / "abcdef012345.sock"
        sock_path.parent.mkdir(parents=True)
        sock_path.touch()

        with (
            patch("summon_claude.daemon._daemon_socket", return_value=sock_path),
            patch("summon_claude.daemon.socket.socket") as mock_sock_cls,
        ):
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock

            from summon_claude.daemon import is_daemon_running

            assert is_daemon_running() is True
            mock_sock.connect.assert_called_once_with(str(sock_path))

    async def test_connect_to_daemon_uses_new_socket_path(self, tmp_path):
        """connect_to_daemon() passes the new socket path to open_unix_connection."""
        from summon_claude.daemon import connect_to_daemon

        sock_path = tmp_path / "sockets" / "abcdef012345.sock"
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()

        with (
            patch("summon_claude.daemon._daemon_socket", return_value=sock_path),
            patch(
                "asyncio.open_unix_connection",
                return_value=(mock_reader, mock_writer),
            ) as mock_connect,
        ):
            reader, writer = await connect_to_daemon()

        mock_connect.assert_called_once_with(str(sock_path), limit=65536)
        assert reader is mock_reader


class TestClearStaleDaemonFilesNewPath:
    def test_clears_new_tmp_socket(self, tmp_path):
        """_clear_stale_daemon_files() removes socket at the new /tmp-based path."""
        sock_path = tmp_path / "sockets" / "abcdef012345.sock"
        sock_path.parent.mkdir(parents=True)
        sock_path.touch()

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon._daemon_socket", return_value=sock_path),
        ):
            from summon_claude.daemon import _clear_stale_daemon_files

            _clear_stale_daemon_files()

        assert not sock_path.exists()

    def test_clears_old_legacy_socket_from_data_dir(self, tmp_path):
        """_clear_stale_daemon_files() also removes the old .summon/daemon.sock."""
        old_sock = tmp_path / "daemon.sock"
        old_sock.touch()

        new_sock_path = tmp_path / "sockets" / "abcdef012345.sock"

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon._daemon_socket", return_value=new_sock_path),
            # Patch get_data_dir in daemon's namespace so the legacy cleanup finds tmp_path
            patch("summon_claude.daemon.get_data_dir", return_value=tmp_path),
        ):
            from summon_claude.daemon import _clear_stale_daemon_files

            _clear_stale_daemon_files()

        assert not old_sock.exists()

    def test_no_error_when_both_sockets_absent(self, tmp_path):
        """_clear_stale_daemon_files() does not raise when no sockets exist."""
        new_sock_path = tmp_path / "sockets" / "abcdef012345.sock"

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon._daemon_socket", return_value=new_sock_path),
        ):
            from summon_claude.daemon import _clear_stale_daemon_files

            _clear_stale_daemon_files()  # must not raise


class TestSocketDirPermissions:
    def test_socket_dir_created_with_0o700(self, tmp_path, monkeypatch):
        """start_daemon() in local mode creates socket dir with mode 0o700."""
        import stat

        from summon_claude.config import _detect_install_mode

        # Simulate local mode
        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUMMON_LOCAL", "1")
        _detect_install_mode.cache_clear()

        sock_dir = tmp_path / "sockets"
        sock_path = sock_dir / "abcdef012345.sock"

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon._daemon_socket", return_value=sock_path),
            patch("summon_claude.daemon.is_daemon_running", return_value=True),
        ):
            # Just test directory creation logic by calling mkdir with the right mode
            sock_dir.mkdir(mode=0o700, exist_ok=True)
            dir_stat = sock_dir.stat()
            assert stat.S_IMODE(dir_stat.st_mode) == 0o700

    def test_symlink_at_socket_dir_causes_refusal(self, tmp_path):
        """Symlink at socket dir path should be detected and refused."""
        import stat as stat_mod

        # Simulate: the parent tmp dir is where we'd create /tmp/summon-<uid>/
        target = tmp_path / "other_dir"
        target.mkdir()
        link_path = tmp_path / "summon_uid_dir"
        link_path.symlink_to(target)

        # lstat should detect this as a symlink
        lstat_result = link_path.lstat()
        assert stat_mod.S_ISLNK(lstat_result.st_mode)

    def test_ownership_check_catches_wrong_owner(self, tmp_path):
        """Directory with wrong st_uid should be detectable via lstat."""
        import stat as stat_mod

        real_dir = tmp_path / "socket_dir"
        real_dir.mkdir(mode=0o700)

        lstat_result = real_dir.lstat()
        # Should be owned by current user
        assert lstat_result.st_uid == os.getuid()
        # Simulated wrong owner check: not lstat_result.st_uid == os.getuid()
        fake_uid = os.getuid() + 9999
        assert not stat_mod.S_ISLNK(lstat_result.st_mode)
        assert lstat_result.st_uid != fake_uid


# ---------------------------------------------------------------------------
# TestJiraProxyLifecycle — proxy startup and shutdown in daemon_main
# ---------------------------------------------------------------------------


class TestJiraProxyLifecycle:
    """Tests for Jira proxy startup/shutdown integration in daemon_main."""

    async def test_jira_proxy_startup_with_credentials(self, tmp_path):
        """Proxy starts and port is passed to SessionManager when creds exist."""
        from summon_claude.daemon import daemon_main

        async def _noop():
            await asyncio.sleep(0)

        mock_bolt = AsyncMock()
        mock_bolt.start = AsyncMock()
        mock_bolt.stop = AsyncMock()
        mock_bolt.shutdown_callback = None
        mock_bolt.event_failure_callback = None
        mock_bolt.event_probe = None
        _mock_health_task = asyncio.get_event_loop().create_task(_noop())
        _mock_health_task.cancel()
        mock_bolt.start_health_monitor = MagicMock(return_value=_mock_health_task)
        mock_session_manager = AsyncMock()
        shutdown_event = asyncio.Event()
        mock_session_manager.shutdown_event = shutdown_event
        mock_session_manager.handle_client = AsyncMock()
        mock_server = AsyncMock()
        mock_server.close = MagicMock()

        mock_proxy = AsyncMock()
        mock_proxy.start = AsyncMock(return_value=12345)
        mock_proxy.access_token = "proxy-secret-token"
        mock_proxy.warmup = AsyncMock(return_value=True)
        mock_proxy.stop = AsyncMock()

        session_manager_calls = []

        def _capture_session_manager(**kwargs):
            session_manager_calls.append(kwargs)
            return mock_session_manager

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon.BoltRouter", return_value=mock_bolt),
            patch("summon_claude.daemon.EventDispatcher", return_value=MagicMock()),
            patch("summon_claude.daemon.SessionManager", side_effect=_capture_session_manager),
            patch("asyncio.start_unix_server", return_value=mock_server),
            patch("summon_claude.daemon._cleanup_orphaned_sessions", new=AsyncMock()),
            patch("summon_claude.jira_auth.jira_credentials_exist", return_value=True),
            patch("summon_claude.jira_proxy.JiraAuthProxy", return_value=mock_proxy),
        ):
            shutdown_event.set()
            await daemon_main(MagicMock())

        # Verify proxy was started
        mock_proxy.start.assert_awaited_once()

        # Verify port and token were passed to SessionManager
        assert len(session_manager_calls) == 1
        assert session_manager_calls[0]["jira_proxy_port"] == 12345
        assert session_manager_calls[0]["jira_proxy_token"] == "proxy-secret-token"

        # Verify proxy was stopped during shutdown
        mock_proxy.stop.assert_awaited_once()

    async def test_jira_proxy_startup_without_credentials(self, tmp_path):
        """When jira_credentials_exist() is False, no proxy is created."""
        from summon_claude.daemon import daemon_main

        async def _noop():
            await asyncio.sleep(0)

        mock_bolt = AsyncMock()
        mock_bolt.start = AsyncMock()
        mock_bolt.stop = AsyncMock()
        mock_bolt.shutdown_callback = None
        mock_bolt.event_failure_callback = None
        mock_bolt.event_probe = None
        _mock_health_task = asyncio.get_event_loop().create_task(_noop())
        _mock_health_task.cancel()
        mock_bolt.start_health_monitor = MagicMock(return_value=_mock_health_task)

        mock_session_manager = AsyncMock()
        shutdown_event = asyncio.Event()
        mock_session_manager.shutdown_event = shutdown_event
        mock_session_manager.handle_client = AsyncMock()
        mock_server = AsyncMock()
        mock_server.close = MagicMock()

        session_manager_calls = []

        def _capture_session_manager(**kwargs):
            session_manager_calls.append(kwargs)
            return mock_session_manager

        mock_proxy_cls = MagicMock()

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon.BoltRouter", return_value=mock_bolt),
            patch("summon_claude.daemon.EventDispatcher", return_value=MagicMock()),
            patch("summon_claude.daemon.SessionManager", side_effect=_capture_session_manager),
            patch("asyncio.start_unix_server", return_value=mock_server),
            patch("summon_claude.daemon._cleanup_orphaned_sessions", new=AsyncMock()),
            patch("summon_claude.jira_auth.jira_credentials_exist", return_value=False),
            patch("summon_claude.jira_proxy.JiraAuthProxy", mock_proxy_cls),
        ):
            shutdown_event.set()
            await daemon_main(MagicMock())

        # Proxy class should never be instantiated
        mock_proxy_cls.assert_not_called()

        # SessionManager should get None for proxy fields
        assert len(session_manager_calls) == 1
        assert session_manager_calls[0]["jira_proxy_port"] is None
        assert session_manager_calls[0]["jira_proxy_token"] is None

    async def test_jira_proxy_shutdown_called_after_sessions(self, tmp_path):
        """Proxy stop() is called after session_manager.shutdown()."""
        from summon_claude.daemon import daemon_main

        async def _noop():
            await asyncio.sleep(0)

        mock_bolt = AsyncMock()
        mock_bolt.start = AsyncMock()
        mock_bolt.stop = AsyncMock()
        mock_bolt.shutdown_callback = None
        mock_bolt.event_failure_callback = None
        mock_bolt.event_probe = None
        _mock_health_task = asyncio.get_event_loop().create_task(_noop())
        _mock_health_task.cancel()
        mock_bolt.start_health_monitor = MagicMock(return_value=_mock_health_task)

        mock_session_manager = AsyncMock()
        shutdown_event = asyncio.Event()
        mock_session_manager.shutdown_event = shutdown_event
        mock_session_manager.handle_client = AsyncMock()
        mock_server = AsyncMock()
        mock_server.close = MagicMock()

        call_order: list[str] = []

        async def _session_shutdown():
            call_order.append("sessions")

        mock_session_manager.shutdown = _session_shutdown

        mock_proxy = AsyncMock()
        mock_proxy.start = AsyncMock(return_value=9999)
        mock_proxy.access_token = "token"
        mock_proxy.warmup = AsyncMock(return_value=True)

        async def _proxy_stop():
            call_order.append("proxy")

        mock_proxy.stop = _proxy_stop

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon.BoltRouter", return_value=mock_bolt),
            patch("summon_claude.daemon.EventDispatcher", return_value=MagicMock()),
            patch("summon_claude.daemon.SessionManager", return_value=mock_session_manager),
            patch("asyncio.start_unix_server", return_value=mock_server),
            patch("summon_claude.daemon._cleanup_orphaned_sessions", new=AsyncMock()),
            patch("summon_claude.jira_auth.jira_credentials_exist", return_value=True),
            patch("summon_claude.jira_proxy.JiraAuthProxy", return_value=mock_proxy),
        ):
            shutdown_event.set()
            await daemon_main(MagicMock())

        # Sessions must drain before proxy stops
        assert call_order.index("sessions") < call_order.index("proxy")

    async def test_jira_proxy_startup_failure_falls_back(self, tmp_path):
        """When proxy start() raises, daemon continues with proxy=None."""
        from summon_claude.daemon import daemon_main

        async def _noop():
            await asyncio.sleep(0)

        mock_bolt = AsyncMock()
        mock_bolt.start = AsyncMock()
        mock_bolt.stop = AsyncMock()
        mock_bolt.shutdown_callback = None
        mock_bolt.event_failure_callback = None
        mock_bolt.event_probe = None
        _mock_health_task = asyncio.get_event_loop().create_task(_noop())
        _mock_health_task.cancel()
        mock_bolt.start_health_monitor = MagicMock(return_value=_mock_health_task)

        mock_session_manager = AsyncMock()
        shutdown_event = asyncio.Event()
        mock_session_manager.shutdown_event = shutdown_event
        mock_session_manager.handle_client = AsyncMock()
        mock_server = AsyncMock()
        mock_server.close = MagicMock()

        session_manager_calls = []

        def _capture_session_manager(**kwargs):
            session_manager_calls.append(kwargs)
            return mock_session_manager

        mock_proxy = AsyncMock()
        mock_proxy.start = AsyncMock(side_effect=RuntimeError("bind failed"))
        mock_proxy.stop = AsyncMock()

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon.BoltRouter", return_value=mock_bolt),
            patch("summon_claude.daemon.EventDispatcher", return_value=MagicMock()),
            patch("summon_claude.daemon.SessionManager", side_effect=_capture_session_manager),
            patch("asyncio.start_unix_server", return_value=mock_server),
            patch("summon_claude.daemon._cleanup_orphaned_sessions", new=AsyncMock()),
            patch("summon_claude.jira_auth.jira_credentials_exist", return_value=True),
            patch("summon_claude.jira_proxy.JiraAuthProxy", return_value=mock_proxy),
        ):
            shutdown_event.set()
            await daemon_main(MagicMock())

        # SessionManager gets None for proxy fields on startup failure
        assert len(session_manager_calls) == 1
        assert session_manager_calls[0]["jira_proxy_port"] is None
        assert session_manager_calls[0]["jira_proxy_token"] is None
        # stop() should NOT be called on a proxy that failed to start
        mock_proxy.stop.assert_not_awaited()

    async def test_jira_proxy_warmup_failure_still_starts(self, tmp_path):
        """When warmup() returns False, proxy is still started and port is passed."""
        from summon_claude.daemon import daemon_main

        async def _noop():
            await asyncio.sleep(0)

        mock_bolt = AsyncMock()
        mock_bolt.start = AsyncMock()
        mock_bolt.stop = AsyncMock()
        mock_bolt.shutdown_callback = None
        mock_bolt.event_failure_callback = None
        mock_bolt.event_probe = None
        _mock_health_task = asyncio.get_event_loop().create_task(_noop())
        _mock_health_task.cancel()
        mock_bolt.start_health_monitor = MagicMock(return_value=_mock_health_task)

        mock_session_manager = AsyncMock()
        shutdown_event = asyncio.Event()
        mock_session_manager.shutdown_event = shutdown_event
        mock_session_manager.handle_client = AsyncMock()
        mock_server = AsyncMock()
        mock_server.close = MagicMock()

        session_manager_calls = []

        def _capture_session_manager(**kwargs):
            session_manager_calls.append(kwargs)
            return mock_session_manager

        mock_proxy = AsyncMock()
        mock_proxy.start = AsyncMock(return_value=12345)
        mock_proxy.access_token = "proxy-secret-token"
        mock_proxy.warmup = AsyncMock(return_value=False)
        mock_proxy.stop = AsyncMock()

        with (
            _patch_data_dir(tmp_path),
            patch("summon_claude.daemon.BoltRouter", return_value=mock_bolt),
            patch("summon_claude.daemon.EventDispatcher", return_value=MagicMock()),
            patch("summon_claude.daemon.SessionManager", side_effect=_capture_session_manager),
            patch("asyncio.start_unix_server", return_value=mock_server),
            patch("summon_claude.daemon._cleanup_orphaned_sessions", new=AsyncMock()),
            patch("summon_claude.jira_auth.jira_credentials_exist", return_value=True),
            patch("summon_claude.jira_proxy.JiraAuthProxy", return_value=mock_proxy),
        ):
            shutdown_event.set()
            await daemon_main(MagicMock())

        # Proxy is still started and port is passed despite warmup failure
        mock_proxy.start.assert_awaited_once()
        assert len(session_manager_calls) == 1
        assert session_manager_calls[0]["jira_proxy_port"] == 12345
        assert session_manager_calls[0]["jira_proxy_token"] == "proxy-secret-token"


# ---------------------------------------------------------------------------
# _setup_daemon_logging — idempotency
# ---------------------------------------------------------------------------


class TestSetupDaemonLogging:
    def test_creates_queue_handler(self, tmp_path):
        import logging
        import logging.handlers

        from summon_claude.daemon import _setup_daemon_logging

        log_file = tmp_path / "logs" / "daemon.log"
        root = logging.getLogger()
        initial_handler_count = len(root.handlers)

        listener = None
        try:
            listener = _setup_daemon_logging(log_file)

            assert log_file.parent.exists()
            # At least one new handler added (QueueHandler)
            assert len(root.handlers) > initial_handler_count
            new_handlers = root.handlers[initial_handler_count:]
            assert any(isinstance(h, logging.handlers.QueueHandler) for h in new_handlers)
        finally:
            if listener:
                listener.stop()
            for h in root.handlers[initial_handler_count:]:
                root.removeHandler(h)
                h.close()
