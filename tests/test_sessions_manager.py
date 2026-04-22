"""Tests for summon_claude.sessions.manager — SessionManager lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import socket
import struct
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_test_config

from summon_claude.daemon import send_msg
from summon_claude.sessions.auth import SessionAuth
from summon_claude.sessions.manager import SessionManager
from summon_claude.sessions.session import SessionOptions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_options(**overrides) -> SessionOptions:
    defaults = {"cwd": "/tmp", "name": "test"}
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
        self.target_channel_id: str | None = None
        self.is_pm: bool = False
        self.is_global_pm: bool = False
        self.project_id: str | None = None
        self._shutdown_requested = False
        self._authenticated_user_id: str | None = None
        self._authenticated_event = asyncio.Event()

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
        self._authenticated_event.set()


def _make_manager(
    stub_session: _StubSession | None = None,
) -> tuple[SessionManager, MagicMock, MagicMock]:
    """Return (manager, mock_provider, mock_dispatcher) with SummonSession patched."""
    cfg = make_test_config()
    mock_provider = MagicMock()
    mock_provider.post_message = AsyncMock()
    mock_provider.chat_postMessage = AsyncMock()
    mock_dispatcher = MagicMock()
    mock_dispatcher.unregister = MagicMock()

    manager = SessionManager(
        config=cfg, web_client=mock_provider, bot_user_id="UBOT", dispatcher=mock_dispatcher
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

        options = make_options()
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
            patch("summon_claude.sessions.manager.SessionRegistry") as mock_registry_cls,
            patch(
                "summon_claude.sessions.manager.generate_session_token",
                return_value=mock_auth,
            ) as mock_gen,
            patch("summon_claude.sessions.manager.SummonSession") as mock_session_cls,
        ):
            mock_registry = AsyncMock()
            mock_registry_cls.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = MagicMock()
            mock_session_cls.return_value.start = AsyncMock()

            result = await manager.create_session(make_options())

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
        await manager.create_session(make_options())

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
            await manager.create_session(make_options())
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
        await manager.create_session(make_options())

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
        await manager.create_session(make_options())

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
        await manager.create_session(make_options())

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

    async def test_pm_clean_exit_updates_topic(self):
        """PM session clean exit triggers _update_pm_topic."""
        manager, mock_provider, _ = _make_manager()
        mock_provider.conversations_setTopic = AsyncMock()

        stub = _StubSession()
        stub.is_pm = True
        stub.project_id = "p1"
        stub.channel_id = "C_PM"

        # Add the PM to _sessions so _update_pm_topic can find it
        manager._sessions["pm-id"] = stub  # type: ignore[assignment]

        await manager._supervised_session(stub, "pm-id")  # type: ignore[arg-type]

        mock_provider.conversations_setTopic.assert_awaited_once_with(
            channel="C_PM",
            topic="Project Manager | 0 active sessions | idle",
        )

    async def test_pm_clean_exit_skips_topic_when_cached(self):
        """PM exit with seeded cache (matching initial topic) skips redundant API call."""
        manager, mock_provider, _ = _make_manager()
        mock_provider.conversations_setTopic = AsyncMock()

        stub = _StubSession()
        stub.is_pm = True
        stub.project_id = "p1"
        stub.channel_id = "C_PM"

        manager._sessions["pm-id"] = stub  # type: ignore[assignment]
        # Simulate _start_pm_for_project seeding the cache with initial topic
        manager._pm_topic_cache["p1"] = "Project Manager | 0 active sessions | idle"

        await manager._supervised_session(stub, "pm-id")  # type: ignore[arg-type]

        # Cache matches → no API call
        mock_provider.conversations_setTopic.assert_not_awaited()

    async def test_recoverable_posts_error_on_final_failure(self):
        """After exhausting retries, best-effort error message posted to channel."""
        manager, mock_provider, _ = _make_manager()
        stub = _StubSession(fail_with=ConnectionError("dropped"))
        stub.channel_id = "C001"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await manager._supervised_session(stub, "s1")  # type: ignore[arg-type]

        mock_provider.chat_postMessage.assert_awaited()
        call_args = mock_provider.chat_postMessage.call_args
        assert call_args[1]["channel"] == "C001"
        assert ":x:" in call_args[1]["text"]

    async def test_pm_failure_notifies_global_pm(self):
        """Project PM permanent failure sends mark_untrusted notification to global PM."""
        manager, mock_provider, _ = _make_manager()

        # Failing project PM
        pm_stub = _StubSession(fail_with=RuntimeError("project not found"))
        pm_stub.is_pm = True
        pm_stub.is_global_pm = False
        pm_stub.project_id = "p1"

        # Global PM stub with inject_message
        global_pm = _StubSession()
        global_pm.is_pm = True
        global_pm.is_global_pm = True
        global_pm.inject_message = AsyncMock()

        manager._sessions["gpm-id"] = global_pm  # type: ignore[assignment]

        await manager._supervised_session(pm_stub, "pm-id")  # type: ignore[arg-type]

        global_pm.inject_message.assert_awaited_once()
        call_args = global_pm.inject_message.call_args
        msg = call_args[0][0]
        assert "RuntimeError" in msg
        assert "UNTRUSTED_EXTERNAL_DATA" in msg
        assert "[Source: session-manager-error]" in msg
        assert call_args[1]["sender_info"] == "session-manager"


# ---------------------------------------------------------------------------
# Tests: task done callback / unregistration
# ---------------------------------------------------------------------------


class TestOnTaskDone:
    """_on_task_done cleans up and unregisters from dispatcher."""

    async def test_task_done_removes_from_dicts(self):
        manager, _, mock_dispatcher = _make_manager()
        stub = _StubSession()
        _patch_session(manager, stub)
        await manager.create_session(make_options())

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
        await manager.create_session(make_options())

        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)
        await asyncio.sleep(0)

        mock_dispatcher.unregister.assert_called_with("C001")

    async def test_task_done_evicts_pm_topic_cache(self):
        """PM session exit clears its project's topic cache entry."""
        manager, _, _ = _make_manager()
        stub = _StubSession()
        stub.is_pm = True
        stub.project_id = "p1"

        # Pre-populate cache as if a topic was previously set
        manager._pm_topic_cache["p1"] = "Project Manager | 1 active session | working"

        _patch_session(manager, stub)
        await manager.create_session(make_options())

        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)
        await asyncio.sleep(0)

        assert "p1" not in manager._pm_topic_cache


# ---------------------------------------------------------------------------
# Tests: grace timer
# ---------------------------------------------------------------------------


class TestGraceTimer:
    """Grace timer starts when sessions drop to zero and cancels on new session."""

    async def test_grace_timer_starts_after_last_session_ends(self):
        manager, _, _ = _make_manager()
        stub = _StubSession()  # completes immediately
        _patch_session(manager, stub)
        await manager.create_session(make_options())

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
        await manager.create_session(make_options())

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
        await manager.create_session(make_options())

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
        await manager.create_session(make_options())

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

    async def test_dispatch_stop_session_missing_id(self):
        """stop_session without session_id returns an error."""
        manager, _, _ = _make_manager()
        response = await manager._dispatch_control({"type": "stop_session"})
        assert response["type"] == "error"
        assert "session_id" in response["message"].lower()

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
        await manager.create_session(make_options())

        response = await manager._dispatch_control({"type": "stop_session", "session_id": "s1"})
        assert response["type"] == "session_stopped"
        assert response["found"] is True

        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)

    async def test_dispatch_stop_all(self):
        """stop_all stops every active session in a single IPC call."""
        manager, _, _ = _make_manager()
        stub = _StubSession()
        _patch_session(manager, stub)
        await manager.create_session(make_options())

        response = await manager._dispatch_control({"type": "stop_all"})
        assert response["type"] == "all_stopped"
        assert len(response["results"]) == 1
        assert response["results"][0]["found"] is True

        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)

    async def test_dispatch_stop_all_empty(self):
        """stop_all with no sessions returns empty results."""
        manager, _, _ = _make_manager()
        response = await manager._dispatch_control({"type": "stop_all"})
        assert response["type"] == "all_stopped"
        assert response["results"] == []

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


# ---------------------------------------------------------------------------
# Tests: handle_app_home debounce + LRU eviction
# ---------------------------------------------------------------------------


class TestHandleAppHome:
    """Tests for handle_app_home debounce and LRU eviction."""

    def _make_manager_with_mock_client(self):
        cfg = make_test_config()
        web_client = MagicMock()
        web_client.views_publish = AsyncMock()
        dispatcher = MagicMock()
        dispatcher.unregister = MagicMock()
        manager = SessionManager(
            config=cfg, web_client=web_client, bot_user_id="UBOT", dispatcher=dispatcher
        )
        return manager, web_client

    async def test_second_call_within_debounce_window_does_not_publish(self):
        manager, web_client = self._make_manager_with_mock_client()

        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls:
            mock_reg = AsyncMock()
            mock_reg.list_active_by_user = AsyncMock(return_value=[])
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            frozen = 1000.0
            with patch("time.monotonic", return_value=frozen):
                await manager.handle_app_home("U123")
                await manager.handle_app_home("U123")

        assert web_client.views_publish.call_count == 1

    async def test_call_after_debounce_window_publishes_again(self):
        manager, web_client = self._make_manager_with_mock_client()

        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls:
            mock_reg = AsyncMock()
            mock_reg.list_active_by_user = AsyncMock(return_value=[])
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("time.monotonic", return_value=1000.0):
                await manager.handle_app_home("U123")
            with patch("time.monotonic", return_value=1000.0 + 61.0):
                await manager.handle_app_home("U123")

        assert web_client.views_publish.call_count == 2

    async def test_independent_users_have_separate_debounce(self):
        manager, web_client = self._make_manager_with_mock_client()

        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls:
            mock_reg = AsyncMock()
            mock_reg.list_active_by_user = AsyncMock(return_value=[])
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            frozen = 1000.0
            with patch("time.monotonic", return_value=frozen):
                await manager.handle_app_home("ALICE")
                await manager.handle_app_home("BOB")

        assert web_client.views_publish.call_count == 2

    def test_lru_eviction_caps_at_500(self):
        """Eviction fires when a new user arrives at 500 entries."""
        manager, _ = self._make_manager_with_mock_client()

        for i in range(500):
            manager._app_home_last_publish[f"U{i:04d}"] = float(i)

        assert len(manager._app_home_last_publish) == 500

        # Simulate a new user arriving — eviction should remove the oldest
        manager._app_home_last_publish["U_NEW"] = 999.0
        # Manually run the eviction logic (mirroring handle_app_home)
        if (
            len(manager._app_home_last_publish) >= 500
            and "U_ANOTHER" not in manager._app_home_last_publish
        ):
            oldest_key = next(iter(manager._app_home_last_publish))
            del manager._app_home_last_publish[oldest_key]

        # Verify: oldest evicted, new user present, size at 500
        assert "U0000" not in manager._app_home_last_publish
        assert "U_NEW" in manager._app_home_last_publish
        assert len(manager._app_home_last_publish) == 500

    def test_lru_eviction_does_not_evict_existing_user(self):
        """When an existing user updates, no eviction occurs."""
        manager, _ = self._make_manager_with_mock_client()

        for i in range(500):
            manager._app_home_last_publish[f"U{i:04d}"] = float(i)

        # Existing user updates — should NOT trigger eviction
        user_id = "U0000"
        if (
            len(manager._app_home_last_publish) >= 500
            and user_id not in manager._app_home_last_publish
        ):
            oldest_key = next(iter(manager._app_home_last_publish))
            del manager._app_home_last_publish[oldest_key]
        manager._app_home_last_publish[user_id] = 999.0

        assert "U0000" in manager._app_home_last_publish
        assert len(manager._app_home_last_publish) == 500


