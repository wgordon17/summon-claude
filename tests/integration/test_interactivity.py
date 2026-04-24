"""Integration tests for Slack interactivity features (PR #139).

Tests cover the new user-facing interaction patterns:
- AskUserQuestion with select menus (>4 options → static_select)
- AskUserQuestion with multi-select menus
- AskUserQuestion "Other" modal submission
- AskUserQuestion message deletion after completion
- Overflow menu on turn messages (posted via Block Kit)
- App Home dashboard (views.publish)
- File upload event dispatch (file_shared → session queue)

Requires SUMMON_TEST_SLACK_BOT_TOKEN — skipped when credentials are absent.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import PermissionResultAllow
from conftest import make_test_config

from summon_claude.event_dispatcher import EventDispatcher, SessionHandle
from summon_claude.sessions.permissions import PermissionHandler
from summon_claude.sessions.registry import SessionRegistry
from summon_claude.slack.client import SlackClient
from summon_claude.slack.formatting import build_home_view
from summon_claude.slack.router import ThreadRouter

pytestmark = [
    pytest.mark.slack,
    pytest.mark.xdist_group("slack_socket"),
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_permission_handler(
    slack_client,
    debounce_ms=50,
    timeout_s=5,
    authenticated_user_id="U_OWNER",
):
    router = ThreadRouter(slack_client)
    config = make_test_config(
        permission_debounce_ms=debounce_ms,
        permission_timeout_s=timeout_s,
    )
    handler = PermissionHandler(
        router,
        config,
        authenticated_user_id=authenticated_user_id,
    )
    handler._check_write_gate = AsyncMock(return_value=None)
    return handler


async def _get_latest_message(web_client, channel_id):
    history = await web_client.conversations_history(channel=channel_id, limit=1)
    messages = history.get("messages", [])
    return messages[0] if messages else None


async def _extract_request_id_from_channel(web_client, channel_id, retries=5):
    for attempt in range(retries):
        history = await web_client.conversations_history(channel=channel_id, limit=5)
        for msg in history.get("messages", []):
            blocks = msg.get("blocks") or []
            for block in blocks:
                if block.get("type") == "actions":
                    for el in block.get("elements", []):
                        val = el.get("value", "")
                        parts = val.split("|")
                        if len(parts) == 3:
                            return parts[0]
                # Also check section accessory (static_select case)
                acc = block.get("accessory", {})
                if acc.get("type") == "static_select":
                    options = acc.get("options", [])
                    if options:
                        parts = options[0].get("value", "").split("|")
                        if len(parts) == 3:
                            return parts[0]
        if attempt < retries - 1:
            await asyncio.sleep(0.5)
    raise ValueError(f"Could not extract request_id from channel {channel_id}")


# ---------------------------------------------------------------------------
# Select Menu Tests (Task 4)
# ---------------------------------------------------------------------------


class TestSelectMenus:
    """AskUserQuestion with >4 options uses static_select instead of buttons."""

    async def test_many_options_renders_static_select(self, slack_harness, test_channel):
        """5+ options → Block Kit renders a static_select element, not buttons."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(slack_client, debounce_ms=50)

        questions = [
            {
                "question": "Pick a language?",
                "header": "Language",
                "options": [{"label": f"Language {i}", "value": f"lang_{i}"} for i in range(6)],
            }
        ]
        task = asyncio.create_task(
            handler.handle("AskUserQuestion", {"questions": questions}, None),
        )
        await asyncio.sleep(0.5)

        msg = await _get_latest_message(slack_harness.client, test_channel)
        assert msg is not None
        blocks = msg.get("blocks") or []

        # Find the section with static_select accessory
        select_blocks = [b for b in blocks if b.get("accessory", {}).get("type") == "static_select"]
        assert select_blocks, "Expected static_select accessory for 6 options"

        accessory = select_blocks[0]["accessory"]
        assert len(accessory["options"]) == 6

        # Resolve via static_select value to unblock
        request_id = await _extract_request_id_from_channel(slack_harness.client, test_channel)
        await handler.handle_ask_user_action(
            value=f"{request_id}|0|0",
            user_id="U_OWNER",
        )
        result = await task
        assert isinstance(result, PermissionResultAllow)

    async def test_few_options_renders_buttons(self, slack_harness, test_channel):
        """<=4 options → Block Kit renders buttons, not a select menu."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(slack_client, debounce_ms=50)

        questions = [
            {
                "question": "Pick one?",
                "header": "Choice",
                "options": [
                    {"label": "A"},
                    {"label": "B"},
                    {"label": "C"},
                ],
            }
        ]
        task = asyncio.create_task(
            handler.handle("AskUserQuestion", {"questions": questions}, None),
        )
        await asyncio.sleep(0.5)

        msg = await _get_latest_message(slack_harness.client, test_channel)
        assert msg is not None
        blocks = msg.get("blocks") or []

        # Should have buttons, not static_select
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert action_blocks, "Expected actions block with buttons"
        elements = action_blocks[0]["elements"]
        button_count = sum(1 for el in elements if el.get("type") == "button")
        # 3 options + Other = 4 buttons
        assert button_count == 4

        # No static_select anywhere
        select_blocks = [b for b in blocks if b.get("accessory", {}).get("type") == "static_select"]
        assert not select_blocks, "Should not have static_select for 3 options"

        # Resolve
        request_id = await _extract_request_id_from_channel(slack_harness.client, test_channel)
        await handler.handle_ask_user_action(value=f"{request_id}|0|0", user_id="U_OWNER")
        await task

    async def test_multiselect_renders_multi_static_select(self, slack_harness, test_channel):
        """Multi-select with >4 options uses multi_static_select element."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(slack_client, debounce_ms=50)

        questions = [
            {
                "question": "Select frameworks?",
                "header": "Frameworks",
                "options": [{"label": f"Framework {i}"} for i in range(5)],
                "multiSelect": True,
            }
        ]
        task = asyncio.create_task(
            handler.handle("AskUserQuestion", {"questions": questions}, None),
        )
        await asyncio.sleep(0.5)

        msg = await _get_latest_message(slack_harness.client, test_channel)
        assert msg is not None
        blocks = msg.get("blocks") or []

        # Find multi_static_select in section accessory
        multiselect_blocks = [
            b for b in blocks if b.get("accessory", {}).get("type") == "multi_static_select"
        ]
        assert multiselect_blocks, "Expected multi_static_select accessory for multiSelect"
        assert len(multiselect_blocks[0]["accessory"]["options"]) == 5

        # Simulate multi-select + Done to resolve
        request_id = await _extract_request_id_from_channel(slack_harness.client, test_channel)
        await handler.handle_ask_user_multiselect_action(
            action_id="ask_user_0_multiselect",
            selected_values=[f"{request_id}|0|0", f"{request_id}|0|2"],
            user_id="U_OWNER",
        )
        await handler.handle_ask_user_action(value=f"{request_id}|0|done", user_id="U_OWNER")
        result = await task
        assert isinstance(result, PermissionResultAllow)
        answers = result.updated_input.get("answers", {})
        assert "Select frameworks?" in answers
        assert "Framework 0" in answers["Select frameworks?"]
        assert "Framework 2" in answers["Select frameworks?"]


