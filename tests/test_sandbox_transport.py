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
