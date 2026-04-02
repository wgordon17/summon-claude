"""Tests for summon_claude.sessions.mcp_health — McpHealthTracker."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from summon_claude.sessions.mcp_health import (
    _AUTH_ERROR_PATTERNS_STRICT,
    _CONSECUTIVE_FAILURE_THRESHOLD,
    _COOLDOWN_SECONDS,
    _PROVIDER_DISPLAY_NAMES,
    _PROVIDER_PREFIXES,
    McpHealthTracker,
)


@pytest.fixture
def callback() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def tracker(callback: AsyncMock) -> McpHealthTracker:
    return McpHealthTracker(on_degraded=callback)


class TestTrackerInit:
    def test_empty_state(self, tracker: McpHealthTracker) -> None:
        assert tracker._failures == {}
        assert tracker._last_notified == {}
        assert tracker._degraded == set()

    def test_constants(self) -> None:
        assert _CONSECUTIVE_FAILURE_THRESHOLD == 3
        assert _COOLDOWN_SECONDS == 1800
        assert "mcp__jira__" in _PROVIDER_PREFIXES
        assert "mcp__workspace__" in _PROVIDER_PREFIXES
        assert "mcp__github__" in _PROVIDER_PREFIXES


class TestRecordToolResult:
    @pytest.mark.asyncio
    async def test_success_resets_counter(
        self, tracker: McpHealthTracker, callback: AsyncMock
    ) -> None:
        # Build up 2 failures
        await tracker.record_tool_result("mcp__jira__getIssue", is_error=True)
        await tracker.record_tool_result("mcp__jira__getIssue", is_error=True)
        assert tracker._failures["mcp__jira__"] == 2

        # Success resets
        await tracker.record_tool_result("mcp__jira__getIssue", is_error=False)
        assert tracker._failures["mcp__jira__"] == 0
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_is_error_resets_counter(
        self, tracker: McpHealthTracker, callback: AsyncMock
    ) -> None:
        await tracker.record_tool_result("mcp__jira__getIssue", is_error=True)
        await tracker.record_tool_result("mcp__jira__getIssue", is_error=None)
        assert tracker._failures["mcp__jira__"] == 0

    @pytest.mark.asyncio
    async def test_three_consecutive_failures_trigger_notification(
        self, tracker: McpHealthTracker, callback: AsyncMock
    ) -> None:
        for _ in range(_CONSECUTIVE_FAILURE_THRESHOLD):
            await tracker.record_tool_result("mcp__jira__getIssue", is_error=True)
        callback.assert_called_once()
        prefix, message = callback.call_args[0]
        assert prefix == "mcp__jira__"
        assert "Jira" in message
        assert "repeated errors" in message
        assert "summon auth jira login" in message

    @pytest.mark.asyncio
    async def test_auth_error_triggers_on_first_failure(
        self, tracker: McpHealthTracker, callback: AsyncMock
    ) -> None:
        await tracker.record_tool_result(
            "mcp__jira__getIssue",
            is_error=True,
            error_content="Failed: HTTP 401 Unauthorized",
        )
        callback.assert_called_once()
        _, message = callback.call_args[0]
        assert "authentication error" in message

    @pytest.mark.asyncio
    async def test_bare_unauthorized_uses_threshold(
        self, tracker: McpHealthTracker, callback: AsyncMock
    ) -> None:
        # "unauthorized" alone should NOT trigger immediate notification
        await tracker.record_tool_result(
            "mcp__jira__getIssue",
            is_error=True,
            error_content="unauthorized access reported by security team",
        )
        callback.assert_not_called()
        # Need 3 total to trigger
        await tracker.record_tool_result(
            "mcp__jira__getIssue",
            is_error=True,
            error_content="unauthorized access",
        )
        callback.assert_not_called()
        await tracker.record_tool_result(
            "mcp__jira__getIssue",
            is_error=True,
            error_content="unauthorized access",
        )
        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_duplicate(
        self, tracker: McpHealthTracker, callback: AsyncMock
    ) -> None:
        # Trigger first notification
        for _ in range(_CONSECUTIVE_FAILURE_THRESHOLD):
            await tracker.record_tool_result("mcp__jira__getIssue", is_error=True)
        assert callback.call_count == 1

        # More failures within cooldown — no new notification
        for _ in range(_CONSECUTIVE_FAILURE_THRESHOLD):
            await tracker.record_tool_result("mcp__jira__getIssue", is_error=True)
        assert callback.call_count == 1

    @pytest.mark.asyncio
    async def test_success_clears_degraded_state(
        self, tracker: McpHealthTracker, callback: AsyncMock
    ) -> None:
        for _ in range(_CONSECUTIVE_FAILURE_THRESHOLD):
            await tracker.record_tool_result("mcp__jira__getIssue", is_error=True)
        assert "mcp__jira__" in tracker._degraded

        await tracker.record_tool_result("mcp__jira__getIssue", is_error=False)
        assert "mcp__jira__" not in tracker._degraded

    @pytest.mark.asyncio
    async def test_unknown_tool_prefix_ignored(
        self, tracker: McpHealthTracker, callback: AsyncMock
    ) -> None:
        await tracker.record_tool_result("Read", is_error=True)
        assert tracker._failures == {}
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_callback_exception_caught(
        self, tracker: McpHealthTracker, callback: AsyncMock
    ) -> None:
        callback.side_effect = RuntimeError("boom")
        # Should not raise
        for _ in range(_CONSECUTIVE_FAILURE_THRESHOLD):
            await tracker.record_tool_result("mcp__jira__getIssue", is_error=True)

    @pytest.mark.asyncio
    async def test_mixed_providers_independent(
        self, tracker: McpHealthTracker, callback: AsyncMock
    ) -> None:
        # Fail Jira twice — below threshold
        await tracker.record_tool_result("mcp__jira__getIssue", is_error=True)
        await tracker.record_tool_result("mcp__jira__getIssue", is_error=True)
        # Succeed on GitHub — should not affect Jira counter
        await tracker.record_tool_result("mcp__github__list_issues", is_error=False)
        assert tracker._failures.get("mcp__jira__") == 2
        assert tracker._failures.get("mcp__github__") == 0

    @pytest.mark.asyncio
    async def test_message_format(self, tracker: McpHealthTracker, callback: AsyncMock) -> None:
        for _ in range(_CONSECUTIVE_FAILURE_THRESHOLD):
            await tracker.record_tool_result("mcp__github__list_issues", is_error=True)
        _, message = callback.call_args[0]
        assert "GitHub" in message
        assert "summon auth github login" in message

    @pytest.mark.asyncio
    async def test_proj_401_not_auth_error(
        self, tracker: McpHealthTracker, callback: AsyncMock
    ) -> None:
        """'PROJ-401' in issue content should NOT trigger auth detection."""
        await tracker.record_tool_result(
            "mcp__jira__getIssue",
            is_error=True,
            error_content="Issue PROJ-401 not found",
        )
        callback.assert_not_called()  # Not an auth error pattern match

    @pytest.mark.asyncio
    async def test_cooldown_expires_allows_renotification(
        self, tracker: McpHealthTracker, callback: AsyncMock
    ) -> None:
        # Trigger first notification
        for _ in range(_CONSECUTIVE_FAILURE_THRESHOLD):
            await tracker.record_tool_result("mcp__jira__getIssue", is_error=True)
        assert callback.call_count == 1

        # Fast-forward past cooldown
        tracker._last_notified["mcp__jira__"] = time.time() - _COOLDOWN_SECONDS - 1
        for _ in range(_CONSECUTIVE_FAILURE_THRESHOLD):
            await tracker.record_tool_result("mcp__jira__getIssue", is_error=True)
        assert callback.call_count == 2
