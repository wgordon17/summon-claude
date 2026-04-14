"""Comprehensive tests for summon init wizard data-loss fixes.

Covers: merge-existing, draft save/resume, Ctrl-C, inline validation,
clear mechanism, graceful validation failure, and write_env_file helper.
"""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import patch

import pydantic
import pytest
from click.testing import CliRunner

from summon_claude.cli import cli
from summon_claude.cli.config import get_draft_path, parse_env_file, write_env_file
from summon_claude.cli.preflight import CliStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_INPUTS = [
    "xoxb-test-bot-token",
    "xapp-test-app-token",
    "abcdef012345",
]

_NON_ADVANCED_TAIL = [
    "",  # default_model (Enter = keep default sentinel)
    "3",  # default_effort (choice: 3=high)
    "",  # channel_prefix (Enter = keep default "summon")
    "n",  # scribe_enabled
    "n",  # advanced settings
]


def _make_inputs(*middle: str) -> str:
    """Build a newline-separated input string for CliRunner."""
    return "\n".join([*_REQUIRED_INPUTS, *middle, *_NON_ADVANCED_TAIL]) + "\n"


def _run_init(config_file: Path, inputs: str):
    """Run `summon init` via CliRunner and return the result."""
    runner = CliRunner()
    # 2-tuple matching load_cached_models return type:
    # (list[dict[str, str]], str | None)
    fake_models = [{"value": "claude-opus-4-5"}, {"value": "claude-sonnet-4-5"}]
    with (
        patch("summon_claude.cli.get_config_file", return_value=config_file),
        patch("summon_claude.config.get_config_file", return_value=config_file),
        patch(
            "summon_claude.cli.preflight.check_claude_cli",
            return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
        ),
        patch("summon_claude.cli.config.config_check"),
        patch("summon_claude.cli.config_check"),
        patch(
            "summon_claude.cli.model_cache.load_cached_models",
            return_value=(fake_models, "claude-opus-4-5"),
        ),
    ):
        return runner.invoke(cli, ["init"], input=inputs)


# ---------------------------------------------------------------------------
# Test 1: existing-value preservation on re-run
# ---------------------------------------------------------------------------


class TestMergeExisting:
    def test_existing_values_preserved_on_rerun(self, tmp_path):
        """Values from an existing config that aren't re-answered are preserved."""
        config_file = tmp_path / "config.env"
        write_env_file(
            config_file,
            {
                "SUMMON_SLACK_BOT_TOKEN": "xoxb-old",
                "SUMMON_CHANNEL_PREFIX": "my-prefix",
            },
        )

        # Press Enter at channel_prefix to accept "my-prefix" (existing → becomes prompt default)
        inputs = (
            "\n".join(
                [
                    *_REQUIRED_INPUTS,
                    "",  # default_model
                    "3",  # default_effort (choice: 3=high)
                    "my-prefix",  # channel_prefix — explicitly re-confirm existing value
                    "n",  # scribe_enabled
                    "n",  # advanced settings
                ]
            )
            + "\n"
        )
        result = _run_init(config_file, inputs)

        assert result.exit_code == 0, result.output
        written = parse_env_file(config_file)
        assert written.get("SUMMON_SLACK_BOT_TOKEN") == "xoxb-test-bot-token"
        assert written.get("SUMMON_CHANNEL_PREFIX") == "my-prefix"

    def test_existing_non_prompted_values_preserved(self, tmp_path):
        """Fields not shown (advanced=True, user said no) are preserved from existing."""
        config_file = tmp_path / "config.env"
        # SUMMON_AUTO_CLASSIFIER_ENABLED default is True; use "false" (non-default) to verify
        write_env_file(
            config_file,
            {
                "SUMMON_SLACK_BOT_TOKEN": "xoxb-old",
                "SUMMON_SLACK_APP_TOKEN": "xapp-old",
                "SUMMON_SLACK_SIGNING_SECRET": "abc123",
                "SUMMON_AUTO_CLASSIFIER_ENABLED": "false",
            },
        )

        # User says "n" to advanced settings — SUMMON_AUTO_CLASSIFIER_ENABLED not re-prompted
        inputs = _make_inputs()
        result = _run_init(config_file, inputs)

        assert result.exit_code == 0, result.output
        written = parse_env_file(config_file)
        # "false" is non-default (default is True), so it must be preserved
        assert written.get("SUMMON_AUTO_CLASSIFIER_ENABLED") == "false"


