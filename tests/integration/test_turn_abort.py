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

    async def test_dispatch_reaction_triggers_abort_callback(self):
        """dispatch_reaction from session owner fires the abort callback."""
        from unittest.mock import MagicMock

        from summon_claude.event_dispatcher import EventDispatcher, SessionHandle
        from summon_claude.sessions.permissions import PermissionHandler

        dispatcher = EventDispatcher()
        abort_event = asyncio.Event()

        def _abort() -> None:
            abort_event.set()

        handle = SessionHandle(
            session_id="test-abort",
            channel_id="C_ABORT",
            message_queue=asyncio.Queue(maxsize=10),
            permission_handler=MagicMock(spec=PermissionHandler),
            abort_callback=_abort,
            authenticated_user_id="U_OWNER",
        )
        dispatcher.register("C_ABORT", handle)

        # Dispatch a reaction from the session owner
        await dispatcher.dispatch_reaction(
            {
                "user": "U_OWNER",
                "reaction": "octagonal_sign",
                "item": {"channel": "C_ABORT", "ts": "123.456"},
            }
        )

        assert abort_event.is_set(), "abort callback should have set the event"
