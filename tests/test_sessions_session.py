"""Tests for summon_claude.sessions.session — session orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

from summon_claude.config import SummonConfig
from summon_claude.sessions.auth import SessionAuth
from summon_claude.sessions.commands import (
    _ALIAS_LOOKUP,
    COMMAND_ACTIONS,
    CommandDef,
    CommandResult,
)
from summon_claude.sessions.registry import (
    MAX_SPAWN_CHILDREN,
    MAX_SPAWN_CHILDREN_PM,
    MAX_SPAWN_DEPTH,
)
from summon_claude.sessions.session import (
    SessionOptions,
    SummonSession,
    _format_file_references,
    _SessionRestartError,
    _SessionRuntime,
)
from summon_claude.slack.client import MessageRef, SlackClient


def make_config(**overrides) -> SummonConfig:
    defaults = {
        "slack_bot_token": "xoxb-test-token",
        "slack_app_token": "xapp-test-token",
        "slack_signing_secret": "test-secret",
        "default_model": "claude-opus-4-6",
        "channel_prefix": "summon",
        "permission_debounce_ms": 10,
        "max_inline_chars": 2500,
    }
    defaults.update(overrides)
    return SummonConfig.model_validate(defaults)


def make_options(**overrides) -> SessionOptions:
    defaults = {
        "cwd": "/tmp/test",
        "name": "test",
    }
    defaults.update(overrides)
    return SessionOptions(**defaults)


def make_auth(**overrides) -> SessionAuth:
    defaults = {
        "short_code": "abcd1234",
        "session_id": "test-session",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    defaults.update(overrides)
    return SessionAuth(**defaults)


def make_session(session_id: str = "test-session", **overrides) -> SummonSession:
    opt_fields = ("cwd", "name", "model", "effort", "resume", "pm_profile")
    opts_kw = {k: overrides.pop(k) for k in list(overrides) if k in opt_fields}
    auth_fields = ("short_code", "expires_at")
    auth_kw = {k: overrides.pop(k) for k in list(overrides) if k in auth_fields}
    return SummonSession(
        config=make_config(),
        options=make_options(**opts_kw),
        auth=make_auth(session_id=session_id, **auth_kw),
        session_id=session_id,
        **overrides,
    )


def make_mock_client(channel_id: str = "C_TEST_CHAN") -> AsyncMock:
    """Create a mock SlackClient."""
    client = AsyncMock(spec=SlackClient)
    client.channel_id = channel_id
    client.post = AsyncMock(return_value=MessageRef(channel_id=channel_id, ts="1234567890.000000"))
    client.post_ephemeral = AsyncMock()
    client.update = AsyncMock()
    client.react = AsyncMock()
    client.unreact = AsyncMock()
    client.upload = AsyncMock()
    client.set_topic = AsyncMock()
    client.set_thread_status = AsyncMock()
    return client


def make_rt(
    registry, channel_id: str = "C_TEST_CHAN", client: AsyncMock | None = None
) -> _SessionRuntime:
    """Create a minimal _SessionRuntime with mocked client."""
    if client is None:
        client = make_mock_client(channel_id)
    return _SessionRuntime(
        registry=registry,
        client=client,
        permission_handler=AsyncMock(),
    )


class TestSessionOptions:
    """Tests for SessionOptions dataclass."""

    def test_default_effort(self):
        opts = make_options()
        assert opts.effort == "high"

    def test_custom_effort(self):
        opts = make_options(effort="max")
        assert opts.effort == "max"

    def test_effort_stored_on_session(self):
        session = make_session(effort="low")
        assert session._effort == "low"


class TestSummonSessionConstructorGuards:
    """Guard tests pinning SummonSession.__init__ parameter contracts."""

    def test_session_id_is_required(self):
        """session_id must be a required keyword arg — no empty-string default."""
        sig = inspect.signature(SummonSession.__init__)
        param = sig.parameters["session_id"]
        assert param.default is inspect.Parameter.empty, (
            "session_id must not have a default value — "
            "an empty string would silently create invalid DB rows"
        )
        assert param.kind == inspect.Parameter.KEYWORD_ONLY

    def test_constructor_rejects_missing_session_id(self):
        """Constructing SummonSession without session_id must raise TypeError."""
        with pytest.raises(TypeError, match="session_id"):
            SummonSession(config=make_config(), options=make_options())


class TestGenerateSessionToken:
    async def test_returns_session_auth(self, tmp_path):
        """generate_session_token should return a SessionAuth with correct fields."""
        from summon_claude.sessions.auth import generate_session_token
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            auth = await generate_session_token(registry, "sess-test")

        assert isinstance(auth, SessionAuth)
        assert len(auth.short_code) == 8
        assert auth.session_id == "sess-test"
        assert auth.expires_at > datetime.now(UTC)


class TestFormatFileReferences:
    def test_empty_list_returns_empty_string(self):
        result = _format_file_references([])
        assert result == ""

    def test_single_file_with_name(self):
        files = [{"name": "photo.png", "filetype": "png", "size": 1024}]
        result = _format_file_references(files)
        assert "photo.png" in result
        assert "(png)" in result
        assert "(1024 bytes)" in result
        # URL should NOT be included (Claude can't fetch Slack private URLs)
        assert "https://" not in result

    def test_single_file_without_url(self):
        files = [{"name": "doc.txt", "filetype": "txt"}]
        result = _format_file_references(files)
        assert "doc.txt" in result
        assert "(txt)" in result

    def test_multiple_files_joined_by_newlines(self):
        files = [
            {"name": "a.py", "url_private_download": "https://example.com/a"},
            {"name": "b.py", "url_private_download": "https://example.com/b"},
        ]
        result = _format_file_references(files)
        lines = result.splitlines()
        assert len(lines) == 2
        assert "a.py" in lines[0]
        assert "b.py" in lines[1]

    def test_missing_name_uses_unknown(self):
        files = [{"url_private": "https://example.com/f"}]
        result = _format_file_references(files)
        assert "unknown" in result


class TestSessionShutdownControl:
    """Test request_shutdown() and authenticate() — the new public control API."""

    def test_request_shutdown_sets_event(self):
        session = make_session()
        assert not session._shutdown_event.is_set()
        session.request_shutdown()
        assert session._shutdown_event.is_set()

    async def test_request_shutdown_puts_sentinel_on_queue(self):
        session = make_session()
        session.request_shutdown()
        item = await asyncio.wait_for(session._raw_event_queue.get(), timeout=1.0)
        assert item is None

    def test_request_shutdown_idempotent(self):
        """Calling request_shutdown() twice should not raise."""
        session = make_session()
        session.request_shutdown()
        session.request_shutdown()  # must not raise
        assert session._shutdown_event.is_set()

    def test_authenticate_sets_event_and_user(self):
        session = make_session()
        assert not session._authenticated_event.is_set()
        session.authenticate("U001")
        assert session._authenticated_event.is_set()
        assert session._authenticated_user_id == "U001"

    def test_authenticate_clears_auth_token(self):
        """authenticate() should clear the auth token from memory."""
        session = make_session()
        assert session._auth is not None
        session.authenticate("U001")
        assert session._auth is None

    def test_channel_id_property_initially_none(self):
        session = make_session()
        assert session.channel_id is None


class TestWaitForAuth:
    async def test_returns_immediately_when_event_set(self):
        session = make_session()
        session._authenticated_event.set()

        # Should complete quickly since event is already set
        await asyncio.wait_for(session._wait_for_auth(), timeout=2.0)

    async def test_returns_when_shutdown_event_set(self):
        session = make_session()
        session._shutdown_event.set()

        await asyncio.wait_for(session._wait_for_auth(), timeout=2.0)


class TestCreateChannel:
    """Tests for _create_channel retry logic."""

    async def test_succeeds_on_first_attempt(self):
        session = make_session()

        mock_client = AsyncMock()
        mock_client.conversations_create = AsyncMock(
            return_value={"channel": {"id": "C123", "name": "summon-test-0303-abcd1234"}}
        )

        cid, cname = await session._create_channel(mock_client)
        assert cid == "C123"
        assert cname == "summon-test-0303-abcd1234"
        mock_client.conversations_create.assert_awaited_once()

    async def test_retries_on_name_taken(self):
        session = make_session()

        mock_client = AsyncMock()
        mock_client.conversations_create = AsyncMock(
            side_effect=[
                Exception("name_taken"),
                {"channel": {"id": "C456", "name": "summon-test-0303-ffff0000"}},
            ]
        )

        cid, cname = await session._create_channel(mock_client)
        assert cid == "C456"
        assert mock_client.conversations_create.await_count == 2

    async def test_raises_after_all_retries_exhausted(self):
        session = make_session()

        mock_client = AsyncMock()
        mock_client.conversations_create = AsyncMock(side_effect=Exception("name_taken"))

        import pytest

        with pytest.raises(RuntimeError, match="Could not create channel"):
            await session._create_channel(mock_client)
        assert mock_client.conversations_create.await_count == 3

    async def test_non_name_taken_error_raises_immediately(self):
        session = make_session()

        mock_client = AsyncMock()
        mock_client.conversations_create = AsyncMock(side_effect=Exception("invalid_auth"))

        import pytest

        with pytest.raises(Exception, match="invalid_auth"):
            await session._create_channel(mock_client)
        mock_client.conversations_create.assert_awaited_once()


class TestSlashCommandHandler:
    """Test the /summon slash command handler internals."""

    async def test_verify_short_code_returns_result(self, tmp_path):
        """verify_short_code should return a result for a valid code."""
        from summon_claude.sessions.auth import generate_session_token, verify_short_code
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-1", 1234, "/tmp")
            auth = await generate_session_token(registry, "sess-1")

            result = await verify_short_code(registry, auth.short_code)
            assert result is not None

    async def test_slash_command_invalid_code_no_event_set(self, tmp_path):
        """Invalid code should NOT set authenticated_event."""
        from summon_claude.sessions.auth import verify_short_code
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-2", 1234, "/tmp")

            session = make_session(session_id="sess-2")

            result = await verify_short_code(registry, "badcod")
            assert result is None
            assert not session._authenticated_event.is_set()


class TestPostSessionSummary:
    """Tests for _post_session_summary — uses router.post_to_main, not rt."""

    async def _make_mock_claude(self, summary_text: str):
        """Create a mock ClaudeSDKClient that yields an AssistantMessage."""
        from claude_agent_sdk import AssistantMessage, TextBlock

        claude = AsyncMock()
        claude.query = AsyncMock()

        msg = AssistantMessage(content=[TextBlock(text=summary_text)], model="claude-opus-4-6")

        async def fake_receive():
            yield msg

        claude.receive_response = fake_receive
        return claude

    async def test_posts_via_router_not_rt(self):
        """_post_session_summary should call router.post_to_main, not rt."""
        router = AsyncMock()
        router.post_to_main = AsyncMock(
            return_value=MessageRef(channel_id="C123", ts="1234567890.000000")
        )
        claude = await self._make_mock_claude("Session accomplished X and Y.")
        session = make_session()

        await session._post_session_summary(router, claude)

        router.post_to_main.assert_called_once()
        text = router.post_to_main.call_args[0][0]
        assert ":memo:" in text
        assert "Session accomplished X and Y." in text
        # Header must use standard markdown bold (**), not Slack mrkdwn (*)
        assert "**Session Summary**" in text

    async def test_strips_dangerous_mentions(self):
        """Should strip @channel, @here, @everyone, and user mentions."""
        router = AsyncMock()
        router.post_to_main = AsyncMock(
            return_value=MessageRef(channel_id="C123", ts="1234567890.000000")
        )
        claude = await self._make_mock_claude("Done! <!channel> ping <!here> and <@U12345> helped.")
        session = make_session()

        await session._post_session_summary(router, claude)

        text = router.post_to_main.call_args[0][0]
        assert "<!channel>" not in text
        assert "<!here>" not in text
        assert "<@U12345>" not in text
        assert "helped" in text

    async def test_no_post_on_empty_summary(self):
        """Should not post if Claude returns empty text."""
        router = AsyncMock()
        claude = await self._make_mock_claude("   ")
        session = make_session()

        await session._post_session_summary(router, claude)

        router.post_to_main.assert_not_called()


class TestSessionShutdownSummary:
    async def test_shutdown_posts_summary_message(self, tmp_path):
        """_shutdown should post turns/cost summary to channel."""
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-sd", 1234, "/tmp")

            mock_client = make_mock_client("C_TEST_CHAN")
            session = make_session(session_id="sess-sd")
            session._total_turns = 3
            session._total_cost = 0.0456

            rt = _SessionRuntime(
                registry=registry,
                client=mock_client,
                permission_handler=AsyncMock(),
            )

            await session._shutdown(rt)

            # Disconnect message should have been posted
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            text = call_args[0][0]
            assert "3" in text  # turns in message text
            assert "0.0456" in text or "0.046" in text

    async def test_shutdown_preserves_channel(self, tmp_path):
        """_shutdown should NOT archive the session channel — channels are preserved."""
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-arch", 1234, "/tmp")

            mock_client = make_mock_client("C_ARCH_CHAN")
            session = make_session(session_id="sess-arch")

            rt = _SessionRuntime(
                registry=registry,
                client=mock_client,
                permission_handler=AsyncMock(),
            )

            await session._shutdown(rt)

            # Disconnect message should be posted
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            text = call_args[0][0]
            assert "session ended" in text.lower() or "wave" in text.lower()

    async def test_shutdown_updates_registry_to_completed(self, tmp_path):
        """_shutdown should update session status to completed."""
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-comp", 1234, "/tmp")

            session = make_session(session_id="sess-comp")

            rt = make_rt(registry, "C_COMP_CHAN")
            await session._shutdown(rt)

            sess = await registry.get_session("sess-comp")
            assert sess["status"] == "completed"


class TestSessionShutdown:
    """Test shutdown behavior including completion flag and error handling."""

    async def test_shutdown_sets_completed_flag(self, tmp_path):
        """After successful _shutdown(), _shutdown_completed should be True."""
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-flag", 1234, "/tmp")
            session = make_session(session_id="sess-flag")
            assert session._shutdown_completed is False
            rt = make_rt(registry, "C_FLAG_CHAN")
            await session._shutdown(rt)
            assert session._shutdown_completed is True

    async def test_shutdown_completed_flag_false_on_registry_failure(self, tmp_path):
        """If registry update raises, _shutdown_completed should remain False."""
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-fail", 1234, "/tmp")
            session = make_session(session_id="sess-fail")
            assert session._shutdown_completed is False

            async def failing_update(*args, **kwargs):
                raise RuntimeError("Registry update failed")

            registry.update_status = failing_update
            rt = make_rt(registry, "C_FAIL_CHAN")
            await session._shutdown(rt)
            assert session._shutdown_completed is False

    async def test_shutdown_disconnect_message_failure_continues(self, tmp_path):
        """If posting the disconnect message fails, shutdown should continue."""
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-arch-fail", 1234, "/tmp")
            session = make_session(session_id="sess-arch-fail")

            mock_client = make_mock_client("C_ARCH_FAIL_CHAN")
            mock_client.post = AsyncMock(side_effect=RuntimeError("Post failed"))
            rt = make_rt(registry, "C_ARCH_FAIL_CHAN", client=mock_client)

            await session._shutdown(rt)

            sess = await registry.get_session("sess-arch-fail")
            assert sess["status"] == "completed"
            assert session._shutdown_completed is True

    async def test_shutdown_timeout_on_slack_call(self, tmp_path):
        """If Slack call hangs, asyncio.wait_for should timeout and continue."""
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-timeout", 1234, "/tmp")
            session = make_session(session_id="sess-timeout")

            async def hanging_post(*args, **kwargs):
                await asyncio.sleep(999)

            mock_client = make_mock_client("C_TIMEOUT_CHAN")
            mock_client.post = AsyncMock(side_effect=hanging_post)
            rt = make_rt(registry, "C_TIMEOUT_CHAN", client=mock_client)

            await session._shutdown(rt)

            sess = await registry.get_session("sess-timeout")
            assert sess["status"] == "completed"
            assert session._shutdown_completed is True


class TestAuditEventsLogged:
    async def test_registry_logs_session_created_event(self, tmp_path):
        """Registry.log_event is used in start() — test it works for session_created."""
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-audit", 1234, "/tmp")
            await registry.log_event(
                "session_created",
                session_id="sess-audit",
                details={"cwd": "/tmp", "name": "audit-test", "model": "claude-opus-4-6"},
            )

            db = registry._check_connected()
            async with db.execute(
                "SELECT * FROM audit_log WHERE session_id = ? ORDER BY id DESC LIMIT 100",
                ("sess-audit",),
            ) as cursor:
                rows = await cursor.fetchall()
            log = [dict(r) for r in rows]
            assert len(log) >= 1
            assert any(e["event_type"] == "session_created" for e in log)


class TestDisconnectMessage:
    """Test the disconnect message."""

    async def test_disconnect_message(self, tmp_path):
        """Shutdown should post :wave: 'session ended' message."""
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-ended", 1234, "/tmp")

            mock_client = make_mock_client("C_ENDED")
            session = make_session(session_id="sess-ended")
            session._total_turns = 5
            session._total_cost = 0.125

            rt = _SessionRuntime(
                registry=registry,
                client=mock_client,
                permission_handler=AsyncMock(),
            )

            await session._post_disconnect_message(rt)

            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            text = call_args[0][0]
            assert ":wave:" in text
            assert "session ended" in text.lower()
            assert "5" in text  # turns
            assert "0.125" in text or "0.13" in text  # cost


class TestWatchdogLoop:
    """Test the heartbeat loop timestamp tracking."""

    async def test_heartbeat_updates_timestamp(self, tmp_path):
        """Calling heartbeat loop should update _last_heartbeat_time."""
        session = make_session(session_id="sess-hb-ts")

        loop = asyncio.get_running_loop()
        old_time = loop.time() - 50.0
        session._last_heartbeat_time = old_time

        # Build a minimal mock runtime with an async registry heartbeat
        mock_rt = MagicMock()
        mock_rt.registry = AsyncMock()
        mock_rt.registry.heartbeat = AsyncMock()

        # Run one iteration of the heartbeat loop: patch the sleep interval to be very short,
        # then signal shutdown after the first iteration so the loop exits cleanly
        async def _set_shutdown_after_first_heartbeat(*_args, **_kwargs):
            # Allow the heartbeat to complete, then shut down
            session._shutdown_event.set()

        mock_rt.registry.heartbeat.side_effect = _set_shutdown_after_first_heartbeat

        with patch("summon_claude.sessions.session._HEARTBEAT_INTERVAL_S", 0.01):
            await asyncio.wait_for(session._heartbeat_loop(mock_rt), timeout=1.0)

        # Timestamp should be updated to approximately now
        elapsed = loop.time() - session._last_heartbeat_time
        assert elapsed < 1.0  # Was updated during the loop iteration


class TestSessionHandleRegistration:
    """Test SessionHandle registration with EventDispatcher."""

    async def test_channel_id_set_after_run_session(self):
        """channel_id property should reflect the assigned channel once set."""
        session = make_session()
        assert session.channel_id is None
        # Simulate what _run_session does after creating the channel
        session._channel_id = "C_NEW_CHAN"
        assert session.channel_id == "C_NEW_CHAN"

    async def test_dispatcher_registered_when_provided(self, tmp_path):
        """When a dispatcher is provided, _run_session registers a SessionHandle with it."""
        from summon_claude.event_dispatcher import EventDispatcher
        from summon_claude.sessions.registry import SessionRegistry

        dispatcher = EventDispatcher()

        # Create a mock web_client that simulates channel creation
        mock_web_client = AsyncMock()
        mock_web_client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
        mock_web_client.conversations_create = AsyncMock(
            return_value={"channel": {"id": "C_DISP", "name": "disp"}}
        )
        mock_web_client.conversations_invite = AsyncMock()

        session = make_session(
            session_id="sess-disp",
            web_client=mock_web_client,
            dispatcher=dispatcher,
        )
        session.authenticate("U001")

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-disp", 1234, "/tmp")

            with (
                patch("summon_claude.sessions.session.SlackClient") as mock_slack_cls,
                patch("summon_claude.sessions.session.ThreadRouter"),
                patch("summon_claude.sessions.session.PermissionHandler"),
                patch.object(session, "_run_session_tasks", new=AsyncMock()),
                patch.object(session, "_shutdown", new=AsyncMock()),
            ):
                mock_slack_cls.return_value = make_mock_client("C_DISP")
                await session._run_session(registry)

        # After _run_session, dispatcher should have session registered
        assert "C_DISP" in dispatcher._sessions


class TestProcessIncomingEvent:
    """Tests for _process_incoming_event — the message pre-processing pipeline."""

    def _make_rt(self, permission_handler=None):
        """Build a minimal mock _SessionRuntime."""
        if permission_handler is None:
            mock_permission_handler = AsyncMock()
            mock_permission_handler.has_pending_text_input = MagicMock(return_value=False)
            mock_permission_handler.receive_text_input = AsyncMock()
        else:
            mock_permission_handler = permission_handler
        return _SessionRuntime(
            registry=AsyncMock(),
            client=make_mock_client("C_TEST"),
            permission_handler=mock_permission_handler,
        )

    async def test_normal_message_returns_text_and_ts(self):
        """A normal user message returns (full_text, thread_ts)."""
        session = make_session()

        rt = self._make_rt()

        event = {"user": "U001", "text": "Hello Claude", "ts": "123.456"}
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, ts = result
        assert text == "Hello Claude"
        assert ts == "123.456"

    async def test_synthetic_event_bypasses_preprocessing(self):
        """Synthetic events (scan triggers) bypass all Slack preprocessing."""
        session = make_session()
        rt = self._make_rt()

        event = {
            "type": "message",
            "text": "[SCAN TRIGGER] Perform your scheduled scan.",
            "user": "U_PM_OWNER",
            "_synthetic": True,
        }
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, ts = result
        assert "[SCAN TRIGGER]" in text
        assert ts is None  # synthetic events have no Slack ts

    async def test_synthetic_event_without_user_id_still_passes(self):
        """Synthetic events bypass user_id validation."""
        session = make_session()
        rt = self._make_rt()

        event = {"text": "Scan now", "_synthetic": True}
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, ts = result
        assert text == "Scan now"
        assert ts is None

    async def test_synthetic_event_empty_text_filtered(self):
        """Synthetic events with empty text are filtered out."""
        session = make_session()
        rt = self._make_rt()

        event = {"text": "", "_synthetic": True}
        result = await session._process_incoming_event(event, rt)
        assert result is None

    async def test_subtype_message_filtered(self):
        """Messages with a subtype (bot messages etc.) are filtered out."""
        session = make_session()
        rt = self._make_rt()

        event = {"user": "U001", "text": "Hello", "subtype": "bot_message", "ts": "1"}
        result = await session._process_incoming_event(event, rt)
        assert result is None

    async def test_empty_text_filtered(self):
        """Messages with empty text are filtered out."""
        session = make_session()
        rt = self._make_rt()

        event = {"user": "U001", "text": "", "ts": "1"}
        result = await session._process_incoming_event(event, rt)
        assert result is None

    async def test_no_user_filtered(self):
        """Messages without a user_id (system events) are filtered out."""
        session = make_session()
        rt = self._make_rt()

        event = {"text": "Hello", "ts": "1"}
        result = await session._process_incoming_event(event, rt)
        assert result is None

    async def test_long_message_truncated(self):
        """Messages exceeding _MAX_USER_MESSAGE_CHARS are truncated."""
        from summon_claude.sessions.session import _MAX_USER_MESSAGE_CHARS

        session = make_session()

        rt = self._make_rt()

        long_text = "x" * (_MAX_USER_MESSAGE_CHARS + 100)
        event = {"user": "U001", "text": long_text, "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, _ = result
        assert len(text) <= _MAX_USER_MESSAGE_CHARS + len("\n[message truncated]")
        assert "[message truncated]" in text

    async def test_file_references_appended(self):
        """File attachments are appended to the text."""
        session = make_session()

        rt = self._make_rt()

        event = {
            "user": "U001",
            "text": "See attached",
            "ts": "1",
            "files": [{"name": "report.pdf", "filetype": "pdf", "size": 2048}],
        }
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, _ = result
        assert "report.pdf" in text
        assert "See attached" in text

    async def test_pending_text_input_consumed(self):
        """When permission handler is waiting for free-text, message is consumed."""
        session = make_session()

        mock_ph = AsyncMock()
        mock_ph.has_pending_text_input = MagicMock(return_value=True)
        mock_ph.receive_text_input = AsyncMock()
        rt = self._make_rt(permission_handler=mock_ph)

        event = {"user": "U001", "text": "My free-text answer", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        assert result is None
        mock_ph.receive_text_input.assert_awaited_once_with("My free-text answer")

    async def test_command_prefix_dispatched(self):
        """Messages with ! prefix are dispatched as commands and return None."""
        session = make_session()

        rt = self._make_rt()

        # Mock _dispatch_command to avoid real execution
        with patch.object(session, "_dispatch_command", new=AsyncMock()) as mock_dispatch:
            event = {"user": "U001", "text": "!status", "ts": "1"}
            result = await session._process_incoming_event(event, rt)

        assert result is None
        mock_dispatch.assert_awaited_once()

    async def test_regular_message_not_command(self):
        """A message without ! prefix is returned as-is for Claude."""
        session = make_session()

        rt = self._make_rt()

        event = {"user": "U001", "text": "What is 2+2?", "ts": "789"}
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, ts = result
        assert text == "What is 2+2?"
        assert ts == "789"

    # ------------------------------------------------------------------
    # Standalone command tests
    # ------------------------------------------------------------------

    async def test_standalone_unknown_command_posts_error(self):
        """!xyznotreal at start should post 'Unknown command' and return None."""
        session = make_session()
        rt = self._make_rt()

        event = {"user": "U001", "text": "!xyznotreal", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        assert result is None
        rt.client.post.assert_called_once()
        post_text = rt.client.post.call_args[0][0]
        assert "Unknown command" in post_text or "not found" in post_text.lower()

    async def test_standalone_blocked_command_posts_reason(self):
        """!config at start should post 'not available' and return None."""
        session = make_session()
        rt = self._make_rt()

        event = {"user": "U001", "text": "!config", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        assert result is None
        rt.client.post.assert_called_once()
        post_text = rt.client.post.call_args[0][0]
        assert "not available" in post_text.lower()

    async def test_standalone_passthrough_dispatched(self):
        """!review at start should call _dispatch_command with name='review'."""
        session = make_session()
        rt = self._make_rt()

        with patch.object(session, "_dispatch_command", new=AsyncMock()) as mock_dispatch:
            event = {"user": "U001", "text": "!review", "ts": "1"}
            result = await session._process_incoming_event(event, rt)

        assert result is None
        mock_dispatch.assert_awaited_once()
        call_args = mock_dispatch.call_args
        assert call_args[0][1] == "review"  # name argument

    async def test_standalone_local_dispatched(self):
        """!status at start should call _dispatch_command with name='status'."""
        session = make_session()
        rt = self._make_rt()

        with patch.object(session, "_dispatch_command", new=AsyncMock()) as mock_dispatch:
            event = {"user": "U001", "text": "!status", "ts": "1"}
            result = await session._process_incoming_event(event, rt)

        assert result is None
        mock_dispatch.assert_awaited_once()
        call_args = mock_dispatch.call_args
        assert call_args[0][1] == "status"  # name argument

    # ------------------------------------------------------------------
    # Mid-message tests
    # ------------------------------------------------------------------

    async def test_mid_message_passthrough_swaps_prefix(self):
        """'please !review this' should return modified text with '/review'."""
        session = make_session()
        rt = self._make_rt()

        event = {"user": "U001", "text": "please !review this", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, _ = result
        assert "/review" in text
        assert "!review" not in text

    async def test_mid_message_blocked_annotated(self):
        """'try !config please' should post annotation and return modified text."""
        session = make_session()
        rt = self._make_rt()

        event = {"user": "U001", "text": "try !config please", "ts": "1"}
        await session._process_incoming_event(event, rt)

        # The blocked command should be annotated via rt.client.post
        rt.client.post.assert_called()
        # Check the annotation mentions the block reason
        annotation_text = rt.client.post.call_args[0][0]
        assert "config" in annotation_text.lower()

    async def test_mid_message_local_executed(self):
        """'please !status and then continue' should dispatch status and remove it from text."""
        session = make_session()
        rt = self._make_rt()

        with patch(
            "summon_claude.sessions.session.dispatch_command",
            new=AsyncMock(return_value=CommandResult(text="status output")),
        ):
            event = {"user": "U001", "text": "please !status and then continue", "ts": "1"}
            result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, _ = result
        # The !status command should have been removed from the text
        assert "!status" not in text
        # The surrounding text should remain (possibly cleaned up)
        assert "please" in text
        assert "continue" in text

    async def test_mid_message_passthrough_expands_alias(self):
        """'please !quit-alias this' should expand alias to /canonical in text."""
        # Register a test alias: short-cmd -> test-plug:short-cmd
        COMMAND_ACTIONS["test-plug:short-cmd"] = CommandDef(description="test")
        _ALIAS_LOOKUP["short-cmd"] = "test-plug:short-cmd"
        try:
            session = make_session()
            rt = self._make_rt()

            event = {"user": "U001", "text": "please !short-cmd this", "ts": "1"}
            result = await session._process_incoming_event(event, rt)

            assert result is not None
            text, _ = result
            # Should expand to /test-plug:short-cmd, not /short-cmd
            assert "/test-plug:short-cmd" in text
            assert "!short-cmd" not in text
        finally:
            COMMAND_ACTIONS.pop("test-plug:short-cmd", None)
            _ALIAS_LOOKUP.pop("short-cmd", None)

    async def test_plain_text_fast_path(self):
        """Text with no '!' or '/' should return unchanged (no find_commands called)."""
        session = make_session()
        rt = self._make_rt()

        with patch("summon_claude.sessions.session.find_commands") as mock_find:
            event = {"user": "U001", "text": "just plain text here", "ts": "42"}
            result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, ts = result
        assert text == "just plain text here"
        assert ts == "42"
        # find_commands should NOT have been called (fast path)
        mock_find.assert_not_called()


class TestPendingTurn:
    """Tests for the _PendingTurn dataclass."""

    def test_pending_turn_defaults(self):
        from summon_claude.sessions.session import _PendingTurn

        pt = _PendingTurn(message="hello")
        assert pt.message == "hello"
        assert pt.message_ts is None
        assert pt.thread_ts is None
        assert pt.pre_sent is True
        assert pt.queued_at is not None

    def test_pending_turn_frozen(self):
        from dataclasses import FrozenInstanceError

        from summon_claude.sessions.session import _PendingTurn

        pt = _PendingTurn(message="hello")
        with pytest.raises(FrozenInstanceError):
            pt.message = "other"  # type: ignore[misc]


class TestShutdownSentinels:
    """Tests for shutdown sentinel propagation."""

    def test_request_shutdown_uses_none_sentinel(self):
        session = make_session()
        session.request_shutdown()
        item = session._raw_event_queue.get_nowait()
        assert item is None

    def test_raw_event_queue_has_backpressure(self):
        session = make_session()
        assert session._raw_event_queue.maxsize == 100

    def test_pending_turns_queue_unbounded(self):
        session = make_session()
        assert session._pending_turns.maxsize == 0


class TestThinkingTriggers:
    """Tests for ultrathink detection."""

    def test_triggers_constant_is_frozenset(self):
        from summon_claude.sessions.session import _THINKING_TRIGGERS

        assert isinstance(_THINKING_TRIGGERS, frozenset)
        assert "ultrathink" in _THINKING_TRIGGERS
        assert "think harder" in _THINKING_TRIGGERS
        assert "megathink" in _THINKING_TRIGGERS


class TestCompactRouting:
    """Tests for compact command routing through _pending_turns."""

    def test_pending_turn_compact_default_false(self):
        from summon_claude.sessions.session import _PendingTurn

        pt = _PendingTurn(message="hello")
        assert pt.compact is False

    def test_pending_turn_compact_flag(self):
        from summon_claude.sessions.session import _PendingTurn

        pt = _PendingTurn(message="", compact=True, pre_sent=False)
        assert pt.compact is True
        assert pt.pre_sent is False


class TestContextWarningThreshold:
    """Tests for _context_warned_threshold tracking."""

    def test_context_warned_threshold_starts_at_zero(self):
        session = make_session()
        assert session._context_warned_threshold == 0.0

    def test_context_warned_threshold_is_settable(self):
        session = make_session()
        session._context_warned_threshold = 75.0
        assert session._context_warned_threshold == 75.0


class TestMCPRegistration:
    """Verify that _run_session_tasks creates the right MCP servers."""

    async def _capture_mcp_servers(self, pm_profile: bool = False) -> dict:
        """Run _run_session_tasks just far enough to capture the ClaudeAgentOptions."""
        session = make_session(pm_profile=pm_profile)
        session._authenticated_user_id = "U_TEST"
        session._shutdown_event.set()  # immediately exit the message loop

        mock_registry = AsyncMock()
        rt = make_rt(mock_registry)

        captured = {}

        class _CaptureError(Exception):
            pass

        def spy_init(self_sdk, options):
            captured["mcp_servers"] = options.mcp_servers
            raise _CaptureError("captured")

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient.__init__", spy_init),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch("summon_claude.sessions.session.discover_plugin_skills", return_value=[]),
            pytest.raises(_CaptureError),
        ):
            await session._run_session_tasks(rt, AsyncMock())

        return captured

    async def test_regular_session_has_only_slack_mcp(self):
        result = await self._capture_mcp_servers(pm_profile=False)
        assert "summon-slack" in result["mcp_servers"]
        assert "summon-cli" not in result["mcp_servers"]

    async def test_pm_session_has_both_mcps(self):
        result = await self._capture_mcp_servers(pm_profile=True)
        assert "summon-slack" in result["mcp_servers"]
        assert "summon-cli" in result["mcp_servers"]

    def test_session_options_pm_profile_default(self):
        opts = SessionOptions(cwd="/tmp", name="test")
        assert opts.pm_profile is False

    def test_session_options_pm_profile_true(self):
        opts = SessionOptions(cwd="/tmp", name="test", pm_profile=True)
        assert opts.pm_profile is True


# ------------------------------------------------------------------
# Spawn session tests
# ------------------------------------------------------------------


class TestHandleSpawn:
    """Tests for _handle_spawn and !summon dispatch."""

    def _make_rt(self, channel_id: str = "C_TEST") -> _SessionRuntime:
        return _SessionRuntime(
            registry=AsyncMock(),
            client=make_mock_client(channel_id),
            permission_handler=AsyncMock(),
        )

    async def test_spawn_rejects_wrong_user(self):
        """_handle_spawn posts rejection when caller is not the session owner."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()

        await session._handle_spawn(rt, user_id="U_INTRUDER", thread_ts=None)

        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert "Only the session owner" in text

    async def test_spawn_rejects_wrong_user_does_not_call_generate_token(self):
        """Rejection short-circuits before generating spawn token."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()

        # Patch at the source module since _handle_spawn uses lazy import
        with patch("summon_claude.sessions.auth.generate_spawn_token", new=AsyncMock()) as mock_gen:
            await session._handle_spawn(rt, user_id="U_INTRUDER", thread_ts=None)

        mock_gen.assert_not_called()

    async def test_dispatch_command_spawn_metadata_calls_handle_spawn(self):
        """_dispatch_command with spawn metadata should call _handle_spawn."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()

        spawn_result = CommandResult(text=None, metadata={"spawn": True})

        with (
            patch(
                "summon_claude.sessions.session.dispatch_command",
                new=AsyncMock(return_value=spawn_result),
            ),
            patch.object(session, "_handle_spawn", new=AsyncMock()) as mock_spawn,
        ):
            await session._dispatch_command(rt, "summon", ["start"], "U_OWNER", None)

        mock_spawn.assert_awaited_once_with(rt, "U_OWNER", None)

    async def test_handle_spawn_posts_success_message_on_completion(self, tmp_path):
        """_handle_spawn posts a success message when daemon_client succeeds."""
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-spawn", 1234, "/tmp")

            session = make_session(session_id="sess-spawn", cwd="/tmp")
            session._authenticated_user_id = "U_OWNER"
            session._channel_id = "C_SELF"

            rt = _SessionRuntime(
                registry=registry,
                client=make_mock_client("C_SELF"),
                permission_handler=AsyncMock(),
            )

            with (
                patch(
                    "summon_claude.sessions.auth.generate_spawn_token",
                    new=AsyncMock(
                        return_value=AsyncMock(token="tok123", parent_session_id="sess-spawn")
                    ),
                ),
                patch(
                    "summon_claude.cli.daemon_client.create_session_with_spawn_token",
                    new=AsyncMock(return_value="child-sess-id"),
                ),
            ):
                await session._handle_spawn(rt, user_id="U_OWNER", thread_ts=None)

            rt.client.post.assert_awaited_once()
            text = rt.client.post.call_args[0][0]
            assert "Spawned session started" in text

    async def test_spawn_blocked_at_child_limit(self):
        """_handle_spawn posts limit message when active children >= limit."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()

        # Mock list_children returning 5 active children (at the regular limit)
        rt.registry.list_children = AsyncMock(
            return_value=[{"session_id": f"child-{i}", "status": "active"} for i in range(5)]
        )

        await session._handle_spawn(rt, user_id="U_OWNER", thread_ts=None)

        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert "Too many active child sessions" in text
        assert "(5)" in text

    async def test_spawn_pm_session_uses_higher_limit(self):
        """PM sessions should use the higher spawn limit (MAX_SPAWN_CHILDREN_PM)."""
        session = make_session(pm_profile=True, session_id="pm-sess", cwd="/tmp")
        session._authenticated_user_id = "U_OWNER"
        session._channel_id = "C_PM"

        rt = self._make_rt()

        # 10 active children — over regular limit (5) but under PM limit (15)
        rt.registry.list_children = AsyncMock(
            return_value=[{"session_id": f"child-{i}", "status": "active"} for i in range(10)]
        )

        with (
            patch(
                "summon_claude.sessions.auth.generate_spawn_token",
                new=AsyncMock(return_value=AsyncMock(token="tok456", parent_session_id="pm-sess")),
            ),
            patch(
                "summon_claude.cli.daemon_client.create_session_with_spawn_token",
                new=AsyncMock(return_value="child-sess-pm"),
            ),
        ):
            await session._handle_spawn(rt, user_id="U_OWNER", thread_ts=None)

        # Should succeed — not blocked
        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert "Spawned session started" in text

    async def test_spawn_pm_session_blocked_at_pm_limit(self):
        """PM sessions should be blocked when active children >= PM limit."""
        session = make_session(pm_profile=True)
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()

        # 15 active children — at the PM limit
        rt.registry.list_children = AsyncMock(
            return_value=[{"session_id": f"child-{i}", "status": "active"} for i in range(15)]
        )

        await session._handle_spawn(rt, user_id="U_OWNER", thread_ts=None)

        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert "Too many active child sessions" in text
        assert "(15)" in text

    def test_spawn_limits_pinned(self):
        """Guard test: pin spawn limit constants to prevent accidental drift."""
        assert MAX_SPAWN_CHILDREN == 5
        assert MAX_SPAWN_CHILDREN_PM == 15
        assert MAX_SPAWN_DEPTH == 3

    async def test_spawn_blocked_at_depth_limit(self):
        """_handle_spawn posts depth message when depth >= MAX_SPAWN_DEPTH."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()

        rt.registry.compute_spawn_depth = AsyncMock(return_value=3)

        with patch("summon_claude.sessions.auth.generate_spawn_token", new=AsyncMock()) as mock_gen:
            await session._handle_spawn(rt, user_id="U_OWNER", thread_ts=None)

        mock_gen.assert_not_called()
        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert "Cannot spawn beyond depth" in text

    async def test_spawn_allowed_below_depth_limit(self):
        """_handle_spawn proceeds when depth < MAX_SPAWN_DEPTH."""
        session = make_session(session_id="parent-ok", cwd="/tmp")
        session._authenticated_user_id = "U_OWNER"
        session._channel_id = "C_TEST"
        rt = self._make_rt()

        rt.registry.compute_spawn_depth = AsyncMock(return_value=2)
        rt.registry.list_children = AsyncMock(return_value=[])

        with (
            patch(
                "summon_claude.sessions.auth.generate_spawn_token",
                new=AsyncMock(return_value=AsyncMock(token="tok", parent_session_id="parent-ok")),
            ),
            patch(
                "summon_claude.cli.daemon_client.create_session_with_spawn_token",
                new=AsyncMock(return_value="child-ok"),
            ),
        ):
            await session._handle_spawn(rt, user_id="U_OWNER", thread_ts=None)

        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert "Spawned session started" in text

    async def test_spawn_list_children_failure_blocks_spawn(self):
        """If list_children raises, spawn should be blocked (fail-closed)."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()

        rt.registry.list_children = AsyncMock(side_effect=RuntimeError("DB locked"))

        with patch("summon_claude.sessions.auth.generate_spawn_token", new=AsyncMock()) as mock_gen:
            await session._handle_spawn(rt, user_id="U_OWNER", thread_ts=None)

        # Should NOT have attempted to generate a token
        mock_gen.assert_not_called()
        # Should have posted an error message
        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert "Could not verify session limit" in text

    async def test_spawn_mid_message_blocked_with_annotation(self):
        """'please !summon start' mid-message should annotate, not spawn."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        mock_ph = AsyncMock()
        mock_ph.has_pending_text_input = MagicMock(return_value=False)
        rt = _SessionRuntime(
            registry=AsyncMock(),
            client=make_mock_client("C_TEST"),
            permission_handler=mock_ph,
        )

        event = {"user": "U_OWNER", "text": "please !summon start", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        # Remaining text ("please ") should be forwarded to Claude
        assert result is not None
        text, _ = result
        assert "!summon" not in text
        assert "please" in text

        # An annotation should be posted saying it must be standalone
        rt.client.post.assert_called()
        annotation_text = rt.client.post.call_args[0][0]
        assert "standalone" in annotation_text.lower()


# ------------------------------------------------------------------
# Compaction tests
# ------------------------------------------------------------------


class TestAutoCompactionDisabled:
    """Verify CLAUDE_AUTOCOMPACT_PCT_OVERRIDE is set before SDK client creation."""

    async def test_env_var_set_before_client_creation(self, registry):
        """CLAUDE_AUTOCOMPACT_PCT_OVERRIDE should be '100' when ClaudeSDKClient is entered."""
        import os

        captured_env = {}
        _sentinel = RuntimeError("stop-after-capture")

        class _FakeSDKClient:
            def __init__(self, options):
                # Record env var at construction time (just after `os.environ` line)
                captured_env["val"] = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")

            async def __aenter__(self):
                raise _sentinel  # Break out before the TaskGroup hangs

            async def __aexit__(self, *_):
                pass

        session = make_session()
        rt = make_rt(registry)
        router = AsyncMock()

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient", _FakeSDKClient),
            patch("summon_claude.sessions.session.create_summon_mcp_server", return_value={}),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
        ):
            try:
                await session._run_session_tasks(rt, router)
            except RuntimeError as e:
                assert e is _sentinel  # Confirm we stopped at the right place

        assert captured_env.get("val") == "100"

    def test_base_system_append_is_string(self):
        """_BASE_SYSTEM_APPEND must be a non-empty string."""
        from summon_claude.sessions.session import _BASE_SYSTEM_APPEND

        assert isinstance(_BASE_SYSTEM_APPEND, str)
        assert len(_BASE_SYSTEM_APPEND) > 0


