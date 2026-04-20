"""Integration tests for interactive permission flows against real Slack.

Tests the full permission cycle: interactive message posting via real Slack
API, action callback resolution (approve/deny/approve-for-session), user
authorization enforcement, timeout auto-denial, session cache population,
and AskUserQuestion interactive flow.

Requires SUMMON_TEST_SLACK_BOT_TOKEN — skipped when credentials are absent.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from conftest import make_test_config

from summon_claude.sessions.permissions import PermissionHandler
from summon_claude.slack.client import SlackClient
from summon_claude.slack.router import ThreadRouter
from tests.integration.conftest import SlackTestHarness

pytestmark = [
    pytest.mark.slack,
    pytest.mark.asyncio(loop_scope="module"),
]


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def slack_harness():
    """Module-scoped harness — skips if credentials not set."""
    if not os.environ.get("SUMMON_TEST_SLACK_BOT_TOKEN"):
        pytest.skip("SUMMON_TEST_SLACK_BOT_TOKEN not set")
    harness = SlackTestHarness()
    await harness.resolve_bot_user_id()
    yield harness


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def test_channel(slack_harness):
    """Module-scoped test channel for permission tests."""
    channel_id = await slack_harness.create_test_channel(prefix="perm")
    yield channel_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_permission_handler(
    slack_client,
    debounce_ms=50,
    timeout_s=5,
    authenticated_user_id="U_OWNER",
):
    """Create a PermissionHandler with real SlackClient for testing."""
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
    # Bypass write gate — these tests exercise HITL flow, not the gate
    from unittest.mock import AsyncMock

    handler._check_write_gate = AsyncMock(return_value=None)
    return handler


async def _extract_batch_id_from_channel(
    web_client,
    channel_id,
    action_prefix="approve:",
):
    """Extract batch_id from the most recent interactive message in the channel."""
    history = await web_client.conversations_history(
        channel=channel_id,
        limit=5,
    )
    for msg in history.get("messages", []):
        blocks = msg.get("blocks") or []
        for block in blocks:
            if block.get("type") == "actions":
                for element in block.get("elements", []):
                    value = element.get("value", "")
                    if value.startswith(action_prefix):
                        return value[len(action_prefix) :]
    raise ValueError(
        f"Could not extract batch_id with prefix {action_prefix!r} from channel {channel_id}"
    )


async def _extract_request_id_from_channel(web_client, channel_id):
    """Extract request_id from the most recent AskUserQuestion message."""
    history = await web_client.conversations_history(
        channel=channel_id,
        limit=5,
    )
    for msg in history.get("messages", []):
        blocks = msg.get("blocks") or []
        for block in blocks:
            if block.get("type") == "actions":
                for el in block.get("elements", []):
                    val = el.get("value", "")
                    parts = val.split("|")
                    if len(parts) == 3:
                        return parts[0]
    raise ValueError(f"Could not extract request_id from channel {channel_id}")


async def _get_latest_message(web_client, channel_id):
    """Get the most recent message from a channel."""
    history = await web_client.conversations_history(
        channel=channel_id,
        limit=1,
    )
    messages = history.get("messages", [])
    return messages[0] if messages else None


# ---------------------------------------------------------------------------
# Permission Prompt Tests
# ---------------------------------------------------------------------------


class TestPermissionPrompt:
    async def test_permission_prompt_posts_to_channel(
        self,
        slack_harness,
        test_channel,
    ):
        """handle() posts an interactive message to the real Slack channel."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(slack_client, debounce_ms=50)

        task = asyncio.create_task(
            handler.handle("Bash", {"command": "ls"}, None),
        )
        await asyncio.sleep(0.3)

        # Verify a message with action blocks appeared in the channel
        msg = await _get_latest_message(slack_harness.client, test_channel)
        assert msg is not None
        blocks = msg.get("blocks") or []
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert action_blocks, "Interactive message should have actions block"

        # Approve to unblock
        batch_id = await _extract_batch_id_from_channel(
            slack_harness.client,
            test_channel,
            "approve:",
        )
        await handler.handle_action(f"approve:{batch_id}", "U_OWNER")
        await task

    async def test_permission_prompt_has_three_buttons(
        self,
        slack_harness,
        test_channel,
    ):
        """The interactive message has Approve, Approve for Session, and Deny buttons."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(slack_client, debounce_ms=50)

        task = asyncio.create_task(
            handler.handle("Bash", {"command": "echo hi"}, None),
        )
        await asyncio.sleep(0.3)

        msg = await _get_latest_message(slack_harness.client, test_channel)
        assert msg is not None
        blocks = msg.get("blocks") or []
        actions_block = next(
            (b for b in blocks if b.get("type") == "actions"),
            None,
        )
        assert actions_block is not None

        elements = actions_block["elements"]
        action_ids = [el["action_id"] for el in elements]
        assert "permission_approve" in action_ids
        assert "permission_approve_session" in action_ids
        assert "permission_deny" in action_ids
        assert len(elements) == 3

        # Cleanup
        batch_id = await _extract_batch_id_from_channel(
            slack_harness.client,
            test_channel,
            "approve:",
        )
        await handler.handle_action(f"approve:{batch_id}", "U_OWNER")
        await task


# ---------------------------------------------------------------------------
# Action Resolution Tests
# ---------------------------------------------------------------------------


class TestActionResolution:
    async def test_approve_action_resolves_allow(
        self,
        slack_harness,
        test_channel,
    ):
        """Clicking Approve resolves handle() with PermissionResultAllow."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(slack_client, debounce_ms=50)

        task = asyncio.create_task(
            handler.handle("Bash", {"command": "ls"}, None),
        )
        await asyncio.sleep(0.3)

        batch_id = await _extract_batch_id_from_channel(
            slack_harness.client,
            test_channel,
            "approve:",
        )
        await handler.handle_action(f"approve:{batch_id}", "U_OWNER")
        result = await task

        assert isinstance(result, PermissionResultAllow)

    async def test_deny_action_resolves_deny(
        self,
        slack_harness,
        test_channel,
    ):
        """Clicking Deny resolves handle() with PermissionResultDeny."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(slack_client, debounce_ms=50)

        task = asyncio.create_task(
            handler.handle("Bash", {"command": "ls"}, None),
        )
        await asyncio.sleep(0.3)

        batch_id = await _extract_batch_id_from_channel(
            slack_harness.client,
            test_channel,
            "deny:",
        )
        await handler.handle_action(f"deny:{batch_id}", "U_OWNER")
        result = await task

        assert isinstance(result, PermissionResultDeny)

    async def test_approve_deletes_interactive_message(
        self,
        slack_harness,
        test_channel,
    ):
        """After approval, the interactive message is deleted from the channel."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(slack_client, debounce_ms=50)

        task = asyncio.create_task(
            handler.handle("Bash", {"command": "ls"}, None),
        )
        await asyncio.sleep(0.3)

        # Capture the message ts before approval
        msg = await _get_latest_message(slack_harness.client, test_channel)
        assert msg is not None
        msg_ts = msg["ts"]

        batch_id = await _extract_batch_id_from_channel(
            slack_harness.client,
            test_channel,
            "approve:",
        )
        await handler.handle_action(f"approve:{batch_id}", "U_OWNER")
        await task

        # Give Slack a moment to process the deletion
        await asyncio.sleep(0.5)

        # The message should be deleted — latest message should be different
        latest = await _get_latest_message(slack_harness.client, test_channel)
        if latest is not None:
            assert latest["ts"] != msg_ts, "Interactive message should have been deleted"