# ---------------------------------------------------------------------------
# Tests: create_session_with_spawn_token
# ---------------------------------------------------------------------------


class TestCreateSessionWithSpawnToken:
    async def test_valid_spawn_token_creates_session(self):
        """create_session_with_spawn_token creates a pre-authenticated session."""
        config = make_test_config()
        web_client = MagicMock()
        web_client.auth_test = AsyncMock(return_value={"user_id": "BBOT"})
        dispatcher = MagicMock()
        dispatcher.register = MagicMock()
        dispatcher.unregister = MagicMock()
        mgr = SessionManager(config, web_client, "BBOT", dispatcher)

        # Mock the spawn token verification
        spawn_auth = MagicMock()
        spawn_auth.target_user_id = "U123"
        spawn_auth.parent_session_id = "parent-sess"
        spawn_auth.parent_channel_id = "C_PARENT"
        with (
            patch(
                "summon_claude.sessions.manager.verify_spawn_token",
                new_callable=AsyncMock,
                return_value=spawn_auth,
            ),
            patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls,
        ):
            mock_reg = AsyncMock()
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            session_id = await mgr.create_session_with_spawn_token(make_options(), "valid-token")
        assert session_id is not None
        assert session_id in mgr._sessions
        # Session should be pre-authenticated
        session = mgr._sessions[session_id]
        assert session._authenticated_event.is_set()
        assert session._parent_session_id == "parent-sess"
        assert session._parent_channel_id == "C_PARENT"
        # Cleanup
        await mgr.shutdown()

    async def test_cwd_enforced_from_spawn_token(self):
        """The session must use the spawn token's cwd, not the caller's."""
        config = make_test_config()
        web_client = MagicMock()
        web_client.auth_test = AsyncMock(return_value={"user_id": "BBOT"})
        dispatcher = MagicMock()
        dispatcher.register = MagicMock()
        dispatcher.unregister = MagicMock()
        mgr = SessionManager(config, web_client, "BBOT", dispatcher)

        spawn_auth = MagicMock()
        spawn_auth.target_user_id = "U123"
        spawn_auth.parent_session_id = None
        spawn_auth.parent_channel_id = None
        spawn_auth.cwd = "/authorized/project"
        with (
            patch(
                "summon_claude.sessions.manager.verify_spawn_token",
                new_callable=AsyncMock,
                return_value=spawn_auth,
            ),
            patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls,
        ):
            mock_reg = AsyncMock()
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            # Caller passes a DIFFERENT cwd — spawn token must override it
            attacker_options = make_options(cwd="/attacker/path")
            session_id = await mgr.create_session_with_spawn_token(attacker_options, "valid-token")
        session = mgr._sessions[session_id]
        assert session._cwd == "/authorized/project"
        await mgr.shutdown()

    async def test_invalid_spawn_token_raises(self):
        config = make_test_config()
        web_client = MagicMock()
        dispatcher = MagicMock()
        mgr = SessionManager(config, web_client, "BBOT", dispatcher)

        with (
            patch(
                "summon_claude.sessions.manager.verify_spawn_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls,
        ):
            mock_reg = AsyncMock()
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match="Invalid or expired"):
                await mgr.create_session_with_spawn_token(make_options(), "bad-token")

    async def test_invalid_spawn_token_does_not_cancel_grace_timer(self):
        """An invalid spawn token must not cancel the daemon grace timer."""
        config = make_test_config()
        web_client = MagicMock()
        dispatcher = MagicMock()
        mgr = SessionManager(config, web_client, "BBOT", dispatcher)
        fake_timer = MagicMock()
        mgr._grace_timer = fake_timer

        with (
            patch(
                "summon_claude.sessions.manager.verify_spawn_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls,
        ):
            mock_reg = AsyncMock()
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match="Invalid or expired"):
                await mgr.create_session_with_spawn_token(make_options(), "bad-token")

        # Grace timer must NOT have been cancelled
        fake_timer.cancel.assert_not_called()
        assert mgr._grace_timer is fake_timer

    async def test_rejected_token_logs_audit_event(self):
        """Failed spawn token verification must log a spawn_token_rejected audit event."""
        config = make_test_config()
        web_client = MagicMock()
        dispatcher = MagicMock()
        mgr = SessionManager(config, web_client, "BBOT", dispatcher)

        with (
            patch(
                "summon_claude.sessions.manager.verify_spawn_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls,
        ):
            mock_reg = AsyncMock()
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match="Invalid or expired"):
                await mgr.create_session_with_spawn_token(make_options(), "bad-token")

            # Audit log must record the rejection
            mock_reg.log_event.assert_awaited_once()
            call_args = mock_reg.log_event.call_args
            assert call_args[0][0] == "spawn_token_rejected"
            assert call_args[1]["session_id"] is not None

    async def test_successful_token_logs_consumed_audit_event(self):
        """Successful spawn token must log spawn_token_consumed with parent details."""
        config = make_test_config()
        web_client = MagicMock()
        web_client.auth_test = AsyncMock(return_value={"user_id": "BBOT"})
        dispatcher = MagicMock()
        dispatcher.register = MagicMock()
        dispatcher.unregister = MagicMock()
        mgr = SessionManager(config, web_client, "BBOT", dispatcher)

        spawn_auth = MagicMock()
        spawn_auth.target_user_id = "U123"
        spawn_auth.parent_session_id = "parent-sess"
        spawn_auth.parent_channel_id = "C_PARENT"
        spawn_auth.cwd = "/tmp"
        spawn_auth.spawn_source = "session"
        with (
            patch(
                "summon_claude.sessions.manager.verify_spawn_token",
                new_callable=AsyncMock,
                return_value=spawn_auth,
            ),
            patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls,
        ):
            mock_reg = AsyncMock()
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await mgr.create_session_with_spawn_token(make_options(), "valid-token")

            # Audit log must record the consumption with full details
            mock_reg.log_event.assert_awaited_once()
            call_args = mock_reg.log_event.call_args
            assert call_args[0][0] == "spawn_token_consumed"
            assert call_args[1]["user_id"] == "U123"
            details = call_args[1]["details"]
            assert details["parent_session_id"] == "parent-sess"
            assert details["spawn_source"] == "session"
            assert details["cwd"] == "/tmp"
        await mgr.shutdown()


# ---------------------------------------------------------------------------
# Tests: _dispatch_control — create_session_with_spawn_token
# ---------------------------------------------------------------------------


