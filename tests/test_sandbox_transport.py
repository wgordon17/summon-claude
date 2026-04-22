"""Tests for MatchlockTransport field coverage, security, and flag construction."""

from __future__ import annotations

import json
from dataclasses import fields
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from summon_claude.sandbox import VmConfig
from summon_claude.sandbox.transport import (
    _ANSI_RE,
    _BUILD_COMMAND_FIELDS,
    _INTENTIONALLY_SKIPPED_FIELDS,
    _QUERY_HANDLED_FIELDS,
    MatchlockTransport,
    _strip_ansi,
)

# ---------------------------------------------------------------------------
# Guard tests — field categorization
# ---------------------------------------------------------------------------


class TestFieldCoverageGuard:
    def test_field_coverage_guard(self):
        """All ClaudeAgentOptions fields must be categorized in exactly one set."""
        all_fields = {f.name for f in fields(ClaudeAgentOptions)}
        covered = _BUILD_COMMAND_FIELDS | _QUERY_HANDLED_FIELDS | _INTENTIONALLY_SKIPPED_FIELDS
        uncovered = all_fields - covered
        extra = covered - all_fields
        assert not uncovered, f"Uncategorized ClaudeAgentOptions fields: {sorted(uncovered)}"
        assert not extra, (
            f"Fields in categorization sets but not in ClaudeAgentOptions: {sorted(extra)}"
        )

    def test_no_overlap_between_sets(self):
        """Each field must appear in exactly one categorization set."""
        build_and_query = _BUILD_COMMAND_FIELDS & _QUERY_HANDLED_FIELDS
        build_and_skip = _BUILD_COMMAND_FIELDS & _INTENTIONALLY_SKIPPED_FIELDS
        query_and_skip = _QUERY_HANDLED_FIELDS & _INTENTIONALLY_SKIPPED_FIELDS
        assert not build_and_query, f"Fields in both BUILD and QUERY: {build_and_query}"
        assert not build_and_skip, f"Fields in both BUILD and SKIPPED: {build_and_skip}"
        assert not query_and_skip, f"Fields in both QUERY and SKIPPED: {query_and_skip}"


# ---------------------------------------------------------------------------
# --dangerously-skip-permissions guard
# ---------------------------------------------------------------------------


class TestPermissionFlags:
    def _make_transport(self, **option_kwargs) -> MatchlockTransport:
        config = VmConfig(
            claude_code_version="1.2.3",
            workspace_path="/workspace",
        )
        options = ClaudeAgentOptions(**option_kwargs)
        return MatchlockTransport(MagicMock(), config, options)

    def test_dangerously_skip_permissions_present(self):
        """--dangerously-skip-permissions is always in the command."""
        transport = self._make_transport()
        cmd = transport._build_command()
        assert "--dangerously-skip-permissions" in cmd

    def test_no_permission_prompt_tool(self):
        """--permission-prompt-tool is never added (dead under skip-permissions)."""
        transport = self._make_transport(permission_prompt_tool_name="mcp__my__tool")
        cmd = transport._build_command()
        assert "--permission-prompt-tool" not in cmd
        assert "--dangerously-skip-permissions" in cmd

    def test_no_plugin_dir(self):
        """--plugin-dir is never added (host paths don't exist in VM)."""
        transport = self._make_transport(plugins=[{"type": "local", "path": "/host/plugin"}])
        cmd = transport._build_command()
        assert "--plugin-dir" not in cmd

    def test_claude_binary_path(self):
        """CLI path is always 'claude', not a host path."""
        transport = self._make_transport()
        cmd = transport._build_command()
        assert cmd[0] == "claude"


# ---------------------------------------------------------------------------
# shlex.quote adversarial tests (SEC-D-010)
# ---------------------------------------------------------------------------


