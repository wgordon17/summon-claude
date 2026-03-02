"""Shared test helpers for summon-claude tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

from summon_claude.slack.client import MessageRef, SlackClient


def make_mock_slack_client():
    """Create a mocked SlackClient with standard return values."""
    client = AsyncMock(spec=SlackClient)
    client.post = AsyncMock(return_value=MessageRef(channel_id="C123", ts="1234567890.123456"))
    client.update = AsyncMock()
    client.react = AsyncMock()
    client.upload = AsyncMock()
    client.set_topic = AsyncMock()
    client.post_ephemeral = AsyncMock()
    client.channel_id = "C123"
    return client
