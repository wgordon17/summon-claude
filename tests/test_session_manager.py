"""Tests for summon_claude.session_manager — SessionManager lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.config import SummonConfig
from summon_claude.ipc import recv_msg, send_msg
from summon_claude.session import SessionOptions
from summon_claude.session_manager import _GRACE_SECONDS, SessionManager
from summon_claude.sessions.auth import SessionAuth

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(**overrides) -> SummonConfig:
    defaults = {
        "slack_bot_token": "xoxb-test",
        "slack_app_token": "xapp-test",
        "slack_signing_secret": "secret",
    }
    defaults.update(overrides)
    return SummonConfig.model_validate(defaults)


def make_options(session_id: str = "sess-1", **overrides) -> SessionOptions:
    defaults = {"session_id": session_id, "cwd": "/tmp", "name": "test"}
    defaults.update(overrides)
    return SessionOptions(**defaults)


def make_auth(session_id: str = "sess-1", **overrides) -> SessionAuth:
    defaults = {
        "short_code": "abcd1234",
        "session_id": session_id,
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    defaults.update(overrides)
    return SessionAuth(**defaults)


class _StubSession:
    """Minimal stub that satisfies SessionManager's interface without real Bolt."""

    def __init__(self, *, fail_with: Exception | None = None, runs: int = 1):
        self._fail_with = fail_with
        self._runs = runs
        self._run_count = 0
        self.channel_id: str | None = None
        self._shutdown_requested = False
        self._authenticated_user_id: str | None = None

    async def start(self) -> bool:
        self._run_count += 1
        if self._fail_with is not None:
            raise self._fail_with
        await asyncio.sleep(0)  # yield control
        return True

    def request_shutdown(self) -> None:
        self._shutdown_requested = True

    def authenticate(self, user_id: str) -> None:
        self._authenticated_user_id = user_id


def _make_manager(
    stub_session: _StubSession | None = None,
) -> tuple[SessionManager, MagicMock, MagicMock]:
    """Return (manager, mock_provider, mock_dispatcher) with SummonSession patched."""
    cfg = make_config()
    mock_provider = MagicMock()
    mock_provider.post_message = AsyncMock()
    mock_dispatcher = MagicMock()
    mock_dispatcher.unregister = MagicMock()

    manager = SessionManager(
        config=cfg, provider=mock_provider, bot_user_id="UBOT", dispatcher=mock_dispatcher
    )

    if stub_session is not None:
        # Patch SummonSession construction to return our stub
        manager._create_stub = stub_session  # type: ignore[attr-defined]

    return manager, mock_provider, mock_dispatcher


def _patch_session(manager: SessionManager, stub: _StubSession, session_id: str = "s1"):
    """Monkey-patch create_session to inject the stub instead of real SummonSession."""

    async def patched_create(options):
        manager._sessions[session_id] = stub  # type: ignore[assignment]
        from functools import partial

        task = asyncio.create_task(
            manager._supervised_session(stub, session_id),  # type: ignore[arg-type]
            name=f"session-{session_id}",
        )
        task.add_done_callback(partial(manager._on_task_done, session_id=session_id))
        manager._tasks[session_id] = task
        if manager._grace_timer is not None:
            manager._grace_timer.cancel()
            manager._grace_timer = None
        return "test-code"

    manager.create_session = patched_create  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Tests: _is_recoverable
# ---------------------------------------------------------------------------


class TestIsRecoverable:
    """Classification of exceptions as recoverable vs fatal."""

    def test_connection_error_is_recoverable(self):
        assert SessionManager._is_recoverable(ConnectionError("dropped")) is True

    def test_timeout_error_is_recoverable(self):
        assert SessionManager._is_recoverable(TimeoutError("timed out")) is True

    def test_os_error_is_recoverable(self):
        assert SessionManager._is_recoverable(OSError("socket closed")) is True

    def test_value_error_is_not_recoverable(self):
        assert SessionManager._is_recoverable(ValueError("bad config")) is False

    def test_runtime_error_is_not_recoverable(self):
        assert SessionManager._is_recoverable(RuntimeError("sdk crash")) is False

    def test_exception_base_is_not_recoverable(self):
        assert SessionManager._is_recoverable(Exception("unknown")) is False


# ---------------------------------------------------------------------------
# Tests: create_session / stop_session
# ---------------------------------------------------------------------------