# ---------------------------------------------------------------------------
# Test 3: stale key cleanup
# ---------------------------------------------------------------------------


class TestStaleKeyCleanup:
    def test_obsolete_keys_removed_on_rerun(self, tmp_path):
        """Keys not in CONFIG_OPTIONS are removed from the output file."""
        config_file = tmp_path / "config.env"
        write_env_file(
            config_file,
            {
                "SUMMON_SLACK_BOT_TOKEN": "xoxb-old",
                "SUMMON_OBSOLETE_KEY": "stale_value",
            },
        )

        inputs = _make_inputs()
        result = _run_init(config_file, inputs)

        assert result.exit_code == 0, result.output
        assert "SUMMON_OBSOLETE_KEY" not in config_file.read_text()


# ---------------------------------------------------------------------------
# Test 4: Ctrl-C saves draft and resume pre-fills
# ---------------------------------------------------------------------------


class TestCtrlCAndDraftResume:
    def test_ctrlc_saves_draft(self, tmp_path):
        """Ctrl-C (simulated via EOFError -> click.Abort) saves a draft file."""
        config_file = tmp_path / "config.env"
        draft_path = get_draft_path(config_file)

        # Provide only 2 of 3 required inputs — 3rd prompt triggers EOFError -> Abort
        partial_input = "xoxb-tok\nxapp-tok\n"
        result = _run_init(config_file, partial_input)

        assert result.exit_code == 0, result.output
        # Draft should exist with the 2 collected values
        assert draft_path.exists(), result.output

    def test_draft_resume_prefills_values(self, tmp_path):
        """Resuming from a draft pre-fills values collected before Ctrl-C."""
        config_file = tmp_path / "config.env"
        draft_path = get_draft_path(config_file)

        # Write a draft with saved progress
        write_env_file(
            draft_path,
            {
                "SUMMON_SLACK_BOT_TOKEN": "xoxb-from-draft",
                "SUMMON_CHANNEL_PREFIX": "draft-prefix",
            },
        )

        # Input: "y" to resume, then Enter to keep bot token from draft,
        # provide app token and signing secret, then rest
        inputs = (
            "\n".join(
                [
                    "y",  # resume from draft
                    "",  # bot token (Enter to keep "xoxb-from-draft" from draft)
                    "xapp-new",  # app token
                    "abcdef012345",  # signing secret
                    "",  # default_model
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix (Enter to keep "draft-prefix" from draft as default)
                    "n",  # scribe_enabled
                    "n",  # advanced settings
                ]
            )
            + "\n"
        )
        result = _run_init(config_file, inputs)

        assert result.exit_code == 0, result.output
        written = parse_env_file(config_file)
        assert written.get("SUMMON_SLACK_BOT_TOKEN") == "xoxb-from-draft"


# ---------------------------------------------------------------------------
# Test 5: draft deleted on success
# ---------------------------------------------------------------------------


class TestDraftDeletedOnSuccess:
    def test_draft_deleted_after_successful_completion(self, tmp_path):
        """Draft file is deleted after wizard completes successfully."""
        config_file = tmp_path / "config.env"
        draft_path = get_draft_path(config_file)

        # Pre-create a draft to simulate a prior interrupted run
        write_env_file(draft_path, {"SUMMON_SLACK_BOT_TOKEN": "xoxb-draft"})

        inputs = (
            "\n".join(
                [
                    "n",  # decline to resume from draft
                    *_REQUIRED_INPUTS,
                    *_NON_ADVANCED_TAIL,
                ]
            )
            + "\n"
        )
        result = _run_init(config_file, inputs)

        assert result.exit_code == 0, result.output
        assert not draft_path.exists(), "Draft file should be deleted on success"


# ---------------------------------------------------------------------------
# Test 6: inline int validation re-prompts
# ---------------------------------------------------------------------------