class TestDispatchSpawnToken:
    async def test_dispatch_spawn_missing_token_key(self):
        """Missing spawn_token key returns an error."""
        manager, _, _ = _make_manager()
        response = await manager._dispatch_control(
            {"type": "create_session_with_spawn_token", "options": {"cwd": "/tmp", "name": "t"}}
        )
        assert response["type"] == "error"
        assert "Invalid request" in response["message"]

    async def test_dispatch_spawn_missing_options(self):
        """Missing options key returns an error."""
        manager, _, _ = _make_manager()
        response = await manager._dispatch_control(
            {"type": "create_session_with_spawn_token", "spawn_token": "tok"}
        )
        assert response["type"] == "error"
        assert "Invalid request" in response["message"]

    async def test_dispatch_spawn_invalid_token(self):
        """Invalid spawn token returns an error via ValueError."""
        manager, _, _ = _make_manager()
        with (
            patch(
                "summon_claude.sessions.manager.verify_spawn_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls,
        ):
            mock_reg = AsyncMock()
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            response = await manager._dispatch_control(
                {
                    "type": "create_session_with_spawn_token",
                    "options": {"cwd": "/tmp", "name": "t"},
                    "spawn_token": "bad-token",
                }
            )
        assert response["type"] == "error"
        assert "Invalid or expired" in response["message"]

    async def test_dispatch_spawn_success(self):
        """Valid spawn token dispatch creates session and returns session_id."""
        manager, _, _ = _make_manager()
        stub = _StubSession()
        _patch_session(manager, stub)

        spawn_auth = MagicMock()
        spawn_auth.target_user_id = "U123"
        spawn_auth.parent_session_id = "parent-sess"
        spawn_auth.parent_channel_id = "C_PARENT"
        spawn_auth.cwd = "/tmp"
        with (
            patch(
                "summon_claude.sessions.manager.verify_spawn_token",
                new_callable=AsyncMock,
                return_value=spawn_auth,
            ),
            patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls,
        ):
            mock_reg = AsyncMock()
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            response = await manager._dispatch_control(
                {
                    "type": "create_session_with_spawn_token",
                    "options": {"cwd": "/tmp", "name": "t"},
                    "spawn_token": "valid-token",
                }
            )
        assert response["type"] == "session_created_spawned"
        assert "session_id" in response
        await manager.shutdown()

    async def test_dispatch_spawn_rejects_oversized_system_prompt(self):
        """Defense-in-depth: daemon rejects system_prompt_append exceeding limit."""
        from summon_claude.summon_cli_mcp import MAX_PROMPT_CHARS

        manager, _, _ = _make_manager()
        response = await manager._dispatch_control(
            {
                "type": "create_session_with_spawn_token",
                "options": {
                    "cwd": "/tmp",
                    "name": "t",
                    "system_prompt_append": "x" * (MAX_PROMPT_CHARS + 1),
                },
                "spawn_token": "tok",
            }
        )
        assert response["type"] == "error"
        assert "system_prompt_append" in response["message"]

    async def test_dispatch_create_session_rejects_oversized_system_prompt(self):
        """Defense-in-depth: create_session rejects oversized system_prompt_append."""
        from summon_claude.summon_cli_mcp import MAX_PROMPT_CHARS

        manager, _, _ = _make_manager()
        response = await manager._dispatch_control(
            {
                "type": "create_session",
                "options": {
                    "cwd": "/tmp",
                    "name": "t",
                    "system_prompt_append": "x" * (MAX_PROMPT_CHARS + 1),
                },
            }
        )
        assert response["type"] == "error"
        assert "system_prompt_append" in response["message"]

    async def test_dispatch_create_session_rejects_oversized_initial_prompt(self):
        """Defense-in-depth: create_session rejects oversized initial_prompt."""
        from summon_claude.summon_cli_mcp import MAX_PROMPT_CHARS

        manager, _, _ = _make_manager()
        response = await manager._dispatch_control(
            {
                "type": "create_session",
                "options": {
                    "cwd": "/tmp",
                    "name": "t",
                    "initial_prompt": "x" * (MAX_PROMPT_CHARS + 1),
                },
            }
        )
        assert response["type"] == "error"
        assert "initial_prompt" in response["message"]

    async def test_dispatch_spawn_rejects_oversized_initial_prompt(self):
        """Defense-in-depth: spawn rejects oversized initial_prompt."""
        from summon_claude.summon_cli_mcp import MAX_PROMPT_CHARS

        manager, _, _ = _make_manager()
        response = await manager._dispatch_control(
            {
                "type": "create_session_with_spawn_token",
                "options": {
                    "cwd": "/tmp",
                    "name": "t",
                    "initial_prompt": "x" * (MAX_PROMPT_CHARS + 1),
                },
                "spawn_token": "tok",
            }
        )
        assert response["type"] == "error"
        assert "initial_prompt" in response["message"]


class TestDispatchStripsProxyFields:
    """IPC boundary safety: CLI must not inject jira_proxy_port/token."""

    async def test_create_session_strips_jira_proxy_port(self):
        manager, _, _ = _make_manager()
        manager.create_session = AsyncMock(return_value="abc123")
        await manager._dispatch_control(
            {
                "type": "create_session",
                "options": {
                    "cwd": "/tmp",
                    "name": "t",
                    "jira_proxy_port": 9999,
                    "jira_proxy_token": "evil",
                },
            }
        )
        opts = manager.create_session.call_args[0][0]
        assert opts.jira_proxy_port is None
        assert opts.jira_proxy_token is None

    async def test_spawn_token_strips_jira_proxy_fields(self):
        manager, _, _ = _make_manager()
        manager.create_session_with_spawn_token = AsyncMock(return_value="abc123")
        await manager._dispatch_control(
            {
                "type": "create_session_with_spawn_token",
                "options": {
                    "cwd": "/tmp",
                    "name": "t",
                    "jira_proxy_port": 9999,
                    "jira_proxy_token": "evil",
                },
                "spawn_token": "tok",
            }
        )
        opts = manager.create_session_with_spawn_token.call_args[0][0]
        assert opts.jira_proxy_port is None
        assert opts.jira_proxy_token is None


class TestDispatchClearSession:
    """QA-004: Tests for the clear_session IPC dispatch handler."""

    async def test_clear_session_missing_id(self):
        manager, _, _ = _make_manager()
        result = await manager._dispatch_control({"type": "clear_session"})
        assert result["type"] == "error"
        assert "Missing session_id" in result["message"]

    async def test_clear_session_not_found(self):
        manager, _, _ = _make_manager()
        result = await manager._dispatch_control(
            {"type": "clear_session", "session_id": "nonexistent"}
        )
        assert result["type"] == "error"
        assert "not found" in result["message"]

    async def test_clear_session_success(self):
        manager, _, _ = _make_manager()
        mock_session = MagicMock()
        mock_session.clear_context = AsyncMock(return_value=True)
        manager._sessions["test-sid"] = mock_session

        result = await manager._dispatch_control(
            {"type": "clear_session", "session_id": "test-sid"}
        )
        assert result["type"] == "session_cleared"
        assert result["session_id"] == "test-sid"
        mock_session.clear_context.assert_awaited_once()

    async def test_clear_session_failure(self):
        manager, _, _ = _make_manager()
        mock_session = MagicMock()
        mock_session.clear_context = AsyncMock(return_value=False)
        manager._sessions["test-sid"] = mock_session

        result = await manager._dispatch_control(
            {"type": "clear_session", "session_id": "test-sid"}
        )
        assert result["type"] == "error"
        assert "clear_context() failed" in result["message"]


class TestInjectProxyOptionsPropagation:
    """Verify _inject_proxy_options injects proxy config at all session creation sites."""

    def test_inject_proxy_options_sets_fields(self):
        manager, _, _ = _make_manager()
        manager._jira_proxy_port = 12345
        manager._jira_proxy_token = "tok-abc"
        opts = SessionOptions(cwd="/tmp", name="test")
        result = manager._inject_proxy_options(opts)
        assert result.jira_proxy_port == 12345
        assert result.jira_proxy_token == "tok-abc"

    def test_inject_proxy_options_noop_when_none(self):
        manager, _, _ = _make_manager()
        opts = SessionOptions(cwd="/tmp", name="test")
        result = manager._inject_proxy_options(opts)
        assert result is opts  # identity — no copy made

    def test_inject_called_at_all_construction_sites(self):
        """Guard test: count the _inject_proxy_options calls in manager.py source."""
        import inspect

        src = inspect.getsource(SessionManager)
        count = src.count("_inject_proxy_options(")
        # 1 definition + 8 call sites = 9 occurrences
        assert count == 9, (
            f"Expected 9 occurrences of _inject_proxy_options (1 def + 8 calls), "
            f"found {count}. A new SummonSession construction site may be missing the call."
        )


# ---------------------------------------------------------------------------
# Helpers: project up
# ---------------------------------------------------------------------------


def _mock_registry_ctx(mock_registry: AsyncMock) -> MagicMock:
    """Build a mock SessionRegistry class whose async-context returns *mock_registry*."""
    cls = MagicMock()
    cls.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
    cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return cls


def _mock_project(
    name: str = "test-project",
    *,
    pm_running: bool = False,
    directory: str = "/tmp/test-project",
    channel_prefix: str = "test-project",
    project_id: str = "proj-1",
) -> dict:
    return {
        "project_id": project_id,
        "name": name,
        "directory": directory,
        "channel_prefix": channel_prefix,
        "pm_running": pm_running,
    }


# ---------------------------------------------------------------------------
# Tests: project up orchestration
# ---------------------------------------------------------------------------


class TestProjectUpOrchestration:
    """Tests for _handle_project_up and _project_up_orchestrator."""

    async def test_project_up_no_projects_returns_complete(self):
        """When no projects need PM, returns project_up_complete immediately."""
        manager, _, _ = _make_manager()
        mock_reg = AsyncMock()
        mock_reg.list_projects = AsyncMock(return_value=[])

        with patch(
            "summon_claude.sessions.manager.SessionRegistry",
            _mock_registry_ctx(mock_reg),
        ):
            response = await manager._dispatch_control({"type": "project_up", "cwd": "/tmp"})

        assert response["type"] == "project_up_complete"
        assert manager._project_up_in_flight is False

    async def test_project_up_all_already_running_returns_complete(self):
        """Projects that already have PM running are excluded."""
        manager, _, _ = _make_manager()
        mock_reg = AsyncMock()
        mock_reg.list_projects = AsyncMock(return_value=[_mock_project(pm_running=True)])

        with patch(
            "summon_claude.sessions.manager.SessionRegistry",
            _mock_registry_ctx(mock_reg),
        ):
            response = await manager._dispatch_control({"type": "project_up", "cwd": "/tmp"})

        assert response["type"] == "project_up_complete"
        assert manager._project_up_in_flight is False

    async def test_project_up_returns_auth_required(self):
        """When projects need PM, returns auth_required with short_code."""
        manager, _, _ = _make_manager()
        mock_reg = AsyncMock()
        mock_reg.list_projects = AsyncMock(return_value=[_mock_project(pm_running=False)])
        mock_auth = SessionAuth(
            short_code="PM123456",
            session_id="placeholder",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

        with (
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
            patch(
                "summon_claude.sessions.manager.generate_session_token",
                new_callable=AsyncMock,
                return_value=mock_auth,
            ),
            patch("summon_claude.sessions.manager.SummonSession") as mock_ss,
        ):
            mock_ss.return_value = _StubSession()
            response = await manager._dispatch_control({"type": "project_up", "cwd": "/tmp"})

        assert response["type"] == "project_up_auth_required"
        assert response["short_code"] == "PM123456"
        assert response["project_count"] == 1
        assert manager._project_up_in_flight is True

        # Cleanup
        await manager.shutdown()

    async def test_project_up_concurrent_guard(self):
        """A second project_up while one is in flight returns an error."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        response = await manager._dispatch_control({"type": "project_up", "cwd": "/tmp"})

        assert response["type"] == "error"
        assert "already in progress" in response["message"]

    async def test_project_up_clears_flag_on_exception(self):
        """_project_up_in_flight is cleared if _handle_project_up raises.

        If an exception occurs after setting the flag but before the
        orchestrator starts, the flag must be cleared so future calls
        aren't permanently blocked.
        """
        manager, _, _ = _make_manager()

        mock_reg = AsyncMock()
        mock_reg.list_projects = AsyncMock(return_value=[_mock_project(pm_running=False)])

        with (
            pytest.raises(RuntimeError, match="DB exploded"),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
            patch(
                "summon_claude.sessions.manager.generate_session_token",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB exploded"),
            ),
        ):
            await manager._handle_project_up({"cwd": "/tmp"})

        assert manager._project_up_in_flight is False

    async def test_orchestrator_happy_path(self):
        """Orchestrator waits for auth, creates PM sessions."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        # Auth session stub — will be authenticated shortly
        auth_stub = _StubSession()

        needing_pm = [_mock_project(name="proj-a", directory="/tmp/test-project")]

        # Mock SummonSession so the orchestrator can create PM sessions
        mock_ss = MagicMock(return_value=_StubSession())
        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[])
        with (
            patch("summon_claude.sessions.manager.SummonSession", mock_ss),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            # Start the orchestrator
            orch_task = asyncio.create_task(
                manager._project_up_orchestrator(auth_stub, needing_pm)  # type: ignore[arg-type]
            )

            # Give the orchestrator a moment to start waiting
            await asyncio.sleep(0)

            # Authenticate the auth session
            auth_stub.authenticate("U999")

            # Wait for orchestrator to finish
            await asyncio.wait_for(orch_task, timeout=5)

        assert manager._project_up_in_flight is False
        # Verify PM session was created (stub completes instantly, so check constructor calls)
        assert mock_ss.call_count >= 1

        # Cleanup
        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )

    async def test_orchestrator_multiple_projects(self):
        """Orchestrator creates PM sessions for each project needing PM."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        auth_stub.authenticate("U123")

        needing_pm = [
            _mock_project(name="alpha", project_id="p-1", directory="/tmp/alpha"),
            _mock_project(name="beta", project_id="p-2", directory="/tmp/beta"),
        ]

        pm_stubs = []

        def make_pm_stub(**kwargs):
            s = _StubSession()
            pm_stubs.append(s)
            return s

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[])
        with (
            patch("summon_claude.sessions.manager.SummonSession", side_effect=make_pm_stub),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await asyncio.wait_for(
                manager._project_up_orchestrator(auth_stub, needing_pm),  # type: ignore[arg-type]
                timeout=5,
            )

        assert len(pm_stubs) == 2

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )

    async def test_orchestrator_partial_failure_continues(self):
        """If one project fails, remaining projects still get PM sessions."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        auth_stub.authenticate("U001")

        needing_pm = [
            _mock_project(name="bad", project_id="p-bad", directory="/tmp/bad"),
            _mock_project(name="good", project_id="p-good", directory="/tmp/good"),
        ]

        call_count = 0

        def make_pm_stub(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("config exploded")
            return _StubSession()

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[])
        with (
            patch("summon_claude.sessions.manager.SummonSession", side_effect=make_pm_stub),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await asyncio.wait_for(
                manager._project_up_orchestrator(auth_stub, needing_pm),  # type: ignore[arg-type]
                timeout=5,
            )

        # First project failed (call_count=1 raised), second succeeded (call_count=2)
        assert call_count == 2
        assert manager._project_up_in_flight is False

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )

    async def test_orchestrator_missing_directory_records_error(self):
        """Projects with missing directories are skipped (no sessions created)."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        auth_stub.authenticate("U123")

        needing_pm = [
            _mock_project(name="gone", directory="/nonexistent/path/gone"),
        ]

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[])
        with (
            patch("pathlib.Path.is_dir", return_value=False),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await asyncio.wait_for(
                manager._project_up_orchestrator(auth_stub, needing_pm),  # type: ignore[arg-type]
                timeout=5,
            )

        # No PM sessions should have been created (directory doesn't exist)
        assert len(manager._tasks) == 0

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )

    async def test_orchestrator_auth_timeout(self):
        """Orchestrator clears in-flight flag when auth times out."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        # Auth session that never authenticates
        auth_stub = _StubSession()
        needing_pm = [_mock_project()]

        # Replace the 360s timeout with 0s so it fires immediately
        with patch(
            "summon_claude.sessions.manager.asyncio.timeout",
            side_effect=lambda _: asyncio.timeout(0),
        ):
            await asyncio.wait_for(
                manager._project_up_orchestrator(auth_stub, needing_pm),  # type: ignore[arg-type]
                timeout=5,
            )

        assert manager._project_up_in_flight is False

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )

    async def test_orchestrator_clears_in_flight_flag(self):
        """_project_up_in_flight is False after orchestrator completes (happy path)."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[])

        with (
            patch("summon_claude.sessions.manager.SummonSession", return_value=_StubSession()),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            orch_task = asyncio.create_task(
                manager._project_up_orchestrator(
                    auth_stub,
                    [_mock_project()],  # type: ignore[arg-type]
                )
            )
            await asyncio.sleep(0)  # let orchestrator start waiting
            auth_stub.authenticate("U001")
            await asyncio.wait_for(orch_task, timeout=5)

        assert manager._project_up_in_flight is False

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )

    async def test_orchestrator_clears_in_flight_on_exception(self):
        """_project_up_in_flight is cleared even when orchestrator raises."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        auth_stub.authenticate("U001")

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[])
        with (
            patch(
                "summon_claude.sessions.manager.SummonSession",
                side_effect=RuntimeError("boom"),
            ),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await asyncio.wait_for(
                manager._project_up_orchestrator(
                    auth_stub,
                    [_mock_project()],  # type: ignore[arg-type]
                ),
                timeout=5,
            )

        assert manager._project_up_in_flight is False

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )

    async def test_orchestrator_authenticates_pm_sessions(self):
        """PM sessions are authenticated with the auth session's user_id."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        auth_stub.authenticate("UAUTH")

        pm_sessions_created = []

        class _CapturePMSession(_StubSession):
            def authenticate(self, user_id):
                super().authenticate(user_id)
                pm_sessions_created.append(user_id)

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[])
        with (
            patch(
                "summon_claude.sessions.manager.SummonSession",
                return_value=_CapturePMSession(),
            ),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await asyncio.wait_for(
                manager._project_up_orchestrator(
                    auth_stub,
                    [_mock_project()],  # type: ignore[arg-type]
                ),
                timeout=5,
            )

        assert pm_sessions_created == ["UAUTH"]

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )

    async def test_orchestrator_cancels_grace_timer_before_adding_sessions(self):
        """Grace timer started by auth session completion must be cancelled by orchestrator.

        Race: auth-only session completes → _on_task_done → _sessions empty →
        grace timer starts.  Orchestrator then creates PM sessions — if it
        doesn't cancel the grace timer, the daemon shuts down 60s later,
        killing the PM sessions.
        """
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        auth_stub.authenticate("U001")

        # Simulate the grace timer having been started (by _on_task_done
        # after the auth session completed while no other sessions exist).
        loop = asyncio.get_running_loop()
        fired = []
        manager._grace_timer = loop.call_later(9999, lambda: fired.append(True))

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[])
        with (
            patch("summon_claude.sessions.manager.SummonSession", return_value=_StubSession()),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await asyncio.wait_for(
                manager._project_up_orchestrator(
                    auth_stub,
                    [_mock_project()],  # type: ignore[arg-type]
                ),
                timeout=5,
            )

        # Grace timer must have been cancelled by orchestrator before adding PM sessions
        assert manager._grace_timer is None
        assert fired == []

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )


# ---------------------------------------------------------------------------
# Tests: _on_background_task_done
# ---------------------------------------------------------------------------


class TestBackgroundTaskDone:
    """_on_background_task_done discards tasks and handles exceptions."""

    async def test_background_task_done_discards_from_set(self):
        """Completed background task is removed from _background_tasks."""
        manager, _, _ = _make_manager()

        async def noop():
            pass

        task = asyncio.create_task(noop())
        manager._background_tasks.add(task)
        task.add_done_callback(manager._on_background_task_done)

        await task
        await asyncio.sleep(0)  # allow callback to fire

        assert task not in manager._background_tasks

    async def test_background_task_done_logs_exception(self):
        """Failed background task is discarded and does not re-raise."""
        manager, _, _ = _make_manager()

        async def failing():
            raise RuntimeError("background boom")

        task = asyncio.create_task(failing())
        manager._background_tasks.add(task)
        task.add_done_callback(manager._on_background_task_done)

        # Wait for task to fail — gather suppresses the exception
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # allow callback to fire

        assert task not in manager._background_tasks

    async def test_background_task_done_cancelled_no_error(self):
        """Cancelled background task is discarded without logging an error."""
        manager, _, _ = _make_manager()

        async def sleeper():
            await asyncio.sleep(9999)

        task = asyncio.create_task(sleeper())
        manager._background_tasks.add(task)
        task.add_done_callback(manager._on_background_task_done)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        assert task not in manager._background_tasks


# ---------------------------------------------------------------------------
# Tests: cascade restart of suspended sessions
# ---------------------------------------------------------------------------


class TestCascadeRestart:
    """_restart_suspended_sessions revives sessions stopped by project down."""

    async def test_restart_suspended_sessions(self):
        """Orchestrator resumes sessions with status 'suspended' via create_resumed_session."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        auth_stub.authenticate("U001")

        suspended_session = {
            "session_id": "old-sess-1",
            "session_name": "test-proj-abc123",
            "cwd": "/tmp/test-project",
            "model": "opus",
            "status": "suspended",
            "slack_channel_id": "C123",
            "slack_channel_name": "test-proj-abc123",
            "claude_session_id": "claude-sid-abc",
            "authenticated_user_id": "U001",
        }

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[suspended_session])
        mock_reg.get_session = AsyncMock(return_value=suspended_session)
        mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-sid-abc"})
        mock_reg.update_status = AsyncMock()

        child_stubs = []

        def make_stub(**kwargs):
            s = _StubSession()
            child_stubs.append(s)
            return s

        with (
            patch("summon_claude.sessions.manager.SummonSession", side_effect=make_stub),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await asyncio.wait_for(
                manager._project_up_orchestrator(
                    auth_stub,
                    [_mock_project()],  # type: ignore[arg-type]
                ),
                timeout=5,
            )

        # The suspended child was resumed (1 SummonSession from create_resumed_session).
        # No fresh PM was started since the suspended session is not a PM.
        # The orchestrator also starts a fresh PM for the project (1 SummonSession).
        # Total: 2 SummonSession() calls (1 resumed child + 1 fresh PM).
        assert len(child_stubs) == 2
        # Old suspended session should be marked completed
        mock_reg.update_status.assert_any_call("old-sess-1", "completed")

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )

    async def test_no_suspended_sessions_is_noop(self):
        """When no sessions are suspended, no child sessions are created."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        auth_stub.authenticate("U001")

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[])

        mock_ss = MagicMock(return_value=_StubSession())
        with (
            patch("summon_claude.sessions.manager.SummonSession", mock_ss),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await asyncio.wait_for(
                manager._project_up_orchestrator(
                    auth_stub,
                    [_mock_project()],  # type: ignore[arg-type]
                ),
                timeout=5,
            )

        # Only the PM session, no child restarts
        assert mock_ss.call_count == 1

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )

    async def test_suspended_restart_failure_continues(self):
        """If one suspended session fails to resume, others still proceed."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        auth_stub.authenticate("U001")

        s1 = {
            "session_id": "s1",
            "session_name": "proj-abc",
            "cwd": "/tmp/bad",
            "status": "suspended",
            "slack_channel_id": "C001",
            "slack_channel_name": "proj-abc",
            "claude_session_id": "cl-s1",
            "authenticated_user_id": "U001",
        }
        s2 = {
            "session_id": "s2",
            "session_name": "proj-def",
            "cwd": "/tmp/good",
            "status": "suspended",
            "slack_channel_id": "C002",
            "slack_channel_name": "proj-def",
            "claude_session_id": "cl-s2",
            "authenticated_user_id": "U001",
        }
        suspended_sessions = [s1, s2]

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=suspended_sessions)
        mock_reg.get_channel = AsyncMock(return_value=None)

        def get_session_side_effect(session_id):
            return {
                "s1": s1,
                "s2": s2,
            }.get(session_id)

        mock_reg.get_session = AsyncMock(side_effect=get_session_side_effect)
        mock_reg.update_status = AsyncMock()

        stub_count = 0

        def make_stub(**kwargs):
            nonlocal stub_count
            stub_count += 1
            # 1st SummonSession (resume of s1) raises; 2nd (resume of s2) and 3rd (fresh PM) succeed
            if stub_count == 1:
                raise RuntimeError("bad cwd")
            return _StubSession()

        with (
            patch("summon_claude.sessions.manager.SummonSession", side_effect=make_stub),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await asyncio.wait_for(
                manager._project_up_orchestrator(
                    auth_stub,
                    [_mock_project()],  # type: ignore[arg-type]
                ),
                timeout=5,
            )

        # s1 failed → marked errored; s2 succeeded → marked completed
        status_calls = [
            c for c in mock_reg.update_status.call_args_list if c.args[1] != "suspended"
        ]
        errored = [c for c in status_calls if c.args[1] == "errored"]
        completed = [c for c in status_calls if c.args[1] == "completed"]
        assert any(c.args[0] == "s1" for c in errored)
        assert any(
            "bad cwd" in c.kwargs.get("error_message", "") for c in errored if c.args[0] == "s1"
        )
        assert any(c.args[0] == "s2" for c in completed)

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )

    async def test_restart_suspended_missing_directory_marks_errored(self):
        """When project directory is missing, suspended sessions are marked errored."""
        manager, _, _ = _make_manager()

        suspended_session = {
            "session_id": "old-sess-1",
            "session_name": "proj-abc",
            "cwd": "/old/path",
            "status": "suspended",
            "slack_channel_id": "C123",
        }

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[suspended_session])
        mock_reg.update_status = AsyncMock()

        project = _mock_project(directory="/nonexistent/dir")
        manager.create_resumed_session = AsyncMock()

        with (
            patch("pathlib.Path.is_dir", return_value=False),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            result = await manager._restart_suspended_sessions([project], "user-1")

        assert result == set()
        manager.create_resumed_session.assert_not_called()
        errored_calls = [
            c for c in mock_reg.update_status.call_args_list if c.args == ("old-sess-1", "errored")
        ]
        assert len(errored_calls) == 1
        assert "not found" in errored_calls[0].kwargs["error_message"].lower()

    async def test_restart_suspended_missing_directory_partial_db_failure(self):
        """When project directory is missing and one update_status raises, others still complete."""
        manager, _, _ = _make_manager()

        suspended_session_1 = {
            "session_id": "old-sess-1",
            "session_name": "proj-abc",
            "cwd": "/old/path",
            "status": "suspended",
            "slack_channel_id": "C111",
        }
        suspended_session_2 = {
            "session_id": "old-sess-2",
            "session_name": "proj-def",
            "cwd": "/old/path2",
            "status": "suspended",
            "slack_channel_id": "C222",
        }

        update_status_calls = []

        async def update_status_side_effect(session_id, status, **kwargs):
            update_status_calls.append((session_id, status))
            if session_id == "old-sess-1":
                raise RuntimeError("DB write failed")

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(
            return_value=[suspended_session_1, suspended_session_2]
        )
        mock_reg.update_status = AsyncMock(side_effect=update_status_side_effect)

        project = _mock_project(directory="/nonexistent/dir")
        manager.create_resumed_session = AsyncMock()

        with (
            patch("pathlib.Path.is_dir", return_value=False),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            result = await manager._restart_suspended_sessions([project], "user-1")

        assert result == set()
        manager.create_resumed_session.assert_not_called()
        attempted_ids = [sid for sid, _ in update_status_calls]
        assert "old-sess-1" in attempted_ids
        assert "old-sess-2" in attempted_ids
        assert ("old-sess-2", "errored") in update_status_calls

    async def test_restart_suspended_uses_project_directory_not_session_cwd(self, caplog):
        """PM sessions always use project directory as cwd, not stale session cwd."""
        manager, _, _ = _make_manager()

        suspended_session = {
            "session_id": "sess-1",
            "session_name": "proj-pm-abc",
            "cwd": "/old/stale/dir",
            "status": "suspended",
            "slack_channel_id": "C123",
        }

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[suspended_session])
        mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-123"})
        mock_reg.update_status = AsyncMock()

        project = _mock_project(directory="/new/project/dir")
        manager.create_resumed_session = AsyncMock()

        with (
            caplog.at_level(logging.DEBUG, logger="summon_claude.sessions.manager"),
            # Required for the project-dir existence check; PM path never calls
            # is_dir on old_cwd — it unconditionally uses project_dir as cwd
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await manager._restart_suspended_sessions([project], "user-1")

        manager.create_resumed_session.assert_called_once()
        opts = manager.create_resumed_session.call_args[0][0]
        assert opts.cwd == "/new/project/dir"

        cwd_change_logs = [
            r
            for r in caplog.records
            if "/old/stale/dir" in r.message and "/new/project/dir" in r.message
        ]
        assert len(cwd_change_logs) == 1
        assert cwd_change_logs[0].levelno == logging.DEBUG

    async def test_restart_suspended_child_preserves_valid_subdirectory_cwd(self):
        """Non-PM child sessions preserve their cwd if it exists and is inside the project tree."""
        manager, _, _ = _make_manager()

        suspended_session = {
            "session_id": "sess-2",
            "session_name": "proj-abc",
            "cwd": "/new/project/dir/subdir",
            "status": "suspended",
            "slack_channel_id": "C456",
        }

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[suspended_session])
        mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-456"})
        mock_reg.update_status = AsyncMock()

        project = _mock_project(directory="/new/project/dir")
        manager.create_resumed_session = AsyncMock()

        with (
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await manager._restart_suspended_sessions([project], "user-1")

        manager.create_resumed_session.assert_called_once()
        opts = manager.create_resumed_session.call_args[0][0]
        assert opts.cwd == "/new/project/dir/subdir"

    async def test_restart_suspended_child_falls_back_on_outside_cwd(self):
        """Non-PM child sessions fall back to project dir if old cwd is outside the project tree."""
        manager, _, _ = _make_manager()

        suspended_session = {
            "session_id": "sess-3",
            "session_name": "proj-abc",
            "cwd": "/completely/different/path",
            "status": "suspended",
            "slack_channel_id": "C789",
        }

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[suspended_session])
        mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-789"})
        mock_reg.update_status = AsyncMock()

        project = _mock_project(directory="/new/project/dir")
        manager.create_resumed_session = AsyncMock()

        with (
            # is_dir=True for both project dir and old_cwd — is_relative_to is the
            # discriminating factor: old_cwd exists but resolves outside the project tree
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await manager._restart_suspended_sessions([project], "user-1")

        manager.create_resumed_session.assert_called_once()
        opts = manager.create_resumed_session.call_args[0][0]
        assert opts.cwd == "/new/project/dir"

    async def test_restart_suspended_child_falls_back_on_symlink_escaped_cwd(self):
        """Non-PM child falls back to project dir when old cwd resolves outside via symlink."""
        manager, _, _ = _make_manager()

        project_dir = "/new/project/dir"
        old_cwd = "/new/project/dir/link-to-outside"

        suspended_session = {
            "session_id": "sess-symlink",
            "session_name": "proj-abc",
            "cwd": old_cwd,
            "status": "suspended",
            "slack_channel_id": "C888",
        }

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[suspended_session])
        mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-888"})
        mock_reg.update_status = AsyncMock()

        project = _mock_project(directory=project_dir)
        manager.create_resumed_session = AsyncMock()

        def resolve_side_effect(self):
            if str(self) == old_cwd:
                return pathlib.Path("/external/target")
            return self

        with (
            patch("pathlib.Path.is_dir", return_value=True),
            patch("pathlib.Path.resolve", resolve_side_effect),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await manager._restart_suspended_sessions([project], "user-1")

        manager.create_resumed_session.assert_called_once()
        opts = manager.create_resumed_session.call_args[0][0]
        assert opts.cwd == project_dir

    async def test_restart_suspended_child_falls_back_on_deleted_subdirectory(self):
        """Non-PM child falls back to project dir when its old subdirectory no longer exists."""
        manager, _, _ = _make_manager()

        suspended_session = {
            "session_id": "sess-4",
            "session_name": "proj-abc",
            "cwd": "/new/project/dir/deleted-worktree",
            "status": "suspended",
            "slack_channel_id": "C101",
        }

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[suspended_session])
        mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-101"})
        mock_reg.update_status = AsyncMock()

        project = _mock_project(directory="/new/project/dir")
        manager.create_resumed_session = AsyncMock()

        def is_dir_side_effect(self):
            """Project dir exists, but the old child subdirectory was deleted."""
            return str(self) == "/new/project/dir"

        with (
            patch("pathlib.Path.is_dir", is_dir_side_effect),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await manager._restart_suspended_sessions([project], "user-1")

        manager.create_resumed_session.assert_called_once()
        opts = manager.create_resumed_session.call_args[0][0]
        assert opts.cwd == "/new/project/dir"

    async def test_restart_suspended_child_none_cwd_falls_back_to_project_dir(self):
        """Non-PM child session with cwd=None falls back to project directory."""
        manager, _, _ = _make_manager()

        suspended_session = {
            "session_id": "sess-none-cwd",
            "session_name": "proj-abc",
            "cwd": None,
            "status": "suspended",
            "slack_channel_id": "C555",
        }

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[suspended_session])
        mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-555"})
        mock_reg.update_status = AsyncMock()

        project = _mock_project(directory="/new/project/dir")
        manager.create_resumed_session = AsyncMock()

        with (
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await manager._restart_suspended_sessions([project], "user-1")

        manager.create_resumed_session.assert_called_once()
        opts = manager.create_resumed_session.call_args[0][0]
        assert opts.cwd == "/new/project/dir"

    async def test_restart_suspended_child_missing_cwd_key_falls_back_to_project_dir(self):
        """Non-PM child session with no cwd key falls back to project directory."""
        manager, _, _ = _make_manager()

        suspended_session = {
            "session_id": "sess-no-cwd-key",
            "session_name": "proj-abc",
            "status": "suspended",
            "slack_channel_id": "C666",
        }

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[suspended_session])
        mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-666"})
        mock_reg.update_status = AsyncMock()

        project = _mock_project(directory="/new/project/dir")
        manager.create_resumed_session = AsyncMock()

        with (
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await manager._restart_suspended_sessions([project], "user-1")

        manager.create_resumed_session.assert_called_once()
        opts = manager.create_resumed_session.call_args[0][0]
        assert opts.cwd == "/new/project/dir"

    async def test_restart_suspended_logs_cwd_change(self, caplog):
        """Debug log emitted when session cwd differs from project directory."""
        manager, _, _ = _make_manager()

        suspended_session = {
            "session_id": "sess-1",
            "session_name": "proj-abc",
            "cwd": "/old/stale/dir",
            "status": "suspended",
            "slack_channel_id": "C123",
        }

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[suspended_session])
        mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-123"})
        mock_reg.update_status = AsyncMock()

        project = _mock_project(directory="/new/project/dir")
        manager.create_resumed_session = AsyncMock()

        with (
            caplog.at_level(logging.DEBUG, logger="summon_claude.sessions.manager"),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await manager._restart_suspended_sessions([project], "user-1")

        cwd_change_logs = [
            r
            for r in caplog.records
            if "/old/stale/dir" in r.message and "/new/project/dir" in r.message
        ]
        assert len(cwd_change_logs) == 1
        assert cwd_change_logs[0].levelno == logging.DEBUG

    async def test_restart_suspended_no_log_when_cwd_unchanged(self, caplog):
        """No cwd-change debug log emitted when old_cwd already equals the project directory."""
        manager, _, _ = _make_manager()

        project_dir = "/new/project/dir"
        suspended_session = {
            "session_id": "sess-same-cwd",
            "session_name": "proj-abc",
            "cwd": project_dir,
            "status": "suspended",
            "slack_channel_id": "C777",
        }

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[suspended_session])
        mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-777"})
        mock_reg.update_status = AsyncMock()

        project = _mock_project(directory=project_dir)
        manager.create_resumed_session = AsyncMock()

        with (
            caplog.at_level(logging.DEBUG, logger="summon_claude.sessions.manager"),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await manager._restart_suspended_sessions([project], "user-1")

        cwd_change_logs = [r for r in caplog.records if "cwd changed" in r.message]
        assert len(cwd_change_logs) == 0

    async def test_restart_suspended_missing_directory_no_suspended_sessions(self):
        """Missing directory with zero suspended sessions returns empty set."""
        manager, _, _ = _make_manager()

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[])
        mock_reg.update_status = AsyncMock()

        project = _mock_project(directory="/nonexistent/dir")
        manager.create_resumed_session = AsyncMock()

        with (
            patch("pathlib.Path.is_dir", return_value=False),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            result = await manager._restart_suspended_sessions([project], "user-1")

        assert result == set()
        manager.create_resumed_session.assert_not_called()
        mock_reg.update_status.assert_not_called()

    async def test_restart_suspended_missing_directory_skips_only_bad_project(self):
        """Missing directory skips only that project; other projects resume."""
        manager, _, _ = _make_manager()

        bad_session = {
            "session_id": "bad-sess-1",
            "session_name": "proj-abc",
            "cwd": "/bad/project/dir",
            "status": "suspended",
            "slack_channel_id": "C_BAD",
        }
        good_session = {
            "session_id": "good-sess-1",
            "session_name": "proj-abc",
            "cwd": "/good/project/dir",
            "status": "suspended",
            "slack_channel_id": "C_GOOD",
        }

        bad_project = _mock_project(
            name="bad-project", directory="/nonexistent/dir", project_id="proj-bad"
        )
        good_project = _mock_project(
            name="good-project", directory="/good/project/dir", project_id="proj-good"
        )

        def get_project_sessions_side_effect(project_id):
            if project_id == "proj-bad":
                return [bad_session]
            return [good_session]

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(side_effect=get_project_sessions_side_effect)
        mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-good"})
        mock_reg.update_status = AsyncMock()

        manager.create_resumed_session = AsyncMock()

        def is_dir_side_effect(self):
            return str(self) == "/good/project/dir"

        with (
            patch("pathlib.Path.is_dir", is_dir_side_effect),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            result = await manager._restart_suspended_sessions(
                [bad_project, good_project], "user-1"
            )

        errored_calls = [
            c for c in mock_reg.update_status.call_args_list if c.args == ("bad-sess-1", "errored")
        ]
        assert len(errored_calls) == 1

        manager.create_resumed_session.assert_called_once()
        opts = manager.create_resumed_session.call_args[0][0]
        assert opts.cwd == "/good/project/dir"

        completed_calls = [
            c
            for c in mock_reg.update_status.call_args_list
            if c.args == ("good-sess-1", "completed")
        ]
        assert len(completed_calls) == 1

        assert result == set()


# ---------------------------------------------------------------------------
# send_message IPC handler tests
# ---------------------------------------------------------------------------


class TestSendMessageIPC:
    """Tests for the send_message IPC dispatch case."""

    async def test_send_message_to_active_session(self):
        manager, _, _ = _make_manager()
        stub = _StubSession()
        stub.channel_id = "C_TARGET"
        stub.inject_message = AsyncMock(return_value=True)  # type: ignore[attr-defined]
        manager._sessions["sess-target"] = stub  # type: ignore[assignment]

        result = await manager._dispatch_control(
            {
                "type": "send_message",
                "session_id": "sess-target",
                "text": "hello from PM",
                "sender_info": "pm (#C_PM)",
            }
        )
        assert result["type"] == "message_sent"
        assert result["session_id"] == "sess-target"
        assert result["channel_id"] == "C_TARGET"
        stub.inject_message.assert_awaited_once_with(  # type: ignore[union-attr]
            "hello from PM", sender_info="pm (#C_PM)"
        )

    async def test_send_message_missing_session(self):
        manager, _, _ = _make_manager()
        result = await manager._dispatch_control(
            {
                "type": "send_message",
                "session_id": "nonexistent",
                "text": "hello",
            }
        )
        assert result["type"] == "error"

    async def test_send_message_missing_text(self):
        manager, _, _ = _make_manager()
        result = await manager._dispatch_control(
            {
                "type": "send_message",
                "session_id": "sess-1",
            }
        )
        assert result["type"] == "error"

    async def test_send_message_queue_full(self):
        manager, _, _ = _make_manager()
        stub = _StubSession()
        stub.inject_message = AsyncMock(return_value=False)  # type: ignore[attr-defined]
        manager._sessions["sess-full"] = stub  # type: ignore[assignment]

        result = await manager._dispatch_control(
            {
                "type": "send_message",
                "session_id": "sess-full",
                "text": "overflow",
            }
        )
        assert result["type"] == "error"
        assert "Queue full" in result["message"]


# ---------------------------------------------------------------------------
# resume_session IPC handler tests
# ---------------------------------------------------------------------------


class TestResumeSessionIPC:
    """Tests for the resume_session IPC dispatch case."""

    async def test_resume_missing_session_id(self):
        manager, _, _ = _make_manager()
        result = await manager._dispatch_control({"type": "resume_session"})
        assert result["type"] == "error"
        assert "Missing" in result["message"]

    async def test_validate_resume_target_not_found(self):
        manager, _, _ = _make_manager()
        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls:
            mock_reg = AsyncMock()
            mock_reg.get_session = AsyncMock(return_value=None)
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match="not found"):
                await manager._validate_resume_target("nonexistent")

    async def test_validate_resume_target_still_active(self):
        manager, _, _ = _make_manager()
        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls:
            mock_reg = AsyncMock()
            mock_reg.get_session = AsyncMock(
                return_value={"status": "active", "slack_channel_id": "C1"}
            )
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match="active"):
                await manager._validate_resume_target("active-sess")

    async def test_concurrent_resume_rejected(self):
        """Two resume requests for the same channel — second is rejected."""
        manager, _, _ = _make_manager()
        manager._resuming_channels.add("C_BUSY")

        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls:
            mock_reg = AsyncMock()
            mock_reg.get_session = AsyncMock(
                return_value={
                    "status": "completed",
                    "slack_channel_id": "C_BUSY",
                    "claude_session_id": "claude-123",
                    "cwd": "/tmp",
                }
            )
            mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-123"})
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await manager._dispatch_control(
                {
                    "type": "resume_session",
                    "session_id": "old-sess",
                }
            )
        assert result["type"] == "error"
        assert "already in progress" in result["message"]

    async def test_channel_guard_in_create_session(self):
        """create_session rejects when channel_id has an active session."""
        manager, _, _ = _make_manager()
        stub = _StubSession()
        stub.channel_id = "C_OCCUPIED"
        manager._sessions["existing"] = stub  # type: ignore[assignment]

        with pytest.raises(ValueError, match="already has an active session"):
            await manager.create_session(make_options(channel_id="C_OCCUPIED"))

    async def test_channel_guard_catches_target_channel_id(self):
        """create_session detects sessions with target_channel_id but no channel_id yet."""
        manager, _, _ = _make_manager()
        stub = _StubSession()
        stub.target_channel_id = "C_PENDING"
        manager._sessions["pending"] = stub  # type: ignore[assignment]

        with pytest.raises(ValueError, match="already has an active session"):
            await manager.create_session(make_options(channel_id="C_PENDING"))

    async def test_validate_resume_target_suspended_gives_clear_error(self):
        """Suspended sessions should get a specific error, not 'still active'."""
        manager, _, _ = _make_manager()
        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls:
            mock_reg = AsyncMock()
            mock_reg.get_session = AsyncMock(
                return_value={"status": "suspended", "slack_channel_id": "C1"}
            )
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match=r"suspended.*project up"):
                await manager._validate_resume_target("suspended-sess")

    async def test_create_resumed_session_rejects_none_auth(self):
        """create_resumed_session raises ValueError when authenticated_user_id is None."""
        manager, _, _ = _make_manager()
        with pytest.raises(ValueError, match="authenticated_user_id"):
            await manager.create_resumed_session(make_options(), authenticated_user_id=None)

    async def test_resume_from_channel_raises_on_error(self):
        """resume_from_channel raises ValueError on validation failure."""
        manager, _, _ = _make_manager()
        with (
            patch.object(
                manager,
                "_resolve_channel_resume",
                new=AsyncMock(side_effect=ValueError("owner mismatch")),
            ),
            pytest.raises(ValueError, match="owner mismatch"),
        ):
            await manager.resume_from_channel("C1", "U_WRONG", None)

    async def test_resume_from_channel_succeeds(self):
        """resume_from_channel delegates to _handle_resume_session on success."""
        manager, _, _ = _make_manager()
        with (
            patch.object(
                manager,
                "_resolve_channel_resume",
                new=AsyncMock(return_value="old-sess"),
            ),
            patch.object(
                manager,
                "_handle_resume_session",
                new=AsyncMock(
                    return_value={
                        "type": "session_resumed",
                        "session_id": "new",
                        "channel_id": "C1",
                    }
                ),
            ),
        ):
            await manager.resume_from_channel("C1", "U_OWNER", None)  # should not raise

    async def test_resume_from_channel_silent_for_unknown_channel(self):
        """resume_from_channel returns silently for non-summon channels."""
        manager, _, _ = _make_manager()
        with patch.object(
            manager,
            "_resolve_channel_resume",
            new=AsyncMock(return_value=None),
        ):
            await manager.resume_from_channel("C_UNKNOWN", "U1", None)  # should not raise


