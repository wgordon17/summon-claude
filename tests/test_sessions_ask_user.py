"""Tests for AskUserQuestion handling in summon_claude.permissions."""

from __future__ import annotations

import asyncio

from claude_agent_sdk import PermissionResultAllow

from helpers import make_mock_provider
from summon_claude.config import SummonConfig
from summon_claude.sessions.permissions import (
    PermissionHandler,
    _build_ask_user_blocks,
)
from summon_claude.slack.router import ThreadRouter


def _make_config():
    return SummonConfig.model_validate(
        {
            "slack_bot_token": "xoxb-t",
            "slack_app_token": "xapp-t",
            "slack_signing_secret": "s",
            "permission_debounce_ms": 10,
        }
    )


def _make_handler():
    provider = make_mock_provider()
    router = ThreadRouter(provider, "C123")
    config = _make_config()
    return PermissionHandler(router, config, authenticated_user_id="U1"), provider, router


def _get_actions_block(blocks: list[dict], idx: int = 0) -> dict:
    """Return the Nth actions block from a list of Slack blocks."""
    return [b for b in blocks if b["type"] == "actions"][idx]


def _extract_request_id(provider) -> str:
    """Extract the request_id from the last posted AskUserQuestion message."""
    # Check ephemeral messages first (new path), then post_message (fallback)
    for call in provider.post_ephemeral.call_args_list:
        blocks = call.kwargs.get("blocks")
        if blocks:
            for b in blocks:
                if b["type"] == "actions":
                    return b["elements"][0]["value"].split("|")[0]
    for call in provider.post_message.call_args_list:
        blocks = call.kwargs.get("blocks")
        if blocks:
            for b in blocks:
                if b["type"] == "actions":
                    return b["elements"][0]["value"].split("|")[0]
    msg = "No ask_user blocks found in posted messages"
    raise AssertionError(msg)


async def _start_ask(handler, provider, questions):
    """Start an AskUserQuestion and return (task, request_id)."""
    task = asyncio.create_task(handler.handle("AskUserQuestion", {"questions": questions}, None))
    await asyncio.sleep(0.05)
    request_id = _extract_request_id(provider)
    return task, request_id


async def _click(handler, request_id, q_idx, opt_val):
    """Simulate a button click."""
    await handler.handle_ask_user_action(
        value=f"{request_id}|{q_idx}|{opt_val}",
        user_id="U1",
    )


# ------------------------------------------------------------------
# Block rendering
# ------------------------------------------------------------------


