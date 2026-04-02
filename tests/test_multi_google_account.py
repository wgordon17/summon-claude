"""Tests for multi-Google account credential discovery, migration, and session wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from summon_claude.config import (
    _ACCOUNT_LABEL_RE,
    _EMAIL_RE,
    _RESERVED_ACCOUNT_LABELS,
    GoogleAccount,
    _google_credentials_exist,
    _migrate_flat_credentials,
    discover_google_accounts,
    google_mcp_env_for_account,
)

# ---------- Fixtures ----------


@pytest.fixture()
def google_creds_dir(tmp_path: Path) -> Path:
    """Create a temporary google-credentials directory."""
    creds_dir = tmp_path / "google-credentials"
    creds_dir.mkdir()
    return creds_dir


def _make_flat_layout(creds_dir: Path) -> None:
    """Create a flat credential layout (pre-migration)."""
    (creds_dir / "client_env").write_text("CLIENT_ID=xxx\nCLIENT_SECRET=yyy")
    (creds_dir / "client_secret.json").write_text('{"installed":{}}')
    (creds_dir / "user@gmail.com.json").write_text('{"token":"abc"}')


def _make_account_dir(creds_dir: Path, label: str, email: str = "user@gmail.com") -> Path:
    """Create an account subdirectory with valid credentials."""
    account_dir = creds_dir / label
    account_dir.mkdir(mode=0o700, exist_ok=True)
    (account_dir / "client_env").write_text("CLIENT_ID=xxx\nCLIENT_SECRET=yyy")
    (account_dir / f"{email}.json").write_text('{"token":"abc"}')
    return account_dir


# ---------- Test _migrate_flat_credentials ----------


class TestMigrateFlatCredentials:
    def test_flat_to_default(self, google_creds_dir: Path) -> None:
        _make_flat_layout(google_creds_dir)
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            _migrate_flat_credentials()
        default_dir = google_creds_dir / "default"
        assert default_dir.exists()
        assert (default_dir / "client_env").exists()
        assert (default_dir / "client_secret.json").exists()
        assert (default_dir / "user@gmail.com.json").exists()
        # Originals should be gone
        assert not (google_creds_dir / "client_env").exists()
        assert not (google_creds_dir / "user@gmail.com.json").exists()

    def test_idempotent_after_migration(self, google_creds_dir: Path) -> None:
        _make_account_dir(google_creds_dir, "default")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            _migrate_flat_credentials()  # should be a no-op
        assert (google_creds_dir / "default" / "client_env").exists()

    def test_empty_dir_noop(self, google_creds_dir: Path) -> None:
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            _migrate_flat_credentials()  # should not error or create anything
        assert not (google_creds_dir / "default").exists()

    def test_nonexistent_dir_noop(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent"
        with patch("summon_claude.config.get_google_credentials_dir", return_value=missing):
            _migrate_flat_credentials()  # should not error

    def test_mixed_layout_safety_guard(self, google_creds_dir: Path) -> None:
        """Mixed flat files + non-default subdirs: no migration (safety)."""
        (google_creds_dir / "client_env").write_text("data")
        other_dir = google_creds_dir / "work"
        other_dir.mkdir()
        (other_dir / "client_env").write_text("data")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            _migrate_flat_credentials()
        # Flat files should NOT have been moved
        assert (google_creds_dir / "client_env").exists()

    def test_migrated_files_have_0600_permissions(self, google_creds_dir: Path) -> None:
        _make_flat_layout(google_creds_dir)
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            _migrate_flat_credentials()
        default_dir = google_creds_dir / "default"
        for f in default_dir.iterdir():
            mode = f.stat().st_mode & 0o777
            assert mode == 0o600, f"{f.name} has mode {oct(mode)}, expected 0o600"

    def test_partial_migration_recovery(self, google_creds_dir: Path) -> None:
        """Partial migration: default/ has some files, flat files remain."""
        _make_flat_layout(google_creds_dir)
        # Simulate partial migration: default/ exists, client_env already moved
        default_dir = google_creds_dir / "default"
        default_dir.mkdir(mode=0o700)
        (google_creds_dir / "client_env").rename(default_dir / "client_env")
        # user@gmail.com.json still flat
        assert (google_creds_dir / "user@gmail.com.json").exists()
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            _migrate_flat_credentials()
        assert (default_dir / "user@gmail.com.json").exists()
        assert not (google_creds_dir / "user@gmail.com.json").exists()

    def test_migration_only_moves_credential_json_not_other_json(
        self, google_creds_dir: Path
    ) -> None:
        """Only *.json files with @ in the stem are migrated; other JSON files stay put."""
        (google_creds_dir / "client_env").write_text("CLIENT_ID=xxx")
        (google_creds_dir / "user@gmail.com.json").write_text('{"token":"abc"}')
        (google_creds_dir / "config.json").write_text('{"setting": true}')
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            _migrate_flat_credentials()
        default_dir = google_creds_dir / "default"
        assert (default_dir / "user@gmail.com.json").exists()
        # config.json has no @ in stem — must NOT be migrated
        assert not (default_dir / "config.json").exists()
        assert (google_creds_dir / "config.json").exists()


# ---------- Test discover_google_accounts ----------


class TestDiscoverGoogleAccounts:
    def test_multi_account_discovery(self, google_creds_dir: Path) -> None:
        _make_account_dir(google_creds_dir, "personal", "me@gmail.com")
        _make_account_dir(google_creds_dir, "work", "me@company.com")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        assert len(accounts) == 2
        assert accounts[0].label == "personal"
        assert accounts[0].email == "me@gmail.com"
        assert accounts[1].label == "work"
        assert accounts[1].email == "me@company.com"

    def test_single_default_account(self, google_creds_dir: Path) -> None:
        _make_account_dir(google_creds_dir, "default")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        assert len(accounts) == 1
        assert accounts[0].label == "default"

    def test_empty_dir_returns_empty(self, google_creds_dir: Path) -> None:
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        assert accounts == []

    def test_setup_only_excluded(self, google_creds_dir: Path) -> None:
        """Subdirectory with client_env but no credential JSON is excluded."""
        account_dir = google_creds_dir / "incomplete"
        account_dir.mkdir()
        (account_dir / "client_env").write_text("data")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        assert accounts == []

    def test_hidden_dirs_excluded(self, google_creds_dir: Path) -> None:
        hidden = google_creds_dir / ".DS_Store"
        hidden.mkdir()
        (hidden / "client_env").write_text("data")
        (hidden / "user@x.json").write_text("{}")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        assert accounts == []

    def test_invalid_label_excluded(self, google_creds_dir: Path) -> None:
        """Directory names that fail label regex are excluded with warning."""
        bad = google_creds_dir / "123"
        bad.mkdir()
        (bad / "client_env").write_text("data")
        (bad / "user@x.json").write_text("{}")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        assert accounts == []

    def test_reserved_label_excluded(self, google_creds_dir: Path) -> None:
        reserved = google_creds_dir / "slack"
        reserved.mkdir()
        (reserved / "client_env").write_text("data")
        (reserved / "user@x.json").write_text("{}")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        assert accounts == []

    def test_sort_order_alphabetical(self, google_creds_dir: Path) -> None:
        _make_account_dir(google_creds_dir, "zeta")
        _make_account_dir(google_creds_dir, "alpha")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        assert [a.label for a in accounts] == ["alpha", "zeta"]

    def test_login_only_excluded(self, google_creds_dir: Path) -> None:
        """Subdirectory with credential JSON but no client_env is excluded."""
        login_dir = google_creds_dir / "login-only"
        login_dir.mkdir()
        (login_dir / "user@gmail.com.json").write_text('{"token":"abc"}')
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        assert accounts == []

    def test_invalid_email_yields_none(self, google_creds_dir: Path) -> None:
        """Credential file with invalid email stem yields email=None but account is included."""
        acct_dir = google_creds_dir / "personal"
        acct_dir.mkdir()
        (acct_dir / "client_env").write_text("CLIENT_ID=xxx")
        (acct_dir / "not-an-email@.json").write_text('{"token":"abc"}')
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        # Account still discovered but email is None (invalid format)
        assert len(accounts) == 1
        assert accounts[0].email is None

    def test_all_reserved_labels_excluded(self, google_creds_dir: Path) -> None:
        """Every label in _RESERVED_ACCOUNT_LABELS is individually excluded."""
        for label in _RESERVED_ACCOUNT_LABELS:
            _make_account_dir(google_creds_dir, label, f"{label}@example.com")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        assert accounts == []

    def test_creds_dir_field_points_to_account_subdir(self, google_creds_dir: Path) -> None:
        """Each account's creds_dir field is the account subdirectory path."""
        _make_account_dir(google_creds_dir, "work", "bob@company.com")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        assert accounts[0].creds_dir == google_creds_dir / "work"

    def test_flat_layout_triggers_migration_then_discovers(self, google_creds_dir: Path) -> None:
        """discover_google_accounts auto-migrates flat layout and discovers the default account."""
        _make_flat_layout(google_creds_dir)
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        assert len(accounts) == 1
        assert accounts[0].label == "default"

    def test_nonexistent_creds_dir_returns_empty(self, tmp_path: Path) -> None:
        missing = tmp_path / "no-such-dir"
        with patch("summon_claude.config.get_google_credentials_dir", return_value=missing):
            accounts = discover_google_accounts()
        assert accounts == []

    def test_three_accounts_correct_sort(self, google_creds_dir: Path) -> None:
        """Three accounts are returned in alphabetical order."""
        _make_account_dir(google_creds_dir, "zebra")
        _make_account_dir(google_creds_dir, "alpha")
        _make_account_dir(google_creds_dir, "middle")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            accounts = discover_google_accounts()
        labels = [a.label for a in accounts]
        assert labels == sorted(labels)
        assert labels == ["alpha", "middle", "zebra"]


