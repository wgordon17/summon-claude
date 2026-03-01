"""Tests for summon_claude.session — session orchestrator."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from summon_claude.auth import SessionAuth
from summon_claude.config import SummonConfig
from summon_claude.rate_limiter import RateLimiter
from summon_claude.session import SessionOptions, SummonSession, _format_file_references


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
        "session_id": "test-session",
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


class TestRateLimiter:
    def test_first_request_allowed(self):
        rl = RateLimiter(cooldown_seconds=2.0)
        assert rl.check("user1") is True

    def test_second_request_within_cooldown_denied(self):
        rl = RateLimiter(cooldown_seconds=2.0)
        rl.check("user1")
        assert rl.check("user1") is False

    def test_different_keys_are_independent(self):
        rl = RateLimiter(cooldown_seconds=2.0)
        rl.check("user1")
        assert rl.check("user2") is True

    async def test_rate_limiter_allows_after_cooldown(self):
        rl = RateLimiter(cooldown_seconds=0.1)
        rl.check("user1")
        assert rl.check("user1") is False
        await asyncio.sleep(0.2)
        assert rl.check("user1") is True

    def test_cleanup_removes_old_entries(self):
        rl = RateLimiter(cooldown_seconds=2.0)
        rl._last_attempt["old-user"] = time.monotonic() - 400  # older than max_age
        rl.check("user1")
        rl._cleanup(max_age=300.0)
        assert "old-user" not in rl._last_attempt
        assert "user1" in rl._last_attempt


class TestGenerateSessionToken:
    async def test_returns_session_auth(self, tmp_path):
        """generate_session_token should return a SessionAuth with correct fields."""
        from summon_claude.auth import generate_session_token
        from summon_claude.registry import SessionRegistry

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
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        assert not session._shutdown_event.is_set()
        session.request_shutdown()
        assert session._shutdown_event.is_set()

    async def test_request_shutdown_puts_sentinel_on_queue(self):
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        session.request_shutdown()
        item = await asyncio.wait_for(session._message_queue.get(), timeout=1.0)
        assert item == ("", None)

    def test_request_shutdown_idempotent(self):
        """Calling request_shutdown() twice should not raise."""
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        session.request_shutdown()
        session.request_shutdown()  # must not raise
        assert session._shutdown_event.is_set()

    def test_authenticate_sets_event_and_user(self):
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        assert not session._authenticated_event.is_set()
        session.authenticate("U001")
        assert session._authenticated_event.is_set()
        assert session._authenticated_user_id == "U001"

    def test_authenticate_clears_auth_token(self):
        """authenticate() should clear the auth token from memory."""
        config = make_config()
        auth = make_auth()
        session = SummonSession(config, make_options(), auth=auth)
        assert session._auth is not None
        session.authenticate("U001")
        assert session._auth is None

    def test_channel_id_property_initially_none(self):
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        assert session.channel_id is None


class TestWaitForAuth:
    async def test_returns_immediately_when_event_set(self):
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        session._authenticated_event.set()

        # Should complete quickly since event is already set
        await asyncio.wait_for(session._wait_for_auth(), timeout=2.0)

    async def test_returns_when_shutdown_event_set(self):
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        session._shutdown_event.set()

        await asyncio.wait_for(session._wait_for_auth(), timeout=2.0)


class TestSlashCommandHandler:
    """Test the /summon slash command handler internals."""

    async def test_verify_short_code_returns_result(self, tmp_path):
        """verify_short_code should return a result for a valid code."""
        from summon_claude.auth import generate_session_token, verify_short_code
        from summon_claude.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-1", 1234, "/tmp")
            auth = await generate_session_token(registry, "sess-1")

            result = await verify_short_code(registry, auth.short_code)
            assert result is not None

    async def test_slash_command_invalid_code_no_event_set(self, tmp_path):
        """Invalid code should NOT set authenticated_event."""
        from summon_claude.auth import verify_short_code
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-2", 1234, "/tmp")

            session = SummonSession(
                config,
                make_options(session_id="sess-2"),
                auth=make_auth(session_id="sess-2"),
            )

            result = await verify_short_code(registry, "badcod")
            assert result is None
            assert not session._authenticated_event.is_set()


class TestSessionShutdownSummary:
    async def test_shutdown_posts_summary_message(self, tmp_path):
        """_shutdown should post turns/cost summary to channel."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry
        from summon_claude.session import _SessionRuntime

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-sd", 1234, "/tmp")

            mock_provider = AsyncMock()
            mock_permission_handler = AsyncMock()
            mock_channel_manager = AsyncMock(spec=ChannelManager)

            session = SummonSession(
                config,
                make_options(session_id="sess-sd"),
                auth=make_auth(session_id="sess-sd"),
            )
            session._total_turns = 3
            session._total_cost = 0.0456

            rt = _SessionRuntime(
                registry=registry,
                provider=mock_provider,
                permission_handler=mock_permission_handler,
                channel_id="C_TEST_CHAN",
                channel_manager=mock_channel_manager,
            )
            mock_channel_manager.archive_session_channel = AsyncMock()

            await session._shutdown(rt)

            # Summary message should have been posted via provider
            mock_provider.post_message.assert_called_once()
            call_args = mock_provider.post_message.call_args
            assert call_args[0][0] == "C_TEST_CHAN"  # channel_id
            assert "3" in call_args[0][1]  # turns in message text
            assert "0.0456" in call_args[0][1] or "0.046" in call_args[0][1]

    async def test_shutdown_preserves_channel(self, tmp_path):
        """_shutdown should NOT archive the session channel — channels are preserved."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry
        from summon_claude.session import _SessionRuntime

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-arch", 1234, "/tmp")

            mock_permission_handler = AsyncMock()
            session = SummonSession(
                config,
                make_options(session_id="sess-arch"),
                auth=make_auth(session_id="sess-arch"),
            )

            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_channel_manager.archive_session_channel = AsyncMock()
            mock_provider = AsyncMock()

            rt = _SessionRuntime(
                registry=registry,
                provider=mock_provider,
                permission_handler=mock_permission_handler,
                channel_id="C_ARCH_CHAN",
                channel_manager=mock_channel_manager,
            )

            await session._shutdown(rt)

            # Channel should NOT be archived — it is preserved
            mock_channel_manager.archive_session_channel.assert_not_called()
            # Disconnect message should be posted instead
            mock_provider.post_message.assert_called_once()
            call_args = mock_provider.post_message.call_args
            assert call_args[0][0] == "C_ARCH_CHAN"  # channel_id
            assert "session ended" in call_args[0][1].lower() or "wave" in call_args[0][1].lower()

    async def test_shutdown_updates_registry_to_completed(self, tmp_path):
        """_shutdown should update session status to completed."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry
        from summon_claude.session import _SessionRuntime

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-comp", 1234, "/tmp")

            mock_permission_handler = AsyncMock()
            session = SummonSession(
                config,
                make_options(session_id="sess-comp"),
                auth=make_auth(session_id="sess-comp"),
            )

            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_channel_manager.archive_session_channel = AsyncMock()

            rt = _SessionRuntime(
                registry=registry,
                provider=AsyncMock(),
                permission_handler=mock_permission_handler,
                channel_id="C_COMP_CHAN",
                channel_manager=mock_channel_manager,
            )

            await session._shutdown(rt)

            sess = await registry.get_session("sess-comp")
            assert sess["status"] == "completed"


