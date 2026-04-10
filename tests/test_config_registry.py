"""Guard tests for ConfigOption registry — prevents drift with SummonConfig."""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import patch

import pytest

from summon_claude.config import (
    CONFIG_OPTIONS,
    SummonConfig,
    get_config_default,
    is_extra_installed,
)


@pytest.fixture(autouse=True)
def _clear_extra_installed_cache():
    """Prevent cross-test contamination from @functools.cache."""
    is_extra_installed.cache_clear()
    yield
    is_extra_installed.cache_clear()


class TestConfigOptionRegistryGuard:
    """Ensure CONFIG_OPTIONS stays in sync with SummonConfig.model_fields."""

    def test_every_config_field_has_option(self):
        """Every SummonConfig field must have a corresponding ConfigOption."""
        option_fields = {opt.field_name for opt in CONFIG_OPTIONS}
        # model_config is pydantic's SettingsConfigDict (ClassVar), not a user field
        model_fields = {f for f in SummonConfig.model_fields if f != "model_config"}
        missing = model_fields - option_fields
        assert not missing, f"SummonConfig fields without ConfigOption: {missing}"

    def test_every_option_maps_to_real_field(self):
        """Every ConfigOption.field_name must exist on SummonConfig."""
        for opt in CONFIG_OPTIONS:
            assert opt.field_name in SummonConfig.model_fields, (
                f"ConfigOption {opt.env_key!r} references non-existent field {opt.field_name!r}"
            )

    def test_env_key_format(self):
        """Every env_key must be SUMMON_ prefixed and match field_name."""
        for opt in CONFIG_OPTIONS:
            expected = f"SUMMON_{opt.field_name.upper()}"
            assert opt.env_key == expected, (
                f"ConfigOption {opt.field_name!r} env_key mismatch: {opt.env_key!r} != {expected!r}"
            )

    def test_required_options_match_required_fields(self):
        """Required ConfigOptions must match fields without defaults in SummonConfig."""
        required_option_fields = {opt.field_name for opt in CONFIG_OPTIONS if opt.required}
        required_model_fields = set()
        for name, info in SummonConfig.model_fields.items():
            if info.is_required():
                required_model_fields.add(name)
        assert required_option_fields == required_model_fields

    def test_secret_options_match_repr_false_fields(self):
        """Secret ConfigOptions should match fields with repr=False."""
        secret_fields = {opt.field_name for opt in CONFIG_OPTIONS if opt.input_type == "secret"}
        repr_false_fields = set()
        for name, info in SummonConfig.model_fields.items():
            if info.repr is False:
                repr_false_fields.add(name)
        assert secret_fields == repr_false_fields

    def test_defaults_from_registry_match_model(self):
        """get_config_default must return the same default as SummonConfig.model_fields."""
        for opt in CONFIG_OPTIONS:
            registry_default = get_config_default(opt)
            field_info = SummonConfig.model_fields[opt.field_name]
            model_default = field_info.default
            assert registry_default == model_default, (
                f"Default mismatch for {opt.field_name!r}: "
                f"registry={registry_default!r}, model={model_default!r}"
            )

    def test_input_types_are_valid(self):
        """All input_type values must be one of the known types."""
        valid_types = {"text", "secret", "choice", "flag", "int"}
        for opt in CONFIG_OPTIONS:
            assert opt.input_type in valid_types, (
                f"ConfigOption {opt.env_key!r} has invalid input_type: {opt.input_type!r}"
            )

    def test_choice_options_have_choices(self):
        """Choice-type options must have choices or choices_fn."""
        for opt in CONFIG_OPTIONS:
            if opt.input_type == "choice":
                assert opt.choices or opt.choices_fn, (
                    f"ConfigOption {opt.env_key!r} is 'choice' type but has no choices"
                )

    def test_field_validators_have_validate_fn(self):
        """Fields with @field_validator must have validate_fn on their ConfigOption.

        Without validate_fn, `config set` accepts invalid values — the pydantic
        validator only fires at SummonConfig construction time.
        Choice-type options are exempt (choices= enforces valid values).
        """
        validators = SummonConfig.__pydantic_decorators__.field_validators
        validated_fields: set[str] = set()
        for _name, dec in validators.items():
            validated_fields.update(dec.info.fields)

        for opt in CONFIG_OPTIONS:
            if opt.field_name not in validated_fields:
                continue
            # Choice-type options enforce valid values via choices=
            if opt.input_type == "choice":
                continue
            assert opt.validate_fn is not None, (
                f"ConfigOption {opt.env_key!r} has a @field_validator on SummonConfig"
                f" but no validate_fn — `config set` would accept invalid values"
            )


