"""Integration tests for interactive permission flows.

Tests the full permission cycle: debounced batch posting, action callback
simulation (approve/deny/approve-for-session), user authorization
enforcement, timeout auto-denial, session cache population, and
AskUserQuestion interactive flow.

Uses mock ThreadRouter — no real Slack connection needed. Tests exercise
PermissionHandler + EventDispatcher interaction logic with real asyncio
coordination (concurrent tasks, events, timeouts).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from conftest import make_test_config

from helpers import make_mock_slack_client
from summon_claude.sessions.permissions import PermissionHandler
from summon_claude.slack.client import MessageRef
from summon_claude.slack.router import ThreadRouter

pytestmark = pytest.mark.asyncio(loop_scope="module")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_permission_handler(
    debounce_ms=50, timeout_s=5, authenticated_user_id="U_OWNER", bridge=None
):
    """Create a PermissionHandler with mock ThreadRouter for testing."""
    client = make_mock_slack_client()
    # post_interactive needs to return a MessageRef for batch tracking
    client.post_interactive = AsyncMock(
        return_value=MessageRef(channel_id="C_TEST", ts="msg_ts_123")
    )
    client.delete_message = AsyncMock()
    router = ThreadRouter(client)
    config = make_test_config(
        permission_debounce_ms=debounce_ms,
        permission_timeout_s=timeout_s,
    )
    handler = PermissionHandler(
        router, config, authenticated_user_id=authenticated_user_id, bridge=bridge
    )
    # Bypass write gate — these tests exercise HITL flow, not the gate
    handler._check_write_gate = AsyncMock(return_value=None)
    return handler, client, router


def _extract_batch_id(mock_client, action_prefix="approve:"):
    """Extract batch_id from the last post_interactive call's blocks."""
    call_kwargs = mock_client.post_interactive.call_args
    # post_interactive is called as post_interactive(text, blocks=...) or
    # post_interactive(text, blocks)
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
    args = call_kwargs.args if call_kwargs.args else ()
    blocks = kwargs.get("blocks")
    if blocks is None and len(args) > 1:
        blocks = args[1]
    # Find the actions block
    for block in blocks or []:
        if block.get("type") == "actions":
            for element in block.get("elements", []):
                value = element.get("value", "")
                if value.startswith(action_prefix):
                    return value[len(action_prefix) :]
    raise ValueError(f"Could not extract batch_id with prefix {action_prefix!r} from {blocks!r}")


def _extract_request_id(mock_client):
    """Extract request_id from the last post_interactive AskUserQuestion blocks."""
    call_kwargs = mock_client.post_interactive.call_args
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
    args = call_kwargs.args if call_kwargs.args else ()
    blocks = kwargs.get("blocks") or (args[1] if len(args) > 1 else None)
    assert blocks is not None
    for block in blocks:
        if block.get("type") == "actions":
            for el in block.get("elements", []):
                val = el.get("value", "")
                parts = val.split("|")
                if len(parts) == 3:
                    return parts[0]
    raise ValueError(f"Could not extract request_id from blocks: {blocks!r}")


# ---------------------------------------------------------------------------
# Permission Prompt Tests
# ---------------------------------------------------------------------------


