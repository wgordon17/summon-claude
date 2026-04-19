"""Tests for summon_claude.config."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_test_config

from summon_claude.config import (
    ConfigOption,
    PluginSkill,
    SummonConfig,
    _parse_frontmatter,
    discover_installed_plugins,
    discover_plugin_skills,
)


def _make_config(**overrides) -> SummonConfig:
    """Create a SummonConfig with test defaults; override any field via kwargs."""
    return make_test_config(**overrides)


class TestSummonConfigDefaults:
    def test_default_model(self):
        cfg = make_test_config()
        assert cfg.default_model is None

    def test_default_model_can_be_set(self):
        cfg = make_test_config(default_model="claude-opus-4-6")
        assert cfg.default_model == "claude-opus-4-6"

    def test_default_channel_prefix(self):
        cfg = make_test_config()
        assert cfg.channel_prefix == "summon"

    def test_default_permission_debounce(self):
        cfg = make_test_config()
        assert cfg.permission_debounce_ms == 2000

    def test_default_max_inline_chars(self):
        cfg = make_test_config()
        assert cfg.max_inline_chars == 2500

    def test_default_effort(self):
        cfg = make_test_config()
        assert cfg.default_effort == "high"

    def test_default_effort_can_be_set(self):
        cfg = make_test_config(default_effort="low")
        assert cfg.default_effort == "low"

    def test_default_effort_all_valid_values(self):
        for level in ("low", "medium", "high", "max"):
            cfg = make_test_config(default_effort=level)
            assert cfg.default_effort == level

    def test_default_effort_rejects_invalid(self):
        with pytest.raises(ValueError, match="SUMMON_DEFAULT_EFFORT"):
            make_test_config(default_effort="ultra")


class TestSummonConfigValidate:
    def test_valid_config_passes(self):
        cfg = make_test_config()
        cfg.validate()  # should not raise

    def test_missing_bot_token_raises(self):
        cfg = make_test_config(slack_bot_token="")
        with pytest.raises(ValueError, match="SUMMON_SLACK_BOT_TOKEN"):
            cfg.validate()

    def test_invalid_bot_token_prefix_raises(self):
        with pytest.raises(ValueError, match="xoxb-"):
            make_test_config(slack_bot_token="xoxp-wrong-prefix")

    def test_missing_app_token_raises(self):
        cfg = make_test_config(slack_app_token="")
        with pytest.raises(ValueError, match="SUMMON_SLACK_APP_TOKEN"):
            cfg.validate()

    def test_invalid_app_token_prefix_raises(self):
        with pytest.raises(ValueError, match="xapp-"):
            make_test_config(slack_app_token="xoxb-wrong-prefix")

    def test_missing_signing_secret_raises(self):
        cfg = make_test_config(slack_signing_secret="")
        with pytest.raises(ValueError, match="SUMMON_SLACK_SIGNING_SECRET"):
            cfg.validate()

    def test_multiple_errors_reported_together(self):
        cfg = make_test_config(slack_bot_token="", slack_signing_secret="")
        with pytest.raises(ValueError) as exc_info:
            cfg.validate()
        msg = str(exc_info.value)
        assert "SUMMON_SLACK_BOT_TOKEN" in msg
        assert "SUMMON_SLACK_SIGNING_SECRET" in msg


class TestGitHubMCPConfig:
    def test_github_mcp_config_returns_none_when_no_token(self):
        cfg = make_test_config()
        with patch("summon_claude.github_auth.load_token", return_value=None):
            assert cfg.github_mcp_config() is None

    def test_github_mcp_config_returns_dict_when_token_exists(self):
        cfg = make_test_config()
        with patch("summon_claude.github_auth.load_token", return_value="gho_test123"):
            result = cfg.github_mcp_config()
        assert result is not None
        assert result["type"] == "http"
        assert result["url"] == "https://api.githubcopilot.com/mcp/"
        assert result["headers"]["Authorization"] == "Bearer gho_test123"

    def test_github_mcp_config_dict_structure(self):
        cfg = make_test_config()
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
        cfg = make_test_config(github_pat="ghp_shouldntsee")
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


# ------------------------------------------------------------------
# Worktree detection tests
# ------------------------------------------------------------------


class TestGetGitMainRepoRoot:
    """Unit tests for get_git_main_repo_root().

    Note: macOS pytest tmp_path resolves to /private/var/... which is outside
    Path.home(). We patch summon_claude.config.Path.home to return tmp_path so
    the home-directory restriction in get_git_main_repo_root passes.
    """

    def test_normal_repo_returns_cwd(self, tmp_path):
        """Normal repo: --git-common-dir returns '.git' (relative) → parent of resolved .git."""
        from summon_claude.config import get_git_main_repo_root

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = b".git\n"

        with (
            patch("subprocess.run", return_value=fake_result),
            patch("summon_claude.config.Path.home", return_value=tmp_path),
        ):
            result = get_git_main_repo_root(tmp_path)

        assert result == tmp_path.resolve()

    def test_worktree_returns_main_repo_root(self, tmp_path):
        """Worktree: --git-common-dir returns absolute path to main repo's .git directory."""
        from summon_claude.config import get_git_main_repo_root

        # Simulate main repo at tmp_path/main, worktree at tmp_path/wt
        main_git = tmp_path / "main" / ".git"
        main_git.mkdir(parents=True)
        worktree_dir = tmp_path / "wt"
        worktree_dir.mkdir()

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = str(main_git).encode() + b"\n"

        with (
            patch("subprocess.run", return_value=fake_result),
            patch("summon_claude.config.Path.home", return_value=tmp_path),
        ):
            result = get_git_main_repo_root(worktree_dir)

        assert result == (tmp_path / "main").resolve()

    def test_git_failure_returns_none(self, tmp_path):
        """Non-zero exit code (not a git repo) returns None."""
        from summon_claude.config import get_git_main_repo_root

        fake_result = MagicMock()
        fake_result.returncode = 128
        fake_result.stdout = b""

        with patch("subprocess.run", return_value=fake_result):
            result = get_git_main_repo_root(tmp_path)

        assert result is None

    def test_timeout_returns_none(self, tmp_path):
        """TimeoutExpired is caught and returns None."""
        import subprocess

        from summon_claude.config import get_git_main_repo_root

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)):
            result = get_git_main_repo_root(tmp_path)

        assert result is None

    def test_git_not_installed_returns_none(self, tmp_path):
        """FileNotFoundError (git not installed) returns None."""
        from summon_claude.config import get_git_main_repo_root

        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = get_git_main_repo_root(tmp_path)

        assert result is None

    def test_oserror_returns_none(self, tmp_path):
        """OSError from subprocess.run (e.g. permission denied) returns None."""
        from summon_claude.config import get_git_main_repo_root

        with patch("subprocess.run", side_effect=OSError("permission denied")):
            result = get_git_main_repo_root(tmp_path)

        assert result is None

    def test_unicode_decode_error_returns_none(self, tmp_path):
        """Non-UTF-8 git output (UnicodeDecodeError on stdout.decode()) returns None."""
        from summon_claude.config import get_git_main_repo_root

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = MagicMock()
        fake_result.stdout.decode.side_effect = UnicodeDecodeError(
            "utf-8", b"\xff\xfe", 0, 1, "invalid start byte"
        )

        with patch("subprocess.run", return_value=fake_result):
            result = get_git_main_repo_root(tmp_path)

        assert result is None

    def test_empty_stdout_returns_none(self, tmp_path):
        """Empty stdout (edge case) returns None."""
        from summon_claude.config import get_git_main_repo_root

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = b""

        with patch("subprocess.run", return_value=fake_result):
            result = get_git_main_repo_root(tmp_path)

        assert result is None

    def test_path_too_long_returns_none(self, tmp_path):
        """Path >= 4096 chars (PATH_MAX) is rejected as a sanity check."""
        from summon_claude.config import get_git_main_repo_root

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = (b"x" * 4096) + b"\n"

        with patch("subprocess.run", return_value=fake_result):
            result = get_git_main_repo_root(tmp_path)

        assert result is None

    def test_path_outside_home_returns_none(self, tmp_path):
        """Security gate: git returns a path that resolves outside Path.home() → None."""
        from summon_claude.config import get_git_main_repo_root

        # git reports a .git dir under /opt — outside the mocked home of /home/testuser
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = b"/opt/somewhere/.git\n"

        with (
            patch("subprocess.run", return_value=fake_result),
            patch("summon_claude.config.Path.home", return_value=tmp_path / "home" / "testuser"),
        ):
            result = get_git_main_repo_root(tmp_path)

        assert result is None

    def test_cache_stores_per_cwd(self, tmp_path):
        """functools.cache stores results per unique cwd — no cross-eviction."""
        from summon_claude.config import get_git_main_repo_root

        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()

        result_a = MagicMock(returncode=0, stdout=b".git\n")
        result_b = MagicMock(returncode=128, stdout=b"")

        with (
            patch("subprocess.run", return_value=result_a),
            patch("summon_claude.config.Path.home", return_value=tmp_path),
        ):
            assert get_git_main_repo_root(dir_a) is not None

        with (
            patch("subprocess.run", return_value=result_b),
            patch("summon_claude.config.Path.home", return_value=tmp_path),
        ):
            assert get_git_main_repo_root(dir_b) is None

        # Calling dir_a again must return cached result without re-invoking git.
        # subprocess.run still mocked to return result_b (which returns None),
        # but cache should return the original non-None result for dir_a.
        with (
            patch("subprocess.run", return_value=result_b),
            patch("summon_claude.config.Path.home", return_value=tmp_path),
        ):
            assert get_git_main_repo_root(dir_a) is not None


