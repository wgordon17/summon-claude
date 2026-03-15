"""MCP tools — session lifecycle management via SessionRegistry + daemon IPC."""

# pyright: reportArgumentType=false, reportReturnType=false
# claude_agent_sdk doesn't ship type stubs

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from summon_claude.sessions.registry import SessionRegistry

if TYPE_CHECKING:
    from claude_agent_sdk import SdkMcpTool
    from claude_agent_sdk.types import McpSdkServerConfig

logger = logging.getLogger(__name__)

_SESSION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")

_SENSITIVE_FIELDS = frozenset({"pid", "error_message", "authenticated_user_id"})


def _sanitize_session(session: dict[str, Any]) -> dict[str, Any]:
    """Remove sensitive fields from a session dict before returning to the caller."""
    return {k: v for k, v in session.items() if k not in _SENSITIVE_FIELDS}


def create_summon_cli_mcp_tools(  # noqa: PLR0915
    registry: SessionRegistry,
    session_id: str,
    authenticated_user_id: str,
    channel_id: str,
    cwd: str,
    *,
    _generate_spawn_token: Callable[..., Awaitable[Any]] | None = None,
    _ipc_create_session: Callable[..., Awaitable[str]] | None = None,
    _ipc_stop_session: Callable[..., Awaitable[bool]] | None = None,
) -> list[SdkMcpTool]:
    """Create MCP tool instances for session lifecycle management.

    Args:
        registry: SessionRegistry instance for querying session data.
        session_id: The calling session's own ID (for parent_session_id filtering).
        authenticated_user_id: For spawn token generation and scope guards.
        channel_id: For spawn token's parent_channel_id.
        cwd: Calling session's working directory, default for spawned sessions.
        _generate_spawn_token: Override for generate_spawn_token (testing).
        _ipc_create_session: Override for daemon IPC create (testing).
        _ipc_stop_session: Override for daemon IPC stop (testing).
    """

    @tool(
        "session_list",
        (
            "List summon-claude sessions. "
            "filter: 'active' (default) for active sessions, "
            "'all' for all sessions including completed/errored, "
            "'mine' for sessions spawned by the calling session."
        ),
        {"filter": str},
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
            if session is None:
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
            "model: model override (optional)."
        ),
        {"name": str, "cwd": str, "model": str},
    )
    async def session_start(args: dict) -> dict:
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

        model = args.get("model")

        try:
            from summon_claude.sessions.session import SessionOptions  # noqa: PLC0415

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

            options = SessionOptions(
                cwd=target_cwd,
                name=name,
                model=model,
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
            "Only sessions owned by the same user can be stopped. "
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
            if target is None:
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

            # Scope guard: same user
            if target.get("authenticated_user_id") != authenticated_user_id:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Error: cannot stop a session owned by a different user.",
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

    return [session_list, session_info, session_start, session_stop]


def create_summon_cli_mcp_server(
    registry: SessionRegistry,
    session_id: str,
    authenticated_user_id: str,
    channel_id: str,
    cwd: str,
) -> McpSdkServerConfig:
    """Create an MCP server with session lifecycle tools."""
    tools = create_summon_cli_mcp_tools(
        registry, session_id, authenticated_user_id, channel_id, cwd
    )
    return create_sdk_mcp_server(name="summon-cli", version="1.0.0", tools=tools)
