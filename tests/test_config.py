"""Tests for summon_claude.config."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from summon_claude.config import (
    PluginSkill,
    SummonConfig,
    _parse_frontmatter,
    discover_installed_plugins,
    discover_plugin_skills,
)


def _make_config(**overrides) -> SummonConfig:
    """Create a SummonConfig with test defaults; override any field via kwargs."""
    return SummonConfig(**overrides)


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
        assert cfg.permission_debounce_ms == 2000

    def test_default_max_inline_chars(self):
        cfg = _make_config()
        assert cfg.max_inline_chars == 2500

    def test_default_effort(self):
        cfg = _make_config()
        assert cfg.default_effort == "high"

    def test_default_effort_can_be_set(self):
        cfg = _make_config(default_effort="low")
        assert cfg.default_effort == "low"

    def test_default_effort_all_valid_values(self):
        for level in ("low", "medium", "high", "max"):
            cfg = _make_config(default_effort=level)
            assert cfg.default_effort == level

    def test_default_effort_rejects_invalid(self):
        with pytest.raises(ValueError, match="SUMMON_DEFAULT_EFFORT"):
            _make_config(default_effort="ultra")


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


class TestGitHubMCPConfig:
    def test_github_mcp_config_returns_none_when_no_token(self):
        cfg = _make_config()
        with patch("summon_claude.github_auth.load_token", return_value=None):
            assert cfg.github_mcp_config() is None

    def test_github_mcp_config_returns_dict_when_token_exists(self):
        cfg = _make_config()
        with patch("summon_claude.github_auth.load_token", return_value="gho_test123"):
            result = cfg.github_mcp_config()
        assert result is not None
        assert result["type"] == "http"
        assert result["url"] == "https://api.githubcopilot.com/mcp/"
        assert result["headers"]["Authorization"] == "Bearer gho_test123"

    def test_github_mcp_config_dict_structure(self):
        cfg = _make_config()
        with patch("summon_claude.github_auth.load_token", return_value="gho_xyz"):
            result = cfg.github_mcp_config()
        assert set(result.keys()) == {"type", "url", "headers"}
        assert "Authorization" in result["headers"]


class TestSlackAppId:
    def test_parses_valid_app_id(self):
        cfg = _make_config(slack_app_token="xapp-1-A0123ABCDE-12345-abc")
        assert cfg.slack_app_id == "A0123ABCDE"

    def test_parses_12_char_app_id(self):
        cfg = _make_config(slack_app_token="xapp-1-A0123ABCDE12-12345-abc")
        assert cfg.slack_app_id == "A0123ABCDE12"

    def test_returns_none_for_malformed_token(self):
        cfg = _make_config(slack_app_token="xapp-test-token")
        assert cfg.slack_app_id is None

    def test_returns_none_for_non_conforming_app_id(self):
        cfg = _make_config(slack_app_token="xapp-1-abc12345678-12345-xyz")
        assert cfg.slack_app_id is None

    def test_returns_none_for_app_id_too_short(self):
        cfg = _make_config(slack_app_token="xapp-1-A01234-12345-abc")
        assert cfg.slack_app_id is None

    def test_slack_app_url_with_app_id(self):
        cfg = _make_config(slack_app_token="xapp-1-A0123ABCDE-12345-abc")
        assert cfg.slack_app_url == "https://api.slack.com/apps/A0123ABCDE"

    def test_slack_app_url_without_app_id(self):
        cfg = _make_config(slack_app_token="xapp-test-token")
        assert cfg.slack_app_url == "https://api.slack.com/apps"


class TestSecretFieldsHiddenFromRepr:
    """Sensitive fields must not appear in repr() or str() output."""

    def test_repr_hides_secrets(self):
        cfg = _make_config()
        r = repr(cfg)
        assert "xoxb-test-token" not in r
        assert "xapp-test-token" not in r
        assert "abc123def456" not in r


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


# ------------------------------------------------------------------
# Frontmatter parsing
# ------------------------------------------------------------------


class TestParseFrontmatter:
    def test_basic_frontmatter(self):
        text = "---\nname: my-skill\ndescription: A skill\n---\n# Body"
        fm = _parse_frontmatter(text)
        assert fm["name"] == "my-skill"
        assert fm["description"] == "A skill"

    def test_no_frontmatter(self):
        assert _parse_frontmatter("# Just markdown") == {}

    def test_quoted_values_stripped(self):
        text = "---\nname: \"my-skill\"\ndescription: 'A skill'\n---\n"
        fm = _parse_frontmatter(text)
        assert fm["name"] == "my-skill"
        assert fm["description"] == "A skill"

    def test_empty_frontmatter(self):
        text = "---\n---\n# Body"
        assert _parse_frontmatter(text) == {}

    def test_block_scalar_folded(self):
        text = "---\nname: my-skill\ndescription: >-\n  First line\n  second line\n---\n"
        fm = _parse_frontmatter(text)
        assert fm["name"] == "my-skill"
        assert fm["description"] == "First line second line"

    def test_block_scalar_literal(self):
        text = "---\nname: my-skill\ndescription: |\n  Line one\n  Line two\n---\n"
        fm = _parse_frontmatter(text)
        assert fm["description"] == "Line one Line two"

    def test_list_value_captured(self):
        text = "---\nname: test\nallowed-tools: [Bash, Read, Write]\n---\n"
        fm = _parse_frontmatter(text)
        assert fm["allowed-tools"] == "[Bash, Read, Write]"


# ------------------------------------------------------------------
# Plugin skill discovery
# ------------------------------------------------------------------


def _make_plugin(tmp_path, plugin_name, *, commands=None, skills=None):
    """Create a plugin directory structure under tmp_path/.claude/."""
    plugin_dir = tmp_path / ".claude" / "plugins" / "cache" / plugin_name
    manifest_dir = plugin_dir / ".claude-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(json.dumps({"name": plugin_name}))

    if commands:
        cmd_dir = plugin_dir / "commands"
        cmd_dir.mkdir()
        for name, desc in commands.items():
            (cmd_dir / f"{name}.md").write_text(f"---\ndescription: {desc}\n---\n")

    if skills:
        skills_dir = plugin_dir / "skills"
        for name, desc in skills.items():
            skill_dir = skills_dir / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n")

    # Register plugin in installed_plugins.json
    plugins_dir = tmp_path / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    registry_path = plugins_dir / "installed_plugins.json"
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else []
    registry.append({"installPath": str(plugin_dir)})
    registry_path.write_text(json.dumps(registry))

    return plugin_dir


class TestDiscoverPluginSkills:
    def test_discovers_commands(self, tmp_path):
        _make_plugin(tmp_path, "my-plugin", commands={"session-start": "Start session"})
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            results = discover_plugin_skills()
        assert len(results) == 1
        assert results[0] == PluginSkill("my-plugin", "session-start", "Start session")

    def test_discovers_skills(self, tmp_path):
        _make_plugin(tmp_path, "my-plugin", skills={"uv-python": "Use uv"})
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            results = discover_plugin_skills()
        assert len(results) == 1
        assert results[0] == PluginSkill("my-plugin", "uv-python", "Use uv")

    def test_discovers_both(self, tmp_path):
        _make_plugin(
            tmp_path,
            "dev-tools",
            commands={"init": "Initialize"},
            skills={"linting": "Lint code"},
        )
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            results = discover_plugin_skills()
        assert len(results) == 2
        names = {r.name for r in results}
        assert names == {"init", "linting"}

    def test_multiple_plugins(self, tmp_path):
        _make_plugin(tmp_path, "plugin-a", commands={"cmd-a": "A"})
        _make_plugin(tmp_path, "plugin-b", skills={"skill-b": "B"})
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            results = discover_plugin_skills()
        assert len(results) == 2
        plugins = {r.plugin_name for r in results}
        assert plugins == {"plugin-a", "plugin-b"}

    def test_no_plugins_returns_empty(self, tmp_path):
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            assert discover_plugin_skills() == []

    def test_missing_manifest_skipped(self, tmp_path):
        """Plugin dir without plugin.json is skipped."""
        plugin_dir = tmp_path / ".claude" / "plugins" / "cache" / "broken"
        plugin_dir.mkdir(parents=True)

        plugins_dir = tmp_path / ".claude" / "plugins"
        (plugins_dir / "installed_plugins.json").write_text(
            json.dumps([{"installPath": str(plugin_dir)}])
        )
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            assert discover_plugin_skills() == []

    def test_skill_name_from_frontmatter(self, tmp_path):
        """Skill name comes from frontmatter 'name' field, not directory name."""
        plugin_dir = tmp_path / ".claude" / "plugins" / "cache" / "test-plugin"
        manifest_dir = plugin_dir / ".claude-plugin"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "plugin.json").write_text(json.dumps({"name": "test-plugin"}))

        skill_dir = plugin_dir / "skills" / "dir-name"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: actual-name\ndescription: X\n---\n")

        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        (plugins_dir / "installed_plugins.json").write_text(
            json.dumps([{"installPath": str(plugin_dir)}])
        )

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            results = discover_plugin_skills()
        assert results[0].name == "actual-name"


class TestCwdValidators:
    """Tests for global_pm_cwd and scribe_cwd pydantic field validators."""

    def test_global_pm_cwd_accepts_absolute(self):
        cfg = _make_config(global_pm_cwd="/tmp/test")
        assert cfg.global_pm_cwd == "/tmp/test"

    def test_global_pm_cwd_expands_tilde(self):
        cfg = _make_config(global_pm_cwd="~/test-dir")
        assert cfg.global_pm_cwd is not None
        assert cfg.global_pm_cwd.startswith("/")
        assert "~" not in cfg.global_pm_cwd
        assert cfg.global_pm_cwd.endswith("/test-dir")

    def test_global_pm_cwd_rejects_relative(self):
        with pytest.raises(Exception, match="absolute path"):
            _make_config(global_pm_cwd="relative/path")

    def test_global_pm_cwd_accepts_none(self):
        cfg = _make_config(global_pm_cwd=None)
        assert cfg.global_pm_cwd is None

    def test_scribe_cwd_accepts_absolute(self):
        cfg = _make_config(scribe_cwd="/tmp/scribe")
        assert cfg.scribe_cwd == "/tmp/scribe"

    def test_scribe_cwd_expands_tilde(self):
        cfg = _make_config(scribe_cwd="~/scribe-dir")
        assert cfg.scribe_cwd is not None
        assert cfg.scribe_cwd.startswith("/")
        assert "~" not in cfg.scribe_cwd
        assert cfg.scribe_cwd.endswith("/scribe-dir")

    def test_scribe_cwd_rejects_relative(self):
        with pytest.raises(Exception, match="absolute path"):
            _make_config(scribe_cwd="relative/path")

    def test_scribe_cwd_accepts_none(self):
        cfg = _make_config(scribe_cwd=None)
        assert cfg.scribe_cwd is None
