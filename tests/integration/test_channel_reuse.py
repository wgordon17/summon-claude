"""Integration tests for channel reuse and archived channel recovery.

Tests the composite flows in SummonSession._reuse_channel and
_handle_archived_channel against real Slack, with real SQLite registry
for channel data migration (canvas, claude_session_id).

Also tests EventDispatcher routing and message injection with
real asyncio queues, and the registry channel operations that
underpin the resume pipeline.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from conftest import make_test_config

from summon_claude.config import SummonConfig
from summon_claude.event_dispatcher import EventDispatcher, SessionHandle
from summon_claude.sessions.manager import SessionManager
from summon_claude.sessions.registry import SessionRegistry
from summon_claude.sessions.session import (
    SessionOptions,
    SummonSession,
    _make_channel_name,
)
from summon_claude.slack.client import SlackClient

if TYPE_CHECKING:
    from tests.integration.conftest import SlackTestHarness

# NOTE: No module-level pytestmark — classes apply marks individually.
# Slack-dependent classes use @pytest.mark.slack; local-only classes have no mark.


def _make_config(harness: SlackTestHarness) -> SummonConfig:
    """Build a SummonConfig from test harness credentials."""
    return SummonConfig(
        slack_bot_token=harness.bot_token,
        slack_app_token=harness.app_token,
        slack_signing_secret=harness.signing_secret,
        default_model="claude-sonnet-4-20250514",
        channel_prefix="test",
        permission_debounce_ms=10,
        max_inline_chars=2500,
        _env_file=None,
    )


@pytest.mark.slack
class TestReuseActiveChannel:
    """Test _reuse_channel with an active (non-archived) channel."""

    async def test_reuse_active_channel_returns_same_id(
        self, slack_harness, fresh_channel, registry
    ):
        """_reuse_channel on an active channel returns the same channel_id."""
        config = _make_config(slack_harness)
        options = SessionOptions(cwd="/tmp/test", name="reuse-test", channel_id=fresh_channel)
        session = SummonSession(
            config=config,
            options=options,
            auth=None,
            session_id="test-reuse-active",
            web_client=slack_harness.client,
            dispatcher=EventDispatcher(),
            bot_user_id=await slack_harness.resolve_bot_user_id(),
        )
        channel_id, channel_name = await session._reuse_channel(
            slack_harness.client, registry, fresh_channel
        )
        assert channel_id == fresh_channel
        assert isinstance(channel_name, str)
        assert len(channel_name) > 0

    async def test_reuse_active_channel_can_post_message(
        self, slack_harness, fresh_channel, registry
    ):
        """After reuse, the channel is usable for posting messages."""
        config = _make_config(slack_harness)
        options = SessionOptions(cwd="/tmp/test", name="reuse-post", channel_id=fresh_channel)
        session = SummonSession(
            config=config,
            options=options,
            auth=None,
            session_id="test-reuse-post",
            web_client=slack_harness.client,
            dispatcher=EventDispatcher(),
            bot_user_id=await slack_harness.resolve_bot_user_id(),
        )
        await session._reuse_channel(slack_harness.client, registry, fresh_channel)
        # Verify we can post to the reused channel
        client = SlackClient(slack_harness.client, fresh_channel)
        ref = await client.post("Message after channel reuse")
        assert ref.ts


@pytest.mark.slack
class TestReuseArchivedChannel:
    """Test _reuse_channel and _handle_archived_channel with archived channels."""

    async def test_reuse_archived_channel_unarchives(self, slack_harness, fresh_channel, registry):
        """_reuse_channel on an archived channel unarchives it."""
        # Archive the channel first (with rate-limit retry)
        await slack_harness.client.conversations_archive(channel=fresh_channel)

        config = _make_config(slack_harness)
        options = SessionOptions(cwd="/tmp/test", name="unarchive-test", channel_id=fresh_channel)
        session = SummonSession(
            config=config,
            options=options,
            auth=None,
            session_id="test-unarchive",
            web_client=slack_harness.client,
            dispatcher=EventDispatcher(),
            bot_user_id=await slack_harness.resolve_bot_user_id(),
        )
        channel_id, channel_name = await session._reuse_channel(
            slack_harness.client, registry, fresh_channel
        )
        # Should return the SAME channel (unarchived it)
        assert channel_id == fresh_channel

        # Verify channel is actually unarchived via Slack API
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        assert info["channel"]["is_archived"] is False

    async def test_reuse_archived_channel_preserves_canvas_data(
        self, slack_harness, fresh_channel, registry
    ):
        """When archived channel is unarchived, canvas data is preserved."""
        # Seed canvas data in channels table
        await registry.register_channel(
            channel_id=fresh_channel,
            channel_name="canvas-preserve",
            cwd="/tmp/test",
            authenticated_user_id="U_TEST",
        )
        await registry.update_channel_canvas(
            fresh_channel, "F_CANVAS_123", "# Important Canvas Data"
        )
        await registry.update_channel_claude_session(fresh_channel, "claude-sid-abc")

        # Archive the channel (with rate-limit retry)
        await slack_harness.client.conversations_archive(channel=fresh_channel)

        config = _make_config(slack_harness)
        options = SessionOptions(cwd="/tmp/test", name="canvas-test", channel_id=fresh_channel)
        session = SummonSession(
            config=config,
            options=options,
            auth=None,
            session_id="test-canvas-preserve",
            web_client=slack_harness.client,
            dispatcher=EventDispatcher(),
            bot_user_id=await slack_harness.resolve_bot_user_id(),
        )
        channel_id, _ = await session._reuse_channel(slack_harness.client, registry, fresh_channel)

        # Unarchived same channel — canvas data should still be in registry
        assert channel_id == fresh_channel
        channel = await registry.get_channel(channel_id)
        assert channel is not None
        assert channel["canvas_id"] == "F_CANVAS_123"
        assert channel["canvas_markdown"] == "# Important Canvas Data"
        assert channel["claude_session_id"] == "claude-sid-abc"

    async def test_reuse_archived_channel_can_post(self, slack_harness, fresh_channel, registry):
        """After unarchiving via _reuse_channel, posting messages works."""
        await slack_harness.client.conversations_archive(channel=fresh_channel)

        config = _make_config(slack_harness)
        options = SessionOptions(cwd="/tmp/test", name="post-test", channel_id=fresh_channel)
        session = SummonSession(
            config=config,
            options=options,
            auth=None,
            session_id="test-post-unarchive",
            web_client=slack_harness.client,
            dispatcher=EventDispatcher(),
            bot_user_id=await slack_harness.resolve_bot_user_id(),
        )
        await session._reuse_channel(slack_harness.client, registry, fresh_channel)

        client = SlackClient(slack_harness.client, fresh_channel)
        ref = await client.post("Message after unarchive-reuse")
        assert ref.ts


@pytest.mark.slack
class TestReuseInvalidChannel:
    """Test _reuse_channel fallback when channel doesn't exist."""

    async def test_reuse_deleted_channel_creates_new(self, slack_harness, registry):
        """_reuse_channel with a bogus channel ID falls back to creating a new one."""
        config = _make_config(slack_harness)
        options = SessionOptions(cwd="/tmp/test", name="fallback-test", channel_id="C0000BOGUS")
        session = SummonSession(
            config=config,
            options=options,
            auth=None,
            session_id="test-fallback",
            web_client=slack_harness.client,
            dispatcher=EventDispatcher(),
            bot_user_id=await slack_harness.resolve_bot_user_id(),
        )
        channel_id, channel_name = await session._reuse_channel(
            slack_harness.client, registry, "C0000BOGUS"
        )
        # Should create a new channel (not return the bogus ID)
        assert channel_id != "C0000BOGUS"
        assert isinstance(channel_name, str)
        assert len(channel_name) > 0

        # Cleanup
        await slack_harness.cleanup_channels([channel_id])


