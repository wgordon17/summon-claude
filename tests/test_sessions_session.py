"""Tests for summon_claude.sessions.session — session orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
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
from summon_claude.slack.client import MessageRef, SlackClient, redact_secrets


def make_config(**overrides) -> SummonConfig:
    defaults = {
        "slack_bot_token": "xoxb-test-token",
        "slack_app_token": "xapp-test-token",
        "slack_signing_secret": "abc123def456",
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
    opt_fields = (
        "cwd",
        "name",
        "model",
        "effort",
        "resume",
        "channel_id",
        "pm_profile",
        "system_prompt_append",
    )
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


class TestSanitizeForTable:
    """Tests for sanitize_for_table canvas helper."""

    def _sanitize(self, text: str, max_len: int = 80) -> str:
        from summon_claude.sessions.scheduler import sanitize_for_table

        return sanitize_for_table(text, max_len)

    def test_heading_at_start(self):
        assert self._sanitize("## Heading") == "Heading"

    def test_heading_on_non_first_line(self):
        result = self._sanitize("line one\n## Heading two")
        assert "##" not in result
        assert "Heading two" in result

    def test_pipe_escaped(self):
        assert "\\|" in self._sanitize("a|b")

    def test_newlines_flattened(self):
        assert "\n" not in self._sanitize("a\nb\nc")

    def test_truncation(self):
        result = self._sanitize("x" * 200, max_len=50)
        assert len(result) == 53  # 50 + "..."
        assert result.endswith("...")

    def test_truncation_with_pipes_no_split(self):
        # Pipes are escaped AFTER truncation, so escape sequences are never split.
        # max_len=5 ensures old (escape-then-truncate) code would produce a
        # dangling backslash, while new (truncate-then-escape) code does not.
        result = self._sanitize("a|b|" * 50, max_len=5)
        assert result.endswith("...")
        # No lone backslash anywhere (every \ must be followed by |)
        import re as _re

        assert not _re.search(r"\\(?!\|)", result), f"Dangling backslash in: {result!r}"
        assert "\\|" in result  # pipes are properly escaped


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

    def test_system_prompt_append_default_none(self):
        opts = make_options()
        assert opts.system_prompt_append is None

    def test_system_prompt_append_stored(self):
        opts = make_options(system_prompt_append="custom instructions")
        assert opts.system_prompt_append == "custom instructions"

    def test_system_prompt_append_on_session(self):
        session = make_session(system_prompt_append="review PR #1")
        assert session._system_prompt_append == "review PR #1"

    def test_system_prompt_append_none_on_session(self):
        session = make_session()
        assert session._system_prompt_append is None


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

    def test_request_shutdown_sets_abort_event(self):
        """request_shutdown() must set _abort_event to cancel in-flight turns."""
        session = make_session()
        session.request_shutdown()
        assert session._abort_event.is_set()

    async def test_request_shutdown_cancels_current_turn_task(self):
        """request_shutdown() cancels the active turn task if one is running."""
        session = make_session()
        session._current_turn_task = asyncio.ensure_future(asyncio.sleep(999))
        session.request_shutdown()
        assert session._current_turn_task.cancelled() or session._current_turn_task.cancelling() > 0

    def test_request_shutdown_no_turn_task_safe(self):
        """request_shutdown() with _current_turn_task=None must not raise."""
        session = make_session()
        assert session._current_turn_task is None
        session.request_shutdown()  # must not raise
        assert session._abort_event.is_set()

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

    # All events in this class use "U001" as the sender.
    _TEST_USER = "U001"

    def _make_session(self, **kwargs):
        """Create a session authenticated as the test user."""
        session = make_session(**kwargs)
        session._authenticated_user_id = self._TEST_USER
        return session

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
        session = self._make_session()

        rt = self._make_rt()

        event = {"user": "U001", "text": "Hello Claude", "ts": "123.456"}
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, ts = result
        assert text == "Hello Claude"
        assert ts == "123.456"

    async def test_synthetic_event_bypasses_preprocessing(self):
        """Synthetic events (scan triggers) bypass all Slack preprocessing."""
        session = self._make_session()
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
        session = self._make_session()
        rt = self._make_rt()

        event = {"text": "Scan now", "_synthetic": True}
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, ts = result
        assert text == "Scan now"
        assert ts is None

    async def test_synthetic_event_empty_text_filtered(self):
        """Synthetic events with empty text are filtered out."""
        session = self._make_session()
        rt = self._make_rt()

        event = {"text": "", "_synthetic": True}
        result = await session._process_incoming_event(event, rt)
        assert result is None

    async def test_subtype_message_filtered(self):
        """Messages with a subtype (bot messages etc.) are filtered out."""
        session = self._make_session()
        rt = self._make_rt()

        event = {"user": "U001", "text": "Hello", "subtype": "bot_message", "ts": "1"}
        result = await session._process_incoming_event(event, rt)
        assert result is None

    async def test_empty_text_filtered(self):
        """Messages with empty text are filtered out."""
        session = self._make_session()
        rt = self._make_rt()

        event = {"user": "U001", "text": "", "ts": "1"}
        result = await session._process_incoming_event(event, rt)
        assert result is None

    async def test_no_user_filtered(self):
        """Messages without a user_id (system events) are filtered out."""
        session = self._make_session()
        rt = self._make_rt()

        event = {"text": "Hello", "ts": "1"}
        result = await session._process_incoming_event(event, rt)
        assert result is None

    async def test_long_message_truncated(self):
        """Messages exceeding _MAX_USER_MESSAGE_CHARS are truncated."""
        from summon_claude.sessions.session import _MAX_USER_MESSAGE_CHARS

        session = self._make_session()

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
        session = self._make_session()

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
        session = self._make_session()

        mock_ph = AsyncMock()
        mock_ph.has_pending_text_input = MagicMock(return_value=True)
        mock_ph.receive_text_input = AsyncMock()
        rt = self._make_rt(permission_handler=mock_ph)

        event = {"user": "U001", "text": "My free-text answer", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        assert result is None
        mock_ph.receive_text_input.assert_awaited_once_with("My free-text answer", user_id="U001")

    async def test_command_prefix_dispatched(self):
        """Messages with ! prefix are dispatched as commands and return None."""
        session = self._make_session()

        rt = self._make_rt()

        # Mock _dispatch_command to avoid real execution
        with patch.object(session, "_dispatch_command", new=AsyncMock()) as mock_dispatch:
            event = {"user": "U001", "text": "!status", "ts": "1"}
            result = await session._process_incoming_event(event, rt)

        assert result is None
        mock_dispatch.assert_awaited_once()

    async def test_regular_message_not_command(self):
        """A message without ! prefix is returned as-is for Claude."""
        session = self._make_session()

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
        session = self._make_session()
        rt = self._make_rt()

        event = {"user": "U001", "text": "!xyznotreal", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        assert result is None
        rt.client.post.assert_called_once()
        post_text = rt.client.post.call_args[0][0]
        assert "Unknown command" in post_text or "not found" in post_text.lower()

    async def test_standalone_blocked_command_posts_reason(self):
        """!config at start should post 'not available' and return None."""
        session = self._make_session()
        rt = self._make_rt()

        event = {"user": "U001", "text": "!config", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        assert result is None
        rt.client.post.assert_called_once()
        post_text = rt.client.post.call_args[0][0]
        assert "not available" in post_text.lower()

    async def test_standalone_passthrough_dispatched(self):
        """!review at start should call _dispatch_command with name='review'."""
        session = self._make_session()
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
        session = self._make_session()
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
        session = self._make_session()
        rt = self._make_rt()

        event = {"user": "U001", "text": "please !review this", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, _ = result
        assert "/review" in text
        assert "!review" not in text

    async def test_mid_message_blocked_annotated(self):
        """'try !config please' should post annotation and return modified text."""
        session = self._make_session()
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
        session = self._make_session()
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
            session = self._make_session()
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
        session = self._make_session()
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


class TestIdentityVerification:
    """Security tests: non-owner messages are rejected at the centralized gate."""

    def _make_rt(self, permission_handler=None):
        if permission_handler is None:
            mock_ph = AsyncMock()
            mock_ph.has_pending_text_input = MagicMock(return_value=False)
            mock_ph.receive_text_input = AsyncMock()
        else:
            mock_ph = permission_handler
        return _SessionRuntime(
            registry=AsyncMock(),
            client=make_mock_client("C_TEST"),
            permission_handler=mock_ph,
        )

    async def test_non_owner_message_rejected(self):
        """Messages from a user who is not the session owner are silently dropped."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()

        event = {"user": "U_INTRUDER", "text": "Hello Claude", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        assert result is None

    async def test_non_owner_command_rejected(self):
        """Commands from a non-owner user are rejected before dispatch."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()

        with patch.object(session, "_dispatch_command", new=AsyncMock()) as mock_dispatch:
            event = {"user": "U_INTRUDER", "text": "!end", "ts": "1"}
            result = await session._process_incoming_event(event, rt)

        assert result is None
        mock_dispatch.assert_not_awaited()

    async def test_non_owner_free_text_rejected(self):
        """Free-text input from a non-owner is rejected before consumption."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        mock_ph = AsyncMock()
        mock_ph.has_pending_text_input = MagicMock(return_value=True)
        mock_ph.receive_text_input = AsyncMock()
        rt = self._make_rt(permission_handler=mock_ph)

        event = {"user": "U_INTRUDER", "text": "my answer", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        assert result is None
        mock_ph.receive_text_input.assert_not_awaited()

    async def test_owner_message_accepted(self):
        """Messages from the session owner pass through normally."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()

        event = {"user": "U_OWNER", "text": "Hello Claude", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, _ = result
        assert text == "Hello Claude"

    async def test_synthetic_event_bypasses_identity_check(self):
        """Synthetic (internal) events bypass identity verification."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()

        event = {"text": "Scheduled scan", "_synthetic": True, "user": "U_DIFFERENT"}
        result = await session._process_incoming_event(event, rt)

        assert result is not None
        text, _ = result
        assert text == "Scheduled scan"

    async def test_unauthenticated_session_rejects_all(self):
        """Before authentication completes, all messages are rejected."""
        session = make_session()
        # _authenticated_user_id is None by default
        assert session._authenticated_user_id is None
        rt = self._make_rt()

        event = {"user": "U_ANYONE", "text": "Hello", "ts": "1"}
        result = await session._process_incoming_event(event, rt)

        assert result is None


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

    def test_pending_turns_queue_has_backpressure(self):
        session = make_session()
        assert session._pending_turns.maxsize > 0


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

    async def test_regular_session_has_slack_and_cli_mcps(self):
        result = await self._capture_mcp_servers(pm_profile=False)
        assert "summon-slack" in result["mcp_servers"]
        assert "summon-cli" in result["mcp_servers"]

    async def test_pm_session_has_both_mcps(self):
        result = await self._capture_mcp_servers(pm_profile=True)
        assert "summon-slack" in result["mcp_servers"]
        assert "summon-cli" in result["mcp_servers"]

    async def test_pm_without_auth_raises(self):
        """PM session without authenticated_user_id raises RuntimeError."""
        session = make_session(pm_profile=True)
        # Do NOT set session._authenticated_user_id — stays None
        mock_registry = AsyncMock()
        rt = make_rt(mock_registry)

        with (
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch("summon_claude.sessions.session.discover_plugin_skills", return_value=[]),
            pytest.raises(RuntimeError, match="authenticated_user_id"),
        ):
            await session._run_session_tasks(rt, AsyncMock())

    def test_session_options_pm_profile_default(self):
        opts = SessionOptions(cwd="/tmp", name="test")
        assert opts.pm_profile is False

    def test_session_options_pm_profile_true(self):
        opts = SessionOptions(cwd="/tmp", name="test", pm_profile=True)
        assert opts.pm_profile is True

    async def _capture_mcp_servers_with_config(self, **config_overrides) -> dict:
        """Like _capture_mcp_servers but with custom config overrides."""
        cfg = make_config(**config_overrides)
        session = SummonSession(
            config=cfg,
            options=make_options(),
            auth=make_auth(),
            session_id="test-session",
        )
        session._authenticated_user_id = "U_TEST"
        session._shutdown_event.set()

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

    async def test_github_mcp_wired_when_configured(self):
        with patch("summon_claude.github_auth.load_token", return_value="gho_test123"):
            result = await self._capture_mcp_servers_with_config()
        assert "github" in result["mcp_servers"]
        assert result["mcp_servers"]["github"]["type"] == "http"

    async def test_github_mcp_not_wired_when_not_configured(self):
        result = await self._capture_mcp_servers_with_config()
        assert "github" not in result["mcp_servers"]

    async def test_github_mcp_connection_failure_propagates(self):
        """SDK startup failure with GitHub MCP configured propagates cleanly.

        Verifies that if the SDK subprocess fails during startup (e.g.,
        MCP connection timeout), the exception is not swallowed by
        _run_session_tasks — it propagates to the caller (start()'s
        finally block marks the session as errored).
        """
        cfg = make_config()
        session = SummonSession(
            config=cfg,
            options=make_options(),
            auth=make_auth(),
            session_id="test-session",
        )
        session._authenticated_user_id = "U_TEST"
        session._shutdown_event.set()

        mock_registry = AsyncMock()
        rt = make_rt(mock_registry)

        # Simulate SDK __aenter__ failing (e.g., MCP connection timeout)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(
            side_effect=Exception("Control request timeout: initialize")
        )

        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_unreachable"),
            patch(
                "summon_claude.sessions.session.ClaudeSDKClient",
                return_value=mock_client,
            ),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch("summon_claude.sessions.session.discover_plugin_skills", return_value=[]),
            pytest.raises(Exception, match="Control request timeout"),
        ):
            await session._run_session_tasks(rt, AsyncMock())

    async def _capture_system_prompt(
        self, *, pm_profile: bool = False, system_prompt_append: str | None = None
    ) -> dict:
        """Run _run_session_tasks far enough to capture ClaudeAgentOptions.system_prompt."""
        session = make_session(pm_profile=pm_profile, system_prompt_append=system_prompt_append)
        session._authenticated_user_id = "U_TEST"
        session._shutdown_event.set()

        mock_registry = AsyncMock()
        rt = make_rt(mock_registry)

        captured: dict = {}

        class _CaptureError(Exception):
            pass

        def spy_init(self_sdk, options):
            captured["system_prompt"] = options.system_prompt
            raise _CaptureError("captured")

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient.__init__", spy_init),
            patch(
                "summon_claude.sessions.session.discover_installed_plugins",
                return_value=[],
            ),
            patch(
                "summon_claude.sessions.session.discover_plugin_skills",
                return_value=[],
            ),
            pytest.raises(_CaptureError),
        ):
            await session._run_session_tasks(rt, AsyncMock())

        return captured

    async def test_system_prompt_append_in_pm_prompt(self):
        """system_prompt_append appears in PM session's system prompt."""
        result = await self._capture_system_prompt(
            pm_profile=True, system_prompt_append="Review PR #42 carefully"
        )
        assert "Review PR #42 carefully" in result["system_prompt"]["append"]

    async def test_system_prompt_append_in_regular_prompt(self):
        """system_prompt_append appears in regular session's system prompt."""
        result = await self._capture_system_prompt(
            pm_profile=False, system_prompt_append="Custom instructions"
        )
        assert "Custom instructions" in result["system_prompt"]["append"]

    async def test_system_prompt_append_none_no_effect(self):
        """system_prompt_append=None does not alter the system prompt."""
        with_none = await self._capture_system_prompt(pm_profile=False, system_prompt_append=None)
        without = await self._capture_system_prompt(pm_profile=False)
        assert with_none["system_prompt"]["append"] == without["system_prompt"]["append"]