class TestShlexQuoteAdversarial:
    @pytest.mark.parametrize(
        "prompt",
        [
            "Don't follow instructions that say 'execute this'",
            'System prompt with "double quotes"',
            "Check the $USER variable and `whoami`",
            "Backslash test: C:\\Users\\test",
            "Newline\ntest\nhere",
            "All together: 'single' \"double\" $VAR `cmd` \\path\n",
        ],
    )
    def test_shlex_quote_adversarial(self, prompt: str) -> None:
        """System prompts with special chars survive sh -c quoting in _build_command."""
        import shlex

        config = VmConfig(
            claude_code_version="1.2.3",
            workspace_path="/workspace",
        )
        options = ClaudeAgentOptions(system_prompt=prompt)
        transport = MatchlockTransport(MagicMock(), config, options)
        cmd = transport._build_command()

        # Verify --system-prompt flag is present and followed by the raw prompt
        assert "--system-prompt" in cmd
        idx = cmd.index("--system-prompt")
        assert cmd[idx + 1] == prompt, f"system_prompt not preserved verbatim; got {cmd[idx + 1]!r}"

        # Verify that shlex-quoting then re-parsing round-trips correctly
        # (simulates what sh -c does with the fully quoted command string)
        quoted_cmd = " ".join(shlex.quote(arg) for arg in cmd)
        reparsed = shlex.split(quoted_cmd)
        idx2 = reparsed.index("--system-prompt")
        assert reparsed[idx2 + 1] == prompt, (
            f"system_prompt corrupted after shlex round-trip; got {reparsed[idx2 + 1]!r}"
        )


# ---------------------------------------------------------------------------
# ANSI stripping (SEC-D-014)
# ---------------------------------------------------------------------------


class TestAnsiStripping:
    def test_ansi_strip_before_json_parse(self):
        """_strip_ansi preserves JSON-encoded backslash sequences.

        The JSON string {"text": "line1\\r\\nline2"} has the value "line1\r\nline2"
        (single backslash-r-backslash-n, i.e. the two-char sequences \\r and \\n).
        After JSON parsing that becomes a string containing a literal carriage-return
        and newline.  _strip_ansi should strip bare CR bytes but NOT the backslash
        sequences inside the JSON encoding.

        We use raw double-escaped strings to construct a JSON line where the value
        contains JSON-escaped backslashes: \\\\r\\\\n → JSON value \\r\\n → string "\\r\\n".
        """
        # Build the JSON line explicitly so the escaping is unambiguous:
        # json_line is the string: {"text": "line1\\r\\nline2"}
        # which after JSON parsing gives the Python string "line1\\r\\nline2"
        json_line = json.dumps({"text": "line1\\r\\nline2"})
        # Simulate PTY: append a carriage-return + newline at the end (bare PTY bytes)
        raw = json_line + "\r\n"
        stripped = _strip_ansi(raw)
        # The trailing \r should be stripped; the JSON body is unchanged
        assert "\r" not in stripped
        parsed = json.loads(stripped.strip())
        # The parsed value should be the Python string "line1\r\nline2" (backslash-r-backslash-n)
        assert parsed["text"] == "line1\\r\\nline2"

    def test_ansi_escape_codes_stripped(self):
        """ANSI escape sequences are removed from PTY output."""
        colored = "\x1b[32mGreen text\x1b[0m"
        assert _strip_ansi(colored) == "Green text"

    def test_carriage_return_stripped(self):
        """Bare carriage returns from PTY are stripped."""
        with_cr = "Hello\r World\r\n"
        result = _strip_ansi(with_cr)
        assert "\r" not in result

    def test_osc_sequence_stripped(self):
        """OSC escape sequences (window title etc.) are stripped."""
        osc = "\x1b]0;window title\x07text"
        assert _strip_ansi(osc) == "text"

    def test_json_content_preserved(self):
        """Valid JSON lines pass through _strip_ansi intact."""
        json_line = '{"type": "assistant", "content": "hello world"}'
        assert _strip_ansi(json_line) == json_line


# ---------------------------------------------------------------------------
# create_vm flag coverage
# ---------------------------------------------------------------------------