# ---------------------------------------------------------------------------
# AskUserQuestion Message Deletion (Task 6)
# ---------------------------------------------------------------------------


class TestAskUserDeletion:
    """AskUserQuestion interactive messages are deleted after completion."""

    async def test_question_message_deleted_after_answer(self, slack_harness, test_channel):
        """After answering, the interactive question message is deleted."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(slack_client, debounce_ms=50)

        questions = [
            {
                "question": "Delete test?",
                "header": "Test",
                "options": [{"label": "Yes"}, {"label": "No"}],
            }
        ]
        task = asyncio.create_task(
            handler.handle("AskUserQuestion", {"questions": questions}, None),
        )
        await asyncio.sleep(0.5)

        # Capture the question message ts
        msg = await _get_latest_message(slack_harness.client, test_channel)
        assert msg is not None
        question_ts = msg["ts"]

        # Answer
        request_id = await _extract_request_id_from_channel(slack_harness.client, test_channel)
        await handler.handle_ask_user_action(value=f"{request_id}|0|0", user_id="U_OWNER")
        await task

        # Give Slack time to process the deletion
        await asyncio.sleep(0.5)

        # The question message should be deleted
        latest = await _get_latest_message(slack_harness.client, test_channel)
        if latest is not None:
            assert latest["ts"] != question_ts, (
                "Question message should have been deleted after answering"
            )


# ---------------------------------------------------------------------------
# Overflow Menu Tests (Task 5)
# ---------------------------------------------------------------------------


class TestOverflowMenu:
    """Turn messages include overflow menus with contextual actions."""

    async def test_turn_message_has_overflow_accessory(self, slack_harness, test_channel):
        """Turn header messages include an overflow menu accessory."""
        from summon_claude.sessions.response import _build_turn_header_blocks

        text = "\U0001f527 Turn 1: Processing..."
        blocks = _build_turn_header_blocks(text)

        # Post using real Slack API to verify Block Kit renders
        resp = await slack_harness.client.chat_postMessage(
            channel=test_channel, text=text, blocks=blocks
        )
        assert resp["ok"]

        # Verify the posted message has the overflow
        await asyncio.sleep(0.3)
        msg = await _get_latest_message(slack_harness.client, test_channel)
        assert msg is not None
        msg_blocks = msg.get("blocks") or []
        assert msg_blocks, "Posted message should have blocks"

        section = msg_blocks[0]
        assert section.get("type") == "section"
        accessory = section.get("accessory", {})
        assert accessory.get("type") == "overflow", "Expected overflow accessory"
        assert accessory.get("action_id") == "turn_overflow"

        option_values = [o["value"] for o in accessory.get("options", [])]
        assert "turn_stop" in option_values
        assert "turn_copy_sid" in option_values
        assert "turn_view_cost" in option_values

    async def test_overflow_stop_calls_abort(self, slack_harness, test_channel):
        """Selecting 'Stop Turn' from overflow triggers the abort callback."""
        dispatcher = EventDispatcher(web_client=slack_harness.client)
        abort_called = asyncio.Event()

        handle = SessionHandle(
            session_id="test-overflow-sid",
            channel_id=test_channel,
            message_queue=asyncio.Queue(),
            permission_handler=MagicMock(spec=PermissionHandler),
            abort_callback=abort_called.set,
            authenticated_user_id="U_OWNER",
            pending_turns=asyncio.Queue(),
        )
        dispatcher.register(test_channel, handle)

        action = {
            "action_id": "turn_overflow",
            "type": "overflow",
            "selected_option": {"value": "turn_stop"},
        }
        body = {"channel": {"id": test_channel}, "user": {"id": "U_OWNER"}}

        await dispatcher.dispatch_action(action, body)
        assert abort_called.is_set(), "Abort callback should have been called"

        dispatcher.unregister(test_channel)

    async def test_overflow_copy_sid_posts_ephemeral(self, slack_harness, test_channel):
        """Selecting 'Copy Session ID' posts an ephemeral message."""
        dispatcher = EventDispatcher(web_client=slack_harness.client)
        bot_user_id = await slack_harness.resolve_bot_user_id()

        handle = SessionHandle(
            session_id="test-copy-sid",
            channel_id=test_channel,
            message_queue=asyncio.Queue(),
            permission_handler=MagicMock(spec=PermissionHandler),
            abort_callback=lambda: None,
            authenticated_user_id=bot_user_id,
            pending_turns=asyncio.Queue(),
        )
        dispatcher.register(test_channel, handle)

        action = {
            "action_id": "turn_overflow",
            "type": "overflow",
            "selected_option": {"value": "turn_copy_sid"},
        }
        body = {"channel": {"id": test_channel}, "user": {"id": bot_user_id}}

        # Should not raise — ephemeral post is best-effort
        await dispatcher.dispatch_action(action, body)

        dispatcher.unregister(test_channel)

    async def test_overflow_rejected_for_wrong_user(self, slack_harness, test_channel):
        """Overflow actions from wrong user are silently rejected."""
        dispatcher = EventDispatcher(web_client=slack_harness.client)
        abort_called = asyncio.Event()

        handle = SessionHandle(
            session_id="test-overflow-auth",
            channel_id=test_channel,
            message_queue=asyncio.Queue(),
            permission_handler=MagicMock(spec=PermissionHandler),
            abort_callback=abort_called.set,
            authenticated_user_id="U_OWNER",
            pending_turns=asyncio.Queue(),
        )
        dispatcher.register(test_channel, handle)

        action = {
            "action_id": "turn_overflow",
            "type": "overflow",
            "selected_option": {"value": "turn_stop"},
        }
        body = {"channel": {"id": test_channel}, "user": {"id": "U_INTRUDER"}}

        await dispatcher.dispatch_action(action, body)
        assert not abort_called.is_set(), "Abort should NOT fire for wrong user"

        dispatcher.unregister(test_channel)


# ---------------------------------------------------------------------------
# App Home Tests (Task 7)
# ---------------------------------------------------------------------------


class TestAppHome:
    """App Home dashboard is published via views.publish."""

    async def test_app_home_view_published(self, slack_harness):
        """views.publish sends the home view to Slack without error."""
        bot_user_id = await slack_harness.resolve_bot_user_id()

        sessions = [
            {
                "session_name": "test-session",
                "model": "opus",
                "slack_channel_name": "test-chan",
                "status": "active",
                "context_pct": 42.5,
            }
        ]
        view = build_home_view(sessions)

        # Publish the home view — this is a real Slack API call
        resp = await slack_harness.client.views_publish(user_id=bot_user_id, view=view)
        assert resp["ok"], f"views.publish failed: {resp}"

    async def test_app_home_empty_sessions(self, slack_harness):
        """App Home with no sessions shows the empty state."""
        bot_user_id = await slack_harness.resolve_bot_user_id()

        view = build_home_view([])
        resp = await slack_harness.client.views_publish(user_id=bot_user_id, view=view)
        assert resp["ok"], f"views.publish failed for empty state: {resp}"

        # Verify the view structure
        assert view["type"] == "home"
        blocks = view["blocks"]
        # header + divider + empty state + last updated = 4 blocks
        text_blocks = [
            b for b in blocks if b.get("text", {}).get("text", "").startswith("_No active")
        ]
        assert text_blocks, "Empty state should show 'No active sessions'"

    async def test_app_home_session_fields(self):
        """App Home session entries include all expected fields."""
        sessions = [
            {
                "session_name": "my-session",
                "model": "sonnet",
                "slack_channel_name": "summon-abc123",
                "status": "active",
                "context_pct": 67.3,
            }
        ]
        view = build_home_view(sessions)

        section_blocks = [b for b in view["blocks"] if b.get("type") == "section"]
        # First section is the "Claude has a question" header — skip.
        # Find the one with fields
        field_blocks = [b for b in section_blocks if "fields" in b]
        assert field_blocks, "Session entry should have a fields block"

        field_texts = [f["text"] for f in field_blocks[0]["fields"]]
        assert any("my-session" in t for t in field_texts)
        assert any("sonnet" in t for t in field_texts)
        assert any("summon-abc123" in t for t in field_texts)
        assert any("active" in t for t in field_texts)
        assert any("67%" in t for t in field_texts)


# ---------------------------------------------------------------------------
# File Upload Dispatch Tests (Task 8)
# ---------------------------------------------------------------------------


class TestFileUploadDispatch:
    """file_shared event dispatch — security filtering, routing, and Socket Mode delivery."""

    async def test_file_shared_event_flows(self, slack_harness, test_channel, event_consumer):
        """Upload a file → file_shared event arrives via Socket Mode."""
        nonce = secrets.token_hex(8)
        await slack_harness.client.files_upload_v2(
            channel=test_channel,
            content=f"print('hello {nonce}')",
            filename=f"test-{nonce}.py",
            title=f"Test {nonce}",
        )

        event = await event_consumer.wait_for_event(
            lambda e: e.get("type") == "file_shared" and e.get("channel_id") == test_channel,
            timeout=15.0,
        )
        assert event.get("file_id"), "file_shared event should include file_id"

    async def test_file_dispatch_rejects_wrong_user(self):
        """dispatch_file_shared drops files from users who don't own the session."""
        channel_id = "C_FILE_AUTH"
        dispatcher = EventDispatcher()

        handle = SessionHandle(
            session_id="test-file-auth",
            channel_id=channel_id,
            message_queue=asyncio.Queue(),
            permission_handler=MagicMock(spec=PermissionHandler),
            abort_callback=lambda: None,
            authenticated_user_id="U_OWNER",
            pending_turns=asyncio.Queue(),
        )
        dispatcher.register(channel_id, handle)

        event = {
            "user_id": "U_INTRUDER",
            "channel_id": channel_id,
            "file_id": "F_FAKE",
        }
        await dispatcher.dispatch_file_shared(event)

        assert handle.pending_turns.empty(), (
            "File from wrong user should not reach the session queue"
        )

        dispatcher.unregister(channel_id)

    async def test_file_dispatch_rejects_bot_self_upload(self):
        """dispatch_file_shared drops files uploaded by the bot itself."""
        channel_id = "C_FILE_SELF"
        dispatcher = EventDispatcher()
        dispatcher.set_bot_user_id("U_BOT")

        handle = SessionHandle(
            session_id="test-file-self",
            channel_id=channel_id,
            message_queue=asyncio.Queue(),
            permission_handler=MagicMock(spec=PermissionHandler),
            abort_callback=lambda: None,
            authenticated_user_id="U_BOT",
            pending_turns=asyncio.Queue(),
        )
        dispatcher.register(channel_id, handle)

        event = {
            "user_id": "U_BOT",
            "channel_id": channel_id,
            "file_id": "F_SELF",
        }
        await dispatcher.dispatch_file_shared(event)

        assert handle.pending_turns.empty(), "Bot's own file uploads should be filtered out"

        dispatcher.unregister(channel_id)

    async def test_file_dispatch_no_session_drops(self):
        """dispatch_file_shared silently drops events for unregistered channels."""
        dispatcher = EventDispatcher()

        event = {
            "user_id": "U_SOMEONE",
            "channel_id": "C_NONEXISTENT",
            "file_id": "F_FAKE",
        }
        # Should not raise
        await dispatcher.dispatch_file_shared(event)