@pytest.mark.slack
class TestHandleArchivedChannel:
    """Test _handle_archived_channel with real Slack.

    Two paths:
    1. Happy path: unarchive succeeds → original channel returned.
    2. Replacement path: unarchive fails → new channel created with -resumed
       suffix, canvas/claude_session data migrated from old channel in registry.

    Path 2 uses a targeted mock for conversations_unarchive (can't easily make
    the real API fail on a bot-owned channel) while using real Slack for
    channel creation and real SQLite for data migration.
    """

    async def test_handle_archived_unarchives_and_returns_same_channel(
        self, slack_harness, registry
    ):
        """_handle_archived_channel unarchives bot-owned channels successfully."""
        channel_id = await slack_harness.create_test_channel(prefix="handle-arch")
        info = await slack_harness.client.conversations_info(channel=channel_id)

        await registry.register_channel(
            channel_id=channel_id,
            channel_name=info["channel"]["name"],
            cwd="/tmp/test",
            authenticated_user_id="U_TEST",
        )
        await registry.update_channel_canvas(channel_id, "F_CANVAS_OLD", "# Old Canvas")
        await registry.update_channel_claude_session(channel_id, "claude-old-sid")

        await slack_harness.client.conversations_archive(channel=channel_id)

        config = _make_config(slack_harness)
        options = SessionOptions(cwd="/tmp/test", name="handle-arch-test")
        session = SummonSession(
            config=config,
            options=options,
            auth=None,
            session_id="test-handle-arch",
            web_client=slack_harness.client,
            dispatcher=EventDispatcher(),
            bot_user_id=await slack_harness.resolve_bot_user_id(),
        )

        new_id, _ = await session._handle_archived_channel(
            slack_harness.client, registry, channel_id, info
        )

        # Unarchive succeeded — same channel returned
        assert new_id == channel_id

        # Registry data intact
        channel = await registry.get_channel(channel_id)
        assert channel is not None
        assert channel["canvas_id"] == "F_CANVAS_OLD"
        assert channel["claude_session_id"] == "claude-old-sid"

        await slack_harness.cleanup_channels([channel_id])

    async def test_replacement_channel_created_when_unarchive_fails(self, slack_harness, registry):
        """When unarchive fails, a replacement channel is created and data migrated.

        Uses a targeted mock for conversations_unarchive to simulate permission
        failure, while all other Slack API calls (conversations_create,
        conversations_info) and SQLite operations are real.
        """
        from unittest.mock import AsyncMock

        # Create and archive a channel
        channel_id = await slack_harness.create_test_channel(prefix="replace")
        info = await slack_harness.client.conversations_info(channel=channel_id)
        old_name = info["channel"]["name"]

        await registry.register_channel(
            channel_id=channel_id,
            channel_name=old_name,
            cwd="/tmp/replace",
            authenticated_user_id="U_REPLACE",
        )
        await registry.update_channel_canvas(channel_id, "F_CANVAS_MIGRATE", "# Canvas To Migrate")
        await registry.update_channel_claude_session(channel_id, "claude-migrate-sid")

        await slack_harness.client.conversations_archive(channel=channel_id)

        config = _make_config(slack_harness)
        options = SessionOptions(cwd="/tmp/test", name="replace-test")
        session = SummonSession(
            config=config,
            options=options,
            auth=None,
            session_id="test-replace",
            web_client=slack_harness.client,
            dispatcher=EventDispatcher(),
            bot_user_id=await slack_harness.resolve_bot_user_id(),
        )

        # Mock ONLY conversations_unarchive to simulate permission failure.
        # Everything else (conversations_create, registry) is real.
        original_unarchive = slack_harness.client.conversations_unarchive
        slack_harness.client.conversations_unarchive = AsyncMock(
            side_effect=Exception("not_allowed — simulated permission failure")
        )
        try:
            new_id, new_name = await session._handle_archived_channel(
                slack_harness.client, registry, channel_id, info
            )
        finally:
            slack_harness.client.conversations_unarchive = original_unarchive

        # Should have created a DIFFERENT channel (replacement)
        assert new_id != channel_id

        # Private channels don't release names, so the -resumed suffix was used
        # (same-name creation fails with name_taken for private channels)
        assert "-resumed" in new_name or new_name != old_name

        # Canvas and claude_session data migrated to the new channel
        new_channel = await registry.get_channel(new_id)
        assert new_channel is not None
        assert new_channel["canvas_id"] == "F_CANVAS_MIGRATE"
        assert new_channel["canvas_markdown"] == "# Canvas To Migrate"
        assert new_channel["claude_session_id"] == "claude-migrate-sid"

        # The new channel is usable (can post messages)
        client = SlackClient(slack_harness.client, new_id)
        ref = await client.post("Message in replacement channel")
        assert ref.ts

        await slack_harness.cleanup_channels([channel_id, new_id])


