"""Integration tests for channel operations against real Slack API.

Channel setup (create, invite, set_topic) is exercised transitively by the
shared test_channel fixture. Tests here focus on topic management via
SlackClient and session header posting.
"""

from __future__ import annotations

import pytest

from summon_claude.sessions.session import _format_topic

pytestmark = [pytest.mark.slack]


class TestTopicManagement:
    """Test channel topic operations via SlackClient."""

    async def test_set_topic_via_slack_client(self, slack_client, test_channel, slack_harness):
        topic = _format_topic(
            model="claude-sonnet-4-20250514",
            cwd="/tmp/test",
            git_branch="main",
        )
        await slack_client.set_topic(topic)
        info = await slack_harness.client.conversations_info(channel=test_channel)
        actual_topic = info["channel"]["topic"]["value"]
        assert "sonnet" in actual_topic
        assert "main" in actual_topic

    async def test_set_topic_plain_text(self, slack_client, test_channel, slack_harness):
        await slack_client.set_topic("Updated topic")
        info = await slack_harness.client.conversations_info(channel=test_channel)
        assert info["channel"]["topic"]["value"] == "Updated topic"