class TestConfigOptionVisibility:
    """Test visibility predicates for conditional options."""

    def test_scribe_options_hidden_when_disabled(self):
        """Scribe sub-options should not be visible when scribe_enabled is false."""
        cfg: dict[str, str] = {"SUMMON_SCRIBE_ENABLED": "false"}
        with (
            patch("summon_claude.config._workspace_mcp_installed", return_value=False),
            patch("summon_claude.config._google_credentials_exist", return_value=False),
            patch("summon_claude.config._slack_browser_auth_exists", return_value=False),
        ):
            for opt in CONFIG_OPTIONS:
                if (
                    opt.group.startswith("Scribe")
                    and opt.field_name != "scribe_enabled"
                    and opt.visible is not None
                ):
                    assert not opt.visible(cfg), (
                        f"ConfigOption {opt.env_key!r} should be hidden when scribe is disabled"
                    )

    def test_scribe_options_visible_when_enabled(self):
        """Scribe core sub-options should be visible when scribe_enabled is true."""
        cfg = {"SUMMON_SCRIBE_ENABLED": "true"}
        scribe_core = [
            opt
            for opt in CONFIG_OPTIONS
            if opt.group == "Scribe" and opt.field_name != "scribe_enabled" and opt.visible
        ]
        for opt in scribe_core:
            assert opt.visible is not None and opt.visible(cfg), (
                f"ConfigOption {opt.env_key!r} should be visible when scribe is enabled"
            )

    def test_scribe_slack_options_need_both_flags_and_playwright(self):
        """Scribe Slack browser/channels need both flags plus playwright installed."""
        cfg_scribe_only = {"SUMMON_SCRIBE_ENABLED": "true"}
        cfg_both = {"SUMMON_SCRIBE_ENABLED": "true", "SUMMON_SCRIBE_SLACK_ENABLED": "true"}

        browser_opt = next(o for o in CONFIG_OPTIONS if o.field_name == "scribe_slack_browser")
        assert browser_opt.visible is not None
        assert not browser_opt.visible(cfg_scribe_only)
        # With playwright importable, both flags should make it visible
        with patch("summon_claude.config.is_extra_installed", return_value=True):
            assert browser_opt.visible(cfg_both)
        # Without playwright, still hidden even with both flags
        with patch("summon_claude.config.is_extra_installed", return_value=False):
            assert not browser_opt.visible(cfg_both)

    @pytest.mark.parametrize("bool_value", ["true", "1", "yes", "on", "True", "YES", "ON"])
    def test_scribe_enabled_accepts_truthy_values(self, bool_value: str):
        """Visibility predicate should accept all truthy boolean strings."""
        cfg = {"SUMMON_SCRIBE_ENABLED": bool_value}
        scan_opt = next(o for o in CONFIG_OPTIONS if o.field_name == "scribe_scan_interval_minutes")
        assert scan_opt.visible is not None
        assert scan_opt.visible(cfg)


class TestVisibilityGracefulDegradation:
    """Visibility predicates degrade gracefully when optional extras missing."""

    def test_scribe_slack_browser_hidden_without_playwright(self):
        """scribe_slack_browser hidden when playwright not importable."""
        cfg = {"SUMMON_SCRIBE_ENABLED": "true", "SUMMON_SCRIBE_SLACK_ENABLED": "true"}
        browser_opt = next(o for o in CONFIG_OPTIONS if o.field_name == "scribe_slack_browser")
        assert browser_opt.visible is not None
        with patch("summon_claude.config.is_extra_installed", return_value=False):
            assert not browser_opt.visible(cfg)