class TestCreateVmFlags:
    @pytest.mark.anyio
    async def test_create_vm_flag_coverage(self):
        """create_vm produces --allow-host, --secret, and -v flags from VmConfig."""
        from summon_claude.sandbox.matchlock import MatchlockBackend

        config = VmConfig(
            claude_code_version="1.2.3",
            workspace_path="/host/workspace",
            memory_volume_path="/host/memory",
            network_allowlist=("api.anthropic.com", "pypi.org"),
            secrets=(("ANTHROPIC_API_KEY", "api.anthropic.com"),),
            cpu=2,
            memory="2G",
        )

        captured_cmds: list[list[str]] = []

        async def fake_run_process(cmd, **kwargs):
            captured_cmds.append(list(cmd))
            result = MagicMock()
            if "--version" in cmd:
                # _check_version call
                result.stdout = b"matchlock version 0.2.9"
                result.stderr = b""
                result.returncode = 0
            elif "run" in cmd:
                # create_vm: matchlock run ... — return a fake VM ID
                result.stdout = b"vm-deadbeef"
                result.stderr = b""
                result.returncode = 0
            else:
                # exec calls (pre_install, useradd)
                result.stdout = b""
                result.stderr = b""
                result.returncode = 0
            return result

        with (
            patch("shutil.which", return_value="/usr/local/bin/matchlock"),
            patch("anyio.run_process", side_effect=fake_run_process),
        ):
            backend = MatchlockBackend()
            vm_id = await backend.create_vm(config)

        assert vm_id == "vm-deadbeef"

        # Find the "matchlock run ..." command (not --version or exec)
        run_cmd = next(cmd for cmd in captured_cmds if "run" in cmd)
        assert "--allow-host" in run_cmd
        assert "api.anthropic.com" in run_cmd
        assert "pypi.org" in run_cmd
        assert "--secret" in run_cmd
        assert "ANTHROPIC_API_KEY@api.anthropic.com" in run_cmd
        # Workspace volume mount
        assert "-v" in run_cmd
        assert "/host/workspace:/workspace:ro" in run_cmd
        # Memory volume mount
        assert "/host/memory:/workspace/.bug-hunter-memory/:rw" in run_cmd
        assert "--cpu" in run_cmd
        assert "2" in run_cmd
        assert "--memory" in run_cmd
        assert "2G" in run_cmd


# ---------------------------------------------------------------------------
# Factory reconnection test
# ---------------------------------------------------------------------------


class TestCreateMatchlockTransportFactory:
    @pytest.mark.anyio
    async def test_factory_reconnects_on_dead_vm(self):
        """create_matchlock_transport creates a fresh VM when existing one is not running."""
        from summon_claude.sandbox.transport import create_matchlock_transport

        config = VmConfig(
            claude_code_version="1.2.3",
            workspace_path="/workspace",
        )
        options = ClaudeAgentOptions()
        backend = MagicMock()
        backend.is_running = AsyncMock(return_value=False)

        transport = await create_matchlock_transport(
            backend, config, options, vm_handle="vm-deadbeef"
        )

        backend.is_running.assert_called_once_with("vm-deadbeef")
        # vm_handle should have been cleared so connect() creates a new VM
        assert transport._vm_handle is None

    @pytest.mark.anyio
    async def test_factory_reuses_running_vm(self):
        """create_matchlock_transport reuses vm_handle when VM is still running."""
        from summon_claude.sandbox.transport import create_matchlock_transport

        config = VmConfig(
            claude_code_version="1.2.3",
            workspace_path="/workspace",
        )
        options = ClaudeAgentOptions()
        backend = MagicMock()
        backend.is_running = AsyncMock(return_value=True)

        transport = await create_matchlock_transport(
            backend, config, options, vm_handle="vm-deadbeef"
        )

        backend.is_running.assert_called_once_with("vm-deadbeef")
        assert transport._vm_handle == "vm-deadbeef"

    @pytest.mark.anyio
    async def test_factory_no_handle_skips_check(self):
        """create_matchlock_transport without vm_handle skips is_running check."""
        from summon_claude.sandbox.transport import create_matchlock_transport

        config = VmConfig(
            claude_code_version="1.2.3",
            workspace_path="/workspace",
        )
        options = ClaudeAgentOptions()
        backend = MagicMock()
        backend.is_running = AsyncMock()

        transport = await create_matchlock_transport(backend, config, options)

        backend.is_running.assert_not_called()
        assert transport._vm_handle is None


# ---------------------------------------------------------------------------
# read_messages buffer overflow guard
# ---------------------------------------------------------------------------