class TestRegistryChannelOperations:
    """Test SessionRegistry channel CRUD with real SQLite (no Slack needed)."""

    async def test_register_channel_upsert(self, registry):
        """register_channel UPSERT updates cwd and preserves auth."""
        cid = "C_REG_UPSERT"
        await registry.register_channel(
            channel_id=cid,
            channel_name="original-name",
            cwd="/tmp/v1",
            authenticated_user_id="U_FIRST",
        )
        # UPSERT with new cwd, no auth (should preserve old auth)
        await registry.register_channel(
            channel_id=cid,
            channel_name="updated-name",
            cwd="/tmp/v2",
        )
        channel = await registry.get_channel(cid)
        assert channel is not None
        assert channel["cwd"] == "/tmp/v2"
        assert channel["channel_name"] == "updated-name"
        assert channel["authenticated_user_id"] == "U_FIRST"  # preserved

    async def test_register_channel_upsert_with_new_auth(self, registry):
        """register_channel UPSERT with new auth updates the user."""
        cid = "C_REG_NEWAUTH"
        await registry.register_channel(
            channel_id=cid,
            channel_name="auth-test",
            cwd="/tmp/test",
            authenticated_user_id="U_FIRST",
        )
        # UPSERT with a DIFFERENT user
        await registry.register_channel(
            channel_id=cid,
            channel_name="auth-test",
            cwd="/tmp/test",
            authenticated_user_id="U_SECOND",
        )
        channel = await registry.get_channel(cid)
        assert channel is not None
        assert channel["authenticated_user_id"] == "U_SECOND"

    async def test_get_channel_returns_none_for_missing(self, registry):
        channel = await registry.get_channel("C_NONEXISTENT")
        assert channel is None

    async def test_update_channel_canvas(self, registry):
        cid = "C_CANVAS_TEST"
        await registry.register_channel(
            channel_id=cid,
            channel_name="canvas-test",
            cwd="/tmp/test",
        )
        await registry.update_channel_canvas(cid, "F_CANVAS_NEW", "# Canvas Content")
        channel = await registry.get_channel(cid)
        assert channel is not None
        assert channel["canvas_id"] == "F_CANVAS_NEW"
        assert channel["canvas_markdown"] == "# Canvas Content"

    async def test_update_channel_claude_session(self, registry):
        cid = "C_CLAUDE_TEST"
        await registry.register_channel(
            channel_id=cid,
            channel_name="claude-test",
            cwd="/tmp/test",
        )
        await registry.update_channel_claude_session(cid, "claude-sid-xyz")
        channel = await registry.get_channel(cid)
        assert channel is not None
        assert channel["claude_session_id"] == "claude-sid-xyz"

    async def test_get_channel_by_name(self, registry):
        cid = "C_BYNAME_TEST"
        await registry.register_channel(
            channel_id=cid,
            channel_name="findme-channel",
            cwd="/tmp/test",
        )
        channel = await registry.get_channel_by_name("findme-channel")
        assert channel is not None
        assert channel["channel_id"] == cid

    async def test_get_latest_session_for_channel(self, registry):
        """get_latest_session_for_channel returns the most recent completed session."""
        import os

        cid = "C_LATEST_TEST"
        await registry.register_channel(
            channel_id=cid,
            channel_name="latest-test",
            cwd="/tmp/test",
        )
        # Create two sessions — use explicit ended_at to guarantee ordering
        await registry.register(
            session_id="sess-old",
            pid=os.getpid(),
            name="old-session",
            cwd="/tmp/test",
            model="claude-sonnet-4-20250514",
        )
        await registry.update_status(
            "sess-old",
            "completed",
            slack_channel_id=cid,
            ended_at="2026-01-01T00:00:00+00:00",
        )

        await registry.register(
            session_id="sess-new",
            pid=os.getpid(),
            name="new-session",
            cwd="/tmp/test",
            model="claude-sonnet-4-20250514",
        )
        await registry.update_status(
            "sess-new",
            "completed",
            slack_channel_id=cid,
            ended_at="2026-01-02T00:00:00+00:00",
        )

        latest = await registry.get_latest_session_for_channel(cid)
        assert latest is not None
        assert latest["session_id"] == "sess-new"

    async def test_get_latest_session_returns_none_for_no_sessions(self, registry):
        """get_latest_session_for_channel returns None when no completed sessions exist."""
        cid = "C_NO_SESSIONS"
        await registry.register_channel(channel_id=cid, channel_name="empty", cwd="/tmp/test")
        latest = await registry.get_latest_session_for_channel(cid)
        assert latest is None