class TestBuildAskUserBlocks:
    def test_single_question_produces_section_and_actions(self):
        questions = [
            {
                "question": "Which DB?",
                "header": "Database",
                "options": [
                    {"label": "PostgreSQL", "description": "Relational DB"},
                    {"label": "MongoDB", "description": "Document DB"},
                ],
                "multiSelect": False,
            }
        ]
        blocks = _build_ask_user_blocks("req-123", questions)
        assert any(b["type"] == "section" for b in blocks)
        actions = _get_actions_block(blocks)
        # 2 options + 1 Other = 3 buttons
        assert len(actions["elements"]) == 3

    def test_other_button_always_present(self):
        questions = [
            {
                "question": "Pick one",
                "header": "Test",
                "options": [{"label": "A", "description": ""}],
                "multiSelect": False,
            }
        ]
        blocks = _build_ask_user_blocks("req-1", questions)
        actions = _get_actions_block(blocks)
        other_btns = [e for e in actions["elements"] if e["text"]["text"] == "Other"]
        assert len(other_btns) == 1

    def test_multi_select_adds_done_button(self):
        questions = [
            {
                "question": "Select features",
                "header": "Features",
                "options": [
                    {"label": "Auth", "description": ""},
                    {"label": "Logging", "description": ""},
                ],
                "multiSelect": True,
            }
        ]
        blocks = _build_ask_user_blocks("req-ms", questions)
        actions = _get_actions_block(blocks)
        done_btns = [e for e in actions["elements"] if e["text"]["text"] == "Done"]
        assert len(done_btns) == 1

    def test_multi_select_hint_in_question_text(self):
        questions = [
            {
                "question": "Select items",
                "header": "Items",
                "options": [{"label": "X", "description": ""}],
                "multiSelect": True,
            }
        ]
        blocks = _build_ask_user_blocks("req-ms2", questions)
        sections = [b for b in blocks if b["type"] == "section" and "Select" in b["text"]["text"]]
        assert any("multiple" in b["text"]["text"] for b in sections)

    def test_markdown_preview_rendered_as_code_block(self):
        questions = [
            {
                "question": "Pick a layout",
                "header": "Layout",
                "options": [
                    {
                        "label": "Grid",
                        "description": "CSS grid layout",
                        "markdown": "+---+---+\n| A | B |\n+---+---+",
                    },
                ],
                "multiSelect": False,
            }
        ]
        blocks = _build_ask_user_blocks("req-md", questions)
        ctx = [b for b in blocks if b["type"] == "context"]
        assert len(ctx) >= 1
        text = ctx[0]["elements"][0]["text"]
        assert "```" in text
        assert "| A | B |" in text

    def test_markdown_preview_escapes_backticks(self):
        """Backticks in preview content must not break the wrapping code block."""
        questions = [
            {
                "question": "Pick",
                "header": "H",
                "options": [
                    {
                        "label": "Code",
                        "description": "",
                        "markdown": "```python\nprint('hi')\n```",
                    },
                ],
                "multiSelect": False,
            }
        ]
        blocks = _build_ask_user_blocks("req-esc", questions)
        ctx = [b for b in blocks if b["type"] == "context"]
        assert len(ctx) >= 1
        text = ctx[0]["elements"][0]["text"]
        # Only the wrapper's ``` should remain as actual backticks.
        assert text.count("```") == 2

    def test_descriptions_rendered_as_context(self):
        questions = [
            {
                "question": "Choose",
                "header": "H",
                "options": [
                    {"label": "A", "description": "Alpha option"},
                    {"label": "B", "description": "Beta option"},
                ],
                "multiSelect": False,
            }
        ]
        blocks = _build_ask_user_blocks("req-d", questions)
        ctx = [b for b in blocks if b["type"] == "context"]
        assert len(ctx) >= 1
        text = ctx[0]["elements"][0]["text"]
        assert "Alpha option" in text
        assert "Beta option" in text

    def test_multiple_questions_produce_multiple_action_blocks(self):
        questions = [
            {
                "question": "Q1?",
                "header": "H1",
                "options": [{"label": "A", "description": ""}],
                "multiSelect": False,
            },
            {
                "question": "Q2?",
                "header": "H2",
                "options": [{"label": "B", "description": ""}],
                "multiSelect": False,
            },
        ]
        blocks = _build_ask_user_blocks("req-mq", questions)
        actions = [b for b in blocks if b["type"] == "actions"]
        assert len(actions) == 2

    def test_button_values_encode_request_id(self):
        questions = [
            {
                "question": "Q?",
                "header": "H",
                "options": [{"label": "X", "description": ""}],
                "multiSelect": False,
            }
        ]
        blocks = _build_ask_user_blocks("abc-def-123", questions)
        actions = _get_actions_block(blocks)
        first_btn = actions["elements"][0]
        assert first_btn["value"].startswith("abc-def-123|")


# ------------------------------------------------------------------
# Single-select flow
# ------------------------------------------------------------------


