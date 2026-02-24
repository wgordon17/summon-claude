"""Debounced permission handler — batches tool approval requests and posts to Slack."""

# pyright: reportArgumentType=false, reportReturnType=false
# claude_agent_sdk doesn't ship type stubs

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from summon_claude._formatting import get_tool_primary_arg, sanitize_for_mrkdwn
from summon_claude.config import SummonConfig
from summon_claude.thread_router import ThreadRouter

logger = logging.getLogger(__name__)

_AUTO_APPROVE_TOOLS = frozenset(
    [
        "Read",
        "Cat",
        "Grep",
        "Glob",
        "WebSearch",
        "WebFetch",
        "LSP",
        "ListFiles",
        "GetSymbolsOverview",
        "FindSymbol",
        "FindReferencingSymbols",
    ]
)

_PERMISSION_TIMEOUT_S = 300  # 5 minutes


@dataclass
class PendingRequest:
    """A single tool use permission request waiting for user approval."""

    request_id: str
    tool_name: str
    input_data: dict[str, Any]
    context: ToolPermissionContext | None
    result_event: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool = False


@dataclass
class _BatchState:
    """Tracks in-flight permission batches awaiting user resolution."""

    events: dict[str, asyncio.Event] = field(default_factory=dict)
    decisions: dict[str, bool] = field(default_factory=dict)
    channels: dict[str, str] = field(default_factory=dict)


