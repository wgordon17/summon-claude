#!/usr/bin/env python
"""Generate docs/reference/prompts.md from source prompt constants.

Replaces content between ``<!-- prompt:xxx -->`` / ``<!-- /prompt:xxx -->``
markers with the verbatim source text.  Prose and descriptions outside
markers are left untouched.

Usage::

    uv run python scripts/generate_prompt_docs.py          # regenerate
    uv run python scripts/generate_prompt_docs.py --check   # exit 1 if stale
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOC_PATH = _REPO_ROOT / "docs" / "reference" / "prompts.md"

# Matches  <!-- prompt:NAME -->\n```text\n...```\n<!-- /prompt:NAME -->
_PROMPT_BLOCK_RE = re.compile(
    r"(<!-- prompt:(\S+) -->\n)```text\n.*?```\n(<!-- /prompt:\2 -->)",
    re.DOTALL,
)


def get_source_prompts() -> dict[str, str]:
    """Return ``{marker_name: prompt_text}`` extracted from source.

    Constants are used directly.  Builder functions are called with all
    optional features enabled and user-facing template variables left as
    placeholder strings (e.g. ``{cwd}``).
    """
    from summon_claude.sessions.prompts import (
        build_global_pm_scan_prompt,
        build_pm_scan_prompt,
        build_scribe_scan_prompt,
    )
    from summon_claude.sessions.prompts.global_pm import _GLOBAL_PM_SYSTEM_PROMPT_APPEND
    from summon_claude.sessions.prompts.pm import (
        _PM_SYSTEM_PROMPT_APPEND,
        _REVIEWER_SYSTEM_PROMPT_TEMPLATE,
    )
    from summon_claude.sessions.prompts.scribe import _SCRIBE_SYSTEM_PROMPT_APPEND
    from summon_claude.sessions.prompts.shared import (
        _CANVAS_PROMPT_SECTION,
        _COMPACT_PROMPT,
        _OVERFLOW_RECOVERY_PROMPT,
        _SCHEDULING_PROMPT_SECTION,
    )

    # Scribe system: resolve *internal* template vars, keep user-facing ones.
    # The google/slack section strings match build_scribe_system_prompt().
    scribe_system = _SCRIBE_SYSTEM_PROMPT_APPEND.replace(
        "{google_section}",
        "Your domain: Gmail, Google Calendar, Google Drive — "
        "watch every inbox, every calendar event, every shared document.\n\n",
    ).replace(
        "{external_slack_section}",
        "Your domain: External Slack channels, DMs, and @mentions — "
        "every message in your monitored workspaces passes through your watch.\n\n",
    )

    return {
        "pm-system": _PM_SYSTEM_PROMPT_APPEND,
        "pm-scan": build_pm_scan_prompt(github_enabled=True),
        "global-pm-system": _GLOBAL_PM_SYSTEM_PROMPT_APPEND,
        "global-pm-scan": build_global_pm_scan_prompt(),
        "scribe-system": scribe_system,
        "scribe-scan": build_scribe_scan_prompt(
            nonce="{nonce}",
            google_enabled=True,
            slack_enabled=True,
            user_mention="{user_mention}",
            importance_keywords="{importance_keywords}",
            quiet_hours="{quiet_hours}",
        ),
        "reviewer-system": _REVIEWER_SYSTEM_PROMPT_TEMPLATE,
        "compact": _COMPACT_PROMPT,
        "overflow-recovery": _OVERFLOW_RECOVERY_PROMPT,
        "canvas": _CANVAS_PROMPT_SECTION,
        "scheduling": _SCHEDULING_PROMPT_SECTION,
    }


def generate(content: str, prompts: dict[str, str]) -> str:
    """Replace prompt blocks in *content* with source text."""

    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        marker = m.group(2)
        if marker not in prompts:
            return m.group(0)  # leave unknown markers unchanged
        text = prompts[marker].strip()
        return f"{m.group(1)}```text\n{text}\n```\n{m.group(3)}"

    return _PROMPT_BLOCK_RE.sub(_replace, content)


def main() -> int:
    check_only = "--check" in sys.argv

    prompts = get_source_prompts()
    content = _DOC_PATH.read_text(encoding="utf-8")
    updated = generate(content, prompts)

    if check_only:
        if content == updated:
            print("prompts.md is up to date")  # noqa: T201
            return 0
        print("prompts.md is stale — run: uv run python scripts/generate_prompt_docs.py")  # noqa: T201
        return 1

    _DOC_PATH.write_text(updated, encoding="utf-8")
    print(f"Updated {_DOC_PATH.relative_to(_REPO_ROOT)}")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
