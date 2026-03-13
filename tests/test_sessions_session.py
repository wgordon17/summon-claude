"""Tests for summon_claude.sessions.session — session orchestrator."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.config import SummonConfig
from summon_claude.sessions.auth import SessionAuth
from summon_claude.sessions.commands import (
    _ALIAS_LOOKUP,
    COMMAND_ACTIONS,
    CommandDef,
    CommandResult,
)
from summon_claude.sessions.session import (
    SessionOptions,
    SummonSession,
    _format_file_references,
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
    opt_fields = ("cwd", "name", "model", "effort", "resume")
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
    client.upload = AsyncMock()
    client.set_topic = AsyncMock()
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
        item = await asyncio.wait_for(session._message_queue.get(), timeout=1.0)
        assert item == ("", None)

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
                patch.object(session, "_run_message_loop", new=AsyncMock()),
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