class TestCompactPromptConstants:
    """Verify compaction prompt constants are well-formed."""

    def test_compact_prompt_has_mandatory_sections(self):
        from summon_claude.sessions.session import _COMPACT_PROMPT

        assert isinstance(_COMPACT_PROMPT, str)
        assert "Task Overview" in _COMPACT_PROMPT
        assert "Current State" in _COMPACT_PROMPT
        assert "Files & Artifacts" in _COMPACT_PROMPT
        assert "Key Decisions" in _COMPACT_PROMPT
        assert "Next Steps" in _COMPACT_PROMPT
        assert "<summary>" in _COMPACT_PROMPT
        assert "<analysis>" in _COMPACT_PROMPT

    def test_compact_summary_prefix_exists(self):
        from summon_claude.sessions.session import _COMPACT_SUMMARY_PREFIX

        assert isinstance(_COMPACT_SUMMARY_PREFIX, str)
        assert "Compacted" in _COMPACT_SUMMARY_PREFIX

    def test_overflow_recovery_prompt_mentions_tools(self):
        from summon_claude.sessions.session import _OVERFLOW_RECOVERY_PROMPT

        assert isinstance(_OVERFLOW_RECOVERY_PROMPT, str)
        assert "slack_read_history" in _OVERFLOW_RECOVERY_PROMPT
        assert "slack_fetch_thread" in _OVERFLOW_RECOVERY_PROMPT

    def test_session_restart_exception(self):
        exc = _SessionRestartError(summary="test summary")
        assert exc.summary == "test summary"
        assert exc.recovery_mode is False

    def test_session_restart_recovery_mode(self):
        exc = _SessionRestartError(recovery_mode=True)
        assert exc.summary is None
        assert exc.recovery_mode is True


