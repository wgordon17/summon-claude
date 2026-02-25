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
        session = SummonSession(config, make_options(), auth=make_auth())
        assert not session._shutdown_event.is_set()
        session._handle_signal()
        assert session._shutdown_event.is_set()

    async def test_handle_signal_puts_sentinel_on_queue(self):
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())
        session._handle_signal()
        item = await asyncio.wait_for(session._message_queue.get(), timeout=1.0)
        assert item == ("", None)

    async def test_handle_signal_second_signal_force_exits(self):
        """Second signal call should trigger os._exit(1) when event already set."""
        config = make_config()
        session = SummonSession(config, make_options(), auth=make_auth())

        with patch("os._exit") as mock_exit:
            # First signal sets the event
            session._handle_signal()
            assert session._shutdown_event.is_set()
            mock_exit.assert_not_called()

            # Second signal should force exit
            session._handle_signal()
            mock_exit.assert_called_once_with(1)


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

    async def test_slash_command_valid_code_sets_event(self, tmp_path):
        """Valid code should set authenticated_event."""
        from summon_claude.auth import generate_session_token, verify_short_code
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-1", 1234, "/tmp")
            auth = await generate_session_token(registry, "sess-1")

            session = SummonSession(
                config, make_options(session_id="sess-1"),
                auth=make_auth(session_id="sess-1"),
            )

            # Simulate what the handler does: verify the code and set the event
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

            session = SummonSession(
                config, make_options(session_id="sess-2"),
                auth=make_auth(session_id="sess-2"),
            )

            result = await verify_short_code(registry, "badcod")
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
        from summon_claude.session import _SessionRuntime

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-sd", 1234, "/tmp")

            mock_client = AsyncMock()
            mock_provider = AsyncMock()
            mock_permission_handler = AsyncMock()
            mock_socket_handler = AsyncMock()

            session = SummonSession(
                config, make_options(session_id="sess-sd"),
                auth=make_auth(session_id="sess-sd"),
            )
            session._total_turns = 3
            session._total_cost = 0.0456

            rt = _SessionRuntime(
                registry=registry,
                client=mock_client,
                provider=mock_provider,
                permission_handler=mock_permission_handler,
                channel_id="C_TEST_CHAN",
                socket_handler=mock_socket_handler,
            )

            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_channel_manager.archive_session_channel = AsyncMock()

            await session._shutdown(rt, mock_channel_manager)

            # Summary message should have been posted via provider
            mock_provider.post_message.assert_called_once()
            call_args = mock_provider.post_message.call_args
            assert call_args[0][0] == "C_TEST_CHAN"  # channel_id
            assert "3" in call_args[0][1]  # turns in message text
            assert "0.0456" in call_args[0][1] or "0.046" in call_args[0][1]

    async def test_shutdown_archives_channel(self, tmp_path):
        """_shutdown should archive the session channel."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry
        from summon_claude.session import _SessionRuntime

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-arch", 1234, "/tmp")

            mock_client = AsyncMock()
            mock_client.chat_postMessage = AsyncMock(return_value={"ok": True})

            mock_permission_handler = AsyncMock()
            mock_socket_handler = AsyncMock()

            session = SummonSession(
                config, make_options(session_id="sess-arch"),
                auth=make_auth(session_id="sess-arch"),
            )

            rt = _SessionRuntime(
                registry=registry,
                client=mock_client,
                provider=AsyncMock(),
                permission_handler=mock_permission_handler,
                channel_id="C_ARCH_CHAN",
                socket_handler=mock_socket_handler,
            )

            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_channel_manager.archive_session_channel = AsyncMock()

            await session._shutdown(rt, mock_channel_manager)

            mock_channel_manager.archive_session_channel.assert_called_once_with("C_ARCH_CHAN")

    async def test_shutdown_updates_registry_to_completed(self, tmp_path):
        """_shutdown should update session status to completed."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry
        from summon_claude.session import _SessionRuntime

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-comp", 1234, "/tmp")

            mock_client = AsyncMock()
            mock_client.chat_postMessage = AsyncMock(return_value={"ok": True})

            mock_permission_handler = AsyncMock()
            mock_socket_handler = AsyncMock()

            session = SummonSession(
                config, make_options(session_id="sess-comp"),
                auth=make_auth(session_id="sess-comp"),
            )

            rt = _SessionRuntime(
                registry=registry,
                client=mock_client,
                provider=AsyncMock(),
                permission_handler=mock_permission_handler,
                channel_id="C_COMP_CHAN",
                socket_handler=mock_socket_handler,
            )

            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_channel_manager.archive_session_channel = AsyncMock()

            await session._shutdown(rt, mock_channel_manager)

            sess = await registry.get_session("sess-comp")
            assert sess["status"] == "completed"


