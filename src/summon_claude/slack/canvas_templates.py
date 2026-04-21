"""Canvas markdown templates for different agent profiles."""

from __future__ import annotations

# Shared sections used across all canvas templates.
_SCHED_JOBS_SECTION = """\

## Scheduled Jobs

_No scheduled jobs._
"""

_TASKS_SECTION = """\

## Tasks

_No tasks tracked._
"""

_WORK_ITEMS_SECTION = """\

## Work Items

_No work items tracked._
"""

AGENT_CANVAS_TEMPLATE = (
    """\
# Session Status

| Field | Value |
|-------|-------|
| Status | Starting... |
| Model | {model} |
| Directory | `{cwd}` |
"""
    + _SCHED_JOBS_SECTION
    + _TASKS_SECTION
    + """
## Current Task

_No task assigned yet._

## Recent Activity

_Session starting..._

## Changed Files

_No files changed yet._

## Notes

_No notes yet._
"""
)

PM_CANVAS_TEMPLATE = (
    """\
# PM Agent — Session Status

| Field | Value |
|-------|-------|
| Status | Starting... |
| Model | {model} |
| Directory | `{cwd}` |
"""
    + _SCHED_JOBS_SECTION
    + _WORK_ITEMS_SECTION
    + """
## Active Tasks

_No tasks tracked yet._

## Decisions Log

_No decisions recorded._

## Blockers

_None._

## Notes

_No notes yet._
"""
)

GLOBAL_PM_CANVAS_TEMPLATE = (
    """\
# Global PM Overview

| Field | Value |
|-------|-------|
| Status | Starting... |
| Model | {model} |
"""
    + _SCHED_JOBS_SECTION
    + _TASKS_SECTION
    + """
## Active Sessions

_No active sessions._

## Task Summary

_No tasks tracked yet._

## Notes

_No notes yet._
"""
)

SCRIBE_CANVAS_TEMPLATE = (
    """\
# Scribe Agent — Session Log

| Field | Value |
|-------|-------|
| Status | Starting... |
| Model | {model} |
| Directory | `{cwd}` |
"""
    + _SCHED_JOBS_SECTION
    + _TASKS_SECTION
    + """
## Session Timeline

_Session starting..._

## Key Decisions

_None recorded._

## Artifacts

_No artifacts captured._
"""
)

BUG_HUNTER_CANVAS_TEMPLATE = (
    """\
# Bug Hunter — Session Status

| Field | Value |
|-------|-------|
| Status | Starting... |
| Model | {model} |
| Directory | `{cwd}` |
"""
    + _SCHED_JOBS_SECTION
    + """
## Findings

| Severity | File | Line | Description | Confidence | Category |
|----------|------|------|-------------|------------|----------|
| _No findings yet_ | | | | | |

## Suppressions

_No suppressions recorded._

## Last Scan

_No scan completed yet._
"""
)

_TEMPLATES: dict[str, str] = {
    "agent": AGENT_CANVAS_TEMPLATE,
    "pm": PM_CANVAS_TEMPLATE,
    "global-pm": GLOBAL_PM_CANVAS_TEMPLATE,
    "scribe": SCRIBE_CANVAS_TEMPLATE,
    "bug_hunter": BUG_HUNTER_CANVAS_TEMPLATE,
}


_JIRA_PM_SECTION = """\

## Jira Issues

| Key | Summary | Status | Assignee | Updated |
|-----|---------|--------|----------|---------|
| _No issues tracked yet_ | | | | |
"""

_JIRA_SCRIBE_SECTION = """\

## Jira Notifications

| Time | Type | Issue | Summary |
|------|------|-------|---------|
| _No notifications yet_ | | | |

**Stats:** 0 mentions, 0 assignments, 0 status changes today
"""


def get_canvas_template(profile: str, jira_enabled: bool = False) -> str:
    """Return the canvas template for the given profile name.

    Falls back to the default agent template for unknown profiles.
    When jira_enabled is True, appends Jira sections for pm and scribe profiles.
    """
    base = _TEMPLATES.get(profile, AGENT_CANVAS_TEMPLATE)
    if not jira_enabled:
        return base
    if profile == "pm":
        return base + _JIRA_PM_SECTION
    if profile == "scribe":
        return base + _JIRA_SCRIBE_SECTION
    return base