class TestResumeValidation:
    """Test SessionManager._resolve_channel_resume with real SQLite.

    This is the critical validation pipeline that determines whether a
    resume request is valid: channel ownership, session state, and
    session-channel binding.
    """

    @staticmethod
    async def _setup_manager() -> SessionManager:
        """Create a minimal SessionManager for resume validation tests.

        Caller must set XDG_DATA_HOME before calling.
        """
        config = make_test_config(
            default_model="claude-sonnet-4-20250514",
            channel_prefix="test",
            permission_debounce_ms=10,
            max_inline_chars=2500,
        )
        return SessionManager(
            config=config,
            web_client=None,  # type: ignore[arg-type]
            bot_user_id="U_BOT",
            dispatcher=EventDispatcher(),
        )

    async def test_resolve_unknown_channel_returns_none(self, tmp_path):
        """Non-summon channels (not in channels table) return None silently."""
        import os

        os.environ["XDG_DATA_HOME"] = str(tmp_path)
        try:
            manager = await self._setup_manager()
            # Empty DB — no channels registered
            async with SessionRegistry() as reg:
                _ = reg  # ensure DB is initialized
            result = await manager._resolve_channel_resume("C_UNKNOWN", "U_SOMEONE", None)
            assert result is None
        finally:
            os.environ.pop("XDG_DATA_HOME", None)

    async def test_resolve_wrong_owner_raises(self, tmp_path):
        """Resume by a different user than the channel owner raises ValueError."""
        import os

        os.environ["XDG_DATA_HOME"] = str(tmp_path)
        try:
            manager = await self._setup_manager()
            async with SessionRegistry() as reg:
                await reg.register_channel(
                    channel_id="C_OWNED",
                    channel_name="owned-channel",
                    cwd="/tmp/test",
                    authenticated_user_id="U_OWNER",
                )
            with pytest.raises(ValueError, match="Only the original session owner"):
                await manager._resolve_channel_resume("C_OWNED", "U_INTRUDER", None)
        finally:
            os.environ.pop("XDG_DATA_HOME", None)

    async def test_resolve_no_previous_session_raises(self, tmp_path):
        """Resume on a channel with no completed sessions raises ValueError."""
        import os

        os.environ["XDG_DATA_HOME"] = str(tmp_path)
        try:
            manager = await self._setup_manager()
            async with SessionRegistry() as reg:
                await reg.register_channel(
                    channel_id="C_EMPTY",
                    channel_name="empty-channel",
                    cwd="/tmp/test",
                    authenticated_user_id="U_OWNER",
                )
            with pytest.raises(ValueError, match="No previous session found"):
                await manager._resolve_channel_resume("C_EMPTY", "U_OWNER", None)
        finally:
            os.environ.pop("XDG_DATA_HOME", None)

    async def test_resolve_active_session_explicit_id_raises(self, tmp_path):
        """Resume with explicit session_id for an active session raises ValueError."""
        import os

        os.environ["XDG_DATA_HOME"] = str(tmp_path)
        try:
            manager = await self._setup_manager()
            async with SessionRegistry() as reg:
                await reg.register_channel(
                    channel_id="C_ACTIVE",
                    channel_name="active-channel",
                    cwd="/tmp/test",
                    authenticated_user_id="U_OWNER",
                )
                await reg.register(
                    session_id="sess-active",
                    pid=os.getpid(),
                    name="active-session",
                    cwd="/tmp/test",
                    model="claude-sonnet-4-20250514",
                )
                await reg.update_status("sess-active", "active", slack_channel_id="C_ACTIVE")
            with pytest.raises(ValueError, match="still active"):
                await manager._resolve_channel_resume("C_ACTIVE", "U_OWNER", "sess-active")
        finally:
            os.environ.pop("XDG_DATA_HOME", None)

    async def test_resolve_active_session_auto_detect_raises(self, tmp_path):
        """Resume without session_id detects active session and gives correct error.

        get_latest_session_for_channel only returns completed/errored sessions.
        When no completed session exists but an active one does, the code falls
        back to get_active_session_for_channel to provide a meaningful error
        instead of the misleading "No previous session found".
        """
        import os

        os.environ["XDG_DATA_HOME"] = str(tmp_path)
        try:
            manager = await self._setup_manager()
            async with SessionRegistry() as reg:
                await reg.register_channel(
                    channel_id="C_ONLY_ACTIVE",
                    channel_name="only-active",
                    cwd="/tmp/test",
                    authenticated_user_id="U_OWNER",
                )
                await reg.register(
                    session_id="sess-only-active",
                    pid=os.getpid(),
                    name="only-active-session",
                    cwd="/tmp/test",
                    model="claude-sonnet-4-20250514",
                )
                await reg.update_status(
                    "sess-only-active",
                    "active",
                    slack_channel_id="C_ONLY_ACTIVE",
                )
            with pytest.raises(ValueError, match="still active"):
                await manager._resolve_channel_resume("C_ONLY_ACTIVE", "U_OWNER", None)
        finally:
            os.environ.pop("XDG_DATA_HOME", None)

    async def test_resolve_completed_session_returns_id(self, tmp_path):
        """Resume on a channel with a completed session returns the session ID."""
        import os

        os.environ["XDG_DATA_HOME"] = str(tmp_path)
        try:
            manager = await self._setup_manager()
            async with SessionRegistry() as reg:
                await reg.register_channel(
                    channel_id="C_DONE",
                    channel_name="done-channel",
                    cwd="/tmp/test",
                    authenticated_user_id="U_OWNER",
                )
                await reg.register(
                    session_id="sess-done",
                    pid=os.getpid(),
                    name="done-session",
                    cwd="/tmp/test",
                    model="claude-sonnet-4-20250514",
                )
                await reg.update_status(
                    "sess-done",
                    "completed",
                    slack_channel_id="C_DONE",
                    ended_at="2026-01-01T00:00:00+00:00",
                )
            result = await manager._resolve_channel_resume("C_DONE", "U_OWNER", None)
            assert result == "sess-done"
        finally:
            os.environ.pop("XDG_DATA_HOME", None)

    async def test_resolve_with_explicit_session_id(self, tmp_path):
        """Resume with explicit session_id validates session belongs to channel."""
        import os

        os.environ["XDG_DATA_HOME"] = str(tmp_path)
        try:
            manager = await self._setup_manager()
            async with SessionRegistry() as reg:
                await reg.register_channel(
                    channel_id="C_EXPLICIT",
                    channel_name="explicit-channel",
                    cwd="/tmp/test",
                    authenticated_user_id="U_OWNER",
                )
                await reg.register(
                    session_id="sess-target",
                    pid=os.getpid(),
                    name="target-session",
                    cwd="/tmp/test",
                    model="claude-sonnet-4-20250514",
                )
                await reg.update_status(
                    "sess-target",
                    "completed",
                    slack_channel_id="C_EXPLICIT",
                    ended_at="2026-01-01T00:00:00+00:00",
                )
            result = await manager._resolve_channel_resume("C_EXPLICIT", "U_OWNER", "sess-target")
            assert result == "sess-target"
        finally:
            os.environ.pop("XDG_DATA_HOME", None)

    async def test_resolve_explicit_session_wrong_channel_raises(self, tmp_path):
        """Resume with explicit session_id that belongs to a different channel raises."""
        import os

        os.environ["XDG_DATA_HOME"] = str(tmp_path)
        try:
            manager = await self._setup_manager()
            async with SessionRegistry() as reg:
                await reg.register_channel(
                    channel_id="C_THIS",
                    channel_name="this-channel",
                    cwd="/tmp/test",
                    authenticated_user_id="U_OWNER",
                )
                await reg.register(
                    session_id="sess-other",
                    pid=os.getpid(),
                    name="other-session",
                    cwd="/tmp/test",
                    model="claude-sonnet-4-20250514",
                )
                # Session belongs to a DIFFERENT channel
                await reg.update_status(
                    "sess-other",
                    "completed",
                    slack_channel_id="C_OTHER",
                    ended_at="2026-01-01T00:00:00+00:00",
                )
            with pytest.raises(ValueError, match="Session not found in this channel"):
                await manager._resolve_channel_resume("C_THIS", "U_OWNER", "sess-other")
        finally:
            os.environ.pop("XDG_DATA_HOME", None)