class TestConfigShowIntegration:
    """Integration tests for config show output."""

    def test_config_show_outputs_all_visible_keys(self, tmp_path, capsys):
        """config show outputs all always-visible CONFIG_OPTIONS keys."""
        from summon_claude.cli.config import config_show

        config_file = tmp_path / "config.env"
        config_file.write_text("SUMMON_SLACK_BOT_TOKEN=xoxb-test\n")

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch("summon_claude.config._workspace_mcp_installed", return_value=False),
            patch("summon_claude.config._google_credentials_exist", return_value=False),
            patch("summon_claude.config._slack_browser_auth_exists", return_value=False),
        ):
            config_show(color=False)

        out = capsys.readouterr().out
        always_visible = [o for o in CONFIG_OPTIONS if o.visible is None]
        for opt in always_visible:
            assert opt.env_key in out, f"{opt.env_key} missing from config show output"

    def test_config_show_hides_scribe_shows_hint(self, tmp_path, capsys):
        """config show hides scribe sub-options and shows disabled hint."""
        from summon_claude.cli.config import config_show

        config_file = tmp_path / "config.env"
        config_file.write_text("")

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch("summon_claude.config._workspace_mcp_installed", return_value=False),
            patch("summon_claude.config._google_credentials_exist", return_value=False),
            patch("summon_claude.config._slack_browser_auth_exists", return_value=False),
        ):
            config_show(color=False)

        out = capsys.readouterr().out
        assert "disabled" in out
        assert "SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES" not in out

    def test_config_show_no_ansi_with_color_false(self, tmp_path, capsys):
        """config show with color=False produces no ANSI escape codes."""
        from summon_claude.cli.config import config_show

        config_file = tmp_path / "config.env"
        config_file.write_text("SUMMON_SLACK_BOT_TOKEN=xoxb-test\n")

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch("summon_claude.config._workspace_mcp_installed", return_value=False),
            patch("summon_claude.config._google_credentials_exist", return_value=False),
            patch("summon_claude.config._slack_browser_auth_exists", return_value=False),
        ):
            config_show(color=False)

        out = capsys.readouterr().out
        assert "\x1b[" not in out, "ANSI escape codes found in --no-color output"


class TestConfigShowBoolSourceLabel:
    """Test that config_show correctly labels bool fields as (default) vs (set)."""

    def test_bool_field_at_default_shows_default(self, tmp_path, capsys):
        """A flag field set to its default value shows (default)."""
        from summon_claude.cli.config import config_show

        config_file = tmp_path / "config.env"
        config_file.write_text("SUMMON_ENABLE_THINKING=true\n")

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch("summon_claude.config._workspace_mcp_installed", return_value=False),
            patch("summon_claude.config._google_credentials_exist", return_value=False),
            patch("summon_claude.config._slack_browser_auth_exists", return_value=False),
        ):
            config_show(color=False)

        out = capsys.readouterr().out
        # Find the SUMMON_ENABLE_THINKING line and check it says (default)
        for line in out.splitlines():
            if "SUMMON_ENABLE_THINKING" in line:
                assert "(default)" in line, f"Expected (default) in: {line}"
                break

    def test_bool_field_not_at_default_shows_set(self, tmp_path, capsys):
        """A flag field set to non-default value shows (set)."""
        from summon_claude.cli.config import config_show

        config_file = tmp_path / "config.env"
        config_file.write_text("SUMMON_ENABLE_THINKING=false\n")

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch("summon_claude.config._workspace_mcp_installed", return_value=False),
            patch("summon_claude.config._google_credentials_exist", return_value=False),
            patch("summon_claude.config._slack_browser_auth_exists", return_value=False),
        ):
            config_show(color=False)

        out = capsys.readouterr().out
        for line in out.splitlines():
            if "SUMMON_ENABLE_THINKING" in line:
                assert "(set)" in line, f"Expected (set) in: {line}"
                break


class TestNoUpdateCheckField:
    """Tests for the promoted no_update_check SummonConfig field."""

    def test_no_update_check_default_false(self):
        """no_update_check defaults to False."""
        default = get_config_default(
            next(o for o in CONFIG_OPTIONS if o.field_name == "no_update_check")
        )
        assert default is False

    def test_no_update_check_in_config_options(self):
        """no_update_check should exist in CONFIG_OPTIONS."""
        matches = [o for o in CONFIG_OPTIONS if o.field_name == "no_update_check"]
        assert len(matches) == 1
        assert matches[0].input_type == "flag"
        assert matches[0].group == "Behavior"


