"""Tests for summon_claude.sessions.manager — SessionManager lifecycle."""

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
from summon_claude.daemon import recv_msg, send_msg
from summon_claude.sessions.auth import SessionAuth
from summon_claude.sessions.manager import _GRACE_SECONDS, SessionManager
from summon_claude.sessions.session import SessionOptions

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
        self.is_pm: bool = False
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
    cfg = make_config()
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
# Tests: create_session_with_spawn_token
# ---------------------------------------------------------------------------


class TestCreateSessionWithSpawnToken:
    async def test_valid_spawn_token_creates_session(self):
        """create_session_with_spawn_token creates a pre-authenticated session."""
        config = make_config()
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
        config = make_config()
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
        config = make_config()
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
        config = make_config()
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
        config = make_config()
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
        config = make_config()
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
        """Orchestrator restarts sessions with status 'suspended'."""
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
        }

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=[suspended_session])
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

        # 1 PM session + 1 restarted child session = 2 SummonSession() calls
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
        """If one suspended session fails to restart, others still proceed."""
        manager, _, _ = _make_manager()
        manager._project_up_in_flight = True

        auth_stub = _StubSession()
        auth_stub.authenticate("U001")

        suspended_sessions = [
            {"session_id": "s1", "cwd": "/tmp/bad", "status": "suspended"},
            {"session_id": "s2", "cwd": "/tmp/good", "status": "suspended"},
        ]

        mock_reg = AsyncMock()
        mock_reg.get_project_sessions = AsyncMock(return_value=suspended_sessions)
        mock_reg.update_status = AsyncMock()

        call_count = 0

        def make_stub(**kwargs):
            nonlocal call_count
            call_count += 1
            # PM session (1st call) succeeds, child #1 (2nd) fails, child #2 (3rd) succeeds
            if call_count == 2:
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
        calls = mock_reg.update_status.call_args_list
        assert len(calls) == 2
        # s1 errored (order: errored first since s1 is processed first)
        assert calls[0].args == ("s1", "errored")
        assert "bad cwd" in calls[0].kwargs.get("error_message", "")
        # s2 completed
        assert calls[1].args == ("s2", "completed")

        for t in list(manager._background_tasks):
            t.cancel()
        await asyncio.gather(
            *manager._tasks.values(), *manager._background_tasks, return_exceptions=True
        )