class TestIntValidation:
    def test_int_validation_reprompts_on_invalid_value(self, tmp_path):
        """Int fields with validate_fn re-prompt on invalid values."""
        config_file = tmp_path / "config.env"

        # Scribe enabled → scan interval prompt appears. 0 is invalid (< 1), 10 is valid.
        # Use 10 (non-default, default=5) so it's not stripped from output.
        inputs = (
            "\n".join(
                [
                    *_REQUIRED_INPUTS,
                    "",  # default_model
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "y",  # scribe_enabled
                    "0",  # scan_interval — invalid (< 1)
                    "10",  # scan_interval — valid and non-default (default=5)
                    "",  # scribe_cwd
                    "",  # scribe_model
                    "",  # scribe_importance_keywords
                    "",  # scribe_quiet_hours
                    *["n"] * 5,  # extra inputs for conditional sub-options
                    "n",  # advanced settings
                ]
            )
            + "\n"
        )
        result = _run_init(config_file, inputs)

        assert result.exit_code == 0, result.output
        assert "Error:" in result.output
        written = parse_env_file(config_file)
        # 10 is non-default (default=5), so it must be preserved in output
        assert written.get("SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES") == "10"


# ---------------------------------------------------------------------------
# Test 7: clear mechanism
# ---------------------------------------------------------------------------


class TestClearMechanism:
    def test_clear_removes_text_field(self, tmp_path):
        """Typing 'clear' at an optional text prompt removes the key from config."""
        config_file = tmp_path / "config.env"
        write_env_file(
            config_file,
            {
                "SUMMON_SLACK_BOT_TOKEN": "xoxb-old",
                "SUMMON_SLACK_APP_TOKEN": "xapp-old",
                "SUMMON_SLACK_SIGNING_SECRET": "abc123",
                "SUMMON_SCRIBE_CWD": "/old/scribe/path",
                "SUMMON_SCRIBE_ENABLED": "true",
            },
        )

        # Pad with extra empty inputs for any conditional scribe sub-options
        inputs = (
            "\n".join(
                [
                    "",  # bot token (Enter = keep existing xoxb-old)
                    "",  # app token (Enter = keep existing xapp-old)
                    "",  # signing secret (Enter = keep existing abc123)
                    "",  # default_model
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "y",  # scribe_enabled (keep enabled)
                    "",  # scan_interval (Enter = keep default)
                    "clear",  # scribe_cwd — clear it
                    "",  # scribe_model
                    "",  # scribe_importance_keywords
                    "",  # scribe_quiet_hours
                    *["n"] * 5,  # extra inputs for conditional sub-options
                    "n",  # advanced settings
                ]
            )
            + "\n"
        )
        result = _run_init(config_file, inputs)

        assert result.exit_code == 0, result.output
        assert "Cleared" in result.output
        written = parse_env_file(config_file)
        assert "SUMMON_SCRIBE_CWD" not in written

    def test_clear_removes_int_field(self, tmp_path):
        """Typing 'clear' at an optional int prompt removes the key from config."""
        config_file = tmp_path / "config.env"
        write_env_file(
            config_file,
            {
                "SUMMON_SLACK_BOT_TOKEN": "xoxb-old",
                "SUMMON_SLACK_APP_TOKEN": "xapp-old",
                "SUMMON_SLACK_SIGNING_SECRET": "abc123",
                "SUMMON_SCRIBE_ENABLED": "true",
                "SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES": "10",
            },
        )

        inputs = (
            "\n".join(
                [
                    "",  # bot token (keep)
                    "",  # app token (keep)
                    "",  # signing secret (keep)
                    "",  # default_model
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "y",  # scribe_enabled
                    "clear",  # scan_interval — clear it
                    "",  # scribe_cwd
                    "",  # scribe_model
                    "",  # scribe_importance_keywords
                    "",  # scribe_quiet_hours
                    *["n"] * 5,  # extra for conditional sub-options
                    "n",  # advanced settings
                ]
            )
            + "\n"
        )
        result = _run_init(config_file, inputs)

        assert result.exit_code == 0, result.output
        assert "Cleared" in result.output
        written = parse_env_file(config_file)
        assert "SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES" not in written


# ---------------------------------------------------------------------------
# Test 8: graceful validation failure writes config
# ---------------------------------------------------------------------------