class TestPermissionPrompt:
    async def test_permission_prompt_posts_interactive(self):
        """handle() posts an interactive message after the debounce window."""
        handler, client, _ = _make_permission_handler(debounce_ms=50)

        task = asyncio.create_task(handler.handle("Bash", {"command": "ls"}, None))
        # Wait for debounce to fire
        await asyncio.sleep(0.15)

        client.post_interactive.assert_called_once()

        # Approve to unblock the pending handle() call
        batch_id = _extract_batch_id(client, "approve:")
        await handler.handle_action(f"approve:{batch_id}", "U_OWNER")
        await task

    async def test_permission_prompt_has_three_buttons(self):
        """The interactive message has Approve, Approve for Session, and Deny buttons."""
        handler, client, _ = _make_permission_handler(debounce_ms=50)

        task = asyncio.create_task(handler.handle("Bash", {"command": "echo hi"}, None))
        await asyncio.sleep(0.15)

        call_kwargs = client.post_interactive.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        args = call_kwargs.args if call_kwargs.args else ()
        blocks = kwargs.get("blocks") or (args[1] if len(args) > 1 else None)
        assert blocks is not None

        actions_block = next(b for b in blocks if b.get("type") == "actions")
        elements = actions_block["elements"]
        action_ids = [el["action_id"] for el in elements]

        assert "permission_approve" in action_ids
        assert "permission_approve_session" in action_ids
        assert "permission_deny" in action_ids
        assert len(elements) == 3

        # Cleanup
        batch_id = _extract_batch_id(client, "approve:")
        await handler.handle_action(f"approve:{batch_id}", "U_OWNER")
        await task


# ---------------------------------------------------------------------------
# Action Resolution Tests
# ---------------------------------------------------------------------------


class TestActionResolution:
    async def test_approve_action_resolves_allow(self):
        """Clicking Approve resolves handle() with PermissionResultAllow."""
        handler, client, _ = _make_permission_handler(debounce_ms=50)

        task = asyncio.create_task(handler.handle("Bash", {"command": "ls"}, None))
        await asyncio.sleep(0.15)

        batch_id = _extract_batch_id(client, "approve:")
        await handler.handle_action(f"approve:{batch_id}", "U_OWNER")
        result = await task

        assert isinstance(result, PermissionResultAllow)

    async def test_deny_action_resolves_deny(self):
        """Clicking Deny resolves handle() with PermissionResultDeny."""
        handler, client, _ = _make_permission_handler(debounce_ms=50)

        task = asyncio.create_task(handler.handle("Bash", {"command": "ls"}, None))
        await asyncio.sleep(0.15)

        batch_id = _extract_batch_id(client, "deny:")
        await handler.handle_action(f"deny:{batch_id}", "U_OWNER")
        result = await task

        assert isinstance(result, PermissionResultDeny)

    async def test_approve_deletes_interactive_message(self):
        """After approval, the interactive message is deleted."""
        handler, client, _ = _make_permission_handler(debounce_ms=50)

        task = asyncio.create_task(handler.handle("Bash", {"command": "ls"}, None))
        await asyncio.sleep(0.15)

        batch_id = _extract_batch_id(client, "approve:")
        await handler.handle_action(f"approve:{batch_id}", "U_OWNER")
        await task

        client.delete_message.assert_called_once_with("msg_ts_123")


# ---------------------------------------------------------------------------
# Authorization Enforcement
# ---------------------------------------------------------------------------


class TestAuthorizationEnforcement:
    async def test_reject_action_from_wrong_user(self):
        """Actions from the wrong user are silently dropped — batch stays unresolved."""
        handler, client, _ = _make_permission_handler(debounce_ms=50)

        task = asyncio.create_task(handler.handle("Bash", {"command": "ls"}, None))
        await asyncio.sleep(0.15)

        batch_id = _extract_batch_id(client, "approve:")
        # Wrong user — should have no effect
        await handler.handle_action(f"approve:{batch_id}", "U_WRONG")

        # Task should still be pending (not done)
        assert not task.done()

        # Now approve with correct user to unblock
        await handler.handle_action(f"approve:{batch_id}", "U_OWNER")
        result = await task
        assert isinstance(result, PermissionResultAllow)

    async def test_accept_action_from_authenticated_user(self):
        """Actions from the authenticated owner are accepted."""
        handler, client, _ = _make_permission_handler(debounce_ms=50)

        task = asyncio.create_task(handler.handle("Bash", {"command": "ls"}, None))
        await asyncio.sleep(0.15)

        batch_id = _extract_batch_id(client, "approve:")
        await handler.handle_action(f"approve:{batch_id}", "U_OWNER")
        result = await task

        assert isinstance(result, PermissionResultAllow)


# ---------------------------------------------------------------------------
# Session Cache Tests
# ---------------------------------------------------------------------------