class TestSessionShutdown:
    """Test shutdown behavior including completion flag and error handling."""

    def _make_rt(self, registry, channel_id="C_CHAN", provider=None):
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.session import _SessionRuntime

        return _SessionRuntime(
            registry=registry,
            provider=provider or AsyncMock(),
            permission_handler=AsyncMock(),
            channel_id=channel_id,
            channel_manager=AsyncMock(spec=ChannelManager),
        )

    async def test_shutdown_sets_completed_flag(self, tmp_path):
        """After successful _shutdown(), _shutdown_completed should be True."""
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-flag", 1234, "/tmp")
            session = SummonSession(
                config, make_options(session_id="sess-flag"), auth=make_auth(session_id="sess-flag")
            )
            assert session._shutdown_completed is False
            rt = self._make_rt(registry, "C_FLAG_CHAN")
            await session._shutdown(rt)
            assert session._shutdown_completed is True

    async def test_shutdown_completed_flag_false_on_registry_failure(self, tmp_path):
        """If registry update raises, _shutdown_completed should remain False."""
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-fail", 1234, "/tmp")
            session = SummonSession(
                config, make_options(session_id="sess-fail"), auth=make_auth(session_id="sess-fail")
            )
            assert session._shutdown_completed is False

            async def failing_update(*args, **kwargs):
                raise RuntimeError("Registry update failed")

            registry.update_status = failing_update
            rt = self._make_rt(registry, "C_FAIL_CHAN")
            await session._shutdown(rt)
            assert session._shutdown_completed is False

    async def test_shutdown_disconnect_message_failure_continues(self, tmp_path):
        """If posting the disconnect message fails, shutdown should continue."""
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-arch-fail", 1234, "/tmp")
            session = SummonSession(
                config,
                make_options(session_id="sess-arch-fail"),
                auth=make_auth(session_id="sess-arch-fail"),
            )

            mock_provider = AsyncMock()
            mock_provider.post_message = AsyncMock(side_effect=RuntimeError("Post failed"))
            rt = self._make_rt(registry, "C_ARCH_FAIL_CHAN", provider=mock_provider)

            await session._shutdown(rt)

            sess = await registry.get_session("sess-arch-fail")
            assert sess["status"] == "completed"
            assert session._shutdown_completed is True

    async def test_shutdown_timeout_on_slack_call(self, tmp_path):
        """If Slack call hangs, asyncio.wait_for should timeout and continue."""
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-timeout", 1234, "/tmp")
            session = SummonSession(
                config,
                make_options(session_id="sess-timeout"),
                auth=make_auth(session_id="sess-timeout"),
            )

            async def hanging_post(*args, **kwargs):
                await asyncio.sleep(999)

            mock_provider = AsyncMock()
            mock_provider.post_message = AsyncMock(side_effect=hanging_post)
            rt = self._make_rt(registry, "C_TIMEOUT_CHAN", provider=mock_provider)

            await session._shutdown(rt)

            sess = await registry.get_session("sess-timeout")
            assert sess["status"] == "completed"
            assert session._shutdown_completed is True


