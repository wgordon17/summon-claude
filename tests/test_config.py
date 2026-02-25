"""Tests for summon_claude.config."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from summon_claude.config import SummonConfig, discover_installed_plugins


def _make_config(**overrides) -> SummonConfig:
    """Create a SummonConfig with valid defaults, bypassing .env file loading."""
    defaults = {
        "slack_bot_token": "xoxb-test-token",
        "slack_app_token": "xapp-test-token",
        "slack_signing_secret": "test-secret",
    }
    defaults.update(overrides)
    # Prevent pydantic-settings from reading any .env file during tests
    with patch.dict(os.environ, {}, clear=False):
        return SummonConfig.model_validate(defaults)


class TestSummonConfigDefaults:
    def test_default_model(self):
        cfg = _make_config()
        assert cfg.default_model is None

    def test_default_model_can_be_set(self):
        cfg = _make_config(default_model="claude-opus-4-6")
        assert cfg.default_model == "claude-opus-4-6"

    def test_default_channel_prefix(self):
        cfg = _make_config()
        assert cfg.channel_prefix == "summon"

    def test_default_permission_debounce(self):
        cfg = _make_config()
        assert cfg.permission_debounce_ms == 500

    def test_default_max_inline_chars(self):
        cfg = _make_config()
        assert cfg.max_inline_chars == 2500


class TestSummonConfigValidate:
    def test_valid_config_passes(self):
        cfg = _make_config()
        cfg.validate()  # should not raise

    def test_missing_bot_token_raises(self):
        cfg = _make_config(slack_bot_token="")
        with pytest.raises(ValueError, match="SUMMON_SLACK_BOT_TOKEN"):
            cfg.validate()

    def test_invalid_bot_token_prefix_raises(self):
        with pytest.raises(ValueError, match="xoxb-"):
            _make_config(slack_bot_token="xoxp-wrong-prefix")

    def test_missing_app_token_raises(self):
        cfg = _make_config(slack_app_token="")
        with pytest.raises(ValueError, match="SUMMON_SLACK_APP_TOKEN"):
            cfg.validate()

    def test_invalid_app_token_prefix_raises(self):
        with pytest.raises(ValueError, match="xapp-"):
            _make_config(slack_app_token="xoxb-wrong-prefix")

    def test_missing_signing_secret_raises(self):
        cfg = _make_config(slack_signing_secret="")
        with pytest.raises(ValueError, match="SUMMON_SLACK_SIGNING_SECRET"):
            cfg.validate()

    def test_multiple_errors_reported_together(self):
        cfg = _make_config(slack_bot_token="", slack_signing_secret="")
        with pytest.raises(ValueError) as exc_info:
            cfg.validate()
        msg = str(exc_info.value)
        assert "SUMMON_SLACK_BOT_TOKEN" in msg
        assert "SUMMON_SLACK_SIGNING_SECRET" in msg


class TestDiscoverInstalledPlugins:
    def test_missing_registry_returns_empty(self, tmp_path):
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            result = discover_installed_plugins()
        assert result == []

    def test_empty_registry_returns_empty(self, tmp_path):
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "installed_plugins.json").write_text("[]")

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            result = discover_installed_plugins()
        assert result == []

    def test_valid_plugins_returned(self, tmp_path):
        plugin_dir = tmp_path / ".claude" / "myplugin"
        plugin_dir.mkdir(parents=True)

        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        registry = [{"installPath": str(plugin_dir)}]
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            result = discover_installed_plugins()

        assert len(result) == 1
        assert result[0]["type"] == "local"
        assert result[0]["path"] == str(plugin_dir)

    def test_plugin_outside_claude_dir_rejected(self, tmp_path):
        plugin_dir = tmp_path / "outside_claude_dir"
        plugin_dir.mkdir()

        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        registry = [{"installPath": str(plugin_dir)}]
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            result = discover_installed_plugins()
        assert result == []

    def test_stale_plugin_path_skipped(self, tmp_path):
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        registry = [{"installPath": "/nonexistent/path/that/does/not/exist"}]
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            result = discover_installed_plugins()
        assert result == []

    def test_alternate_path_key_supported(self, tmp_path):
        plugin_dir = tmp_path / ".claude" / "altplugin"
        plugin_dir.mkdir(parents=True)

        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        registry = [{"path": str(plugin_dir)}]
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            result = discover_installed_plugins()
        assert len(result) == 1

    def test_invalid_json_returns_empty(self, tmp_path):
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "installed_plugins.json").write_text("not json {{{")

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            result = discover_installed_plugins()
        assert result == []

    def test_non_list_registry_returns_empty(self, tmp_path):
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "installed_plugins.json").write_text('{"installPath": "/foo"}')

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            result = discover_installed_plugins()
        assert result == []

    def test_v2_format_supported(self, tmp_path):
        plugin_dir = tmp_path / ".claude" / "plugins" / "cache" / "myplugin"
        plugin_dir.mkdir(parents=True)

        plugins_dir = tmp_path / ".claude" / "plugins"
        registry = {
            "version": 2,
            "plugins": {
                "myplugin@org": [
                    {"installPath": str(plugin_dir), "version": "1.0.0"},
                ],
            },
        }
        (plugins_dir / "installed_plugins.json").write_text(json.dumps(registry))

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            result = discover_installed_plugins()
        assert len(result) == 1
        assert result[0]["type"] == "local"
        assert result[0]["path"] == str(plugin_dir)