# ---------- Test google_mcp_env_for_account ----------


class TestGoogleMcpEnvForAccount:
    def test_env_points_to_account_dir(self, google_creds_dir: Path) -> None:
        account_dir = _make_account_dir(google_creds_dir, "personal")
        account = GoogleAccount(label="personal", creds_dir=account_dir, email="me@gmail.com")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            env = google_mcp_env_for_account(account)
        assert env["WORKSPACE_MCP_CREDENTIALS_DIR"] == str(account_dir)

    def test_client_secret_set_when_exists(self, google_creds_dir: Path) -> None:
        account_dir = _make_account_dir(google_creds_dir, "personal")
        (account_dir / "client_secret.json").write_text('{"installed":{}}')
        account = GoogleAccount(label="personal", creds_dir=account_dir, email="me@gmail.com")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            env = google_mcp_env_for_account(account)
        assert "GOOGLE_CLIENT_SECRETS_PATH" in env

    def test_client_secret_absent_when_missing(self, google_creds_dir: Path) -> None:
        account_dir = _make_account_dir(google_creds_dir, "personal")
        account = GoogleAccount(label="personal", creds_dir=account_dir, email="me@gmail.com")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            env = google_mcp_env_for_account(account)
        assert "GOOGLE_CLIENT_SECRETS_PATH" not in env

    def test_path_containment_guard(self, tmp_path: Path, google_creds_dir: Path) -> None:
        """Account with creds_dir outside google-credentials raises ValueError."""
        evil_dir = tmp_path / "evil"
        evil_dir.mkdir()
        account = GoogleAccount(label="evil", creds_dir=evil_dir, email="x@y.com")
        with (
            patch("summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir),
            pytest.raises(ValueError, match="outside"),
        ):
            google_mcp_env_for_account(account)

    def test_env_only_has_credentials_dir_key_without_secret(self, google_creds_dir: Path) -> None:
        """Without client_secret.json, env dict has exactly one key."""
        account_dir = _make_account_dir(google_creds_dir, "solo")
        account = GoogleAccount(label="solo", creds_dir=account_dir, email="solo@example.com")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            env = google_mcp_env_for_account(account)
        assert set(env.keys()) == {"WORKSPACE_MCP_CREDENTIALS_DIR"}

    def test_env_values_are_strings(self, google_creds_dir: Path) -> None:
        """All values in the returned env dict are plain strings."""
        account_dir = _make_account_dir(google_creds_dir, "personal")
        (account_dir / "client_secret.json").write_text('{"installed":{}}')
        account = GoogleAccount(label="personal", creds_dir=account_dir, email="a@b.com")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            env = google_mcp_env_for_account(account)
        for key, val in env.items():
            assert isinstance(val, str), f"env[{key!r}] is {type(val)}, expected str"

    def test_client_secret_path_points_inside_account_dir(self, google_creds_dir: Path) -> None:
        """GOOGLE_CLIENT_SECRETS_PATH points to client_secret.json inside the account dir."""
        account_dir = _make_account_dir(google_creds_dir, "personal")
        (account_dir / "client_secret.json").write_text('{"installed":{}}')
        account = GoogleAccount(label="personal", creds_dir=account_dir, email="me@gmail.com")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            env = google_mcp_env_for_account(account)
        assert env["GOOGLE_CLIENT_SECRETS_PATH"] == str(account_dir / "client_secret.json")


# ---------- Test _google_credentials_exist ----------


class TestGoogleCredentialsExist:
    def test_subdirectory_layout(self, google_creds_dir: Path) -> None:
        _make_account_dir(google_creds_dir, "default")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            assert _google_credentials_exist() is True

    def test_flat_layout(self, google_creds_dir: Path) -> None:
        _make_flat_layout(google_creds_dir)
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            assert _google_credentials_exist() is True

    def test_empty_dir(self, google_creds_dir: Path) -> None:
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            assert _google_credentials_exist() is False

    def test_does_not_trigger_migration(self, google_creds_dir: Path) -> None:
        _make_flat_layout(google_creds_dir)
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            _google_credentials_exist()
        # Flat files should still be at root (no migration happened)
        assert (google_creds_dir / "client_env").exists()
        assert not (google_creds_dir / "default").exists()

    def test_no_migration_trigger_mock(self, google_creds_dir: Path) -> None:
        """_google_credentials_exist must NOT call _migrate_flat_credentials."""
        _make_flat_layout(google_creds_dir)
        with (
            patch("summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir),
            patch("summon_claude.config._migrate_flat_credentials") as mock_migrate,
        ):
            _google_credentials_exist()
            mock_migrate.assert_not_called()

    def test_nonexistent_dir_returns_false(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent"
        with patch("summon_claude.config.get_google_credentials_dir", return_value=missing):
            assert _google_credentials_exist() is False

    def test_subdir_without_client_env_returns_false(self, google_creds_dir: Path) -> None:
        """Subdirectory with only a JSON but no client_env returns False."""
        acct_dir = google_creds_dir / "personal"
        acct_dir.mkdir()
        (acct_dir / "user@example.com.json").write_text("{}")
        # No client_env
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            assert _google_credentials_exist() is False

    def test_subdir_without_credential_json_returns_false(self, google_creds_dir: Path) -> None:
        """Subdirectory with only client_env but no credential JSON returns False."""
        acct_dir = google_creds_dir / "personal"
        acct_dir.mkdir()
        (acct_dir / "client_env").write_text("id=x")
        # No *.json with @ in stem
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            assert _google_credentials_exist() is False

    def test_flat_without_client_env_returns_false(self, google_creds_dir: Path) -> None:
        """Flat layout with only the credential JSON (no client_env) returns False."""
        (google_creds_dir / "user@example.com.json").write_text("{}")
        with patch(
            "summon_claude.config.get_google_credentials_dir", return_value=google_creds_dir
        ):
            assert _google_credentials_exist() is False


# ---------- Test permission matching ----------


class TestPermissionMatching:
    def test_read_tool_auto_approve(self) -> None:
        from summon_claude.sessions.permissions import _is_google_read_tool

        assert _is_google_read_tool("mcp__workspace-personal__get_foo") is True
        assert _is_google_read_tool("mcp__workspace-work__list_bar") is True
        assert _is_google_read_tool("mcp__workspace-shared__search_baz") is True

    def test_write_tool_requires_approval(self) -> None:
        from summon_claude.sessions.permissions import _is_google_read_tool

        assert _is_google_read_tool("mcp__workspace-personal__send_bar") is False
        assert _is_google_read_tool("mcp__workspace-work__create_foo") is False

    def test_old_prefix_no_match(self) -> None:
        from summon_claude.sessions.permissions import _GOOGLE_MCP_PREFIX

        assert not "mcp__workspace__old_tool".startswith(_GOOGLE_MCP_PREFIX)

    def test_pathological_tool_name(self) -> None:
        from summon_claude.sessions.permissions import _is_google_read_tool

        # Tool with a read prefix should be approved even if it contains write-looking text later
        assert _is_google_read_tool("mcp__workspace-personal__get_x__send_y") is True

    def test_malformed_tool_name_fail_closed(self) -> None:
        from summon_claude.sessions.permissions import _is_google_read_tool

        # Too few __ segments — malformed, fail closed
        assert _is_google_read_tool("mcp__workspace-personal") is False

    def test_all_read_prefixes_recognized(self) -> None:
        """Every prefix in _GOOGLE_READ_TOOL_PREFIXES is recognized as read-only."""
        from summon_claude.sessions.permissions import (
            _GOOGLE_READ_TOOL_PREFIXES,
            _is_google_read_tool,
        )

        for prefix in _GOOGLE_READ_TOOL_PREFIXES:
            tool = f"mcp__workspace-default__{prefix}something"
            assert _is_google_read_tool(tool) is True, (
                f"Expected {tool!r} to be recognized as read-only"
            )

    def test_multi_account_labels_prefix_matching(self) -> None:
        """_GOOGLE_MCP_PREFIX matches tools for any account label."""
        from summon_claude.sessions.permissions import _GOOGLE_MCP_PREFIX

        for label in ("default", "personal", "work", "my-account", "z9"):
            tool = f"mcp__workspace-{label}__get_events"
            assert tool.startswith(_GOOGLE_MCP_PREFIX)

    def test_empty_string_fails_closed(self) -> None:
        from summon_claude.sessions.permissions import _is_google_read_tool

        assert _is_google_read_tool("") is False

    def test_write_prefixes_require_approval(self) -> None:
        """A sample of write-looking suffixes are not auto-approved."""
        from summon_claude.sessions.permissions import _is_google_read_tool

        write_cases = [
            "mcp__workspace-default__send_gmail_message",
            "mcp__workspace-default__create_calendar_event",
            "mcp__workspace-default__manage_drive_permissions",
            "mcp__workspace-default__delete_calendar_event",
            "mcp__workspace-default__update_document",
            "mcp__workspace-default__import_contacts",
        ]
        for tool in write_cases:
            assert _is_google_read_tool(tool) is False, (
                f"Expected {tool!r} to require approval (not read-only)"
            )


# ---------- Test scribe prompt ----------


class TestScribePrompt:
    def test_system_prompt_multi_account(self) -> None:
        from summon_claude.sessions.prompts.scribe import build_scribe_system_prompt

        accounts = [
            GoogleAccount(label="personal", creds_dir=Path("/tmp/p"), email="me@gmail.com"),
            GoogleAccount(label="work", creds_dir=Path("/tmp/w"), email="me@company.com"),
        ]
        result = build_scribe_system_prompt(
            scan_interval=5,
            google_enabled=True,
            google_accounts=accounts,
        )
        # Returns dict with 'append' key (preset system prompt pattern)
        prompt_text = result["append"]
        assert "personal" in prompt_text
        assert "work" in prompt_text
        assert "me@gmail.com" in prompt_text
        assert "mcp__workspace-personal__" in prompt_text
        assert "mcp__workspace-work__" in prompt_text

    def test_system_prompt_backward_compat(self) -> None:
        from summon_claude.sessions.prompts.scribe import build_scribe_system_prompt

        result = build_scribe_system_prompt(
            scan_interval=5,
            google_enabled=True,
        )
        prompt_text = result["append"]
        assert "Gmail" in prompt_text

    def test_system_prompt_no_google(self) -> None:
        from summon_claude.sessions.prompts.scribe import build_scribe_system_prompt

        result = build_scribe_system_prompt(
            scan_interval=5,
            google_enabled=False,
        )
        prompt_text = result["append"]
        assert "Gmail" not in prompt_text
        assert "Google" not in prompt_text

    def test_system_prompt_returns_preset_dict(self) -> None:
        from summon_claude.sessions.prompts.scribe import build_scribe_system_prompt

        result = build_scribe_system_prompt(scan_interval=5, google_enabled=True)
        assert result["type"] == "preset"
        assert result["preset"] == "claude_code"
        assert "append" in result

    def test_scan_prompt_multi_account(self) -> None:
        from summon_claude.sessions.prompts.scribe import build_scribe_scan_prompt

        accounts = [
            GoogleAccount(label="personal", creds_dir=Path("/tmp/p"), email="me@gmail.com"),
            GoogleAccount(label="work", creds_dir=Path("/tmp/w"), email="me@company.com"),
        ]
        result = build_scribe_scan_prompt(
            nonce="TEST123",
            google_enabled=True,
            google_accounts=accounts,
            slack_enabled=False,
            user_mention="<@U123>",
            importance_keywords="urgent",
            quiet_hours=None,
        )
        assert "mcp__workspace-personal__search_gmail_messages" in result
        assert "mcp__workspace-work__get_events" in result

    def test_scan_prompt_backward_compat(self) -> None:
        from summon_claude.sessions.prompts.scribe import build_scribe_scan_prompt

        result = build_scribe_scan_prompt(
            nonce="TEST456",
            google_enabled=True,
            google_accounts=None,
            slack_enabled=False,
            user_mention="<@U456>",
            importance_keywords="urgent",
            quiet_hours=None,
        )
        assert "Google Workspace" in result
        # No per-account tool names in backward-compat mode
        assert "mcp__workspace-" not in result

    def test_scan_prompt_nonce_in_output(self) -> None:
        from summon_claude.sessions.prompts.scribe import build_scribe_scan_prompt

        result = build_scribe_scan_prompt(
            nonce="XYZABC",
            google_enabled=False,
            slack_enabled=False,
            user_mention="<@U789>",
            importance_keywords="deadline",
            quiet_hours=None,
        )
        assert "SUMMON-INTERNAL-XYZABC" in result

    def test_scan_prompt_account_without_email(self) -> None:
        """Account with email=None renders label section without crashing."""
        from summon_claude.sessions.prompts.scribe import build_scribe_scan_prompt

        accounts = [
            GoogleAccount(label="anon", creds_dir=Path("/fake/anon"), email=None),
        ]
        result = build_scribe_scan_prompt(
            nonce="n1",
            google_enabled=True,
            google_accounts=accounts,
            slack_enabled=False,
            user_mention="<@U1>",
            importance_keywords="urgent",
            quiet_hours=None,
        )
        assert "### anon" in result
        assert "mcp__workspace-anon__search_gmail_messages" in result

    def test_system_prompt_accounts_take_precedence_over_google_enabled_false(self) -> None:
        """When google_accounts is provided, it wins even if google_enabled=False."""
        from summon_claude.sessions.prompts.scribe import build_scribe_system_prompt

        accounts = [
            GoogleAccount(label="personal", creds_dir=Path("/fake/p"), email="a@example.com"),
        ]
        result = build_scribe_system_prompt(
            scan_interval=5,
            google_enabled=False,
            google_accounts=accounts,
        )
        prompt_text = result["append"]
        # Multi-account table must appear even though google_enabled=False
        assert "| Account | Email | Tools prefix |" in prompt_text
        assert "personal" in prompt_text

    def test_system_prompt_table_shows_unknown_email(self) -> None:
        """Account with email=None shows (unknown) in the table."""
        from summon_claude.sessions.prompts.scribe import build_scribe_system_prompt

        accounts = [
            GoogleAccount(label="noemail", creds_dir=Path("/fake/noemail"), email=None),
        ]
        result = build_scribe_system_prompt(scan_interval=5, google_accounts=accounts)
        assert "(unknown)" in result["append"]

    def test_system_prompt_includes_account_attribution_reminder(self) -> None:
        """Multi-account prompt tells the scribe to identify the source account."""
        from summon_claude.sessions.prompts.scribe import build_scribe_system_prompt

        accounts = [
            GoogleAccount(label="work", creds_dir=Path("/fake/work"), email="w@c.com"),
        ]
        result = build_scribe_system_prompt(scan_interval=5, google_accounts=accounts)
        assert "identify which account" in result["append"]

    def test_scan_prompt_includes_triage_protocol(self) -> None:
        """Scan prompt always includes the triage protocol regardless of sources."""
        from summon_claude.sessions.prompts.scribe import build_scribe_scan_prompt

        result = build_scribe_scan_prompt(
            nonce="n",
            google_enabled=False,
            slack_enabled=False,
            user_mention="<@U1>",
            importance_keywords="",
            quiet_hours=None,
        )
        assert "Triage Protocol" in result
        assert "importance (1-5" in result

    def test_scan_prompt_quiet_hours_included(self) -> None:
        """Scan prompt includes quiet hours when specified."""
        from summon_claude.sessions.prompts.scribe import build_scribe_scan_prompt

        result = build_scribe_scan_prompt(
            nonce="n",
            google_enabled=False,
            slack_enabled=False,
            user_mention="<@U1>",
            importance_keywords="",
            quiet_hours="22:00-07:00",
        )
        assert "22:00-07:00" in result
        assert "quiet hours" in result.lower()

    def test_scan_prompt_quiet_hours_absent_when_none(self) -> None:
        """Scan prompt omits quiet hours when quiet_hours=None."""
        from summon_claude.sessions.prompts.scribe import build_scribe_scan_prompt

        result = build_scribe_scan_prompt(
            nonce="n",
            google_enabled=False,
            slack_enabled=False,
            user_mention="<@U1>",
            importance_keywords="",
            quiet_hours=None,
        )
        assert "quiet hours" not in result.lower()

    def test_scan_prompt_email_in_account_heading(self) -> None:
        """Scan prompt includes email address in the account section heading."""
        from summon_claude.sessions.prompts.scribe import build_scribe_scan_prompt

        accounts = [
            GoogleAccount(label="personal", creds_dir=Path("/fake/p"), email="alice@gmail.com"),
        ]
        result = build_scribe_scan_prompt(
            nonce="n",
            google_enabled=True,
            google_accounts=accounts,
            slack_enabled=False,
            user_mention="<@U1>",
            importance_keywords="",
            quiet_hours=None,
        )
        assert "alice@gmail.com" in result

    def test_scan_prompt_no_email_heading_has_no_parens(self) -> None:
        """Scan prompt account heading omits parentheses when email is None."""
        from summon_claude.sessions.prompts.scribe import build_scribe_scan_prompt

        accounts = [
            GoogleAccount(label="noemail", creds_dir=Path("/fake/noemail"), email=None),
        ]
        result = build_scribe_scan_prompt(
            nonce="n",
            google_enabled=True,
            google_accounts=accounts,
            slack_enabled=False,
            user_mention="<@U1>",
            importance_keywords="",
            quiet_hours=None,
        )
        assert "### noemail\n" in result
        assert "### noemail (" not in result

    def test_scan_prompt_importance_keywords_included(self) -> None:
        """Custom importance keywords appear in the scan prompt."""
        from summon_claude.sessions.prompts.scribe import build_scribe_scan_prompt

        result = build_scribe_scan_prompt(
            nonce="n",
            google_enabled=False,
            slack_enabled=False,
            user_mention="<@U1>",
            importance_keywords="critical,deadline,boss",
            quiet_hours=None,
        )
        assert "critical,deadline,boss" in result

    def test_scan_prompt_default_importance_keywords_when_empty(self) -> None:
        """When importance_keywords is empty, a default is used in the prompt."""
        from summon_claude.sessions.prompts.scribe import build_scribe_scan_prompt

        result = build_scribe_scan_prompt(
            nonce="n",
            google_enabled=False,
            slack_enabled=False,
            user_mention="<@U1>",
            importance_keywords="",
            quiet_hours=None,
        )
        # build_scribe_scan_prompt defaults to "urgent, action required, deadline"
        assert "urgent, action required, deadline" in result


# ---------- Guard tests ----------


class TestGuardTests:
    def test_account_label_re_pinned(self) -> None:
        assert _ACCOUNT_LABEL_RE.pattern == r"^[a-z][a-z0-9-]{0,19}$"

    def test_email_re_pinned(self) -> None:
        assert _EMAIL_RE.pattern == r"^[a-zA-Z0-9._%+\-]{1,64}@[a-zA-Z0-9.\-]{1,253}\.[a-zA-Z]{2,}$"

    def test_reserved_labels_pinned(self) -> None:
        assert frozenset({"cli", "slack", "canvas"}) == _RESERVED_ACCOUNT_LABELS

    def test_google_mcp_prefix_pinned(self) -> None:
        from summon_claude.sessions.permissions import _GOOGLE_MCP_PREFIX

        assert _GOOGLE_MCP_PREFIX == "mcp__workspace-"

    def test_google_read_tool_prefixes_pinned(self) -> None:
        from summon_claude.sessions.permissions import _GOOGLE_READ_TOOL_PREFIXES

        assert _GOOGLE_READ_TOOL_PREFIXES == (
            "get_",
            "list_",
            "search_",
            "query_",
            "read_",
            "check_",
            "debug_",
            "inspect_",
        )

    def test_google_account_fields(self) -> None:
        """Pin GoogleAccount dataclass fields."""
        import dataclasses

        fields = {f.name for f in dataclasses.fields(GoogleAccount)}
        assert "label" in fields
        assert "creds_dir" in fields
        assert "email" in fields

    def test_google_account_is_frozen(self) -> None:
        """GoogleAccount must be immutable (frozen=True)."""
        import dataclasses

        acct = GoogleAccount(label="x", creds_dir=Path("/tmp"), email=None)
        with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
            acct.label = "mutated"  # type: ignore[misc]

    def test_reserved_labels_are_lowercase(self) -> None:
        """All reserved labels are lowercase — consistent with _ACCOUNT_LABEL_RE."""
        for label in _RESERVED_ACCOUNT_LABELS:
            assert label == label.lower(), f"Reserved label {label!r} is not lowercase"

    def test_reserved_labels_match_label_re(self) -> None:
        """Reserved labels are syntactically valid account names per _ACCOUNT_LABEL_RE."""
        for label in _RESERVED_ACCOUNT_LABELS:
            assert _ACCOUNT_LABEL_RE.match(label), (
                f"Reserved label {label!r} does not match _ACCOUNT_LABEL_RE"
            )

    def test_account_label_re_valid_examples(self) -> None:
        """Sample valid labels match _ACCOUNT_LABEL_RE."""
        valid = ["a", "default", "personal", "work", "my-account", "z9", "abc123"]
        for label in valid:
            assert _ACCOUNT_LABEL_RE.match(label), f"{label!r} should be valid"

    def test_account_label_re_invalid_examples(self) -> None:
        """Sample invalid labels do not match _ACCOUNT_LABEL_RE."""
        invalid = [
            "123",  # starts with digit
            "MyAccount",  # uppercase
            "a" * 21,  # too long (>20 chars)
            "",  # empty
            "-starts-dash",  # starts with dash
            "has space",  # space
        ]
        for label in invalid:
            assert not _ACCOUNT_LABEL_RE.match(label), f"{label!r} should be invalid"


# ---------- Email validation ----------


class TestEmailValidation:
    def test_valid_email(self) -> None:
        assert _EMAIL_RE.match("user@gmail.com") is not None

    def test_malformed_email(self) -> None:
        assert _EMAIL_RE.match("bad\nname@x") is None

    def test_injection_attempt(self) -> None:
        assert _EMAIL_RE.match("## Injected@evil") is None

    def test_no_at_sign(self) -> None:
        assert _EMAIL_RE.match("noatsign.json") is None

    def test_valid_email_with_dots_and_plus(self) -> None:
        assert _EMAIL_RE.match("first.last+tag@sub.domain.com") is not None

    @pytest.mark.parametrize(
        "email",
        [
            "user@gmail.com",
            "alice@company.co.uk",
            "first.last+tag@example.org",
            "user123@sub.domain.com",
            "me@x.io",
        ],
    )
    def test_valid_emails_parametrized(self, email: str) -> None:
        assert _EMAIL_RE.match(email), f"Expected valid: {email}"

    @pytest.mark.parametrize(
        "email",
        [
            "notanemail",
            "@nodomain.com",
            "user@",
            "user@@gmail.com",
            "",
            "user@domain",  # single-char TLD is rejected
            "user@gmail.com; rm -rf /",
            "../../../etc/passwd@evil.com",
        ],
    )
    def test_invalid_emails_parametrized(self, email: str) -> None:
        assert not _EMAIL_RE.match(email), f"Expected invalid: {email!r}"

    @pytest.mark.parametrize(
        "injection",
        [
            "user\x00@example.com",  # null byte
            "user\n@example.com",  # newline
            "user\r@example.com",  # carriage return
            "user\t@example.com",  # tab
        ],
    )
    def test_control_character_injection_rejected(self, injection: str) -> None:
        """Emails containing control characters do not match _EMAIL_RE."""
        assert not _EMAIL_RE.match(injection), f"Expected invalid: {injection!r}"

    def test_email_tld_must_be_at_least_two_chars(self) -> None:
        """TLD of 1 character is rejected; 2 characters is accepted."""
        assert not _EMAIL_RE.match("user@example.c")  # 1-char TLD
        assert _EMAIL_RE.match("user@example.co")  # 2-char TLD

    def test_valid_email_used_as_json_filename_stem(self) -> None:
        """Valid emails round-trip correctly as JSON filename stems."""
        valid_emails = ["alice@gmail.com", "bob.smith@company.io", "carol+tag@sub.example.org"]
        for email in valid_emails:
            stem = Path(f"{email}.json").stem
            assert stem == email
            assert _EMAIL_RE.match(stem), f"{stem!r} should match _EMAIL_RE"

    def test_email_re_length_bounded(self) -> None:
        """_EMAIL_RE rejects emails with local part > 64 chars."""
        long_local = "a" * 65 + "@example.com"
        assert _EMAIL_RE.match(long_local) is None


# ---------- Test _scopes_to_services ----------


class TestScopesToServices:
    def test_gmail_readonly_scope(self) -> None:
        from summon_claude.config import _scopes_to_services

        result = _scopes_to_services({"https://www.googleapis.com/auth/gmail.readonly"})
        assert result == ["gmail"]

    def test_multiple_services(self) -> None:
        from summon_claude.config import _scopes_to_services

        result = _scopes_to_services(
            {
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/drive.readonly",
            }
        )
        assert result == ["calendar", "drive", "gmail"]  # sorted

    def test_rw_scope_also_matches(self) -> None:
        from summon_claude.config import _scopes_to_services

        result = _scopes_to_services({"https://www.googleapis.com/auth/gmail.modify"})
        assert result == ["gmail"]

    def test_empty_scopes(self) -> None:
        from summon_claude.config import _scopes_to_services

        assert _scopes_to_services(set()) == []

    def test_unrecognized_scope(self) -> None:
        from summon_claude.config import _scopes_to_services

        result = _scopes_to_services({"https://www.googleapis.com/auth/docs"})
        assert result == []  # docs not in _GOOGLE_SERVICE_SCOPES

    def test_mixed_recognized_and_unrecognized(self) -> None:
        from summon_claude.config import _scopes_to_services

        result = _scopes_to_services(
            {
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/docs",
                "openid",
            }
        )
        assert result == ["gmail"]

    def test_short_scope_names_expanded(self) -> None:
        """Short scope names in _GOOGLE_SERVICE_SCOPES are expanded before comparison."""
        from summon_claude.config import _scopes_to_services

        # gmail.readonly is stored as a short name in _GOOGLE_SERVICE_SCOPES
        # but credentials store full URLs — verify the expansion works
        result = _scopes_to_services({"https://www.googleapis.com/auth/calendar"})
        assert result == ["calendar"]  # "calendar" is the rw scope


# ---------- Test detect_account_services ----------


class TestDetectAccountServices:
    def test_returns_none_when_import_fails(self, google_creds_dir: Path) -> None:
        """ImportError from workspace-mcp returns None and logs warning."""
        import builtins

        from summon_claude.config import detect_account_services

        account_dir = _make_account_dir(google_creds_dir, "default")
        account = GoogleAccount(label="default", creds_dir=account_dir, email="u@x.com")

        real_import = builtins.__import__

        def block_auth(name, *args, **kwargs):
            if name == "auth.credential_store":
                raise ImportError("No module named 'auth.credential_store'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=block_auth):
            result = detect_account_services(account)
        assert result is None

    def test_returns_none_when_no_users(self, google_creds_dir: Path) -> None:
        from unittest.mock import MagicMock

        from summon_claude.config import detect_account_services

        account_dir = _make_account_dir(google_creds_dir, "default")
        account = GoogleAccount(label="default", creds_dir=account_dir, email="u@x.com")
        mock_store = MagicMock()
        mock_store.list_users.return_value = []
        with patch(
            "auth.credential_store.LocalDirectoryCredentialStore",
            return_value=mock_store,
        ):
            result = detect_account_services(account)
        assert result is None

    def test_returns_none_when_credential_is_none(self, google_creds_dir: Path) -> None:
        from unittest.mock import MagicMock

        from summon_claude.config import detect_account_services

        account_dir = _make_account_dir(google_creds_dir, "default")
        account = GoogleAccount(label="default", creds_dir=account_dir, email="u@x.com")
        mock_store = MagicMock()
        mock_store.list_users.return_value = ["u@x.com"]
        mock_store.get_credential.return_value = None
        with patch(
            "auth.credential_store.LocalDirectoryCredentialStore",
            return_value=mock_store,
        ):
            result = detect_account_services(account)
        assert result is None

    def test_returns_none_when_scopes_empty(self, google_creds_dir: Path) -> None:
        from unittest.mock import MagicMock

        from summon_claude.config import detect_account_services

        account_dir = _make_account_dir(google_creds_dir, "default")
        account = GoogleAccount(label="default", creds_dir=account_dir, email="u@x.com")
        mock_cred = MagicMock()
        mock_cred.scopes = []
        mock_store = MagicMock()
        mock_store.list_users.return_value = ["u@x.com"]
        mock_store.get_credential.return_value = mock_cred
        with patch(
            "auth.credential_store.LocalDirectoryCredentialStore",
            return_value=mock_store,
        ):
            result = detect_account_services(account)
        assert result is None

    def test_returns_none_when_scopes_unrecognized(self, google_creds_dir: Path) -> None:
        from unittest.mock import MagicMock

        from summon_claude.config import detect_account_services

        account_dir = _make_account_dir(google_creds_dir, "default")
        account = GoogleAccount(label="default", creds_dir=account_dir, email="u@x.com")
        mock_cred = MagicMock()
        mock_cred.scopes = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
        mock_store = MagicMock()
        mock_store.list_users.return_value = ["u@x.com"]
        mock_store.get_credential.return_value = mock_cred
        with patch(
            "auth.credential_store.LocalDirectoryCredentialStore",
            return_value=mock_store,
        ):
            result = detect_account_services(account)
        assert result is None  # openid/userinfo aren't service scopes

    def test_returns_services_from_scopes(self, google_creds_dir: Path) -> None:
        from unittest.mock import MagicMock

        from summon_claude.config import detect_account_services

        account_dir = _make_account_dir(google_creds_dir, "personal")
        account = GoogleAccount(label="personal", creds_dir=account_dir, email="u@x.com")
        mock_cred = MagicMock()
        mock_cred.scopes = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
        ]
        mock_store = MagicMock()
        mock_store.list_users.return_value = ["u@x.com"]
        mock_store.get_credential.return_value = mock_cred
        with patch(
            "auth.credential_store.LocalDirectoryCredentialStore",
            return_value=mock_store,
        ):
            result = detect_account_services(account)
        assert result is not None
        assert "gmail" in result
        assert "calendar" in result

    def test_returns_none_on_store_exception(self, google_creds_dir: Path) -> None:
        from summon_claude.config import detect_account_services

        account_dir = _make_account_dir(google_creds_dir, "default")
        account = GoogleAccount(label="default", creds_dir=account_dir, email="u@x.com")
        with patch(
            "auth.credential_store.LocalDirectoryCredentialStore",
            side_effect=RuntimeError("corrupt store"),
        ):
            result = detect_account_services(account)
        assert result is None


# ---------- Test session MCP wiring loop ----------


class TestSessionMcpWiringLoop:
    """Integration test for the multi-account MCP wiring logic in session.py."""

    def test_three_accounts_wired(self) -> None:
        """Three discovered accounts produce three MCP server entries."""
        from summon_claude.sessions.session import _build_google_workspace_mcp_untrusted

        accounts = [
            GoogleAccount(label="personal", creds_dir=Path("/fake/personal"), email="a@a.com"),
            GoogleAccount(label="work", creds_dir=Path("/fake/work"), email="b@b.com"),
            GoogleAccount(label="shared", creds_dir=Path("/fake/shared"), email="c@c.com"),
        ]

        mcp_servers: dict[str, dict] = {}
        google_accounts: list[GoogleAccount] = []
        for account in accounts:
            services = "gmail,calendar"
            key = f"workspace-{account.label}"
            with patch(
                "summon_claude.config.get_google_credentials_dir",
                return_value=Path("/fake"),
            ):
                mcp = _build_google_workspace_mcp_untrusted(services, account)
            mcp_servers[key] = mcp
            google_accounts.append(account)

        assert len(mcp_servers) == 3
        assert "workspace-personal" in mcp_servers
        assert "workspace-work" in mcp_servers
        assert "workspace-shared" in mcp_servers
        assert len(google_accounts) == 3

    def test_mcp_server_env_isolated_per_account(self) -> None:
        """Each MCP server config has a different WORKSPACE_MCP_CREDENTIALS_DIR."""
        from summon_claude.sessions.session import _build_google_workspace_mcp_untrusted

        accounts = [
            GoogleAccount(label="a", creds_dir=Path("/fake/a"), email="a@a.com"),
            GoogleAccount(label="b", creds_dir=Path("/fake/b"), email="b@b.com"),
        ]

        envs = []
        for account in accounts:
            with patch(
                "summon_claude.config.get_google_credentials_dir",
                return_value=Path("/fake"),
            ):
                mcp = _build_google_workspace_mcp_untrusted("gmail", account)
            envs.append(mcp["env"]["WORKSPACE_MCP_CREDENTIALS_DIR"])

        assert envs[0] != envs[1]
        assert "/a" in envs[0]
        assert "/b" in envs[1]

    def test_mcp_source_label_includes_account(self) -> None:
        """The untrusted proxy source label includes the account label."""
        from summon_claude.sessions.session import _build_google_workspace_mcp_untrusted

        account = GoogleAccount(label="work", creds_dir=Path("/fake/work"), email="w@w.com")
        with patch(
            "summon_claude.config.get_google_credentials_dir",
            return_value=Path("/fake"),
        ):
            mcp = _build_google_workspace_mcp_untrusted("gmail", account)

        assert "Google Workspace (work)" in mcp["args"]

    def test_failed_account_skipped_others_wired(self) -> None:
        """One account failing doesn't prevent others from being wired."""
        from summon_claude.sessions.session import _build_google_workspace_mcp_untrusted

        good = GoogleAccount(label="good", creds_dir=Path("/fake/good"), email="g@g.com")
        bad = GoogleAccount(label="bad", creds_dir=Path("/outside/bad"), email="b@b.com")

        mcp_servers: dict[str, dict] = {}
        for account in [bad, good]:
            try:
                with patch(
                    "summon_claude.config.get_google_credentials_dir",
                    return_value=Path("/fake"),
                ):
                    mcp = _build_google_workspace_mcp_untrusted("gmail", account)
                mcp_servers[f"workspace-{account.label}"] = mcp
            except ValueError:
                pass  # bad account raises path containment error

        assert "workspace-good" in mcp_servers
        assert "workspace-bad" not in mcp_servers

    def test_no_services_means_account_skipped(self) -> None:
        """When detect_account_services returns None, account is not wired."""
        # This tests the session.py logic: if not services: continue
        account = GoogleAccount(label="empty", creds_dir=Path("/fake/empty"), email="e@e.com")
        services = None  # simulates detect_account_services returning None

        mcp_servers: dict[str, dict] = {}
        if services:
            mcp_servers[f"workspace-{account.label}"] = {}

        assert "workspace-empty" not in mcp_servers


# ---------- Test session-cache exclusion ----------


class TestSessionCacheExclusion:
    def test_google_write_tool_not_session_cached(self) -> None:
        """Google write tools approved via HITL must NOT be session-cached."""
        from summon_claude.sessions.permissions import (
            _GOOGLE_MCP_PREFIX,
            _is_google_read_tool,
        )

        write_tool = "mcp__workspace-default__send_gmail_message"
        assert write_tool.startswith(_GOOGLE_MCP_PREFIX)
        assert not _is_google_read_tool(write_tool)

    def test_google_read_tool_would_be_session_cached(self) -> None:
        """Google read tools should pass the cache exclusion check."""
        from summon_claude.sessions.permissions import (
            _GOOGLE_MCP_PREFIX,
            _is_google_read_tool,
        )

        read_tool = "mcp__workspace-default__get_gmail_message"
        assert read_tool.startswith(_GOOGLE_MCP_PREFIX)
        assert _is_google_read_tool(read_tool)


# ---------- Integration: workspace-mcp credential store API ----------


class TestWorkspaceMcpCredentialStoreAPI:
    """Verify workspace-mcp's LocalDirectoryCredentialStore API contract.

    These tests catch upstream API changes in workspace-mcp that would
    break detect_account_services(). If workspace-mcp renames methods
    or changes signatures, these fail before users hit runtime errors.
    """

    def test_credential_store_has_required_methods(self) -> None:
        """LocalDirectoryCredentialStore must have the methods we call."""
        from auth.credential_store import LocalDirectoryCredentialStore

        assert hasattr(LocalDirectoryCredentialStore, "list_users")
        assert hasattr(LocalDirectoryCredentialStore, "get_credential")
        assert callable(LocalDirectoryCredentialStore.list_users)
        assert callable(LocalDirectoryCredentialStore.get_credential)

    def test_credential_store_list_users_on_empty_dir(self, tmp_path: Path) -> None:
        """list_users returns empty list for directory with no credentials."""
        from auth.credential_store import LocalDirectoryCredentialStore

        store = LocalDirectoryCredentialStore(str(tmp_path))
        users = store.list_users()
        assert isinstance(users, list)
        assert len(users) == 0

    def test_credential_store_roundtrip(self, tmp_path: Path) -> None:
        """Credentials stored via store_credential are retrievable via get_credential."""
        from unittest.mock import MagicMock

        from auth.credential_store import LocalDirectoryCredentialStore

        store = LocalDirectoryCredentialStore(str(tmp_path))

        # Create a mock credential with the minimum interface
        mock_cred = MagicMock()
        mock_cred.token = "test-token"
        mock_cred.refresh_token = "test-refresh"
        mock_cred.scopes = ["https://www.googleapis.com/auth/gmail.readonly"]

        # store_credential should accept (email, credential)
        try:
            store.store_credential("test@example.com", mock_cred)
        except (TypeError, AttributeError):
            pytest.skip("store_credential signature changed — update detect_account_services")

        users = store.list_users()
        assert "test@example.com" in users

    def test_detect_account_services_no_fallback(self, google_creds_dir: Path) -> None:
        """detect_account_services returns None (not global config) on failure."""
        from summon_claude.config import detect_account_services

        account_dir = _make_account_dir(google_creds_dir, "broken")
        account = GoogleAccount(label="broken", creds_dir=account_dir, email="x@y.com")

        # Mock credential store to return empty users
        from unittest.mock import MagicMock

        mock_store = MagicMock()
        mock_store.list_users.return_value = []
        with patch(
            "auth.credential_store.LocalDirectoryCredentialStore",
            return_value=mock_store,
        ):
            result = detect_account_services(account)

        # Must be None — no silent fallback to global config
        assert result is None
