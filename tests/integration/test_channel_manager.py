"""Integration tests for ChannelManager against real Slack API.

Channel lifecycle (create, invite, topic, archive) is exercised
transitively by the shared test_channel fixture. Name collision
and slugify logic are covered by unit tests. Tests here focus on
ChannelManager operations that require real Slack verification.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.slack]


class TestChannelLifecycle:
    """Test ChannelManager operations against real Slack."""

    async def test_post_session_header(self, channel_manager, test_channel, slack_harness):
        session_info = {
            "cwd": "/tmp/test-project",
            "model": "claude-sonnet-4-20250514",
            "session_id": "abc123def456",
        }
        ts = await channel_manager.post_session_header(test_channel, session_info)
        assert ts
        history = await slack_harness.client.conversations_history(channel=test_channel, limit=5)
        assert any(m["ts"] == ts for m in history["messages"])


class TestTopicManagement:
    """Test ChannelManager topic operations."""

    async def test_set_session_topic(self, channel_manager, test_channel, slack_harness):
        await channel_manager.set_session_topic(
            test_channel,
            model="claude-sonnet-4-20250514",
            cwd="/tmp/test",
            git_branch="main",
            context=None,
        )
        info = await slack_harness.client.conversations_info(channel=test_channel)
        topic = info["channel"]["topic"]["value"]
        assert "sonnet" in topic
        assert "main" in topic

    async def test_update_topic(self, channel_manager, test_channel, slack_harness):
        await channel_manager.update_topic(test_channel, "Updated topic")
        info = await slack_harness.client.conversations_info(channel=test_channel)
        assert info["channel"]["topic"]["value"] == "Updated topic"