class TestEventDispatcherRouting:
    """Test EventDispatcher message routing with real asyncio queues (no Slack needed)."""

    async def test_dispatch_to_registered_channel(self):
        """Messages to a registered channel land in the session's queue."""
        dispatcher = EventDispatcher()
        cid = "C_DISPATCH_REG"
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)  # type: ignore[type-arg]

        from unittest.mock import MagicMock

        from summon_claude.sessions.permissions import PermissionHandler

        handle = SessionHandle(
            session_id="test-session",
            channel_id=cid,
            message_queue=queue,
            permission_handler=MagicMock(spec=PermissionHandler),
            abort_callback=lambda: None,
            authenticated_user_id="U_TEST",
        )
        dispatcher.register(cid, handle)

        event = {"channel": cid, "user": "U_TEST", "text": "hello"}
        await dispatcher.dispatch_message(event)

        assert not queue.empty()
        queued = queue.get_nowait()
        assert queued["text"] == "hello"

    async def test_dispatch_to_unregistered_channel_drops_message(self):
        """Messages to unregistered channels are silently dropped."""
        dispatcher = EventDispatcher()
        event = {"channel": "C_UNKNOWN", "user": "U_TEST", "text": "hello"}
        await dispatcher.dispatch_message(event)

    async def test_dispatch_queue_full_drops_message(self):
        """When session queue is full, message is dropped (not raised)."""
        dispatcher = EventDispatcher()
        cid = "C_QFULL"
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)  # type: ignore[type-arg]

        from unittest.mock import MagicMock

        from summon_claude.sessions.permissions import PermissionHandler

        handle = SessionHandle(
            session_id="test-session",
            channel_id=cid,
            message_queue=queue,
            permission_handler=MagicMock(spec=PermissionHandler),
            abort_callback=lambda: None,
            authenticated_user_id="U_TEST",
        )
        dispatcher.register(cid, handle)

        # Fill the queue
        queue.put_nowait({"text": "first"})

        # This should not raise — just log a warning and drop
        event = {"channel": cid, "user": "U_TEST", "text": "second"}
        await dispatcher.dispatch_message(event)
        assert queue.qsize() == 1  # only the first message

    async def test_unrouted_resume_command_triggers_handler(self):
        """!summon resume in unrouted channel triggers the resume handler."""
        dispatcher = EventDispatcher()

        resume_calls: list[tuple[str, str, str | None]] = []

        async def mock_resume(channel_id: str, user_id: str, target: str | None) -> None:
            resume_calls.append((channel_id, user_id, target))

        dispatcher.set_resume_handler(mock_resume)

        event = {
            "channel": "C_OLD_CHANNEL",
            "user": "U_OWNER",
            "text": "!summon resume",
        }
        await dispatcher.dispatch_message(event)

        assert len(resume_calls) == 1
        assert resume_calls[0] == ("C_OLD_CHANNEL", "U_OWNER", None)

    async def test_unrouted_resume_with_session_id(self):
        """!summon resume <session_id> passes target to handler."""
        dispatcher = EventDispatcher()

        resume_calls: list[tuple[str, str, str | None]] = []

        async def mock_resume(channel_id: str, user_id: str, target: str | None) -> None:
            resume_calls.append((channel_id, user_id, target))

        dispatcher.set_resume_handler(mock_resume)

        event = {
            "channel": "C_OLD",
            "user": "U_OWNER",
            "text": "!summon resume abc-123-def",
        }
        await dispatcher.dispatch_message(event)

        assert len(resume_calls) == 1
        assert resume_calls[0] == ("C_OLD", "U_OWNER", "abc-123-def")

    async def test_unrouted_non_resume_message_ignored(self):
        """Regular messages in unrouted channels don't trigger resume."""
        dispatcher = EventDispatcher()
        called = False

        async def mock_resume(channel_id: str, user_id: str, target: str | None) -> None:
            nonlocal called
            called = True

        dispatcher.set_resume_handler(mock_resume)

        event = {"channel": "C_OLD", "user": "U_SOMEONE", "text": "just chatting"}
        await dispatcher.dispatch_message(event)
        assert not called

    async def test_reaction_dispatch_to_registered_channel(self):
        """Reaction events from the session owner trigger abort callback."""
        dispatcher = EventDispatcher()
        cid = "C_REACTION_REG"
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)  # type: ignore[type-arg]
        aborted = False

        def abort():
            nonlocal aborted
            aborted = True

        from unittest.mock import MagicMock

        from summon_claude.sessions.permissions import PermissionHandler

        handle = SessionHandle(
            session_id="test-session",
            channel_id=cid,
            message_queue=queue,
            permission_handler=MagicMock(spec=PermissionHandler),
            abort_callback=abort,
            authenticated_user_id="U_OWNER",
        )
        dispatcher.register(cid, handle)

        event = {
            "user": "U_OWNER",
            "reaction": "octagonal_sign",
            "item": {"channel": cid, "ts": "123.456"},
        }
        await dispatcher.dispatch_reaction(event)
        assert aborted is True

    async def test_reaction_from_non_owner_ignored(self):
        """Reactions from non-owners are silently dropped."""
        dispatcher = EventDispatcher()
        cid = "C_REACTION_IGN"
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)  # type: ignore[type-arg]
        aborted = False

        def abort():
            nonlocal aborted
            aborted = True

        from unittest.mock import MagicMock

        from summon_claude.sessions.permissions import PermissionHandler

        handle = SessionHandle(
            session_id="test-session",
            channel_id=cid,
            message_queue=queue,
            permission_handler=MagicMock(spec=PermissionHandler),
            abort_callback=abort,
            authenticated_user_id="U_OWNER",
        )
        dispatcher.register(cid, handle)

        event = {
            "user": "U_INTRUDER",
            "reaction": "octagonal_sign",
            "item": {"channel": cid, "ts": "123.456"},
        }
        await dispatcher.dispatch_reaction(event)
        assert aborted is False

    async def test_dispatch_action_routes_to_permission_handler(self):
        """Permission button actions are routed to the session's permission handler."""
        dispatcher = EventDispatcher()
        cid = "C_ACTION_ROUTE"
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)  # type: ignore[type-arg]

        from unittest.mock import AsyncMock, MagicMock

        from summon_claude.sessions.permissions import PermissionHandler

        mock_handler = MagicMock(spec=PermissionHandler)
        mock_handler.handle_action = AsyncMock()

        handle = SessionHandle(
            session_id="test-session",
            channel_id=cid,
            message_queue=queue,
            permission_handler=mock_handler,
            abort_callback=lambda: None,
            authenticated_user_id="U_OWNER",
        )
        dispatcher.register(cid, handle)

        action = {"action_id": "permission_approve", "value": "tool_123"}
        body = {
            "channel": {"id": cid},
            "user": {"id": "U_OWNER"},
            "response_url": "https://hooks.slack.com/actions/...",
        }
        await dispatcher.dispatch_action(action, body)

        mock_handler.handle_action.assert_awaited_once_with(
            value="tool_123",
            user_id="U_OWNER",
        )

    async def test_dispatch_action_ask_user_routes_correctly(self):
        """AskUserQuestion button actions use handle_ask_user_action."""
        dispatcher = EventDispatcher()
        cid = "C_ASK_USER"
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)  # type: ignore[type-arg]

        from unittest.mock import AsyncMock, MagicMock

        from summon_claude.sessions.permissions import PermissionHandler

        mock_handler = MagicMock(spec=PermissionHandler)
        mock_handler.handle_ask_user_action = AsyncMock()

        handle = SessionHandle(
            session_id="test-session",
            channel_id=cid,
            message_queue=queue,
            permission_handler=mock_handler,
            abort_callback=lambda: None,
            authenticated_user_id="U_OWNER",
        )
        dispatcher.register(cid, handle)

        action = {"action_id": "ask_user_12345_option_a", "value": "Yes"}
        body = {
            "channel": {"id": cid},
            "user": {"id": "U_OWNER"},
            "response_url": "https://hooks.slack.com/actions/...",
        }
        await dispatcher.dispatch_action(action, body)

        mock_handler.handle_ask_user_action.assert_awaited_once_with(
            value="Yes",
            user_id="U_OWNER",
        )

    async def test_dispatch_action_unregistered_channel_ignored(self):
        """Actions for unregistered channels are silently dropped."""
        dispatcher = EventDispatcher()
        action = {"action_id": "permission_approve", "value": "tool_123"}
        body = {"channel": {"id": "C_UNKNOWN"}, "user": {"id": "U_SOMEONE"}}
        # Should not raise
        await dispatcher.dispatch_action(action, body)


