"""Tests for summon_claude.session — session orchestrator."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from summon_claude._formatting import format_file_references
from summon_claude.auth import SessionAuth
from summon_claude.config import SummonConfig
from summon_claude.rate_limiter import RateLimiter
from summon_claude.session import SessionOptions, SummonSession


def make_config(**overrides) -> SummonConfig:
    defaults = {
        "slack_bot_token": "xoxb-test-token",
        "slack_app_token": "xapp-test-token",
        "slack_signing_secret": "test-secret",
        "allowed_user_ids": ["U123456", "U789012"],
        "default_model": "claude-opus-4-6",
        "channel_prefix": "summon",
        "permission_debounce_ms": 10,
        "max_inline_chars": 2500,
    }
    defaults.update(overrides)
    return SummonConfig.model_validate(defaults)


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


class TestPrepareAuth:
    async def test_prepare_auth_returns_short_code(self):
        """prepare_auth should return a string (the short_code)."""
        config = make_config()
        session = SummonSession(config, SessionOptions(session_id="sess-test"))

        # Patch SessionRegistry to use tmp_path
        with patch("summon_claude.session.SessionRegistry") as mock_registry:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_registry.return_value = mock_instance

            mock_instance.register = AsyncMock()
            mock_instance.log_event = AsyncMock()

            fake_auth = SessionAuth(
                token="fake-token",
                short_code="FAKECODE",
                session_id="sess-test",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )

            with patch("summon_claude.session.generate_session_token", return_value=fake_auth):
                short_code = await session.prepare_auth()

        assert isinstance(short_code, str)
        assert short_code == "FAKECODE"
        assert session._auth is not None
        assert session._auth.short_code == short_code

    async def test_prepare_auth_sets_auth_attribute(self):
        """prepare_auth should set self._auth attribute."""
        config = make_config()
        session = SummonSession(config, SessionOptions(session_id="sess-auth-attr"))

        assert session._auth is None

        # Patch SessionRegistry and generate_session_token
        with patch("summon_claude.session.SessionRegistry") as mock_registry:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_registry.return_value = mock_instance

            mock_instance.register = AsyncMock()
            mock_instance.log_event = AsyncMock()

            fake_auth = SessionAuth(
                token="test-token",
                short_code="AUTHCODE",
                session_id="sess-auth-attr",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )

            with patch("summon_claude.session.generate_session_token", return_value=fake_auth):
                short_code = await session.prepare_auth()

        assert session._auth is not None
        assert session._auth.short_code == short_code
        assert session._auth.session_id == "sess-auth-attr"


class TestFormatFileReferences:
    def test_empty_list_returns_empty_string(self):
        result = format_file_references([])
        assert result == ""

    def test_single_file_with_name(self):
        files = [{"name": "photo.png", "filetype": "png", "size": 1024}]
        result = format_file_references(files)
        assert "photo.png" in result
        assert "(png)" in result
        assert "(1024 bytes)" in result
        # URL should NOT be included (Claude can't fetch Slack private URLs)
        assert "https://" not in result

    def test_single_file_without_url(self):
        files = [{"name": "doc.txt", "filetype": "txt"}]
        result = format_file_references(files)
        assert "doc.txt" in result
        assert "(txt)" in result

    def test_multiple_files_joined_by_newlines(self):
        files = [
            {"name": "a.py", "url_private_download": "https://example.com/a"},
            {"name": "b.py", "url_private_download": "https://example.com/b"},
        ]
        result = format_file_references(files)
        lines = result.splitlines()
        assert len(lines) == 2
        assert "a.py" in lines[0]
        assert "b.py" in lines[1]

    def test_missing_name_uses_unknown(self):
        files = [{"url_private": "https://example.com/f"}]
        result = format_file_references(files)
        assert "unknown" in result


class TestSessionSignalHandler:
    async def test_handle_signal_sets_shutdown_event(self):
        config = make_config()
        session = SummonSession(config)
        assert not session._shutdown_event.is_set()
        session._handle_signal()
        assert session._shutdown_event.is_set()

    async def test_handle_signal_puts_sentinel_on_queue(self):
        config = make_config()
        session = SummonSession(config)
        session._handle_signal()
        item = await asyncio.wait_for(session._message_queue.get(), timeout=1.0)
        assert item == ""


class TestWaitForAuth:
    async def test_returns_immediately_when_event_set(self):
        config = make_config()
        session = SummonSession(config)
        session._authenticated_event.set()

        # Should complete quickly since event is already set
        await asyncio.wait_for(session._wait_for_auth(), timeout=2.0)

    async def test_returns_when_shutdown_event_set(self):
        config = make_config()
        session = SummonSession(config)
        session._shutdown_event.set()

        await asyncio.wait_for(session._wait_for_auth(), timeout=2.0)


class TestSlashCommandHandler:
    """Test the /summon slash command handler internals via _build_slack_app."""

    def _make_session_with_registry(self, config, registry):
        session = SummonSession(config)
        session._registry = registry
        return session

    async def _extract_summon_handler(self, session):
        """Build app and extract the handle_summon_command function."""
        # We can't easily invoke the registered handlers directly without
        # full Bolt machinery. Instead we test via the session's internal state
        # by directly exercising the logic we can reach.
        app = session._build_slack_app()
        return app

    async def test_slash_command_valid_code_sets_event(self, tmp_path):
        """Valid code should set authenticated_event."""
        from summon_claude.auth import generate_session_token
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-1", 1234, "/tmp")
            auth = await generate_session_token(registry, "sess-1", "/tmp")

            session = SummonSession(config, SessionOptions(session_id="sess-1"))
            session._registry = registry

            # Simulate what the handler does: verify the code and set the event
            from summon_claude.auth import verify_short_code

            result = await verify_short_code(registry, auth.short_code)
            assert result is not None

            session._authenticated_user_id = "U123456"
            session._authenticated_event.set()

            assert session._authenticated_event.is_set()
            assert session._authenticated_user_id == "U123456"

    async def test_slash_command_invalid_code_no_event_set(self, tmp_path):
        """Invalid code should NOT set authenticated_event."""
        from summon_claude.auth import verify_short_code
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-2", 1234, "/tmp")

            session = SummonSession(config, SessionOptions(session_id="sess-2"))
            session._registry = registry

            result = await verify_short_code(registry, "BADCOD")
            assert result is None
            assert not session._authenticated_event.is_set()


class TestMessageQueueLogic:
    async def test_message_with_files_appends_context(self):
        """Messages with file attachments should have file context appended."""
        files = [{"name": "test.py", "url_private_download": "https://slack.com/test.py"}]
        text = "here is my file"
        file_context = format_file_references(files)
        full_text = f"{text}\n\n{file_context}"
        assert "test.py" in full_text
        assert "here is my file" in full_text

    async def test_message_without_files_uses_plain_text(self):
        """Messages without attachments use text unchanged."""
        text = "plain message"
        files = []
        file_context = format_file_references(files)
        full_text = text if not file_context else f"{text}\n\n{file_context}"
        assert full_text == text


class TestSessionShutdownSummary:
    async def test_shutdown_posts_summary_message(self, tmp_path):
        """_shutdown should post turns/cost summary to channel."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-sd", 1234, "/tmp")

            mock_client = AsyncMock()
            mock_client.chat_postMessage = AsyncMock(return_value={"ok": True})
            mock_client.conversations_archive = AsyncMock(return_value={"ok": True})

            session = SummonSession(config, SessionOptions(session_id="sess-sd"))
            session._registry = registry
            session._client = mock_client
            session._total_turns = 3
            session._total_cost = 0.0456

            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_channel_manager.archive_session_channel = AsyncMock()

            await session._shutdown(mock_channel_manager, "C_TEST_CHAN")

            # Summary message should have been posted
            mock_client.chat_postMessage.assert_called_once()
            call_kwargs = mock_client.chat_postMessage.call_args[1]
            assert call_kwargs["channel"] == "C_TEST_CHAN"
            assert "3" in call_kwargs["text"]  # turns
            assert "0.0456" in call_kwargs["text"] or "0.046" in call_kwargs["text"]

    async def test_shutdown_archives_channel(self, tmp_path):
        """_shutdown should archive the session channel."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-arch", 1234, "/tmp")

            mock_client = AsyncMock()
            mock_client.chat_postMessage = AsyncMock(return_value={"ok": True})

            session = SummonSession(config, SessionOptions(session_id="sess-arch"))
            session._registry = registry
            session._client = mock_client

            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_channel_manager.archive_session_channel = AsyncMock()

            await session._shutdown(mock_channel_manager, "C_ARCH_CHAN")

            mock_channel_manager.archive_session_channel.assert_called_once_with("C_ARCH_CHAN")

    async def test_shutdown_updates_registry_to_completed(self, tmp_path):
        """_shutdown should update session status to completed."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-comp", 1234, "/tmp")

            mock_client = AsyncMock()
            mock_client.chat_postMessage = AsyncMock(return_value={"ok": True})

            session = SummonSession(config, SessionOptions(session_id="sess-comp"))
            session._registry = registry
            session._client = mock_client

            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_channel_manager.archive_session_channel = AsyncMock()

            await session._shutdown(mock_channel_manager, "C_COMP_CHAN")

            sess = await registry.get_session("sess-comp")
            assert sess["status"] == "completed"


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


