"""MatchlockTransport — SDK Transport subclass for Matchlock VM execution."""

# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
# claude_agent_sdk doesn't ship type stubs

from __future__ import annotations

import fcntl
import json
import logging
import os
import pty
import re
import shlex
import struct
import subprocess
import termios
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import anyio
import anyio.abc
from claude_agent_sdk import ClaudeAgentOptions, Transport

from summon_claude.sandbox import SandboxBackend, VmConfig, VmHandle

logger = logging.getLogger(__name__)

# ANSI escape sequences + carriage return stripping
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b\([A-Z]|\r")

_MAX_BUFFER_BYTES = 10 * 1024 * 1024  # 10MB

# ClaudeAgentOptions field categorization for guard test (C4).
# These sets document which fields _build_command handles vs delegates to Query.
_BUILD_COMMAND_FIELDS: frozenset[str] = frozenset(
    {
        "tools",
        "allowed_tools",
        "system_prompt",
        "mcp_servers",
        "permission_mode",
        "continue_conversation",
        "resume",
        "session_id",
        "max_turns",
        "max_budget_usd",
        "disallowed_tools",
        "model",
        "fallback_model",
        "betas",
        "permission_prompt_tool_name",
        "settings",
        "add_dirs",
        "include_partial_messages",
        "fork_session",
        "setting_sources",
        "plugins",
        "extra_args",
        "max_thinking_tokens",
        "thinking",
        "effort",
        "output_format",
        "task_budget",
        "skills",
    }
)

_QUERY_HANDLED_FIELDS: frozenset[str] = frozenset(
    {
        "can_use_tool",
        "hooks",
        "agents",
        "debug_stderr",
        "stderr",
        "env",
        "user",
        "cwd",
        "max_buffer_size",
        "enable_file_checkpointing",
    }
)

_INTENTIONALLY_SKIPPED_FIELDS: frozenset[str] = frozenset(
    {
        "cli_path",  # meaningless inside VM — always "claude"
        "sandbox",  # VM IS the sandbox
    }
)

# SDK version string, read once at module import
try:
    import importlib.metadata as _importlib_metadata

    _SDK_VERSION: str = _importlib_metadata.version("claude-agent-sdk")
except Exception:
    _SDK_VERSION = "unknown"


def _strip_ansi(data: str) -> str:
    """Strip ANSI escape sequences and carriage returns from PTY output.

    MUST be applied to raw PTY output BEFORE json.loads() (SEC-D-014).
    """
    return _ANSI_RE.sub("", data)


class MatchlockTransport(Transport):
    """SDK Transport that runs Claude Code inside a Matchlock VM via PTY.

    Connects by either creating a new VM (via backend.create_vm) or reusing
    an existing handle. Runs `claude` as the non-root `claude` user inside
    the VM. PTY is used so Claude Code receives a terminal and produces
    stream-json output correctly (Decision 5).
    """

    def __init__(
        self,
        backend: SandboxBackend,
        vm_config: VmConfig,
        options: ClaudeAgentOptions,
        *,
        vm_handle: VmHandle | None = None,
    ) -> None:
        self._backend = backend
        self._config = vm_config
        self._options = options
        self._vm_handle: VmHandle | None = vm_handle
        self._process: anyio.abc.Process | None = None
        self._master_fd: int | None = None
        self._ready = False
        self._buffer = ""

    def _build_command(self) -> list[str]:  # noqa: PLR0912, PLR0915
        """Build the `claude` CLI command to run inside the VM.

        Mirrors SubprocessCLITransport._build_command() with VM-specific
        adjustments:
        - CLI path is always "claude" (installed in VM image)
        - --dangerously-skip-permissions is hardcoded (Decision 7, safe in VM)
        - --permission-prompt-tool is skipped (dead under skip-permissions)
        - --plugin-dir is skipped (host paths don't exist in VM)
        """
        cmd = [
            "claude",
            "--output-format",
            "stream-json",
            "--verbose",
            "--input-format",
            "stream-json",
            "--dangerously-skip-permissions",
        ]

        # System prompt
        if self._options.system_prompt is None:
            cmd.extend(["--system-prompt", ""])
        elif isinstance(self._options.system_prompt, str):
            cmd.extend(["--system-prompt", self._options.system_prompt])
        else:
            sp = self._options.system_prompt
            if sp.get("type") == "file":
                cmd.extend(["--system-prompt-file", sp["path"]])
            elif sp.get("type") == "preset" and "append" in sp:
                cmd.extend(["--append-system-prompt", sp["append"]])

        # Tools (base set)
        if self._options.tools is not None:
            tools = self._options.tools
            if isinstance(tools, list):
                if len(tools) == 0:
                    cmd.extend(["--tools", ""])
                else:
                    cmd.extend(["--tools", ",".join(tools)])
            else:
                # Preset object
                cmd.extend(["--tools", "default"])

        # Compute effective allowed_tools via skills defaults (same as SubprocessCLI)
        allowed_tools, effective_setting_sources = self._apply_skills_defaults()
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])

        if self._options.disallowed_tools:
            cmd.extend(["--disallowedTools", ",".join(self._options.disallowed_tools)])

        if self._options.model:
            cmd.extend(["--model", self._options.model])

        if self._options.fallback_model:
            cmd.extend(["--fallback-model", self._options.fallback_model])

        if self._options.max_turns:
            cmd.extend(["--max-turns", str(self._options.max_turns)])

        if self._options.max_budget_usd is not None:
            cmd.extend(["--max-budget-usd", str(self._options.max_budget_usd)])

        if self._options.task_budget is not None:
            cmd.extend(["--task-budget", str(self._options.task_budget["total"])])

        if self._options.betas:
            cmd.extend(["--betas", ",".join(self._options.betas)])

        # permission_mode: skipped — --dangerously-skip-permissions overrides it
        # permission_prompt_tool_name: skipped — dead under --dangerously-skip-permissions

        if self._options.continue_conversation:
            cmd.append("--continue")

        if self._options.resume:
            cmd.extend(["--resume", self._options.resume])

        if self._options.session_id:
            cmd.extend(["--session-id", self._options.session_id])

        # settings: defer complex _build_settings_value — pass raw string if provided
        if self._options.settings is not None and isinstance(self._options.settings, str):
            cmd.extend(["--settings", self._options.settings])

        if self._options.add_dirs:
            for directory in self._options.add_dirs:
                cmd.extend(["--add-dir", str(directory)])

        if self._options.mcp_servers:
            if isinstance(self._options.mcp_servers, dict):
                servers_for_cli: dict[str, Any] = {}
                for name, config in self._options.mcp_servers.items():
                    if isinstance(config, dict) and config.get("type") == "sdk":
                        servers_for_cli[name] = {k: v for k, v in config.items() if k != "instance"}
                    else:
                        servers_for_cli[name] = config
                if servers_for_cli:
                    cmd.extend(["--mcp-config", json.dumps({"mcpServers": servers_for_cli})])
            else:
                cmd.extend(["--mcp-config", str(self._options.mcp_servers)])

        if self._options.include_partial_messages:
            cmd.append("--include-partial-messages")

        if self._options.fork_session:
            cmd.append("--fork-session")

        if effective_setting_sources is not None:
            cmd.append(f"--setting-sources={','.join(effective_setting_sources)}")

        # plugins: skipped — host-side paths don't exist in VM

        # extra_args passthrough
        for flag, value in self._options.extra_args.items():
            if value is None:
                cmd.append(f"--{flag}")
            else:
                cmd.extend([f"--{flag}", str(value)])

        # Thinking config
        if self._options.thinking is not None:
            t = self._options.thinking
            if t["type"] == "adaptive":
                cmd.extend(["--thinking", "adaptive"])
            elif t["type"] == "enabled":
                cmd.extend(["--max-thinking-tokens", str(t["budget_tokens"])])
            elif t["type"] == "disabled":
                cmd.extend(["--thinking", "disabled"])
        elif self._options.max_thinking_tokens is not None:
            cmd.extend(["--max-thinking-tokens", str(self._options.max_thinking_tokens)])

        if self._options.effort is not None:
            cmd.extend(["--effort", self._options.effort])

        # output_format: json_schema
        if (
            self._options.output_format is not None
            and isinstance(self._options.output_format, dict)
            and self._options.output_format.get("type") == "json_schema"
        ):
            schema = self._options.output_format.get("schema")
            if schema is not None:
                cmd.extend(["--json-schema", json.dumps(schema)])

        return cmd

    def _apply_skills_defaults(self) -> tuple[list[str], list[str] | None]:
        """Delegate to SubprocessCLITransport._apply_skills_defaults via mixin call.

        We replicate the logic directly to avoid instantiating SubprocessCLITransport.
        """
        allowed_tools: list[str] = list(self._options.allowed_tools)
        setting_sources: list[str] | None = (
            list(self._options.setting_sources)
            if self._options.setting_sources is not None
            else None
        )

        skills = self._options.skills
        if skills is None:
            return allowed_tools, setting_sources

        if skills == "all":
            if "Skill" not in allowed_tools:
                allowed_tools.append("Skill")
        else:
            for name in skills:
                pattern = f"Skill({name})"
                if pattern not in allowed_tools:
                    allowed_tools.append(pattern)

        if setting_sources is None:
            setting_sources = ["user", "project"]

        return allowed_tools, setting_sources

    async def connect(self) -> None:
        """Start Claude Code inside the VM via PTY."""
        if self._ready:
            return

        # Create VM if no existing handle
        if self._vm_handle is None:
            self._vm_handle = await self._backend.create_vm(self._config)

        # Allocate PTY
        master_fd, slave_fd = pty.openpty()

        try:
            # Set PTY window size: 24 rows x 65535 cols (wide enough to not wrap JSON)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 65535, 0, 0))

            # Build claude command
            cmd = self._build_command()

            # Build env var prefix to inject into the VM
            env_vars: dict[str, str] = {
                "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
                "CLAUDE_AGENT_SDK_VERSION": _SDK_VERSION,
                "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1",
                "PWD": self._config.guest_workspace_path,
            }

            # Vertex ADC passthrough — if host uses Vertex, propagate to VM
            for key in ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION"):
                val = os.environ.get(key)
                if val:
                    env_vars[key] = val

            # Build the full command:
            # 1. Quote every claude arg individually for the inner su -c shell
            claude_cmd_str = " ".join(shlex.quote(arg) for arg in cmd)
            # 2. Wrap with su to run as the non-root claude user
            su_command = f"su -c {shlex.quote(claude_cmd_str)} claude"
            # 3. Prepend env vars
            env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env_vars.items())
            full_sh_cmd = f"{env_prefix} {su_command}"

            # 4. Wrap in matchlock exec -i <handle> -- sh -c <full_cmd>
            exec_cmd = [
                "matchlock",
                "exec",
                "-i",
                self._vm_handle,
                "--",
                "sh",
                "-c",
                full_sh_cmd,
            ]

            self._process = await anyio.open_process(
                exec_cmd,
                stdin=subprocess.PIPE,
                stdout=slave_fd,
                stderr=subprocess.PIPE,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise

        # Close slave_fd on the host side — the child process holds it
        os.close(slave_fd)
        self._master_fd = master_fd
        self._ready = True

        logger.info(
            "MatchlockTransport connected to VM %s (claude in su as claude user)",
            self._vm_handle,
        )

    async def write(self, data: str) -> None:
        """Write raw data to the process stdin."""
        if not self._ready or self._process is None or self._process.stdin is None:
            raise RuntimeError("MatchlockTransport is not connected")
        await self._process.stdin.send(data.encode())

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        """Read and yield parsed JSON messages from the PTY master fd."""
        while self._ready:
            if self._master_fd is None:
                break
            try:
                await anyio.wait_readable(self._master_fd)
                raw = os.read(self._master_fd, 65536)
            except OSError:
                # PTY closed or EOF
                break
            if not raw:
                break

            # SEC-D-014: strip ANSI before parsing
            text = _strip_ansi(raw.decode(errors="replace"))
            self._buffer += text

            # Guard against unbounded buffer growth from a misbehaving process.
            # Keep the last partial line so we don't drop a valid in-progress message.
            if len(self._buffer) > _MAX_BUFFER_BYTES:
                last_newline = self._buffer.rfind("\n")
                if last_newline >= 0:
                    logger.warning(
                        "Transport buffer exceeded %d bytes, discarding completed lines",
                        _MAX_BUFFER_BYTES,
                    )
                    self._buffer = self._buffer[last_newline + 1 :]
                else:
                    logger.warning(
                        "Transport buffer exceeded %d bytes with no newline, clearing",
                        _MAX_BUFFER_BYTES,
                    )
                    self._buffer = ""

            # Process complete lines
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("MatchlockTransport: non-JSON line: %r", line[:200])

    async def close(self) -> None:
        """Terminate the process. VM stays alive for reuse."""
        self._ready = False

        if self._process is not None:
            if self._process.returncode is None:
                with suppress(ProcessLookupError, OSError):
                    self._process.terminate()
                with suppress(Exception):
                    with anyio.fail_after(5):
                        await self._process.wait()
                if self._process.returncode is None:
                    with suppress(ProcessLookupError, OSError):
                        self._process.kill()
                    with suppress(Exception):
                        await self._process.wait()
            self._process = None

        if self._master_fd is not None:
            with suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None

        logger.info("MatchlockTransport closed (VM %s still running)", self._vm_handle)

    def is_ready(self) -> bool:
        """Return True if transport is connected and ready."""
        return self._ready

    async def end_input(self) -> None:
        """No-op — Query never calls this for PTY transports."""


async def create_matchlock_transport(
    backend: SandboxBackend,
    vm_config: VmConfig,
    options: ClaudeAgentOptions,
    *,
    vm_handle: VmHandle | None = None,
) -> MatchlockTransport:
    """Create a MatchlockTransport, optionally reusing an existing VM.

    If vm_handle is provided, checks is_running first. Falls back to
    creating a new VM with fresh volume on crash (Decision 6 security).
    """
    if vm_handle is not None and not await backend.is_running(vm_handle):
        logger.warning("VM %s is not running — creating fresh VM (crash recovery)", vm_handle)
        vm_handle = None  # Force new VM creation in connect()
    return MatchlockTransport(backend, vm_config, options, vm_handle=vm_handle)