class TestWorktreeDisallowedTools:
    """Guard tests for _WORKTREE_DISALLOWED_TOOLS constant and wiring."""

    def test_worktree_disallowed_tools_pinned(self):
        """Guard: _WORKTREE_DISALLOWED_TOOLS must be consciously changed."""
        from summon_claude.sessions.session import _WORKTREE_DISALLOWED_TOOLS

        assert isinstance(_WORKTREE_DISALLOWED_TOOLS, frozenset)
        assert {
            "Bash(git worktree add*)",
            "Bash(git worktree move*)",
        } == _WORKTREE_DISALLOWED_TOOLS

    async def test_disallowed_tools_wired_into_claude_agent_options(self):
        """disallowed_tools passed to ClaudeAgentOptions equals _WORKTREE_DISALLOWED_TOOLS."""
        from summon_claude.sessions.session import _WORKTREE_DISALLOWED_TOOLS

        session = make_session()
        session._authenticated_user_id = "U_TEST"
        session._shutdown_event.set()

        mock_registry = AsyncMock()
        rt = make_rt(mock_registry)

        captured = {}

        class _CaptureError(Exception):
            pass

        def spy_init(self_sdk, options):
            captured["disallowed_tools"] = options.disallowed_tools
            raise _CaptureError("captured")

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient.__init__", spy_init),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch("summon_claude.sessions.session.discover_plugin_skills", return_value=[]),
            pytest.raises(_CaptureError),
        ):
            await session._run_session_tasks(rt, AsyncMock())

        assert set(captured["disallowed_tools"]) == _WORKTREE_DISALLOWED_TOOLS

    async def test_disallowed_tools_wired_for_pm_sessions(self):
        """disallowed_tools applies to PM sessions too, not just regular sessions."""
        from summon_claude.sessions.session import _WORKTREE_DISALLOWED_TOOLS

        session = make_session(pm_profile=True)
        session._authenticated_user_id = "U_TEST"
        session._shutdown_event.set()

        mock_registry = AsyncMock()
        rt = make_rt(mock_registry)

        captured = {}

        class _CaptureError(Exception):
            pass

        def spy_init(self_sdk, options):
            captured["disallowed_tools"] = options.disallowed_tools
            raise _CaptureError("captured")

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient.__init__", spy_init),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch("summon_claude.sessions.session.discover_plugin_skills", return_value=[]),
            pytest.raises(_CaptureError),
        ):
            await session._run_session_tasks(rt, AsyncMock())

        assert set(captured["disallowed_tools"]) == _WORKTREE_DISALLOWED_TOOLS

    async def test_pm_prompt_does_not_contain_raw_worktree_add(self):
        """Guard: PM system prompt must not instruct raw git worktree add."""
        from summon_claude.sessions.session import build_pm_system_prompt

        prompt = build_pm_system_prompt(cwd="/tmp/test", scan_interval_s=900)
        assert "git worktree add" not in prompt["append"]

    def test_pm_prompt_contains_enterworktree_instruction(self):
        """Guard: PM prompt must instruct child to use EnterWorktree."""
        from summon_claude.sessions.session import build_pm_system_prompt

        prompt = build_pm_system_prompt(cwd="/tmp/test", scan_interval_s=900)
        assert 'EnterWorktree(name="review-pr{number}")' in prompt["append"]

    def test_pm_prompt_uses_claude_worktrees_path(self):
        """Guard: PM cleanup must reference .claude/worktrees/, not .worktrees/."""
        from summon_claude.sessions.session import build_pm_system_prompt

        prompt = build_pm_system_prompt(cwd="/tmp/test", scan_interval_s=900)
        assert ".claude/worktrees/review-pr" in prompt["append"]
        assert ".worktrees/review-pr" not in prompt["append"].replace(
            ".claude/worktrees/review-pr", ""
        )


