"""Hooks subcommand logic for CLI — install/uninstall Claude Code hook bridge and manage lifecycle hooks."""  # noqa: E501

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import click

from summon_claude.cli.formatting import echo
from summon_claude.config import get_claude_config_dir
from summon_claude.sessions.hooks import VALID_HOOK_TYPES, run_post_worktree_hooks
from summon_claude.sessions.registry import SessionRegistry

# ---------------------------------------------------------------------------
# Shell wrapper templates
# ---------------------------------------------------------------------------

# Substitution token replaced at install time with the absolute summon binary path.
_SUMMON_PATH_TOKEN = "@@SUMMON_PATH@@"  # noqa: S105

# summon-pre-worktree.sh — runs before EnterWorktree; fetches latest main branch.
# GIT_SSH_COMMAND sets ConnectTimeout + BatchMode to prevent SSH hangs (security C5).
PRE_WORKTREE_TEMPLATE = """\
#!/bin/bash
# summon PreToolUse:EnterWorktree hook — fetches latest code before branching
DB="${XDG_DATA_HOME:-$HOME/.local/share}/summon/registry.db"
[ -f "$DB" ] || exit 0
# Fast-path: skip if no projects registered. Without sqlite3, assume projects
# exist (DB file presence is sufficient — non-summon users won't have it).
if command -v sqlite3 >/dev/null 2>&1; then
    RESULT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM projects" 2>/dev/null)
    [ "${RESULT:-0}" -gt 0 ] || exit 0
fi
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null)
if [ "$BRANCH" = "main" ]; then
    GIT_SSH='ssh -o ConnectTimeout=10 -o BatchMode=yes'
    GIT_SSH_COMMAND="$GIT_SSH" git fetch upstream main --no-tags 2>/dev/null \
        || GIT_SSH_COMMAND="$GIT_SSH" git fetch origin main --no-tags 2>/dev/null
    git merge --ff-only upstream/main 2>/dev/null \
        || git merge --ff-only origin/main 2>/dev/null || true
fi
exit 0
"""

# summon-post-worktree.sh — runs after EnterWorktree; triggers worktree_create hooks.
# @@SUMMON_PATH@@ is substituted at install time via str.replace() (not format strings).
# sqlite3 is soft-gated: if missing, falls through to the Python runner which handles it.
# No exec: exec replaces the shell, making any fallback exit unreachable for non-zero returns.
# Unconditional `; exit 0` after subprocess call ensures clean exit regardless of summon's status.
POST_WORKTREE_TEMPLATE = """\
#!/bin/bash
# summon PostToolUse:EnterWorktree hook — runs worktree_create lifecycle hooks
DB="${XDG_DATA_HOME:-$HOME/.local/share}/summon/registry.db"
[ -f "$DB" ] || exit 0
if command -v sqlite3 >/dev/null 2>&1; then
    RESULT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM projects" 2>/dev/null)
    [ "${RESULT:-0}" -gt 0 ] || exit 0
fi
SUMMON_BIN='@@SUMMON_PATH@@'
[ -x "$SUMMON_BIN" ] || SUMMON_BIN=$(command -v summon 2>/dev/null)
[ -x "$SUMMON_BIN" ] || exit 0
"$SUMMON_BIN" hooks run post-worktree; exit 0
"""

# ---------------------------------------------------------------------------
# settings.json helpers
# ---------------------------------------------------------------------------

_CLAUDE_DIR = get_claude_config_dir()
_SETTINGS_PATH = _CLAUDE_DIR / "settings.json"
_HOOKS_DIR = _CLAUDE_DIR / "hooks"
_PRE_SCRIPT = _HOOKS_DIR / "summon-pre-worktree.sh"
_POST_SCRIPT = _HOOKS_DIR / "summon-post-worktree.sh"

# Markers used to identify summon-owned hook entries when reading settings.json.
_PRE_MARKER = "summon-pre-worktree"
_POST_MARKER = "summon-post-worktree"


def _read_settings() -> dict[str, Any]:
    """Read ~/.claude/settings.json, returning an empty dict if missing."""
    if not _SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(_SETTINGS_PATH.read_text())
    except json.JSONDecodeError as e:
        raise click.ClickException(
            f"{_SETTINGS_PATH} contains invalid JSON. Fix or remove it before installing hooks: {e}"
        ) from e


def _write_settings(settings: dict[str, Any]) -> None:
    """Write settings dict back to ~/.claude/settings.json with 2-space indent."""
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")