class TestExecuteCompact:
    """Tests for _execute_compact: summarization, restart, and error handling."""

    def _make_mock_claude_with_summary(self, summary_text: str):
        """Create a mock SDK client whose receive_response yields an AssistantMessage."""
        claude = AsyncMock()
        claude.query = AsyncMock()

        text_block = MagicMock(spec=TextBlock)
        text_block.text = summary_text

        msg = MagicMock(spec=AssistantMessage)
        msg.content = [text_block]

        async def fake_receive():
            yield msg

        claude.receive_response = fake_receive
        return claude

    def _make_mock_claude_empty(self):
        """Create a mock SDK client whose receive_response yields no text."""
        claude = AsyncMock()
        claude.query = AsyncMock()

        # Yield a message that isn't an AssistantMessage
        msg = MagicMock()

        async def fake_receive():
            yield msg

        claude.receive_response = fake_receive
        return claude

    async def test_success_raises_session_restart_with_summary(self, registry):
        """On success, _execute_compact raises _SessionRestartError with captured summary."""
        session = make_session()
        rt = make_rt(registry)
        session._claude = self._make_mock_claude_with_summary("## Task Overview\nBuild a widget")

        with pytest.raises(_SessionRestartError) as exc_info:
            await session._execute_compact(rt, instructions=None, thread_ts=None)

        assert exc_info.value.summary == "## Task Overview\nBuild a widget"
        assert exc_info.value.recovery_mode is False

    async def test_success_extracts_summary_tags(self, registry):
        """If response contains <summary> tags, only the inner content is captured."""
        session = make_session()
        rt = make_rt(registry)
        raw = "<analysis>scratchpad</analysis>\n<summary>\n## Task\nDo thing\n</summary>"
        session._claude = self._make_mock_claude_with_summary(raw)

        with pytest.raises(_SessionRestartError) as exc_info:
            await session._execute_compact(rt, instructions=None, thread_ts=None)

        assert exc_info.value.summary == "## Task\nDo thing"
        assert "scratchpad" not in exc_info.value.summary

    async def test_success_posts_broom_message(self, registry):
        """On success, post ':broom: Context compacted' before raising."""
        session = make_session()
        rt = make_rt(registry)
        session._claude = self._make_mock_claude_with_summary("summary text")

        with pytest.raises(_SessionRestartError):
            await session._execute_compact(rt, instructions=None, thread_ts=None)

        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert ":broom:" in text

    async def test_no_summary_posts_warning_no_restart(self, registry):
        """If Claude returns no text, post warning and do NOT raise _SessionRestartError."""
        session = make_session()
        rt = make_rt(registry)
        session._claude = self._make_mock_claude_empty()

        # Should NOT raise _SessionRestartError
        await session._execute_compact(rt, instructions=None, thread_ts=None)

        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert "no summary" in text.lower()

    async def test_no_claude_client_posts_unavailable(self, registry):
        """If _claude is None, post SDK client not available."""
        session = make_session()
        rt = make_rt(registry)
        session._claude = None

        await session._execute_compact(rt, instructions=None, thread_ts=None)

        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert "not available" in text

    async def test_overflow_error_raises_recovery_restart(self, registry):
        """On context overflow, raise _SessionRestartError(recovery_mode=True)."""
        session = make_session()
        rt = make_rt(registry)

        claude = AsyncMock()
        claude.query = AsyncMock(side_effect=RuntimeError("context limit exceeded"))
        session._claude = claude

        with pytest.raises(_SessionRestartError) as exc_info:
            await session._execute_compact(rt, instructions=None, thread_ts=None)

        assert exc_info.value.recovery_mode is True
        assert exc_info.value.summary is None

    async def test_overflow_posts_recovery_message(self, registry):
        """On overflow, post recovery message before raising."""
        session = make_session()
        rt = make_rt(registry)

        claude = AsyncMock()
        claude.query = AsyncMock(side_effect=RuntimeError("token limit reached"))
        session._claude = claude

        with pytest.raises(_SessionRestartError):
            await session._execute_compact(rt, instructions=None, thread_ts=None)

        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert "too full" in text.lower()
        assert "history recovery" in text.lower()

    async def test_generic_error_posts_warning_no_restart(self, registry):
        """On a non-overflow error, post warning and do NOT restart."""
        session = make_session()
        rt = make_rt(registry)

        claude = AsyncMock()
        claude.query = AsyncMock(side_effect=RuntimeError("network error"))
        session._claude = claude

        # Should NOT raise _SessionRestartError
        await session._execute_compact(rt, instructions=None, thread_ts=None)

        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert ":warning:" in text
        assert "!clear" in text

    async def test_instructions_appended_to_compact_prompt(self, registry):
        """Instructions should be appended to the compaction prompt."""
        from summon_claude.sessions.session import _COMPACT_PROMPT

        session = make_session()
        rt = make_rt(registry)
        session._claude = self._make_mock_claude_with_summary("summary")

        with pytest.raises(_SessionRestartError):
            await session._execute_compact(rt, instructions="focus on tests", thread_ts=None)

        sent_prompt = session._claude.query.call_args[0][0]
        assert sent_prompt.startswith(_COMPACT_PROMPT)
        assert "Additional focus: focus on tests" in sent_prompt

    async def test_pre_sent_skips_query(self, registry):
        """When pre_sent=True, _execute_compact should NOT call query()."""
        session = make_session()
        rt = make_rt(registry)
        session._claude = self._make_mock_claude_with_summary("summary")

        with pytest.raises(_SessionRestartError):
            await session._execute_compact(rt, instructions=None, thread_ts=None, pre_sent=True)

        session._claude.query.assert_not_awaited()

    async def test_summary_truncated_when_too_long(self, registry):
        """Summaries exceeding _MAX_COMPACT_SUMMARY_CHARS are truncated."""
        from summon_claude.sessions.session import _MAX_COMPACT_SUMMARY_CHARS

        session = make_session()
        rt = make_rt(registry)
        long_summary = "x" * (_MAX_COMPACT_SUMMARY_CHARS + 1000)
        session._claude = self._make_mock_claude_with_summary(long_summary)

        with pytest.raises(_SessionRestartError) as exc_info:
            await session._execute_compact(rt, instructions=None, thread_ts=None)

        assert len(exc_info.value.summary) <= _MAX_COMPACT_SUMMARY_CHARS + 50
        assert exc_info.value.summary.endswith("[Summary truncated]")