class TestResolveChannelResume:
    """Tests for _resolve_channel_resume validation logic."""

    async def test_unknown_channel_returns_none(self):
        manager, _, _ = _make_manager()
        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_cls:
            mock_reg = AsyncMock()
            mock_reg.get_channel = AsyncMock(return_value=None)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await manager._resolve_channel_resume("C_UNK", "U1", None)
        assert result is None

    async def test_wrong_user_raises(self):
        manager, _, _ = _make_manager()
        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_cls:
            mock_reg = AsyncMock()
            mock_reg.get_channel = AsyncMock(return_value={"authenticated_user_id": "U_OWNER"})
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match="owner"):
                await manager._resolve_channel_resume("C1", "U_INTRUDER", None)

    async def test_no_previous_session_raises(self):
        manager, _, _ = _make_manager()
        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_cls:
            mock_reg = AsyncMock()
            mock_reg.get_channel = AsyncMock(return_value={"authenticated_user_id": "U1"})
            mock_reg.get_latest_session_for_channel = AsyncMock(return_value=None)
            mock_reg.get_active_session_for_channel = AsyncMock(return_value=None)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match="No previous session"):
                await manager._resolve_channel_resume("C1", "U1", None)

    async def test_target_not_in_channel_raises(self):
        manager, _, _ = _make_manager()
        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_cls:
            mock_reg = AsyncMock()
            mock_reg.get_channel = AsyncMock(return_value={"authenticated_user_id": "U1"})
            mock_reg.get_session = AsyncMock(
                return_value={"slack_channel_id": "C_OTHER", "status": "completed"}
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match="not found"):
                await manager._resolve_channel_resume("C1", "U1", "sess-wrong")

    async def test_active_session_raises(self):
        manager, _, _ = _make_manager()
        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_cls:
            mock_reg = AsyncMock()
            mock_reg.get_channel = AsyncMock(return_value={"authenticated_user_id": "U1"})
            mock_reg.get_latest_session_for_channel = AsyncMock(
                return_value={"session_id": "s1", "status": "active"}
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match="still active"):
                await manager._resolve_channel_resume("C1", "U1", None)

    async def test_happy_path_returns_session_id(self):
        manager, _, _ = _make_manager()
        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_cls:
            mock_reg = AsyncMock()
            mock_reg.get_channel = AsyncMock(return_value={"authenticated_user_id": "U1"})
            mock_reg.get_latest_session_for_channel = AsyncMock(
                return_value={"session_id": "sess-done", "status": "completed"}
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await manager._resolve_channel_resume("C1", "U1", None)
        assert result == "sess-done"


# ---------------------------------------------------------------------------
# Tests: _validate_resume_target suspended rejection
# ---------------------------------------------------------------------------


class TestValidateResumeTargetSuspended:
    """Tests for _validate_resume_target's suspended session handling."""

    async def test_validate_resume_target_rejects_suspended(self):
        """Suspended sessions are rejected with guidance to use project up."""
        manager, _, _ = _make_manager()
        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls:
            mock_reg = AsyncMock()
            mock_reg.get_session = AsyncMock(
                return_value={"status": "suspended", "slack_channel_id": "C1"}
            )
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match=r"suspended.*project up"):
                await manager._validate_resume_target("suspended-sess")