class TestReadMessagesBufferGuard:
    """Tests for _truncate_buffer_if_needed — the production truncation method."""

    def _make_transport(self) -> MatchlockTransport:
        config = VmConfig(claude_code_version="1.2.3", workspace_path="/workspace")
        options = ClaudeAgentOptions()
        backend = MagicMock()
        transport = MatchlockTransport(backend, config, options)
        transport._ready = True
        return transport

    def test_buffer_truncated_keeps_partial_line(self):
        from summon_claude.sandbox.transport import _MAX_BUFFER_BYTES

        transport = self._make_transport()
        junk = "x" * _MAX_BUFFER_BYTES
        partial = '{"type": "partial"}'
        transport._buffer = junk + "\n" + partial

        transport._truncate_buffer_if_needed()

        assert transport._buffer == partial

    def test_buffer_truncated_clears_without_newline(self):
        from summon_claude.sandbox.transport import _MAX_BUFFER_BYTES

        transport = self._make_transport()
        transport._buffer = "x" * (_MAX_BUFFER_BYTES + 1)

        transport._truncate_buffer_if_needed()

        assert transport._buffer == ""

    def test_buffer_within_limit_untouched(self):
        from summon_claude.sandbox.transport import _MAX_BUFFER_BYTES

        transport = self._make_transport()
        content = "x" * (_MAX_BUFFER_BYTES - 1)
        transport._buffer = content

        transport._truncate_buffer_if_needed()

        assert transport._buffer == content


# ---------------------------------------------------------------------------
# _check_version failure paths
# ---------------------------------------------------------------------------


class TestCheckVersion:
    def _make_backend(self):
        from summon_claude.sandbox.matchlock import MatchlockBackend

        with patch("shutil.which", return_value="/usr/local/bin/matchlock"):
            return MatchlockBackend()

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "version_output",
        [b"matchlock version 0.2.8", b"matchlock version 0.1.0"],
    )
    async def test_check_version_too_old(self, version_output: bytes) -> None:
        from summon_claude.sandbox import SandboxNotAvailableError

        backend = self._make_backend()

        async def fake_run_process(cmd, **kwargs):
            result = MagicMock()
            result.stdout = version_output
            result.stderr = b""
            result.returncode = 0
            return result

        with (
            patch("anyio.run_process", side_effect=fake_run_process),
            pytest.raises(SandboxNotAvailableError, match="upgrade"),
        ):
            await backend._check_version()

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "version_output",
        [b"matchlock garbage-output", b"matchlock version not-a-version", b""],
    )
    async def test_check_version_unparseable(self, version_output: bytes) -> None:
        from summon_claude.sandbox import SandboxNotAvailableError

        backend = self._make_backend()

        async def fake_run_process(cmd, **kwargs):
            result = MagicMock()
            result.stdout = version_output
            result.stderr = b""
            result.returncode = 0
            return result

        with (
            patch("anyio.run_process", side_effect=fake_run_process),
            pytest.raises(SandboxNotAvailableError, match="Cannot parse"),
        ):
            await backend._check_version()

    @pytest.mark.anyio
    async def test_check_version_cli_failure(self) -> None:
        from summon_claude.sandbox import SandboxNotAvailableError

        backend = self._make_backend()

        async def fake_run_process(cmd, **kwargs):
            result = MagicMock()
            result.stdout = b""
            result.stderr = b"matchlock: command failed"
            result.returncode = 1
            return result

        with (
            patch("anyio.run_process", side_effect=fake_run_process),
            pytest.raises(SandboxNotAvailableError, match="Failed to get"),
        ):
            await backend._check_version()


# ---------------------------------------------------------------------------
# create_vm cleanup path
# ---------------------------------------------------------------------------


