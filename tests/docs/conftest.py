"""Shared fixtures for documentation validation tests."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from dotenv import dotenv_values

# ---------------------------------------------------------------------------
# Helpers — module-level, not fixtures
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"`(SUMMON_[A-Z_]+)`")
_CLI_CMD_BACKTICK_RE = re.compile(r"`summon\s+([^`]+)`")
_CLI_CMD_CODEBLOCK_RE = re.compile(r"^\$?\s*summon\s+((?:[a-z][\w-]*[ \t]*)+)", re.MULTILINE)
_MCP_TOOL_RE = re.compile(r"^#{3,4}\s+`([a-zA-Z][a-zA-Z0-9_]+)`", re.MULTILINE)


def parse_env_var_refs(content: str) -> set[str]:
    """Extract SUMMON_* env var names from backtick-wrapped references."""
    return set(_ENV_VAR_RE.findall(content))


def parse_cli_command_refs(content: str) -> set[str]:
    """Extract 'summon <cmd>' from backtick code spans and code block lines."""
    results = set()
    for match in _CLI_CMD_BACKTICK_RE.finditer(content):
        words = match.group(1).strip().split()
        cmd_words = [w for w in words if not w.startswith("-")]
        if cmd_words:
            results.add(" ".join(cmd_words))
    for match in _CLI_CMD_CODEBLOCK_RE.finditer(content):
        words = match.group(1).strip().split()
        cmd_words = [w for w in words if not w.startswith("-")]
        if cmd_words:
            results.add(" ".join(cmd_words))
    return results


def parse_mcp_tool_refs(content: str) -> set[str]:
    """Extract tool names from ### or #### heading backticks in MCP docs."""
    return set(_MCP_TOOL_RE.findall(content))


# ---------------------------------------------------------------------------
# Path fixtures
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="session")
def docs_dir() -> Path:
    """Path to the docs/ directory."""
    d = _REPO_ROOT / "docs"
    assert d.exists(), f"docs/ not found at {d}"
    return d


@pytest.fixture(scope="session")
def all_md_files(docs_dir: Path) -> list[Path]:
    """All markdown files under docs/."""
    files = sorted(docs_dir.rglob("*.md"))
    assert files, "No .md files found under docs/"
    return files


# ---------------------------------------------------------------------------
# Credential fixture
# ---------------------------------------------------------------------------


def _map_test_env_vars() -> dict[str, str]:
    """Read SUMMON_TEST_* env vars and map to SUMMON_* names.

    Used as a fallback when no ``.env`` file provides credentials.
    ``_isolate_summon_config`` preserves ``SUMMON_TEST_*`` vars, so
    CI-injected credentials survive the session fixture.
    """
    creds: dict[str, str] = {}
    for k, v in os.environ.items():
        if k.startswith("SUMMON_TEST_") and v:
            creds[k.replace("SUMMON_TEST_", "SUMMON_", 1)] = v
    return creds


@pytest.fixture(scope="session")
def env_credentials() -> dict[str, str]:
    """Load SUMMON_* credentials from sibling repo .env or SUMMON_TEST_* env vars.

    Primary: reads from ``<repo-parent>/summon-claude/.env``.
    Fallback: reads ``SUMMON_TEST_*`` env vars via :func:`_map_test_env_vars`.
    """
    env_file = _REPO_ROOT.parent / "summon-claude" / ".env"
    raw = dotenv_values(str(env_file)) if env_file.exists() else {}
    creds = {k: v for k, v in raw.items() if k.startswith("SUMMON_") and v is not None}
    if not creds:
        creds = _map_test_env_vars()
    return creds


# ---------------------------------------------------------------------------
# Source introspection fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def summon_config_fields() -> dict[str, object]:
    """Map of SUMMON_* env var name -> FieldInfo from SummonConfig."""
    from summon_claude.config import SummonConfig

    prefix = SummonConfig.model_config.get("env_prefix", "SUMMON_")
    result = {}
    for field_name, field_info in SummonConfig.model_fields.items():
        env_name = f"{prefix}{field_name.upper()}"
        result[env_name] = field_info
    return result