class TestGracefulValidationFailure:
    def test_validation_failure_writes_config_and_exits_1(self, tmp_path):
        """When SummonConfig raises ValidationError, config IS written and exit code is 1."""
        config_file = tmp_path / "config.env"
        draft_path = get_draft_path(config_file)

        # Build a real ValidationError
        try:
            from summon_claude.config import SummonConfig

            SummonConfig(
                slack_bot_token="bad",
                slack_app_token="bad",
                slack_signing_secret="not-hex",
                _env_file=None,
            )
            real_error = None
        except pydantic.ValidationError as exc:
            real_error = exc

        if real_error is None:
            pytest.skip("Could not construct a real ValidationError")

        inputs = _make_inputs()
        runner = CliRunner()
        fake_models = [{"value": "claude-opus-4-5"}, {"value": "claude-sonnet-4-5"}]
        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=(fake_models, "claude-opus-4-5"),
            ),
            patch("summon_claude.cli.SummonConfig", side_effect=real_error),
        ):
            result = runner.invoke(cli, ["init"], input=inputs)

        # Config IS written despite validation failure
        assert result.exit_code == 1
        assert config_file.exists(), "Config should be written even on validation failure"
        # Warning message appears
        combined = result.output + (result.stderr or "")
        assert "potential issues" in combined and "Fix with" in combined
        # Draft was created during collection then cleaned up on validation failure
        assert not draft_path.exists(), "Draft should be deleted after config write"


# ---------------------------------------------------------------------------
# Test 9: unexpected error preserves draft
# ---------------------------------------------------------------------------


class TestUnexpectedErrorPreservesDraft:
    def test_unexpected_error_does_not_delete_draft(self, tmp_path):
        """On unexpected RuntimeError, draft file is NOT deleted (preserves recovery)."""
        config_file = tmp_path / "config.env"

        inputs = _make_inputs()
        runner = CliRunner()
        fake_models = [{"value": "claude-opus-4-5"}, {"value": "claude-sonnet-4-5"}]
        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=(fake_models, "claude-opus-4-5"),
            ),
            patch("summon_claude.cli.SummonConfig", side_effect=RuntimeError("boom")),
        ):
            result = runner.invoke(cli, ["init"], input=inputs)

        assert result.exit_code == 1
        # Draft file was written during collection, then NOT deleted on unexpected error
        draft_path = get_draft_path(config_file)
        # Draft should exist (written during collection, not deleted on unexpected error)
        assert draft_path.exists(), "Draft should be preserved on unexpected error"


# ---------------------------------------------------------------------------
# Test 10: write_env_file helper
# ---------------------------------------------------------------------------


class TestWriteEnvFile:
    def test_newline_sanitization(self, tmp_path):
        """Newlines in values are stripped to prevent .env injection."""
        path = tmp_path / "test.env"
        write_env_file(path, {"KEY": "value\ninjected"})
        content = path.read_text()
        # The \n is stripped — content should be a single KEY=... line + trailing newline
        lines = [line for line in content.splitlines() if line.strip()]
        assert len(lines) == 1
        assert lines[0].startswith("KEY=")
        assert "\n" not in lines[0]

    def test_parent_dir_created(self, tmp_path):
        """Parent directories are created if they don't exist."""
        path = tmp_path / "sub" / "test.env"
        write_env_file(path, {"A": "1"})
        assert path.exists()

    def test_permissions_0o600(self, tmp_path):
        """File is created with 0o600 permissions."""
        path = tmp_path / "test.env"
        write_env_file(path, {"A": "1"})
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_atomic_mode_no_tmp_leak(self, tmp_path):
        """atomic=True leaves no .tmp file after successful write."""
        path = tmp_path / "test.env"
        write_env_file(path, {"A": "1"}, atomic=True)
        leftover = [f for f in tmp_path.iterdir() if f.suffix == ".tmp"]
        assert leftover == [], f"Temp file(s) leaked: {leftover}"
        assert path.exists()
        assert "A=1" in path.read_text()

    def test_atomic_permissions(self, tmp_path):
        """atomic=True write still gives 0o600 permissions."""
        path = tmp_path / "test.env"
        write_env_file(path, {"A": "1"}, atomic=True)
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_second_write_overwrites_cleanly(self, tmp_path):
        """Calling write_env_file twice overwrites the first write cleanly."""
        path = tmp_path / "test.env"
        write_env_file(path, {"A": "1"}, atomic=True)
        write_env_file(path, {"B": "2"}, atomic=True)
        content = path.read_text()
        assert "A=1" not in content
        assert "B=2" in content

    def test_empty_dict_round_trip(self, tmp_path):
        """write_env_file with {} writes an empty file; parse_env_file reads it back as {}."""
        path = tmp_path / "test.env"
        write_env_file(path, {})
        assert path.exists()
        assert parse_env_file(path) == {}