class TestCreateVmCleanup:
    @pytest.mark.anyio
    async def test_useradd_failure_calls_destroy_vm(self) -> None:
        from summon_claude.sandbox.matchlock import MatchlockBackend

        config = VmConfig(claude_code_version="1.2.3", workspace_path="/host/workspace")
        call_log: list[list[str]] = []

        async def fake_run_process(cmd, **kwargs):
            call_log.append(list(cmd))
            result = MagicMock()
            result.stderr = b""
            result.returncode = 0
            if "--version" in cmd:
                result.stdout = b"matchlock version 0.2.9"
            elif "run" in cmd:
                result.stdout = b"vm-aabbccdd"
            elif "useradd" in cmd:
                result.stdout = b""
                result.stderr = b"useradd: user already exists"
                result.returncode = 9
            else:
                result.stdout = b""
            return result

        with (
            patch("shutil.which", return_value="/usr/local/bin/matchlock"),
            patch("anyio.run_process", side_effect=fake_run_process),
        ):
            backend = MatchlockBackend()
            with pytest.raises(RuntimeError, match="useradd failed"):
                await backend.create_vm(config)

        kill_cmds = [cmd for cmd in call_log if "kill" in cmd]
        assert kill_cmds, "destroy_vm was not called (no 'kill' command)"
        assert any("vm-aabbccdd" in cmd for cmd in kill_cmds)


# ---------------------------------------------------------------------------
# create_bug_hunter_vm_config factory
# ---------------------------------------------------------------------------


class TestCreateBugHunterVmConfig:
    def _base_env(self) -> dict[str, str]:
        return {"ANTHROPIC_API_KEY": "sk-test"}

    def test_happy_path_returns_vmconfig(self, monkeypatch):
        from summon_claude.sandbox import create_bug_hunter_vm_config

        for k, v in self._base_env().items():
            monkeypatch.setenv(k, v)
        config = create_bug_hunter_vm_config(
            workspace_path="/host/repo",
            claude_code_version="latest",
        )
        assert isinstance(config, VmConfig)
        assert config.workspace_path == "/host/repo"

    def test_default_allowlist_always_included(self, monkeypatch):
        from summon_claude.sandbox import _DEFAULT_NETWORK_ALLOWLIST, create_bug_hunter_vm_config

        for k, v in self._base_env().items():
            monkeypatch.setenv(k, v)
        config = create_bug_hunter_vm_config(
            workspace_path="/host/repo",
            claude_code_version="latest",
        )
        for domain in _DEFAULT_NETWORK_ALLOWLIST:
            assert domain in config.network_allowlist

    def test_extra_allowlist_is_additive(self, monkeypatch):
        from summon_claude.sandbox import _DEFAULT_NETWORK_ALLOWLIST, create_bug_hunter_vm_config

        for k, v in self._base_env().items():
            monkeypatch.setenv(k, v)
        config = create_bug_hunter_vm_config(
            workspace_path="/host/repo",
            claude_code_version="latest",
            network_allowlist=("example.com",),
        )
        assert "example.com" in config.network_allowlist
        for domain in _DEFAULT_NETWORK_ALLOWLIST:
            assert domain in config.network_allowlist

    def test_invalid_domain_raises(self, monkeypatch):
        from summon_claude.sandbox import create_bug_hunter_vm_config

        for k, v in self._base_env().items():
            monkeypatch.setenv(k, v)
        with pytest.raises(ValueError, match="Invalid domain"):
            create_bug_hunter_vm_config(
                workspace_path="/host/repo",
                claude_code_version="latest",
                network_allowlist=("not a domain!!!",),
            )

    def test_missing_anthropic_api_key_raises(self, monkeypatch):
        from summon_claude.sandbox import create_bug_hunter_vm_config

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            create_bug_hunter_vm_config(workspace_path="/host/repo", claude_code_version="latest")

    def test_missing_custom_secret_raises(self, monkeypatch):
        from summon_claude.sandbox import create_bug_hunter_vm_config

        for k, v in self._base_env().items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("MY_TOKEN", raising=False)
        with pytest.raises(ValueError, match="MY_TOKEN"):
            create_bug_hunter_vm_config(
                workspace_path="/host/repo",
                claude_code_version="latest",
                secrets={"MY_TOKEN": "api.example.com"},
            )

    def test_vertex_adc_warning_logged(self, monkeypatch, caplog):
        import logging

        from summon_claude.sandbox import create_bug_hunter_vm_config

        for k, v in self._base_env().items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
        monkeypatch.setenv(
            "GOOGLE_APPLICATION_CREDENTIALS",
            "/home/user/.config/gcloud/application_default_credentials.json",
        )
        with caplog.at_level(logging.WARNING, logger="summon_claude.sandbox"):
            create_bug_hunter_vm_config(
                workspace_path="/host/repo",
                claude_code_version="latest",
            )
        assert any("Vertex" in r.message or "user credentials" in r.message for r in caplog.records)

    def test_pre_install_latest_tag(self, monkeypatch):
        from summon_claude.sandbox import create_bug_hunter_vm_config

        for k, v in self._base_env().items():
            monkeypatch.setenv(k, v)
        config = create_bug_hunter_vm_config(
            workspace_path="/host/repo",
            claude_code_version="latest",
        )
        assert any("@latest" in step for step in config.pre_install)


