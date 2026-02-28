"""Tests for summon_claude.event_dispatcher."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from summon_claude.event_dispatcher import EventDispatcher, SessionHandle


def _make_handle(
    session_id: str = "sess-1",
    channel_id: str = "C001",
    queue: asyncio.Queue | None = None,
    permission_handler: object | None = None,
    abort_callback: object | None = None,
) -> SessionHandle:
    """Return a SessionHandle with sensible defaults for tests."""
    return SessionHandle(
        session_id=session_id,
        channel_id=channel_id,
        message_queue=queue if queue is not None else asyncio.Queue(),
        permission_handler=permission_handler if permission_handler is not None else AsyncMock(),
        abort_callback=abort_callback if abort_callback is not None else MagicMock(),
        authenticated_user_id="U001",
    )


class TestRegisterUnregister:
    """Tests for register() and unregister()."""

    def test_register_stores_handle(self):
        """register() stores the handle for the given channel_id."""
        dispatcher = EventDispatcher()
        handle = _make_handle(channel_id="C001")
        dispatcher.register("C001", handle)
        assert dispatcher._sessions["C001"] is handle

    def test_register_overwrites_existing(self):
        """Registering the same channel_id twice replaces the previous handle."""
        dispatcher = EventDispatcher()
        old = _make_handle(session_id="old", channel_id="C001")
        new = _make_handle(session_id="new", channel_id="C001")
        dispatcher.register("C001", old)
        dispatcher.register("C001", new)
        assert dispatcher._sessions["C001"] is new

    def test_unregister_removes_handle(self):
        """unregister() removes the handle for a known channel_id."""
        dispatcher = EventDispatcher()
        handle = _make_handle(channel_id="C001")
        dispatcher.register("C001", handle)
        dispatcher.unregister("C001")
        assert "C001" not in dispatcher._sessions

    def test_unregister_unknown_channel_is_noop(self):
        """unregister() on an unknown channel_id does not raise."""
        dispatcher = EventDispatcher()
        dispatcher.unregister("C_UNKNOWN")  # must not raise

    def test_multiple_sessions_independent(self):
        """Multiple sessions on different channels are tracked independently."""
        dispatcher = EventDispatcher()
        h1 = _make_handle(session_id="s1", channel_id="C001")
        h2 = _make_handle(session_id="s2", channel_id="C002")
        dispatcher.register("C001", h1)
        dispatcher.register("C002", h2)
        dispatcher.unregister("C001")
        assert "C001" not in dispatcher._sessions
        assert dispatcher._sessions["C002"] is h2


class TestDispatchMessage:
    """Tests for dispatch_message()."""

    async def test_routes_to_correct_queue(self):
        """dispatch_message puts the event on the handle's message_queue."""
        dispatcher = EventDispatcher()
        queue: asyncio.Queue = asyncio.Queue()
        handle = _make_handle(channel_id="C001", queue=queue)
        dispatcher.register("C001", handle)

        event = {"type": "message", "channel": "C001", "text": "hello"}
        await dispatcher.dispatch_message(event)

        assert not queue.empty()
        assert queue.get_nowait() is event

    async def test_unknown_channel_silently_ignored(self):
        """dispatch_message with an unknown channel_id does not raise."""
        dispatcher = EventDispatcher()
        event = {"type": "message", "channel": "C_UNKNOWN", "text": "hello"}
        await dispatcher.dispatch_message(event)  # must not raise

    async def test_does_not_route_to_wrong_session(self):
        """dispatch_message only routes to the matching channel's session."""
        dispatcher = EventDispatcher()
        q1: asyncio.Queue = asyncio.Queue()
        q2: asyncio.Queue = asyncio.Queue()
        dispatcher.register("C001", _make_handle(channel_id="C001", queue=q1))
        dispatcher.register("C002", _make_handle(channel_id="C002", queue=q2))

        await dispatcher.dispatch_message({"channel": "C001", "text": "for C001"})

        assert q1.qsize() == 1
        assert q2.empty()

    async def test_missing_channel_key_silently_ignored(self):
        """Events with no 'channel' key map to '' which has no session."""
        dispatcher = EventDispatcher()
        dispatcher.register("C001", _make_handle(channel_id="C001"))
        await dispatcher.dispatch_message({"type": "message"})  # no 'channel' key

    async def test_after_unregister_messages_dropped(self):
        """Messages for an unregistered channel are dropped silently."""
        dispatcher = EventDispatcher()
        queue: asyncio.Queue = asyncio.Queue()
        dispatcher.register("C001", _make_handle(channel_id="C001", queue=queue))
        dispatcher.unregister("C001")

        await dispatcher.dispatch_message({"channel": "C001", "text": "lost"})
        assert queue.empty()


