"""McpHealthTracker — tracks MCP tool failures per provider with degradation notifications."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

__all__ = ["McpHealthTracker"]

logger = logging.getLogger(__name__)

_PROVIDER_PREFIXES: dict[str, str] = {
    "mcp__jira__": "summon auth jira login",
    "mcp__workspace__": "summon auth google login",
    "mcp__github__": "summon auth github login",
}

_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "mcp__jira__": "Jira",
    "mcp__workspace__": "Google Workspace",
    "mcp__github__": "GitHub",
}

# Patterns that indicate auth-specific errors — trigger immediate degradation.
# Bare "unauthorized"/"unauthenticated" deliberately excluded to avoid false
# positives from Jira issue content (e.g., "unauthorized access reported").
_AUTH_ERROR_PATTERNS_STRICT: tuple[str, ...] = (
    "http 401",
    "http 403",
    "status 401",
    "status 403",
    "invalid_grant",
    "token expired",
    "token_expired",
)

_CONSECUTIVE_FAILURE_THRESHOLD = 3
_COOLDOWN_SECONDS = 1800  # 30 minutes


class McpHealthTracker:
    """Tracks MCP tool failures per provider and fires a callback on degradation."""

    def __init__(self, on_degraded: Callable[[str, str], Awaitable[None]]) -> None:
        self._on_degraded = on_degraded
        self._failures: dict[str, int] = {}
        self._last_notified: dict[str, float] = {}
        self._degraded: set[str] = set()

    async def record_tool_result(
        self,
        tool_name: str,
        is_error: bool | None,
        error_content: str | None = None,
    ) -> None:
        """Record a tool result and fire on_degraded when threshold is met.

        Args:
            tool_name: Full MCP tool name (e.g., ``mcp__jira__getJiraIssue``).
            is_error: ``ToolResultBlock.is_error`` (``bool | None`` — None = non-error).
            error_content: Truncated error text for auth pattern matching only.
                SEC-HEALTH-01: never logged or surfaced — pattern matching only.
        """
        # Determine provider prefix
        prefix = None
        for p in _PROVIDER_PREFIXES:
            if tool_name.startswith(p):
                prefix = p
                break
        if prefix is None:
            return  # Not an MCP tool or unknown provider

        # Success resets counter (is_error=None treated as non-error)
        if not is_error:
            self._failures[prefix] = 0
            if prefix in self._degraded:
                self._degraded.discard(prefix)
                display = _PROVIDER_DISPLAY_NAMES.get(prefix, prefix)
                logger.info("%s tools recovered", display)
            return

        # Error — increment counter
        self._failures[prefix] = self._failures.get(prefix, 0) + 1
        count = self._failures[prefix]

        # Check for auth-specific errors
        is_auth_error = False
        if error_content:
            lower = error_content.lower()
            is_auth_error = any(pat in lower for pat in _AUTH_ERROR_PATTERNS_STRICT)

        # Determine threshold
        threshold = 1 if is_auth_error else _CONSECUTIVE_FAILURE_THRESHOLD
        if count < threshold:
            return

        # Check cooldown
        now = time.time()
        last = self._last_notified.get(prefix, 0)
        if now - last < _COOLDOWN_SECONDS:
            return

        # Fire notification
        self._degraded.add(prefix)
        self._last_notified[prefix] = now
        display = _PROVIDER_DISPLAY_NAMES.get(prefix, prefix)
        reauth_cmd = _PROVIDER_PREFIXES[prefix]
        reason = "authentication error" if is_auth_error else "repeated errors"
        message = f"{display} tools are failing ({reason}). To restore access, run: {reauth_cmd}"

        try:
            await self._on_degraded(prefix, message)
        except Exception:
            logger.warning("McpHealthTracker: on_degraded callback failed", exc_info=True)
