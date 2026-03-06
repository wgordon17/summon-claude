"""Tests for session authentication UX (BUG-026, BUG-030)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

from summon_claude.sessions.session import SummonSession


def _capture_session_logs() -> tuple[logging.Handler, list[str]]:
    """Return a (handler, messages) pair; caller must remove the handler."""
    messages: list[str] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _CapturingHandler()
    session_logger = logging.getLogger("summon_claude.sessions.session")
    session_logger.addHandler(handler)
    session_logger.setLevel(logging.DEBUG)
    return handler, messages


class TestAuthCountdown:
    """BUG-030: Auth countdown should log at periodic intervals."""

    async def test_countdown_uses_logger(self):
        """_wait_for_auth should call logger.info for countdown messages."""
        session = object.__new__(SummonSession)
        session._authenticated_event = asyncio.Event()
        session._shutdown_event = asyncio.Event()

        handler, log_messages = _capture_session_logs()
        try:
            # Short timeout + tiny countdown interval → fast countdown messages
            with (
                patch("summon_claude.sessions.session._AUTH_TIMEOUT_S", 0.15),
                patch("summon_claude.sessions.session._AUTH_COUNTDOWN_INTERVAL_S", 0.03),
            ):
                result = await session._wait_for_auth()
        finally:
            session_logger = logging.getLogger("summon_claude.sessions.session")
            session_logger.removeHandler(handler)
            session_logger.setLevel(logging.NOTSET)

        assert result == "timed_out"
        countdown_msgs = [m for m in log_messages if "remaining" in m]
        assert len(countdown_msgs) >= 1, f"Expected countdown messages, got: {log_messages}"
        assert "remaining" in countdown_msgs[0]

    async def test_countdown_interval(self):
        """Multiple countdown messages should appear at configured intervals."""
        session = object.__new__(SummonSession)
        session._authenticated_event = asyncio.Event()
        session._shutdown_event = asyncio.Event()

        handler, log_messages = _capture_session_logs()
        try:
            # With 0.2s timeout and 0.04s interval, expect at least 2 countdown messages
            with (
                patch("summon_claude.sessions.session._AUTH_TIMEOUT_S", 0.2),
                patch("summon_claude.sessions.session._AUTH_COUNTDOWN_INTERVAL_S", 0.04),
            ):
                await session._wait_for_auth()
        finally:
            session_logger = logging.getLogger("summon_claude.sessions.session")
            session_logger.removeHandler(handler)
            session_logger.setLevel(logging.NOTSET)

        countdown_msgs = [m for m in log_messages if "remaining" in m]
        assert len(countdown_msgs) >= 2, f"Expected >=2 countdown msgs, got: {countdown_msgs}"
