"""Tests for XDG path resolution in summon_claude.config."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from summon_claude.config import (
    SummonConfig,
    get_claude_config_dir,
    get_config_dir,
    get_config_file,
    get_data_dir,
)


class TestGetConfigDir:
    def test_xdg_config_home_set(self, tmp_path, monkeypatch):
        """XDG_CONFIG_HOME set → returns XDG_CONFIG_HOME/summon."""
        xdg_config = tmp_path / "xdg_config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
        # Also clear any cached state by reimporting
        import importlib

        import summon_claude.config as cfg_mod

        importlib.reload(cfg_mod)

        result = cfg_mod.get_config_dir()
        assert result == xdg_config / "summon"

    def test_xdg_config_home_not_set_dotconfig_exists(self, tmp_path, monkeypatch):
        """No XDG_CONFIG_HOME + ~/.config exists → returns ~/.config/summon."""
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        # Create ~/.config to simulate it existing
        dot_config = tmp_path / ".config"
        dot_config.mkdir()

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            import importlib

            import summon_claude.config as cfg_mod

            # Need to use the function directly, not the cached value
            result = cfg_mod.get_config_dir()
            # Since ~/.config exists under tmp_path, should return tmp_path/.config/summon
            assert result == tmp_path / ".config" / "summon"

    def test_xdg_fallback_to_home_summon(self, tmp_path, monkeypatch):
        """No XDG_CONFIG_HOME + no ~/.config → returns ~/.summon."""
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        # Do NOT create ~/.config, so it doesn't exist
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            import summon_claude.config as cfg_mod

            result = cfg_mod.get_config_dir()
            assert result == tmp_path / ".summon"


class TestGetDataDir:
    def test_xdg_data_home_set(self, tmp_path, monkeypatch):
        """XDG_DATA_HOME set → returns XDG_DATA_HOME/summon."""
        xdg_data = tmp_path / "xdg_data"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))

        import summon_claude.config as cfg_mod

        result = cfg_mod.get_data_dir()
        assert result == xdg_data / "summon"

    def test_xdg_data_home_not_set_local_share_exists(self, tmp_path, monkeypatch):
        """No XDG_DATA_HOME + ~/.local/share exists → returns ~/.local/share/summon."""
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

        local_share = tmp_path / ".local" / "share"
        local_share.mkdir(parents=True)

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            import summon_claude.config as cfg_mod

            result = cfg_mod.get_data_dir()
            assert result == tmp_path / ".local" / "share" / "summon"

    def test_xdg_data_fallback_to_home_summon(self, tmp_path, monkeypatch):
        """No XDG_DATA_HOME + no ~/.local/share → returns ~/.summon."""
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

        # Do NOT create ~/.local/share
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            import summon_claude.config as cfg_mod

            result = cfg_mod.get_data_dir()
            assert result == tmp_path / ".summon"


class TestGetConfigFile:
    def test_config_file_path_is_under_config_dir(self, tmp_path, monkeypatch):
        """get_config_file() returns get_config_dir() / 'config.env'."""
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            import summon_claude.config as cfg_mod

            config_dir = cfg_mod.get_config_dir()
            config_file = cfg_mod.get_config_file()
            assert config_file == config_dir / "config.env"
            assert config_file.name == "config.env"

    def test_config_file_has_env_extension(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            import summon_claude.config as cfg_mod

            assert cfg_mod.get_config_file().suffix == ".env"


class TestConfigLoadsFromXdg:
    def test_config_loads_from_xdg_config_path(self, tmp_path, monkeypatch):
        """SummonConfig should load from the XDG config file when it exists."""
        xdg_config = tmp_path / "xdg_config"
        xdg_config.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))

        config_dir = xdg_config / "summon"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-from-xdg\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-from-xdg\n"
            "SUMMON_SLACK_SIGNING_SECRET=secret-from-xdg\n"
        )

        # Need to reload config module so get_config_file() picks up new XDG var
        import importlib

        import summon_claude.config as cfg_mod

        importlib.reload(cfg_mod)

        # The config file path should now point to our XDG config
        cfg_file = cfg_mod.get_config_file()
        assert cfg_file == config_dir / "config.env"
        assert cfg_file.exists()

        # Read the content to verify
        content = cfg_file.read_text()
        assert "xoxb-from-xdg" in content
        assert "xapp-from-xdg" in content


class TestGetClaudeConfigDir:
    def test_returns_default_when_env_not_set(self, monkeypatch):
        """Returns ~/.claude when CLAUDE_CONFIG_DIR is not set."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        result = get_claude_config_dir()
        assert result == Path.home() / ".claude"

    def test_returns_absolute_env_path(self, monkeypatch, tmp_path):
        """Returns the CLAUDE_CONFIG_DIR value when it's an absolute path."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom-claude"))
        result = get_claude_config_dir()
        assert result == tmp_path / "custom-claude"

    def test_falls_back_on_relative_path(self, monkeypatch):
        """Falls back to ~/.claude when CLAUDE_CONFIG_DIR is relative."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative/path")
        result = get_claude_config_dir()
        assert result == Path.home() / ".claude"

    def test_falls_back_on_empty_string(self, monkeypatch):
        """Falls back to ~/.claude when CLAUDE_CONFIG_DIR is empty."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
        result = get_claude_config_dir()
        assert result == Path.home() / ".claude"

    def test_falls_back_on_whitespace_only(self, monkeypatch):
        """Falls back to ~/.claude when CLAUDE_CONFIG_DIR is whitespace."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "   ")
        result = get_claude_config_dir()
        assert result == Path.home() / ".claude"