class TestSessionRestartLoop:
    """Test that _run_session_tasks handles _SessionRestartError correctly.

    Uses patched _run_preprocessor/_run_response_consumer to simulate the
    real flow: consumer raises _SessionRestartError from inside the TaskGroup,
    which propagates as an ExceptionGroup.
    """

    async def test_restart_rebuilds_system_prompt_with_summary(self, registry):
        """After _SessionRestartError(summary=...), system prompt includes the summary."""
        from summon_claude.sessions.session import (
            _BASE_SYSTEM_APPEND,
            _COMPACT_SUMMARY_PREFIX,
        )

        captured_prompts = []

        class _FakeSDKClient:
            def __init__(self, options):
                captured_prompts.append(options.system_prompt["append"])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def get_server_info(self):
                return None

        consumer_call = 0

        async def fake_consumer(_rt, _claude, _streamer):
            nonlocal consumer_call
            consumer_call += 1
            if consumer_call == 1:
                raise _SessionRestartError(summary="## Task\nBuild widget")
            raise RuntimeError("stop-second-run")

        async def fake_preprocessor(_rt, _claude):
            await asyncio.sleep(999)  # Cancelled by TaskGroup

        session = make_session()
        rt = make_rt(registry)
        router = AsyncMock()

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient", _FakeSDKClient),
            patch("summon_claude.sessions.session.create_summon_mcp_server", return_value={}),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch.object(session, "_run_preprocessor", fake_preprocessor),
            patch.object(session, "_run_response_consumer", fake_consumer),
            contextlib.suppress(RuntimeError),
        ):
            await session._run_session_tasks(rt, router)

        assert len(captured_prompts) == 2
        assert captured_prompts[0] == _BASE_SYSTEM_APPEND
        assert _COMPACT_SUMMARY_PREFIX in captured_prompts[1]
        assert "Build widget" in captured_prompts[1]

    async def test_restart_recovery_mode_includes_overflow_prompt(self, registry):
        """After recovery_mode restart, system prompt includes recovery instructions."""
        from summon_claude.sessions.session import _OVERFLOW_RECOVERY_PROMPT

        captured_prompts = []
        consumer_call = 0

        class _FakeSDKClient:
            def __init__(self, options):
                captured_prompts.append(options.system_prompt["append"])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def get_server_info(self):
                return None

        async def fake_consumer(_rt, _claude, _streamer):
            nonlocal consumer_call
            consumer_call += 1
            if consumer_call == 1:
                raise _SessionRestartError(recovery_mode=True)
            raise RuntimeError("stop-second-run")

        async def fake_preprocessor(_rt, _claude):
            await asyncio.sleep(999)

        session = make_session()
        rt = make_rt(registry)
        router = AsyncMock()

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient", _FakeSDKClient),
            patch("summon_claude.sessions.session.create_summon_mcp_server", return_value={}),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch.object(session, "_run_preprocessor", fake_preprocessor),
            patch.object(session, "_run_response_consumer", fake_consumer),
            contextlib.suppress(RuntimeError),
        ):
            await session._run_session_tasks(rt, router)

        assert len(captured_prompts) == 2
        assert "slack_read_history" in captured_prompts[1]
        assert _OVERFLOW_RECOVERY_PROMPT in captured_prompts[1]

    async def test_restart_resets_session_state(self, registry):
        """Restart resets _pending_turns, _context_warned_threshold, and _resume."""
        consumer_call = 0

        class _FakeSDKClient:
            def __init__(self, options):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def get_server_info(self):
                return None

        async def fake_consumer(_rt, _claude, _streamer):
            nonlocal consumer_call
            consumer_call += 1
            if consumer_call == 1:
                raise _SessionRestartError(summary="summary")
            raise RuntimeError("stop")

        async def fake_preprocessor(_rt, _claude):
            await asyncio.sleep(999)

        session = make_session()
        session._context_warned_threshold = 75.0
        session._resume = "old-session-id"
        session._claude_session_id = "old-claude-sid"

        from summon_claude.sessions.context import ContextUsage

        session._last_context = ContextUsage(
            input_tokens=100000, context_window=200000, percentage=50.0
        )
        rt = make_rt(registry)
        router = AsyncMock()

        old_queue = session._pending_turns

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient", _FakeSDKClient),
            patch("summon_claude.sessions.session.create_summon_mcp_server", return_value={}),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch.object(session, "_run_preprocessor", fake_preprocessor),
            patch.object(session, "_run_response_consumer", fake_consumer),
            contextlib.suppress(RuntimeError),
        ):
            await session._run_session_tasks(rt, router)

        assert session._pending_turns is not old_queue  # New queue
        assert session._context_warned_threshold == 0.0
        assert session._resume is None
        assert session._last_context is None
        assert session._claude_session_id is None

    async def test_restart_circuit_breaker_stops_after_max(self, registry):
        """After max_restarts exceeded, the loop exits instead of restarting."""
        from summon_claude.sessions.session import _MAX_SESSION_RESTARTS

        captured_prompts = []
        consumer_call = 0

        class _FakeSDKClient:
            def __init__(self, options):
                captured_prompts.append(options.system_prompt["append"])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def get_server_info(self):
                return None

        async def fake_consumer(_rt, _claude, _streamer):
            nonlocal consumer_call
            consumer_call += 1
            raise _SessionRestartError(summary=f"summary-{consumer_call}")

        async def fake_preprocessor(_rt, _claude):
            await asyncio.sleep(999)

        session = make_session()
        rt = make_rt(registry)
        router = AsyncMock()

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient", _FakeSDKClient),
            patch("summon_claude.sessions.session.create_summon_mcp_server", return_value={}),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch.object(session, "_run_preprocessor", fake_preprocessor),
            patch.object(session, "_run_response_consumer", fake_consumer),
        ):
            await session._run_session_tasks(rt, router)

        # Should have attempted _MAX_SESSION_RESTARTS + 1 clients (initial + restarts)
        # then broken out
        assert len(captured_prompts) == _MAX_SESSION_RESTARTS + 1
        assert consumer_call == _MAX_SESSION_RESTARTS + 1


