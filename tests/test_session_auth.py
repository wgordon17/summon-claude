"""Tests for session authentication UX (BUG-026, BUG-030)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from summon_claude.session import SummonSession


def _capture_session_logs() -> tuple[logging.Handler, list[str]]:
    """Return a (handler, messages) pair; caller must remove the handler."""
    messages: list[str] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _CapturingHandler()
    session_logger = logging.getLogger("summon_claude.session")
    session_logger.addHandler(handler)
    session_logger.setLevel(logging.DEBUG)
    return handler, messages


class TestAuthCountdown:
    """BUG-030: Auth countdown should log at 15s intervals."""

    async def test_countdown_uses_logger(self):
        """_wait_for_auth should call logger.info for countdown messages."""
        # Create a minimal session instance
        session = object.__new__(SummonSession)
        session._authenticated_event = asyncio.Event()
        session._shutdown_event = asyncio.Event()

        # Trigger timeout after simulated 20 seconds
        call_count = 0

        async def fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 20:
                session._shutdown_event.set()

        handler, log_messages = _capture_session_logs()
        try:
            with (
                patch("summon_claude.session._AUTH_TIMEOUT_S", 20),
                patch("summon_claude.session._AUTH_POLL_INTERVAL_S", 1.0),
                patch("asyncio.sleep", side_effect=fake_sleep),
            ):
                result = await session._wait_for_auth()
        finally:
            session_logger = logging.getLogger("summon_claude.session")
            session_logger.removeHandler(handler)
            session_logger.setLevel(logging.NOTSET)

        assert result in ("timed_out", "shutdown")
        countdown_msgs = [m for m in log_messages if "remaining" in m]
        assert len(countdown_msgs) >= 1, f"Expected countdown messages, got: {log_messages}"
        assert "remaining" in countdown_msgs[0]

    async def test_countdown_interval_is_15s(self):
        """Countdown messages should appear at 15-second intervals."""
        session = object.__new__(SummonSession)
        session._authenticated_event = asyncio.Event()
        session._shutdown_event = asyncio.Event()

        call_count = 0

        async def fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 45:
                session._shutdown_event.set()

        handler, log_messages = _capture_session_logs()
        try:
            with (
                patch("summon_claude.session._AUTH_TIMEOUT_S", 45),
                patch("summon_claude.session._AUTH_POLL_INTERVAL_S", 1.0),
                patch("asyncio.sleep", side_effect=fake_sleep),
            ):
                await session._wait_for_auth()
        finally:
            session_logger = logging.getLogger("summon_claude.session")
            session_logger.removeHandler(handler)
            session_logger.setLevel(logging.NOTSET)

        countdown_msgs = [m for m in log_messages if "remaining" in m]
        # At 45s timeout with 15s intervals: messages at 15s (30 remaining) and 30s (15 remaining)
        assert len(countdown_msgs) >= 2