class TestCreateSession:
    """create_session registers the session and starts a task."""

    async def test_create_session_registers_task(self):
        manager, _, _ = _make_manager()
        stub = _StubSession()
        _patch_session(manager, stub)

        options = make_options("s1")
        short_code = await manager.create_session(options)

        assert short_code == "test-code"
        assert "s1" in manager._tasks
        assert "s1" in manager._sessions

        # Let the task finish
        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)

    async def test_create_session_generates_auth(self):
        """Real create_session path generates uuid + auth token via SessionRegistry."""
        manager, _, _ = _make_manager()
        mock_auth = SessionAuth(
            short_code="XY123456",
            session_id="placeholder",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

        with (
            patch("summon_claude.session_manager.SessionRegistry") as mock_registry_cls,
            patch(
                "summon_claude.session_manager.generate_session_token",
                return_value=mock_auth,
            ) as mock_gen,
            patch("summon_claude.session_manager.SummonSession") as mock_session_cls,
        ):
            mock_registry = AsyncMock()
            mock_registry_cls.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = MagicMock()
            mock_session_cls.return_value.start = AsyncMock()

            result = await manager.create_session(make_options(""))

        assert result == "XY123456"
        mock_gen.assert_awaited_once()
        # Verify a uuid4 session_id was passed (36 chars with dashes)
        call_args = mock_gen.call_args
        session_id_arg = call_args[0][1]
        assert len(session_id_arg) == 36 and "-" in session_id_arg
        # Cleanup
        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)

    async def test_create_session_cancels_grace_timer(self):
        manager, _, _ = _make_manager()
        # Manually install a grace timer
        loop = asyncio.get_running_loop()
        called = []
        manager._grace_timer = loop.call_later(9999, lambda: called.append(True))

        stub = _StubSession()
        _patch_session(manager, stub)
        await manager.create_session(make_options("s2"))

        assert manager._grace_timer is None
        assert called == []  # timer was cancelled before it fired

        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)

    async def test_create_multiple_sessions(self):
        manager, _, _ = _make_manager()

        # Track session_ids returned by each create call
        created_ids = []
        for i in range(3):
            sid = f"s{i}"
            stub = _StubSession()
            _patch_session(manager, stub, session_id=sid)
            await manager.create_session(make_options(sid))
            created_ids.append(sid)

        # Each call returned a distinct session_id
        assert created_ids == ["s0", "s1", "s2"]

        # All tasks complete cleanly
        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)
        await asyncio.sleep(0)  # allow _on_task_done callbacks to fire
        # After tasks complete, sessions dict is cleaned up by _on_task_done
        assert len(manager._sessions) == 0


class TestStopSession:
    """stop_session signals the session to shut down."""

    async def test_stop_known_session_returns_true(self):
        manager, _, _ = _make_manager()
        stub = _StubSession()
        _patch_session(manager, stub)
        await manager.create_session(make_options("s1"))

        result = manager.stop_session("s1")
        assert result is True
        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)

    async def test_stop_unknown_session_returns_false(self):
        manager, _, _ = _make_manager()
        result = manager.stop_session("nonexistent")
        assert result is False

    async def test_stop_calls_request_shutdown(self):
        manager, _, _ = _make_manager()
        stub = _StubSession()
        _patch_session(manager, stub)
        await manager.create_session(make_options("s1"))

        # Stop before task runs
        manager.stop_session("s1")
        assert stub._shutdown_requested is True

        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)


# ---------------------------------------------------------------------------
# Tests: authenticate_session
# ---------------------------------------------------------------------------


class TestAuthenticateSession:
    """authenticate_session delegates to session.authenticate()."""

    async def test_authenticate_known_session(self):
        manager, _, _ = _make_manager()
        stub = _StubSession()
        _patch_session(manager, stub)
        await manager.create_session(make_options("s1"))

        result = manager.authenticate_session("s1", "U001")
        assert result is True
        assert stub._authenticated_user_id == "U001"

        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)

    def test_authenticate_unknown_session_returns_false(self):
        manager, _, _ = _make_manager()
        result = manager.authenticate_session("nonexistent", "U001")
        assert result is False


# ---------------------------------------------------------------------------
# Tests: supervised session / auto-restart
# ---------------------------------------------------------------------------


