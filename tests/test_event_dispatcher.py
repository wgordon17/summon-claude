"""Tests for summon_claude.event_dispatcher."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

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
        pending_turns=asyncio.Queue(),
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
        )

    async def test_permission_approve_session_calls_handle_action(self):
        """dispatch_action routes permission_approve_session to handle_action."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        action = {"action_id": "permission_approve_session", "value": "approve_session:batch-3"}
        body = {"channel": {"id": "C001"}, "user": {"id": "U001"}, "response_url": ""}
        await dispatcher.dispatch_action(action, body)

        ph.handle_action.assert_awaited_once_with(
            value="approve_session:batch-3",
            user_id="U001",
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
            trigger_id=None,
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

    async def test_static_select_extracts_selected_option_value(self):
        """dispatch_action with type=static_select extracts value from selected_option."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        action = {
            "action_id": "ask_user_0_select",
            "type": "static_select",
            "selected_option": {"value": "req-1|0|2"},
        }
        body = {"channel": {"id": "C001"}, "user": {"id": "U001"}, "response_url": ""}
        await dispatcher.dispatch_action(action, body)

        ph.handle_ask_user_action.assert_awaited_once_with(
            value="req-1|0|2",
            user_id="U001",
            trigger_id=None,
        )
        ph.handle_ask_user_multiselect_action.assert_not_awaited()

    async def test_static_select_missing_selected_option_sends_empty_value(self):
        """dispatch_action with type=static_select and no selected_option sends empty value."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        action = {
            "action_id": "ask_user_0_select",
            "type": "static_select",
            # No selected_option key
        }
        body = {"channel": {"id": "C001"}, "user": {"id": "U001"}, "response_url": ""}
        await dispatcher.dispatch_action(action, body)

        ph.handle_ask_user_action.assert_awaited_once_with(
            value="",
            user_id="U001",
            trigger_id=None,
        )

    async def test_multi_static_select_routes_to_multiselect_action(self):
        """dispatch_action with multi_static_select routes to handle_ask_user_multiselect_action."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        action = {
            "action_id": "ask_user_0_multiselect",
            "type": "multi_static_select",
            "selected_options": [
                {"value": "req-1|0|0"},
                {"value": "req-1|0|2"},
            ],
        }
        body = {"channel": {"id": "C001"}, "user": {"id": "U001"}, "response_url": ""}
        await dispatcher.dispatch_action(action, body)

        ph.handle_ask_user_multiselect_action.assert_awaited_once_with(
            action_id="ask_user_0_multiselect",
            selected_values=["req-1|0|0", "req-1|0|2"],
            user_id="U001",
        )
        ph.handle_ask_user_action.assert_not_awaited()

    async def test_multi_static_select_empty_selection_passes_empty_list(self):
        """dispatch_action with multi_static_select and no selection passes empty list."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        action = {
            "action_id": "ask_user_0_multiselect",
            "type": "multi_static_select",
            "selected_options": [],
        }
        body = {"channel": {"id": "C001"}, "user": {"id": "U001"}, "response_url": ""}
        await dispatcher.dispatch_action(action, body)

        ph.handle_ask_user_multiselect_action.assert_awaited_once_with(
            action_id="ask_user_0_multiselect",
            selected_values=[],
            user_id="U001",
        )

    async def test_multi_static_select_missing_selected_options_passes_empty_list(self):
        """dispatch_action with multi_static_select and no selected_options key passes empty."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        action = {
            "action_id": "ask_user_0_multiselect",
            "type": "multi_static_select",
            # No selected_options key
        }
        body = {"channel": {"id": "C001"}, "user": {"id": "U001"}, "response_url": ""}
        await dispatcher.dispatch_action(action, body)

        ph.handle_ask_user_multiselect_action.assert_awaited_once_with(
            action_id="ask_user_0_multiselect",
            selected_values=[],
            user_id="U001",
        )

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

        event = {"type": "reaction_added", "user": "U001", "item": {"channel": "C001"}}
        await dispatcher.dispatch_reaction(event)

        abort.assert_called_once()

    async def test_reaction_from_wrong_user_ignored(self):
        """dispatch_reaction ignores reactions from non-owner users."""
        dispatcher = EventDispatcher()
        abort = MagicMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", abort_callback=abort))

        event = {"type": "reaction_added", "user": "U_INTRUDER", "item": {"channel": "C001"}}
        await dispatcher.dispatch_reaction(event)

        abort.assert_not_called()

    async def test_unknown_channel_silently_ignored(self):
        """dispatch_reaction for an unknown channel does not raise."""
        dispatcher = EventDispatcher()
        event = {"type": "reaction_added", "user": "U001", "item": {"channel": "C_UNKNOWN"}}
        await dispatcher.dispatch_reaction(event)  # must not raise

    async def test_does_not_call_wrong_abort(self):
        """dispatch_reaction only calls the callback for the matching channel."""
        dispatcher = EventDispatcher()
        abort1 = MagicMock()
        abort2 = MagicMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", abort_callback=abort1))
        dispatcher.register("C002", _make_handle(channel_id="C002", abort_callback=abort2))

        await dispatcher.dispatch_reaction({"user": "U001", "item": {"channel": "C002"}})

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


class TestHasHandler:
    """Tests for has_handler()."""

    def test_registered_channel_returns_true(self):
        dispatcher = EventDispatcher()
        dispatcher.register("C001", _make_handle(channel_id="C001"))
        assert dispatcher.has_handler("C001") is True

    def test_unregistered_channel_returns_false(self):
        dispatcher = EventDispatcher()
        assert dispatcher.has_handler("C999") is False

    def test_after_unregister_returns_false(self):
        dispatcher = EventDispatcher()
        dispatcher.register("C001", _make_handle(channel_id="C001"))
        dispatcher.unregister("C001")
        assert dispatcher.has_handler("C001") is False


class TestUnroutedMessageFallback:
    """Tests for _handle_unrouted_message fallback."""

    async def test_ignores_non_resume_messages(self):
        """Non-!summon messages in unrouted channels are silently dropped."""
        mock_web = AsyncMock()
        dispatcher = EventDispatcher(web_client=mock_web)
        event = {"channel": "C_DEAD", "text": "hello", "user": "U001"}
        await dispatcher._handle_unrouted_message(event)
        mock_web.chat_postMessage.assert_not_awaited()

    async def test_resume_command_triggers_handler(self):
        """!summon resume in an unrouted channel triggers _handle_resume_request."""
        mock_web = AsyncMock()
        dispatcher = EventDispatcher(web_client=mock_web)
        dispatcher._handle_resume_request = AsyncMock()  # type: ignore[method-assign]
        event = {"channel": "C_DEAD", "text": "!summon resume", "user": "U001"}
        await dispatcher._handle_unrouted_message(event)
        dispatcher._handle_resume_request.assert_awaited_once_with("C_DEAD", "U001", None)

    async def test_resume_with_session_id(self):
        """!summon resume <id> passes the session ID."""
        dispatcher = EventDispatcher()
        dispatcher._handle_resume_request = AsyncMock()  # type: ignore[method-assign]
        event = {"channel": "C_DEAD", "text": "!summon resume sess-abc", "user": "U001"}
        await dispatcher._handle_unrouted_message(event)
        dispatcher._handle_resume_request.assert_awaited_once_with("C_DEAD", "U001", "sess-abc")

    async def test_resume_does_not_match_partial_word(self):
        """!summon resumed should not trigger resume handler."""
        dispatcher = EventDispatcher()
        dispatcher._handle_resume_request = AsyncMock()  # type: ignore[method-assign]
        event = {"channel": "C_DEAD", "text": "!summon resumed", "user": "U001"}
        await dispatcher._handle_unrouted_message(event)
        dispatcher._handle_resume_request.assert_not_awaited()

    async def test_resume_delegates_to_handler(self):
        """_handle_resume_request delegates to the resume handler."""
        mock_handler = AsyncMock()
        dispatcher = EventDispatcher()
        dispatcher.set_resume_handler(mock_handler)
        await dispatcher._handle_resume_request("C_CHAN", "U001", "sess-abc")
        mock_handler.assert_awaited_once_with("C_CHAN", "U001", "sess-abc")

    async def test_resume_handler_error_posts_to_channel(self):
        """_handle_resume_request posts handler ValueError to channel."""
        mock_web = AsyncMock()
        dispatcher = EventDispatcher(web_client=mock_web)
        mock_handler = AsyncMock(side_effect=ValueError("Only the owner can resume."))
        dispatcher.set_resume_handler(mock_handler)
        await dispatcher._handle_resume_request("C_CHAN", "U_INTRUDER", None)
        # Should post via SlackClient (which calls chat_postMessage)
        mock_web.chat_postMessage.assert_awaited_once()
        assert "owner" in mock_web.chat_postMessage.call_args.kwargs.get("text", "").lower()

    async def test_resume_no_handler_is_silent(self):
        """_handle_resume_request without a handler logs and returns silently."""
        mock_web = AsyncMock()
        dispatcher = EventDispatcher(web_client=mock_web)
        await dispatcher._handle_resume_request("C_CHAN", "U001", None)
        mock_web.chat_postMessage.assert_not_awaited()

    async def test_unrouted_message_dispatched_for_unregistered_channel(self):
        """Messages in unregistered channels go through the fallback path."""
        dispatcher = EventDispatcher()
        dispatcher._handle_unrouted_message = AsyncMock()  # type: ignore[method-assign]
        await dispatcher.dispatch_message({"channel": "C_DEAD", "text": "!summon resume"})
        dispatcher._handle_unrouted_message.assert_awaited_once()


class TestDispatchViewSubmission:
    """Tests for dispatch_view_submission()."""

    def _make_view(self, channel_id: str, request_id: str = "req-1", q_idx: int = 0) -> dict:
        import json

        return {
            "private_metadata": json.dumps(
                {"channel_id": channel_id, "request_id": request_id, "q_idx": q_idx}
            ),
            "state": {"values": {"other_input": {"other_value": {"value": "My answer"}}}},
        }

    async def test_routes_to_correct_session(self):
        """dispatch_view_submission calls handle_ask_user_view_submission on the right session."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        view = self._make_view("C001")
        body = {"user": {"id": "U001"}}
        await dispatcher.dispatch_view_submission(view, body)

        ph.handle_ask_user_view_submission.assert_awaited_once_with(view=view, user_id="U001")

    async def test_unauthorized_user_rejected(self):
        """dispatch_view_submission drops submissions from non-owner users."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        # authenticated_user_id="U001" from _make_handle default
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        view = self._make_view("C001")
        body = {"user": {"id": "U_INTRUDER"}}
        await dispatcher.dispatch_view_submission(view, body)

        ph.handle_ask_user_view_submission.assert_not_awaited()

    async def test_malformed_metadata_dropped(self):
        """dispatch_view_submission with bad private_metadata is silently dropped."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        view = {"private_metadata": "not-json", "state": {}}
        body = {"user": {"id": "U001"}}
        await dispatcher.dispatch_view_submission(view, body)  # must not raise

        ph.handle_ask_user_view_submission.assert_not_awaited()

    async def test_missing_channel_id_in_metadata_dropped(self):
        """dispatch_view_submission drops submissions with no channel_id in metadata."""
        import json

        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        view = {
            "private_metadata": json.dumps({"request_id": "req-1", "q_idx": 0}),  # no channel_id
            "state": {},
        }
        body = {"user": {"id": "U001"}}
        await dispatcher.dispatch_view_submission(view, body)

        ph.handle_ask_user_view_submission.assert_not_awaited()

    async def test_unknown_channel_silently_dropped(self):
        """dispatch_view_submission for an unknown channel_id is silently dropped."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        view = self._make_view("C_UNKNOWN")
        body = {"user": {"id": "U001"}}
        await dispatcher.dispatch_view_submission(view, body)  # must not raise

        ph.handle_ask_user_view_submission.assert_not_awaited()

    async def test_does_not_route_to_wrong_session(self):
        """dispatch_view_submission only calls the handler for the matching channel."""
        import json

        dispatcher = EventDispatcher()
        ph1 = AsyncMock()
        ph2 = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph1))
        dispatcher.register("C002", _make_handle(channel_id="C002", permission_handler=ph2))

        view = self._make_view("C001")
        body = {"user": {"id": "U001"}}
        await dispatcher.dispatch_view_submission(view, body)

        ph1.handle_ask_user_view_submission.assert_awaited_once()
        ph2.handle_ask_user_view_submission.assert_not_awaited()


class TestDispatchActionTriggerIdPassthrough:
    """Verify trigger_id is passed from dispatch_action to handle_ask_user_action."""

    async def test_trigger_id_passed_to_ask_user_action(self):
        """dispatch_action passes trigger_id from body to handle_ask_user_action."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        action = {"action_id": "ask_user_0_other", "value": "req-1|0|other", "type": "button"}
        body = {
            "channel": {"id": "C001"},
            "user": {"id": "U001"},
            "trigger_id": "trigger-xyz",
        }
        await dispatcher.dispatch_action(action, body)

        ph.handle_ask_user_action.assert_awaited_once_with(
            value="req-1|0|other",
            user_id="U001",
            trigger_id="trigger-xyz",
        )

    async def test_trigger_id_none_when_absent(self):
        """dispatch_action passes trigger_id=None when not in body."""
        dispatcher = EventDispatcher()
        ph = AsyncMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", permission_handler=ph))

        action = {"action_id": "ask_user_0_other", "value": "req-1|0|other", "type": "button"}
        body = {"channel": {"id": "C001"}, "user": {"id": "U001"}}  # no trigger_id
        await dispatcher.dispatch_action(action, body)

        ph.handle_ask_user_action.assert_awaited_once_with(
            value="req-1|0|other",
            user_id="U001",
            trigger_id=None,
        )