class TestDetectInstallModeWorktree:
    """Tests for worktree-aware install mode detection.

    All tests patch summon_claude.config.Path.home so that tmp_path directories
    satisfy the home-directory restriction in _detect_install_mode() and
    get_git_main_repo_root(). On macOS, pytest tmp_path resolves to
    /private/var/... which is outside Path.home().
    """

    def _clear_caches(self):
        from summon_claude.config import (
            _detect_install_mode,
            _find_project_root,
            get_git_main_repo_root,
        )

        _detect_install_mode.cache_clear()
        _find_project_root.cache_clear()
        get_git_main_repo_root.cache_clear()

    def test_normal_repo_venv_under_root_is_local(self, tmp_path, monkeypatch):
        """(a) Regression: normal repo with venv under project root → local mode."""
        from summon_claude.config import _detect_install_mode

        self._clear_caches()

        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'")
        venv = tmp_path / ".venv"
        venv.mkdir()

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VIRTUAL_ENV", str(venv))

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = b".git\n"

        with (
            patch("subprocess.run", return_value=fake_result),
            patch("summon_claude.config.Path.home", return_value=tmp_path.parent),
        ):
            mode, root = _detect_install_mode()

        assert mode == "local"
        assert root == tmp_path

    def test_worktree_venv_under_main_repo_is_local(self, tmp_path, monkeypatch):
        """(b) NEW: worktree with venv under main repo root → local mode."""
        from summon_claude.config import _detect_install_mode

        self._clear_caches()

        # Main repo at tmp_path/main, worktree at tmp_path/wt
        main_root = tmp_path / "main"
        main_root.mkdir()
        main_git = main_root / ".git"
        main_git.mkdir()
        venv = main_root / ".venv"
        venv.mkdir()

        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "pyproject.toml").write_text("[project]\nname = 'test'")

        monkeypatch.chdir(worktree)
        monkeypatch.setenv("VIRTUAL_ENV", str(venv))

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = str(main_git).encode() + b"\n"

        with (
            patch("subprocess.run", return_value=fake_result) as mock_run,
            patch("summon_claude.config.Path.home", return_value=tmp_path.parent),
        ):
            mode, root = _detect_install_mode()

        assert mode == "local"
        # project_root stays as the worktree root, not main repo root
        assert root == worktree
        # Verify get_git_main_repo_root was called with the worktree (project_root),
        # not the main repo root or any other path.
        assert mock_run.called
        actual_cwd = mock_run.call_args.kwargs.get("cwd")
        assert actual_cwd == worktree, (
            f"subprocess.run cwd={actual_cwd!r}, expected worktree={worktree!r}"
        )

    def test_worktree_no_virtual_env_is_global(self, tmp_path, monkeypatch):
        """(c) Worktree with no VIRTUAL_ENV → global mode."""
        from summon_claude.config import _detect_install_mode

        self._clear_caches()

        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "pyproject.toml").write_text("[project]\nname = 'test'")

        monkeypatch.chdir(worktree)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)

        with patch("summon_claude.config.Path.home", return_value=tmp_path.parent):
            mode, root = _detect_install_mode()

        assert mode == "global"
        assert root is None

    def test_non_git_directory_is_global(self, tmp_path, monkeypatch):
        """(d) Non-git directory → global mode even with VIRTUAL_ENV set."""
        from summon_claude.config import _detect_install_mode

        self._clear_caches()

        # No pyproject.toml → no project root found → global mode
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))

        mode, root = _detect_install_mode()

        assert mode == "global"
        assert root is None

    def test_git_rev_parse_failure_falls_through_to_global(self, tmp_path, monkeypatch):
        """(e) git rev-parse failure → fallback to global (no false-positive local detection)."""
        import subprocess

        from summon_claude.config import _detect_install_mode

        self._clear_caches()

        # Worktree with pyproject.toml, venv under main root (NOT under worktree root)
        main_root = tmp_path / "main"
        main_root.mkdir()
        venv = main_root / ".venv"
        venv.mkdir()

        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "pyproject.toml").write_text("[project]\nname = 'test'")

        monkeypatch.chdir(worktree)
        monkeypatch.setenv("VIRTUAL_ENV", str(venv))

        # git fails → get_git_main_repo_root returns None → no local detection
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)),
            patch("summon_claude.config.Path.home", return_value=tmp_path.parent),
        ):
            mode, root = _detect_install_mode()

        assert mode == "global"
        assert root is None

    def test_relative_virtual_env_is_ignored(self, tmp_path, monkeypatch):
        """Relative VIRTUAL_ENV path is not absolute → is_absolute() guard skips it → global.

        No subprocess.run mock needed: is_absolute() returns False before
        get_git_main_repo_root is ever called.
        """
        from summon_claude.config import _detect_install_mode

        self._clear_caches()

        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VIRTUAL_ENV", ".venv")  # relative path

        with patch("summon_claude.config.Path.home", return_value=tmp_path.parent):
            mode, root = _detect_install_mode()

        assert mode == "global"
        assert root is None

    def test_get_config_dir_returns_summon_under_project_root_in_local_mode(
        self, tmp_path, monkeypatch
    ):
        """(f) Integration: get_config_dir() → project_root/.summon/ in local mode.

        Patches get_local_root explicitly to override the session-scoped
        _isolate_data_dir fixture which pins it to None (global mode).
        """
        from summon_claude.config import _detect_install_mode, get_config_dir

        self._clear_caches()

        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'")
        venv = tmp_path / ".venv"
        venv.mkdir()

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VIRTUAL_ENV", str(venv))

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = b".git\n"

        with (
            patch("subprocess.run", return_value=fake_result),
            patch("summon_claude.config.Path.home", return_value=tmp_path.parent),
            patch("summon_claude.config.get_local_root", return_value=tmp_path),
        ):
            mode, root = _detect_install_mode()
            assert mode == "local"
            config_dir = get_config_dir()

        assert config_dir == tmp_path / ".summon"