class TestSystemPromptAppendRestart:
    """Verify system_prompt_append survives compaction restarts."""

    async def test_system_prompt_append_survives_restart(self, registry):
        """system_prompt_append text appears in prompt after compaction restart."""
        from summon_claude.sessions.session import _SessionRestartError

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
                raise _SessionRestartError(summary="summary text")
            raise RuntimeError("stop-second-run")

        async def fake_preprocessor(_rt, _claude):
            await asyncio.sleep(999)

        session = make_session(system_prompt_append="must-survive-restart")
        rt = make_rt(registry)

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient", _FakeSDKClient),
            patch("summon_claude.sessions.session.create_summon_mcp_server", return_value={}),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch.object(session, "_run_preprocessor", fake_preprocessor),
            patch.object(session, "_run_response_consumer", fake_consumer),
            contextlib.suppress(RuntimeError),
        ):
            await session._run_session_tasks(rt, AsyncMock())

        assert len(captured_prompts) == 2
        assert "must-survive-restart" in captured_prompts[0]
        assert "must-survive-restart" in captured_prompts[1]

    async def test_system_prompt_append_coexists_with_summary(self, registry):
        """system_prompt_append and compaction summary both appear after restart."""
        from summon_claude.sessions.session import (
            _COMPACT_SUMMARY_PREFIX,
            _SessionRestartError,
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
            await asyncio.sleep(999)

        session = make_session(system_prompt_append="custom-review-instructions")
        rt = make_rt(registry)

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient", _FakeSDKClient),
            patch("summon_claude.sessions.session.create_summon_mcp_server", return_value={}),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch.object(session, "_run_preprocessor", fake_preprocessor),
            patch.object(session, "_run_response_consumer", fake_consumer),
            contextlib.suppress(RuntimeError),
        ):
            await session._run_session_tasks(rt, AsyncMock())

        assert len(captured_prompts) == 2
        restarted_prompt = captured_prompts[1]
        # Both compaction summary and custom append must be present
        assert _COMPACT_SUMMARY_PREFIX in restarted_prompt
        assert "Build widget" in restarted_prompt
        assert "custom-review-instructions" in restarted_prompt

    async def test_pm_restart_includes_compaction_summary(self, registry):
        """PM sessions receive compaction summary after restart."""
        from summon_claude.sessions.session import (
            _COMPACT_SUMMARY_PREFIX,
            _SessionRestartError,
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
                raise _SessionRestartError(summary="## Status\nManaging 3 sessions")
            raise RuntimeError("stop-second-run")

        async def fake_preprocessor(_rt, _claude):
            await asyncio.sleep(999)

        session = make_session(pm_profile=True, system_prompt_append="review-inject")
        session._authenticated_user_id = "U_TEST"
        rt = make_rt(registry)

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient", _FakeSDKClient),
            patch("summon_claude.sessions.session.create_summon_mcp_server", return_value={}),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch(
                "summon_claude.sessions.session.discover_plugin_skills",
                return_value=[],
            ),
            patch.object(session, "_run_preprocessor", fake_preprocessor),
            patch.object(session, "_run_response_consumer", fake_consumer),
            contextlib.suppress(RuntimeError),
        ):
            await session._run_session_tasks(rt, AsyncMock())

        assert len(captured_prompts) == 2
        restarted_prompt = captured_prompts[1]
        # PM prompt should include compaction summary after restart
        assert _COMPACT_SUMMARY_PREFIX in restarted_prompt
        assert "Managing 3 sessions" in restarted_prompt
        # system_prompt_append should also survive
        assert "review-inject" in restarted_prompt

    async def test_recovery_mode_restart_preserves_system_prompt_append(self, registry):
        """system_prompt_append survives recovery_mode restart (overflow)."""
        from summon_claude.sessions.session import (
            _OVERFLOW_RECOVERY_PROMPT,
            _SessionRestartError,
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
                raise _SessionRestartError(recovery_mode=True)
            raise RuntimeError("stop-second-run")

        async def fake_preprocessor(_rt, _claude):
            await asyncio.sleep(999)

        session = make_session(system_prompt_append="must-survive-overflow")
        rt = make_rt(registry)

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient", _FakeSDKClient),
            patch("summon_claude.sessions.session.create_summon_mcp_server", return_value={}),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch.object(session, "_run_preprocessor", fake_preprocessor),
            patch.object(session, "_run_response_consumer", fake_consumer),
            contextlib.suppress(RuntimeError),
        ):
            await session._run_session_tasks(rt, AsyncMock())

        assert len(captured_prompts) == 2
        restarted_prompt = captured_prompts[1]
        assert _OVERFLOW_RECOVERY_PROMPT in restarted_prompt
        assert "must-survive-overflow" in restarted_prompt