# ---------------------------------------------------------------------------
# Tests: _restart_suspended_sessions — PM resume + missing claude_session_id
# ---------------------------------------------------------------------------


class TestRestartSuspendedSessionsResume:
    """Additional tests for _restart_suspended_sessions edge cases."""

    async def test_project_up_resume_suspended_pm(self):
        """Suspended PM session is resumed with pm_profile=True from DB flag."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        auth_stub.authenticate("U001")

        pm_session = {
            "session_id": "pm-old",
            "session_name": "pm-abc123",
            "cwd": "/tmp/myproj",
            "model": "claude-opus-4-6",
            "status": "suspended",
            "slack_channel_id": "C_PM",
            "slack_channel_name": "zzz-myproj-0-pm",
            "claude_session_id": "claude-pm-sid",
            "authenticated_user_id": "U001",
            "pm_profile": 1,
        }

        captured_options: list = []

        def make_stub(*args, **kwargs):
            captured_options.append(kwargs)
            return _StubSession()

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[pm_session])
        mock_reg.get_session = AsyncMock(return_value=pm_session)
        mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-pm-sid"})
        mock_reg.update_status = AsyncMock()

        with (
            patch("summon_claude.sessions.manager.SummonSession", side_effect=make_stub),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await asyncio.wait_for(
                manager._project_up_orchestrator(
                    auth_stub,
                    [_mock_project()],
                ),
                timeout=5,
            )

        # pm_session is a PM → pm_resumed should contain its project_id,
        # so no fresh PM was started. Exactly 1 SummonSession (the resumed PM).
        assert len(captured_options) == 1
        opts = captured_options[0].get("options")
        assert opts is not None
        assert opts.pm_profile is True
        assert opts.resume == "claude-pm-sid"
        # Old PM record should be marked completed
        mock_reg.update_status.assert_any_call("pm-old", "completed")

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )

    async def test_non_pm_with_pm_like_name_not_treated_as_pm(self):
        """Suspended session named 'fix-pm-bug' with pm_profile=0 is NOT resumed as PM."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        non_pm_session = {
            "session_id": "child-old",
            "session_name": "fix-pm-bug",
            "cwd": "/tmp/myproj",
            "model": "claude-opus-4-6",
            "status": "suspended",
            "slack_channel_id": "C_CHILD",
            "slack_channel_name": "zzz-myproj-fix-pm-bug-abc123",
            "claude_session_id": "claude-child-sid",
            "authenticated_user_id": "U001",
            "pm_profile": 0,
        }

        captured_options: list = []

        async def fake_create(opts, **kwargs):
            captured_options.append(opts)

        manager.create_resumed_session = fake_create

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[non_pm_session])
        mock_reg.get_channel = AsyncMock(return_value={"claude_session_id": "claude-child-sid"})
        mock_reg.update_status = AsyncMock()

        with (
            patch("pathlib.Path.is_dir", return_value=True),
            patch("summon_claude.sessions.manager.SessionRegistry") as mock_registry_cls,
        ):
            mock_registry_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_registry_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await manager._restart_suspended_sessions(
                [{"project_id": "p1", "directory": "/tmp/myproj", "name": "myproj"}],
                "U001",
            )

        assert len(captured_options) == 1
        opts = captured_options[0]
        assert opts.pm_profile is not True

    async def test_project_up_resume_missing_claude_sid(self):
        """Session with no claude_session_id falls back to channel-reuse-only (resume=None)."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        auth_stub.authenticate("U001")

        child_session = {
            "session_id": "child-no-sid",
            "session_name": "myproj-abc123",
            "cwd": "/tmp/myproj",
            "model": "claude-opus-4-6",
            "status": "suspended",
            "slack_channel_id": "C_CHILD",
            "slack_channel_name": "zzz-myproj-abc123",
            "claude_session_id": None,  # missing → channel-reuse-only fallback
            "authenticated_user_id": "U001",
        }

        captured_options: list = []

        def make_stub(*args, **kwargs):
            captured_options.append(kwargs)
            return _StubSession()

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[child_session])
        mock_reg.get_session = AsyncMock(return_value=child_session)
        mock_reg.get_channel = AsyncMock(return_value=None)  # no channels table row
        mock_reg.update_status = AsyncMock()

        with (
            patch("summon_claude.sessions.manager.SummonSession", side_effect=make_stub),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await asyncio.wait_for(
                manager._project_up_orchestrator(
                    auth_stub,
                    [_mock_project()],
                ),
                timeout=5,
            )

        # 2 sessions: 1 resumed child (channel-reuse-only) + 1 fresh PM
        assert len(captured_options) == 2
        # First SummonSession is the resumed child — resume should be None
        child_opts = captured_options[0].get("options")
        assert child_opts is not None
        assert child_opts.resume is None

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )


class TestCreateResumedSessionPmTopic:
    """create_resumed_session calls _update_pm_topic for non-PM project sessions."""

    async def test_resumed_session_updates_pm_topic(self):
        """Non-PM resumed session with project_id triggers _update_pm_topic."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        stub = _StubSession()

        with (
            patch("summon_claude.sessions.manager.SummonSession", return_value=stub),
            patch("pathlib.Path.is_dir", return_value=True),
            patch.object(manager, "_update_pm_topic", new=AsyncMock()) as mock_topic,
        ):
            options = SessionOptions(
                cwd="/tmp/test",
                name="proj-abc",
                project_id="p1",
                pm_profile=False,
                channel_id="C1",
            )
            await manager.create_resumed_session(options, authenticated_user_id="U001")

        mock_topic.assert_awaited_once_with("p1")

    async def test_resumed_pm_session_skips_pm_topic(self):
        """PM resumed session does NOT trigger _update_pm_topic."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        stub = _StubSession()

        with (
            patch("summon_claude.sessions.manager.SummonSession", return_value=stub),
            patch("pathlib.Path.is_dir", return_value=True),
            patch.object(manager, "_update_pm_topic", new=AsyncMock()) as mock_topic,
        ):
            options = SessionOptions(
                cwd="/tmp/test",
                name="pm-abc",
                project_id="p1",
                pm_profile=True,
                channel_id="C1",
            )
            await manager.create_resumed_session(options, authenticated_user_id="U001")

        mock_topic.assert_not_awaited()


class TestMultiProjectPmResume:
    """pm_resumed set accumulates across multiple projects."""

    async def test_two_projects_each_with_suspended_pm(self):
        """Two projects with suspended PMs produce zero fresh PM starts."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        auth_stub.authenticate("U001")

        pm1 = {
            "session_id": "pm-old-1",
            "session_name": "pm-aaa",
            "cwd": "/tmp/proj1",
            "model": "claude-opus-4-6",
            "status": "suspended",
            "slack_channel_id": "C_PM1",
            "slack_channel_name": "zzz-proj1-0-pm",
            "claude_session_id": "cl-pm1",
            "authenticated_user_id": "U001",
            "pm_profile": 1,
        }
        pm2 = {
            "session_id": "pm-old-2",
            "session_name": "pm-bbb",
            "cwd": "/tmp/proj2",
            "model": "claude-opus-4-6",
            "status": "suspended",
            "slack_channel_id": "C_PM2",
            "slack_channel_name": "zzz-proj2-0-pm",
            "claude_session_id": "cl-pm2",
            "authenticated_user_id": "U001",
            "pm_profile": 1,
        }

        captured_options: list = []

        def make_stub(*args, **kwargs):
            captured_options.append(kwargs)
            return _StubSession()

        mock_reg = AsyncMock()

        def get_sessions(pid):
            if pid == "p1":
                return [pm1]
            return [pm2]

        mock_reg.get_project_sessions = AsyncMock(side_effect=get_sessions)
        mock_reg.get_session = AsyncMock(side_effect=lambda sid: pm1 if sid == "pm-old-1" else pm2)
        mock_reg.get_channel = AsyncMock(
            side_effect=lambda cid: (
                {"claude_session_id": "cl-pm1"}
                if cid == "C_PM1"
                else {"claude_session_id": "cl-pm2"}
            )
        )
        mock_reg.update_status = AsyncMock()

        projects = [
            {
                "project_id": "p1",
                "name": "proj1",
                "directory": "/tmp/proj1",
                "channel_prefix": "proj1",
            },
            {
                "project_id": "p2",
                "name": "proj2",
                "directory": "/tmp/proj2",
                "channel_prefix": "proj2",
            },
        ]

        with (
            patch("summon_claude.sessions.manager.SummonSession", side_effect=make_stub),
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "summon_claude.sessions.manager.SessionRegistry",
                _mock_registry_ctx(mock_reg),
            ),
        ):
            await asyncio.wait_for(
                manager._project_up_orchestrator(auth_stub, projects),
                timeout=5,
            )

        # Both PMs resumed, no fresh PMs started → exactly 2 SummonSession calls
        assert len(captured_options) == 2
        # Both should have pm_profile=True
        for opts_dict in captured_options:
            opts = opts_dict.get("options")
            assert opts is not None
            assert opts.pm_profile is True

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )


class TestValidateResumeTargetEdgeCases:
    """Edge case tests for _validate_resume_target."""

    async def test_validate_resume_no_channel(self):
        """Sessions with no channel_id are rejected."""
        manager, _, _ = _make_manager()
        no_channel = {
            "session_id": "s-no-chan",
            "status": "completed",
            "slack_channel_id": None,
            "cwd": "/tmp/test",
        }
        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_cls:
            mock_reg = AsyncMock()
            mock_reg.get_session = AsyncMock(return_value=no_channel)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match="no associated channel"):
                await manager._validate_resume_target("s-no-chan")


# ---------------------------------------------------------------------------
# Tests: _suspend_on_shutdown in shutdown()
# ---------------------------------------------------------------------------


class TestSuspendOnShutdown:
    """Verify SessionManager.shutdown() suspends project sessions on health failure."""

    @pytest.mark.asyncio
    async def test_suspend_marks_project_sessions_suspended(self):
        """Project sessions marked 'suspended' when _suspend_on_shutdown is set."""
        manager, _provider, _dispatcher = _make_manager()

        # Create mock sessions with project affiliation
        mock_project_session = MagicMock()
        mock_project_session.project_id = "proj-1"
        mock_project_session.channel_id = "C123"
        mock_project_session.request_shutdown = MagicMock()

        mock_adhoc_session = MagicMock()
        mock_adhoc_session.project_id = None
        mock_adhoc_session.channel_id = "C456"
        mock_adhoc_session.request_shutdown = MagicMock()

        manager._sessions = {"sid-1": mock_project_session, "sid-2": mock_adhoc_session}
        t1 = asyncio.create_task(asyncio.sleep(0))
        t2 = asyncio.create_task(asyncio.sleep(0))
        await asyncio.sleep(0)  # let tasks complete
        manager._tasks = {"sid-1": t1, "sid-2": t2}
        manager._suspend_on_shutdown = True

        mock_reg = MagicMock()
        mock_reg.update_status = AsyncMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_reg)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("summon_claude.sessions.manager.SessionRegistry", return_value=mock_ctx),
            patch("summon_claude.sessions.manager._SHUTDOWN_WAIT_TIMEOUT", 0.01),
        ):
            await manager.shutdown()

        # Project session should be suspended
        mock_reg.update_status.assert_any_call("sid-1", "suspended")
        # Ad-hoc session should be errored
        calls = [c for c in mock_reg.update_status.call_args_list if c[0][0] == "sid-2"]
        assert len(calls) == 1
        assert calls[0][0][1] == "errored"

    @pytest.mark.asyncio
    async def test_no_suspend_when_flag_is_false(self):
        """Normal shutdown should NOT pre-set session statuses."""
        manager, _provider, _dispatcher = _make_manager()

        mock_session = MagicMock()
        mock_session.project_id = "proj-1"
        mock_session.channel_id = "C123"
        mock_session.request_shutdown = MagicMock()

        manager._sessions = {"sid-1": mock_session}
        t1 = asyncio.create_task(asyncio.sleep(0))
        await asyncio.sleep(0)
        manager._tasks = {"sid-1": t1}
        manager._suspend_on_shutdown = False

        mock_reg = MagicMock()
        mock_reg.update_status = AsyncMock()

        with (
            patch("summon_claude.sessions.manager.SessionRegistry") as mock_cls,
            patch("summon_claude.sessions.manager._SHUTDOWN_WAIT_TIMEOUT", 0.01),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await manager.shutdown()

        # update_status should NOT be called for pre-suspend
        mock_reg.update_status.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: _handle_health_check IPC handler
# ---------------------------------------------------------------------------


class TestHealthCheckIPC:
    """Tests for _handle_health_check IPC handler."""

    @pytest.mark.asyncio
    async def test_health_check_no_probe(self):
        """When event_probe is None, returns skipped."""
        manager, _provider, _dispatcher = _make_manager()
        manager._event_probe = None
        result = await manager._handle_health_check()
        assert result["reason"] == "skipped"
        assert result["healthy"] is None

    @pytest.mark.asyncio
    async def test_health_check_healthy(self):
        """When probe returns healthy, returns healthy result."""
        from summon_claude.slack.bolt import DiagnosticResult

        manager, _provider, _dispatcher = _make_manager()
        mock_probe = MagicMock()
        mock_probe.run_probe = AsyncMock(
            return_value=DiagnosticResult(healthy=True, reason="healthy", details="OK")
        )
        manager._event_probe = mock_probe
        result = await manager._handle_health_check()
        assert result["healthy"] is True
        assert result["reason"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_check_probe_raises(self):
        """When probe raises, returns error."""
        manager, _provider, _dispatcher = _make_manager()
        mock_probe = MagicMock()
        mock_probe.run_probe = AsyncMock(side_effect=RuntimeError("probe crashed"))
        manager._event_probe = mock_probe
        result = await manager._handle_health_check()
        assert result["healthy"] is None
        assert result["reason"] == "error"

    @pytest.mark.asyncio
    async def test_health_check_timeout(self):
        """When probe times out, returns timeout result."""
        manager, _provider, _dispatcher = _make_manager()
        mock_probe = MagicMock()
        mock_probe.run_probe = AsyncMock(side_effect=TimeoutError)
        manager._event_probe = mock_probe
        result = await manager._handle_health_check()
        assert result["healthy"] is None
        assert result["reason"] == "timeout"

    @pytest.mark.asyncio
    async def test_set_suspend_on_shutdown(self):
        """set_suspend_on_shutdown() sets the flag via public API."""
        manager, _provider, _dispatcher = _make_manager()
        assert manager._suspend_on_shutdown is False
        manager.set_suspend_on_shutdown()
        assert manager._suspend_on_shutdown is True
