"""Guard tests: CLI commands documented in docs/ match actual Click definitions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.docs.conftest import DOCS_DIR, REPO_ROOT, parse_cli_command_refs

pytestmark = pytest.mark.docs

# Auto-generated files — excluded from all checks (circular or machine-written)
_EXCLUDED_PATHS = {
    DOCS_DIR / "reference" / "cli.md",
    DOCS_DIR / "reference" / "api",
}

# Human-authored docs where commands should be discoverable
_HUMAN_DOCS = [
    *(DOCS_DIR / "getting-started").glob("*.md"),
    *(DOCS_DIR / "guide").glob("*.md"),
    *(DOCS_DIR / "concepts").glob("*.md"),
    DOCS_DIR / "troubleshooting.md",
]

# Commands not expected in getting-started/ or guide/ docs.
# Group commands (containers), aliases, and admin/internal commands.
INTERNAL_COMMANDS: frozenset[str] = frozenset(
    {
        # Hidden internal
        "hooks run post-worktree",
        # Group containers (not leaf commands — the subcommands are documented)
        "auth jira",
        "db",
        "hooks",
        "hooks run",
    }
)


# Alias-derived commands (e.g., "p add", "s list") are not expected
# in docs — only the canonical names are documented.
def _get_alias_prefixes() -> tuple[str, ...]:
    from summon_claude.cli import AliasedGroup

    return tuple(AliasedGroup._ALIASES.keys())


_ALIAS_PREFIXES = _get_alias_prefixes()


def _is_excluded(path: Path) -> bool:
    """Return True if path is under an excluded path (file or directory)."""
    for excl in _EXCLUDED_PATHS:
        if path == excl or (excl.is_dir() and path.is_relative_to(excl)):
            return True
    return False


def _command_prefix_of(ref: str, valid_names: set[str]) -> str | None:
    """
    Return the longest valid command name that is a word-prefix of ref, or None.

    For example, "project remove my-api" returns "project remove" if that
    name is in valid_names. This handles trailing arguments after the command.
    """
    words = ref.split()
    for length in range(len(words), 0, -1):
        candidate = " ".join(words[:length])
        if candidate in valid_names:
            return candidate
    return None


def _extract_first_words(click_commands: set[str]) -> frozenset[str]:
    """Derive valid first words from Click command paths."""
    return frozenset(cmd.split()[0] for cmd in click_commands if cmd)


def _is_prose_artifact(ref: str, valid_first_words: frozenset[str]) -> bool:
    """
    Return True if the parsed ref is almost certainly a prose false-positive.

    Filters out:
    - Angle brackets (placeholder like <name>)
    - All-caps single token (bare argument like ABC123)
    - Mid-string "summon" (cross-line regex artifact)
    - First word not a known top-level command, group, or alias
    """
    if "<" in ref or ">" in ref:
        return True
    words = ref.split()
    if len(words) == 1 and words[0].isupper():
        return True
    # Cross-line regex artifact: a subsequent "summon" sentence was captured
    if "summon" in words:
        return True
    # First word must be a known entry point
    return bool(words and words[0] not in valid_first_words)


# ---------------------------------------------------------------------------
# Test 1: documented commands exist in Click
# ---------------------------------------------------------------------------


def test_documented_commands_exist(click_commands: set[str]) -> None:
    """Every 'summon <cmd>' reference in docs (non-auto-generated) must be a real command."""
    valid_first_words = _extract_first_words(click_commands)
    fabricated: list[tuple[str, Path]] = []

    all_md = sorted(DOCS_DIR.rglob("*.md"))
    for md_file in all_md:
        if _is_excluded(md_file):
            continue
        content = md_file.read_text(encoding="utf-8")
        refs = parse_cli_command_refs(content)
        for ref in refs:
            if _is_prose_artifact(ref, valid_first_words):
                continue
            # Accept if any prefix of the ref words is a valid command/group/alias.
            # This handles "project remove my-api" -> "project remove".
            if _command_prefix_of(ref, click_commands) is None:
                fabricated.append((ref, md_file))

    if fabricated:
        lines = [
            f"\nDocumented commands not found in Click ({len(fabricated)}):",
        ]
        for cmd, path in sorted(fabricated):
            rel = path.relative_to(REPO_ROOT)
            lines.append(f"  summon {cmd!r}  [{rel}]")
        pytest.fail("\n".join(lines))


# ---------------------------------------------------------------------------
# Test 2: all Click commands appear in at least one human-authored doc
# ---------------------------------------------------------------------------


def test_all_commands_are_documented(click_commands: set[str]) -> None:
    """Every Click command must appear in at least one human-authored guide doc."""
    valid_first_words = _extract_first_words(click_commands)
    # Collect all command refs from human-authored docs.
    # Because parse_cli_command_refs may include trailing arguments (e.g.
    # "project remove my-api"), we use prefix matching: a click command is
    # considered documented if it is a word-prefix of any documented ref.
    documented_refs: set[str] = set()
    for md_file in _HUMAN_DOCS:
        if not md_file.exists():
            continue
        content = md_file.read_text(encoding="utf-8")
        documented_refs |= {
            ref
            for ref in parse_cli_command_refs(content)
            if not _is_prose_artifact(ref, valid_first_words)
        }

    def _is_documented(cmd: str) -> bool:
        cmd_words = cmd.split()
        return any(ref.split()[: len(cmd_words)] == cmd_words for ref in documented_refs)

    def _is_alias_command(cmd: str) -> bool:
        """Return True if cmd is an alias-derived command (e.g., 'p add', 's list')."""
        return any(cmd == prefix or cmd.startswith(prefix + " ") for prefix in _ALIAS_PREFIXES)

    undocumented = {
        cmd
        for cmd in click_commands
        if not _is_documented(cmd) and cmd not in INTERNAL_COMMANDS and not _is_alias_command(cmd)
    }

    if undocumented:
        lines = [
            f"\nClick commands not found in any human-authored doc ({len(undocumented)}):",
        ]
        for cmd in sorted(undocumented):
            lines.append(f"  summon {cmd}")
        lines.append(
            "\nIf a command is intentionally internal, add it to INTERNAL_COMMANDS in this file."
        )
        pytest.fail("\n".join(lines))


# ---------------------------------------------------------------------------
# Test 3: documented flags exist in Click
# ---------------------------------------------------------------------------

# Flags always synthesised by Click — not present in param.opts introspection
_ALWAYS_PRESENT_FLAGS: frozenset[str] = frozenset({"--help", "-h"})

_LONG_FLAG_RE = re.compile(r"`(--[\w-]+)`")
_SHORT_FLAG_RE = re.compile(r"`(-\w)`")


def test_documented_options_exist(click_all_options: set[str]) -> None:
    """Every --flag / -f documented in human-authored docs must exist in Click."""
    fabricated: list[tuple[str, Path]] = []

    for md_file in _HUMAN_DOCS:
        if not md_file.exists():
            continue
        if _is_excluded(md_file):
            continue
        content = md_file.read_text(encoding="utf-8")
        flags: set[str] = set()
        flags.update(_LONG_FLAG_RE.findall(content))
        flags.update(_SHORT_FLAG_RE.findall(content))

        for flag in flags:
            if flag in _ALWAYS_PRESENT_FLAGS:
                continue
            if flag not in click_all_options:
                fabricated.append((flag, md_file))

    if fabricated:
        lines = [
            f"\nDocumented flags not found in Click ({len(fabricated)}):",
        ]
        for flag, path in sorted(fabricated):
            rel = path.relative_to(REPO_ROOT)
            lines.append(f"  {flag!r}  [{rel}]")
        pytest.fail("\n".join(lines))