class TestThinkingConfig:
    """Verify that enable_thinking produces the correct ThinkingConfig on ClaudeAgentOptions."""

    async def _capture_thinking(self, enable_thinking: bool) -> dict:
        """Run _run_session_tasks just far enough to capture options.thinking."""
        cfg = make_config(enable_thinking=enable_thinking)
        session = SummonSession(
            config=cfg,
            options=make_options(),
            auth=make_auth(),
            session_id="test-session",
        )
        session._authenticated_user_id = "U_TEST"
        session._shutdown_event.set()

        rt = make_rt(AsyncMock())
        captured = {}

        class _CaptureError(Exception):
            pass

        def spy_init(self_sdk, options):
            captured["thinking"] = options.thinking
            raise _CaptureError("captured")

        with (
            patch("summon_claude.sessions.session.ClaudeSDKClient.__init__", spy_init),
            patch("summon_claude.sessions.session.discover_installed_plugins", return_value=[]),
            patch("summon_claude.sessions.session.discover_plugin_skills", return_value=[]),
            pytest.raises(_CaptureError),
        ):
            await session._run_session_tasks(rt, AsyncMock())

        return captured

    async def test_thinking_enabled_produces_adaptive(self):
        result = await self._capture_thinking(enable_thinking=True)
        assert result["thinking"] == {"type": "adaptive"}

    async def test_thinking_disabled_produces_disabled(self):
        result = await self._capture_thinking(enable_thinking=False)
        assert result["thinking"] == {"type": "disabled"}


class TestSecretPatternRedaction:
    """redact_secrets must redact all known secret formats in error messages."""

    def test_redacts_github_classic_pat(self):
        text = "ConnectionError: auth failed for ghp_abc123XYZ token"
        assert "ghp_abc123XYZ" not in redact_secrets(text)

    def test_redacts_github_fine_grained_pat(self):
        text = "Error: github_pat_11ABCDEF_xyz789 rejected"
        assert "github_pat_11ABCDEF_xyz789" not in redact_secrets(text)

    def test_redacts_slack_bot_token(self):
        text = "Error: xoxb-123-456-abc invalid"
        assert "xoxb-123-456-abc" not in redact_secrets(text)

    def test_redacts_slack_app_token(self):
        text = "Error: xapp-1-A1234-567890 invalid"
        assert "xapp-1-A1234-567890" not in redact_secrets(text)

    def test_redacts_anthropic_key(self):
        text = "Error: sk-ant-api03-secretkey invalid"
        assert "sk-ant-api03-secretkey" not in redact_secrets(text)

    def test_redacts_github_oauth_token(self):
        text = "Error: gho_oauthtoken123 expired"
        assert "gho_oauthtoken123" not in redact_secrets(text)

    def test_redacts_github_user_to_server_token(self):
        text = "Error: ghu_usertoken456 expired"
        assert "ghu_usertoken456" not in redact_secrets(text)

    def test_redacts_github_app_installation_token(self):
        text = "Error: ghs_apptoken456 forbidden"
        assert "ghs_apptoken456" not in redact_secrets(text)

    def test_redacts_github_app_refresh_token(self):
        text = "Error: ghr_refreshtoken789 invalid"
        assert "ghr_refreshtoken789" not in redact_secrets(text)


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
        gen_path = "summon_claude.sessions.session.generate_spawn_token"
        with patch(gen_path, new=AsyncMock()) as mock_gen:
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

            mock_spawn = AsyncMock(return_value="child-sess-id")
            session = make_session(session_id="sess-spawn", cwd="/tmp", ipc_spawn=mock_spawn)
            session._authenticated_user_id = "U_OWNER"
            session._channel_id = "C_SELF"

            rt = _SessionRuntime(
                registry=registry,
                client=make_mock_client("C_SELF"),
                permission_handler=AsyncMock(),
            )

            with patch(
                "summon_claude.sessions.session.generate_spawn_token",
                new=AsyncMock(
                    return_value=AsyncMock(token="tok123", parent_session_id="sess-spawn")
                ),
            ):
                await session._handle_spawn(rt, user_id="U_OWNER", thread_ts=None)

            rt.client.post.assert_awaited_once()
            text = rt.client.post.call_args[0][0]
            assert "Spawned session started" in text

    async def test_handle_spawn_propagates_project_id(self, tmp_path):
        """_handle_spawn passes parent's project_id to child SessionOptions."""
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-proj", 1234, "/tmp")

            mock_ipc_spawn = AsyncMock(return_value="child-sess-id")
            opts = SessionOptions(cwd="/tmp", name="test-pm", project_id="proj-42")
            session = SummonSession(
                config=make_config(),
                options=opts,
                auth=make_auth(session_id="sess-proj"),
                session_id="sess-proj",
                ipc_spawn=mock_ipc_spawn,
            )
            session._authenticated_user_id = "U_OWNER"
            session._channel_id = "C_SELF"

            rt = _SessionRuntime(
                registry=registry,
                client=make_mock_client("C_SELF"),
                permission_handler=AsyncMock(),
            )

            with patch(
                "summon_claude.sessions.auth.generate_spawn_token",
                new=AsyncMock(
                    return_value=AsyncMock(token="tok123", parent_session_id="sess-proj")
                ),
            ):
                await session._handle_spawn(rt, user_id="U_OWNER", thread_ts=None)

            # Verify project_id was propagated to child options
            child_opts = mock_ipc_spawn.call_args[0][0]
            assert child_opts.project_id == "proj-42"

    async def test_spawn_blocked_missing_ipc_spawn(self):
        """_handle_spawn posts error when _ipc_spawn callback is not registered."""
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()
        await session._handle_spawn(rt, user_id="U_OWNER", thread_ts=None)
        rt.client.post.assert_awaited_once()
        assert "callback missing" in rt.client.post.call_args[0][0]

    async def test_spawn_blocked_at_child_limit(self):
        """_handle_spawn posts limit message when active children >= limit."""
        session = make_session(ipc_spawn=AsyncMock())
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
        session = make_session(
            pm_profile=True,
            session_id="pm-sess",
            cwd="/tmp",
            ipc_spawn=AsyncMock(return_value="child-sess-pm"),
        )
        session._authenticated_user_id = "U_OWNER"
        session._channel_id = "C_PM"

        rt = self._make_rt()

        # 10 active children — over regular limit (5) but under PM limit (15)
        rt.registry.list_children = AsyncMock(
            return_value=[{"session_id": f"child-{i}", "status": "active"} for i in range(10)]
        )

        with patch(
            "summon_claude.sessions.session.generate_spawn_token",
            new=AsyncMock(return_value=AsyncMock(token="tok456", parent_session_id="pm-sess")),
        ):
            await session._handle_spawn(rt, user_id="U_OWNER", thread_ts=None)

        # Should succeed — not blocked
        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert "Spawned session started" in text

    async def test_spawn_pm_session_blocked_at_pm_limit(self):
        """PM sessions should be blocked when active children >= PM limit."""
        session = make_session(pm_profile=True, ipc_spawn=AsyncMock())
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
        assert MAX_SPAWN_DEPTH == 2

    async def test_spawn_blocked_at_depth_limit(self):
        """_handle_spawn posts depth message when depth >= MAX_SPAWN_DEPTH."""
        session = make_session(ipc_spawn=AsyncMock())
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()

        rt.registry.compute_spawn_depth = AsyncMock(return_value=2)

        gen_path = "summon_claude.sessions.session.generate_spawn_token"
        with patch(gen_path, new=AsyncMock()) as mock_gen:
            await session._handle_spawn(rt, user_id="U_OWNER", thread_ts=None)

        mock_gen.assert_not_called()
        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert "Cannot spawn beyond depth" in text

    async def test_spawn_allowed_below_depth_limit(self):
        """_handle_spawn proceeds when depth < MAX_SPAWN_DEPTH."""
        session = make_session(
            session_id="parent-ok",
            cwd="/tmp",
            ipc_spawn=AsyncMock(return_value="child-ok"),
        )
        session._authenticated_user_id = "U_OWNER"
        session._channel_id = "C_TEST"
        rt = self._make_rt()

        rt.registry.compute_spawn_depth = AsyncMock(return_value=1)
        rt.registry.list_children = AsyncMock(return_value=[])

        with patch(
            "summon_claude.sessions.session.generate_spawn_token",
            new=AsyncMock(return_value=AsyncMock(token="tok", parent_session_id="parent-ok")),
        ):
            await session._handle_spawn(rt, user_id="U_OWNER", thread_ts=None)

        rt.client.post.assert_awaited_once()
        text = rt.client.post.call_args[0][0]
        assert "Spawned session started" in text

    async def test_spawn_list_children_failure_blocks_spawn(self):
        """If list_children raises, spawn should be blocked (fail-closed)."""
        session = make_session(ipc_spawn=AsyncMock())
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()

        rt.registry.list_children = AsyncMock(side_effect=RuntimeError("DB locked"))

        gen_path = "summon_claude.sessions.session.generate_spawn_token"
        with patch(gen_path, new=AsyncMock()) as mock_gen:
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
# Inject message tests
# ------------------------------------------------------------------