# ---------------------------------------------------------------------------
# VmConfig.__post_init__ semver validation
# ---------------------------------------------------------------------------


class TestVmConfigSemverValidation:
    @pytest.mark.parametrize("version", ["1.2.3", "0.0.1", "100.200.300", "latest"])
    def test_valid_versions_accepted(self, version: str) -> None:
        config = VmConfig(claude_code_version=version, workspace_path="/workspace")
        assert config.claude_code_version == version

    @pytest.mark.parametrize(
        "version",
        [
            "1.2",
            "1.2.3-alpha",
            "1.2.3+build",
            "latest ",
            " latest",
            "",
            "foo",
            "foo; rm -rf /",
            "v1.2.3",
        ],
    )
    def test_invalid_versions_rejected(self, version: str) -> None:
        with pytest.raises(ValueError, match="must be 'latest' or"):
            VmConfig(claude_code_version=version, workspace_path="/workspace")


# ---------------------------------------------------------------------------
# validate_vm_id injection guard
# ---------------------------------------------------------------------------


class TestValidateVmId:
    @pytest.mark.parametrize(
        "vm_id",
        ["vm-00000000", "vm-deadbeef", "vm-12345678", "vm-abcdef01"],
    )
    def test_valid_ids_pass(self, vm_id: str) -> None:
        from summon_claude.sandbox import validate_vm_id

        assert validate_vm_id(vm_id) == vm_id

    @pytest.mark.parametrize(
        "vm_id",
        [
            "",
            "vm-",
            "vm-1234567",
            "vm-123456789",
            "vm-DEADBEEF",
            "vm-GGGGGGGG",
            "../evil",
            "vm-1234; rm -rf /",
            "vm 12345678",
        ],
    )
    def test_invalid_ids_rejected(self, vm_id: str) -> None:
        from summon_claude.sandbox import validate_vm_id

        with pytest.raises(ValueError, match="Invalid VM ID"):
            validate_vm_id(vm_id)


# ---------------------------------------------------------------------------
# validate_bug_hunter_secrets — direct edge case coverage
# ---------------------------------------------------------------------------


class TestValidateBugHunterSecrets:
    def test_valid_single_entry(self) -> None:
        from summon_claude.sandbox import validate_bug_hunter_secrets

        result = validate_bug_hunter_secrets({"MY_TOKEN": "api.example.com"})
        assert result == (("MY_TOKEN", "api.example.com"),)

    def test_valid_domain_with_port(self) -> None:
        from summon_claude.sandbox import validate_bug_hunter_secrets

        result = validate_bug_hunter_secrets({"MY_TOKEN": "api.example.com:8080"})
        assert result == (("MY_TOKEN", "api.example.com:8080"),)

    def test_empty_dict_returns_empty_tuple(self) -> None:
        from summon_claude.sandbox import validate_bug_hunter_secrets

        assert validate_bug_hunter_secrets({}) == ()

    @pytest.mark.parametrize(
        "key",
        ["A", "a", "my_token", "MY TOKEN", "", "1TOKEN", "_TOKEN"],
    )
    def test_invalid_env_var_name_raises(self, key: str) -> None:
        from summon_claude.sandbox import validate_bug_hunter_secrets

        with pytest.raises(ValueError, match="Invalid env var name"):
            validate_bug_hunter_secrets({key: "api.example.com"})

    @pytest.mark.parametrize(
        "domain",
        [
            "",
            "notadomain",
            "not a domain",
            "http://example.com",
            "example.com/path",
            "api.example.com; rm -rf /",
        ],
    )
    def test_invalid_domain_raises(self, domain: str) -> None:
        from summon_claude.sandbox import validate_bug_hunter_secrets

        with pytest.raises(ValueError, match="Invalid domain"):
            validate_bug_hunter_secrets({"MY_TOKEN": domain})


