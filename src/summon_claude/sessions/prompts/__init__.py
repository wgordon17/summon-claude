"""Agent prompt constants and builder functions.

Public API — builder functions and helpers::

    from summon_claude.sessions.prompts import build_pm_system_prompt

Constants can be imported directly from submodules when needed::

    from summon_claude.sessions.prompts.shared import _COMPACT_PROMPT
"""

from summon_claude.sessions.prompts.global_pm import (
    build_global_pm_scan_prompt,
    build_global_pm_system_prompt,
)
from summon_claude.sessions.prompts.pm import (
    build_pm_scan_prompt,
    build_pm_system_prompt,
    format_pm_topic,
)
from summon_claude.sessions.prompts.scribe import (
    build_scribe_scan_prompt,
    build_scribe_system_prompt,
)

__all__ = [
    "build_global_pm_scan_prompt",
    "build_global_pm_system_prompt",
    "build_pm_scan_prompt",
    "build_pm_system_prompt",
    "build_scribe_scan_prompt",
    "build_scribe_system_prompt",
    "format_pm_topic",
]
