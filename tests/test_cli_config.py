"""Tests for summon_claude.cli.config — config set sentinel and soft-validation."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from summon_claude.cli.config import config_set


class TestConfigSetModelValidation:
    def test_config_set_known_model(self, tmp_path):
        """Known model in choices → accepted with exit code 0, no warning."""
        config_file = tmp_path / "config.env"
        config_file.write_text("")

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=([{"value": "claude-opus-4-6"}], None),
            ),
        ):
            # Should not raise SystemExit
            config_set("SUMMON_DEFAULT_MODEL", "claude-opus-4-6")

        content = config_file.read_text()
        assert "SUMMON_DEFAULT_MODEL=claude-opus-4-6" in content

    def test_config_set_custom_model_with_warning(self, tmp_path, capsys):
        """Custom model not in choices → accepted (exit 0), warning in output."""
        config_file = tmp_path / "config.env"
        config_file.write_text("")

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=([{"value": "claude-opus-4-6"}], None),
            ),
        ):
            config_set("SUMMON_DEFAULT_MODEL", "my-custom-model")

        content = config_file.read_text()
        assert "SUMMON_DEFAULT_MODEL=my-custom-model" in content
        # Warning is printed via click.echo(err=True) → captured in stderr
        captured = capsys.readouterr()
        assert "Warning" in captured.err and "my-custom-model" in captured.err

    def test_config_set_other_sentinel_blocked(self, tmp_path):
        """Literal 'other' sent to a model field → hard rejection."""
        config_file = tmp_path / "config.env"
        config_file.write_text("")

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=([{"value": "claude-opus-4-6"}], None),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            config_set("SUMMON_DEFAULT_MODEL", "other")

        assert exc_info.value.code != 0

    def test_config_set_default_auto_sentinel_blocked(self, tmp_path):
        """Literal 'default (auto)' sent to a model field → hard rejection."""
        config_file = tmp_path / "config.env"
        config_file.write_text("")

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=([{"value": "claude-opus-4-6"}], None),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            config_set("SUMMON_DEFAULT_MODEL", "default (auto)")

        assert exc_info.value.code != 0

    def test_config_set_invalid_effort_rejected(self, tmp_path):
        """Invalid effort value → hard rejection (no validate_fn, static choices only)."""
        config_file = tmp_path / "config.env"
        config_file.write_text("")

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            pytest.raises(SystemExit) as exc_info,
        ):
            config_set("SUMMON_DEFAULT_EFFORT", "invalid")

        assert exc_info.value.code != 0

    def test_config_set_empty_string_model_bypasses_validation(self, tmp_path):
        """Empty string bypasses choices validation — documents existing 'clear' behavior."""
        config_file = tmp_path / "config.env"
        config_file.write_text("SUMMON_DEFAULT_MODEL=old-model\n")

        with patch("summon_claude.cli.config.get_config_file", return_value=config_file):
            config_set("SUMMON_DEFAULT_MODEL", "")

        content = config_file.read_text()
        assert "SUMMON_DEFAULT_MODEL=" in content
