"""Context window usage tracking."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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


_TAIL_BYTES = 65536  # 64KB tail read for efficient transcript parsing


def derive_transcript_path(cwd: str, session_id: str) -> Path:
    """Derive the JSONL transcript path from cwd and Claude session ID.

    Claude Code stores transcripts at:
    ``~/.claude/projects/{path_hash}/sessions/{session_id}.jsonl``
    where ``path_hash`` is the SHA-256 hex digest of the absolute cwd path.
    """
    path_hash = hashlib.sha256(cwd.encode()).hexdigest()
    return Path.home() / ".claude" / "projects" / path_hash / "sessions" / f"{session_id}.jsonl"


def get_last_step_usage(transcript_path: Path) -> dict[str, Any] | None:
    """Read the last API step's usage from the JSONL transcript.

    Performs an efficient tail read (last 64KB) to avoid reading the
    entire file, which can be several MB for long sessions.

    Skips entries with ``parentToolUseId`` (subagent steps) and returns
    the last top-level step's ``usage`` dict.
    """
    if not transcript_path.is_file():
        return None
    try:
        with transcript_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            if f.tell() > 0:
                f.readline()  # Skip partial first line
            last_usage = None
            for line in f:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if entry.get("parentToolUseId"):
                    continue
                usage = entry.get("message", {}).get("usage")
                if usage and usage.get("input_tokens") is not None:
                    last_usage = usage
        return last_usage
    except Exception as e:
        logger.debug("Failed to read transcript %s: %s", transcript_path, e)
        return None