@pytest.fixture(scope="session")
def click_commands() -> set[str]:
    """All CLI command paths (leaf + group) via Click introspection, including aliases."""
    import click

    from summon_claude.cli import cli

    commands: set[str] = set()

    def _walk(group: click.BaseCommand, prefix: str = "") -> None:
        if isinstance(group, click.Group):
            # Include the group path itself (e.g., "db", "project workflow")
            if prefix.strip():
                commands.add(prefix.strip())
            ctx = click.Context(group, info_name=prefix.strip() or "summon")
            for name in group.list_commands(ctx):
                cmd = group.get_command(ctx, name)
                if cmd is None:
                    continue
                full = f"{prefix}{name}".strip()
                if isinstance(cmd, click.Group):
                    _walk(cmd, full + " ")
                else:
                    commands.add(full)
        elif prefix.strip():
            commands.add(prefix.strip())

    _walk(cli)

    # Add AliasedGroup aliases (e.g., "s" -> "session", "p" -> "project")
    from summon_claude.cli import AliasedGroup

    if isinstance(cli, AliasedGroup) and hasattr(AliasedGroup, "_ALIASES"):
        for alias, canonical in AliasedGroup._ALIASES.items():
            # Add alias as a command (e.g., "p") and alias+subcommand combos (e.g., "s list")
            commands.add(alias)
            for cmd in list(commands):
                if cmd.startswith(canonical + " "):
                    aliased = alias + cmd[len(canonical) :]
                    commands.add(aliased)

    return commands


@pytest.fixture(scope="session")
def click_all_options() -> set[str]:
    """All option flags from all CLI commands."""
    import click

    from summon_claude.cli import cli

    options: set[str] = set()

    def _walk(group: click.BaseCommand) -> None:
        for param in group.params:
            if isinstance(param, click.Option):
                options.update(param.opts)
                options.update(param.secondary_opts)
        if isinstance(group, click.Group):
            ctx = click.Context(group, info_name="summon")
            for name in group.list_commands(ctx):
                cmd = group.get_command(ctx, name)
                if cmd is not None:
                    _walk(cmd)

    _walk(cli)
    return options


async def _async_collect_mcp_tools() -> list:
    """Collect all MCP tool objects from the three servers."""
    from summon_claude.canvas_mcp import create_canvas_mcp_tools
    from summon_claude.sessions.registry import SessionRegistry
    from summon_claude.slack.mcp import create_summon_mcp_tools
    from summon_claude.summon_cli_mcp import create_summon_cli_mcp_tools
    from tests.conftest import make_scheduler

    tools = []

    # summon-cli tools — needs a real (temp) registry for introspection
    fd, tmp_str = tempfile.mkstemp(suffix=".db", prefix="summon_doc_test_")
    os.close(fd)
    tmp = Path(tmp_str)
    reg = SessionRegistry(db_path=tmp)
    async with reg:
        cli_tools = create_summon_cli_mcp_tools(
            registry=reg,
            session_id="test-doc-session",
            authenticated_user_id="U_TEST",
            channel_id="C_TEST",
            cwd="/tmp/test",
            scheduler=make_scheduler(),
            is_pm=True,
            pm_status_ts="1234567890.123456",
            _web_client=AsyncMock(),
        )
        tools.extend(cli_tools)

        # GPM-only tools (get_workflow_instructions etc.)
        gpm_tools = create_summon_cli_mcp_tools(
            registry=reg,
            session_id="test-doc-gpm",
            authenticated_user_id="U_TEST",
            channel_id="C_TEST",
            cwd="/tmp/test",
            scheduler=make_scheduler(),
            is_pm=True,
            is_global_pm=True,
        )
        gpm_only = {t.name for t in gpm_tools} - {t.name for t in cli_tools}
        tools.extend(t for t in gpm_tools if t.name in gpm_only)

        # summon-canvas tools — reuse registry
        canvas_tools = create_canvas_mcp_tools(
            canvas_store=AsyncMock(),
            registry=reg,
            authenticated_user_id="U_TEST",
            channel_id="C_TEST",
        )
        tools.extend(canvas_tools)

    # Clean up temp db
    tmp.unlink(missing_ok=True)

    # summon-slack tools
    slack_mock = AsyncMock()
    slack_mock.channel_id = "C_TEST"
    slack_tools = create_summon_mcp_tools(
        slack_mock,
        allowed_channels=AsyncMock(return_value={"C_TEST"}),
    )
    tools.extend(slack_tools)

    return tools


_CACHED_TOOLS: list | None = None


def _get_mcp_tools() -> list:
    global _CACHED_TOOLS  # noqa: PLW0603
    if _CACHED_TOOLS is None:
        loop = asyncio.new_event_loop()
        try:
            _CACHED_TOOLS = loop.run_until_complete(_async_collect_mcp_tools())
        finally:
            loop.close()
    return _CACHED_TOOLS


@pytest.fixture(scope="session")
def mcp_tool_names() -> set[str]:
    """All MCP tool names across all three servers."""
    return {t.name for t in _get_mcp_tools()}


@pytest.fixture(scope="session")
def mcp_tool_schemas() -> dict[str, dict]:
    """Map of tool name -> input_schema dict for all MCP tools."""
    return {t.name: t.input_schema for t in _get_mcp_tools()}