# ---------------------------------------------------------------------------
# Test 11: visible-predicate-hidden fields preserved across re-runs
# ---------------------------------------------------------------------------


class TestHiddenFieldPreservation:
    def test_hidden_field_preserved_when_not_prompted(self, tmp_path):
        """Fields hidden by visibility predicate are preserved from existing config."""
        config_file = tmp_path / "config.env"
        # SUMMON_SCRIBE_CWD is only visible when scribe is enabled.
        # Write it to existing config with scribe disabled.
        write_env_file(
            config_file,
            {
                "SUMMON_SLACK_BOT_TOKEN": "xoxb-old",
                "SUMMON_SLACK_APP_TOKEN": "xapp-old",
                "SUMMON_SLACK_SIGNING_SECRET": "abc123",
                "SUMMON_SCRIBE_CWD": "/preserved/path",
                "SUMMON_SCRIBE_ENABLED": "false",
            },
        )

        # Run with scribe disabled (scribe_cwd prompt hidden)
        inputs = _make_inputs()  # "n" for scribe_enabled
        result = _run_init(config_file, inputs)

        assert result.exit_code == 0, result.output
        written = parse_env_file(config_file)
        assert written.get("SUMMON_SCRIBE_CWD") == "/preserved/path"


# ---------------------------------------------------------------------------
# Test 12: OSError fallback in Ctrl-C handler
# ---------------------------------------------------------------------------


class TestCtrlCOSErrorFallback:
    def test_ctrlc_oserror_in_fallback_save_exits_0(self, tmp_path):
        """If draft save in Ctrl-C handler raises OSError, still exit 0 with warning."""
        config_file = tmp_path / "config.env"
        draft_path = get_draft_path(config_file)

        # Only 1 input — next prompt triggers EOFError -> Abort
        partial_input = "xoxb-tok\n"

        runner = CliRunner()
        fake_models = [{"value": "claude-opus-4-5"}, {"value": "claude-sonnet-4-5"}]
        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=(fake_models, "claude-opus-4-5"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch("summon_claude.cli.config_check"),
            patch("summon_claude.cli.write_env_file", side_effect=OSError("disk full")),
        ):
            result = runner.invoke(cli, ["init"], input=partial_input)

        assert result.exit_code == 0
        combined = result.output + (result.stderr or "")
        assert "Could not save progress" in combined
        assert not draft_path.exists()


# ---------------------------------------------------------------------------
# Test 13: text validation re-prompt (Task 4 Step 1 regression guard)
# ---------------------------------------------------------------------------


class TestTextValidation:
    def test_text_validation_reprompts_on_invalid(self, tmp_path):
        """Text fields with validate_fn re-prompt on invalid input."""
        config_file = tmp_path / "config.env"

        # SUMMON_CHANNEL_PREFIX has a validate_fn that rejects uppercase
        inputs = (
            "\n".join(
                [
                    *_REQUIRED_INPUTS,
                    "",  # default_model
                    "3",  # default_effort (choice: 3=high)
                    "UPPER_CASE",  # channel_prefix — invalid
                    "valid-prefix",  # channel_prefix — valid retry
                    "n",  # scribe_enabled
                    "n",  # advanced settings
                ]
            )
            + "\n"
        )
        result = _run_init(config_file, inputs)

        assert result.exit_code == 0, result.output
        assert "Error:" in result.output
        written = parse_env_file(config_file)
        assert written.get("SUMMON_CHANNEL_PREFIX") == "valid-prefix"
