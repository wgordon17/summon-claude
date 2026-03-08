"""Tests for the command dispatch system."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from summon_claude.sessions.commands import (
    COMMAND_ACTIONS,
    CommandContext,
    dispatch,
    find_commands,
    parse,
    validate_sdk_commands,
)


@pytest.fixture
def make_context():
    """Factory for creating CommandContext instances."""

    def _make(**overrides):
        defaults = {
            "session_id": "test-session",
            "model": "sonnet",
            "turns": 5,
            "cost_usd": 0.0123,
            "start_time": datetime(2026, 1, 1, tzinfo=UTC),
        }
        defaults.update(overrides)
        return CommandContext(**defaults)

    return _make


# ------------------------------------------------------------------
# parse() tests
# ------------------------------------------------------------------


class TestParse:
    """Test the module-level parse function."""

    def test_parse_bang_alone(self):
        """! alone should return None."""
        assert parse("!") is None

    def test_parse_double_bang(self):
        """!! should return None."""
        assert parse("!!") is None

    def test_parse_bang_space_command(self):
        """! followed by space should return None."""
        assert parse("! help") is None

    def test_parse_bang_number(self):
        """!123 should return None."""
        assert parse("!123") is None

    def test_parse_mid_text_command(self):
        """Command in middle of text should return None (parse only checks start)."""
        assert parse("hello !help") is None

    def test_parse_help_command(self):
        """!help should return ('help', [])."""
        assert parse("!help") == ("help", [])

    def test_parse_model_sonnet(self):
        """!model sonnet should return ('model', ['sonnet'])."""
        assert parse("!model sonnet") == ("model", ["sonnet"])

    def test_parse_model_with_hyphens(self):
        """!model claude-opus-4-6 should parse correctly."""
        assert parse("!model claude-opus-4-6") == ("model", ["claude-opus-4-6"])

    def test_parse_uppercase_command(self):
        """!HELP should be lowercased to ('help', [])."""
        assert parse("!HELP") == ("help", [])

    def test_parse_multi_word_args(self):
        """!model claude sonnet should return ('model', ['claude', 'sonnet'])."""
        assert parse("!model claude sonnet") == ("model", ["claude", "sonnet"])

    def test_parse_empty_string(self):
        """Empty string should return None."""
        assert parse("") is None

    def test_parse_regular_text(self):
        """Regular text without ! should return None."""
        assert parse("hello world") is None

    def test_parse_with_multiple_spaces(self):
        """Commands with multiple spaces between args should split correctly."""
        result = parse("!model  sonnet  opus")
        assert result is not None
        assert result[0] == "model"
        assert result[1] == ["sonnet", "opus"]

    def test_parse_with_special_characters_in_args(self):
        """Args can contain special characters."""
        result = parse("!remind @user check-logs")
        assert result is not None
        assert result[0] == "remind"
        assert result[1] == ["@user", "check-logs"]

    def test_parse_mixed_case_normalized(self):
        """Command names should be normalized to lowercase."""
        assert parse("!HeLp something") == ("help", ["something"])


# ------------------------------------------------------------------
# dispatch() tests
# ------------------------------------------------------------------


class TestDispatch:
    """Test the module-level dispatch function."""

    async def test_dispatch_local_command(self, make_context):
        """Local command should be dispatched to registered handler."""
        context = make_context()
        result = await dispatch("help", [], context)
        assert result.text is not None
        assert result.suppress_queue is True

    @pytest.mark.parametrize("alias", ["quit", "exit", "logout"])
    async def test_dispatch_alias_to_end(self, alias, make_context):
        """End-session aliases should resolve to !end handler."""
        context = make_context()
        result = await dispatch(alias, [], context)
        assert result.metadata.get("shutdown") is True
        assert result.text is not None
        assert "Ending session" in result.text

    @pytest.mark.parametrize("cmd", ["login", "insights", "context", "cost", "release-notes"])
    async def test_dispatch_blocked_commands(self, cmd, make_context):
        """Blocked commands should return block_reason text."""
        context = make_context()
        result = await dispatch(cmd, [], context)
        assert result.text is not None
        assert "not available" in result.text.lower()
        assert result.suppress_queue is True

    async def test_dispatch_passthrough_command(self, make_context):
        """Passthrough command should return suppress_queue=False."""
        context = make_context()
        result = await dispatch("review", [], context)
        assert result.suppress_queue is False
        assert result.text is None

    async def test_dispatch_unknown_command(self, make_context):
        """Unknown command should return error text."""
        context = make_context()
        result = await dispatch("unknown_xyz", [], context)
        assert result.text is not None
        assert "Unknown command" in result.text
        assert result.suppress_queue is True


# ------------------------------------------------------------------
# Handler tests
# ------------------------------------------------------------------


class TestHelpHandler:
    """Test the !help command handler."""

    async def test_help_contains_local_commands(self, make_context):
        """Help output should contain local command names."""
        context = make_context()
        result = await dispatch("help", [], context)
        assert result.text is not None
        assert "!help" in result.text
        assert "!status" in result.text
        assert "!end" in result.text

    async def test_help_contains_passthrough_commands(self, make_context):
        """Help output should list passthrough commands."""
        context = make_context()
        result = await dispatch("help", [], context)
        assert result.text is not None
        assert "!review" in result.text

    async def test_help_has_section_headers(self, make_context):
        """Help output should have section headers."""
        context = make_context()
        result = await dispatch("help", [], context)
        assert result.text is not None
        assert "*Session* (local):" in result.text

    async def test_help_with_known_command_shows_detail(self, make_context):
        """!help status should show local detail view."""
        context = make_context()
        result = await dispatch("help", ["status"], context)
        assert result.text is not None
        assert "status" in result.text
        assert result.suppress_queue is True

    async def test_help_with_unknown_command_shows_error(self, make_context):
        """!help nonexistent should show unknown error."""
        context = make_context()
        result = await dispatch("help", ["nonexistent"], context)
        assert result.text is not None
        assert "Unknown command" in result.text

    async def test_help_detail_shows_aliases(self, make_context):
        """!help end should show quit/exit/logout aliases."""
        context = make_context()
        result = await dispatch("help", ["end"], context)
        assert result.text is not None
        assert "quit" in result.text
        assert "exit" in result.text
        assert "logout" in result.text

    async def test_help_detail_blocked_command(self, make_context):
        """!help config should show 'blocked' type."""
        context = make_context()
        result = await dispatch("help", ["config"], context)
        assert result.text is not None
        assert "blocked" in result.text

    async def test_help_detail_passthrough_command(self, make_context):
        """!help review should show 'passthrough' type."""
        context = make_context()
        result = await dispatch("help", ["review"], context)
        assert result.text is not None
        assert "passthrough" in result.text

    async def test_help_detail_alias_resolves(self, make_context):
        """!help quit should resolve to 'end' and show end info."""
        context = make_context()
        result = await dispatch("help", ["quit"], context)
        assert result.text is not None
        assert "end" in result.text


class TestStatusHandler:
    """Test the !status command handler."""

    async def test_status_contains_model(self, make_context):
        """Status should contain model."""
        context = make_context(model="opus")
        result = await dispatch("status", [], context)
        assert result.text is not None
        assert "opus" in result.text

    async def test_status_contains_turns_count(self, make_context):
        """Status should contain turn count."""
        context = make_context(turns=10)
        result = await dispatch("status", [], context)
        assert result.text is not None
        assert "Turns: 10" in result.text

    async def test_status_contains_cost(self, make_context):
        """Status should contain cost."""
        context = make_context(cost_usd=1.2345)
        result = await dispatch("status", [], context)
        assert result.text is not None
        assert "$1.2345" in result.text

    async def test_status_contains_uptime(self, make_context):
        """Status should contain uptime."""
        start_time = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        context = make_context(start_time=start_time)
        result = await dispatch("status", [], context)
        assert result.text is not None
        assert "Uptime:" in result.text

    async def test_status_no_start_time(self, make_context):
        """Status with no start_time should show 'unknown' uptime."""
        context = make_context(start_time=None)
        result = await dispatch("status", [], context)
        assert result.text is not None
        assert "unknown" in result.text


class TestEndHandler:
    """Test the !end command handler."""

    async def test_end_returns_shutdown_metadata(self, make_context):
        """!end should return metadata with shutdown=True."""
        context = make_context()
        result = await dispatch("end", [], context)
        assert result.metadata.get("shutdown") is True
        assert result.text is not None
        assert "Ending session" in result.text

    async def test_end_suppresses_queue(self, make_context):
        """!end result should suppress queue forwarding."""
        context = make_context()
        result = await dispatch("end", [], context)
        assert result.suppress_queue is True


class TestModelLocal:
    """Test the !model local handler with arg validation."""

    async def test_model_no_args_shows_current(self, make_context):
        """!model with no args should show current model."""
        context = make_context(model="opus")
        result = await dispatch("model", [], context)
        assert result.text is not None
        assert "opus" in result.text
        assert result.suppress_queue is True

    async def test_model_with_valid_arg_switches(self, make_context):
        """!model opus should trigger model switch."""
        context = make_context(metadata={"models": [{"value": "opus"}, {"value": "sonnet"}]})
        result = await dispatch("model", ["opus"], context)
        assert result.metadata.get("set_model") == "opus"
        assert result.suppress_queue is True

    async def test_model_with_invalid_arg_errors(self, make_context):
        """!model invalid should return error when models list is known."""
        context = make_context(metadata={"models": [{"value": "opus"}, {"value": "sonnet"}]})
        result = await dispatch("model", ["invalid"], context)
        assert result.text is not None
        assert "Unknown model" in result.text
        assert result.suppress_queue is True

    async def test_model_no_args_no_models_available(self, make_context):
        """!model with no args and empty models list should show 'unknown' only."""
        context = make_context(model=None, metadata={"models": []})
        result = await dispatch("model", [], context)
        assert result.text is not None
        assert "unknown" in result.text
        # Should not list "Available:" since no models
        assert "Available:" not in result.text

    async def test_model_no_validation_when_no_models(self, make_context):
        """!model with a name but no models metadata should accept any name."""
        context = make_context(metadata={})
        result = await dispatch("model", ["anything-goes"], context)
        assert result.text is not None
        assert result.metadata.get("set_model") == "anything-goes"

    async def test_model_with_dict_models(self, make_context):
        """Models as list of dicts with 'value' key should work."""
        context = make_context(metadata={"models": [{"value": "opus", "displayName": "Opus"}]})
        result = await dispatch("model", ["opus"], context)
        assert result.metadata.get("set_model") == "opus"


class TestClearHandler:
    """Test the !clear command handler."""

    async def test_clear_is_local(self, make_context):
        """!clear should be handled locally with suppress_queue=True."""
        context = make_context()
        result = await dispatch("clear", [], context)
        assert result.suppress_queue is True
        assert result.metadata.get("clear") is True


class TestStopCommand:
    """Test the !stop command handler."""

    async def test_stop_returns_stop_metadata(self, make_context):
        """!stop should return metadata with stop=True."""
        context = make_context()
        result = await dispatch("stop", [], context)
        assert result.metadata.get("stop") is True
        assert result.text is not None
        assert "Cancelling" in result.text

    async def test_stop_suppresses_queue(self, make_context):
        """!stop result should suppress queue forwarding."""
        context = make_context()
        result = await dispatch("stop", [], context)
        assert result.suppress_queue is True


class TestCompactHandler:
    """Test the !compact command handler."""

    async def test_compact_returns_metadata(self, make_context):
        """!compact should return metadata with compact=True, handled locally."""
        context = make_context()
        result = await dispatch("compact", [], context)
        assert result.metadata.get("compact") is True
        assert result.suppress_queue is True

    async def test_compact_with_instructions(self, make_context):
        """!compact with args should pass instructions."""
        context = make_context()
        result = await dispatch("compact", ["focus", "on", "tests"], context)
        assert result.metadata.get("instructions") == "focus on tests"


# ------------------------------------------------------------------
# find_commands() tests (mid-message detection)
# ------------------------------------------------------------------


class TestFindCommands:
    """Test mid-message command detection with find_commands."""

    def test_single_command_at_start(self):
        """!command at the beginning should be found."""
        matches = find_commands("!help")
        assert len(matches) == 1
        assert matches[0].name == "help"
        assert matches[0].prefix == "!"
        assert matches[0].args == []

    def test_command_mid_message(self):
        """!command in the middle of text should be found."""
        matches = find_commands("please run !status for me")
        assert len(matches) == 1
        assert matches[0].name == "status"

    def test_multiple_commands(self):
        """Multiple commands in one message should all be found."""
        matches = find_commands("!model opus and !clear")
        assert len(matches) == 2
        assert matches[0].name == "model"
        assert matches[0].args == ["opus"]
        assert matches[1].name == "clear"

    def test_alias_resolved(self):
        """!new should resolve to 'clear'."""
        matches = find_commands("!new")
        assert len(matches) == 1
        assert matches[0].name == "clear"
        assert matches[0].raw_name == "new"

    def test_url_resistance(self):
        """URLs like https://example.com/help should not match."""
        matches = find_commands("check https://example.com/help")
        assert len(matches) == 0

    def test_blocked_mid_message(self):
        """Blocked command mid-message should still be found."""
        matches = find_commands("try !config please")
        assert len(matches) == 1
        assert matches[0].name == "config"

    def test_arg_consumption(self):
        """!model opus should consume 'opus' as arg, leaving 'rest of text'."""
        matches = find_commands("!model opus rest of text")
        assert len(matches) == 1
        assert matches[0].name == "model"
        assert matches[0].args == ["opus"]
        # The end position should be after "opus", not after "text"
        assert matches[0].end < len("!model opus rest of text")

    def test_arg_consumption_stops_at_next_command(self):
        """!model !stop — model should NOT consume !stop as an arg."""
        matches = find_commands("!model !stop")
        assert len(matches) == 2
        assert matches[0].name == "model"
        assert matches[0].args == []
        assert matches[1].name == "stop"

    def test_no_match_in_plain_text(self):
        """Plain text with no commands should return empty."""
        matches = find_commands("just regular text here")
        assert len(matches) == 0

    def test_slash_prefix_detected(self):
        """/command should also be detected."""
        matches = find_commands("/review")
        assert len(matches) == 1
        assert matches[0].prefix == "/"
        assert matches[0].name == "review"

    def test_command_after_newline(self):
        """Command after newline should be found."""
        matches = find_commands("first line\n!help")
        assert len(matches) == 1
        assert matches[0].name == "help"

    def test_empty_string(self):
        """find_commands('') should return []."""
        assert find_commands("") == []

    def test_whitespace_only(self):
        """find_commands('   ') should return []."""
        assert find_commands("   ") == []

    def test_file_path_resistance(self):
        """/usr/local/bin should not match as a command."""
        matches = find_commands("/usr/local/bin")
        # /usr might match, but /local and /bin should not (preceded by /)
        # The key point: this should not produce a match for "local" or "bin"
        for m in matches:
            assert m.name not in ("local", "bin")

    def test_bang_in_url(self):
        """http://example.com!help should not match."""
        matches = find_commands("http://example.com!help")
        assert len(matches) == 0

    def test_max_args_zero_no_consumption(self):
        """!status has max_args=0, should NOT consume 'extra'."""
        matches = find_commands("!status extra")
        assert len(matches) == 1
        assert matches[0].name == "status"
        assert matches[0].args == []
        # end should be right after "!status"
        assert matches[0].end == len("!status")

    def test_compact_no_mid_message_consumption(self):
        """!compact has max_args=None — find_commands should NOT consume args (None means skip)."""
        matches = find_commands("!compact summarize this")
        assert len(matches) == 1
        assert matches[0].name == "compact"
        # max_args=None means the condition `max_args is not None and max_args > 0` is False
        # so no args are consumed by find_commands
        assert matches[0].args == []

    def test_passthrough_no_arg_consumption(self):
        """!review is passthrough (no handler), no args consumed."""
        matches = find_commands("!review this code")
        assert len(matches) == 1
        assert matches[0].name == "review"
        assert matches[0].args == []

    def test_command_at_end_of_text(self):
        """please run !help should find help at end."""
        matches = find_commands("please run !help")
        assert len(matches) == 1
        assert matches[0].name == "help"

    def test_multiple_local_and_passthrough(self):
        """!model opus then !review this should find both."""
        matches = find_commands("!model opus then !review this")
        assert len(matches) == 2
        assert matches[0].name == "model"
        assert matches[0].args == ["opus"]
        assert matches[1].name == "review"

    def test_unknown_command_found(self):
        """!xyznotreal should be found but defn will be None from COMMAND_ACTIONS.get()."""
        matches = find_commands("!xyznotreal")
        assert len(matches) == 1
        assert matches[0].name == "xyznotreal"
        assert COMMAND_ACTIONS.get("xyznotreal") is None

    def test_case_insensitive(self):
        """!HELP should resolve name to 'help' (lowercase)."""
        matches = find_commands("!HELP")
        assert len(matches) == 1
        assert matches[0].name == "help"
        assert matches[0].raw_name == "help"

    def test_command_with_hyphens(self):
        """!pr-comments should match correctly."""
        matches = find_commands("!pr-comments")
        assert len(matches) == 1
        assert matches[0].name == "pr-comments"

    def test_adjacent_commands_no_space(self):
        """!help!status — only !help matches (no space before !status)."""
        matches = find_commands("!help!status")
        assert len(matches) == 1
        assert matches[0].name == "help"