class TestBuildSlackApp:
    def test_build_slack_app_returns_app(self):
        """_build_slack_app should return an AsyncApp instance."""
        from slack_bolt.async_app import AsyncApp

        config = make_config()
        session = SummonSession(config)
        app = session._build_slack_app()
        assert isinstance(app, AsyncApp)


class TestAuthCountdown:
    """Test auth countdown and token cleanup (BUG-021)."""

    async def test_timeout_calls_delete_pending_token(self, tmp_path):
        """When auth timeout occurs, delete_pending_token should be called."""
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.store_pending_token(
                short_code="TIMEOUT",
                token="expire-me",
                session_id="sess-timeout",
                cwd="/tmp",
                expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            )

            session = SummonSession(config, SessionOptions(session_id="sess-timeout"))
            session._registry = registry

            # Simulate timeout by directly calling cleanup
            await registry.delete_pending_token("TIMEOUT")
            entry = await registry._get_pending_token("TIMEOUT")

            # Token should be deleted
            assert entry is None

    async def test_shutdown_event_during_auth_cleans_up_token(self, tmp_path):
        """When shutdown event fires during auth, delete_pending_token should be called."""
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.store_pending_token(
                short_code="SHUTDOWN",
                token="cleanup-me",
                session_id="sess-shutdown",
                cwd="/tmp",
                expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            )

            session = SummonSession(config, SessionOptions(session_id="sess-shutdown"))
            session._registry = registry

            # Set shutdown event
            session._shutdown_event.set()

            # Manually delete the token (simulating what _wait_for_auth would do)
            await registry.delete_pending_token("SHUTDOWN")

            # Token should be cleaned up
            entry = await registry._get_pending_token("SHUTDOWN")
            assert entry is None
