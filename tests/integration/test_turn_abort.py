"""Integration tests for turn abort mechanisms against real Slack.

Tests the !stop command path (CommandResult metadata), abort event
coordination with asyncio tasks, and reaction-based abort delivery
via real Socket Mode events. Full EventDispatcher routing coverage
lives in test_channel_reuse.py and tests/test_event_dispatcher.py.

Requires SUMMON_TEST_SLACK_BOT_TOKEN — skipped when credentials are absent.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from summon_claude.event_dispatcher import EventDispatcher, SessionHandle
from summon_claude.sessions.commands import CommandContext, CommandResult, _handle_stop
from summon_claude.sessions.permissions import PermissionHandler
from tests.integration.conftest import EventConsumer, SlackTestHarness

pytestmark = pytest.mark.asyncio(loop_scope="module")


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def slack_harness(_slack_socket_lock):
    """Module-scoped harness — skips if credentials not set."""
    if not os.environ.get("SUMMON_TEST_SLACK_BOT_TOKEN"):
        pytest.skip("SUMMON_TEST_SLACK_BOT_TOKEN not set")
    harness = SlackTestHarness()
    await harness.resolve_bot_user_id()
    yield harness


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def test_channel(slack_harness):
    """Module-scoped test channel for abort tests."""
    channel_id = await slack_harness.create_test_channel(prefix="abort")
    yield channel_id


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def event_consumer(slack_harness, test_channel):
    """Module-scoped Socket Mode consumer for reaction event capture."""
    consumer = EventConsumer(
        bot_token=slack_harness.bot_token,
        app_token=slack_harness.app_token,
        signing_secret=slack_harness.signing_secret,
    )
    try:
        await asyncio.wait_for(consumer.start(), timeout=25.0)
    except TimeoutError:
        pytest.skip("Socket Mode connection timed out")
    except Exception as exc:
        await consumer.stop()
        pytest.skip(f"Socket Mode connection failed: {exc}")

    yield consumer
    await consumer.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


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
    async def test_reaction_abort_via_socket_mode(
        self,
        slack_harness,
        test_channel,
        event_consumer,
    ):
        """Adding :octagonal_sign: reaction delivers reaction_added event via Socket Mode.

        Posts a message, adds the abort reaction via API, and verifies
        the reaction_added event arrives through the Socket Mode consumer.
        This validates the full Slack round-trip for the abort signal path.
        """
        nonce = f"abort-target-{secrets.token_hex(6)}"
        resp = await slack_harness.client.chat_postMessage(
            channel=test_channel,
            text=nonce,
        )
        msg_ts = resp["ts"]

        # Consume the message event first
        await event_consumer.wait_for_event(
            lambda e: e.get("type") == "message" and nonce in e.get("text", ""),
            timeout=10.0,
        )

        # Add the abort reaction
        await slack_harness.client.reactions_add(
            channel=test_channel,
            name="octagonal_sign",
            timestamp=msg_ts,
        )

        # Verify reaction_added event arrives via Socket Mode
        event = await event_consumer.wait_for_event(
            lambda e: e.get("type") == "reaction_added" and e.get("item", {}).get("ts") == msg_ts,
            timeout=10.0,
        )
        assert event["reaction"] == "octagonal_sign"
        assert event["item"]["channel"] == test_channel

    async def test_dispatch_reaction_triggers_abort_callback(
        self,
        slack_harness,
        test_channel,
        event_consumer,
    ):
        """Full round-trip: post → react → event → dispatch → abort fires.

        Posts a real message to Slack, adds :octagonal_sign: via API,
        captures the reaction_added event via Socket Mode, then feeds
        it through EventDispatcher to verify the abort callback fires.
        """
        bot_user_id = await slack_harness.resolve_bot_user_id()

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
        )
        dispatcher.register(test_channel, handle)

        nonce = f"abort-dispatch-{secrets.token_hex(6)}"
        resp = await slack_harness.client.chat_postMessage(
            channel=test_channel,
            text=nonce,
        )
        msg_ts = resp["ts"]

        # Consume the message event
        await event_consumer.wait_for_event(
            lambda e: e.get("type") == "message" and nonce in e.get("text", ""),
            timeout=10.0,
        )

        # Add abort reaction
        await slack_harness.client.reactions_add(
            channel=test_channel,
            name="octagonal_sign",
            timestamp=msg_ts,
        )

        # Capture the reaction event from Socket Mode
        event = await event_consumer.wait_for_event(
            lambda e: e.get("type") == "reaction_added" and e.get("item", {}).get("ts") == msg_ts,
            timeout=10.0,
        )

        # Feed the real event through the dispatcher
        await dispatcher.dispatch_reaction(event)

        assert abort_event.is_set(), "abort callback should have fired"

        # Cleanup: unregister to avoid interference with other tests
        dispatcher.unregister(test_channel)

    async def test_reaction_from_non_owner_ignored(
        self,
        slack_harness,
        test_channel,
    ):
        """Reactions from non-owner users do not trigger abort."""
        dispatcher = EventDispatcher()
        abort_event = asyncio.Event()

        handle = SessionHandle(
            session_id="test-abort-nouser",
            channel_id=test_channel,
            message_queue=asyncio.Queue(maxsize=10),
            permission_handler=MagicMock(spec=PermissionHandler),
            abort_callback=abort_event.set,
            authenticated_user_id="U_REAL_OWNER",
        )
        dispatcher.register(test_channel, handle)

        # Dispatch a reaction from a different user
        await dispatcher.dispatch_reaction(
            {
                "user": "U_INTRUDER",
                "reaction": "octagonal_sign",
                "item": {"channel": test_channel, "ts": "123.456"},
            }
        )

        assert not abort_event.is_set(), "abort should NOT fire for non-owner"

        dispatcher.unregister(test_channel)