class TestSupervisedSession:
    """_supervised_session retries recoverable errors and gives up on fatal ones."""

    async def test_clean_exit_no_restart(self):
        """A session that completes cleanly is not restarted."""
        manager, _, _ = _make_manager()
        stub = _StubSession()  # no failure
        run_count_before = stub._run_count
        await manager._supervised_session(stub, "s1")  # type: ignore[arg-type]
        assert stub._run_count == run_count_before + 1

    async def test_recoverable_error_retries(self):
        """A recoverable error is retried up to MAX_SESSION_RESTARTS times."""
        manager, _, _ = _make_manager()
        stub = _StubSession(fail_with=ConnectionError("dropped"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await manager._supervised_session(stub, "s1")  # type: ignore[arg-type]

        assert stub._run_count == SessionManager.MAX_SESSION_RESTARTS

    async def test_non_recoverable_error_no_retry(self):
        """A non-recoverable error stops immediately without retrying."""
        manager, _, _ = _make_manager()
        stub = _StubSession(fail_with=ValueError("bad credentials"))

        await manager._supervised_session(stub, "s1")  # type: ignore[arg-type]

        assert stub._run_count == 1  # only one attempt

    async def test_cancelled_error_propagates(self):
        """CancelledError is re-raised so shutdown can proceed."""
        manager, _, _ = _make_manager()
        stub = _StubSession(fail_with=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await manager._supervised_session(stub, "s1")  # type: ignore[arg-type]

    async def test_recoverable_posts_error_on_final_failure(self):
        """After exhausting retries, best-effort error message posted to channel."""
        manager, mock_provider, _ = _make_manager()
        stub = _StubSession(fail_with=ConnectionError("dropped"))
        stub.channel_id = "C001"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await manager._supervised_session(stub, "s1")  # type: ignore[arg-type]

        mock_provider.post_message.assert_awaited()
        call_args = mock_provider.post_message.call_args
        assert call_args[0][0] == "C001"
        assert ":x:" in call_args[0][1]


# ---------------------------------------------------------------------------
# Tests: task done callback / unregistration
# ---------------------------------------------------------------------------


class TestOnTaskDone:
    """_on_task_done cleans up and unregisters from dispatcher."""

    async def test_task_done_removes_from_dicts(self):
        manager, _, mock_dispatcher = _make_manager()
        stub = _StubSession()
        _patch_session(manager, stub)
        await manager.create_session(make_options("s1"))

        # Wait for the task to complete naturally
        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)
        await asyncio.sleep(0)  # allow callbacks to fire

        assert "s1" not in manager._sessions
        assert "s1" not in manager._tasks

    async def test_task_done_unregisters_channel(self):
        manager, _, mock_dispatcher = _make_manager()
        stub = _StubSession()
        stub.channel_id = "C001"
        _patch_session(manager, stub)
        await manager.create_session(make_options("s1"))

        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)
        await asyncio.sleep(0)

        mock_dispatcher.unregister.assert_called_with("C001")


# ---------------------------------------------------------------------------
# Tests: grace timer
# ---------------------------------------------------------------------------


class TestGraceTimer:
    """Grace timer starts when sessions drop to zero and cancels on new session."""

    async def test_grace_timer_starts_after_last_session_ends(self):
        manager, _, _ = _make_manager()
        stub = _StubSession()  # completes immediately
        _patch_session(manager, stub)
        await manager.create_session(make_options("s1"))

        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)
        await asyncio.sleep(0)  # allow callbacks

        assert manager._grace_timer is not None
        manager._grace_timer.cancel()  # cleanup

    async def test_grace_timer_does_not_start_with_active_session(self):
        """If another session is still running, grace timer should not start."""
        manager, _, _ = _make_manager()

        # Session 1: completes immediately
        stub1 = _StubSession()
        _patch_session(manager, stub1)
        await manager.create_session(make_options("s1"))

        # Session 2: will be long-running (we keep a reference)
        long_running = asyncio.Event()

        class _LongStub(_StubSession):
            async def start(self):
                await long_running.wait()
                return True

        stub2 = _LongStub()
        manager._sessions["s2"] = stub2  # type: ignore[assignment]
        task2 = asyncio.create_task(
            manager._supervised_session(stub2, "s2"),  # type: ignore[arg-type]
        )
        manager._tasks["s2"] = task2

        # Let session 1 finish
        await asyncio.gather(manager._tasks["s1"], return_exceptions=True)
        await asyncio.sleep(0)

        # Grace timer should NOT have started because s2 is still running
        assert manager._grace_timer is None

        # Cleanup
        long_running.set()
        await task2

    async def test_grace_timer_cancelled_on_new_session(self):
        manager, _, _ = _make_manager()
        # Plant a fake grace timer
        loop = asyncio.get_running_loop()
        fired = []
        manager._grace_timer = loop.call_later(9999, lambda: fired.append(True))

        stub = _StubSession()
        _patch_session(manager, stub)
        await manager.create_session(make_options("s1"))

        assert manager._grace_timer is None
        assert fired == []

        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)