# ---------------------------------------------------------------------------
# Authorization Enforcement
# ---------------------------------------------------------------------------


class TestAuthorizationEnforcement:
    async def test_reject_action_from_wrong_user(
        self,
        slack_harness,
        test_channel,
    ):
        """Actions from the wrong user are silently dropped — batch stays unresolved."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(slack_client, debounce_ms=50)

        task = asyncio.create_task(
            handler.handle("Bash", {"command": "ls"}, None),
        )
        await asyncio.sleep(0.3)

        batch_id = await _extract_batch_id_from_channel(
            slack_harness.client,
            test_channel,
            "approve:",
        )
        # Wrong user — should have no effect
        await handler.handle_action(f"approve:{batch_id}", "U_INTRUDER")

        # Task should still be pending (not resolved)
        assert not task.done(), "Permission should not resolve for wrong user"

        # Now approve from the correct user to unblock
        await handler.handle_action(f"approve:{batch_id}", "U_OWNER")
        result = await task
        assert isinstance(result, PermissionResultAllow)

    async def test_accept_action_from_authenticated_user(
        self,
        slack_harness,
        test_channel,
    ):
        """Actions from the authenticated user resolve the batch."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(
            slack_client,
            debounce_ms=50,
            authenticated_user_id="U_AUTH",
        )

        task = asyncio.create_task(
            handler.handle("Bash", {"command": "ls"}, None),
        )
        await asyncio.sleep(0.3)

        batch_id = await _extract_batch_id_from_channel(
            slack_harness.client,
            test_channel,
            "approve:",
        )
        await handler.handle_action(f"approve:{batch_id}", "U_AUTH")
        result = await task

        assert isinstance(result, PermissionResultAllow)


