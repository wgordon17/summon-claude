"""Lifecycle hooks runner for summon-claude sessions.

Lifecycle hooks are shell commands stored in the DB (per-project or global
workflow_defaults) and executed at defined points in the session lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Timeout per hook subprocess in seconds. Module-level for testability.
_HOOK_TIMEOUT_SECONDS: int = 30

# Valid hook type identifiers.  Guard test pins this set.
VALID_HOOK_TYPES: frozenset[str] = frozenset({"worktree_create", "project_up", "project_down"})

# Token that project hooks can include to splice in global hooks at that position.
INCLUDE_GLOBAL_TOKEN: str = "$INCLUDE_GLOBAL"  # noqa: S105


async def run_lifecycle_hooks(
    hook_type: str,
    cwd: Path,
    hooks: list[str],
    project_root: Path | None = None,
) -> list[str]:
    """Run lifecycle hook commands. Returns list of error messages (empty = success).

    Each hook is a shell command string executed with asyncio.create_subprocess_shell.
    Hooks run sequentially. A failing hook does NOT abort remaining hooks.
    """
    if hook_type not in VALID_HOOK_TYPES:
        raise ValueError(
            f"Invalid hook_type {hook_type!r}; must be one of {sorted(VALID_HOOK_TYPES)}"
        )

    if not hooks:
        return []

    hook_env = {**os.environ, "PROJECT_ROOT": str(project_root or cwd)}
    errors: list[str] = []

    for cmd in hooks:
        logger.debug("Running %s hook: %s", hook_type, cmd)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(cwd),
                env=hook_env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_HOOK_TIMEOUT_SECONDS
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()  # reap zombie
                errors.append(f"Hook timed out after {_HOOK_TIMEOUT_SECONDS}s: {cmd}")
                logger.warning("Hook timed out after %ds: %s", _HOOK_TIMEOUT_SECONDS, cmd)
                continue

            if proc.returncode != 0:
                stderr_text = stderr.decode(errors="replace").strip()
                msg = f"Hook failed (exit {proc.returncode}): {cmd}"
                if stderr_text:
                    msg += f"\n  stderr: {stderr_text}"
                errors.append(msg)
                logger.warning("%s", msg)
            else:
                logger.debug("Hook succeeded: %s", cmd)

        except Exception as exc:
            msg = f"Hook error: {cmd}: {exc}"
            errors.append(msg)
            logger.warning("%s", msg)

    return errors


def _get_worktree_project_root(cwd: Path) -> Path | None:
    """Return the project root (main worktree path) by parsing `git worktree list`.

    Returns None if git is unavailable or cwd is not inside a git worktree.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],  # noqa: S607
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return None
        # The first "worktree " line in porcelain output is the main worktree.
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                return Path(line[len("worktree ") :].strip())
    except Exception as exc:
        logger.debug("git worktree list failed: %s", exc)
    return None


def run_post_worktree_hooks(cwd: Path) -> int:
    """CLI entry point for PostToolUse shell wrapper. Returns 0 always.

    Looks up worktree_create hooks for the project containing *cwd* and
    runs them.  Hook failures are logged as warnings but never fatal.
    """
    project_root = _get_worktree_project_root(cwd)
    if project_root is None:
        logger.debug("run_post_worktree_hooks: could not determine project root for %s", cwd)
        return 0

    # Import here to avoid circular imports at module level.
    from summon_claude.sessions.registry import SessionRegistry  # noqa: PLC0415

    async def _run() -> None:
        hooks: list[str] = []
        async with SessionRegistry() as registry:
            hooks = await registry.get_lifecycle_hooks_by_directory("worktree_create", project_root)
        if hooks:
            errors = await run_lifecycle_hooks(
                "worktree_create", cwd, hooks, project_root=project_root
            )
            for err in errors:
                logger.warning("worktree_create hook error: %s", err)

    try:
        asyncio.run(_run())
    except Exception as exc:
        logger.warning("run_post_worktree_hooks failed: %s", exc)

    return 0
