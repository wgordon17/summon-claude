"""Tests for session authentication UX (BUG-026, BUG-030)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from summon_claude.session import SummonSession


class TestAuthCountdown:
    """BUG-030: Auth countdown should use click.echo at 15s intervals."""

    async def test_countdown_uses_click_echo(self):
        """_wait_for_auth should call click.echo for countdown messages."""
        echoed: list[str] = []

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

        with (
            patch("summon_claude.session._AUTH_TIMEOUT_S", 20),
            patch("summon_claude.session._AUTH_POLL_INTERVAL_S", 1.0),
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch("click.echo", side_effect=echoed.append),
        ):
            result = await session._wait_for_auth()

        assert result in ("timed_out", "shutdown")
        countdown_msgs = [m for m in echoed if "remaining" in m]
        assert len(countdown_msgs) >= 1, f"Expected countdown messages, got: {echoed}"
        assert "15" in countdown_msgs[0] or "remaining" in countdown_msgs[0]

    async def test_countdown_interval_is_15s(self):
        """Countdown messages should appear at 15-second intervals."""
        echoed: list[str] = []

        session = object.__new__(SummonSession)
        session._authenticated_event = asyncio.Event()
        session._shutdown_event = asyncio.Event()

        call_count = 0

        async def fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 45:
                session._shutdown_event.set()

        with (
            patch("summon_claude.session._AUTH_TIMEOUT_S", 45),
            patch("summon_claude.session._AUTH_POLL_INTERVAL_S", 1.0),
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch("click.echo", side_effect=echoed.append),
        ):
            await session._wait_for_auth()

        countdown_msgs = [m for m in echoed if "remaining" in m]
        # At 45s timeout with 15s intervals: messages at 15s (30 remaining) and 30s (15 remaining)
        assert len(countdown_msgs) >= 2