# ---------------------------------------------------------------------------
# MatchlockBackend.is_running() output parsing
# ---------------------------------------------------------------------------


class TestIsRunning:
    def _make_backend(self):
        from summon_claude.sandbox.matchlock import MatchlockBackend

        with patch("shutil.which", return_value="/usr/local/bin/matchlock"):
            return MatchlockBackend()

    def _make_list_output(self, *vm_ids: str) -> bytes:
        lines = [f"{vm_id}\trunning\tnode:24-slim\t2026-01-01T00:00:00Z\t12345" for vm_id in vm_ids]
        return "\n".join(lines).encode()

    @pytest.mark.anyio
    async def test_vm_present_returns_true(self) -> None:
        backend = self._make_backend()

        async def fake_run_process(cmd, **kwargs):
            result = MagicMock()
            result.stdout = self._make_list_output("vm-aabbccdd", "vm-11223344")
            result.stderr = b""
            result.returncode = 0
            return result

        with patch("anyio.run_process", side_effect=fake_run_process):
            assert await backend.is_running("vm-aabbccdd") is True

    @pytest.mark.anyio
    async def test_vm_absent_returns_false(self) -> None:
        backend = self._make_backend()

        async def fake_run_process(cmd, **kwargs):
            result = MagicMock()
            result.stdout = self._make_list_output("vm-11223344")
            result.stderr = b""
            result.returncode = 0
            return result

        with patch("anyio.run_process", side_effect=fake_run_process):
            assert await backend.is_running("vm-aabbccdd") is False

    @pytest.mark.anyio
    async def test_empty_output_returns_false(self) -> None:
        backend = self._make_backend()

        async def fake_run_process(cmd, **kwargs):
            result = MagicMock()
            result.stdout = b""
            result.stderr = b""
            result.returncode = 0
            return result

        with patch("anyio.run_process", side_effect=fake_run_process):
            assert await backend.is_running("vm-aabbccdd") is False

    @pytest.mark.anyio
    async def test_nonzero_returncode_returns_false(self) -> None:
        backend = self._make_backend()

        async def fake_run_process(cmd, **kwargs):
            result = MagicMock()
            result.stdout = self._make_list_output("vm-aabbccdd")
            result.stderr = b"matchlock: permission denied"
            result.returncode = 1
            return result

        with patch("anyio.run_process", side_effect=fake_run_process):
            assert await backend.is_running("vm-aabbccdd") is False


# ---------------------------------------------------------------------------
# MatchlockTransport.connect() — PTY allocation and command construction
# ---------------------------------------------------------------------------