class TestSessionCache:
    async def test_approve_for_session_caches_tool(self):
        """approve_session populates the session cache — subsequent call skips HITL."""
        handler, client, _ = _make_permission_handler(debounce_ms=50)

        # First request
        task = asyncio.create_task(handler.handle("SomeTool", {}, None))
        await asyncio.sleep(0.15)

        batch_id = _extract_batch_id(client, "approve_session:")
        await handler.handle_action(f"approve_session:{batch_id}", "U_OWNER")
        result = await task
        assert isinstance(result, PermissionResultAllow)

        # post_interactive called exactly once so far
        assert client.post_interactive.call_count == 1

        # Second request — should be cache-hit, no new interactive message
        result2 = await handler.handle("SomeTool", {}, None)
        assert isinstance(result2, PermissionResultAllow)

        # Still only one call — second was served from cache
        assert client.post_interactive.call_count == 1


# ---------------------------------------------------------------------------
# Timeout Tests
# ---------------------------------------------------------------------------


class TestTimeout:
    async def test_timeout_auto_denies(self):
        """When no action is taken before timeout, handle() returns PermissionResultDeny."""
        handler, _, _ = _make_permission_handler(debounce_ms=10, timeout_s=1)
        # Suppress timeout message posting — router not wired
        handler._post_timeout_message = AsyncMock()

        task = asyncio.create_task(handler.handle("Bash", {"command": "ls"}, None))
        result = await asyncio.wait_for(task, timeout=5.0)

        assert isinstance(result, PermissionResultDeny)
        handler._post_timeout_message.assert_called_once()


# ---------------------------------------------------------------------------
# AskUserQuestion Tests
# ---------------------------------------------------------------------------


class TestAskUserQuestion:
    async def test_ask_user_posts_question_blocks(self):
        """AskUserQuestion causes an interactive post with question content."""
        handler, client, _ = _make_permission_handler(debounce_ms=50)

        questions = [
            {
                "question": "Pick one?",
                "header": "Choice",
                "options": [
                    {"label": "A", "description": "Option A"},
                    {"label": "B", "description": "Option B"},
                ],
                "multiSelect": False,
            }
        ]

        task = asyncio.create_task(
            handler.handle("AskUserQuestion", {"questions": questions}, None)
        )
        await asyncio.sleep(0.1)

        client.post_interactive.assert_called_once()

        # Verify blocks contain an actions block with option buttons
        call_kwargs = client.post_interactive.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        args = call_kwargs.args if call_kwargs.args else ()
        blocks = kwargs.get("blocks") or (args[1] if len(args) > 1 else None)
        assert blocks is not None
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert action_blocks, "AskUserQuestion should produce an actions block"

        # Resolve by simulating option click
        request_id = _extract_request_id(client)
        assert request_id is not None, "Could not find request_id in blocks"
        await handler.handle_ask_user_action(
            value=f"{request_id}|0|0",
            user_id="U_OWNER",
        )
        await task

    async def test_ask_user_action_resolves_answer(self):
        """Clicking an AskUserQuestion option resolves handle() with the answer."""
        handler, client, _ = _make_permission_handler(debounce_ms=50)

        question_text = "Pick one?"
        questions = [
            {
                "question": question_text,
                "header": "Choice",
                "options": [
                    {"label": "A", "description": "Option A"},
                    {"label": "B", "description": "Option B"},
                ],
                "multiSelect": False,
            }
        ]

        task = asyncio.create_task(
            handler.handle("AskUserQuestion", {"questions": questions}, None)
        )
        await asyncio.sleep(0.1)

        # Extract request_id from block values
        request_id = _extract_request_id(client)
        assert request_id is not None

        # Click option 0 ("A") for question 0
        await handler.handle_ask_user_action(
            value=f"{request_id}|0|0",
            user_id="U_OWNER",
        )
        result = await task

        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input is not None
        answers = result.updated_input.get("answers", {})
        assert answers.get(question_text) == "A"