class PermissionHandler:
    """Handles tool permission requests with 500ms debouncing and Slack interactive buttons.

    Safe tools (Read, Grep, Glob, WebSearch, WebFetch) are auto-approved.
    Risky tools (Write, Edit, Bash, etc.) are batched into a single Slack
    message per debounce window and wait for user approval.

    Permission messages are posted with reply_broadcast=True (when in a thread)
    and <!channel> to notify all channel members. The 500ms debounce window
    batches rapid permission requests into a single message, so <!channel>
    fires once per batch — not once per individual tool request.
    """

    def __init__(
        self,
        router: ThreadRouter,
        config: SummonConfig,
    ) -> None:
        self._router = router
        self._config = config
        self._debounce_ms = config.permission_debounce_ms

        # Pending requests waiting for batched approval
        self._pending: dict[str, PendingRequest] = {}
        self._batch_task: asyncio.Task | None = None
        self._batch_lock = asyncio.Lock()

        # Per-batch tracking (events, decisions, channels)
        self._batch = _BatchState()

    async def handle(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext | None,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Main entry point for the can_use_tool callback."""
        # 1. Check SDK suggestions for deny — always honor denials unconditionally
        if context is not None:
            for suggestion in getattr(context, "suggestions", []) or []:
                if getattr(suggestion, "behavior", None) == "deny":
                    logger.info("SDK suggestion: denying %s", tool_name)
                    return PermissionResultDeny(message="Denied by permission rules")

        # 2. Static auto-approve list is the primary gate for allowing tools
        if tool_name in _AUTO_APPROVE_TOOLS:
            logger.debug("Auto-approving tool: %s", tool_name)
            return PermissionResultAllow()

        # 3. Check SDK suggestions for allow — secondary, after static allowlist
        if context is not None:
            for suggestion in getattr(context, "suggestions", []) or []:
                if getattr(suggestion, "behavior", None) == "allow":
                    logger.info("SDK suggestion: approving %s", tool_name)
                    return PermissionResultAllow()
                # behavior == "ask" or None falls through to Slack buttons

        # 4. Request user approval via Slack
        logger.info("Permission required for tool: %s", tool_name)
        return await self._request_approval(tool_name, input_data, context)

    async def _request_approval(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext | None,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Queue a permission request and wait for user approval."""
        request_id = str(uuid.uuid4())
        req = PendingRequest(
            request_id=request_id,
            tool_name=tool_name,
            input_data=input_data,
            context=context,
        )

        async with self._batch_lock:
            self._pending[request_id] = req

            # Start or reset the debounce timer
            if self._batch_task and not self._batch_task.done():
                self._batch_task.cancel()
            self._batch_task = asyncio.create_task(self._debounce_and_post())

        # Wait for this specific request to be resolved
        try:
            await asyncio.wait_for(req.result_event.wait(), timeout=_PERMISSION_TIMEOUT_S)
        except TimeoutError:
            logger.warning("Permission request timed out for tool %s", tool_name)
            await self._post_timeout_message()
            return PermissionResultDeny(message="Permission request timed out (5 minutes)")

        if req.approved:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Denied by user in Slack")

    async def _debounce_and_post(self) -> None:
        """Wait for the debounce window, then post a single batch message."""
        await asyncio.sleep(self._debounce_ms / 1000.0)

        async with self._batch_lock:
            if not self._pending:
                return
            batch = dict(self._pending)
            self._pending.clear()

        batch_id = str(uuid.uuid4())
        batch_event = asyncio.Event()
        self._batch.events[batch_id] = batch_event
        self._batch.channels[batch_id] = self._router.channel_id

        await self._post_approval_message(batch_id, batch)

        # Wait for user response
        try:
            await asyncio.wait_for(batch_event.wait(), timeout=_PERMISSION_TIMEOUT_S)
        except TimeoutError:
            approved = False
        else:
            approved = self._batch.decisions.get(batch_id, False)

        # Resolve all requests in this batch
        for req in batch.values():
            req.approved = approved
            req.result_event.set()

        # Cleanup
        self._batch.events.pop(batch_id, None)
        self._batch.decisions.pop(batch_id, None)
        self._batch.channels.pop(batch_id, None)

    async def _post_approval_message(self, batch_id: str, batch: dict[str, PendingRequest]) -> None:
        """Post the Slack interactive approval message for a batch of requests."""
        requests = list(batch.values())

        if len(requests) == 1:
            req = requests[0]
            summary = _format_request_summary(req)
            header_text = f"Claude wants to run:\n{summary}"
        else:
            summaries = "\n".join(
                f"{i + 1}. {_format_request_summary(r)}" for i, r in enumerate(requests)
            )
            header_text = f"Claude wants to perform {len(requests)} actions:\n{summaries}"

        approve_value = f"approve:{batch_id}"
        deny_value = f"deny:{batch_id}"

        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": header_text},
            },
            {
                "type": "actions",
                "block_id": f"permission_{batch_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "permission_approve",
                        "value": approve_value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "action_id": "permission_deny",
                        "value": deny_value,
                    },
                ],
            },
        ]

        try:
            await self._router.post_permission(
                f"Permission required: {header_text[:100]}",
                blocks,
            )
        except Exception as e:
            logger.error("Failed to post permission message: %s", e)
            # Auto-deny if we can't post
            self._batch.decisions[batch_id] = False
            if batch_id in self._batch.events:
                self._batch.events[batch_id].set()

    async def handle_action(
        self,
        action_id: str,
        value: str,
        user_id: str,
        channel_id: str,
        message_ts: str,
    ) -> None:
        """Handle a Slack interactive button click for permission approval/denial.

        Must be called AFTER ack() (the 3-second deadline is the caller's responsibility).
        """
        # Verify the clicking user is authorized
        if self._config.allowed_user_ids and user_id not in self._config.allowed_user_ids:
            logger.warning("Unauthorized user %s tried to approve/deny permissions", user_id)
            return

        if value.startswith("approve:"):
            batch_id = value[len("approve:") :]
            approved = True
        elif value.startswith("deny:"):
            batch_id = value[len("deny:") :]
            approved = False
        else:
            logger.warning("Unknown permission action value: %r", value)
            return

        # Validate the action came from the correct channel
        expected_channel = self._batch.channels.get(batch_id)
        if expected_channel and channel_id != expected_channel:
            logger.warning("Permission action channel mismatch for batch %s", batch_id)
            return

        self._batch.decisions[batch_id] = approved

        # Update the original message to show the decision
        status_text = ":white_check_mark: Approved" if approved else ":x: Denied"
        blocks = [
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"{status_text} by <@{user_id}>"}],
            }
        ]
        try:
            await self._router.update_message(
                channel_id,
                message_ts,
                f"Permission {status_text} by user",
                blocks=blocks,
            )
        except Exception as e:
            logger.warning("Failed to update permission message: %s", e)

        # Signal the waiting batch
        if batch_id in self._batch.events:
            self._batch.events[batch_id].set()

    async def _post_timeout_message(self) -> None:
        """Post a message indicating permission timed out."""
        try:
            await self._router.post_to_turn_thread(
                ":hourglass: Permission request timed out after 5 minutes. Denied.",
            )
        except Exception as e:
            logger.warning("Failed to post timeout message: %s", e)


def _format_request_summary(req: PendingRequest) -> str:
    """Create a human-readable summary of a permission request."""
    tool = req.tool_name
    data = req.input_data

    arg = get_tool_primary_arg(tool, data)
    if arg:
        safe_arg = sanitize_for_mrkdwn(arg)
        return f"`{tool}`: `{safe_arg}`"

    # Generic fallback
    keys = list(data.keys())[:2]
    params = ", ".join(f"{k}={sanitize_for_mrkdwn(str(data[k]), 40)!r}" for k in keys)
    return f"`{tool}`({params})"
