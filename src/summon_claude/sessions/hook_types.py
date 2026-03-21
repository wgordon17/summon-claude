"""Constants for lifecycle hooks.

Shared between hooks.py and registry.py to avoid circular imports.
"""

from __future__ import annotations

# Valid hook type identifiers. Guard test pins this set.
VALID_HOOK_TYPES: frozenset[str] = frozenset({"worktree_create", "project_up", "project_down"})

# Token that project hooks can include to splice in global hooks at that position.
INCLUDE_GLOBAL_TOKEN: str = "$INCLUDE_GLOBAL"  # noqa: S105