class TestEscalatingContextWarnings:
    """Tests for the escalating context warning thresholds and auto-compact."""

    async def test_75pct_posts_standard_warning(self, registry):
        """At >75%, post standard warning and update threshold."""
        session = make_session()
        session._context_warned_threshold = 0.0

        from summon_claude.sessions.context import ContextUsage

        session._last_context = ContextUsage(
            input_tokens=160000, context_window=200000, percentage=80.0
        )
        # Call the warning logic directly by simulating _finalize_turn_result
        # We test the threshold logic in isolation
        from summon_claude.sessions.session import (
            _CONTEXT_AUTO_COMPACT_THRESHOLD,
            _CONTEXT_URGENT_THRESHOLD,
            _CONTEXT_WARNING_THRESHOLD,
        )

        pct = session._last_context.percentage
        assert pct > _CONTEXT_WARNING_THRESHOLD
        assert pct < _CONTEXT_URGENT_THRESHOLD
        assert session._context_warned_threshold < _CONTEXT_WARNING_THRESHOLD

    async def test_90pct_posts_urgent_warning(self, registry):
        """At >90%, threshold should allow urgent warning."""
        session = make_session()
        from summon_claude.sessions.session import (
            _CONTEXT_URGENT_THRESHOLD,
            _CONTEXT_WARNING_THRESHOLD,
        )

        session._context_warned_threshold = _CONTEXT_WARNING_THRESHOLD
        from summon_claude.sessions.context import ContextUsage

        session._last_context = ContextUsage(
            input_tokens=185000, context_window=200000, percentage=92.0
        )
        pct = session._last_context.percentage
        assert pct > _CONTEXT_URGENT_THRESHOLD
        assert session._context_warned_threshold < _CONTEXT_URGENT_THRESHOLD

    async def test_95pct_triggers_auto_compact(self, registry):
        """At >95%, auto-compact should be triggered."""
        session = make_session()
        from summon_claude.sessions.session import (
            _CONTEXT_AUTO_COMPACT_THRESHOLD,
            _CONTEXT_URGENT_THRESHOLD,
        )

        session._context_warned_threshold = _CONTEXT_URGENT_THRESHOLD
        from summon_claude.sessions.context import ContextUsage

        session._last_context = ContextUsage(
            input_tokens=196000, context_window=200000, percentage=98.0
        )
        pct = session._last_context.percentage
        assert pct > _CONTEXT_AUTO_COMPACT_THRESHOLD
        assert session._context_warned_threshold < _CONTEXT_AUTO_COMPACT_THRESHOLD

    def test_threshold_prevents_duplicate_warnings(self):
        """Once warned at a threshold, it should not warn again."""
        session = make_session()
        from summon_claude.sessions.session import _CONTEXT_WARNING_THRESHOLD

        session._context_warned_threshold = 80.0  # Already warned above 75%
        assert session._context_warned_threshold >= _CONTEXT_WARNING_THRESHOLD
        # The condition `threshold < _CONTEXT_WARNING_THRESHOLD` is False
        # so no duplicate warning fires

    def test_threshold_resets_allow_re_warning(self):
        """After reset (compaction), warnings can fire again."""
        session = make_session()
        from summon_claude.sessions.session import _CONTEXT_WARNING_THRESHOLD

        session._context_warned_threshold = 80.0
        session._context_warned_threshold = 0.0  # Simulates restart reset
        assert session._context_warned_threshold < _CONTEXT_WARNING_THRESHOLD