class TestSingleSelect:
    async def test_single_select_returns_answer(self):
        handler, provider, _ = _make_handler()
        questions = [
            {
                "question": "Which DB?",
                "header": "Database",
                "options": [
                    {"label": "PostgreSQL", "description": ""},
                    {"label": "MongoDB", "description": ""},
                ],
                "multiSelect": False,
            }
        ]
        task, req_id = await _start_ask(handler, provider, questions)

        await _click(handler, req_id, 0, 0)

        result = await asyncio.wait_for(task, timeout=2.0)
        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input["answers"]["Which DB?"] == "PostgreSQL"

    async def test_empty_questions_returns_allow(self):
        handler, _, _ = _make_handler()
        result = await handler.handle("AskUserQuestion", {"questions": []}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_two_questions_requires_both_answers(self):
        handler, provider, _ = _make_handler()
        questions = [
            {
                "question": "Q1?",
                "header": "H1",
                "options": [{"label": "A", "description": ""}],
                "multiSelect": False,
            },
            {
                "question": "Q2?",
                "header": "H2",
                "options": [{"label": "B", "description": ""}],
                "multiSelect": False,
            },
        ]
        task, req_id = await _start_ask(handler, provider, questions)

        await _click(handler, req_id, 0, 0)
        assert not task.done()

        await _click(handler, req_id, 1, 0)

        result = await asyncio.wait_for(task, timeout=2.0)
        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input["answers"]["Q1?"] == "A"
        assert result.updated_input["answers"]["Q2?"] == "B"


# ------------------------------------------------------------------
# Other (free-text) flow
# ------------------------------------------------------------------


class TestOtherTextInput:
    async def test_other_sets_pending_flag(self):
        handler, provider, _ = _make_handler()
        questions = [
            {
                "question": "Pick one",
                "header": "Test",
                "options": [{"label": "A", "description": ""}],
                "multiSelect": False,
            }
        ]
        task, req_id = await _start_ask(handler, provider, questions)

        assert not handler.has_pending_text_input()

        await _click(handler, req_id, 0, "other")
        assert handler.has_pending_text_input()

        await handler.receive_text_input("My custom answer")
        assert not handler.has_pending_text_input()

        result = await asyncio.wait_for(task, timeout=2.0)
        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input["answers"]["Pick one"] == "My custom answer"


# ------------------------------------------------------------------
# Multi-select flow
# ------------------------------------------------------------------


class TestMultiSelect:
    async def test_multi_select_toggle_and_done(self):
        handler, provider, _ = _make_handler()
        questions = [
            {
                "question": "Select features",
                "header": "Features",
                "options": [
                    {"label": "Auth", "description": ""},
                    {"label": "Logging", "description": ""},
                    {"label": "Metrics", "description": ""},
                ],
                "multiSelect": True,
            }
        ]
        task, req_id = await _start_ask(handler, provider, questions)

        await _click(handler, req_id, 0, 0)  # Auth
        await _click(handler, req_id, 0, 2)  # Metrics
        assert not task.done()

        await _click(handler, req_id, 0, "done")

        result = await asyncio.wait_for(task, timeout=2.0)
        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input["answers"]["Select features"] == "Auth, Metrics"

    async def test_multi_select_toggle_deselect(self):
        handler, provider, _ = _make_handler()
        questions = [
            {
                "question": "Q?",
                "header": "H",
                "options": [
                    {"label": "A", "description": ""},
                    {"label": "B", "description": ""},
                ],
                "multiSelect": True,
            }
        ]
        task, req_id = await _start_ask(handler, provider, questions)

        # Select A, deselect A, select B, done
        await _click(handler, req_id, 0, 0)
        await _click(handler, req_id, 0, 0)
        await _click(handler, req_id, 0, 1)
        await _click(handler, req_id, 0, "done")

        result = await asyncio.wait_for(task, timeout=2.0)
        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input["answers"]["Q?"] == "B"


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------


class TestEdgeCases:
    async def test_unknown_request_id_ignored(self):
        handler, _, _ = _make_handler()
        await _click(handler, "nonexistent", 0, 0)

    async def test_invalid_value_format_ignored(self):
        handler, _, _ = _make_handler()
        await handler.handle_ask_user_action(
            value="bad-format",
            user_id="U1",
        )

    async def test_receive_text_without_pending_is_noop(self):
        handler, _, _ = _make_handler()
        await handler.receive_text_input("stray text")
        assert not handler.has_pending_text_input()

    async def test_ask_user_not_auto_approved(self):
        """AskUserQuestion should not go through auto-approve flow."""
        handler, provider, _ = _make_handler()
        questions = [
            {
                "question": "Q?",
                "header": "H",
                "options": [{"label": "A", "description": ""}],
                "multiSelect": False,
            }
        ]
        task, req_id = await _start_ask(handler, provider, questions)

        # Verify it posted question blocks, not permission buttons
        posted = False
        for call in provider.post_ephemeral.call_args_list:
            blocks = call.kwargs.get("blocks")
            if blocks and any(
                b.get("type") == "section"
                and "question" in b.get("text", {}).get("text", "").lower()
                for b in blocks
            ):
                posted = True
                break
        assert posted, "Expected AskUserQuestion blocks"

        # Clean up
        await _click(handler, req_id, 0, 0)
        await asyncio.wait_for(task, timeout=2.0)