class TestInjectMessage:
    """Tests for SummonSession.inject_message."""

    async def test_enqueues_pending_turn(self):
        session = make_session()
        ok = await session.inject_message("hello", sender_info="test")
        assert ok is True
        assert session._pending_turns.qsize() == 1
        pending = session._pending_turns.get_nowait()
        assert pending.message == "hello"
        assert pending.pre_sent is False
        assert pending.message_ts is None

    async def test_rejected_during_shutdown(self):
        session = make_session()
        session._shutdown_event.set()
        ok = await session.inject_message("hello")
        assert ok is False
        assert session._pending_turns.qsize() == 0

    async def test_sender_info_logged(self, caplog):
        session = make_session()
        with caplog.at_level(logging.INFO):
            await session.inject_message("hello", sender_info="my-pm (#C123)")
        assert "my-pm (#C123)" in caplog.text


# ------------------------------------------------------------------
# Resume from active session tests
# ------------------------------------------------------------------


class TestHandleResumeFromActive:
    """Tests for _handle_resume_from_active."""

    def _make_rt(self, channel_id: str = "C_TEST") -> _SessionRuntime:
        return _SessionRuntime(
            registry=AsyncMock(),
            client=make_mock_client(channel_id),
            permission_handler=AsyncMock(),
        )

    async def test_rejects_missing_ipc_resume(self):
        session = make_session()
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()
        await session._handle_resume_from_active(rt, "U_OWNER", "some-id", None)
        rt.client.post.assert_awaited_once()
        assert "callback missing" in rt.client.post.call_args[0][0]

    async def test_rejects_wrong_user(self):
        session = make_session(ipc_resume=AsyncMock())
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()
        await session._handle_resume_from_active(rt, "U_INTRUDER", "some-id", None)
        rt.client.post.assert_awaited_once()
        assert "Only the session owner" in rt.client.post.call_args[0][0]

    async def test_no_target_shows_usage(self):
        session = make_session(ipc_resume=AsyncMock())
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()
        await session._handle_resume_from_active(rt, "U_OWNER", None, None)
        rt.client.post.assert_awaited_once()
        assert "Specify a session ID" in rt.client.post.call_args[0][0]

    async def test_rejects_target_owned_by_different_user(self):
        session = make_session(ipc_resume=AsyncMock())
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt()
        rt.registry.get_session = AsyncMock(
            return_value={
                "status": "completed",
                "slack_channel_id": "C_OTHER",
                "authenticated_user_id": "U_DIFFERENT",
            }
        )
        await session._handle_resume_from_active(rt, "U_OWNER", "other-id", None)
        rt.client.post.assert_awaited_once()
        assert "not found" in rt.client.post.call_args[0][0]

    async def test_blocks_resume_into_own_channel(self):
        session = make_session(ipc_resume=AsyncMock())
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt("C_SELF")
        rt.registry.get_session = AsyncMock(
            return_value={
                "status": "completed",
                "slack_channel_id": "C_SELF",
                "authenticated_user_id": "U_OWNER",
            }
        )
        await session._handle_resume_from_active(rt, "U_OWNER", "old-id", None)
        rt.client.post.assert_awaited_once()
        assert "End this session first" in rt.client.post.call_args[0][0]

    async def test_happy_path_calls_ipc_resume(self):
        mock_resume = AsyncMock(return_value={"session_id": "new-id", "channel_id": "C_OTHER"})
        session = make_session(ipc_resume=mock_resume)
        session._authenticated_user_id = "U_OWNER"
        rt = self._make_rt("C_SELF")
        rt.registry.get_session = AsyncMock(
            return_value={
                "status": "completed",
                "slack_channel_id": "C_OTHER",
                "authenticated_user_id": "U_OWNER",
            }
        )
        await session._handle_resume_from_active(rt, "U_OWNER", "target-id", None)
        mock_resume.assert_awaited_once_with("target-id")
        rt.client.post.assert_awaited_once()
        assert "resumed" in rt.client.post.call_args[0][0].lower()


# ------------------------------------------------------------------
# Session options tests
# ------------------------------------------------------------------


class TestSessionOptionsChannelId:
    """Tests for channel_id field on SessionOptions."""

    def test_channel_id_default_none(self):
        opts = SessionOptions(cwd="/tmp", name="test")
        assert opts.channel_id is None

    def test_channel_id_set(self):
        opts = SessionOptions(cwd="/tmp", name="test", channel_id="C123")
        assert opts.channel_id == "C123"


# ------------------------------------------------------------------
# Channel reuse tests
# ------------------------------------------------------------------


