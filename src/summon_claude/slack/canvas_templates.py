"""Canvas markdown templates for different agent profiles."""

from __future__ import annotations

AGENT_CANVAS_TEMPLATE = """\
# Session Status

| Field | Value |
|-------|-------|
| Status | Starting... |
| Model | {model} |
| Directory | `{cwd}` |

## Current Task

_No task assigned yet._

## Recent Activity

_Session starting..._

## Changed Files

_No files changed yet._

## Notes

_No notes yet._
"""

PM_CANVAS_TEMPLATE = """\
# PM Agent — Session Status

| Field | Value |
|-------|-------|
| Status | Starting... |
| Model | {model} |
| Directory | `{cwd}` |

## Active Tasks

_No tasks tracked yet._

## Decisions Log

_No decisions recorded._

## Blockers

_None._

## Notes

_No notes yet._
"""

GLOBAL_PM_CANVAS_TEMPLATE = """\
# Global PM Overview

| Field | Value |
|-------|-------|
| Status | Starting... |
| Model | {model} |

## Active Sessions

_No active sessions._

## Task Summary

_No tasks tracked yet._

## Notes

_No notes yet._
"""

SCRIBE_CANVAS_TEMPLATE = """\
# Scribe Agent — Session Log

| Field | Value |
|-------|-------|
| Status | Starting... |
| Model | {model} |
| Directory | `{cwd}` |

## Session Timeline

_Session starting..._

## Key Decisions

_None recorded._

## Artifacts

_No artifacts captured._
"""

_TEMPLATES: dict[str, str] = {
    "agent": AGENT_CANVAS_TEMPLATE,
    "pm": PM_CANVAS_TEMPLATE,
    "global-pm": GLOBAL_PM_CANVAS_TEMPLATE,
    "scribe": SCRIBE_CANVAS_TEMPLATE,
}


def get_canvas_template(profile: str) -> str:
    """Return the canvas template for the given profile name.

    Falls back to the default agent template for unknown profiles.
    """
    return _TEMPLATES.get(profile, AGENT_CANVAS_TEMPLATE)
