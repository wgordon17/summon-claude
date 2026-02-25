"""Tests for the command dispatch system."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from helpers import make_mock_provider
from summon_claude.commands import (
    CommandContext,
    CommandRegistry,
    CommandResult,
    build_registry,
)


@pytest.fixture
def mock_provider():
    """Provide a mocked ChatProvider."""
    return make_mock_provider()


@pytest.fixture
def make_context(mock_provider):
    """Factory for creating CommandContext instances."""

    def _make(**overrides):
        defaults = {
            "channel_id": "C123",
            "thread_ts": "100.000",
            "user_id": "U123",
            "provider": mock_provider,
            "session_id": "test-session",
            "model": "sonnet",
            "turns": 5,
            "cost_usd": 0.0123,
            "start_time": datetime(2026, 1, 1, tzinfo=UTC),
        }
        defaults.update(overrides)
        return CommandContext(**defaults)

    return _make


class TestCommandRegistryParse:
    """Test CommandRegistry.parse method."""

    def test_parse_bang_alone(self):
        """! alone should return None."""
        registry = CommandRegistry()
        assert registry.parse("!") is None

    def test_parse_double_bang(self):
        """!! should return None."""
        registry = CommandRegistry()
        assert registry.parse("!!") is None

    def test_parse_bang_space_command(self):
        """! followed by space should return None."""
        registry = CommandRegistry()
        assert registry.parse("! help") is None

    def test_parse_bang_number(self):
        """!123 should return None."""
        registry = CommandRegistry()
        assert registry.parse("!123") is None

    def test_parse_mid_text_command(self):
        """Command in middle of text should return None."""
        registry = CommandRegistry()
        assert registry.parse("hello !help") is None

    def test_parse_help_command(self):
        """!help should return ('help', [])."""
        registry = CommandRegistry()
        result = registry.parse("!help")
        assert result == ("help", [])

    def test_parse_model_sonnet(self):
        """!model sonnet should return ('model', ['sonnet'])."""
        registry = CommandRegistry()
        result = registry.parse("!model sonnet")
        assert result == ("model", ["sonnet"])

    def test_parse_model_with_hyphens(self):
        """!model claude-opus-4-6 should parse correctly."""
        registry = CommandRegistry()
        result = registry.parse("!model claude-opus-4-6")
        assert result == ("model", ["claude-opus-4-6"])

    def test_parse_uppercase_command(self):
        """!HELP should be lowercased to ('help', [])."""
        registry = CommandRegistry()
        result = registry.parse("!HELP")
        assert result == ("help", [])

    def test_parse_multi_word_args(self):
        """!model claude sonnet should return ('model', ['claude', 'sonnet'])."""
        registry = CommandRegistry()
        result = registry.parse("!model claude sonnet")
        assert result == ("model", ["claude", "sonnet"])

    def test_parse_empty_string(self):
        """Empty string should return None."""
        registry = CommandRegistry()
        assert registry.parse("") is None

    def test_parse_regular_text(self):
        """Regular text without ! should return None."""
        registry = CommandRegistry()
        assert registry.parse("hello world") is None


class TestCommandRegistryDispatch:
    """Test CommandRegistry.dispatch method."""

    async def test_dispatch_local_command(self, make_context):
        """Local command should be dispatched to registered handler."""
        registry = CommandRegistry()
        handler_called = False
        captured_args = None

        async def test_handler(args: list[str], _context: CommandContext) -> CommandResult:
            nonlocal handler_called, captured_args
            handler_called = True
            captured_args = args
            return CommandResult(text="Handled")

        registry.register("test", test_handler, "Test command")
        context = make_context()

        result = await registry.dispatch("test", ["arg1", "arg2"], context)

        assert handler_called
        assert captured_args == ["arg1", "arg2"]
        assert result.text == "Handled"
        assert result.suppress_queue is True

    async def test_dispatch_remap_quit_to_end(self, make_context):
        """!quit should be remapped to !end handler."""
        registry = build_registry()
        context = make_context(metadata={"registry": registry})

        result = await registry.dispatch("quit", [], context)

        assert result.metadata.get("shutdown") is True
        assert result.text is not None
        assert "Ending session" in result.text

    async def test_dispatch_remap_exit_to_end(self, make_context):
        """!exit should be remapped to !end handler."""
        registry = build_registry()
        context = make_context(metadata={"registry": registry})

        result = await registry.dispatch("exit", [], context)

        assert result.metadata.get("shutdown") is True

    async def test_dispatch_remap_logout_to_end(self, make_context):
        """!logout should be remapped to !end handler."""
        registry = build_registry()
        context = make_context(metadata={"registry": registry})

        result = await registry.dispatch("logout", [], context)

        assert result.metadata.get("shutdown") is True

    async def test_dispatch_blocked_login(self, make_context):
        """!login should be blocked with error text."""
        registry = CommandRegistry()
        context = make_context()

        result = await registry.dispatch("login", [], context)

        assert result.text is not None
        assert "not available" in result.text.lower()
        assert result.suppress_queue is True

    async def test_dispatch_passthrough_command(self, make_context):
        """Passthrough command should return suppress_queue=False."""
        registry = CommandRegistry()
        registry.set_passthrough_commands(["compact", "clear"])
        context = make_context()

        result = await registry.dispatch("compact", [], context)

        assert result.suppress_queue is False
        assert result.text is None

    async def test_dispatch_unknown_command(self, make_context):
        """Unknown command should return error text."""
        registry = CommandRegistry()
        context = make_context()

        result = await registry.dispatch("unknown", [], context)

        assert result.text is not None
        assert "Unknown command" in result.text
        assert result.suppress_queue is True


class TestHelpHandler:
    """Test the !help command handler."""

    async def test_help_contains_local_commands(self, make_context):
        """Help output should contain all local command names."""
        registry = build_registry()
        context = make_context(metadata={"registry": registry})

        result = await registry.dispatch("help", [], context)

        assert result.text is not None
        assert "!help" in result.text
        assert "!status" in result.text
        assert "!end" in result.text
        assert "!model" in result.text

    async def test_help_contains_passthrough_commands(self, make_context):
        """Help output should contain passthrough command names."""
        registry = build_registry()
        registry.set_passthrough_commands(["compact", "clear"])
        context = make_context(metadata={"registry": registry})

        result = await registry.dispatch("help", [], context)

        assert result.text is not None
        assert "!compact" in result.text
        assert "!clear" in result.text

    async def test_help_has_section_headers(self, make_context):
        """Help output should have section headers."""
        registry = build_registry()
        registry.set_passthrough_commands(["compact"])
        context = make_context(metadata={"registry": registry})

        result = await registry.dispatch("help", [], context)

        assert result.text is not None
        assert "Session Commands" in result.text
        assert "Claude Commands" in result.text

    async def test_help_no_claude_section_when_no_passthrough(self, make_context):
        """Help should not have Claude Commands section when no passthrough."""
        registry = build_registry()
        context = make_context(metadata={"registry": registry})

        result = await registry.dispatch("help", [], context)

        assert result.text is not None
        assert "Session Commands" in result.text
        assert "Claude Commands" not in result.text


class TestStatusHandler:
    """Test the !status command handler."""

    async def test_status_contains_model(self, make_context):
        """Status should contain model."""
        registry = build_registry()
        context = make_context(model="opus", metadata={"registry": registry})

        result = await registry.dispatch("status", [], context)

        assert result.text is not None
        assert "opus" in result.text

    async def test_status_contains_turns_count(self, make_context):
        """Status should contain turn count."""
        registry = build_registry()
        context = make_context(turns=10, metadata={"registry": registry})

        result = await registry.dispatch("status", [], context)

        assert result.text is not None
        assert "Turns: 10" in result.text

    async def test_status_contains_cost(self, make_context):
        """Status should contain cost."""
        registry = build_registry()
        context = make_context(cost_usd=1.2345, metadata={"registry": registry})

        result = await registry.dispatch("status", [], context)

        assert result.text is not None
        assert "$1.2345" in result.text

    async def test_status_contains_uptime(self, make_context):
        """Status should contain uptime."""
        registry = build_registry()
        start_time = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        context = make_context(start_time=start_time, metadata={"registry": registry})

        result = await registry.dispatch("status", [], context)

        assert result.text is not None
        assert "Uptime:" in result.text

    async def test_status_no_start_time(self, make_context):
        """Status with no start_time should show 'unknown' uptime."""
        registry = build_registry()
        context = make_context(start_time=None, metadata={"registry": registry})

        result = await registry.dispatch("status", [], context)

        assert result.text is not None
        assert "unknown" in result.text


class TestEndHandler:
    """Test the !end command handler."""

    async def test_end_returns_shutdown_metadata(self, make_context):
        """!end should return metadata with shutdown=True."""
        registry = build_registry()
        context = make_context(metadata={"registry": registry})

        result = await registry.dispatch("end", [], context)

        assert result.metadata.get("shutdown") is True
        assert result.text is not None
        assert "Ending session" in result.text

    async def test_end_suppresses_queue(self, make_context):
        """!end result should suppress queue forwarding."""
        registry = build_registry()
        context = make_context(metadata={"registry": registry})

        result = await registry.dispatch("end", [], context)

        assert result.suppress_queue is True


class TestModelHandler:
    """Test the !model command handler."""

    async def test_model_bare_shows_current(self, make_context):
        """!model (bare) should show current model."""
        registry = build_registry()
        context = make_context(model="sonnet", metadata={"registry": registry})

        result = await registry.dispatch("model", [], context)

        assert result.text is not None
        assert "Current model:" in result.text
        assert "sonnet" in result.text

    async def test_model_with_valid_arg(self, make_context):
        """!model opus should return new_model metadata."""
        registry = build_registry()
        context = make_context(metadata={"registry": registry})

        result = await registry.dispatch("model", ["opus"], context)

        assert result.metadata.get("new_model") == "opus"
        assert result.text is not None
        assert "opus" in result.text

    async def test_model_with_hyphenated_name(self, make_context):
        """!model claude-opus-4-6 should accept hyphenated model names."""
        registry = build_registry()
        context = make_context(metadata={"registry": registry})

        result = await registry.dispatch("model", ["claude-opus-4-6"], context)

        assert result.metadata.get("new_model") == "claude-opus-4-6"

    async def test_model_with_dotted_name(self, make_context):
        """!model claude-3.5-sonnet should accept dotted model names."""
        registry = build_registry()
        context = make_context(metadata={"registry": registry})
        result = await registry.dispatch("model", ["claude-3.5-sonnet"], context)
        assert result.metadata.get("new_model") == "claude-3.5-sonnet"

    async def test_model_with_invalid_name(self, make_context):
        """!model with invalid name should return error."""
        registry = build_registry()
        context = make_context(metadata={"registry": registry})

        result = await registry.dispatch("model", ["foo bar!"], context)

        assert result.text is not None
        assert "Invalid model name" in result.text
        assert "new_model" not in result.metadata


class TestSetPassthroughCommands:
    """Test CommandRegistry.set_passthrough_commands method."""

    def test_accepts_list_of_dicts(self):
        """Should accept list of dicts with 'name' key."""
        registry = CommandRegistry()
        commands = [
            {"name": "compact", "description": "Compress history"},
            {"name": "clear", "description": "Clear conversation"},
        ]
        registry.set_passthrough_commands(commands)
        assert "compact" in registry._passthrough
        assert "clear" in registry._passthrough

    def test_accepts_list_of_strings(self):
        """Should accept list of strings."""
        registry = CommandRegistry()
        commands = ["compact", "clear"]
        registry.set_passthrough_commands(commands)
        assert "compact" in registry._passthrough
        assert "clear" in registry._passthrough

    def test_strips_leading_slash_from_names(self):
        """Should strip leading / from command names."""
        registry = CommandRegistry()
        commands = ["/compact", "/clear"]
        registry.set_passthrough_commands(commands)
        assert "compact" in registry._passthrough
        assert "/compact" not in registry._passthrough

    def test_filters_out_blocked_commands(self):
        """Should filter out blocked commands like login."""
        registry = CommandRegistry()
        commands = ["compact", "login", "clear"]
        registry.set_passthrough_commands(commands)
        assert "compact" in registry._passthrough
        assert "clear" in registry._passthrough
        assert "login" not in registry._passthrough

    def test_filters_out_remap_commands(self):
        """Should filter out remap commands (quit, exit, logout)."""
        registry = CommandRegistry()
        commands = ["compact", "quit", "exit", "logout", "clear"]
        registry.set_passthrough_commands(commands)
        assert "compact" in registry._passthrough
        assert "clear" in registry._passthrough
        assert "quit" not in registry._passthrough
        assert "exit" not in registry._passthrough
        assert "logout" not in registry._passthrough

    def test_clears_previous_passthrough_commands(self):
        """Should clear previous passthrough commands."""
        registry = CommandRegistry()
        registry.set_passthrough_commands(["compact"])
        assert "compact" in registry._passthrough
        registry.set_passthrough_commands(["clear"])
        assert "clear" in registry._passthrough
        assert "compact" not in registry._passthrough


class TestAllCommands:
    """Test CommandRegistry.all_commands method."""

    def test_returns_local_commands(self):
        """Should include all local commands."""
        registry = build_registry()
        commands = registry.all_commands()
        assert "help" in commands
        assert "status" in commands
        assert "end" in commands
        assert "model" in commands

    def test_returns_passthrough_commands(self):
        """Should include all passthrough commands."""
        registry = build_registry()
        registry.set_passthrough_commands(["compact", "clear"])
        commands = registry.all_commands()
        assert "compact" in commands
        assert "clear" in commands

    def test_returns_remap_aliases(self):
        """Should include remap aliases."""
        registry = build_registry()
        commands = registry.all_commands()
        assert "quit" in commands
        assert "exit" in commands
        assert "logout" in commands

    def test_combined_dict_format(self):
        """all_commands should return a dict with descriptions."""
        registry = build_registry()
        registry.set_passthrough_commands(["compact"])
        commands = registry.all_commands()
        assert isinstance(commands, dict)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in commands.items())
        assert commands["quit"] == "Alias for !end"


class TestParseAdvanced:
    """Additional parse tests for edge cases."""

    def test_parse_with_multiple_spaces(self):
        """Commands with multiple spaces between args should split correctly."""
        registry = CommandRegistry()
        result = registry.parse("!model  sonnet  opus")
        assert result is not None
        assert result[0] == "model"
        # split() handles multiple spaces by treating them as one separator
        assert result[1] == ["sonnet", "opus"]

    def test_parse_with_special_characters_in_args(self):
        """Args can contain special characters."""
        registry = CommandRegistry()
        result = registry.parse("!remind @user check-logs")
        assert result is not None
        assert result[0] == "remind"
        assert result[1] == ["@user", "check-logs"]

    def test_parse_mixed_case_normalized(self):
        """Command names should be normalized to lowercase."""
        registry = CommandRegistry()
        result = registry.parse("!HeLp something")
        assert result == ("help", ["something"])


class TestDispatchEdgeCases:
    """Additional dispatch tests for edge cases."""

    async def test_dispatch_empty_registry(self, make_context):
        """Dispatch on empty registry returns unknown error."""
        registry = CommandRegistry()
        context = make_context()

        result = await registry.dispatch("anything", [], context)

        assert result.text is not None
        assert "Unknown command" in result.text

    async def test_dispatch_handler_exception(self, make_context):
        """Handler exception should propagate (session catches it)."""
        registry = CommandRegistry()

        async def failing_handler(_args: list[str], _ctx: CommandContext) -> CommandResult:
            raise ValueError("Handler failed")

        registry.register("fail", failing_handler, "Fails")
        context = make_context()

        with pytest.raises(ValueError, match="Handler failed"):
            await registry.dispatch("fail", [], context)

    async def test_dispatch_remap_all_aliases(self, make_context):
        """All remap aliases should produce the same result as !end."""
        registry = build_registry()
        context = make_context(metadata={"registry": registry})

        for alias in ("quit", "exit", "logout"):
            result = await registry.dispatch(alias, [], context)
            assert result.metadata.get("shutdown") is True, f"{alias} should trigger shutdown"

    async def test_passthrough_descriptions_from_dicts(self, make_context):
        """Passthrough commands from dicts should preserve descriptions."""
        registry = CommandRegistry()
        registry.set_passthrough_commands([
            {"name": "compact", "description": "Compress conversation history"},
        ])
        commands = registry.all_commands()
        assert commands["compact"] == "Compress conversation history"