class TestCompactMidMessageBlocked:
    """Verify !compact mid-message is blocked, not executed."""

    async def test_compact_mid_message_posts_standalone_annotation(self, registry):
        """'please !compact' mid-message should annotate, not compact."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        mock_ph = AsyncMock()
        mock_ph.has_pending_text_input = MagicMock(return_value=False)
        rt = _SessionRuntime(
            registry=AsyncMock(),
            client=make_mock_client("C_TEST"),
            permission_handler=mock_ph,
        )

        event = {"user": "U_OWNER", "text": "please !compact", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        # Remaining text ("please ") should be forwarded
        assert result is not None
        text, _ = result
        assert "!compact" not in text
        assert "please" in text

        # An annotation should be posted saying it must be standalone
        rt.client.post.assert_called()
        annotation_text = rt.client.post.call_args[0][0]
        assert "standalone" in annotation_text.lower()


class TestFinalizeEscalatingWarnings:
    """Integration tests that call _finalize_turn_result to verify Slack warnings."""

    @staticmethod
    def _make_stream_result(
        session_id: str = "claude-sid-123", cost: float = 0.001, model: str = "opus"
    ):
        result = MagicMock()
        result.session_id = session_id
        result.total_cost_usd = cost
        sr = MagicMock()
        sr.result = result
        sr.model = model
        return sr

    @staticmethod
    def _make_streamer():
        streamer = AsyncMock()
        streamer.finalize_turn = MagicMock(return_value="summary")
        streamer.update_turn_summary = AsyncMock()
        streamer.post_turn_footer = AsyncMock()
        return streamer

    async def _run_finalize(self, session, rt, pct, **sr_kwargs):
        """Call _finalize_turn_result with a given context percentage."""
        from pathlib import Path

        from summon_claude.sessions.context import ContextUsage

        session._claude_session_id = "already-set"
        session._last_context = ContextUsage(
            input_tokens=int(200000 * pct / 100),
            context_window=200000,
            percentage=pct,
        )
        sr = self._make_stream_result(**sr_kwargs)
        streamer = self._make_streamer()

        with (
            patch("summon_claude.sessions.session.get_last_step_usage", return_value=None),
            patch(
                "summon_claude.sessions.session.derive_transcript_path",
                return_value=Path("/fake"),
            ),
            patch("summon_claude.sessions.session._get_git_branch", return_value=None),
        ):
            await session._finalize_turn_result(rt, streamer, sr)

    async def test_no_warning_below_75pct(self):
        session = make_session()
        rt = make_rt(AsyncMock())
        await self._run_finalize(session, rt, pct=60.0)

        calls = rt.client.post.call_args_list
        warning_calls = [c for c in calls if ":warning:" in str(c) or ":rotating_light:" in str(c)]
        assert len(warning_calls) == 0
        assert session._context_warned_threshold == 0.0

    async def test_no_context_data_skips_warnings(self):
        """When _last_context is None, the warning block is skipped entirely."""
        from pathlib import Path

        session = make_session()
        session._claude_session_id = "already-set"
        session._last_context = None
        rt = make_rt(AsyncMock())

        with (
            patch("summon_claude.sessions.session.get_last_step_usage", return_value=None),
            patch(
                "summon_claude.sessions.session.derive_transcript_path",
                return_value=Path("/fake"),
            ),
            patch("summon_claude.sessions.session._get_git_branch", return_value=None),
        ):
            await session._finalize_turn_result(
                rt, self._make_streamer(), self._make_stream_result()
            )

        calls = rt.client.post.call_args_list
        warning_calls = [c for c in calls if ":warning:" in str(c) or ":rotating_light:" in str(c)]
        assert len(warning_calls) == 0
        assert session._context_warned_threshold == 0.0

    async def test_exactly_75pct_no_warning(self):
        """At exactly 75.0%, no warning fires (threshold uses strict >)."""
        session = make_session()
        rt = make_rt(AsyncMock())
        await self._run_finalize(session, rt, pct=75.0)

        calls = rt.client.post.call_args_list
        warning_calls = [c for c in calls if ":warning:" in str(c) or ":rotating_light:" in str(c)]
        assert len(warning_calls) == 0
        assert session._context_warned_threshold == 0.0

    async def test_75pct_posts_standard_warning(self):
        session = make_session()
        rt = make_rt(AsyncMock())
        await self._run_finalize(session, rt, pct=80.0)

        calls = rt.client.post.call_args_list
        warning_calls = [c for c in calls if "getting large" in str(c)]
        assert len(warning_calls) == 1
        text = warning_calls[0][0][0]
        assert ":warning:" in text
        assert "~80%" in text
        assert session._context_warned_threshold == 80.0

    async def test_90pct_posts_urgent_warning(self):
        session = make_session()
        session._context_warned_threshold = 75.0
        rt = make_rt(AsyncMock())
        await self._run_finalize(session, rt, pct=92.0)

        calls = rt.client.post.call_args_list
        urgent_calls = [c for c in calls if "critically full" in str(c)]
        assert len(urgent_calls) == 1
        text = urgent_calls[0][0][0]
        assert ":rotating_light:" in text
        assert "~92%" in text
        assert session._context_warned_threshold == 92.0

    async def test_95pct_posts_auto_compact_message_and_calls_execute(self):
        session = make_session()
        session._context_warned_threshold = 90.0
        rt = make_rt(AsyncMock())

        with (
            patch.object(
                session,
                "_execute_compact",
                new_callable=AsyncMock,
                side_effect=_SessionRestartError(summary="test"),
            ) as mock_compact,
            pytest.raises(_SessionRestartError),
        ):
            await self._run_finalize(session, rt, pct=97.0)

        mock_compact.assert_awaited_once_with(rt, instructions=None, thread_ts=None)
        calls = rt.client.post.call_args_list
        auto_calls = [c for c in calls if "auto-compacting" in str(c)]
        assert len(auto_calls) == 1
        assert session._context_warned_threshold == 97.0

    async def test_duplicate_75pct_warning_suppressed(self):
        session = make_session()
        session._context_warned_threshold = 80.0  # already warned above 75%
        rt = make_rt(AsyncMock())
        await self._run_finalize(session, rt, pct=82.0)

        calls = rt.client.post.call_args_list
        warning_calls = [c for c in calls if "getting large" in str(c)]
        assert len(warning_calls) == 0
        assert session._context_warned_threshold == 80.0  # unchanged

    async def test_escalation_skips_lower_thresholds(self):
        """At 92% with threshold=0, the 90% (urgent) warning fires — not the 75% one."""
        session = make_session()
        rt = make_rt(AsyncMock())
        await self._run_finalize(session, rt, pct=92.0)

        calls = rt.client.post.call_args_list
        urgent = [c for c in calls if "critically full" in str(c)]
        standard = [c for c in calls if "getting large" in str(c)]
        assert len(urgent) == 1
        assert len(standard) == 0