# ---------------------------------------------------------------------------
# View Submission Dispatch Tests (Task 3)
# ---------------------------------------------------------------------------


class TestViewSubmission:
    """Modal view submissions are routed to the correct permission handler."""

    async def test_view_submission_dispatch(self):
        """dispatch_view_submission routes to the correct session by channel_id."""
        channel_id = "C_VIEW_TEST"
        dispatcher = EventDispatcher()

        mock_handler = MagicMock(spec=PermissionHandler)
        mock_handler.handle_ask_user_view_submission = AsyncMock()

        handle = SessionHandle(
            session_id="test-view-sub",
            channel_id=channel_id,
            message_queue=asyncio.Queue(),
            permission_handler=mock_handler,
            abort_callback=lambda: None,
            authenticated_user_id="U_OWNER",
            pending_turns=asyncio.Queue(),
        )
        dispatcher.register(channel_id, handle)

        view = {
            "callback_id": "ask_user_other",
            "private_metadata": json.dumps({"channel_id": channel_id}),
            "state": {"values": {"other_input": {"other_value": {"value": "My custom answer"}}}},
        }
        body = {"user": {"id": "U_OWNER"}}

        await dispatcher.dispatch_view_submission(view, body)

        mock_handler.handle_ask_user_view_submission.assert_called_once_with(
            view=view, user_id="U_OWNER"
        )

        dispatcher.unregister(channel_id)

    async def test_view_submission_rejects_wrong_user(self):
        """View submissions from wrong user are silently rejected."""
        channel_id = "C_VIEW_AUTH"
        dispatcher = EventDispatcher()

        mock_handler = MagicMock(spec=PermissionHandler)
        mock_handler.handle_ask_user_view_submission = AsyncMock()

        handle = SessionHandle(
            session_id="test-view-auth",
            channel_id=channel_id,
            message_queue=asyncio.Queue(),
            permission_handler=mock_handler,
            abort_callback=lambda: None,
            authenticated_user_id="U_OWNER",
            pending_turns=asyncio.Queue(),
        )
        dispatcher.register(channel_id, handle)

        view = {
            "callback_id": "ask_user_other",
            "private_metadata": json.dumps({"channel_id": channel_id}),
        }
        body = {"user": {"id": "U_INTRUDER"}}

        await dispatcher.dispatch_view_submission(view, body)

        mock_handler.handle_ask_user_view_submission.assert_not_called()

        dispatcher.unregister(channel_id)

    async def test_view_submission_bad_metadata_drops(self):
        """Malformed private_metadata in view submission is silently dropped."""
        dispatcher = EventDispatcher()

        view = {
            "callback_id": "ask_user_other",
            "private_metadata": "not-valid-json",
        }
        body = {"user": {"id": "U_SOMEONE"}}

        # Should not raise
        await dispatcher.dispatch_view_submission(view, body)


