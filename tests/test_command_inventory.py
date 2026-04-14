"""Tests for the declarative command inventory."""

from __future__ import annotations

from summon_claude.sessions.commands import (
    _ALIAS_LOOKUP,
    COMMAND_ACTIONS,
)


class TestCommandDefValidStates:
    """Every CommandDef must have valid state: handler XOR block_reason XOR neither."""

    def test_no_command_has_both_handler_and_block_reason(self):
        for name, defn in COMMAND_ACTIONS.items():
            assert not (defn.handler and defn.block_reason), (
                f"'{name}' has both handler and block_reason"
            )

    def test_all_commands_have_description(self):
        for name, defn in COMMAND_ACTIONS.items():
            assert defn.description, f"'{name}' missing description"


class TestAliasLookupIntegrity:
    def test_all_aliases_resolve_to_command_actions_keys(self):
        for alias, canonical in _ALIAS_LOOKUP.items():
            assert canonical in COMMAND_ACTIONS, (
                f"Alias '{alias}' -> '{canonical}' not in COMMAND_ACTIONS"
            )

    def test_no_alias_shadows_a_command_actions_key(self):
        for alias in _ALIAS_LOOKUP:
            assert alias not in COMMAND_ACTIONS, f"Alias '{alias}' shadows a key in COMMAND_ACTIONS"


class TestLocalCommands:
    def test_all_local_handlers_are_callable(self):
        for name, defn in COMMAND_ACTIONS.items():
            if defn.handler:
                assert callable(defn.handler), f"'{name}' handler not callable"

    def test_all_local_commands_have_max_args_defined(self):
        """LOCAL commands must have max_args set to int or None."""
        for name, defn in COMMAND_ACTIONS.items():
            if defn.handler:
                assert isinstance(defn.max_args, int) or defn.max_args is None, (
                    f"'{name}' max_args must be int or None, got {type(defn.max_args)}"
                )


class TestExpectedCommands:
    """Verify known commands exist in COMMAND_ACTIONS."""

    EXPECTED_LOCAL = {
        "help",
        "status",
        "end",
        "clear",
        "stop",
        "model",
        "compact",
        "effort",
        "auto",
        "summon",
        "diff",
        "show",
        "changes",
    }
    EXPECTED_PASSTHROUGH = {
        "review",
        "init",
        "pr-comments",
        "security-review",
        "debug",
        "claude-developer-platform",
    }
    EXPECTED_BLOCKED_SPECIFIC = {"insights", "context", "cost", "release-notes", "login"}

    def test_all_expected_local_commands_present(self):
        for name in self.EXPECTED_LOCAL:
            assert name in COMMAND_ACTIONS, f"Local command '{name}' missing"
            assert COMMAND_ACTIONS[name].handler is not None, f"'{name}' should have handler"

    def test_all_expected_passthrough_commands_present(self):
        for name in self.EXPECTED_PASSTHROUGH:
            assert name in COMMAND_ACTIONS, f"Passthrough command '{name}' missing"
            defn = COMMAND_ACTIONS[name]
            assert defn.handler is None and defn.block_reason is None, (
                f"'{name}' should be passthrough (no handler, no block_reason)"
            )

    def test_all_expected_blocked_commands_present(self):
        for name in self.EXPECTED_BLOCKED_SPECIFIC:
            assert name in COMMAND_ACTIONS, f"Blocked command '{name}' missing"
            assert COMMAND_ACTIONS[name].block_reason is not None, (
                f"'{name}' should have block_reason"
            )

    def test_expected_aliases(self):
        expected = {
            "quit": "end",
            "exit": "end",
            "logout": "end",
            "new": "clear",
            "reset": "clear",
            "settings": "config",
        }
        for alias, canonical in expected.items():
            assert _ALIAS_LOOKUP.get(alias) == canonical, (
                f"Expected alias '{alias}' -> '{canonical}'"
            )
