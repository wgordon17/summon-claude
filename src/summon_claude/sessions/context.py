"""Context window usage tracking."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContextUsage:
    """Snapshot of context window consumption for a single turn."""

    input_tokens: int
    context_window: int
    percentage: float  # 0-100


async def get_sdk_context_usage(client: ClaudeSDKClient) -> ContextUsage | None:
    """Get context usage from the SDK's get_context_usage() method.

    Returns None on any error (network, method not available, etc.).
    Must be called while the client is still connected (inside the
    ``async with ClaudeSDKClient()`` block).
    """
    try:
        usage = await client.get_context_usage()
        total_tokens = usage.get("totalTokens")
        max_tokens = usage.get("maxTokens")
        percentage = usage.get("percentage")
        if total_tokens is None or max_tokens is None or percentage is None:
            logger.debug("get_context_usage() returned incomplete data: %s", usage)
            return None
        return ContextUsage(
            input_tokens=total_tokens,
            context_window=max_tokens,
            percentage=percentage,
        )
    except Exception as e:
        logger.debug("get_context_usage() failed: %s", e)
        return None