# ------------------------------------------------------------------
# validate_sdk_commands() tests
# ------------------------------------------------------------------


class TestValidateSDKCommands:
    """Test SDK command validation against COMMAND_ACTIONS."""

    def test_known_commands_accepted(self):
        """Known SDK commands should not be returned as unknown."""
        sdk_commands = [{"name": "/review", "argumentHint": ""}, {"name": "/help"}]
        unknown = validate_sdk_commands(sdk_commands)
        assert len(unknown) == 0

    def test_unknown_commands_returned(self):
        """Unknown SDK commands should be returned and auto-registered."""
        sdk_commands = [{"name": "/totally_new_cmd_xyz"}]
        try:
            unknown = validate_sdk_commands(sdk_commands)
            assert "totally_new_cmd_xyz" in unknown
        finally:
            COMMAND_ACTIONS.pop("totally_new_cmd_xyz", None)

    def test_argument_hint_stored(self):
        """argumentHint from SDK should be stored on matching CommandDef."""
        original_hint = COMMAND_ACTIONS["review"].argument_hint
        try:
            sdk_commands = [{"name": "/review", "argumentHint": "<files>"}]
            validate_sdk_commands(sdk_commands)
            assert COMMAND_ACTIONS["review"].argument_hint == "<files>"
        finally:
            COMMAND_ACTIONS["review"].argument_hint = original_hint

    def test_string_format_accepted(self):
        """SDK commands as plain strings should work."""
        unknown = validate_sdk_commands(["/help", "/status"])
        assert len(unknown) == 0

    def test_invalid_name_format_rejected(self):
        """Name with spaces or special chars should be skipped, not added."""
        original_keys = set(COMMAND_ACTIONS.keys())
        validate_sdk_commands([{"name": "has space"}, {"name": "bad@char"}])
        # No new keys should have been added
        assert set(COMMAND_ACTIONS.keys()) == original_keys

    def test_empty_name_skipped(self):
        """{'name': ''} should be skipped."""
        original_keys = set(COMMAND_ACTIONS.keys())
        unknown = validate_sdk_commands([{"name": ""}])
        assert len(unknown) == 0
        assert set(COMMAND_ACTIONS.keys()) == original_keys

    def test_non_dict_non_str_items_skipped(self):
        """[123, None] should be skipped without crash."""
        unknown = validate_sdk_commands([123, None])  # type: ignore[list-item]
        assert len(unknown) == 0

    def test_idempotent_on_known_commands(self):
        """Calling twice with same known commands doesn't change anything."""
        sdk_commands = [{"name": "/help"}, {"name": "/status"}]
        unknown1 = validate_sdk_commands(sdk_commands)
        unknown2 = validate_sdk_commands(sdk_commands)
        assert unknown1 == unknown2 == []

    def test_argument_hint_first_write_wins(self):
        """Calling twice, first with hint, second without — hint preserved."""
        original_hint = COMMAND_ACTIONS["review"].argument_hint
        try:
            validate_sdk_commands([{"name": "/review", "argumentHint": "<first>"}])
            assert COMMAND_ACTIONS["review"].argument_hint == "<first>"
            # Second call without hint should not overwrite
            validate_sdk_commands([{"name": "/review", "argumentHint": ""}])
            assert COMMAND_ACTIONS["review"].argument_hint == "<first>"
        finally:
            COMMAND_ACTIONS["review"].argument_hint = original_hint


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------


