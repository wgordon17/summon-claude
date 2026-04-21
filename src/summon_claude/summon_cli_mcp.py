"""MCP tools — session lifecycle management via SessionRegistry + daemon IPC."""

# pyright: reportArgumentType=false, reportReturnType=false
# claude_agent_sdk doesn't ship type stubs

from __future__ import annotations

import contextlib
import datetime
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from summon_claude.security import validate_agent_output
from summon_claude.sessions.registry import MAX_SPAWN_CHILDREN_PM, MAX_SPAWN_DEPTH, SessionRegistry
from summon_claude.sessions.scheduler import (
    SessionScheduler,
    explain_cron,
    sanitize_for_table,
)
from summon_claude.slack.client import sanitize_for_slack

if TYPE_CHECKING:
    from claude_agent_sdk import SdkMcpTool
    from claude_agent_sdk.types import McpSdkServerConfig

logger = logging.getLogger(__name__)

_SESSION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")
MAX_PROMPT_CHARS = 10_000

_SENSITIVE_FIELDS = frozenset({"pid", "error_message", "authenticated_user_id"})
_MAX_TASKS_PER_SESSION = 100
_MAX_CROSS_SESSION_IDS = 20


def _sanitize_session(session: dict[str, Any]) -> dict[str, Any]:
    """Remove sensitive fields from a session dict before returning to the caller."""
    return {k: v for k, v in session.items() if k not in _SENSITIVE_FIELDS}