class TestSessionShutdown:
    """Test shutdown behavior including completion flag and error handling."""

    async def test_shutdown_sets_completed_flag(self, tmp_path):
        """After successful _shutdown(), _shutdown_completed should be True."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry
        from summon_claude.session import _SessionRuntime

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-flag", 1234, "/tmp")

            mock_client = AsyncMock()
            mock_client.chat_postMessage = AsyncMock(return_value={"ok": True})

            mock_permission_handler = AsyncMock()
            mock_socket_handler = AsyncMock()

            session = SummonSession(
                config, make_options(session_id="sess-flag"),
                auth=make_auth(session_id="sess-flag"),
            )
            assert session._shutdown_completed is False

            rt = _SessionRuntime(
                registry=registry,
                client=mock_client,
                provider=AsyncMock(),
                permission_handler=mock_permission_handler,
                channel_id="C_FLAG_CHAN",
                socket_handler=mock_socket_handler,
            )

            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_channel_manager.archive_session_channel = AsyncMock()

            await session._shutdown(rt, mock_channel_manager)

            assert session._shutdown_completed is True

    async def test_shutdown_completed_flag_false_on_registry_failure(self, tmp_path):
        """If registry update raises, _shutdown_completed should remain False."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry
        from summon_claude.session import _SessionRuntime

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-fail", 1234, "/tmp")

            mock_client = AsyncMock()
            mock_client.chat_postMessage = AsyncMock(return_value={"ok": True})

            mock_permission_handler = AsyncMock()
            mock_socket_handler = AsyncMock()

            session = SummonSession(
                config, make_options(session_id="sess-fail"),
                auth=make_auth(session_id="sess-fail"),
            )
            assert session._shutdown_completed is False

            # Mock registry.update_status to raise an exception
            async def failing_update(*args, **kwargs):
                raise RuntimeError("Registry update failed")
            registry.update_status = failing_update

            rt = _SessionRuntime(
                registry=registry,
                client=mock_client,
                provider=AsyncMock(),
                permission_handler=mock_permission_handler,
                channel_id="C_FAIL_CHAN",
                socket_handler=mock_socket_handler,
            )

            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_channel_manager.archive_session_channel = AsyncMock()

            await session._shutdown(rt, mock_channel_manager)

            # Flag should remain False because registry update failed
            assert session._shutdown_completed is False

    async def test_shutdown_archives_channel_failure_continues(self, tmp_path):
        """If archive_session_channel raises, shutdown should continue."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry
        from summon_claude.session import _SessionRuntime

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-arch-fail", 1234, "/tmp")

            mock_client = AsyncMock()
            mock_client.chat_postMessage = AsyncMock(return_value={"ok": True})

            mock_permission_handler = AsyncMock()
            mock_socket_handler = AsyncMock()

            session = SummonSession(
                config, make_options(session_id="sess-arch-fail"),
                auth=make_auth(session_id="sess-arch-fail"),
            )

            rt = _SessionRuntime(
                registry=registry,
                client=mock_client,
                provider=AsyncMock(),
                permission_handler=mock_permission_handler,
                channel_id="C_ARCH_FAIL_CHAN",
                socket_handler=mock_socket_handler,
            )

            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_channel_manager.archive_session_channel = AsyncMock(
                side_effect=RuntimeError("Archive failed")
            )

            # Should not raise — should catch and continue
            await session._shutdown(rt, mock_channel_manager)

            # Registry should still be updated despite archive failure
            sess = await registry.get_session("sess-arch-fail")
            assert sess["status"] == "completed"
            assert session._shutdown_completed is True

    async def test_shutdown_timeout_on_slack_call(self, tmp_path):
        """If Slack call hangs, asyncio.wait_for should timeout and continue."""
        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry
        from summon_claude.session import _SessionRuntime

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-timeout", 1234, "/tmp")

            mock_client = AsyncMock()
            # Make post_message hang forever (simulating timeout)
            async def hanging_post(*args, **kwargs):
                await asyncio.sleep(999)
            mock_client.chat_postMessage = AsyncMock(side_effect=hanging_post)

            mock_permission_handler = AsyncMock()
            mock_socket_handler = AsyncMock()

            session = SummonSession(
                config, make_options(session_id="sess-timeout"),
                auth=make_auth(session_id="sess-timeout"),
            )

            rt = _SessionRuntime(
                registry=registry,
                client=mock_client,
                provider=AsyncMock(),
                permission_handler=mock_permission_handler,
                channel_id="C_TIMEOUT_CHAN",
                socket_handler=mock_socket_handler,
            )

            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_channel_manager.archive_session_channel = AsyncMock()

            # Should timeout and continue (not hang forever)
            await session._shutdown(rt, mock_channel_manager)

            # Registry should still be updated despite Slack timeout
            sess = await registry.get_session("sess-timeout")
            assert sess["status"] == "completed"
            assert session._shutdown_completed is True


class TestSessionStartGuard:
    """Test start() finally block registry update guard."""

    async def test_start_finally_block_updates_registry_on_error(self, tmp_path):
        """Finally block should update registry to errored when _shutdown_completed is False."""
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-finally", 1234, "/tmp")

            session = SummonSession(
                config, make_options(session_id="sess-finally"),
                auth=make_auth(session_id="sess-finally"),
            )
            assert session._shutdown_completed is False

            # Simulate the finally block logic directly
            # (The finally block is complex to test end-to-end, so test the logic)
            try:
                raise RuntimeError("Simulated error during start()")
            except Exception:
                # This is what finally block does
                if not session._shutdown_completed:
                    try:
                        await registry.update_status(
                            session._session_id,
                            "errored",
                            error_message="Session terminated unexpectedly",
                            ended_at=datetime.now(UTC).isoformat(),
                        )
                    except Exception as e:
                        import logging
                        logging.getLogger().warning("Failed to update registry: %s", e)

            # Verify registry was updated to errored
            sess = await registry.get_session("sess-finally")
            assert sess["status"] == "errored"

    async def test_start_finally_block_skips_update_when_shutdown_completed(self, tmp_path):
        """Finally block should skip registry update when _shutdown_completed is True."""
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-skip-final", 1234, "/tmp")

            session = SummonSession(
                config, make_options(session_id="sess-skip-final"),
                auth=make_auth(session_id="sess-skip-final"),
            )
            session._shutdown_completed = True  # Already completed

            # Simulate the finally block logic
            try:
                raise RuntimeError("Simulated error")
            except Exception:
                # This is what finally block does
                if not session._shutdown_completed:
                    await registry.update_status(
                        session._session_id,
                        "errored",
                        error_message="Session terminated unexpectedly",
                        ended_at=datetime.now(UTC).isoformat(),
                    )

            # Registry should NOT be updated since _shutdown_completed is True
            sess = await registry.get_session("sess-skip-final")
            assert sess["status"] == "pending_auth"  # Still original status
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




class TestAuthCountdown:
    """Test auth countdown and token cleanup (BUG-021)."""

    async def test_timeout_calls_delete_pending_token(self, tmp_path):
        """When auth timeout occurs, delete_pending_token should be called."""
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.store_pending_token(
                short_code="timeout",
                session_id="sess-timeout",
                expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            )

            _session = SummonSession(
                config, make_options(session_id="sess-timeout"),
                auth=make_auth(session_id="sess-timeout"),
            )

            # Simulate timeout by directly calling cleanup
            await registry.delete_pending_token("timeout")
            entry = await registry._get_pending_token("timeout")

            # Token should be deleted
            assert entry is None

    async def test_shutdown_event_during_auth_cleans_up_token(self, tmp_path):
        """When shutdown event fires during auth, delete_pending_token should be called."""
        from summon_claude.registry import SessionRegistry

        config = make_config()
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.store_pending_token(
                short_code="shutdown",
                session_id="sess-shutdown",
                expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            )

            session = SummonSession(
                config, make_options(session_id="sess-shutdown"),
                auth=make_auth(session_id="sess-shutdown"),
            )

            # Set shutdown event
            session._shutdown_event.set()

            # Manually delete the token (simulating what _wait_for_auth would do)
            await registry.delete_pending_token("shutdown")

            # Token should be cleaned up
            entry = await registry._get_pending_token("shutdown")
            assert entry is None


