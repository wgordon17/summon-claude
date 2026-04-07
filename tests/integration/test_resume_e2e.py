"""End-to-end smoke test for the full session resume pipeline.

Exercises every Slack API call in the resume path with real Slack +
real SQLite, simulating the complete lifecycle:

  1. Create a session channel (conversations_create)
  2. Post messages and set topic (chat_postMessage, conversations_setTopic)
  3. Create a canvas (canvases.create) and populate with content
  4. Register everything in SQLite (channels, sessions tables)
  5. Mark session completed and archive the channel
  6. Resume: _reuse_channel → unarchive → CanvasStore.restore → post resume banner
  7. Verify: channel active, canvas restored, topic updated, messages work

The only piece NOT exercised is Claude SDK subprocess reconnection
(--resume flag with claude_session_id). Everything else — every Slack
API call, every SQLite operation, every channel state transition — is real.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from summon_claude.config import SummonConfig
from summon_claude.event_dispatcher import EventDispatcher
from summon_claude.sessions.registry import SessionRegistry
from summon_claude.sessions.session import (
    SessionOptions,
    SummonSession,
    _format_topic,
)
from summon_claude.slack.canvas_store import CanvasStore
from summon_claude.slack.client import SlackClient

if TYPE_CHECKING:
    from tests.integration.conftest import SlackTestHarness

pytestmark = [pytest.mark.slack]


def _config(harness: SlackTestHarness) -> SummonConfig:
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


class TestResumeEndToEnd:
    """Full resume pipeline smoke test against real Slack + real SQLite."""

    @staticmethod
    async def _create_original_session(
        slack_harness: SlackTestHarness,
        registry: SessionRegistry,
        bot_user_id: str,
    ) -> tuple[str, str, str | None]:
        """Phase 1: Create channel, post messages, create canvas, register in DB.

        Returns (channel_id, channel_name, canvas_id).
        """
        channel_id = await slack_harness.create_test_channel(prefix="e2e-resume")
        info = await slack_harness.client.conversations_info(channel=channel_id)
        channel_name: str = info["channel"]["name"]

        await registry.register_channel(
            channel_id=channel_id,
            channel_name=channel_name,
            cwd="/tmp/e2e-test",
            authenticated_user_id=bot_user_id,
        )
        await registry.register(
            session_id="e2e-original",
            pid=os.getpid(),
            name="e2e-test",
            cwd="/tmp/e2e-test",
            model="claude-sonnet-4-20250514",
        )
        await registry.update_status(
            "e2e-original",
            "active",
            slack_channel_id=channel_id,
            slack_channel_name=channel_name,
            authenticated_user_id=bot_user_id,
        )

        client = SlackClient(slack_harness.client, channel_id)
        header = await client.post(
            ":robot_face: *Session started*\n"
            "Model: `claude-sonnet-4-20250514` | CWD: `/tmp/e2e-test`"
        )
        assert header.ts
        msg = await client.post("User: Can you help me with this project?")
        assert msg.ts

        topic = _format_topic(
            model="claude-sonnet-4-20250514", cwd="/tmp/e2e-test", git_branch="main"
        )
        await client.set_topic(topic)

        canvas_md = "# E2E Test Session\n\n## Status\n\nActive\n\n## Notes\n\nImportant context"
        canvas_id = await client.canvas_create(canvas_md, title="E2E Test Canvas")
        if canvas_id:
            await registry.update_channel_canvas(channel_id, canvas_id, canvas_md)

        await registry.update_channel_claude_session(channel_id, "claude-e2e-sid-abc123")
        return channel_id, channel_name, canvas_id

    @staticmethod
    async def _end_and_archive(
        slack_harness: SlackTestHarness,
        registry: SessionRegistry,
        channel_id: str,
    ) -> None:
        """Phase 2: Complete session and archive channel."""
        await registry.update_status(
            "e2e-original", "completed", ended_at="2026-03-20T12:00:00+00:00"
        )
        await slack_harness.client.conversations_archive(channel=channel_id)
        info = await slack_harness.client.conversations_info(channel=channel_id)
        assert info["channel"]["is_archived"] is True

    @staticmethod
    async def _resume_and_verify(  # noqa: PLR0913
        slack_harness: SlackTestHarness,
        registry: SessionRegistry,
        config: SummonConfig,
        bot_user_id: str,
        channel_id: str,
        canvas_id: str | None,
    ) -> None:
        """Phase 3+4: Resume session, restore canvas, verify everything works."""
        # Validate pre-resume registry state
        channel_data = await registry.get_channel(channel_id)
        assert channel_data is not None
        assert channel_data["claude_session_id"] == "claude-e2e-sid-abc123"

        # Create resumed session and reuse channel
        options = SessionOptions(
            cwd="/tmp/e2e-test",
            name="e2e-resumed",
            channel_id=channel_id,
            resume="claude-e2e-sid-abc123",
        )
        session = SummonSession(
            config=config,
            options=options,
            auth=None,
            session_id="e2e-resumed",
            web_client=slack_harness.client,
            dispatcher=EventDispatcher(),
            bot_user_id=bot_user_id,
        )
        reused_id, reused_name = await session._reuse_channel(
            slack_harness.client, registry, channel_id
        )
        assert reused_id == channel_id

        info = await slack_harness.client.conversations_info(channel=channel_id)
        assert info["channel"]["is_archived"] is False

        await registry.register_channel(
            channel_id=reused_id,
            channel_name=reused_name,
            cwd="/tmp/e2e-test",
            authenticated_user_id=bot_user_id,
        )

        # Restore canvas and verify content
        client = SlackClient(slack_harness.client, reused_id)
        if canvas_id:
            store = await CanvasStore.restore(
                session_id="e2e-resumed",
                client=client,
                registry=registry,
                channel_id=reused_id,
            )
            assert store is not None
            assert store.canvas_id == canvas_id
            assert "Important context" in store.markdown
            await store.update_section("Status", "Resumed")
            assert "Resumed" in store.read()
            assert "Important context" in store.read()

        # Post resume banner and update topic
        resume_ref = await client.post(
            ":arrows_counterclockwise: Session resumed \u2014 continuing previous conversation."
        )
        assert resume_ref.ts

        new_topic = _format_topic(
            model="claude-sonnet-4-20250514",
            cwd="/tmp/e2e-test",
            git_branch="feature/e2e",
        )
        await client.set_topic(new_topic)

        info_final = await slack_harness.client.conversations_info(channel=channel_id)
        assert "feature/e2e" in info_final["channel"]["topic"]["value"]

        new_msg = await client.post("User: Let's continue where we left off.")
        assert new_msg.ts

        # Verify message history spans both sessions
        history = await slack_harness.client.conversations_history(channel=channel_id, limit=10)
        texts = [m.get("text", "") for m in history["messages"]]
        assert any("Session started" in t for t in texts)
        assert any("continue where we left off" in t for t in texts)
        assert any("Session resumed" in t for t in texts)

        # Verify final registry state
        final = await registry.get_channel(channel_id)
        assert final is not None
        assert final["authenticated_user_id"] == bot_user_id
        assert final["claude_session_id"] == "claude-e2e-sid-abc123"
        if canvas_id:
            assert "Resumed" in (final["canvas_markdown"] or "")

    async def test_full_resume_lifecycle(self, slack_harness, registry):
        """Simulate a complete session → archive → resume cycle.

        Exercises every Slack API call and every SQLite operation in
        the resume path, in the exact order they happen in production.
        """
        bot_user_id = await slack_harness.resolve_bot_user_id()
        config = _config(slack_harness)

        channel_id, _, canvas_id = await self._create_original_session(
            slack_harness, registry, bot_user_id
        )
        await self._end_and_archive(slack_harness, registry, channel_id)
        await self._resume_and_verify(
            slack_harness, registry, config, bot_user_id, channel_id, canvas_id
        )

        await slack_harness.cleanup_channels([channel_id])