class TestDispatchAction:
    """Tests for dispatch_action()."""

    async def test_permission_action_calls_handle_action(self):
        """dispatch_action routes permission_approve to handle_action."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        action = {"action_id": "permission_approve", "value": "approve:batch-1"}
        body = {
            "channel": {"id": "C001"},
            "user": {"id": "U001"},
            "response_url": "https://hooks.slack.com/actions/...",
        }
        await dispatcher.dispatch_action(action, body)

        ph.handle_action.assert_awaited_once_with(
            value="approve:batch-1",
            user_id="U001",
            channel_id="C001",
            response_url="https://hooks.slack.com/actions/...",
        )

    async def test_permission_deny_calls_handle_action(self):
        """dispatch_action routes permission_deny to handle_action."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        action = {"action_id": "permission_deny", "value": "deny:batch-2"}
        body = {"channel": {"id": "C001"}, "user": {"id": "U001"}, "response_url": ""}
        await dispatcher.dispatch_action(action, body)

        ph.handle_action.assert_awaited_once_with(
            value="deny:batch-2",
            user_id="U001",
            channel_id="C001",
            response_url="",
        )

    async def test_ask_user_action_calls_handle_ask_user_action(self):
        """dispatch_action routes ask_user_* to handle_ask_user_action."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        action = {"action_id": "ask_user_0_0", "value": "req-1|0|0"}
        body = {"channel": {"id": "C001"}, "user": {"id": "U001"}, "response_url": ""}
        await dispatcher.dispatch_action(action, body)

        ph.handle_ask_user_action.assert_awaited_once_with(
            value="req-1|0|0",
            user_id="U001",
            response_url="",
        )

    async def test_ask_user_other_action_routes_correctly(self):
        """ask_user_*_other action_id pattern routes to handle_ask_user_action."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        action = {"action_id": "ask_user_1_other", "value": "req-2|1|other"}
        body = {"channel": {"id": "C001"}, "user": {"id": "U002"}, "response_url": ""}
        await dispatcher.dispatch_action(action, body)

        ph.handle_ask_user_action.assert_awaited_once()
        ph.handle_action.assert_not_called()

    async def test_unknown_channel_silently_ignored(self):
        """dispatch_action for an unknown channel does not raise."""
        dispatcher = EventDispatcher()
        action = {"action_id": "permission_approve", "value": "approve:x"}
        body = {"channel": {"id": "C_UNKNOWN"}, "user": {"id": "U001"}, "response_url": ""}
        await dispatcher.dispatch_action(action, body)  # must not raise

    async def test_routes_to_correct_session_only(self):
        """dispatch_action routes only to the session matching the action's channel."""
        dispatcher = EventDispatcher()
        ph1 = AsyncMock()
        ph2 = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph1))
        dispatcher.register("C002", _make_handle(channel_id="C002", permission_handler=ph2))

        action = {"action_id": "permission_approve", "value": "approve:b"}
        body = {"channel": {"id": "C001"}, "user": {"id": "U001"}, "response_url": ""}
        await dispatcher.dispatch_action(action, body)

        ph1.handle_action.assert_awaited_once()
        ph2.handle_action.assert_not_called()


class TestDispatchReaction:
    """Tests for dispatch_reaction()."""

    async def test_routes_to_correct_abort_callback(self):
        """dispatch_reaction calls the abort_callback for the matching channel."""
        dispatcher = EventDispatcher()
        abort = MagicMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", abort_callback=abort))

        event = {"type": "reaction_added", "item": {"channel": "C001"}}
        await dispatcher.dispatch_reaction(event)

        abort.assert_called_once()

    async def test_unknown_channel_silently_ignored(self):
        """dispatch_reaction for an unknown channel does not raise."""
        dispatcher = EventDispatcher()
        event = {"type": "reaction_added", "item": {"channel": "C_UNKNOWN"}}
        await dispatcher.dispatch_reaction(event)  # must not raise

    async def test_does_not_call_wrong_abort(self):
        """dispatch_reaction only calls the callback for the matching channel."""
        dispatcher = EventDispatcher()
        abort1 = MagicMock()
        abort2 = MagicMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", abort_callback=abort1))
        dispatcher.register("C002", _make_handle(channel_id="C002", abort_callback=abort2))

        await dispatcher.dispatch_reaction({"item": {"channel": "C002"}})

        abort1.assert_not_called()
        abort2.assert_called_once()

    async def test_missing_item_key_silently_ignored(self):
        """Reaction events with no 'item' key map to '' which has no session."""
        dispatcher = EventDispatcher()
        dispatcher.register("C001", _make_handle(channel_id="C001"))
        await dispatcher.dispatch_reaction({"type": "reaction_added"})  # no 'item' key

    async def test_after_unregister_abort_not_called(self):
        """After unregister, reactions for that channel are silently dropped."""
        dispatcher = EventDispatcher()
        abort = MagicMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", abort_callback=abort))
        dispatcher.unregister("C001")

        await dispatcher.dispatch_reaction({"item": {"channel": "C001"}})
        abort.assert_not_called()