class TestAuditEventsLogged:
    async def test_registry_logs_session_created_event(self, tmp_path):
        """Registry.log_event is used in start() — test it works for session_created."""
        from summon_claude.registry import SessionRegistry

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


class TestDisconnectMessageVariants:
    """Test the three variants of disconnect messages."""

    async def test_disconnect_message_ended(self, tmp_path):
        """Normal shutdown should post :wave: 'session ended' message."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry
        from summon_claude.session import _SessionRuntime

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-ended", 1234, "/tmp")

            mock_permission_handler = AsyncMock()
            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_provider = AsyncMock()

            session = SummonSession(
                config,
                make_options(session_id="sess-ended"),
                auth=make_auth(session_id="sess-ended"),
            )
            session._total_turns = 5
            session._total_cost = 0.125
            session._disconnect_reason = "ended"

            rt = _SessionRuntime(
                registry=registry,
                provider=mock_provider,
                permission_handler=mock_permission_handler,
                channel_id="C_ENDED",
                channel_manager=mock_channel_manager,
            )

            await session._post_disconnect_message(rt, reason="ended")

            # Should post message with :wave: emoji and "session ended"
            mock_provider.post_message.assert_called_once()
            call_args = mock_provider.post_message.call_args
            text = call_args[0][1]
            assert ":wave:" in text
            assert "session ended" in text.lower()
            assert "5" in text  # turns
            assert "0.125" in text or "0.13" in text  # cost

    async def test_disconnect_message_reconnect_exhausted(self, tmp_path):
        """Reconnect exhaustion should post :x: 'disconnected' message."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry
        from summon_claude.session import _SessionRuntime

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-exhausted", 1234, "/tmp")

            mock_permission_handler = AsyncMock()
            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_provider = AsyncMock()

            session = SummonSession(
                config,
                make_options(session_id="sess-exhausted"),
                auth=make_auth(session_id="sess-exhausted"),
            )
            session._total_turns = 3
            session._total_cost = 0.075
            session._claude_session_id = "claude-sess-123"
            session._disconnect_reason = "reconnect_exhausted"

            rt = _SessionRuntime(
                registry=registry,
                provider=mock_provider,
                permission_handler=mock_permission_handler,
                channel_id="C_EXHAUSTED",
                channel_manager=mock_channel_manager,
            )

            await session._post_disconnect_message(rt, reason="reconnect_exhausted")

            # Should post message with :x: and "disconnected"
            mock_provider.post_message.assert_called_once()
            call_args = mock_provider.post_message.call_args
            text = call_args[0][1]
            assert ":x:" in text
            assert "disconnected" in text.lower()
            assert "3" in text  # turns
            assert "claude-sess-123" in text  # session id

    async def test_disconnect_message_watchdog(self, tmp_path):
        """Watchdog termination should post :rotating_light: message."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry
        from summon_claude.session import _SessionRuntime

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-watchdog", 1234, "/tmp")

            mock_permission_handler = AsyncMock()
            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_provider = AsyncMock()

            session = SummonSession(
                config,
                make_options(session_id="sess-watchdog"),
                auth=make_auth(session_id="sess-watchdog"),
            )
            session._total_turns = 7
            session._total_cost = 0.235
            session._disconnect_reason = "watchdog"

            rt = _SessionRuntime(
                registry=registry,
                provider=mock_provider,
                permission_handler=mock_permission_handler,
                channel_id="C_WATCHDOG",
                channel_manager=mock_channel_manager,
            )

            await session._post_disconnect_message(rt, reason="watchdog")

            # Should post message with :rotating_light: and "watchdog"
            mock_provider.post_message.assert_called_once()
            call_args = mock_provider.post_message.call_args
            text = call_args[0][1]
            assert ":rotating_light:" in text
            assert "watchdog" in text.lower() or "unresponsive" in text.lower()
            assert "7" in text  # turns


class TestWatchdogLoop:
    """Test the heartbeat loop timestamp tracking."""

    async def test_heartbeat_updates_timestamp(self, tmp_path):
        """Calling heartbeat loop should update _last_heartbeat_time."""
        config = make_config()
        session = SummonSession(
            config,
            make_options(session_id="sess-hb-ts"),
            auth=make_auth(session_id="sess-hb-ts"),
        )

        loop = asyncio.get_running_loop()
        old_time = loop.time() - 50.0
        session._last_heartbeat_time = old_time

        # Build a minimal mock runtime with an async registry heartbeat
        from unittest.mock import MagicMock

        mock_rt = MagicMock()
        mock_rt.registry = AsyncMock()
        mock_rt.registry.heartbeat = AsyncMock()

        # Run one iteration of the heartbeat loop: patch the sleep interval to be very short,
        # then signal shutdown after the first iteration so the loop exits cleanly
        async def _set_shutdown_after_first_heartbeat(*_args, **_kwargs):
            # Allow the heartbeat to complete, then shut down
            session._shutdown_event.set()

        mock_rt.registry.heartbeat.side_effect = _set_shutdown_after_first_heartbeat

        with patch("summon_claude.session._HEARTBEAT_INTERVAL_S", 0.01):
            await asyncio.wait_for(session._heartbeat_loop(mock_rt), timeout=1.0)

        # Timestamp should be updated to approximately now
        elapsed = loop.time() - session._last_heartbeat_time
        assert elapsed < 1.0  # Was updated during the loop iteration


class TestSessionHandleRegistration:
    """Test SessionHandle registration with EventDispatcher."""

    async def test_channel_id_set_after_run_session(self):
        """channel_id property should reflect the assigned channel once set."""
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        assert session.channel_id is None
        # Simulate what _run_session does after creating the channel
        session._channel_id = "C_NEW_CHAN"
        assert session.channel_id == "C_NEW_CHAN"

    async def test_dispatcher_registered_when_provided(self, tmp_path):
        """When a dispatcher is provided, _run_session registers a SessionHandle with it."""
        from summon_claude.event_dispatcher import EventDispatcher, SessionHandle
        from summon_claude.registry import SessionRegistry

        config = make_config()
        dispatcher = EventDispatcher()

        # Create a mock shared provider whose _client.auth_test returns bot_user_id
        mock_client = AsyncMock()
        mock_client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
        mock_provider = AsyncMock()
        mock_provider._client = mock_client
        mock_provider.create_channel = AsyncMock(
            return_value=AsyncMock(channel_id="C_DISP", name="disp")
        )
        mock_provider.post_message = AsyncMock()
        mock_provider.post_ephemeral = AsyncMock()

        # Patch ChannelManager to avoid real Slack calls
        from unittest.mock import MagicMock, patch

        mock_channel_manager = AsyncMock()
        mock_channel_manager.create_session_channel = AsyncMock(return_value=("C_DISP", "disp"))
        mock_channel_manager.invite_user_to_channel = AsyncMock()
        mock_channel_manager.post_session_header = AsyncMock()
        mock_channel_manager.set_session_topic = AsyncMock()

        session = SummonSession(
            config,
            make_options(session_id="sess-disp"),
            auth=make_auth(session_id="sess-disp"),
            shared_provider=mock_provider,
            dispatcher=dispatcher,
        )
        session.authenticate("U001")

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-disp", 1234, "/tmp")

            with (
                patch("summon_claude.session.AsyncWebClient", return_value=mock_client),
                patch("summon_claude.session.ChannelManager", return_value=mock_channel_manager),
                patch("summon_claude.session.ThreadRouter"),
                patch("summon_claude.session.PermissionHandler"),
                patch.object(session, "_run_message_loop", new=AsyncMock()),
                patch.object(session, "_shutdown", new=AsyncMock()),
            ):
                await session._run_session(registry)

        # After _run_session, dispatcher should have session registered
        assert "C_DISP" in dispatcher._sessions


class TestProcessIncomingEvent:
    """Tests for _process_incoming_event — the message pre-processing pipeline."""

    def _make_rt(self, permission_handler=None):
        """Build a minimal mock _SessionRuntime."""
        from unittest.mock import MagicMock

        from summon_claude.channel_manager import ChannelManager
        from summon_claude.session import _SessionRuntime

        if permission_handler is None:
            mock_permission_handler = AsyncMock()
            mock_permission_handler.has_pending_text_input = MagicMock(return_value=False)
            mock_permission_handler.receive_text_input = AsyncMock()
        else:
            mock_permission_handler = permission_handler
        return _SessionRuntime(
            registry=AsyncMock(),
            provider=AsyncMock(),
            permission_handler=mock_permission_handler,
            channel_id="C_TEST",
            channel_manager=AsyncMock(spec=ChannelManager),
        )

    async def test_normal_message_returns_text_and_ts(self):
        """A normal user message returns (full_text, thread_ts)."""
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        rt = self._make_rt()

        event = {"user": "U001", "text": "Hello Claude", "ts": "123.456"}
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, ts = result
        assert text == "Hello Claude"
        assert ts == "123.456"

    async def test_subtype_message_filtered(self):
        """Messages with a subtype (bot messages etc.) are filtered out."""
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        rt = self._make_rt()

        event = {"user": "U001", "text": "Hello", "subtype": "bot_message", "ts": "1"}
        result = await session._process_incoming_event(event, rt)
        assert result is None

    async def test_empty_text_filtered(self):
        """Messages with empty text are filtered out."""
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        rt = self._make_rt()

        event = {"user": "U001", "text": "", "ts": "1"}
        result = await session._process_incoming_event(event, rt)
        assert result is None

    async def test_no_user_filtered(self):
        """Messages without a user_id (system events) are filtered out."""
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        rt = self._make_rt()

        event = {"text": "Hello", "ts": "1"}
        result = await session._process_incoming_event(event, rt)
        assert result is None

    async def test_long_message_truncated(self):
        """Messages exceeding _MAX_USER_MESSAGE_CHARS are truncated."""
        from summon_claude.session import _MAX_USER_MESSAGE_CHARS

        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
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
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
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
        from unittest.mock import MagicMock

        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())

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
        from unittest.mock import patch

        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        rt = self._make_rt()

        # Mock _dispatch_command to avoid real execution
        with patch.object(session, "_dispatch_command", new=AsyncMock()) as mock_dispatch:
            event = {"user": "U001", "text": "!status", "ts": "1"}
            result = await session._process_incoming_event(event, rt)

        assert result is None
        mock_dispatch.assert_awaited_once()

    async def test_regular_message_not_command(self):
        """A message without ! prefix is returned as-is for Claude."""
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        rt = self._make_rt()

        event = {"user": "U001", "text": "What is 2+2?", "ts": "789"}
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, ts = result
        assert text == "What is 2+2?"
        assert ts == "789"
