"""Test bash CLI command execution in documentation.

Extracts summon commands from bash code blocks and executes them.
Non-summon commands (git, brew, make, npm, etc.) are ignored.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.docs]

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DOCS_DIR = _REPO_ROOT / "docs"

# Commands that require a running daemon, are interactive, or have side effects
SKIP_COMMANDS: frozenset[str] = frozenset(
    {
        "summon init",
        "summon start",
        "summon stop",
        "summon session logs",
        "summon project up",
        "summon project down",
        "summon project add",
        "summon project remove",
        "summon project workflow",
        "summon hooks run",
        "summon hooks install",
        "summon hooks uninstall",
        "summon hooks set",
        "summon hooks clear",
        "summon auth github login",
        "summon auth github logout",
        "summon auth google login",
        "summon auth slack login",
        "summon auth slack logout",
        "summon auth slack channels",
        "summon reset",
        "summon db purge",
        "summon db vacuum",
        "summon config set",
        "summon config edit",
        "summon config check",
    }
)

# Commands that run without any credentials
TIER1_COMMANDS: frozenset[str] = frozenset(
    {
        "summon --version",
        "summon --help",
        "summon version",
        "summon config path",
    }
)

# Code fence regex — matches ```bash or ```{ .bash } but not notest variants
# No re.MULTILINE needed: these are used with .match() on individual lines
_BASH_FENCE_RE = re.compile(r"^```(?:bash|\{\s*\.bash\s*\})\s*$")
_BASH_NOTEST_FENCE_RE = re.compile(r"^```(?:bash\s+notest|\{\s*\.bash\s+\.notest\s*\})\s*$")
_FENCE_CLOSE_RE = re.compile(r"^```\s*$")

# Summon command line regex — matches "$ summon ..." or bare "summon ..."
_SUMMON_CMD_RE = re.compile(r"^\$?\s*(summon\s+.+)$", re.MULTILINE)


def _is_tier1(cmd: str) -> bool:
    """Check if command runs without credentials."""
    return any(cmd.startswith(t) for t in TIER1_COMMANDS) or cmd.endswith("--help")


def _should_skip(cmd: str) -> bool:
    """Check if command should be skipped.

    Handles global flags before the subcommand (e.g. ``summon -v start``
    matches ``summon start``).
    """
    # Strip global flags between "summon" and the subcommand
    parts = cmd.split()
    if parts and parts[0] == "summon":
        non_flag = [parts[0]] + [p for p in parts[1:] if not p.startswith("-")]
        normalized = " ".join(non_flag)
        return any(normalized.startswith(s) for s in SKIP_COMMANDS)
    return any(cmd.startswith(s) for s in SKIP_COMMANDS)


def _extract_bash_blocks(content: str) -> list[str]:
    """Extract content of ```bash blocks (skip ```bash notest blocks)."""
    blocks: list[str] = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        # Check if this is a bash fence
        if _BASH_FENCE_RE.match(line.strip()):
            # Collect block content
            block_lines: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE_CLOSE_RE.match(lines[i].strip()):
                block_lines.append(lines[i])
                i += 1
            blocks.append("\n".join(block_lines))
        elif _BASH_NOTEST_FENCE_RE.match(line.strip()):
            # Skip notest block
            i += 1
            while i < len(lines) and not _FENCE_CLOSE_RE.match(lines[i].strip()):
                i += 1
        i += 1
    return blocks


def _extract_summon_commands(block: str) -> list[str]:
    """Extract summon command lines from a bash block."""
    commands: list[str] = []
    for match in _SUMMON_CMD_RE.finditer(block):
        cmd = match.group(1).strip()
        # Remove trailing comments
        if " #" in cmd:
            cmd = cmd[: cmd.index(" #")].strip()
        if cmd:
            commands.append(cmd)
    return commands


def _collect_testable_files() -> list[tuple[str, Path]]:
    """Collect markdown files that have executable bash blocks with summon commands."""
    testable: list[tuple[str, Path]] = []
    for md_file in sorted(_DOCS_DIR.rglob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        blocks = _extract_bash_blocks(content)
        commands: list[str] = []
        for block in blocks:
            cmds = _extract_summon_commands(block)
            commands.extend(c for c in cmds if not _should_skip(c))
        if commands:
            rel = str(md_file.relative_to(_DOCS_DIR))
            testable.append((rel, md_file))
    return testable


_TESTABLE_FILES = _collect_testable_files()


@pytest.mark.parametrize(
    "md_file",
    [t[1] for t in _TESTABLE_FILES],
    ids=[t[0] for t in _TESTABLE_FILES],
)
def test_bash_commands_execute(
    md_file: Path,
    env_credentials: dict[str, str],
) -> None:
    """Summon commands in bash code blocks must execute successfully."""
    env = dict(os.environ)
    env.update(env_credentials)

    content = md_file.read_text(encoding="utf-8")
    blocks = _extract_bash_blocks(content)

    for block in blocks:
        commands = _extract_summon_commands(block)
        for cmd in commands:
            if _should_skip(cmd):
                continue
            if not _is_tier1(cmd) and not env_credentials:
                continue  # Skip credential-requiring commands when no creds

            # Replace summon prefix with uv run summon
            exec_cmd = cmd.replace("summon ", "uv run summon ", 1)

            result = subprocess.run(  # noqa: S602
                exec_cmd,
                shell=True,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )

            assert result.returncode == 0, (
                f"Command failed: {cmd}\n"
                f"Exit code: {result.returncode}\n"
                f"stdout: {result.stdout[:500]}\n"
                f"stderr: {result.stderr[:500]}"
            )
            assert "Traceback" not in result.stderr, (
                f"Python traceback in: {cmd}\nstderr: {result.stderr[:500]}"
            )


# ---------------------------------------------------------------------------
# Bash syntax checking for notest blocks
# ---------------------------------------------------------------------------

_BASH_KEYWORDS = re.compile(r"\b(if|for|while|function|do|case|then|fi|done|esac)\b")


def _extract_notest_blocks(content: str) -> list[str]:
    """Extract content of ```bash notest blocks."""
    blocks: list[str] = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if _BASH_NOTEST_FENCE_RE.match(line.strip()):
            block_lines: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE_CLOSE_RE.match(lines[i].strip()):
                block_lines.append(lines[i])
                i += 1
            block_content = "\n".join(block_lines)
            # Only syntax-check blocks with bash keywords (actual scripts)
            if _BASH_KEYWORDS.search(block_content):
                blocks.append(block_content)
        i += 1
    return blocks


def _collect_notest_files() -> list[tuple[str, Path]]:
    """Collect files with notest bash blocks containing bash keywords."""
    testable: list[tuple[str, Path]] = []
    for md_file in sorted(_DOCS_DIR.rglob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        if _extract_notest_blocks(content):
            rel = str(md_file.relative_to(_DOCS_DIR))
            testable.append((rel, md_file))
    return testable


_NOTEST_FILES = _collect_notest_files()


@pytest.mark.parametrize(
    "md_file",
    [t[1] for t in _NOTEST_FILES]
    if _NOTEST_FILES
    else [pytest.param(None, marks=pytest.mark.skip)],
    ids=[t[0] for t in _NOTEST_FILES] if _NOTEST_FILES else ["no-notest-blocks"],
)
def test_bash_syntax_valid(md_file: Path | None) -> None:
    """Notest bash blocks with script keywords must pass bash -n syntax check."""
    assert md_file is not None  # guarded by skip mark above

    content = md_file.read_text(encoding="utf-8")
    blocks = _extract_notest_blocks(content)

    for i, block in enumerate(blocks):
        result = subprocess.run(
            ["bash", "-n"],
            input=block,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"Bash syntax error in {md_file.relative_to(_DOCS_DIR)} notest block {i + 1}:\n"
            f"stderr: {result.stderr[:500]}\n"
            f"block: {block[:200]}"
        )