class TestResolveHelpHint:
    """Tests for ConfigOption.resolve_help_hint()."""

    def test_string(self):
        """resolve_help_hint() returns a str help_hint as-is."""
        opt = ConfigOption(
            field_name="test",
            env_key="TEST",
            group="g",
            label="l",
            help_text="h",
            input_type="text",
            help_hint="static hint",
        )
        assert opt.resolve_help_hint() == "static hint"

    def test_callable(self):
        """resolve_help_hint() calls a callable help_hint and returns the result."""
        opt = ConfigOption(
            field_name="test",
            env_key="TEST",
            group="g",
            label="l",
            help_text="h",
            input_type="text",
            help_hint=lambda: "lazy hint",
        )
        assert opt.resolve_help_hint() == "lazy hint"

    def test_none(self):
        """resolve_help_hint() returns None when help_hint is None."""
        opt = ConfigOption(
            field_name="test",
            env_key="TEST",
            group="g",
            label="l",
            help_text="h",
            input_type="text",
        )
        assert opt.resolve_help_hint() is None


class TestModelChoices:
    def test_get_model_choices_with_cache_and_default(self):
        """Mock cached models with default → sentinel shows 'currently: ...'."""
        from summon_claude.config import get_model_choices

        with patch(
            "summon_claude.cli.model_cache.load_cached_models",
            return_value=([{"value": "model-a"}, {"value": "model-b"}], "model-a"),
        ):
            choices = get_model_choices()
        assert choices == ["default (currently: model-a)", "model-a", "model-b", "other"]

    def test_get_model_choices_with_cache_no_default(self):
        """Mock cached models without default → sentinel shows 'auto'."""
        from summon_claude.config import get_model_choices

        with patch(
            "summon_claude.cli.model_cache.load_cached_models",
            return_value=([{"value": "model-a"}, {"value": "model-b"}], None),
        ):
            choices = get_model_choices()
        assert choices == ["default (auto)", "model-a", "model-b", "other"]

    def test_get_model_choices_without_cache(self):
        """Cache returns None → fallback to _FALLBACK_MODEL_CHOICES."""
        from summon_claude.config import _FALLBACK_MODEL_CHOICES, get_model_choices

        with patch(
            "summon_claude.cli.model_cache.load_cached_models",
            return_value=None,
        ):
            choices = get_model_choices()
        assert choices == ["default (auto)", *list(_FALLBACK_MODEL_CHOICES), "other"]

    def test_get_model_choices_skips_missing_value(self):
        """Entries without 'value' key are skipped."""
        from summon_claude.config import get_model_choices

        with patch(
            "summon_claude.cli.model_cache.load_cached_models",
            return_value=([{"value": "model-a"}, {"displayName": "no-value"}], None),
        ):
            choices = get_model_choices()
        assert choices == ["default (auto)", "model-a", "other"]

    def test_get_model_choices_all_entries_lack_value(self):
        """All entries lack 'value' key → falls back to static list."""
        from summon_claude.config import _FALLBACK_MODEL_CHOICES, get_model_choices

        with patch(
            "summon_claude.cli.model_cache.load_cached_models",
            return_value=([{"displayName": "A"}, {"displayName": "B"}], None),
        ):
            choices = get_model_choices()
        assert choices == ["default (auto)", *list(_FALLBACK_MODEL_CHOICES), "other"]

    def test_warn_unrecognized_model_known(self):
        """Known model (in cached model list) → returns None, no warning."""
        from unittest.mock import patch as _patch

        from summon_claude.config import _warn_unrecognized_model

        cached = ([{"value": "claude-opus-4-6"}, {"value": "claude-sonnet-4-6"}], None)
        with (
            _patch("summon_claude.cli.model_cache.load_cached_models", return_value=cached),
            _patch("click.echo") as mock_echo,
        ):
            result = _warn_unrecognized_model("claude-opus-4-6")
        assert result is None
        mock_echo.assert_not_called()

    def test_warn_unrecognized_model_unknown(self):
        """Unknown model (not in cached model list) → returns None, warns to stderr."""
        from unittest.mock import patch as _patch

        from summon_claude.config import _warn_unrecognized_model

        cached = ([{"value": "claude-opus-4-6"}], None)
        with (
            _patch("summon_claude.cli.model_cache.load_cached_models", return_value=cached),
            _patch("click.echo") as mock_echo,
        ):
            result = _warn_unrecognized_model("totally-unknown-model-xyz")
        assert result is None
        mock_echo.assert_called_once()
        assert "totally-unknown-model-xyz" in mock_echo.call_args[0][0]
        assert mock_echo.call_args[1].get("err") is True

    def test_warn_unrecognized_model_no_cache_skips_warning(self):
        """No cached models → returns None, no warning (graceful degradation)."""
        from unittest.mock import patch as _patch

        from summon_claude.config import _warn_unrecognized_model

        with (
            _patch("summon_claude.cli.model_cache.load_cached_models", return_value=None),
            _patch("click.echo") as mock_echo,
        ):
            result = _warn_unrecognized_model("anything")
        assert result is None
        mock_echo.assert_not_called()

    def test_model_config_options_use_choice_type(self):
        """All three model ConfigOptions use input_type='choice' and choices_fn."""
        from summon_claude.config import CONFIG_OPTIONS, get_model_choices

        model_fields = {"default_model", "scribe_model", "global_pm_model"}
        matched = {opt.field_name: opt for opt in CONFIG_OPTIONS if opt.field_name in model_fields}
        missing = model_fields - set(matched.keys())
        assert not missing, f"Missing model fields: {missing}"
        for field_name, opt in matched.items():
            assert opt.input_type == "choice", (
                f"{field_name}: expected input_type='choice', got {opt.input_type!r}"
            )
            assert opt.choices_fn is get_model_choices, (
                f"{field_name}: choices_fn is not get_model_choices"
            )
