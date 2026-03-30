"""Shared prompt constants used across all agent types."""

from __future__ import annotations

# Prepended to every headless agent system prompt (PM, Scribe, GPM, child sessions).
_HEADLESS_BOILERPLATE = (
    "You are running headlessly via summon-claude, bridged to a private Slack channel. "
    "There is no terminal, no visible desktop, and no interactive UI. "
    "The user interacts through Slack messages — all your replies, tool use, "
    "and thinking are captured and routed to Slack automatically. "
    "UI-based tools (non-headless browsers, GUI editors, desktop apps) "
    "will not be visible to the user. "
    "Use standard markdown formatting "
    "(e.g. **bold**, *italic*, [text](url), ```code```). "
    "Your output will be automatically converted for Slack display. "
    "The user can use !commands (e.g. !help, !status, !stop, !end) "
    "for session control.\n\n"
    "Permission requests: some tool calls require user approval via Slack. "
    "If the user does not respond within 10 minutes, the request times out "
    "and appears as a denial. A denial does not mean the action is forbidden — "
    "it may simply mean the user was away. Consider retrying or trying an "
    "alternative approach."
)

# Appended to sessions with a canvas attached.
_CANVAS_PROMPT_SECTION = (
    "\n\nCanvas: a persistent markdown document is visible in the channel's "
    "Canvas tab. Use it to track work across the session. Tools: summon_canvas_read "
    "(read full canvas), summon_canvas_update_section (update one section by heading — "
    "preferred), summon_canvas_write (replace all content — use sparingly). "
    "Update these sections as you work: "
    "'Current Task' when starting or completing a task; "
    "'Recent Activity' after significant actions; "
    "'Notes' for key decisions, blockers, and discoveries. "
    "Do not update the '# Session Status' heading (it spans the entire document). "
    "Always prefer summon_canvas_update_section over summon_canvas_write."
)

# Appended to sessions with scheduling and task tracking capabilities.
_SCHEDULING_PROMPT_SECTION = (
    "\n\nScheduling & Tasks: you have scheduling and task tracking tools. "
    "CronCreate schedules recurring or one-shot prompts (5-field cron syntax). "
    "CronDelete cancels a job by ID. CronList shows all jobs (including system jobs). "
    "TaskCreate tracks work items with priority (high/medium/low). "
    "TaskUpdate changes status (pending/in_progress/completed) or content. "
    "TaskList shows all tasks, optionally filtered by status. "
    "Scheduled jobs and tasks auto-sync to the channel canvas. "
    "System jobs (scan timers) are visible but cannot be deleted. "
    "Mark tasks as 'completed' via TaskUpdate when done — completed tasks "
    "stay visible (strikethrough) but keep the list manageable. "
    "If context compaction occurs, you will be prompted to re-create any lost scheduled jobs."
)

# Maximum characters for a compaction summary injected into the system prompt.
_MAX_COMPACT_SUMMARY_CHARS = 50_000

_COMPACT_PROMPT = (
    "Your task is to create a detailed summary of our conversation so far. "
    "This summary will REPLACE the current conversation history — it is the "
    "sole record of what happened and must enable seamless continuation.\n\n"
    "Before writing your summary, plan in <analysis> tags "
    "(private scratchpad — walk through chronologically, note what "
    "belongs in each section, flag anything you might otherwise forget).\n\n"
    "Then write your summary in <summary> tags with these MANDATORY sections:\n\n"
    "## Task Overview\n"
    "Core request, success criteria, clarifications, constraints.\n\n"
    "## Current State\n"
    "What has been accomplished. What is in progress. What remains.\n\n"
    "## Files & Artifacts\n"
    "Exact file paths read, created, or modified — include line numbers where "
    "relevant. Preserve exact error messages, command outputs, and code "
    "references VERBATIM. Do NOT paraphrase file paths or error text.\n\n"
    "## Key Decisions\n"
    "Technical decisions made and their rationale. User corrections or preferences.\n\n"
    "## Errors & Resolutions\n"
    "Issues encountered and how they were resolved. Failed approaches to avoid.\n\n"
    "## Next Steps\n"
    "Specific actions needed, in priority order. Blockers and open questions.\n\n"
    "## Context to Preserve\n"
    "User preferences, domain details, promises made, Slack thread references, "
    "any important context about the user's goals or working style.\n\n"
    "Be comprehensive but concise. Preserve exact identifiers "
    "(file paths, function names, error messages) — paraphrasing destroys "
    "navigability. This summary must fit in a system prompt."
)

_COMPACT_SUMMARY_PREFIX = (
    "\n\n## Session Context (Compacted)\n"
    "This session was compacted to free context space. The summary below "
    "preserves key context from the previous conversation. Continue from "
    "where you left off without re-asking answered questions.\n\n"
)

_OVERFLOW_RECOVERY_PROMPT = (
    "\n\n## Context Recovery Required\n"
    "This session was restarted because the previous context was too full "
    "to summarize. Your conversation history has been cleared.\n\n"
    "To recover context, use the `slack_read_history` MCP tool to read the "
    "channel's message history. Use `slack_fetch_thread` to read specific "
    "thread conversations.\n\n"
    "After reading the history:\n"
    "1. Identify what was being worked on\n"
    "2. Note any decisions, file changes, or errors mentioned\n"
    "3. Resume work from where the previous session left off\n"
    "4. Confirm with the user what you have recovered before proceeding\n\n"
    "The user is aware the session was restarted and expects you to "
    "recover context from the channel history."
)
