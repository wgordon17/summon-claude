"""Tests for summon_claude.context — context window usage tracking."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from summon_claude.sessions.context import (
    ContextUsage,
    get_sdk_context_usage,
)


class TestContextUsageDataclass:
    def test_context_usage_dataclass_is_frozen(self):
        """ContextUsage should be immutable."""
        ctx = ContextUsage(input_tokens=50000, context_window=200000, percentage=25.0)
        with pytest.raises(AttributeError):
            ctx.input_tokens = 60000

    def test_context_usage_has_correct_attributes(self):
        """ContextUsage should have input_tokens, context_window, percentage."""
        ctx = ContextUsage(input_tokens=50000, context_window=200000, percentage=25.0)
        assert ctx.input_tokens == 50000
        assert ctx.context_window == 200000
        assert ctx.percentage == pytest.approx(25.0)


class TestGetSdkContextUsage:
    async def test_maps_sdk_response_to_context_usage(self):
        """get_sdk_context_usage maps SDK response fields to ContextUsage."""
        client = MagicMock()
        client.get_context_usage = AsyncMock(
            return_value={
                "totalTokens": 50000,
                "maxTokens": 200000,
                "percentage": 25.0,
            }
        )
        result = await get_sdk_context_usage(client)
        assert result is not None
        assert isinstance(result, ContextUsage)
        assert result.input_tokens == 50000
        assert result.context_window == 200000
        assert result.percentage == pytest.approx(25.0)

    async def test_returns_none_on_exception(self):
        """get_sdk_context_usage returns None when SDK raises an error."""
        client = MagicMock()
        client.get_context_usage = AsyncMock(side_effect=RuntimeError("connection lost"))
        result = await get_sdk_context_usage(client)
        assert result is None

    async def test_returns_none_on_missing_keys(self):
        """get_sdk_context_usage returns None when response lacks required keys."""
        client = MagicMock()
        client.get_context_usage = AsyncMock(
            return_value={"totalTokens": 50000}  # missing maxTokens and percentage
        )
        result = await get_sdk_context_usage(client)
        assert result is None

    async def test_handles_1m_context_window(self):
        """get_sdk_context_usage correctly maps 1M context window values."""
        client = MagicMock()
        client.get_context_usage = AsyncMock(
            return_value={
                "totalTokens": 500000,
                "maxTokens": 1000000,
                "percentage": 50.0,
            }
        )
        result = await get_sdk_context_usage(client)
        assert result is not None
        assert result.context_window == 1000000
        assert result.percentage == pytest.approx(50.0)

    async def test_handles_zero_tokens(self):
        """get_sdk_context_usage handles zero token count."""
        client = MagicMock()
        client.get_context_usage = AsyncMock(
            return_value={
                "totalTokens": 0,
                "maxTokens": 200000,
                "percentage": 0.0,
            }
        )
        result = await get_sdk_context_usage(client)
        assert result is not None
        assert result.input_tokens == 0
        assert result.percentage == pytest.approx(0.0)