# ---------------------------------------------------------------------------
# Session Approve
# ---------------------------------------------------------------------------


class TestSessionApprove:
    async def test_session_approve_caches_for_tool(
        self,
        slack_harness,
        test_channel,
    ):
        """approve_session caches the tool — second call auto-approves."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(slack_client, debounce_ms=50)

        # First call — posts interactive message
        task1 = asyncio.create_task(
            handler.handle("Bash", {"command": "ls"}, None),
        )
        await asyncio.sleep(0.3)

        batch_id = await _extract_batch_id_from_channel(
            slack_harness.client,
            test_channel,
            "approve_session:",
        )
        await handler.handle_action(f"approve_session:{batch_id}", "U_OWNER")
        result1 = await task1
        assert isinstance(result1, PermissionResultAllow)

        # Second call for same tool — should auto-approve without posting
        result2 = await handler.handle("Bash", {"command": "echo hello"}, None)
        assert isinstance(result2, PermissionResultAllow)


# ---------------------------------------------------------------------------
# Permission Timeout
# ---------------------------------------------------------------------------


class TestPermissionTimeout:
    async def test_permission_timeout_auto_denies(
        self,
        slack_harness,
        test_channel,
    ):
        """Unanswered permission requests auto-deny after timeout."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(
            slack_client,
            debounce_ms=50,
            timeout_s=1,
        )

        result = await handler.handle("Bash", {"command": "ls"}, None)

        assert isinstance(result, PermissionResultDeny)


# ---------------------------------------------------------------------------
# AskUserQuestion
# ---------------------------------------------------------------------------


class TestAskUserQuestion:
    async def test_ask_user_posts_question_blocks(
        self,
        slack_harness,
        test_channel,
    ):
        """_handle_ask_user_question posts interactive blocks with options."""
        slack_client = SlackClient(slack_harness.client, test_channel)
        handler = _make_permission_handler(slack_client, debounce_ms=50)

        questions = [
            {
                "question": "Pick one?",
                "options": [
                    {"value": "a", "label": "Choice A"},
                    {"value": "b", "label": "Choice B"},
                ],
            }
        ]
        task = asyncio.create_task(
            handler.handle("AskUserQuestion", {"questions": questions}, None),
        )
        await asyncio.sleep(0.3)

        # Verify blocks contain an actions block with option buttons
        msg = await _get_latest_message(slack_harness.client, test_channel)
        assert msg is not None
        blocks = msg.get("blocks") or []
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert action_blocks, "AskUserQuestion should produce an actions block"

        # Resolve by simulating option click
        request_id = await _extract_request_id_from_channel(
            slack_harness.client,
            test_channel,
        )
        await handler.handle_action(f"{request_id}|0|a", "U_OWNER")
        result = await task

        assert isinstance(result, PermissionResultAllow)