# ---------------------------------------------------------------------------
# Registry: list_active_by_user (Task 7 support)
# ---------------------------------------------------------------------------


class TestRegistryAppHome:
    """SessionRegistry.list_active_by_user provides data for App Home."""

    async def test_list_active_by_user_scopes_correctly(self, tmp_path):
        """list_active_by_user returns only sessions for the given user."""
        db_path = tmp_path / "test-home.db"
        async with SessionRegistry(db_path=db_path) as reg:
            # Insert sessions for two users
            await reg.register(
                session_id="s1",
                pid=1,
                cwd="/tmp/a",
                name="session-1",
                model="opus",
            )
            await reg.update_status(
                "s1",
                "active",
                slack_channel_id="C1",
                authenticated_user_id="U_ALICE",
            )

            await reg.register(
                session_id="s2",
                pid=1,
                cwd="/tmp/b",
                name="session-2",
                model="sonnet",
            )
            await reg.update_status(
                "s2",
                "active",
                slack_channel_id="C2",
                authenticated_user_id="U_BOB",
            )

            alice_sessions = await reg.list_active_by_user("U_ALICE")
            assert len(alice_sessions) == 1
            assert alice_sessions[0]["session_id"] == "s1"

            bob_sessions = await reg.list_active_by_user("U_BOB")
            assert len(bob_sessions) == 1
            assert bob_sessions[0]["session_id"] == "s2"

            nobody_sessions = await reg.list_active_by_user("U_NOBODY")
            assert len(nobody_sessions) == 0
