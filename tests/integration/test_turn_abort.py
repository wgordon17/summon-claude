"""Integration tests for turn abort mechanisms.

Tests the !stop command path (CommandResult metadata) and verifies
abort event coordination with asyncio tasks. EventDispatcher reaction
routing is covered by test_channel_reuse.py — these tests focus on
SummonSession-level abort behavior.
"""

from __future__ import annotations

import asyncio

import pytest

from summon_claude.sessions.commands import CommandContext, CommandResult, _handle_stop

pytestmark = pytest.mark.asyncio(loop_scope="module")


class TestStopCommand:
    async def test_stop_command_returns_stop_metadata(self):
        """_handle_stop returns CommandResult with stop=True metadata."""
        context = CommandContext()
        result = await _handle_stop([], context)

        assert isinstance(result, CommandResult)
        assert result.metadata == {"stop": True}
        assert result.text is not None
        assert ":octagonal_sign:" in result.text

    async def test_abort_event_coordination(self):
        """asyncio.Event-based abort coordination races abort against a long turn."""
        abort_event = asyncio.Event()

        # Simulate a long-running turn task
        turn_task = asyncio.create_task(asyncio.sleep(10))
        abort_wait = asyncio.create_task(abort_event.wait())

        async def _set_abort_after_delay():
            await asyncio.sleep(0.1)
            abort_event.set()

        abort_task = asyncio.create_task(_set_abort_after_delay())

        done, _ = await asyncio.wait(
            {turn_task, abort_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )

        assert abort_wait in done, "abort_wait should complete first"
        assert turn_task not in done, "turn_task should still be running"

        # Cleanup
        await abort_task  # Ensure the delayed set completes
        turn_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn_task