class TestDispatchAppHome:
    """Tests for dispatch_app_home() — comp-7 App Home."""

    async def test_calls_registered_handler_with_user_id(self):
        """dispatch_app_home calls the registered handler with the correct user_id."""
        dispatcher = EventDispatcher()
        handler = AsyncMock()
        dispatcher.set_app_home_handler(handler)

        await dispatcher.dispatch_app_home("U_ALICE")

        handler.assert_awaited_once_with("U_ALICE")

    async def test_no_handler_is_silent(self):
        """dispatch_app_home without a registered handler does not raise."""
        dispatcher = EventDispatcher()
        await dispatcher.dispatch_app_home("U_ALICE")  # must not raise

    async def test_handler_exception_is_swallowed(self):
        """Handler exceptions are caught and logged, not propagated."""
        dispatcher = EventDispatcher()
        handler = AsyncMock(side_effect=RuntimeError("boom"))
        dispatcher.set_app_home_handler(handler)

        await dispatcher.dispatch_app_home("U_ALICE")  # must not raise
        handler.assert_awaited_once()


class TestDispatchFileShared:
    """Tests for dispatch_file_shared() — comp-8 file handling."""

    def _make_file_event(
        self,
        user_id: str = "U001",
        channel_id: str = "C001",
        file_id: str = "F001",
    ) -> dict:
        return {"user_id": user_id, "channel_id": channel_id, "file_id": file_id}

    def _make_files_info_response(
        self,
        name: str = "script.py",
        mimetype: str = "text/plain",
        size: int = 100,
        url: str = "https://files.slack.com/file",
    ) -> dict:
        return {
            "file": {
                "name": name,
                "mimetype": mimetype,
                "size": size,
                "url_private_download": url,
            }
        }

    async def test_self_upload_filtered(self):
        """Bot self-uploads are silently dropped."""
        mock_web = AsyncMock()
        dispatcher = EventDispatcher(web_client=mock_web)
        dispatcher.set_bot_user_id("U_BOT")
        dispatcher.register("C001", _make_handle(channel_id="C001"))

        event = self._make_file_event(user_id="U_BOT")
        await dispatcher.dispatch_file_shared(event)

        mock_web.files_info.assert_not_awaited()

    async def test_unauthorized_user_rejected(self):
        """Files from non-owner users are rejected before files.info."""
        mock_web = AsyncMock()
        dispatcher = EventDispatcher(web_client=mock_web)
        # authenticated_user_id = "U001" (from _make_handle default)
        dispatcher.register("C001", _make_handle(channel_id="C001"))

        event = self._make_file_event(user_id="U_INTRUDER")
        await dispatcher.dispatch_file_shared(event)

        mock_web.files_info.assert_not_awaited()

    async def test_unknown_channel_silently_dropped(self):
        """File shared in a channel with no session is silently dropped."""
        mock_web = AsyncMock()
        dispatcher = EventDispatcher(web_client=mock_web)

        event = self._make_file_event(channel_id="C_UNKNOWN")
        await dispatcher.dispatch_file_shared(event)

        mock_web.files_info.assert_not_awaited()

    async def test_size_too_large_rejected_before_download(self):
        """Files exceeding MAX_FILE_SIZE are rejected without downloading."""
        from unittest.mock import patch

        from summon_claude.file_handler import MAX_FILE_SIZE

        mock_web = AsyncMock()
        mock_web.files_info = AsyncMock(
            return_value=self._make_files_info_response(
                name="big.py",
                size=MAX_FILE_SIZE + 1,
            )
        )
        dispatcher = EventDispatcher(web_client=mock_web)
        dispatcher.register("C001", _make_handle(channel_id="C001"))

        with patch("summon_claude.event_dispatcher.download_file") as mock_dl:
            await dispatcher.dispatch_file_shared(self._make_file_event())
            mock_dl.assert_not_called()

    async def test_unsupported_file_type_dropped(self):
        """Unsupported file types are silently dropped after classification."""
        from unittest.mock import patch

        mock_web = AsyncMock()
        mock_web.files_info = AsyncMock(
            return_value=self._make_files_info_response(
                name="binary.exe",
                mimetype="application/octet-stream",
            )
        )
        dispatcher = EventDispatcher(web_client=mock_web)
        dispatcher.register("C001", _make_handle(channel_id="C001"))

        with patch("summon_claude.event_dispatcher.download_file") as mock_dl:
            await dispatcher.dispatch_file_shared(self._make_file_event())
            mock_dl.assert_not_called()

    async def test_text_file_enqueued_on_pending_turns(self):
        """A valid text file is downloaded and put on pending_turns queue."""
        from unittest.mock import patch

        mock_web = AsyncMock()
        mock_web.files_info = AsyncMock(
            return_value=self._make_files_info_response(name="script.py")
        )
        mock_web.token = "xoxb-test"

        pending_q: asyncio.Queue = asyncio.Queue()
        handle = _make_handle(channel_id="C001")
        handle.pending_turns = pending_q

        dispatcher = EventDispatcher(web_client=mock_web)
        dispatcher.register("C001", handle)

        with patch(
            "summon_claude.event_dispatcher.download_file",
            new_callable=AsyncMock,
            return_value=b"print('hello')",
        ):
            await dispatcher.dispatch_file_shared(self._make_file_event())

        assert not pending_q.empty()
        item = pending_q.get_nowait()
        # _PendingTurn has a message attribute containing the text
        assert "script.py" in item.message

    async def test_no_web_client_returns_silently(self):
        """dispatch_file_shared without a web_client returns immediately."""
        dispatcher = EventDispatcher()  # no web_client
        dispatcher.register("C001", _make_handle(channel_id="C001"))
        # Must not raise
        await dispatcher.dispatch_file_shared(self._make_file_event())