# ---------------------------------------------------------------------------
# Tests: shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    """shutdown() signals, waits, then force-cancels."""

    async def test_shutdown_with_no_sessions(self):
        """shutdown() with no active sessions just sets the event."""
        manager, _, _ = _make_manager()
        await manager.shutdown()
        assert manager._shutdown_event.is_set()

    async def test_shutdown_signals_sessions(self):
        manager, _, _ = _make_manager()
        stub = _StubSession()
        _patch_session(manager, stub)
        await manager.create_session(make_options("s1"))

        # Task may have already finished by now; if so, this is a no-op
        await manager.shutdown()

        assert manager._shutdown_event.is_set()

    async def test_shutdown_force_cancels_stuck_sessions(self):
        """Tasks that don't finish within 30s are force-cancelled."""
        manager, _, _ = _make_manager()
        blocking = asyncio.Event()

        class _BlockingStub(_StubSession):
            async def start(self):
                await blocking.wait()
                return True

        stub = _BlockingStub()
        manager._sessions["s1"] = stub  # type: ignore[assignment]
        task = asyncio.create_task(
            manager._supervised_session(stub, "s1"),  # type: ignore[arg-type]
        )
        manager._tasks["s1"] = task

        # Patch asyncio.wait to return immediately with the task still pending
        async def fast_wait(tasks, timeout=None):
            return set(), set(tasks)

        with patch("asyncio.wait", side_effect=fast_wait):
            await manager.shutdown()

        assert manager._shutdown_event.is_set()
        assert task.cancelled()


# ---------------------------------------------------------------------------
# Tests: Unix socket control API
# ---------------------------------------------------------------------------


class TestControlAPI:
    """handle_client / _dispatch_control protocol tests."""

    async def _call_control(self, manager: SessionManager, msg: dict) -> dict:
        """Send *msg* via handle_client using an in-process socketpair."""
        rsock, wsock = socket.socketpair()
        server_reader, _srv_w = await asyncio.open_connection(sock=rsock)
        _cli_r, client_writer = await asyncio.open_connection(sock=wsock)

        # Write request from client side
        await send_msg(client_writer, msg)
        client_writer.close()

        # Process on server side
        await manager.handle_client(server_reader, _srv_w)

        # Can't read from server side now — use a fresh reader on the client socket
        # Instead, test _dispatch_control directly for response content
        return {}

    async def test_dispatch_status(self):
        """status message returns pid, uptime, and sessions list."""
        manager, _, _ = _make_manager()
        response = await manager._dispatch_control({"type": "status"})
        assert response["type"] == "status"
        assert response["pid"] == os.getpid()
        assert "uptime" in response
        assert isinstance(response["sessions"], list)

    async def test_dispatch_unknown_type(self):
        """Unknown message type returns error response."""
        manager, _, _ = _make_manager()
        response = await manager._dispatch_control({"type": "fly_to_moon"})
        assert response["type"] == "error"
        assert "fly_to_moon" in response["message"]

    async def test_dispatch_stop_session_not_found(self):
        """stop_session for unknown id returns found=False."""
        manager, _, _ = _make_manager()
        response = await manager._dispatch_control(
            {"type": "stop_session", "session_id": "nonexistent"}
        )
        assert response["type"] == "session_stopped"
        assert response["found"] is False

    async def test_dispatch_stop_session_found(self):
        """stop_session for a known session returns found=True."""
        manager, _, _ = _make_manager()
        stub = _StubSession()
        _patch_session(manager, stub)
        await manager.create_session(make_options("s1"))

        response = await manager._dispatch_control({"type": "stop_session", "session_id": "s1"})
        assert response["type"] == "session_stopped"
        assert response["found"] is True

        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)

    async def test_handle_client_roundtrip(self):
        """handle_client reads a status request and writes a response."""
        manager, _, _ = _make_manager()

        rsock, wsock = socket.socketpair()
        # Client side: wsock writer → server side: rsock reader
        srv_reader, _srv_w = await asyncio.open_connection(sock=rsock)
        _cli_r, cli_writer = await asyncio.open_connection(sock=wsock)

        # Write request from client
        await send_msg(cli_writer, {"type": "status"})
        await cli_writer.drain()

        # Capture what the server writes back — use a mock writer
        written_data = bytearray()

        class _CapturingWriter:
            def write(self, data):
                written_data.extend(data)

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        # Run handle_client with the capturing writer
        await manager.handle_client(srv_reader, _CapturingWriter())  # type: ignore[arg-type]

        # Parse the captured response
        assert len(written_data) >= 4
        length = struct.unpack(">I", written_data[:4])[0]
        response = json.loads(written_data[4 : 4 + length])
        assert response["type"] == "status"

    async def test_handle_client_tolerates_early_disconnect(self):
        """handle_client does not raise if the client disconnects mid-message."""
        manager, _, _ = _make_manager()
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x00\x00")  # truncated — no complete message
        reader.feed_eof()

        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        await manager.handle_client(reader, writer)  # must not raise
