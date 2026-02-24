"""Simple per-key cooldown rate limiter.

Algorithm: in-memory dict mapping key -> last_allowed_time. A key is allowed
if ``now - last_time >= cooldown_seconds``. The map is periodically compacted
(every 100 calls) to prevent unbounded growth.

Characteristics:
- Per-process only -- not shared across processes or persisted
- No locking -- safe for single-threaded asyncio (no awaits in check())
- O(1) per check, O(n) cleanup every _CLEANUP_EVERY calls
"""

from __future__ import annotations

import time


class RateLimiter:
    """Simple per-key rate limiter with automatic cleanup.

    Safe for single-threaded asyncio (no await inside check/cleanup).
    """

    _CLEANUP_EVERY = 100

    def __init__(self, cooldown_seconds: float = 2.0):
        self._cooldown = cooldown_seconds
        self._last_attempt: dict[str, float] = {}
        self._call_count = 0

    def check(self, key: str) -> bool:
        """Return True if the request is allowed."""
        now = time.monotonic()
        # Periodic cleanup to prevent unbounded growth
        self._call_count += 1
        if self._call_count >= self._CLEANUP_EVERY:
            self._call_count = 0
            self._cleanup()
        last = self._last_attempt.get(key, 0.0)
        if now - last < self._cooldown:
            return False
        self._last_attempt[key] = now
        return True

    def _cleanup(self, max_age: float = 300.0) -> None:
        """Remove entries older than max_age."""
        now = time.monotonic()
        self._last_attempt = {k: v for k, v in self._last_attempt.items() if now - v < max_age}