class TestMatchlockTransportConnect:
    def _make_transport(self, vm_handle=None, **option_kwargs) -> MatchlockTransport:
        config = VmConfig(
            claude_code_version="1.2.3",
            workspace_path="/workspace",
        )
        options = ClaudeAgentOptions(**option_kwargs)
        backend = MagicMock()
        backend.create_vm = AsyncMock(return_value="vm-aabbccdd")
        backend.destroy_vm = AsyncMock()
        return MatchlockTransport(
            backend, config, options, vm_handle=vm_handle, cli_path="matchlock"
        )

    @pytest.mark.anyio
    async def test_connect_creates_vm_when_no_handle(self) -> None:
        transport = self._make_transport()
        fake_process = MagicMock()
        fake_process.stdin = MagicMock()
        fake_process.returncode = None

        with (
            patch("pty.openpty", return_value=(10, 11)),
            patch("fcntl.ioctl"),
            patch("os.close"),
            patch("anyio.open_process", new_callable=AsyncMock, return_value=fake_process),
        ):
            await transport.connect()

        transport._backend.create_vm.assert_called_once()
        assert transport._vm_handle == "vm-aabbccdd"
        assert transport._ready is True

    @pytest.mark.anyio
    async def test_connect_skips_create_when_handle_provided(self) -> None:
        transport = self._make_transport(vm_handle="vm-deadbeef")
        fake_process = MagicMock()
        fake_process.stdin = MagicMock()
        fake_process.returncode = None

        with (
            patch("pty.openpty", return_value=(10, 11)),
            patch("fcntl.ioctl"),
            patch("os.close"),
            patch("anyio.open_process", new_callable=AsyncMock, return_value=fake_process),
        ):
            await transport.connect()

        transport._backend.create_vm.assert_not_called()
        assert transport._vm_handle == "vm-deadbeef"

    @pytest.mark.anyio
    async def test_connect_command_contains_vm_handle(self) -> None:
        transport = self._make_transport(vm_handle="vm-cafebabe")
        captured: list[list[str]] = []
        fake_process = MagicMock()
        fake_process.stdin = MagicMock()
        fake_process.returncode = None

        async def capture_open_process(cmd, **kwargs):
            captured.append(list(cmd))
            return fake_process

        with (
            patch("pty.openpty", return_value=(10, 11)),
            patch("fcntl.ioctl"),
            patch("os.close"),
            patch("anyio.open_process", side_effect=capture_open_process),
        ):
            await transport.connect()

        assert captured
        exec_cmd = captured[0]
        assert exec_cmd[0] == "matchlock"
        assert exec_cmd[1] == "exec"
        assert exec_cmd[3] == "vm-cafebabe"
        assert exec_cmd[5] == "sh"
        assert exec_cmd[6] == "-c"

    @pytest.mark.anyio
    async def test_connect_env_vars_inside_su_command(self) -> None:
        transport = self._make_transport(vm_handle="vm-cafebabe")
        captured: list[list[str]] = []
        fake_process = MagicMock()
        fake_process.stdin = MagicMock()
        fake_process.returncode = None

        async def capture_open_process(cmd, **kwargs):
            captured.append(list(cmd))
            return fake_process

        with (
            patch("pty.openpty", return_value=(10, 11)),
            patch("fcntl.ioctl"),
            patch("os.close"),
            patch("anyio.open_process", side_effect=capture_open_process),
        ):
            await transport.connect()

        full_sh_cmd = captured[0][7]
        assert "su -c" in full_sh_cmd
        assert full_sh_cmd.rstrip().endswith("claude")
        # Verify env vars are INSIDE the su -c argument (not before su).
        # The structure is: su -c '<env_prefix> <claude_cmd>' claude
        # Extract the su -c argument (the single-quoted inner command).
        import shlex

        parts = shlex.split(full_sh_cmd)
        su_idx = parts.index("su")
        su_c_arg = parts[su_idx + 2]  # su -c <this_arg> claude
        assert "CLAUDE_CODE_ENTRYPOINT" in su_c_arg, (
            f"env vars must be inside su -c argument, not outside. su -c arg: {su_c_arg}"
        )

    @pytest.mark.anyio
    async def test_connect_cleanup_on_failure_destroys_new_vm(self) -> None:
        transport = self._make_transport()

        with (
            patch("pty.openpty", return_value=(10, 11)),
            patch("fcntl.ioctl"),
            patch("os.close"),
            patch("anyio.open_process", new_callable=AsyncMock, side_effect=OSError("fail")),
            pytest.raises(OSError, match="fail"),
        ):
            await transport.connect()

        transport._backend.destroy_vm.assert_called_once_with("vm-aabbccdd")
        assert transport._vm_handle is None

    @pytest.mark.anyio
    async def test_connect_cleanup_does_not_destroy_preexisting_vm(self) -> None:
        transport = self._make_transport(vm_handle="vm-existing")

        with (
            patch("pty.openpty", return_value=(10, 11)),
            patch("fcntl.ioctl"),
            patch("os.close"),
            patch("anyio.open_process", new_callable=AsyncMock, side_effect=OSError("fail")),
            pytest.raises(OSError),
        ):
            await transport.connect()

        transport._backend.destroy_vm.assert_not_called()
