"""Debounced permission handler — batches tool approval requests and posts to Slack."""

# pyright: reportArgumentType=false, reportReturnType=false
# claude_agent_sdk doesn't ship type stubs

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from summon_claude.config import SummonConfig
from summon_claude.sessions.response import get_tool_primary_arg
from summon_claude.slack.client import sanitize_for_mrkdwn
from summon_claude.slack.router import ThreadRouter

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


async def _dismiss_ephemeral(response_url: str) -> None:
    """Delete an ephemeral Slack message via its response_url."""
    try:
        async with aiohttp.ClientSession() as http:
            await http.post(
                response_url,
                json={"delete_original": True},
            )
    except Exception as e:
        logger.debug("Failed to dismiss ephemeral via response_url: %s", e)


@dataclass
class PendingRequest:
    """A single tool use permission request waiting for user approval."""

    request_id: str
    tool_name: str
    input_data: dict[str, Any]
    result_event: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool = False


@dataclass
class _BatchState:
    """Tracks in-flight permission batches awaiting user resolution."""

    events: dict[str, asyncio.Event] = field(default_factory=dict)
    decisions: dict[str, bool] = field(default_factory=dict)


@dataclass
class _AskUserState:
    """Tracks in-flight AskUserQuestion requests awaiting user answers."""

    events: dict[str, asyncio.Event] = field(default_factory=dict)
    questions: dict[str, list[dict]] = field(default_factory=dict)
    answers: dict[str, dict[str, str]] = field(default_factory=dict)
    expected: dict[str, int] = field(default_factory=dict)
    # For "Other" free-text input: (request_id, question_index)
    pending_other: tuple[str, int] | None = None
    # For multi-select: toggled selections per question keyed by (request_id, question_idx)
    multi_selections: dict[tuple[str, int], list[str]] = field(default_factory=dict)


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
        authenticated_user_id: str = "",
    ) -> None:
        self._router = router
        self._authenticated_user_id = authenticated_user_id
        self._debounce_ms = config.permission_debounce_ms

        # Pending requests waiting for batched approval
        self._pending: dict[str, PendingRequest] = {}
        self._batch_task: asyncio.Task | None = None
        self._batch_lock = asyncio.Lock()

        # Per-batch tracking (events, decisions)
        self._batch = _BatchState()

        # AskUserQuestion tracking
        self._ask_user = _AskUserState()

    async def handle(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext | None,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Main entry point for the can_use_tool callback."""
        # 0. Intercept AskUserQuestion — route to Slack interactive UI
        if tool_name == "AskUserQuestion":
            return await self._handle_ask_user_question(input_data)

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
            await self._router.client.post_ephemeral(
                self._authenticated_user_id,
                f"Permission required: {header_text[:100]}",
                blocks=blocks,
            )
        except Exception as e:
            logger.error("Failed to post permission message: %s", e)
            # Auto-deny if we can't post
            self._batch.decisions[batch_id] = False
            if batch_id in self._batch.events:
                self._batch.events[batch_id].set()

    async def handle_action(
        self,
        value: str,
        user_id: str,
        response_url: str = "",
    ) -> None:
        """Handle a Slack interactive button click for permission approval/denial.

        Must be called AFTER ack() (the 3-second deadline is the caller's responsibility).
        Channel routing is handled by ``EventDispatcher.dispatch_action``.
        """
        if self._authenticated_user_id and user_id != self._authenticated_user_id:
            logger.warning(
                "Permission action from unauthorized user %s (expected %s)",
                user_id,
                self._authenticated_user_id,
            )
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

        self._batch.decisions[batch_id] = approved

        # Dismiss the ephemeral message via response_url (the only reliable way)
        if response_url:
            await _dismiss_ephemeral(response_url)

        # Post a persistent confirmation to the turn thread
        status_text = ":white_check_mark: Approved" if approved else ":x: Denied"
        try:
            await self._router.post_to_active_thread(f"{status_text} by user")
        except Exception as e:
            logger.warning("Failed to post permission confirmation: %s", e)

        # Signal the waiting batch
        if batch_id in self._batch.events:
            self._batch.events[batch_id].set()

    async def _post_timeout_message(self) -> None:
        """Post a message indicating permission timed out."""
        try:
            await self._router.post_to_active_thread(
                ":hourglass: Permission request timed out after 5 minutes. Denied.",
            )
        except Exception as e:
            logger.warning("Failed to post timeout message: %s", e)

    # ------------------------------------------------------------------
    # AskUserQuestion handling
    # ------------------------------------------------------------------

    async def _handle_ask_user_question(
        self, input_data: dict[str, Any]
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Render AskUserQuestion as Slack interactive buttons and wait for answers."""
        questions = input_data.get("questions", [])
        if not questions:
            return PermissionResultAllow(updated_input=input_data)

        request_id = str(uuid.uuid4())
        event = asyncio.Event()

        self._ask_user.events[request_id] = event
        self._ask_user.questions[request_id] = questions
        self._ask_user.answers[request_id] = {}
        self._ask_user.expected[request_id] = len(questions)

        blocks = _build_ask_user_blocks(request_id, questions)
        try:
            await self._router.client.post_ephemeral(
                self._authenticated_user_id,
                "Claude has a question for you",
                blocks=blocks,
            )
        except Exception as e:
            logger.error("Failed to post AskUserQuestion message: %s", e)
            self._cleanup_ask_user(request_id)
            return PermissionResultDeny(message="Failed to display question")

        try:
            await asyncio.wait_for(event.wait(), timeout=_PERMISSION_TIMEOUT_S)
        except TimeoutError:
            logger.warning("AskUserQuestion timed out")
            self._cleanup_ask_user(request_id)
            return PermissionResultDeny(message="Question timed out (5 minutes)")

        answers = dict(self._ask_user.answers.get(request_id, {}))
        self._cleanup_ask_user(request_id)

        return PermissionResultAllow(
            updated_input={
                "questions": questions,
                "answers": answers,
            }
        )

    async def handle_ask_user_action(
        self,
        value: str,
        user_id: str,
        response_url: str = "",
    ) -> None:
        """Handle a Slack button click for an AskUserQuestion option.

        Value format: ``{request_id}|{question_idx}|{option_idx_or_other_or_done}``
        """
        if self._authenticated_user_id and user_id != self._authenticated_user_id:
            logger.warning(
                "Ask user action from unauthorized user %s (expected %s)",
                user_id,
                self._authenticated_user_id,
            )
            return

        parsed = _parse_ask_user_value(value)
        if parsed is None:
            return

        request_id, q_idx, opt_val = parsed

        if request_id not in self._ask_user.events:
            return

        questions = self._ask_user.questions.get(request_id, [])
        if q_idx >= len(questions):
            return

        question = questions[q_idx]

        if opt_val == "other":
            await self._handle_ask_other(request_id, q_idx, question)
        elif opt_val == "done":
            await self._handle_ask_done(request_id, q_idx, question)
        else:
            await self._handle_ask_option(request_id, q_idx, question, opt_val)

    async def _handle_ask_other(self, request_id: str, q_idx: int, question: dict) -> None:
        """Handle 'Other' button — set pending flag for free-text capture."""
        self._ask_user.pending_other = (request_id, q_idx)
        q_text = sanitize_for_mrkdwn(question.get("question", ""))
        # Post as ephemeral to main channel so the user sees the prompt prominently
        try:
            await self._router.client.post_ephemeral(
                self._authenticated_user_id,
                f":pencil: Type your answer for: _{q_text}_",
                blocks=[],
            )
        except Exception as e:
            logger.debug("Failed to post 'Other' prompt: %s", e)

    async def _handle_ask_done(self, request_id: str, q_idx: int, question: dict) -> None:
        """Handle 'Done' button for multi-select — finalize toggled selections."""
        key = (request_id, q_idx)
        selections = self._ask_user.multi_selections.pop(key, [])
        answer = ", ".join(selections) if selections else ""
        q_text = question.get("question", "")
        header = sanitize_for_mrkdwn(question.get("header", ""))
        self._ask_user.answers[request_id][q_text] = answer
        await _post_quietly(
            self._router,
            f":white_check_mark: *{header}*: {sanitize_for_mrkdwn(answer)}",
        )
        self._check_ask_user_complete(request_id)

    async def _handle_ask_option(
        self, request_id: str, q_idx: int, question: dict, opt_val: str
    ) -> None:
        """Handle a numbered option button click."""
        try:
            opt_idx = int(opt_val)
        except ValueError:
            return

        options = question.get("options", [])
        if opt_idx >= len(options):
            return

        label = options[opt_idx].get("label", "")
        q_text = question.get("question", "")
        header = sanitize_for_mrkdwn(question.get("header", ""))

        if question.get("multiSelect", False):
            await self._toggle_multi_select(request_id, q_idx, label, header)
        else:
            self._ask_user.answers[request_id][q_text] = label
            await _post_quietly(
                self._router,
                f":white_check_mark: *{header}*: {sanitize_for_mrkdwn(label)}",
            )
            self._check_ask_user_complete(request_id)

    async def _toggle_multi_select(
        self, request_id: str, q_idx: int, label: str, header: str
    ) -> None:
        """Toggle a multi-select option and post feedback."""
        key = (request_id, q_idx)
        selections = self._ask_user.multi_selections.setdefault(key, [])
        safe_label = sanitize_for_mrkdwn(label)
        if label in selections:
            selections.remove(label)
            await _post_quietly(
                self._router,
                f":heavy_minus_sign: *{header}*: deselected _{safe_label}_",
            )
        else:
            selections.append(label)
            await _post_quietly(
                self._router,
                f":heavy_plus_sign: *{header}*: selected _{safe_label}_",
            )

    def has_pending_text_input(self) -> bool:
        """Return True if we're waiting for free-text input from the user (Other)."""
        return self._ask_user.pending_other is not None

    async def receive_text_input(self, text: str) -> None:
        """Receive free-text input from the user for an 'Other' answer."""
        if not self._ask_user.pending_other:
            return

        request_id, q_idx = self._ask_user.pending_other
        self._ask_user.pending_other = None

        questions = self._ask_user.questions.get(request_id, [])
        if q_idx >= len(questions):
            return

        question = questions[q_idx]
        question_text = question.get("question", "")
        header = question.get("header", "")

        self._ask_user.answers[request_id][question_text] = text
        safe_header = sanitize_for_mrkdwn(header)
        await _post_quietly(
            self._router,
            f":white_check_mark: *{safe_header}*: {sanitize_for_mrkdwn(text)}",
        )

        self._check_ask_user_complete(request_id)

    def _check_ask_user_complete(self, request_id: str) -> None:
        """If all questions for a request are answered, signal the waiting coroutine."""
        answers = self._ask_user.answers.get(request_id, {})
        expected = self._ask_user.expected.get(request_id, 0)
        if len(answers) >= expected:
            event = self._ask_user.events.get(request_id)
            if event:
                event.set()

    def _cleanup_ask_user(self, request_id: str) -> None:
        """Remove all state for a completed or timed-out ask_user request."""
        self._ask_user.events.pop(request_id, None)
        questions = self._ask_user.questions.pop(request_id, [])
        self._ask_user.answers.pop(request_id, None)
        self._ask_user.expected.pop(request_id, None)
        if self._ask_user.pending_other and self._ask_user.pending_other[0] == request_id:
            self._ask_user.pending_other = None
        # Clean up multi-select state for all questions in this request
        for i in range(len(questions)):
            self._ask_user.multi_selections.pop((request_id, i), None)


def _parse_ask_user_value(value: str) -> tuple[str, int, str] | None:
    """Parse an ask_user action value into (request_id, question_idx, opt_val)."""
    parts = value.split("|")
    if len(parts) != 3:
        logger.warning("Invalid ask_user action value: %r", value)
        return None
    request_id, q_idx_str, opt_val = parts
    try:
        q_idx = int(q_idx_str)
    except ValueError:
        return None
    return request_id, q_idx, opt_val


async def _post_quietly(router: ThreadRouter, text: str) -> None:
    """Post to the turn thread, swallowing errors."""
    try:
        await router.post_to_active_thread(text)
    except Exception as e:
        logger.debug("Failed to post ask_user feedback: %s", e)


def _build_ask_user_blocks(request_id: str, questions: list[dict]) -> list[dict]:
    """Build Slack Block Kit blocks for AskUserQuestion rendering."""
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":question: *Claude has a question for you*"},
        },
        {"type": "divider"},
    ]

    for i, q in enumerate(questions):
        header = q.get("header", "")
        question_text = q.get("question", "")
        options = q.get("options", [])
        multi_select = q.get("multiSelect", False)

        # Question text (with multi-select hint)
        q_text = f"*{sanitize_for_mrkdwn(header)}*\n{sanitize_for_mrkdwn(question_text)}"
        if multi_select:
            q_text += "\n_Select multiple, then click Done_"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": q_text}})

        # Option descriptions + markdown previews as context
        desc_parts = []
        for opt in options:
            label = opt.get("label", "")
            desc = opt.get("description", "")
            md_preview = opt.get("markdown", "")
            if desc:
                desc_parts.append(
                    f"\u2022 *{sanitize_for_mrkdwn(label)}*: {sanitize_for_mrkdwn(desc)}"
                )
            if md_preview:
                # Render markdown preview as a code block (monospace)
                # Escape backticks to prevent breaking out of the code block
                safe_preview = md_preview.strip().replace("`", "\u2019")
                preview_lines = safe_preview.splitlines()
                # Truncate long previews to keep Slack message manageable
                if len(preview_lines) > 8:
                    preview_lines = [*preview_lines[:8], "..."]
                preview_text = "\n".join(preview_lines)
                desc_parts.append(f"```{preview_text}```")
        if desc_parts:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "\n".join(desc_parts)}],
                }
            )

        # Option buttons
        elements = []
        for j, opt in enumerate(options):
            label = opt.get("label", f"Option {j + 1}")
            elements.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": label[:75]},
                    "action_id": f"ask_user_{i}_{j}",
                    "value": f"{request_id}|{i}|{j}",
                }
            )

        # "Other" button
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Other"},
                "action_id": f"ask_user_{i}_other",
                "value": f"{request_id}|{i}|other",
            }
        )

        # "Done" button for multi-select
        if multi_select:
            elements.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Done"},
                    "style": "primary",
                    "action_id": f"ask_user_{i}_done",
                    "value": f"{request_id}|{i}|done",
                }
            )

        blocks.append(
            {
                "type": "actions",
                "block_id": f"ask_user_{request_id[:8]}_{i}",
                "elements": elements,
            }
        )

    return blocks


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
