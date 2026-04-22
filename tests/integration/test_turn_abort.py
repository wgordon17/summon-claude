"""Integration tests for turn abort mechanisms against real Slack.

Tests the !stop command path (CommandResult metadata), abort event
coordination with asyncio tasks, and reaction-based abort delivery
via real Socket Mode events. Full EventDispatcher routing coverage
lives in test_channel_reuse.py and tests/test_event_dispatcher.py.

Requires SUMMON_TEST_SLACK_BOT_TOKEN — skipped when credentials are absent.
"""

from __future__ import annotations

import asyncio
import secrets
from unittest.mock import MagicMock

import pytest

from summon_claude.event_dispatcher import EventDispatcher, SessionHandle
from summon_claude.sessions.commands import CommandContext, CommandResult, _handle_stop
from summon_claude.sessions.permissions import PermissionHandler
from tests.integration.conftest import EventConsumer

pytestmark = pytest.mark.asyncio(loop_scope="session")


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

        await abort_task
        turn_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn_task


@pytest.mark.slack
@pytest.mark.xdist_group("slack_socket")
class TestReactionAbort:
    """Reaction-based abort delivery via Socket Mode.

    Uses a dedicated consumer instead of the session-scoped ``event_consumer``
    fixture because socket resilience tests (which run earlier on the same
    worker) may leave the session-scoped consumer's connection in an
    unreliable state for ``reaction_added`` event delivery.
    """

    async def test_reaction_abort_via_socket_mode(
        self,
        slack_harness,
        test_channel,
        _slack_socket_lock,
        event_store,
    ):
        """Adding :octagonal_sign: reaction delivers reaction_added event via Socket Mode."""
        consumer = EventConsumer(
            bot_token=slack_harness.bot_token,
            app_token=slack_harness.app_token,
            signing_secret=slack_harness.signing_secret,
            event_store=event_store,
        )
        await asyncio.wait_for(consumer.start(), timeout=15.0)
        await asyncio.sleep(3.0)
        try:
            event_store.reset_reader()

            # Canary: verify events are flowing
            canary = f"canary-{secrets.token_hex(4)}"
            await slack_harness.client.chat_postMessage(channel=test_channel, text=canary)
            await consumer.wait_for_event(
                lambda e: e.get("type") == "message" and canary in e.get("text", ""),
                timeout=15.0,
            )
            consumer.drain()

            nonce = f"abort-target-{secrets.token_hex(6)}"
            resp = await slack_harness.client.chat_postMessage(
                channel=test_channel,
                text=nonce,
            )
            msg_ts = resp["ts"]

            # Consume the message event first
            await consumer.wait_for_event(
                lambda e: e.get("type") == "message" and nonce in e.get("text", ""),
                timeout=15.0,
            )

            await asyncio.sleep(1.0)

            # Add the abort reaction
            await slack_harness.client.reactions_add(
                channel=test_channel,
                name="octagonal_sign",
                timestamp=msg_ts,
            )

            # Verify reaction_added event arrives via Socket Mode
            event = await consumer.wait_for_event(
                lambda e: (
                    e.get("type") == "reaction_added" and e.get("item", {}).get("ts") == msg_ts
                ),
                timeout=15.0,
            )
            assert event["reaction"] == "octagonal_sign"
            assert event["item"]["channel"] == test_channel
        finally:
            await consumer.stop()

    async def test_dispatch_reaction_triggers_abort_callback(
        self,
        slack_harness,
        test_channel,
        _slack_socket_lock,
        event_store,
    ):
        """Full round-trip: post → react → event → dispatch → abort fires."""
        bot_user_id = await slack_harness.resolve_bot_user_id()
        consumer = EventConsumer(
            bot_token=slack_harness.bot_token,
            app_token=slack_harness.app_token,
            signing_secret=slack_harness.signing_secret,
            event_store=event_store,
        )
        await asyncio.wait_for(consumer.start(), timeout=15.0)
        await asyncio.sleep(3.0)
        try:
            event_store.reset_reader()

            # Canary: verify events are flowing
            canary = f"canary-{secrets.token_hex(4)}"
            await slack_harness.client.chat_postMessage(channel=test_channel, text=canary)
            await consumer.wait_for_event(
                lambda e: e.get("type") == "message" and canary in e.get("text", ""),
                timeout=15.0,
            )
            consumer.drain()

            dispatcher = EventDispatcher()
            abort_event = asyncio.Event()

            def _abort() -> None:
                abort_event.set()

            handle = SessionHandle(
                session_id="test-abort-rt",
                channel_id=test_channel,
                message_queue=asyncio.Queue(maxsize=10),
                permission_handler=MagicMock(spec=PermissionHandler),
                abort_callback=_abort,
                authenticated_user_id=bot_user_id,
                pending_turns=asyncio.Queue(),
            )
            dispatcher.register(test_channel, handle)

            nonce = f"abort-dispatch-{secrets.token_hex(6)}"
            resp = await slack_harness.client.chat_postMessage(
                channel=test_channel,
                text=nonce,
            )
            msg_ts = resp["ts"]

            # Consume the message event
            await consumer.wait_for_event(
                lambda e: e.get("type") == "message" and nonce in e.get("text", ""),
                timeout=15.0,
            )

            await asyncio.sleep(1.0)

            # Add abort reaction
            await slack_harness.client.reactions_add(
                channel=test_channel,
                name="octagonal_sign",
                timestamp=msg_ts,
            )

            # Capture the reaction event from Socket Mode
            event = await consumer.wait_for_event(
                lambda e: (
                    e.get("type") == "reaction_added" and e.get("item", {}).get("ts") == msg_ts
                ),
                timeout=15.0,
            )

            # Feed the real event through the dispatcher
            await dispatcher.dispatch_reaction(event)

            assert abort_event.is_set(), "abort callback should have fired"

            # Cleanup
            dispatcher.unregister(test_channel)
        finally:
            await consumer.stop()


class TestReactionAuthorization:
    """Pure-logic abort authorization tests — no Slack credentials needed."""

    async def test_reaction_from_non_owner_ignored(self):
        """Reactions from non-owner users do not trigger abort."""
        channel_id = "C_TEST_CHANNEL"
        dispatcher = EventDispatcher()
        abort_event = asyncio.Event()

        handle = SessionHandle(
            session_id="test-abort-nouser",
            channel_id=channel_id,
            message_queue=asyncio.Queue(maxsize=10),
            permission_handler=MagicMock(spec=PermissionHandler),
            abort_callback=abort_event.set,
            authenticated_user_id="U_REAL_OWNER",
            pending_turns=asyncio.Queue(),
        )
        dispatcher.register(channel_id, handle)

        # Dispatch a reaction from a different user
        await dispatcher.dispatch_reaction(
            {
                "user": "U_INTRUDER",
                "reaction": "octagonal_sign",
                "item": {"channel": channel_id, "ts": "123.456"},
            }
        )

        assert not abort_event.is_set(), "abort should NOT fire for non-owner"

        dispatcher.unregister(channel_id)