class TestConfigOptionOrdering:
    """Guard: CONFIG_OPTIONS must list all core options before any advanced options."""

    def test_core_options_precede_advanced(self):
        """Once an advanced option appears, no core options may follow."""
        seen_advanced = False
        for opt in CONFIG_OPTIONS:
            if opt.advanced:
                seen_advanced = True
            elif seen_advanced:
                # Core option after an advanced one — ordering is broken.
                # visibility-gated options (like scribe sub-options) that appear
                # before the advanced block are fine since they're core.
                pytest.fail(
                    f"Core option {opt.env_key!r} appears after advanced options. "
                    "Move it before the advanced block in CONFIG_OPTIONS."
                )


class TestChannelPrefixValidation:
    """Guard: channel_prefix must conform to Slack channel naming rules."""

    def test_valid_prefix(self):
        cfg = SummonConfig(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            slack_signing_secret="abc123",
            channel_prefix="my-team",
            _env_file=None,
        )
        assert cfg.channel_prefix == "my-team"

    def test_invalid_prefix_uppercase(self):
        with pytest.raises(ValueError, match="channel_prefix must be"):
            SummonConfig(
                slack_bot_token="xoxb-test",
                slack_app_token="xapp-test",
                slack_signing_secret="abc123",
                channel_prefix="MyTeam",
                _env_file=None,
            )

    def test_invalid_prefix_spaces(self):
        with pytest.raises(ValueError, match="channel_prefix must be"):
            SummonConfig(
                slack_bot_token="xoxb-test",
                slack_app_token="xapp-test",
                slack_signing_secret="abc123",
                channel_prefix="my team",
                _env_file=None,
            )

    def test_invalid_prefix_empty(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            SummonConfig(
                slack_bot_token="xoxb-test",
                slack_app_token="xapp-test",
                slack_signing_secret="abc123",
                channel_prefix="",
                _env_file=None,
            )


class TestSigningSecretValidation:
    """Guard: signing_secret must be a hex string."""

    def test_valid_hex(self):
        cfg = SummonConfig(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            slack_signing_secret="abcdef012345",
            _env_file=None,
        )
        assert cfg.slack_signing_secret == "abcdef012345"

    def test_invalid_non_hex(self):
        with pytest.raises(ValueError, match="hex string"):
            SummonConfig(
                slack_bot_token="xoxb-test",
                slack_app_token="xapp-test",
                slack_signing_secret="not-hex-value",
                _env_file=None,
            )

    def test_empty_passes_validator(self):
        """Empty string bypasses the @field_validator; caught by validate() instead."""
        cfg = SummonConfig(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
            slack_signing_secret="",
            _env_file=None,
        )
        assert cfg.slack_signing_secret == ""


class TestSlackScopeGuard:
    """Guard: hardcoded required scopes in config_check must match the manifest."""

    def test_required_scopes_match_manifest(self):
        import yaml

        manifest_path = Path(__file__).resolve().parent.parent / "slack-app-manifest.yaml"
        if not manifest_path.exists():
            pytest.skip("slack-app-manifest.yaml not found")

        manifest = yaml.safe_load(manifest_path.read_text())
        manifest_scopes = set(manifest["oauth_config"]["scopes"]["bot"])

        # Import the same set used in config_check
        # (hardcoded in _check_slack_scopes → inline in config_check)
        from summon_claude.cli.config import _REQUIRED_SLACK_SCOPES

        assert manifest_scopes == _REQUIRED_SLACK_SCOPES, (
            f"Scope mismatch.\n"
            f"  In manifest but not in code: {manifest_scopes - _REQUIRED_SLACK_SCOPES}\n"
            f"  In code but not in manifest: {_REQUIRED_SLACK_SCOPES - manifest_scopes}"
        )


class TestFeatureInventory:
    """Tests for _print_feature_inventory output."""

    def test_shows_no_projects(self, tmp_path, capsys):
        """Feature inventory shows 'none registered' when DB has no projects."""
        from summon_claude.cli.config import _print_feature_inventory

        db_path = tmp_path / "registry.db"
        _print_feature_inventory(db_path, {})

        out = capsys.readouterr().out
        assert "none registered" in out

    def test_db_failure_does_not_show_getting_started(self, tmp_path, capsys):
        """Feature inventory suppresses 'Getting started' nudge on DB failure."""
        from summon_claude.cli.config import _print_feature_inventory

        def _failing_run(coro, *a, **kw):
            coro.close()
            raise OSError("DB fail")

        db_path = tmp_path / "registry.db"
        with patch("summon_claude.cli.config.asyncio.run", side_effect=_failing_run):
            _print_feature_inventory(db_path, {})

        out = capsys.readouterr().out
        assert "Getting started" not in out
        assert "summon doctor" in out

    def test_shows_workflow_configured(self, tmp_path, capsys):
        """Feature inventory shows PASS when workflow defaults are set."""
        from unittest.mock import AsyncMock

        from summon_claude.cli.config import _print_feature_inventory

        with patch(
            "summon_claude.cli.config._check_features",
            new_callable=AsyncMock,
            return_value=(True, False, 1),  # has_workflow, has_hooks, project_count
        ):
            _print_feature_inventory(tmp_path / "r.db", {})

        out = capsys.readouterr().out
        assert "Workflow instructions: configured" in out

    def test_shows_hooks_configured(self, tmp_path, capsys):
        """Feature inventory shows PASS when lifecycle hooks are set."""
        from unittest.mock import AsyncMock

        from summon_claude.cli.config import _print_feature_inventory

        with patch(
            "summon_claude.cli.config._check_features",
            new_callable=AsyncMock,
            return_value=(False, True, 1),
        ):
            _print_feature_inventory(tmp_path / "r.db", {})

        out = capsys.readouterr().out
        assert "Lifecycle hooks: configured" in out

    def test_shows_scribe_google_nudge(self, tmp_path, capsys):
        """Feature inventory shows Google nudge when scribe is enabled without creds."""
        from unittest.mock import AsyncMock

        from summon_claude.cli.config import _print_feature_inventory

        with (
            patch(
                "summon_claude.cli.config._check_features",
                new_callable=AsyncMock,
                return_value=(False, False, 0),
            ),
            patch(
                "summon_claude.cli.config.get_google_credentials_dir",
                return_value=tmp_path / "nonexistent-creds",
            ),
        ):
            _print_feature_inventory(tmp_path / "r.db", {"SUMMON_SCRIBE_ENABLED": "true"})

        out = capsys.readouterr().out
        assert "Scribe enabled but Google not configured" in out


class TestIncludeGlobalToken:
    """Tests for the INCLUDE_GLOBAL_TOKEN."""

    def test_include_global_token_defined(self):
        from summon_claude.sessions.hook_types import INCLUDE_GLOBAL_TOKEN

        assert INCLUDE_GLOBAL_TOKEN == "$INCLUDE_GLOBAL"


class TestValidateFunctions:
    """Direct unit tests for _validate_* functions used by ConfigOption.validate_fn."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1", None),
            ("60", None),
            ("0", "Must be at least 1"),
            ("-1", "Must be at least 1"),
            ("abc", "Must be an integer"),
            ("", "Must be an integer"),
        ],
    )
    def test_validate_scribe_scan_interval(self, value, expected):
        from summon_claude.config import _validate_scan_interval_minutes

        assert _validate_scan_interval_minutes(value) == expected

    @pytest.mark.parametrize(
        "value,expected_none",
        [
            ("summon", True),
            ("my-team", True),
            ("a1_b2", True),
            ("1abc", True),  # starts with digit — valid per Slack rules
            ("", False),  # empty
            ("UPPER", False),  # uppercase
            ("has space", False),  # spaces
            ("-start", False),  # starts with hyphen
        ],
    )
    def test_validate_channel_prefix(self, value, expected_none):
        from summon_claude.config import _validate_channel_prefix

        result = _validate_channel_prefix(value)
        if expected_none:
            assert result is None, f"Expected valid for {value!r}, got {result!r}"
        else:
            assert result is not None, f"Expected error for {value!r}"

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("0", None),
            ("900", None),
            ("1", None),
            ("-1", "Must be >= 0 (0 = no timeout)"),
            ("1.5", "Must be an integer"),
            (" 900", None),  # int() accepts leading/trailing whitespace
            ("abc", "Must be an integer"),
            ("", "Must be an integer"),
        ],
    )
    def test_validate_permission_timeout(self, value, expected):
        from summon_claude.config import _validate_permission_timeout

        assert _validate_permission_timeout(value) == expected

    @pytest.mark.parametrize(
        "value,expected_none",
        [
            ("", True),  # empty = no quiet hours
            ("22:00-07:00", True),
            ("00:00-23:59", True),
            ("23:00", False),  # missing second part
            ("abc-def", False),
            ("25:00-07:00", False),  # invalid hour
        ],
    )
    def test_validate_quiet_hours(self, value, expected_none):
        from summon_claude.config import _validate_quiet_hours

        result = _validate_quiet_hours(value)
        if expected_none:
            assert result is None, f"Expected valid for {value!r}, got {result!r}"
        else:
            assert result is not None, f"Expected error for {value!r}"


class TestBoolTrueConstant:
    """Guard: _BOOL_TRUE must cover all values accepted by config_set normalization."""

    def test_bool_true_includes_on(self):
        from summon_claude.config import _BOOL_TRUE

        assert "on" in _BOOL_TRUE
        assert "true" in _BOOL_TRUE
        assert "1" in _BOOL_TRUE
        assert "yes" in _BOOL_TRUE


# ---------------------------------------------------------------------------
# Config check: hook bridge detection (BUG-076)
# ---------------------------------------------------------------------------


class TestHookBridgeDetection:
    """BUG-076: Hook bridge detection must iterate hook entry values, not dict keys."""

    def test_detects_installed_hooks(self, capsys):
        """config_check should detect summon hooks in PreToolUse/PostToolUse lists."""
        from summon_claude.cli.config import config_check

        settings = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "EnterWorktree", "command": "/path/to/summon-pre-worktree.sh"}
                ],
                "PostToolUse": [
                    {"matcher": "EnterWorktree", "command": "/path/to/summon-post-worktree.sh"}
                ],
            }
        }
        with (
            patch("summon_claude.cli.hooks.read_settings", return_value=settings),
            patch("summon_claude.daemon.is_daemon_running", return_value=False),
        ):
            config_check(quiet=False)
        output = capsys.readouterr().out
        assert "Hook bridge: installed" in output

    def test_no_hooks_shows_not_installed(self, capsys):
        """config_check should show 'not installed' when no summon hooks exist."""
        from summon_claude.cli.config import config_check

        settings = {"hooks": {"PreToolUse": [], "PostToolUse": []}}
        with (
            patch("summon_claude.cli.hooks.read_settings", return_value=settings),
            patch("summon_claude.daemon.is_daemon_running", return_value=False),
        ):
            config_check(quiet=False)
        output = capsys.readouterr().out
        assert "Hook bridge: not installed" in output

    def test_empty_hooks_section_shows_not_installed(self, capsys):
        """config_check handles missing hooks section gracefully."""
        from summon_claude.cli.config import config_check

        settings = {}
        with (
            patch("summon_claude.cli.hooks.read_settings", return_value=settings),
            patch("summon_claude.daemon.is_daemon_running", return_value=False),
        ):
            config_check(quiet=False)
        output = capsys.readouterr().out
        assert "Hook bridge: not installed" in output


# ---------------------------------------------------------------------------
# Config check: event health probe section
# ---------------------------------------------------------------------------


class TestConfigCheckEventHealth:
    """Tests for the event health check section in summon config check."""

    def test_config_check_health_daemon_not_running(self, capsys):
        """When daemon is not running, health check should show skip message."""
        from summon_claude.cli.config import config_check

        with patch("summon_claude.daemon.is_daemon_running", return_value=False):
            config_check(quiet=False)
        output = capsys.readouterr().out
        assert "daemon not running" in output

    def test_config_check_health_daemon_healthy(self, capsys):
        """When daemon returns healthy, should show OK."""
        from summon_claude.cli.config import config_check

        healthy_result = {"healthy": True, "reason": "healthy", "details": "OK"}

        with (
            patch("summon_claude.daemon.is_daemon_running", return_value=True),
            patch("summon_claude.cli.daemon_client.health_check", return_value=healthy_result),
            patch("asyncio.run", return_value=healthy_result),
        ):
            config_check(quiet=False)
        output = capsys.readouterr().out
        assert "Event health: OK" in output

    def test_config_check_health_daemon_unhealthy(self, capsys):
        """When daemon returns unhealthy, should show FAIL with details."""
        from summon_claude.cli.config import config_check

        unhealthy_result = {
            "healthy": False,
            "reason": "events_disabled",
            "details": "Events not delivered",
            "remediation_url": "https://api.slack.com/apps/A123/event-subscriptions",
        }

        with (
            patch("summon_claude.daemon.is_daemon_running", return_value=True),
            patch("summon_claude.cli.daemon_client.health_check", return_value=unhealthy_result),
            patch("asyncio.run", return_value=unhealthy_result),
        ):
            config_check(quiet=False)
        output = capsys.readouterr().out
        assert "Events not delivered" in output


class TestNextSteps:
    """Tests for 'Next steps' section of _print_feature_inventory."""

    def test_shows_github_when_no_token(self, tmp_path, capsys):
        """Next steps includes GitHub login when no token is stored."""
        from unittest.mock import AsyncMock

        from summon_claude.cli.config import _print_feature_inventory

        with (
            patch(
                "summon_claude.cli.config._check_features",
                new_callable=AsyncMock,
                return_value=(True, True, 1),
            ),
            patch("summon_claude.cli.hooks.read_settings", return_value={}),
            patch("summon_claude.github_auth.load_token", return_value=None),
        ):
            _print_feature_inventory(tmp_path / "r.db", {})

        out = capsys.readouterr().out
        assert "summon auth github login" in out
        assert "Next steps:" in out

    def test_omits_github_when_token_present(self, tmp_path, capsys):
        """Next steps omits GitHub login when token is already stored."""
        from unittest.mock import AsyncMock

        from summon_claude.cli.config import _print_feature_inventory

        with (
            patch(
                "summon_claude.cli.config._check_features",
                new_callable=AsyncMock,
                return_value=(True, True, 1),
            ),
            patch("summon_claude.cli.hooks.read_settings", return_value={}),
            patch("summon_claude.github_auth.load_token", return_value="gho_test"),
        ):
            _print_feature_inventory(tmp_path / "r.db", {})

        out = capsys.readouterr().out
        assert "summon auth github login" not in out

    def test_shows_project_add_when_no_projects(self, tmp_path, capsys):
        """Next steps includes project add/up when no projects registered."""
        from unittest.mock import AsyncMock

        from summon_claude.cli.config import _print_feature_inventory

        with (
            patch(
                "summon_claude.cli.config._check_features",
                new_callable=AsyncMock,
                return_value=(True, True, 0),
            ),
            patch("summon_claude.cli.hooks.read_settings", return_value={}),
            patch("summon_claude.github_auth.load_token", return_value="gho_test"),
        ):
            _print_feature_inventory(tmp_path / "r.db", {})

        out = capsys.readouterr().out
        assert "summon project add" in out
        assert "summon project up" in out

    def test_db_failure_shows_doctor_in_next_steps(self, tmp_path, capsys):
        """When DB is unavailable, next steps shows summon doctor."""
        from summon_claude.cli.config import _print_feature_inventory

        def _failing_run(coro, *a, **kw):
            coro.close()
            raise OSError("DB fail")

        with (
            patch("summon_claude.cli.config.asyncio.run", side_effect=_failing_run),
            patch("summon_claude.cli.hooks.read_settings", return_value={}),
            patch("summon_claude.github_auth.load_token", return_value=None),
        ):
            _print_feature_inventory(tmp_path / "r.db", {})

        out = capsys.readouterr().out
        assert "Next steps:" in out
        assert "summon doctor" in out


_BRIDGE_SETTINGS = types.MappingProxyType(
    {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "EnterWorktree",
                    "hooks": [{"type": "command", "command": "summon-pre-worktree.sh"}],
                }
            ]
        }
    }
)


class TestShowThinkingSummariesWarn:
    """Tests for showThinkingSummaries WARN line in _print_feature_inventory."""

    def test_warn_emitted_when_conditions_met(self, tmp_path, capsys):
        """WARN emitted when bridge installed, show_thinking=true, showThinkingSummaries absent."""
        from unittest.mock import AsyncMock

        from summon_claude.cli.config import _print_feature_inventory

        with (
            patch(
                "summon_claude.cli.config._check_features",
                new_callable=AsyncMock,
                return_value=(True, True, 1),
            ),
            patch("summon_claude.cli.hooks.read_settings", return_value=_BRIDGE_SETTINGS),
            patch("summon_claude.github_auth.load_token", return_value="gho_test"),
        ):
            _print_feature_inventory(tmp_path / "r.db", {"SUMMON_SHOW_THINKING": "true"})

        out = capsys.readouterr().out
        assert "showThinkingSummaries" in out
        assert "WARN" in out

    def test_no_warn_when_set_to_true(self, tmp_path, capsys):
        """No WARN when showThinkingSummaries is explicitly True in settings."""
        from unittest.mock import AsyncMock

        from summon_claude.cli.config import _print_feature_inventory

        settings = {**_BRIDGE_SETTINGS, "showThinkingSummaries": True}
        with (
            patch(
                "summon_claude.cli.config._check_features",
                new_callable=AsyncMock,
                return_value=(True, True, 1),
            ),
            patch("summon_claude.cli.hooks.read_settings", return_value=settings),
            patch("summon_claude.github_auth.load_token", return_value="gho_test"),
        ):
            _print_feature_inventory(tmp_path / "r.db", {"SUMMON_SHOW_THINKING": "true"})

        out = capsys.readouterr().out
        assert "showThinkingSummaries" not in out

    def test_no_warn_when_bridge_not_installed(self, tmp_path, capsys):
        """No WARN when hook bridge is not installed (even with show_thinking=true)."""
        from unittest.mock import AsyncMock

        from summon_claude.cli.config import _print_feature_inventory

        with (
            patch(
                "summon_claude.cli.config._check_features",
                new_callable=AsyncMock,
                return_value=(True, True, 1),
            ),
            patch("summon_claude.cli.hooks.read_settings", return_value={}),
            patch("summon_claude.github_auth.load_token", return_value="gho_test"),
        ):
            _print_feature_inventory(tmp_path / "r.db", {"SUMMON_SHOW_THINKING": "true"})

        out = capsys.readouterr().out
        assert "showThinkingSummaries" not in out

    def test_no_warn_when_show_thinking_disabled(self, tmp_path, capsys):
        """No WARN when show_thinking is disabled (even with bridge installed)."""
        from unittest.mock import AsyncMock

        from summon_claude.cli.config import _print_feature_inventory

        with (
            patch(
                "summon_claude.cli.config._check_features",
                new_callable=AsyncMock,
                return_value=(True, True, 1),
            ),
            patch("summon_claude.cli.hooks.read_settings", return_value=_BRIDGE_SETTINGS),
            patch("summon_claude.github_auth.load_token", return_value="gho_test"),
        ):
            _print_feature_inventory(tmp_path / "r.db", {"SUMMON_SHOW_THINKING": "false"})

        out = capsys.readouterr().out
        assert "showThinkingSummaries" not in out

    def test_no_warn_when_explicitly_false(self, tmp_path, capsys):
        """No WARN when showThinkingSummaries=False (key present, user chose false)."""
        from unittest.mock import AsyncMock

        from summon_claude.cli.config import _print_feature_inventory

        settings = {**_BRIDGE_SETTINGS, "showThinkingSummaries": False}
        with (
            patch(
                "summon_claude.cli.config._check_features",
                new_callable=AsyncMock,
                return_value=(True, True, 1),
            ),
            patch("summon_claude.cli.hooks.read_settings", return_value=settings),
            patch("summon_claude.github_auth.load_token", return_value="gho_test"),
        ):
            _print_feature_inventory(tmp_path / "r.db", {"SUMMON_SHOW_THINKING": "true"})

        out = capsys.readouterr().out
        assert "showThinkingSummaries" not in out
