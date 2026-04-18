"""Real SDK integration tests — spawn fresh Claude Code sessions.

Skipped automatically when Claude Code CLI is not installed. Safe to run
inside a Claude Code session — the CLAUDECODE env var is stripped from
child subprocesses via an autouse fixture. Run with::

    uv run pytest tests/test_sdk_integration.py -m llm -v
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ThinkingConfigAdaptive,
    ThinkingConfigDisabled,
)

from summon_claude.sessions.commands import COMMAND_ACTIONS

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(
        shutil.which("claude") is None,
        reason="Claude Code CLI not installed",
    ),
]


# Common options applied to all SDK sessions.
# Must match session.py's ClaudeAgentOptions configuration.
_COMMON_OPTS = {"setting_sources": ["user", "project"]}


async def test_basic_query_and_response():
    """Send a simple query and verify we get AssistantMessage + ResultMessage."""
    with tempfile.TemporaryDirectory() as cwd:
        options = ClaudeAgentOptions(cwd=cwd, max_turns=1, **_COMMON_OPTS)
        async with ClaudeSDKClient(options) as client:
            await client.query("Reply with exactly: SUMMON_TEST_OK")
            messages = []
            async for msg in client.receive_response():
                messages.append(msg)

    assert any(isinstance(m, AssistantMessage) for m in messages), "Expected AssistantMessage"
    assert any(isinstance(m, ResultMessage) for m in messages), "Expected ResultMessage"


async def test_result_message_has_cost():
    """ResultMessage should expose total_cost_usd and num_turns."""
    with tempfile.TemporaryDirectory() as cwd:
        options = ClaudeAgentOptions(cwd=cwd, max_turns=1, **_COMMON_OPTS)
        async with ClaudeSDKClient(options) as client:
            await client.query("What is 2 + 2?")
            result_msg = None
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    result_msg = msg

    assert result_msg is not None
    assert hasattr(result_msg, "total_cost_usd"), "ResultMessage should have total_cost_usd"
    assert hasattr(result_msg, "num_turns"), "ResultMessage should have num_turns"
    assert isinstance(result_msg.total_cost_usd, float | int)
    assert isinstance(result_msg.num_turns, int)


async def test_text_block_content():
    """AssistantMessage should contain at least one TextBlock with non-empty text."""
    with tempfile.TemporaryDirectory() as cwd:
        options = ClaudeAgentOptions(cwd=cwd, max_turns=1, **_COMMON_OPTS)
        async with ClaudeSDKClient(options) as client:
            await client.query("Say hello.")
            assistant_msg = None
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    assistant_msg = msg

    assert assistant_msg is not None
    text_blocks = [b for b in assistant_msg.content if isinstance(b, TextBlock)]
    assert len(text_blocks) > 0, "AssistantMessage should have at least one TextBlock"
    assert any(b.text.strip() for b in text_blocks), "TextBlock should have non-empty text"


async def test_can_use_tool_callback():
    """can_use_tool callback should be invoked when Claude reads a file."""
    invoked_tools: list[str] = []
    received_messages: list[str] = []

    async def _auto_approve(tool_name: str, input_data: dict, context):
        from claude_agent_sdk import PermissionResultAllow

        invoked_tools.append(tool_name)
        return PermissionResultAllow()

    with tempfile.TemporaryDirectory() as cwd:
        Path(cwd, "secret.txt").write_text("SUMMON_TOOL_TEST_42")

        options = ClaudeAgentOptions(
            cwd=cwd,
            max_turns=3,
            # permission_mode="plan" forces CLI to ask for ALL tool uses,
            # ensuring the can_use_tool callback is always invoked.
            permission_mode="plan",
            can_use_tool=_auto_approve,
            **_COMMON_OPTS,
        )
        async with ClaudeSDKClient(options) as client:
            await client.query(
                "Read the file secret.txt in the current directory and tell me its exact contents."
            )
            async for msg in client.receive_response():
                msg_type = type(msg).__name__
                if isinstance(msg, AssistantMessage):
                    blocks = [
                        f"{type(b).__name__}({getattr(b, 'name', '')}{getattr(b, 'text', '')[:80]})"
                        for b in msg.content
                    ]
                    received_messages.append(f"{msg_type}: {blocks}")
                elif isinstance(msg, ResultMessage):
                    received_messages.append(
                        f"{msg_type}: turns={msg.num_turns} cost={msg.total_cost_usd}"
                    )
                else:
                    received_messages.append(msg_type)

    debug = "\n  ".join(received_messages)
    assert len(invoked_tools) >= 1, (
        f"Expected can_use_tool callback to be invoked, got: {invoked_tools}\n"
        f"Messages received:\n  {debug}"
    )


async def test_ask_user_question_callback():
    """AskUserQuestion tool should be intercepted by can_use_tool and receive answers."""
    captured_input: list[dict] = []

    async def _handle_tools(tool_name: str, input_data: dict, context):
        from claude_agent_sdk import PermissionResultAllow

        if tool_name == "AskUserQuestion":
            captured_input.append(input_data)
            questions = input_data.get("questions", [])
            answers = {}
            for q in questions:
                # Auto-select the first option for each question
                options = q.get("options", [])
                if options:
                    answers[q["question"]] = options[0]["label"]
            return PermissionResultAllow(updated_input={"questions": questions, "answers": answers})
        return PermissionResultAllow()

    with tempfile.TemporaryDirectory() as cwd:
        options = ClaudeAgentOptions(
            cwd=cwd,
            max_turns=3,
            permission_mode="plan",
            can_use_tool=_handle_tools,
            **_COMMON_OPTS,
        )
        async with ClaudeSDKClient(options) as client:
            # Prompt that triggers AskUserQuestion
            await client.query(
                "Ask me a question using the AskUserQuestion tool. "
                "Ask what programming language I prefer with options: Python, Rust, Go. "
                "Use the AskUserQuestion tool, do not just type the question."
            )
            result_msg = None
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    result_msg = msg

    assert result_msg is not None, "Expected a ResultMessage"
    assert len(captured_input) >= 1, (
        "Expected AskUserQuestion to be intercepted by can_use_tool callback"
    )
    # Verify the captured input has the expected structure
    first = captured_input[0]
    assert "questions" in first
    assert len(first["questions"]) >= 1
    assert "options" in first["questions"][0]


async def test_passthrough_command_populates_result():
    """Passthrough slash commands like /cost should complete and produce a ResultMessage."""
    with tempfile.TemporaryDirectory() as cwd:
        options = ClaudeAgentOptions(cwd=cwd, max_turns=1, **_COMMON_OPTS)
        async with ClaudeSDKClient(options) as client:
            await client.query("/cost")
            result_msg = None
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    result_msg = msg

    assert result_msg is not None, "Expected a ResultMessage from /cost"
    # CLI-only system commands (/compact, /context, /cost, /release-notes)
    # are handled internally by the CLI — they complete as a successful result
    # but don't produce AssistantMessage or populate ResultMessage.result
    # (output goes to the terminal, not the SDK stream).
    assert result_msg.is_error is False, (
        "Expected /cost to complete without error, got is_error=True"
    )


# ------------------------------------------------------------------
# ThinkingConfig integration tests
# ------------------------------------------------------------------


class TestThinkingConfigIntegration:
    """Verify ThinkingConfig type discriminator is accepted by the SDK."""

    async def test_adaptive_thinking_produces_thinking_blocks(self):
        """ThinkingConfigAdaptive(type='adaptive') should produce ThinkingBlocks."""
        with tempfile.TemporaryDirectory() as cwd:
            options = ClaudeAgentOptions(
                cwd=cwd,
                max_turns=1,
                thinking=ThinkingConfigAdaptive(type="adaptive"),
                **_COMMON_OPTS,
            )
            async with ClaudeSDKClient(options) as client:
                await client.query(
                    "Think step by step about what 17 * 23 equals. Show your reasoning."
                )
                messages = []
                async for msg in client.receive_response():
                    messages.append(msg)

        assistant_msgs = [m for m in messages if isinstance(m, AssistantMessage)]
        assert any(isinstance(m, ResultMessage) for m in messages), "Expected ResultMessage"
        thinking_blocks = [
            b for m in assistant_msgs for b in m.content if isinstance(b, ThinkingBlock)
        ]
        assert len(thinking_blocks) > 0, (
            "Expected at least one ThinkingBlock with adaptive thinking enabled, "
            f"got content types: {[type(b).__name__ for m in assistant_msgs for b in m.content]}"
        )

    async def test_disabled_thinking_produces_no_thinking_blocks(self):
        """ThinkingConfigDisabled(type='disabled') should produce no ThinkingBlocks."""
        with tempfile.TemporaryDirectory() as cwd:
            options = ClaudeAgentOptions(
                cwd=cwd,
                max_turns=1,
                thinking=ThinkingConfigDisabled(type="disabled"),
                **_COMMON_OPTS,
            )
            async with ClaudeSDKClient(options) as client:
                await client.query("What is 2 + 2?")
                messages = []
                async for msg in client.receive_response():
                    messages.append(msg)

        assistant_msgs = [m for m in messages if isinstance(m, AssistantMessage)]
        assert any(isinstance(m, ResultMessage) for m in messages), "Expected ResultMessage"
        thinking_blocks = [
            b for m in assistant_msgs for b in m.content if isinstance(b, ThinkingBlock)
        ]
        assert len(thinking_blocks) == 0, (
            f"Expected no ThinkingBlocks with thinking disabled, got {len(thinking_blocks)}"
        )


# ------------------------------------------------------------------
# SDK command inventory tests
# ------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="class")
class TestSDKCommandInventory:
    """Verify COMMAND_ACTIONS covers all SDK commands."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def server_info(self):
        """Get server_info from a real Claude SDK connection."""
        options = ClaudeAgentOptions(cwd="/tmp")
        async with ClaudeSDKClient(options) as client:
            info = await client.get_server_info()
        yield info

    async def test_all_sdk_commands_in_inventory(self, server_info):
        if not server_info:
            pytest.skip("No server_info available")
        commands = server_info.get("commands", [])
        for item in commands:
            name = (
                item.get("name", item).lstrip("/") if isinstance(item, dict) else item.lstrip("/")
            )
            assert name in COMMAND_ACTIONS, f"SDK command '{name}' not in COMMAND_ACTIONS — add it"

    async def test_models_available(self, server_info):
        if not server_info:
            pytest.skip("No server_info available")
        models = server_info.get("models", [])
        assert len(models) > 0, "Expected at least one model in server_info"