def _build_hook_entry(matcher: str, command: str) -> dict[str, Any]:
    return {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command}],
    }


def _upsert_hook(
    entries: list[dict[str, Any]], marker: str, matcher: str, command: str
) -> list[dict[str, Any]]:
    """Replace the entry containing *marker* in its command path, or append if absent."""
    new_entry = _build_hook_entry(matcher, command)
    for i, entry in enumerate(entries):
        for hook in entry.get("hooks", []):
            if marker in hook.get("command", ""):
                entries[i] = new_entry
                return entries
    entries.append(new_entry)
    return entries


def _remove_hook(entries: list[dict[str, Any]], marker: str) -> list[dict[str, Any]]:
    """Remove any entry containing *marker* in its command path."""
    return [
        e for e in entries if not any(marker in h.get("command", "") for h in e.get("hooks", []))
    ]


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------


def install_hooks() -> None:
    """Install the Claude Code hook bridge (shell wrappers + settings.json entries).

    Idempotent — safe to run multiple times. Replaces existing summon entries.
    """
    # Resolve summon binary path. Fall back to sys.argv[0] for dev installs
    # (e.g. 'uv run summon' where 'summon' is not on PATH but is the calling binary).
    summon_bin = shutil.which("summon")
    if summon_bin is None and sys.argv and sys.argv[0]:
        # The calling binary might be summon invoked via 'uv run' or 'python -m'.
        candidate = Path(sys.argv[0]).resolve()
        if candidate.exists() and "summon" in candidate.name:
            summon_bin = str(candidate)
    if summon_bin is None:
        raise click.ClickException(
            "Cannot find 'summon' on PATH. Install summon first, then re-run."
        )

    # Note if sqlite3 CLI is absent — hooks still work but skip the
    # "no projects registered" fast-path (falls through to Python runner).
    if shutil.which("sqlite3") is None:
        click.echo(
            "Note: sqlite3 CLI not found on PATH. "
            "Hook fast-path will be skipped (minor performance impact).",
            err=True,
        )

    # Write shell wrapper scripts.
    _HOOKS_DIR.mkdir(parents=True, exist_ok=True)

    pre_content = PRE_WORKTREE_TEMPLATE
    post_content = POST_WORKTREE_TEMPLATE.replace(_SUMMON_PATH_TOKEN, summon_bin)

    _PRE_SCRIPT.write_text(pre_content)
    _POST_SCRIPT.write_text(post_content)

    # chmod 700 — owner-only execute permissions.
    for script in (_PRE_SCRIPT, _POST_SCRIPT):
        script.chmod(0o700)

    # Update ~/.claude/settings.json.
    settings = _read_settings()
    settings.setdefault("hooks", {})

    pre_command = str(_PRE_SCRIPT)
    post_command = str(_POST_SCRIPT)

    pre_entries: list[dict[str, Any]] = settings["hooks"].get("PreToolUse", [])
    pre_entries = _upsert_hook(pre_entries, _PRE_MARKER, "EnterWorktree", pre_command)
    settings["hooks"]["PreToolUse"] = pre_entries

    post_entries: list[dict[str, Any]] = settings["hooks"].get("PostToolUse", [])
    post_entries = _upsert_hook(post_entries, _POST_MARKER, "EnterWorktree", post_command)
    settings["hooks"]["PostToolUse"] = post_entries

    _write_settings(settings)

    click.echo(f"Installed pre-worktree hook:  {_PRE_SCRIPT}")
    click.echo(f"Installed post-worktree hook: {_POST_SCRIPT}")
    click.echo("Updated ~/.claude/settings.json with PreToolUse + PostToolUse entries.")


def uninstall_hooks() -> None:
    """Remove summon-owned Claude Code hook entries and shell wrapper files.

    Only removes entries it owns — never touches other hooks.
    """
    settings = _read_settings()
    hooks_section: dict[str, Any] = settings.get("hooks", {})
    changed = False

    for hook_type in ("PreToolUse", "PostToolUse"):
        marker = _PRE_MARKER if hook_type == "PreToolUse" else _POST_MARKER
        before = hooks_section.get(hook_type, [])
        after = _remove_hook(before, marker)
        if after != before:
            hooks_section[hook_type] = after
            changed = True

    if changed:
        settings["hooks"] = hooks_section
        _write_settings(settings)
        click.echo("Removed summon entries from ~/.claude/settings.json.")
    else:
        click.echo("No summon entries found in ~/.claude/settings.json.")

    for script in (_PRE_SCRIPT, _POST_SCRIPT):
        if script.exists():
            script.unlink()
            click.echo(f"Removed {script}")
        else:
            click.echo(f"Not found (already removed): {script}")