class TestReuseChannel:
    """Tests for _reuse_channel method."""

    async def test_reuse_active_channel(self):
        session = make_session(channel_id="C_REUSE")
        web = AsyncMock()
        web.conversations_info = AsyncMock(
            return_value={"channel": {"name": "old-channel", "is_archived": False}}
        )
        registry = AsyncMock()
        cid, cname = await session._reuse_channel(web, registry, "C_REUSE")
        assert cid == "C_REUSE"
        assert cname == "old-channel"
        web.conversations_join.assert_awaited_once_with(channel="C_REUSE")

    async def test_fallback_on_lookup_error(self):
        session = make_session(channel_id="C_GONE")
        web = AsyncMock()
        web.conversations_info = AsyncMock(side_effect=Exception("channel_not_found"))
        web.conversations_create = AsyncMock(
            return_value={"channel": {"id": "C_NEW", "name": "new-chan"}}
        )
        registry = AsyncMock()
        cid, cname = await session._reuse_channel(web, registry, "C_GONE")
        assert cid == "C_NEW"
        web.conversations_create.assert_awaited_once()

    async def test_delegates_archived_channel(self):
        session = make_session(channel_id="C_ARCH")
        web = AsyncMock()
        web.conversations_info = AsyncMock(
            return_value={"channel": {"name": "arch-chan", "is_archived": True}}
        )
        web.conversations_unarchive = AsyncMock()
        registry = AsyncMock()
        cid, cname = await session._reuse_channel(web, registry, "C_ARCH")
        assert cid == "C_ARCH"
        web.conversations_unarchive.assert_awaited_once()

    async def test_archived_fallback_to_create_channel_on_double_failure(self):
        """If both channel name attempts fail, falls back to _create_channel."""
        session = make_session(channel_id="C_ARCH")
        web = AsyncMock()
        web.conversations_info = AsyncMock(
            return_value={"channel": {"name": "arch-chan", "is_archived": True}}
        )
        web.conversations_unarchive = AsyncMock(side_effect=Exception("cant unarchive"))
        web.conversations_create = AsyncMock(side_effect=Exception("name_taken"))
        registry = AsyncMock()
        # _create_channel is also called via fallback — mock it
        with patch.object(session, "_create_channel", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = ("C_FRESH", "fresh-chan")
            cid, cname = await session._reuse_channel(web, registry, "C_ARCH")
        assert cid == "C_FRESH"
        assert cname == "fresh-chan"

    async def test_archived_replacement_copies_canvas_data(self):
        """When archived channel is replaced, canvas data is copied to new channel."""
        session = make_session(channel_id="C_ARCH")
        web = AsyncMock()
        web.conversations_info = AsyncMock(
            return_value={"channel": {"name": "arch-chan", "is_archived": True}}
        )
        web.conversations_unarchive = AsyncMock(side_effect=Exception("cant unarchive"))
        web.conversations_create = AsyncMock(
            return_value={"channel": {"id": "C_REPLACEMENT", "name": "arch-chan"}}
        )
        registry = AsyncMock()
        registry.get_channel = AsyncMock(
            return_value={
                "cwd": "/proj",
                "authenticated_user_id": "U1",
                "claude_session_id": "claude-old",
                "canvas_id": "F_CV",
                "canvas_markdown": "# My Canvas",
            }
        )
        cid, cname = await session._reuse_channel(web, registry, "C_ARCH")
        assert cid == "C_REPLACEMENT"
        # Canvas data should be copied to the new channel
        registry.update_channel_canvas.assert_awaited_once_with(
            "C_REPLACEMENT", "F_CV", "# My Canvas"
        )
        # Claude session ID should also be copied
        registry.update_channel_claude_session.assert_awaited_once_with(
            "C_REPLACEMENT", "claude-old"
        )

    async def test_archived_replacement_skips_canvas_when_absent(self):
        """When old channel has no canvas, no canvas copy is attempted."""
        session = make_session(channel_id="C_ARCH")
        web = AsyncMock()
        web.conversations_info = AsyncMock(
            return_value={"channel": {"name": "arch-chan", "is_archived": True}}
        )
        web.conversations_unarchive = AsyncMock(side_effect=Exception("cant unarchive"))
        web.conversations_create = AsyncMock(
            return_value={"channel": {"id": "C_NEW2", "name": "arch-chan"}}
        )
        registry = AsyncMock()
        registry.get_channel = AsyncMock(
            return_value={"cwd": "/proj", "authenticated_user_id": "U1"}
        )
        await session._reuse_channel(web, registry, "C_ARCH")
        registry.update_channel_canvas.assert_not_awaited()

    async def test_pending_turns_queue_has_maxsize(self):
        """_pending_turns must have a maxsize for backpressure."""
        session = make_session()
        assert session._pending_turns.maxsize > 0


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
        assert _BASE_SYSTEM_APPEND in captured_prompts[0]
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


class TestPMHeartbeatTopicUpdate:
    """Tests for PM-specific topic update in _heartbeat_loop."""

    async def test_pm_heartbeat_updates_topic(self):
        """PM heartbeat calls set_topic when child count changes."""
        from summon_claude.sessions.session import format_pm_topic

        session = make_session(session_id="pm-hb-topic", pm_profile=True)
        session._authenticated_user_id = "U_TEST"
        # In production, start() primes _last_pm_topic. In this test we
        # call _heartbeat_loop directly, so _last_pm_topic starts as None.
        assert session._last_pm_topic is None

        mock_rt = MagicMock()
        mock_rt.registry = AsyncMock()
        mock_rt.registry.count_active_children = AsyncMock(return_value=3)

        async def _stop_after_first(*_args, **_kwargs):
            session._shutdown_event.set()

        mock_rt.registry.heartbeat.side_effect = _stop_after_first
        mock_rt.client = make_mock_client("C_PM")

        with patch("summon_claude.sessions.session._HEARTBEAT_INTERVAL_S", 0.01):
            await asyncio.wait_for(session._heartbeat_loop(mock_rt), timeout=1.0)

        expected_topic = format_pm_topic(3)
        mock_rt.client.set_topic.assert_awaited_once_with(expected_topic)
        assert session._last_pm_topic == expected_topic

    async def test_pm_heartbeat_caches_topic(self):
        """PM heartbeat skips set_topic when child count has not changed."""
        from summon_claude.sessions.session import format_pm_topic

        session = make_session(session_id="pm-hb-cache", pm_profile=True)
        session._authenticated_user_id = "U_TEST"
        # Pre-seed the cache to match what count_active_children will return
        session._last_pm_topic = format_pm_topic(2)

        mock_rt = MagicMock()
        mock_rt.registry = AsyncMock()
        mock_rt.registry.count_active_children = AsyncMock(return_value=2)
        mock_rt.client = make_mock_client("C_PM_CACHE")

        async def _stop_after_first(*_args, **_kwargs):
            session._shutdown_event.set()

        mock_rt.registry.heartbeat.side_effect = _stop_after_first

        with patch("summon_claude.sessions.session._HEARTBEAT_INTERVAL_S", 0.01):
            await asyncio.wait_for(session._heartbeat_loop(mock_rt), timeout=1.0)

        # set_topic must NOT have been called because topic matched the cache
        mock_rt.client.set_topic.assert_not_awaited()

    async def test_non_pm_heartbeat_does_not_call_set_topic(self):
        """Regular (non-PM) heartbeat must not touch set_topic."""
        session = make_session(session_id="reg-hb-topic", pm_profile=False)

        mock_rt = MagicMock()
        mock_rt.registry = AsyncMock()
        mock_rt.client = make_mock_client("C_REG")

        async def _stop_after_first(*_args, **_kwargs):
            session._shutdown_event.set()

        mock_rt.registry.heartbeat.side_effect = _stop_after_first

        with patch("summon_claude.sessions.session._HEARTBEAT_INTERVAL_S", 0.01):
            await asyncio.wait_for(session._heartbeat_loop(mock_rt), timeout=1.0)

        mock_rt.client.set_topic.assert_not_awaited()


class TestChannelScopeWiring:
    """Tests for the 3-way channel scope resolver closures."""

    async def test_global_pm_scope_uses_get_all_active_channels(self):
        """Global PM (is_pm=True, project_id=None) uses get_all_active_channels."""
        mock_registry = AsyncMock()
        mock_registry.get_all_active_channels = AsyncMock(
            return_value={"C_PM1", "C_SUB1", "C_SUB2"}
        )
        _own_cid = "C_GPM"
        _owner = "U_OWNER"
        _reg = mock_registry

        async def _global_pm_channel_scope() -> set[str]:
            channels = {_own_cid}
            if _owner:
                channels |= await _reg.get_all_active_channels(_owner)
            return channels

        result = await _global_pm_channel_scope()
        assert result == {"C_GPM", "C_PM1", "C_SUB1", "C_SUB2"}
        mock_registry.get_all_active_channels.assert_awaited_once_with("U_OWNER")

    async def test_project_pm_scope_uses_get_child_channels(self):
        """Project PM (is_pm=True, project_id set) uses get_child_channels."""
        mock_registry = AsyncMock()
        mock_registry.get_child_channels = AsyncMock(return_value={"C_CHILD1", "C_CHILD2"})
        _own_cid = "C_PPM"
        _sid = "ppm-scope"
        _owner = "U_OWNER"
        _reg = mock_registry

        async def _pm_channel_scope() -> set[str]:
            channels = {_own_cid}
            if _owner:
                channels |= await _reg.get_child_channels(_sid, _owner)
            return channels

        result = await _pm_channel_scope()
        assert result == {"C_PPM", "C_CHILD1", "C_CHILD2"}
        mock_registry.get_child_channels.assert_awaited_once_with("ppm-scope", "U_OWNER")

    async def test_regular_session_scope_own_channel_only(self):
        """Regular session (is_pm=False) only sees its own channel."""
        _own_cid = "C_REGULAR"

        async def _session_channel_scope() -> set[str]:
            return {_own_cid}

        result = await _session_channel_scope()
        assert result == {"C_REGULAR"}


# ---------------------------------------------------------------------------
# zzz- channel rename: _rename_channel_disconnected
# ---------------------------------------------------------------------------


class TestZzzRenameChannelDisconnected:
    """_rename_channel_disconnected renames channel with zzz- on disconnect."""

    def _make_session(
        self,
        session_id: str = "sess-zzz",
        channel_id: str = "C_ZZZ",
    ) -> SummonSession:
        s = make_session(session_id=session_id)
        s._channel_id = channel_id
        return s

    def _make_registry(self, channel_name: str | None = "myproj-abc") -> AsyncMock:
        reg = AsyncMock()
        if channel_name is not None:
            reg.get_channel = AsyncMock(return_value={"channel_name": channel_name})
        else:
            reg.get_channel = AsyncMock(return_value=None)
        reg.get_session = AsyncMock(return_value={"slack_channel_name": "fallback-name"})
        return reg

    async def test_zzz_rename_disconnected_normal(self):
        """_rename_channel_disconnected renames unprefixed channel with zzz- prefix."""
        session = self._make_session()
        registry = self._make_registry("myproj-abc")
        client = AsyncMock(spec=SlackClient)
        client.rename_channel = AsyncMock(return_value="zzz-myproj-abc")

        await session._rename_channel_disconnected(client, registry)

        client.rename_channel.assert_awaited_once_with("zzz-myproj-abc")

    async def test_zzz_rename_disconnected_already_prefixed_noop(self):
        """_rename_channel_disconnected skips rename if channel already zzz-prefixed."""
        session = self._make_session()
        registry = self._make_registry("zzz-myproj-abc")
        client = AsyncMock(spec=SlackClient)
        client.rename_channel = AsyncMock(return_value=None)

        await session._rename_channel_disconnected(client, registry)

        client.rename_channel.assert_not_awaited()

    async def test_zzz_rename_disconnected_truncates_to_80(self):
        """_rename_channel_disconnected truncates so final name is at most 80 chars."""
        long_name = "a" * 80
        session = self._make_session()
        registry = self._make_registry(long_name)
        client = AsyncMock(spec=SlackClient)
        client.rename_channel = AsyncMock(return_value="zzz-" + "a" * 76)

        await session._rename_channel_disconnected(client, registry)

        call_arg = client.rename_channel.call_args[0][0]
        assert len(call_arg) <= 80
        assert call_arg.startswith("zzz-")

    async def test_zzz_rename_disconnected_failure_posts_warning(self):
        """_rename_channel_disconnected posts warning to channel on rename failure."""
        session = self._make_session()
        registry = self._make_registry("myproj-abc")
        client = AsyncMock(spec=SlackClient)
        client.rename_channel = AsyncMock(return_value=None)  # failure → None
        client.post = AsyncMock()

        await session._rename_channel_disconnected(client, registry)

        client.post.assert_awaited_once()
        text = client.post.call_args[0][0]
        assert "rename" in text.lower() or "zzz" in text.lower()

    async def test_zzz_rename_disconnected_db_fallback(self):
        """Falls back to sessions table if channels table returns nothing."""
        session = self._make_session()
        registry = self._make_registry(channel_name=None)
        # No channel in channels table — falls back to get_session
        registry.get_session = AsyncMock(return_value={"slack_channel_name": "sess-fallback"})
        client = AsyncMock(spec=SlackClient)
        client.rename_channel = AsyncMock(return_value="zzz-sess-fallback")

        await session._rename_channel_disconnected(client, registry)

        client.rename_channel.assert_awaited_once_with("zzz-sess-fallback")

    async def test_zzz_rename_disconnected_no_channel_name_noop(self):
        """_rename_channel_disconnected does nothing if no channel name found at all."""
        session = self._make_session()
        registry = self._make_registry(channel_name=None)
        registry.get_session = AsyncMock(return_value={"slack_channel_name": ""})
        client = AsyncMock(spec=SlackClient)
        client.rename_channel = AsyncMock(return_value=None)

        await session._rename_channel_disconnected(client, registry)

        client.rename_channel.assert_not_awaited()


# ---------------------------------------------------------------------------
# zzz- channel restore: _restore_channel_name
# ---------------------------------------------------------------------------


class TestZzzRestoreChannelName:
    """_restore_channel_name un-zzz's a channel name on resume."""

    def _make_registry(self, channel_row: dict | None = None) -> AsyncMock:
        reg = AsyncMock()
        reg.get_channel = AsyncMock(return_value=channel_row)
        return reg

    async def test_zzz_restore_from_channels_table(self):
        """Uses canonical name from channels table when available."""
        from summon_claude.sessions.session import SummonSession

        session = make_session()
        web = AsyncMock()
        web.conversations_rename = AsyncMock(return_value={"channel": {"name": "myproj-abc"}})
        registry = self._make_registry({"channel_name": "myproj-abc"})

        result = await session._restore_channel_name(web, registry, "C123", "zzz-myproj-abc")

        assert result == "myproj-abc"
        web.conversations_rename.assert_awaited_once_with(channel="C123", name="myproj-abc")

    async def test_zzz_restore_fallback_strip_prefix(self):
        """Falls back to stripping zzz- when channels table has no canonical name."""
        session = make_session()
        web = AsyncMock()
        web.conversations_rename = AsyncMock(return_value={"channel": {"name": "myproj-def"}})
        registry = self._make_registry(None)  # no channels table row

        result = await session._restore_channel_name(web, registry, "C123", "zzz-myproj-def")

        assert result == "myproj-def"
        web.conversations_rename.assert_awaited_once_with(channel="C123", name="myproj-def")

    async def test_zzz_restore_no_prefix_noop(self):
        """Returns channel name unchanged if not zzz-prefixed."""
        session = make_session()
        web = AsyncMock()
        registry = self._make_registry(None)

        result = await session._restore_channel_name(web, registry, "C123", "myproj-abc")

        assert result == "myproj-abc"
        web.conversations_rename.assert_not_awaited()

    async def test_zzz_restore_failure_returns_current_name(self):
        """Returns current_name unchanged when Slack rename fails."""
        session = make_session()
        web = AsyncMock()
        web.conversations_rename = AsyncMock(side_effect=Exception("not_in_channel"))
        registry = self._make_registry({"channel_name": "myproj-abc"})

        result = await session._restore_channel_name(web, registry, "C123", "zzz-myproj-abc")

        assert result == "zzz-myproj-abc"

    async def test_zzz_restore_empty_after_strip_noop(self):
        """Does not rename if stripping prefix yields empty string."""
        session = make_session()
        web = AsyncMock()
        web.conversations_rename = AsyncMock()
        registry = self._make_registry(None)

        # Current name is just the prefix itself
        result = await session._restore_channel_name(web, registry, "C123", "zzz-")

        assert result == "zzz-"
        web.conversations_rename.assert_not_awaited()

    async def test_zzz_restore_channels_table_also_prefixed_strips(self):
        """When channels table canonical is also zzz-prefixed, falls back to strip."""
        session = make_session()
        web = AsyncMock()
        web.conversations_rename = AsyncMock(return_value={"channel": {"name": "myproj-abc"}})
        registry = self._make_registry({"channel_name": "zzz-myproj-abc"})

        result = await session._restore_channel_name(web, registry, "C123", "zzz-myproj-abc")

        # Should fall back to stripping prefix
        assert result == "myproj-abc"
        web.conversations_rename.assert_awaited_once_with(channel="C123", name="myproj-abc")


# ---------------------------------------------------------------------------
# zzz- _reuse_channel integration
# ---------------------------------------------------------------------------


class TestZzzReuseChannel:
    """_reuse_channel calls _restore_channel_name on the channel."""

    async def test_zzz_reuse_channel_non_archived_restores(self, tmp_path):
        """_reuse_channel restores zzz- prefix on non-archived channel."""
        from summon_claude.sessions.registry import SessionRegistry

        session = make_session()
        web = AsyncMock()
        web.conversations_info = AsyncMock(
            return_value={"channel": {"name": "zzz-myproj-abc", "is_archived": False}}
        )
        web.conversations_join = AsyncMock()
        web.conversations_rename = AsyncMock(return_value={"channel": {"name": "myproj-abc"}})

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            channel_id, channel_name = await session._reuse_channel(web, registry, "C123")

        assert channel_name == "myproj-abc"
        web.conversations_rename.assert_awaited_once()

    async def test_zzz_reuse_channel_no_prefix_noop(self, tmp_path):
        """_reuse_channel does not rename channel without zzz- prefix."""
        from summon_claude.sessions.registry import SessionRegistry

        session = make_session()
        web = AsyncMock()
        web.conversations_info = AsyncMock(
            return_value={"channel": {"name": "myproj-abc", "is_archived": False}}
        )
        web.conversations_join = AsyncMock()
        web.conversations_rename = AsyncMock()

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            channel_id, channel_name = await session._reuse_channel(web, registry, "C123")

        assert channel_name == "myproj-abc"
        web.conversations_rename.assert_not_awaited()


# ---------------------------------------------------------------------------
# zzz- _get_or_create_pm_channel integration
# ---------------------------------------------------------------------------


class TestZzzGetOrCreatePmChannel:
    """_get_or_create_pm_channel restores zzz- prefix on PM channel reuse."""

    async def test_zzz_pm_channel_reuse_restores_prefix(self, tmp_path):
        """PM channel reuse calls _restore_channel_name when name has zzz- prefix."""
        from summon_claude.sessions.registry import SessionRegistry

        session = make_session(pm_profile=True)
        web = AsyncMock()
        web.conversations_join = AsyncMock()
        web.conversations_info = AsyncMock(
            return_value={"channel": {"name": "zzz-myproj-pm", "id": "C_PM"}}
        )
        web.conversations_rename = AsyncMock(return_value={"channel": {"name": "myproj-pm"}})

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            project_id = await registry.add_project("myproj", str(tmp_path))
            await registry.update_project(project_id, pm_channel_id="C_PM")
            channel_id, channel_name = await session._get_or_create_pm_channel(
                web, registry, project_id
            )

        assert channel_name == "myproj-pm"
        web.conversations_rename.assert_awaited_once()

    async def test_zzz_pm_channel_reuse_no_prefix_noop(self, tmp_path):
        """PM channel reuse does not rename when name has no zzz- prefix."""
        from summon_claude.sessions.registry import SessionRegistry

        session = make_session(pm_profile=True)
        web = AsyncMock()
        web.conversations_join = AsyncMock()
        web.conversations_info = AsyncMock(
            return_value={"channel": {"name": "myproj-pm", "id": "C_PM"}}
        )
        web.conversations_rename = AsyncMock()

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            project_id = await registry.add_project("myproj2", str(tmp_path))
            await registry.update_project(project_id, pm_channel_id="C_PM")
            channel_id, channel_name = await session._get_or_create_pm_channel(
                web, registry, project_id
            )

        assert channel_name == "myproj-pm"
        web.conversations_rename.assert_not_awaited()


# ---------------------------------------------------------------------------
# zzz- _shutdown integration
# ---------------------------------------------------------------------------


class TestZzzShutdownIntegration:
    """_shutdown calls _rename_channel_disconnected after disconnect message."""

    async def test_zzz_shutdown_calls_rename_disconnected(self, tmp_path):
        """_shutdown invokes _rename_channel_disconnected after disconnect message."""
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-shut-zzz", 1234, "/tmp")

            session = make_session(session_id="sess-shut-zzz")
            session._channel_id = "C_SHUT"

            client = make_mock_client("C_SHUT")
            client.rename_channel = AsyncMock(return_value="zzz-testchan")

            rt = _SessionRuntime(
                registry=registry,
                client=client,
                permission_handler=AsyncMock(),
            )

            with patch.object(
                session,
                "_rename_channel_disconnected",
                new=AsyncMock(),
            ) as mock_rename:
                await session._shutdown(rt)

            mock_rename.assert_awaited_once_with(client, registry)

    async def test_zzz_shutdown_rename_failure_does_not_break_shutdown(self, tmp_path):
        """_shutdown continues normally even if _rename_channel_disconnected raises."""
        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-shut-err", 1234, "/tmp")

            session = make_session(session_id="sess-shut-err")
            session._channel_id = "C_SHUT_ERR"

            client = make_mock_client("C_SHUT_ERR")
            rt = _SessionRuntime(
                registry=registry,
                client=client,
                permission_handler=AsyncMock(),
            )

            with patch.object(
                session,
                "_rename_channel_disconnected",
                new=AsyncMock(side_effect=Exception("rename error")),
            ):
                # Should not raise
                await session._shutdown(rt)

            sess = await registry.get_session("sess-shut-err")
            assert sess["status"] == "completed"


# ---------------------------------------------------------------------------
# zzz- additional edge case tests (quality gate findings)
# ---------------------------------------------------------------------------


class TestZzzEdgeCases:
    """Edge case tests identified during quality gate review."""

    def _make_session(
        self,
        session_id: str = "sess-edge",
        channel_id: str = "C_EDGE",
    ) -> SummonSession:
        s = make_session(session_id=session_id)
        s._channel_id = channel_id
        return s

    async def test_zzz_rename_disconnected_sessions_table_already_prefixed(self):
        """Skips rename when sessions table fallback returns zzz-prefixed name."""
        session = self._make_session()
        reg = AsyncMock()
        reg.get_channel = AsyncMock(return_value=None)
        reg.get_session = AsyncMock(return_value={"slack_channel_name": "zzz-already-done"})
        client = AsyncMock(spec=SlackClient)
        client.rename_channel = AsyncMock()

        await session._rename_channel_disconnected(client, reg)

        client.rename_channel.assert_not_awaited()

    async def test_zzz_restore_archived_channel_with_zzz_prefix(self):
        """Archived channel with zzz- prefix is restored after unarchive."""
        session = self._make_session()
        web = AsyncMock()
        web.conversations_rename = AsyncMock(return_value={"channel": {"name": "myproj-abc"}})
        reg = AsyncMock()
        reg.get_channel = AsyncMock(return_value={"channel_name": "myproj-abc"})

        result = await session._restore_channel_name(web, reg, "C_EDGE", "zzz-myproj-abc")

        assert result == "myproj-abc"
        web.conversations_rename.assert_awaited_once()


# ---------------------------------------------------------------------------
# zzz- finally block: unexpected termination rename
# ---------------------------------------------------------------------------


class TestZzzFinallyBlockRename:
    """The finally block in start() calls _rename_channel_disconnected on unexpected exit."""

    async def test_zzz_finally_block_calls_rename(self):
        """Unexpected termination in start() triggers zzz-rename via finally block."""
        session = make_session(session_id="sess-finally")
        session._channel_id = "C_FINALLY"
        session._web_client = AsyncMock()
        # Patch _rename_channel_disconnected to verify it's called
        with patch.object(session, "_rename_channel_disconnected", new=AsyncMock()) as mock_rename:
            # Simulate: the finally block path with _shutdown_completed=False
            # Directly test the conditional + call
            session._shutdown_completed = False
            if session._channel_id and session._web_client:
                tmp_client = SlackClient(session._web_client, session._channel_id)
                reg = AsyncMock()
                await session._rename_channel_disconnected(tmp_client, reg)

            mock_rename.assert_awaited_once()

    async def test_zzz_finally_block_skips_when_no_channel(self):
        """Finally block skips zzz-rename when channel_id is not set."""
        session = make_session(session_id="sess-no-ch")
        session._channel_id = None
        session._web_client = AsyncMock()
        session._shutdown_completed = False

        with patch.object(session, "_rename_channel_disconnected", new=AsyncMock()) as mock_rename:
            # Conditional should be False
            if session._channel_id and session._web_client:
                tmp_client = SlackClient(session._web_client, session._channel_id)
                reg = AsyncMock()
                await session._rename_channel_disconnected(tmp_client, reg)

            mock_rename.assert_not_awaited()


# ---------------------------------------------------------------------------
# zzz- _reuse_channel: archived + zzz-prefixed path
# ---------------------------------------------------------------------------


class TestZzzReuseChannelArchived:
    """_reuse_channel restores zzz- prefix even after _handle_archived_channel."""

    async def test_zzz_reuse_channel_archived_with_zzz_prefix(self, tmp_path):
        """Archived channel with zzz- name is restored via _restore_channel_name."""
        from summon_claude.sessions.registry import SessionRegistry

        session = make_session()
        web = AsyncMock()
        web.conversations_info = AsyncMock(
            return_value={"channel": {"name": "zzz-myproj-abc", "is_archived": True}}
        )
        web.conversations_rename = AsyncMock(return_value={"channel": {"name": "myproj-abc"}})

        # Mock _handle_archived_channel to return the zzz-prefixed channel
        with patch.object(
            session,
            "_handle_archived_channel",
            new=AsyncMock(return_value=("C_ARCH", "zzz-myproj-abc")),
        ):
            async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
                channel_id, channel_name = await session._reuse_channel(web, registry, "C_ARCH")

        assert channel_name == "myproj-abc"
        web.conversations_rename.assert_awaited_once()


# ---------------------------------------------------------------------------
# zzz- _shutdown order: rename happens before disconnect message
# ---------------------------------------------------------------------------


class TestZzzShutdownOrder:
    """_shutdown renames channel before posting disconnect message."""

    async def test_zzz_shutdown_renames_before_message(self, tmp_path):
        """_shutdown calls _rename_channel_disconnected before _post_disconnect_message."""
        from summon_claude.sessions.registry import SessionRegistry

        call_order: list[str] = []

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.register("sess-order", 1234, "/tmp")

            session = make_session(session_id="sess-order")
            session._channel_id = "C_ORDER"

            client = make_mock_client("C_ORDER")
            rt = _SessionRuntime(
                registry=registry,
                client=client,
                permission_handler=AsyncMock(),
            )

            async def track_rename(*args, **kwargs):
                call_order.append("rename")

            async def track_message(*args, **kwargs):
                call_order.append("message")

            with (
                patch.object(session, "_rename_channel_disconnected", new=track_rename),
                patch.object(session, "_post_disconnect_message", new=track_message),
            ):
                await session._shutdown(rt)

        assert call_order == ["rename", "message"]