class TestDispatchTurnOverflow:
    """Tests for _dispatch_turn_overflow() — comp-5 overflow menus."""

    def _make_overflow_action(self, value: str) -> dict:
        return {
            "action_id": "turn_overflow",
            "selected_option": {"value": value},
        }

    def _make_body(self, user_id: str = "U001", channel_id: str = "C001") -> dict:
        return {"channel": {"id": channel_id}, "user": {"id": user_id}}

    async def test_turn_stop_calls_abort_callback(self):
        """turn_stop dispatches to abort_callback for the authenticated user."""
        mock_web = AsyncMock()
        dispatcher = EventDispatcher(web_client=mock_web)
        abort = MagicMock()
        dispatcher.register(
            "C001",
            _make_handle(
                channel_id="C001",
                abort_callback=abort,
                session_id="sess-abc",
            ),
        )

        action = self._make_overflow_action("turn_stop")
        await dispatcher.dispatch_action(action, self._make_body())

        abort.assert_called_once()

    async def test_turn_copy_sid_posts_ephemeral_with_session_id(self):
        """turn_copy_sid posts ephemeral message containing the session ID."""
        mock_web = AsyncMock()
        dispatcher = EventDispatcher(web_client=mock_web)
        dispatcher.register(
            "C001",
            _make_handle(channel_id="C001", session_id="sess-xyz"),
        )

        action = self._make_overflow_action("turn_copy_sid")
        await dispatcher.dispatch_action(action, self._make_body())

        mock_web.chat_postEphemeral.assert_awaited_once()
        call_kwargs = mock_web.chat_postEphemeral.call_args.kwargs
        assert "sess-xyz" in call_kwargs.get("text", "")
        assert call_kwargs.get("user") == "U001"
        assert call_kwargs.get("channel") == "C001"

    async def test_turn_view_cost_posts_ephemeral(self):
        """turn_view_cost posts an ephemeral message to the user."""
        mock_web = AsyncMock()
        dispatcher = EventDispatcher(web_client=mock_web)
        dispatcher.register(
            "C001",
            _make_handle(channel_id="C001", session_id="sess-cost"),
        )

        action = self._make_overflow_action("turn_view_cost")
        await dispatcher.dispatch_action(action, self._make_body())

        mock_web.chat_postEphemeral.assert_awaited_once()
        call_kwargs = mock_web.chat_postEphemeral.call_args.kwargs
        assert call_kwargs.get("user") == "U001"
        assert call_kwargs.get("channel") == "C001"

    async def test_unauthorized_user_rejected(self):
        """turn_overflow from a non-owner user is silently dropped."""
        mock_web = AsyncMock()
        dispatcher = EventDispatcher(web_client=mock_web)
        abort = MagicMock()
        # authenticated_user_id = "U001" (from _make_handle default)
        dispatcher.register("C001", _make_handle(channel_id="C001", abort_callback=abort))

        action = self._make_overflow_action("turn_stop")
        body = {"channel": {"id": "C001"}, "user": {"id": "U_INTRUDER"}}
        await dispatcher.dispatch_action(action, body)

        abort.assert_not_called()
        mock_web.chat_postEphemeral.assert_not_awaited()

    async def test_unknown_value_is_noop(self):
        """An unrecognised turn_overflow value is logged and dropped."""
        mock_web = AsyncMock()
        dispatcher = EventDispatcher(web_client=mock_web)
        abort = MagicMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", abort_callback=abort))

        action = self._make_overflow_action("turn_unknown_action")
        await dispatcher.dispatch_action(action, self._make_body())  # must not raise

        abort.assert_not_called()
        mock_web.chat_postEphemeral.assert_not_awaited()

    async def test_turn_stop_does_not_post_ephemeral(self):
        """turn_stop only calls abort; it must not post any ephemeral message."""
        mock_web = AsyncMock()
        dispatcher = EventDispatcher(web_client=mock_web)
        abort = MagicMock()
        dispatcher.register("C001", _make_handle(channel_id="C001", abort_callback=abort))

        action = self._make_overflow_action("turn_stop")
        await dispatcher.dispatch_action(action, self._make_body())

        mock_web.chat_postEphemeral.assert_not_awaited()

    async def test_ephemeral_no_web_client_is_safe(self):
        """turn_copy_sid with no web_client logs and returns without crash."""
        dispatcher = EventDispatcher()  # no web_client
        dispatcher.register("C001", _make_handle(channel_id="C001", session_id="sess-1"))

        action = self._make_overflow_action("turn_copy_sid")
        await dispatcher.dispatch_action(action, self._make_body())  # must not raise