# ---------------------------------------------------------------------------
# Hooks management helpers
# ---------------------------------------------------------------------------


async def async_show_hooks(ctx: click.Context, project_id: str | None = None) -> None:
    """Print configured lifecycle hooks for a project or globally."""
    raw_hooks: dict[str, list[str]] = {}
    label = "global defaults"
    async with SessionRegistry() as registry:
        if project_id is not None:
            raw_hooks = {
                ht: await registry.get_lifecycle_hooks(ht, project_id=project_id)
                for ht in sorted(VALID_HOOK_TYPES)
            }
            label = f"project {project_id!r}"
        else:
            raw_hooks = {
                ht: await registry.get_lifecycle_hooks(ht) for ht in sorted(VALID_HOOK_TYPES)
            }

    if all(not cmds for cmds in raw_hooks.values()):
        echo(f"No lifecycle hooks configured ({label}).", ctx)
        return

    echo(f"Lifecycle hooks ({label}):", ctx)
    for hook_type, cmds in raw_hooks.items():
        if cmds:
            echo(f"  {hook_type}:", ctx)
            for cmd in cmds:
                echo(f"    - {cmd}", ctx)
        else:
            echo(f"  {hook_type}: (none)", ctx)


_HOOKS_TEMPLATE = """\
{
  "worktree_create": ["ln -sfn $PROJECT_ROOT/hack hack"],
  "project_up": [],
  "project_down": []
}
"""

# Explanatory header prepended when opening $EDITOR (stripped before parsing).
_EDITOR_HEADER = """\
// Lifecycle hooks — shell commands run at each lifecycle point.
// $PROJECT_ROOT is set to the project directory.
// Use "$INCLUDE_GLOBAL" in per-project hooks to include global hooks:
//   "worktree_create": ["$INCLUDE_GLOBAL", "make setup"]
// Delete this comment block before saving. Lines starting with // are stripped.
"""


async def async_set_hooks(hooks_json: str | None = None, *, project_id: str | None = None) -> None:
    """Set lifecycle hooks via JSON string or $EDITOR.

    If *hooks_json* is provided, parse and store it directly.
    If None, open $EDITOR with the current hooks (or a template) for editing.
    """
    if hooks_json is None:
        # Fetch current hooks to pre-populate editor.
        current = _HOOKS_TEMPLATE
        async with SessionRegistry() as registry:
            existing: dict[str, list[str]] = {}
            for ht in sorted(VALID_HOOK_TYPES):
                cmds = await registry.get_lifecycle_hooks(ht, project_id=project_id)
                if cmds:
                    existing[ht] = cmds
            if existing:
                current = json.dumps(existing, indent=2) + "\n"

        edited = click.edit(_EDITOR_HEADER + current, extension=".json")
        if edited is None:
            click.echo("Aborted — no changes saved.")
            return
        # Strip comment lines (// ...) before parsing as JSON.
        hooks_json = "\n".join(
            line for line in edited.splitlines() if not line.lstrip().startswith("//")
        )

    if hooks_json is None:
        raise click.ClickException("No hooks JSON provided.")
    try:
        hooks: dict[str, list[str]] = json.loads(hooks_json)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Invalid JSON: {e}") from e

    if not isinstance(hooks, dict):
        raise click.ClickException("Hooks must be a JSON object (dict).")

    async with SessionRegistry() as registry:
        try:
            await registry.set_lifecycle_hooks(hooks, project_id=project_id)
        except (ValueError, KeyError) as e:
            raise click.ClickException(str(e)) from e

    label = f"project {project_id!r}" if project_id else "global defaults"
    click.echo(f"Lifecycle hooks updated ({label}).")


async def async_clear_hooks(project_id: str | None = None) -> None:
    """Clear lifecycle hooks for a project or globally (sets column to NULL)."""
    async with SessionRegistry() as registry:
        await registry.clear_lifecycle_hooks(project_id=project_id)

    label = f"project {project_id!r}" if project_id else "global defaults"
    click.echo(f"Lifecycle hooks cleared ({label}).")


# ---------------------------------------------------------------------------
# Shell wrapper CLI entry point
# ---------------------------------------------------------------------------


def run_post_worktree_cli() -> None:
    """CLI entry point for 'summon hooks run post-worktree'. Always exits 0."""
    run_post_worktree_hooks(Path.cwd())
