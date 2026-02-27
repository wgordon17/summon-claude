"""Shared test helpers for summon-claude tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

from summon_claude.providers.base import ChannelRef, ChatProvider, MessageRef


def make_mock_provider():
    """Create a mocked ChatProvider with standard return values."""
    provider = AsyncMock(spec=ChatProvider)
    provider.post_message = AsyncMock(
        return_value=MessageRef(channel_id="C123", ts="1234567890.123456")
    )
    provider.update_message = AsyncMock()
    provider.add_reaction = AsyncMock()
    provider.upload_file = AsyncMock()
    provider.create_channel = AsyncMock(
        return_value=ChannelRef(channel_id="C_NEW", name="test-channel")
    )
    provider.invite_user = AsyncMock()
    provider.archive_channel = AsyncMock()
    provider.set_topic = AsyncMock()
    provider.post_ephemeral = AsyncMock()
    return provider