class TestMessageInjection:
    """Test SummonSession.inject_message with real asyncio queues (no Slack needed)."""

    @staticmethod
    def _make_local_config() -> SummonConfig:
        return make_test_config(
            default_model="claude-sonnet-4-20250514",
            channel_prefix="test",
            permission_debounce_ms=10,
            max_inline_chars=2500,
        )

    async def test_inject_message_enqueues(self):
        """inject_message puts a _PendingTurn on the pending_turns queue."""
        config = self._make_local_config()
        options = SessionOptions(cwd="/tmp/test", name="inject-test")
        session = SummonSession(
            config=config,
            options=options,
            auth=None,
            session_id="test-inject",
            dispatcher=EventDispatcher(),
            bot_user_id="U_BOT",
        )
        result = await session.inject_message("Hello from PM", sender_info="pm-session")
        assert result is True
        assert session._pending_turns.qsize() == 1
        turn: object = session._pending_turns.get_nowait()
        assert hasattr(turn, "message")
        assert turn.message == "Hello from PM"  # type: ignore[union-attr]
        # pre_sent=False means the response consumer will call query() on this
        # turn — critical for injected messages to actually reach Claude
        assert turn.pre_sent is False  # type: ignore[union-attr]

    async def test_inject_message_rejected_when_shutdown(self):
        """inject_message returns False after shutdown_event is set."""
        config = self._make_local_config()
        options = SessionOptions(cwd="/tmp/test", name="shutdown-test")
        session = SummonSession(
            config=config,
            options=options,
            auth=None,
            session_id="test-shutdown",
            dispatcher=EventDispatcher(),
            bot_user_id="U_BOT",
        )
        session._shutdown_event.set()
        result = await session.inject_message("Should be rejected")
        assert result is False
        assert session._pending_turns.empty()

    async def test_inject_message_backpressure(self):
        """inject_message returns False when the queue is full."""
        config = self._make_local_config()
        options = SessionOptions(cwd="/tmp/test", name="backpressure-test")
        session = SummonSession(
            config=config,
            options=options,
            auth=None,
            session_id="test-backpressure",
            dispatcher=EventDispatcher(),
            bot_user_id="U_BOT",
        )
        # Fill the queue to capacity
        from summon_claude.sessions.session import _MAX_PENDING_TURNS, _PendingTurn

        for i in range(_MAX_PENDING_TURNS):
            session._pending_turns.put_nowait(_PendingTurn(message=f"msg-{i}", pre_sent=False))

        result = await session.inject_message("overflow message")
        assert result is False


class TestChannelNameGeneration:
    """Test channel name generation helper."""

    def test_make_channel_name_format(self):
        """_make_channel_name produces valid Slack channel names."""
        name = _make_channel_name("summon", "my-feature")
        assert name.startswith("summon-my-feature-")
        assert len(name) <= 80
        assert name == name.lower()

    def test_make_channel_name_slugifies_special_chars(self):
        name = _make_channel_name("summon", "Feature With Spaces!")
        assert " " not in name
        assert "!" not in name

    def test_make_channel_name_truncates_long_names(self):
        name = _make_channel_name("summon", "a" * 100)
        assert len(name) <= 80