def create_summon_cli_mcp_tools(  # noqa: PLR0913, PLR0915
    registry: SessionRegistry,
    session_id: str,
    authenticated_user_id: str,
    channel_id: str,
    cwd: str,
    *,
    session_name: str = "",
    is_pm: bool = False,
    is_global_pm: bool = False,
    is_bug_hunter: bool = False,
    scheduler: SessionScheduler,
    project_id: str | None = None,
    on_task_change: Callable[[], Coroutine[Any, Any, None]] | None = None,
    _generate_spawn_token: Callable[..., Awaitable[Any]] | None = None,
    _ipc_create_session: Callable[..., Awaitable[str]] | None = None,
    _ipc_stop_session: Callable[..., Awaitable[bool]] | None = None,
    _ipc_send_message: Callable[..., Awaitable[dict]] | None = None,
    _ipc_resume_session: Callable[..., Awaitable[dict]] | None = None,
    _ipc_queue_session: Callable[..., int] | None = None,
    _web_client: Any | None = None,
    pm_status_ts: str | None = None,
) -> list[SdkMcpTool]:
    """Create MCP tool instances for session lifecycle and scheduling.

    Args:
        registry: SessionRegistry instance for querying session data.
        session_id: The calling session's own ID (for parent_session_id filtering).
        authenticated_user_id: For spawn token generation and scope guards.
        channel_id: For spawn token's parent_channel_id.
        cwd: Calling session's working directory, default for spawned sessions.
        session_name: Calling session's name (for sender_info attribution).
        is_pm: Whether this is a PM session (gates session_start/stop/log_status).
        is_global_pm: Whether this is the Global PM session (excludes session_start).
        is_bug_hunter: Whether this is a bug hunter session
            (skips session_start/stop/message/resume).
        scheduler: SessionScheduler for cron/task scheduling.
        project_id: Project ID for cross-session task queries (optional).
        on_task_change: Async callback for task mutations (canvas sync).
        _generate_spawn_token: Override for generate_spawn_token (testing).
        _ipc_create_session: Override for daemon IPC create (testing).
        _ipc_stop_session: Override for daemon IPC stop (testing).
        _ipc_send_message: Override for daemon IPC send_message (testing).
        _ipc_resume_session: Override for daemon IPC resume (testing).
        _ipc_queue_session: Override for daemon queue_session (testing).
        _web_client: AsyncWebClient for cross-channel Slack posts (testing).
    """
    if is_global_pm and not is_pm:
        raise ValueError("is_global_pm requires is_pm=True")

    @tool(
        "session_list",
        (
            "List summon-claude sessions. "
            "filter: 'active' (default) for active sessions, "
            "'all' for all sessions including completed/errored, "
            "'mine' for sessions spawned by the calling session."
        ),
        {
            "type": "object",
            "properties": {"filter": {"type": "string"}},
            "required": [],
        },
    )
    async def session_list(args: dict) -> dict:
        filter_type = args.get("filter", "active")
        if filter_type not in ("active", "all", "mine"):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: filter must be 'active', 'all', or 'mine'.",
                    }
                ],
                "is_error": True,
            }
        try:
            if filter_type == "active":
                sessions = await registry.list_active()
            elif filter_type == "all":
                sessions = await registry.list_all()
            else:  # mine
                sessions = await registry.list_children(session_id)

            # Scope guard: only show sessions owned by the same user
            sessions = [
                s for s in sessions if s.get("authenticated_user_id") == authenticated_user_id
            ]
            sanitized = [_sanitize_session(s) for s in sessions]
            if not sanitized:
                return {"content": [{"type": "text", "text": "No sessions found."}]}

            lines = []
            for s in sanitized:
                name = s.get("session_name") or "unnamed"
                sid = s["session_id"]
                status = s.get("status", "?")
                channel = s.get("slack_channel_name") or s.get("slack_channel_id") or "—"
                turns = s.get("total_turns", 0)
                cost = s.get("total_cost_usd", 0.0)
                lines.append(
                    f"[{sid}] {name} ({status}) channel={channel} turns={turns} cost=${cost:.4f}"
                )
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error listing sessions: {e}"}],
                "is_error": True,
            }

    @tool(
        "session_info",
        (
            "Get detailed information about a specific session. "
            "session_id: the full session ID to look up."
        ),
        {"session_id": str},
    )
    async def session_info(args: dict) -> dict:
        target_id = args.get("session_id", "")
        if not target_id:
            return {
                "content": [{"type": "text", "text": "Error: session_id is required."}],
                "is_error": True,
            }
        try:
            session = await registry.get_session(target_id)
            if session is None or session.get("authenticated_user_id") != authenticated_user_id:
                return {
                    "content": [
                        {"type": "text", "text": f"Error: session '{target_id}' not found."}
                    ],
                    "is_error": True,
                }
            sanitized = _sanitize_session(session)
            lines = [f"{k}: {v}" for k, v in sanitized.items()]
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error getting session info: {e}"}],
                "is_error": True,
            }

    @tool(
        "session_start",
        (
            "Start a new summon-claude session. Generates a spawn token and creates "
            "a pre-authenticated session via daemon IPC. "
            "name: session name (required, lowercase alphanumeric + hyphens, max 20 chars). "
            "cwd: working directory (optional, defaults to calling session's cwd). "
            "model: model override (optional). "
            "system_prompt: additional system prompt text appended to session, "
            f"max {MAX_PROMPT_CHARS} chars (optional). "
            "initial_prompt: first message injected into the session after startup, "
            f"max {MAX_PROMPT_CHARS} chars (optional). "
            "bug_hunter_profile: if true, starts a sandboxed bug hunter session "
            "(PM sessions only, requires Matchlock to be installed)."
        ),
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "cwd": {"type": "string"},
                "model": {"type": "string"},
                "system_prompt": {"type": "string", "maxLength": MAX_PROMPT_CHARS},
                "initial_prompt": {"type": "string", "maxLength": MAX_PROMPT_CHARS},
                "bug_hunter_profile": {"type": "boolean"},
            },
            "required": ["name"],
        },
    )
    async def session_start(args: dict) -> dict:  # noqa: PLR0911, PLR0912
        name = args.get("name", "")
        if not _SESSION_NAME_RE.match(name):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Error: invalid name. Must be lowercase alphanumeric + hyphens, "
                            "1-20 chars, starting with alphanumeric."
                        ),
                    }
                ],
                "is_error": True,
            }

        system_prompt_val = args.get("system_prompt")
        if system_prompt_val and len(system_prompt_val) > MAX_PROMPT_CHARS:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Error: system_prompt exceeds {MAX_PROMPT_CHARS} chars "
                            f"({len(system_prompt_val)} provided)."
                        ),
                    }
                ],
                "is_error": True,
            }

        initial_prompt_val = args.get("initial_prompt")
        if initial_prompt_val and len(initial_prompt_val) > MAX_PROMPT_CHARS:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Error: initial_prompt exceeds {MAX_PROMPT_CHARS} chars "
                            f"({len(initial_prompt_val)} provided)."
                        ),
                    }
                ],
                "is_error": True,
            }

        target_cwd = args.get("cwd") or cwd
        if not Path(target_cwd).is_dir():  # noqa: ASYNC240
            return {
                "content": [
                    {"type": "text", "text": f"Error: directory '{target_cwd}' does not exist."}
                ],
                "is_error": True,
            }

        # CWD ancestor constraint: target must be equal to or a descendant
        # of the calling session's CWD.  Resolve symlinks first to prevent
        # escape via crafted symlinks.
        resolved_parent = Path(cwd).resolve()  # noqa: ASYNC240
        resolved_target = Path(target_cwd).resolve()  # noqa: ASYNC240
        if not resolved_target.is_relative_to(resolved_parent):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Error: cwd must be within the calling session's "
                            f"directory ({cwd}). Cannot spawn sessions in "
                            f"parent or unrelated directories."
                        ),
                    }
                ],
                "is_error": True,
            }

        # Enforce spawn depth limit
        try:
            depth = await registry.compute_spawn_depth(session_id)
            if depth >= MAX_SPAWN_DEPTH:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Error: spawn depth limit reached ({depth}/{MAX_SPAWN_DEPTH}). "
                                "Cannot spawn sessions this deep in the hierarchy."
                            ),
                        }
                    ],
                    "is_error": True,
                }
        except Exception as e:
            logger.error("Failed to verify spawn depth: %s", e)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: could not verify spawn depth. Try again.",
                    }
                ],
                "is_error": True,
            }

        model = args.get("model")

        # Auto-propagate project_id from calling session to spawned sessions
        try:
            calling_session = await registry.get_session(session_id)
            parent_project_id = calling_session.get("project_id") if calling_session else None
        except Exception as e:
            logger.error("Failed to fetch calling session: %s", e)
            parent_project_id = None

        from summon_claude.sessions.session import SessionOptions  # noqa: PLC0415

        # bug_hunter_profile: only PM sessions may spawn bug hunters
        bug_hunter_profile_val = bool(args.get("bug_hunter_profile", False))
        if bug_hunter_profile_val and not is_pm:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: bug_hunter_profile=True requires a PM session.",
                    }
                ],
                "is_error": True,
            }

        options = SessionOptions(
            cwd=target_cwd,
            name=name,
            model=model,
            project_id=parent_project_id,
            system_prompt_append=system_prompt_val,
            initial_prompt=initial_prompt_val,
            bug_hunter_profile=bug_hunter_profile_val,
        )

        # Enforce active-child cap before spawning (fail-closed)
        try:
            children = await registry.list_children(session_id, limit=500)
            active = [c for c in children if c.get("status") in ("pending_auth", "active")]
            if len(active) >= MAX_SPAWN_CHILDREN_PM:
                # Queue if this is a PM with a project and a queue callback is available
                if is_pm and parent_project_id and _ipc_queue_session is not None:
                    position = _ipc_queue_session(
                        options,
                        project_id=parent_project_id,
                        pm_session_id=session_id,
                        authenticated_user_id=authenticated_user_id,
                        parent_channel_id=channel_id,
                    )
                    if position == -1:
                        return {
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "Error: session queue is full. "
                                        "Stop or wait for existing sessions to finish."
                                    ),
                                }
                            ],
                            "is_error": True,
                        }
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"Session '{name}' queued (position {position}). "
                                    "It will start automatically when a slot opens."
                                ),
                            }
                        ]
                    }
                active_list = ", ".join(
                    f"{c.get('session_name', 'unnamed')} ({c['session_id']})" for c in active
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Error: active session limit reached "
                                f"({len(active)}/{MAX_SPAWN_CHILDREN_PM}). "
                                f"Stop existing sessions before starting new ones.\n"
                                f"Active sessions: {active_list}"
                            ),
                        }
                    ],
                    "is_error": True,
                }
        except Exception as e:
            logger.error("Failed to verify session limit: %s", e)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: could not verify session limit. Try again.",
                    }
                ],
                "is_error": True,
            }

        try:
            gen_token = _generate_spawn_token
            if gen_token is None:
                from summon_claude.sessions.auth import generate_spawn_token  # noqa: PLC0415

                gen_token = generate_spawn_token

            ipc_create = _ipc_create_session
            if ipc_create is None:
                from summon_claude.cli.daemon_client import (  # noqa: PLC0415
                    create_session_with_spawn_token,
                )

                ipc_create = create_session_with_spawn_token

            spawn_auth = await gen_token(
                registry=registry,
                target_user_id=authenticated_user_id,
                cwd=target_cwd,
                spawn_source="session",
                parent_session_id=session_id,
                parent_channel_id=channel_id,
                parent_cwd=cwd,
            )

            new_session_id = await ipc_create(options, spawn_auth.token)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Session '{name}' created (session_id: {new_session_id}). "
                            "Channel will appear in Slack shortly."
                        ),
                    }
                ]
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error starting session: {e}"}],
                "is_error": True,
            }

    @tool(
        "session_stop",
        (
            "Stop a running session. Cannot stop your own session. "
            "session_id: the session ID to stop."
        ),
        {"session_id": str},
    )
    async def session_stop(args: dict) -> dict:  # noqa: PLR0911
        target_id = args.get("session_id", "")
        if not target_id:
            return {
                "content": [{"type": "text", "text": "Error: session_id is required."}],
                "is_error": True,
            }

        # Cannot stop self
        if target_id == session_id:
            return {
                "content": [{"type": "text", "text": "Error: cannot stop your own session."}],
                "is_error": True,
            }

        try:
            target = await registry.get_session(target_id)

            # Scope guard first: other users' sessions appear as "not found"
            # to prevent leaking existence, status, or ownership.
            if target is None or target.get("authenticated_user_id") != authenticated_user_id:
                return {
                    "content": [
                        {"type": "text", "text": f"Error: session '{target_id}' not found."}
                    ],
                    "is_error": True,
                }

            # Must be active
            if target.get("status") not in ("pending_auth", "active"):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Error: session '{target_id}' is already "
                                f"{target.get('status', 'ended')}."
                            ),
                        }
                    ],
                    "is_error": True,
                }

            ipc_stop = _ipc_stop_session
            if ipc_stop is None:
                from summon_claude.cli.daemon_client import (  # noqa: PLC0415
                    stop_session,
                )

                ipc_stop = stop_session

            found = await ipc_stop(target_id)
            if not found:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Warning: session '{target_id}' was not found in the daemon. "
                                "It may have already stopped."
                            ),
                        }
                    ],
                }

            name = target.get("session_name") or target_id[:8]
            return {"content": [{"type": "text", "text": f"Stopped session '{name}'."}]}
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error stopping session: {e}"}],
                "is_error": True,
            }

    @tool(
        "session_log_status",
        (
            "Log a status update to the session registry audit trail. "
            "Use this to record current project status, active tasks, and blockers. "
            "Note: this does not post to Slack — use the summon-slack MCP post tool for that. "
            "status: one of 'active', 'idle', 'blocked', or 'error'. "
            "summary: brief status summary (required, max 500 chars). "
            "details: optional structured details (markdown, max 2000 chars)."
        ),
        {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "summary": {"type": "string"},
                "details": {"type": "string"},
            },
            "required": ["status", "summary"],
        },
    )
    async def session_log_status(args: dict) -> dict:
        valid_statuses = {"active", "idle", "blocked", "error"}
        status = args.get("status", "active")
        if status not in valid_statuses:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Error: status must be one of {sorted(valid_statuses)}.",
                    }
                ],
                "is_error": True,
            }
        raw_summary = (args.get("summary") or "").strip()
        summary = raw_summary[:500]
        if not summary:
            return {
                "content": [{"type": "text", "text": "Error: summary is required."}],
                "is_error": True,
            }
        raw_details = (args.get("details") or "").strip()
        details = raw_details[:2000]

        truncated_parts: list[str] = []
        if len(raw_summary) > 500:
            truncated_parts.append(f"summary truncated from {len(raw_summary)} to 500 chars")
        if len(raw_details) > 2000:
            truncated_parts.append(f"details truncated from {len(raw_details)} to 2000 chars")

        try:
            await registry.log_event(
                "pm_status_update",
                session_id=session_id,
                user_id=authenticated_user_id,
                details={"status": status, "summary": summary, "details": details},
            )
            msg = f"Status update recorded: [{status}] {summary}."
            if truncated_parts:
                msg += f" (Warning: {'; '.join(truncated_parts)})"
            return {
                "content": [
                    {
                        "type": "text",
                        "text": msg,
                    }
                ]
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error updating status: {e}"}],
                "is_error": True,
            }

    @tool(
        "session_message",
        (
            "Send a message to a running session. The message is injected into "
            "the session's processing queue and processed as a new turn. "
            "Also posts the message to the target session's Slack channel for "
            "observability, with source attribution. "
            "session_id: the target session's ID. "
            "text: the message text to send."
        ),
        {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["session_id", "text"],
        },
    )
    async def session_message(args: dict) -> dict:  # noqa: PLR0911
        target_id = args.get("session_id", "")
        text = args.get("text", "")

        if not target_id:
            return {
                "content": [{"type": "text", "text": "Error: session_id is required."}],
                "is_error": True,
            }
        if not text:
            return {
                "content": [{"type": "text", "text": "Error: text is required."}],
                "is_error": True,
            }
        if target_id == session_id:
            return {
                "content": [
                    {"type": "text", "text": "Error: cannot send a message to your own session."}
                ],
                "is_error": True,
            }
        if len(text) > MAX_PROMPT_CHARS:
            text = text[:MAX_PROMPT_CHARS]

        try:
            target = await registry.get_session(target_id)

            # Scope guard: session must exist and belong to same user
            if target is None or target.get("authenticated_user_id") != authenticated_user_id:
                return {
                    "content": [
                        {"type": "text", "text": f"Error: session '{target_id}' not found."}
                    ],
                    "is_error": True,
                }

            # Parent-child scope guard: caller must be the parent
            # Global PM is exempt — it supervises all sessions without spawning them
            if not is_global_pm and target.get("parent_session_id") != session_id:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Error: can only message sessions you spawned.",
                        }
                    ],
                    "is_error": True,
                }

            # Target must be active
            if target.get("status") != "active":
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Error: session '{target_id}' is "
                                f"{target.get('status', 'not active')}. "
                                "Can only message active sessions."
                            ),
                        }
                    ],
                    "is_error": True,
                }

            # Send via daemon IPC
            ipc_send = _ipc_send_message
            if ipc_send is None:
                from summon_claude.cli.daemon_client import (  # noqa: PLC0415
                    send_message_to_session,
                )

                ipc_send = send_message_to_session

            sender_info = f"{session_name} (#{channel_id})" if session_name else channel_id
            result = await ipc_send(
                session_id=target_id,
                text=text,
                sender_info=sender_info,
            )

            # Observability: post to target's Slack channel (best-effort)
            target_channel_id = result.get("channel_id") or target.get("slack_channel_id")
            if target_channel_id and _web_client:
                safe_text = sanitize_for_slack(text)
                safe_sender = sanitize_for_slack(sender_info)
                attribution = f"_Message from {safe_sender}:_\n{safe_text}"
                attribution, sec_warnings = validate_agent_output(attribution)
                for w in sec_warnings:
                    logger.warning("session_message output validation: %s", w)
                try:
                    await _web_client.chat_postMessage(channel=target_channel_id, text=attribution)
                except Exception:
                    logger.warning("Failed to post observability message to %s", target_channel_id)

            target_name = target.get("session_name") or target_id[:8]
            target_channel_name = target.get("slack_channel_name") or target_channel_id or "?"
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Message sent to session '{target_name}' ({target_id}). "
                            f"Channel: #{target_channel_name}"
                        ),
                    }
                ]
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error sending message: {e}"}],
                "is_error": True,
            }

    @tool(
        "session_resume",
        (
            "Resume a completed or errored session. Creates a new Summon "
            "session connected to the same Slack channel with Claude SDK "
            "transcript continuity. The channel is bound to one Claude "
            "session chain — all resumes continue the same conversation. "
            "session_id: the stopped session's ID. "
            "model: optional model override."
        ),
        {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "model": {"type": "string"},
            },
            "required": ["session_id"],
        },
    )
    async def session_resume(args: dict) -> dict:  # noqa: PLR0911
        target_id = args.get("session_id", "")
        if not target_id:
            return {
                "content": [{"type": "text", "text": "Error: session_id is required."}],
                "is_error": True,
            }

        try:
            target = await registry.get_session(target_id)

            # Scope guard: session must exist and belong to same user
            if target is None or target.get("authenticated_user_id") != authenticated_user_id:
                return {
                    "content": [
                        {"type": "text", "text": f"Error: session '{target_id}' not found."}
                    ],
                    "is_error": True,
                }

            # Parent-child scope guard
            if target.get("parent_session_id") != session_id:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Error: can only resume sessions you spawned.",
                        }
                    ],
                    "is_error": True,
                }

            # Target must be completed or errored
            if target.get("status") not in ("completed", "errored"):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Error: session '{target_id}' is "
                                f"{target.get('status', 'unknown')}. "
                                "Can only resume completed or errored sessions."
                            ),
                        }
                    ],
                    "is_error": True,
                }

            if not target.get("slack_channel_id"):
                return {
                    "content": [{"type": "text", "text": "Error: session has no Slack channel."}],
                    "is_error": True,
                }

            ipc_resume = _ipc_resume_session
            if ipc_resume is None:
                from summon_claude.cli.daemon_client import (  # noqa: PLC0415
                    resume_session as _resume,
                )

                ipc_resume = _resume

            model = args.get("model")
            result = await ipc_resume(session_id=target_id, model=model)

            new_sid = result.get("session_id", "?")
            target_name = target.get("session_name") or target_id[:8]
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Session '{target_name}' resumed (new session: {new_sid}). "
                            f"Channel: <#{target.get('slack_channel_id')}>"
                        ),
                    }
                ]
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error resuming session: {e}"}],
                "is_error": True,
            }

    # --- Cron tools (all sessions) ---

    @tool(
        "CronCreate",
        (
            "Schedule a prompt to be enqueued on a recurring or one-shot basis. "
            "Uses standard 5-field cron: minute hour day-of-month month day-of-week. "
            "Example: '*/5 * * * *' = every 5 minutes. "
            "Returns a job ID for use with CronDelete."
        ),
        {
            "type": "object",
            "properties": {
                "cron": {"type": "string"},
                "prompt": {"type": "string"},
                "recurring": {"type": "boolean"},
            },
            "required": ["cron", "prompt"],
        },
    )
    async def cron_create(args: dict) -> dict:
        try:
            cron_expr = args.get("cron", "")
            prompt = args.get("prompt", "")
            recurring = args.get("recurring", True)
            job = await scheduler.create(cron_expr, prompt, recurring=recurring, internal=False)
            explain, _ = explain_cron(job.cron_expr)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Created job {job.id} ({explain}). "
                        f"Recurring: {job.recurring}. "
                        f"Use CronDelete with id '{job.id}' to cancel.",
                    }
                ]
            }
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}

    @tool(
        "CronDelete",
        "Cancel a scheduled job by ID.",
        {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    )
    async def cron_delete(args: dict) -> dict:
        try:
            job_id = args.get("id", "")
            result = await scheduler.delete(job_id)
            if not result:
                return {
                    "content": [{"type": "text", "text": f"Job '{job_id}' not found."}],
                    "is_error": True,
                }
            return {"content": [{"type": "text", "text": f"Job '{job_id}' cancelled."}]}
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}

    @tool(
        "CronList",
        "List all scheduled jobs in this session.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def cron_list(args: dict) -> dict:  # noqa: ARG001
        jobs = scheduler.list_jobs()
        if not jobs:
            return {"content": [{"type": "text", "text": "No scheduled jobs."}]}
        lines = [
            "| ID | Schedule | Prompt | Type | Next Fire | Recurring |",
            "|-----|----------|--------|------|-----------|-----------|",
        ]
        for j in jobs:
            explain, next_fire = explain_cron(j.cron_expr)
            job_type = "System" if j.internal else "Agent"
            prompt_short = sanitize_for_table(j.prompt, 80)
            lines.append(
                f"| {j.id} | {explain} | {prompt_short} "
                f"| {job_type} | {next_fire} | {j.recurring} |"
            )
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    # --- Task tools (all sessions) ---

    @tool(
        "TaskCreate",
        (
            "Create a task to track work items. Tasks persist across context compaction "
            "and are visible in the channel canvas. Returns the task ID. "
            "priority: 'high' | 'medium' | 'low'."
        ),
        {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "priority": {"type": "string"},
            },
            "required": ["content"],
        },
    )
    async def task_create(args: dict) -> dict:
        try:
            content = args.get("content", "")
            if not content or not content.strip():
                return {
                    "content": [{"type": "text", "text": "Task content cannot be empty."}],
                    "is_error": True,
                }
            priority = args.get("priority", "medium")
            task_id = secrets.token_hex(8)
            created = await registry.create_task(
                session_id, task_id, content, priority, max_active=_MAX_TASKS_PER_SESSION
            )
            if not created:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Maximum of {_MAX_TASKS_PER_SESSION} active tasks "
                            "per session reached. Mark completed tasks with "
                            "TaskUpdate before creating new ones.",
                        }
                    ],
                    "is_error": True,
                }
            if on_task_change:
                await on_task_change()
            return {"content": [{"type": "text", "text": f"Created task {task_id}."}]}
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}

    @tool(
        "TaskUpdate",
        (
            "Update a task's status, content, or priority. "
            "status: 'pending' | 'in_progress' | 'completed'. "
            "priority: 'high' | 'medium' | 'low'. "
            "All fields except id are optional — only update provided fields."
        ),
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string"},
                "content": {"type": "string"},
                "priority": {"type": "string"},
            },
            "required": ["id"],
        },
    )
    async def task_update(args: dict) -> dict:
        try:
            task_id = args.get("id", "")
            kwargs: dict[str, str] = {}
            if args.get("status"):
                kwargs["status"] = args["status"]
            if "content" in args:
                val = args["content"]
                if not val or not val.strip():
                    return {
                        "content": [{"type": "text", "text": "Task content cannot be empty."}],
                        "is_error": True,
                    }
                kwargs["content"] = val
            if args.get("priority"):
                kwargs["priority"] = args["priority"]
            # session_id is closure-captured — NOT overridable by agent input
            result = await registry.update_task(session_id, task_id, **kwargs)
            if not result:
                return {
                    "content": [{"type": "text", "text": f"Task '{task_id}' not found."}],
                    "is_error": True,
                }
            if on_task_change:
                await on_task_change()
            return {"content": [{"type": "text", "text": f"Task '{task_id}' updated."}]}
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}

    @tool(
        "TaskList",
        (
            "List all tasks in this session. Optionally filter by status. "
            "PM sessions: pass session_ids (comma-separated) to query child session tasks."
        ),
        {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "session_ids": {"type": "string"},
            },
            "required": [],
        },
    )
    async def task_list(args: dict) -> dict:  # noqa: PLR0911
        try:
            status = args.get("status", "")
            session_ids = args.get("session_ids", "")
            if session_ids:
                if not is_pm:
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": "Cross-session task queries are PM-only.",
                            }
                        ],
                        "is_error": True,
                    }
                ids = [s.strip() for s in session_ids.split(",") if s.strip()]
                if len(ids) > _MAX_CROSS_SESSION_IDS:
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": f"Too many session IDs (max {_MAX_CROSS_SESSION_IDS}).",
                            }
                        ],
                        "is_error": True,
                    }
                result = await registry.get_tasks_for_sessions(
                    ids, authenticated_user_id, project_id
                )
                if not result:
                    return {
                        "content": [
                            {"type": "text", "text": "No tasks found for specified sessions."}
                        ]
                    }
                lines: list[str] = []
                for sid, tasks in result.items():
                    lines.append(f"\n**Session {sid}:**")
                    for t in tasks:
                        lines.append(f"  - [{t['status']}] {t['content'][:100]} (id: {t['id']})")
                return {"content": [{"type": "text", "text": "\n".join(lines)}]}

            # Own-session query
            if status and status not in SessionRegistry._VALID_TASK_STATUSES:  # noqa: SLF001
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Invalid status '{status}'. "
                            f"Use: {', '.join(sorted(SessionRegistry._VALID_TASK_STATUSES))}.",  # noqa: SLF001
                        }
                    ],
                    "is_error": True,
                }
            filter_status = status if status else None
            tasks = await registry.list_tasks(session_id, status=filter_status)
            if not tasks:
                msg = (
                    "No tasks." if not filter_status else f"No tasks with status '{filter_status}'."
                )
                return {"content": [{"type": "text", "text": msg}]}
            lines_own = [
                "| ID | Status | Priority | Task | Updated |",
                "|-----|--------|----------|------|---------|",
            ]
            for t in tasks:
                content_short = sanitize_for_table(t["content"], 80)
                lines_own.append(
                    f"| {t['id']} | {t['status']} | {t['priority']} "
                    f"| {content_short} | {t['updated_at'][:16]} |"
                )
            return {"content": [{"type": "text", "text": "\n".join(lines_own)}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}

    _pm_status_tool: SdkMcpTool | None = None
    if is_pm and pm_status_ts and _web_client:

        @tool(
            "session_status_update",
            (
                "Update the pinned status message in this PM channel. "
                "Use this to keep your status message current with active session information. "
                "summary: Required brief status (max 500 chars). "
                "details: Optional detailed breakdown (max 2000 chars)."
            ),
            {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "maxLength": 500},
                    "details": {"type": "string", "maxLength": 2000},
                },
                "required": ["summary"],
            },
        )
        async def session_status_update(args: dict) -> dict:
            summary = args.get("summary", "").strip()
            if not summary:
                return {
                    "content": [{"type": "text", "text": "Error: summary is required"}],
                    "is_error": True,
                }
            summary = summary[:500]
            details = args.get("details", "").strip()[:2000]

            summary = sanitize_for_slack(summary)
            details = sanitize_for_slack(details)

            now = datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%d %I:%M %p UTC")
            text = f"*Project Manager Status*\n---\n{summary}"
            if details:
                text += f"\n\n{details}"
            text += f"\n\n_Last updated: {now}_"
            text, sec_warnings = validate_agent_output(text)
            for w in sec_warnings:
                logger.warning("session_status_update output validation: %s", w)

            try:
                await _web_client.chat_update(
                    channel=channel_id,
                    ts=pm_status_ts,
                    text=text,
                )
                with contextlib.suppress(Exception):
                    await registry.log_event(
                        "pm_pinned_status_update",
                        session_id=session_id,
                        user_id=authenticated_user_id,
                        details={"summary_len": len(summary)},
                    )
                return {
                    "content": [
                        {"type": "text", "text": f"Status message updated: {summary[:100]}"}
                    ]
                }
            except Exception as e:
                return {
                    "content": [{"type": "text", "text": f"Error updating status: {e}"}],
                    "is_error": True,
                }

        _pm_status_tool = session_status_update

    _wf_tool: SdkMcpTool | None = None
    if is_global_pm:

        @tool(
            "get_workflow_instructions",
            (
                "Retrieve workflow instructions for a project or the global defaults. "
                "project: project name or project_id to look up (optional, accepts either). "
                "If omitted, returns global default instructions. "
                "If provided, returns effective instructions for that project "
                "(project-specific override if set, otherwise global defaults)."
            ),
            {
                "type": "object",
                "properties": {"project": {"type": "string"}},
                "required": [],
            },
        )
        async def get_workflow_instructions(args: dict) -> dict:
            project_ref = (args.get("project") or "").strip() or None
            try:
                if project_ref:
                    project = await registry.get_project(project_ref)
                    if not project:
                        return {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Error: project '{project_ref}' not found.",
                                }
                            ],
                            "is_error": True,
                        }
                    instructions = await registry.get_effective_workflow(project["project_id"])
                    source = (
                        "project-specific"
                        if project.get("workflow_instructions") is not None
                        else "global default"
                    )
                else:
                    instructions = await registry.get_workflow_defaults()
                    source = "global default"

                if not instructions:
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": "No workflow instructions configured.",
                            }
                        ]
                    }
                # Workflow instructions are trusted operator config (set via
                # `summon project workflow set`, requires CLI access).  Not wrapped
                # with mark_untrusted() because the GPM must act on them as
                # authoritative rules.  validate_agent_output strips exfiltration
                # vectors and redacts secrets as defense-in-depth.
                marked, sec_warnings = validate_agent_output(instructions)
                for w in sec_warnings:
                    logger.warning("get_workflow_instructions output validation: %s", w)
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"[Source: {source}]\n\n{marked}",
                        }
                    ]
                }
            except Exception as e:
                logger.warning("get_workflow_instructions failed: %s", e)
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Error retrieving workflow instructions: {e}",
                        }
                    ],
                    "is_error": True,
                }

        _wf_tool = get_workflow_instructions

    tools: list[SdkMcpTool] = [
        session_list,
        session_info,
        cron_create,
        cron_delete,
        cron_list,
        task_create,
        task_update,
        task_list,
    ]
    if is_bug_hunter:
        # Bug hunter: remove session lifecycle tools — it reads code, not manages sessions
        _bug_hunter_skip = {"session_start", "session_stop", "session_message", "session_resume"}
        return [t for t in tools if getattr(t, "name", None) not in _bug_hunter_skip]
    if is_pm:
        pm_tools: list[SdkMcpTool] = [
            session_stop,
            session_log_status,
            session_resume,
        ]
        # GPM: no session_start (oversight, not spawner)
        if not is_global_pm:
            pm_tools.insert(0, session_start)
        pm_tools.append(session_message)
        tools.extend(pm_tools)
        if _pm_status_tool is not None:
            tools.append(_pm_status_tool)
        # _wf_tool is set iff is_global_pm (is_global_pm → is_pm, so append here)
        if _wf_tool is not None:
            tools.append(_wf_tool)
    return tools


def create_summon_cli_mcp_server(  # noqa: PLR0913
    registry: SessionRegistry,
    session_id: str,
    authenticated_user_id: str,
    channel_id: str,
    cwd: str,
    *,
    session_name: str = "",
    web_client: Any | None = None,
    is_pm: bool = False,
    is_global_pm: bool = False,
    scheduler: SessionScheduler,
    project_id: str | None = None,
    on_task_change: Callable[[], Coroutine[Any, Any, None]] | None = None,
    pm_status_ts: str | None = None,
    ipc_queue: Callable[..., int] | None = None,
) -> McpSdkServerConfig:
    """Create an MCP server with session lifecycle + scheduling tools."""
    tools = create_summon_cli_mcp_tools(
        registry,
        session_id,
        authenticated_user_id,
        channel_id,
        cwd,
        session_name=session_name,
        is_pm=is_pm,
        is_global_pm=is_global_pm,
        scheduler=scheduler,
        project_id=project_id,
        on_task_change=on_task_change,
        _ipc_queue_session=ipc_queue,
        _web_client=web_client,
        pm_status_ts=pm_status_ts,
    )
    return create_sdk_mcp_server(name="summon-cli", version="1.0.0", tools=tools)