class TestDispatchEdgeCases:
    """Additional dispatch tests for edge cases."""

    async def test_dispatch_blocked_cli_only(self, make_context):
        """CLI-only commands should be blocked."""
        context = make_context()
        result = await dispatch("config", [], context)
        assert result.text is not None
        assert "not available" in result.text.lower()
        assert result.suppress_queue is True

    async def test_dispatch_alias_settings_to_config(self, make_context):
        """!settings should resolve to !config (blocked)."""
        context = make_context()
        result = await dispatch("settings", [], context)
        assert result.text is not None
        assert "not available" in result.text.lower()

    async def test_dispatch_unknown_shows_original_name(self, make_context):
        """dispatch('xyznotreal', ...) error text should contain 'xyznotreal'."""
        context = make_context()
        result = await dispatch("xyznotreal", [], context)
        assert result.text is not None
        assert "xyznotreal" in result.text

    async def test_dispatch_blocked_shows_original_alias(self, make_context):
        """Error text should show the alias name, not the canonical."""
        context = make_context()
        result = await dispatch("settings", [], context)
        assert result.text is not None
        assert "settings" in result.text

    async def test_dispatch_passthrough_returns_correct_flags(self, make_context):
        """dispatch('review', ...) should return text=None, suppress_queue=False."""
        context = make_context()
        result = await dispatch("review", [], context)
        assert result.text is None
        assert result.suppress_queue is False

    async def test_dispatch_handler_exception_propagates(self, make_context):
        """If a handler raises, the exception should propagate out of dispatch."""
        from summon_claude.sessions.commands import CommandDef

        async def _boom(args, ctx):
            raise ValueError("boom")

        COMMAND_ACTIONS["_test_boom"] = CommandDef(description="test", handler=_boom, max_args=0)
        try:
            context = make_context()
            with pytest.raises(ValueError, match="boom"):
                await dispatch("_test_boom", [], context)
        finally:
            COMMAND_ACTIONS.pop("_test_boom", None)
