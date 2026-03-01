"""Context window usage tracking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_1M = 1_000_000
_200K = 200_000

# Models that support a 1M context window (via beta header or [1m] suffix).
_1M_CAPABLE_PREFIXES: tuple[str, ...] = (
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-sonnet-4-0",
    "claude-sonnet-4-2",  # future-proof dated snapshots
)

# Prefix -> default (non-1M) context window.  Order matters: more specific
# prefixes must come before shorter ones so startswith matching works.
CONTEXT_WINDOW_SIZES: dict[str, int] = {
    # Current generation
    "claude-opus-4-6": _200K,
    "claude-sonnet-4-6": _200K,
    "claude-haiku-4-5": _200K,
    # Previous generation
    "claude-sonnet-4-5": _200K,
    "claude-opus-4-5": _200K,
    "claude-opus-4-1": _200K,
    "claude-sonnet-4-0": _200K,
    "claude-opus-4-0": _200K,
    # Catch-all for claude-4 family (e.g. claude-sonnet-4, claude-opus-4)
    "claude-opus-4": _200K,
    "claude-sonnet-4": _200K,
    "claude-haiku-4": _200K,
    # Claude 3.x family
    "claude-3-7-sonnet": _200K,
    "claude-3-5-sonnet": _200K,
    "claude-3-5-haiku": _200K,
    "claude-3-opus": _200K,
    "claude-3-sonnet": _200K,
    "claude-3-haiku": _200K,
}

DEFAULT_CONTEXT_WINDOW = _200K


@dataclass(frozen=True, slots=True)
class ContextUsage:
    """Snapshot of context window consumption for a single turn."""

    input_tokens: int
    context_window: int
    percentage: float  # 0-100


def compute_context_usage(usage: dict[str, Any] | None, model: str | None) -> ContextUsage | None:
    """Compute context usage from a ResultMessage usage dict.

    Returns None if usage is missing or has no input_tokens.
    """
    if not usage:
        return None
    input_tokens = usage.get("input_tokens")
    if input_tokens is None:
        return None
    total = (
        input_tokens
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )
    context_window = DEFAULT_CONTEXT_WINDOW
    if model:
        # Claude Code appends "[1m]" to the model ID when 1M context is active.
        is_1m = "[1m]" in model
        base_model = model.replace("[1m]", "")
        for prefix, size in CONTEXT_WINDOW_SIZES.items():
            if base_model.startswith(prefix):
                context_window = size
                break
        if is_1m and any(base_model.startswith(p) for p in _1M_CAPABLE_PREFIXES):
            context_window = _1M
    pct = (total / context_window) * 100 if context_window > 0 else 0.0
    return ContextUsage(input_tokens=total, context_window=context_window, percentage=pct)
