"""Response pipeline — ResponseStreamer + ContentDisplay merged.

Owns the full turn lifecycle: numbering, tool tracking, turn summary, streaming.
"""

# pyright: reportArgumentType=false, reportReturnType=false
# claude_agent_sdk doesn't ship type stubs

from __future__ import annotations

import asyncio
import contextlib
import difflib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    TaskNotificationMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from summon_claude.security import validate_agent_output
from summon_claude.sessions.context import ContextUsage
from summon_claude.sessions.mcp_health import McpHealthTracker
from summon_claude.sessions.types import ChangeType, FileChange
from summon_claude.slack.client import redact_secrets, sanitize_for_mrkdwn
from summon_claude.slack.formatting import snippet_type_for_extension
from summon_claude.slack.markdown_split import split_markdown
from summon_claude.slack.router import ThreadRouter

if TYPE_CHECKING:
    from slack_sdk.web.async_chat_stream import AsyncChatStream

    from summon_claude.sessions.permissions import ApprovalBridge, ApprovalInfo

logger = logging.getLogger(__name__)

_TaskStatus = Literal["in_progress", "complete", "error"]

_MAX_MESSAGE_CHARS = 3000
_FLUSH_HEADROOM_CHARS = 100
_FLUSH_INTERVAL_S = 2.0  # 2 seconds to stay under Slack Tier 3 rate limits
# Built-in tools that bypass can_use_tool — the SDK never fires the callback
# for these, so a bridge Future would never resolve (bridge_timeout_s hang). Skip the
# bridge entirely for these tools. Spike-confirmed 2026-03-20 on SDK 0.1.48.
# If SDK is upgraded, re-run the spike (hack/spikes/spike_enter_worktree.py)
# to verify no new built-ins bypass can_use_tool; add them here if found.
_BRIDGE_SKIP_TOOLS = frozenset(["EnterWorktree", "ExitWorktree"])


def _sanitize_approval_reason(text: str) -> str:
    """Sanitize an approval reason for Slack mrkdwn display.

    Applies sanitize_for_mrkdwn (strips mrkdwn chars, truncates to 60),
    then removes underscores (prevent italic injection) and angle brackets
    (prevent Slack link/mention injection).
    """
    return sanitize_for_mrkdwn(text, 60).replace("_", " ").replace("<", "").replace(">", "")


# Maps tool names to the keys where their primary argument lives (tried in order).
_TOOL_PATH_KEYS: dict[str, tuple[str, ...]] = {
    "Read": ("file_path", "path"),
    "Cat": ("file_path", "path"),
    "Edit": ("file_path", "path"),
    "str_replace_editor": ("path", "file_path"),
    "Write": ("file_path", "path"),
    "MultiEdit": ("file_path", "path"),
    "Glob": ("pattern",),
    "Grep": ("pattern",),
    "NotebookEdit": ("notebook_path",),
}

_BASH_PREVIEW_CHARS = 120

# Characters that break mrkdwn formatting when embedded in italic/bold text
_MRKDWN_SPECIAL_RE = re.compile(r"[*_~`<>]")


def get_tool_primary_arg(tool_name: str, input_data: dict[str, Any]) -> str:
    """Return the primary argument for *tool_name* from *input_data*.

    For file-oriented tools this is the path; for Bash the command preview;
    for WebSearch/WebFetch the query/url.  Returns ``""`` when nothing useful
    is found.
    """
    if tool_name == "Bash":
        cmd = input_data.get("command", "")
        return cmd[:_BASH_PREVIEW_CHARS] + ("..." if len(cmd) > _BASH_PREVIEW_CHARS else "")

    if tool_name == "WebSearch":
        return input_data.get("query", "")

    if tool_name == "WebFetch":
        url = input_data.get("url", "")
        return url[:60] if url else ""

    keys = _TOOL_PATH_KEYS.get(tool_name)
    if keys:
        for key in keys:
            val = input_data.get(key, "")
            if val:
                return val

    return ""


_FENCE_OVERHEAD = len("\n```")  # bytes added when closing an unclosed code fence


def split_text(text: str, limit: int) -> list[str]:
    """Split text into chunks that each fit within the block character limit.

    Code-block-aware: if a split would occur inside an open ``` fence,
    the fence is closed at the end of the chunk and re-opened at the start
    of the next chunk so Slack renders both halves correctly.
    """
    if len(text) <= limit:
        return [text]

    # Precompute whether fences exist anywhere — avoids O(n) rescan per iteration.
    any_fences = "```" in text

    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        # If the original text contains fences, leave headroom for a potential
        # closure suffix. Only reduce the limit when fences exist.
        effective_limit = limit
        if any_fences:
            effective_limit = max(limit - _FENCE_OVERHEAD, 1)

        # Try to break at a newline boundary
        split_at = text.rfind("\n", 0, effective_limit)
        if split_at == -1:
            split_at = effective_limit
        chunk = text[:split_at]
        rest = text[split_at:]

        # Check if we're splitting inside an open code fence.
        # Count triple-backtick fences in the chunk — odd count means unclosed.
        if any_fences:
            fence_count = chunk.count("```")
            if fence_count % 2 == 1:
                chunk += "\n```"
                rest = "```\n" + rest

        chunks.append(chunk)
        text = rest
    return chunks


@dataclass
class _TurnState:
    """Mutable per-turn routing state, reset on each ``stream_with_flush`` call."""

    has_seen_tool_use: bool = False
    last_intermediate_text: str = ""
    posted_conclusion: bool = False
    posted_text_to_main: bool = False
    main_ts: str | None = None
    thread_ts: str | None = None
    buffer: str = ""
    last_message_ts: str | None = None
    posting_to_thread: bool = False
    resolved_model: str | None = None
    # Turn lifecycle fields
    tool_call_count: int = 0
    files_touched: list[str] = field(default_factory=list)
    user_snippet: str = ""
    turn_thread_ts: str | None = None
    thinking_buffer: str = ""
    md_rendered_paths: set[str] = field(default_factory=set)
    # Track EnterWorktree tool_use_ids → (name, path) for callback
    pending_worktree_names: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Track tool_use_id → tool name for health tracker
    tool_names: dict[str, str] = field(default_factory=dict)
    # tool_use_id → (tool_name, filepath, old_str, new_str); consumed by _handle_tool_result_block
    pending_file_changes: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)
    # Denied by approval bridge — suppresses :x: error posts and blocks file change tracking
    denied_tool_use_ids: set[str] = field(default_factory=set)
    # Hybrid streaming — thread-based chat_stream for tool progress
    active_stream: AsyncChatStream | None = None
    stream_failed: bool = False  # once True, fall back to chat_postMessage for rest of turn


@dataclass
class StreamResult:
    """Result of a stream_with_flush call."""

    result: ResultMessage
    model: str | None


class ResponseStreamer:
    """Streams Claude SDK messages into Slack with threaded routing.

    Owns the full turn lifecycle: numbering, tool tracking, turn summary,
    streaming, and content display (inline diffs).

    Routing heuristic:
    - TextBlock BEFORE any ToolUseBlock in the turn -> main channel
    - ToolUseBlock -> turn thread (sets _has_seen_tool_use)
    - TextBlock AFTER ToolUseBlock -> accumulated for main channel conclusion
    - On ResultMessage -> post accumulated conclusion to main channel
    - StreamEvent with parent_tool_use_id -> subagent thread
    """

    def __init__(  # noqa: PLR0913
        self,
        router: ThreadRouter,
        user_id: str | None = None,
        team_id: str | None = None,
        show_thinking: bool = False,
        max_inline_chars: int = 2500,
        on_file_change: Callable[[FileChange], Awaitable[None]] | None = None,
        on_worktree_entered: Callable[[str, str], Awaitable[None]] | None = None,
        mcp_health: McpHealthTracker | None = None,
        bridge: ApprovalBridge | None = None,
        bridge_timeout_s: float = 960.0,
        on_subagent_return: Callable[[dict, str], Awaitable[None]] | None = None,
    ) -> None:
        self._router = router
        self._user_id = user_id
        self._team_id = team_id
        self._show_thinking = show_thinking
        self._max_inline_chars = max_inline_chars
        self._on_file_change = on_file_change
        self._on_worktree_entered = on_worktree_entered
        self._mcp_health = mcp_health
        self._bridge = bridge
        self._bridge_timeout_s = bridge_timeout_s
        self._on_subagent_return = on_subagent_return

        # Cross-turn tracking of pending agent verifications (keyed by tool_use_id)
        self._pending_agent_verifications: dict[str, dict] = {}
        # Per-turn routing state (reset on each stream call)
        self._turn = _TurnState()
        # Turn number counter
        self._current_turn_number: int = 0
        # Strong references to fire-and-forget tasks (prevent GC)
        self._background_tasks: set[asyncio.Task[None]] = set()

    # --- Turn lifecycle ---

    async def start_turn(self, turn_number: int, user_snippet: str | None = None) -> str:
        """Create turn thread starter message, return thread_ts."""
        self._current_turn_number = turn_number
        if user_snippet:
            # Truncate, strip newlines, remove mrkdwn special chars
            snippet = user_snippet.replace("\n", " ")[:60]
            snippet = _MRKDWN_SPECIAL_RE.sub("", snippet)
            self._turn.user_snippet = snippet
            header = f"\U0001f527 Turn {turn_number}: re: _{snippet}_..."
        else:
            header = f"\U0001f527 Turn {turn_number}: Processing..."
        blocks = _build_turn_header_blocks(header)
        ref = await self._router.post_to_main(header, blocks=blocks)
        self._router.set_active_thread(ref.ts, ref)
        self._turn.turn_thread_ts = ref.ts

        # Set initial thread status
        await self._set_status("Thinking...")

        return ref.ts

    def finalize_turn(self, context: ContextUsage | None = None) -> str:
        """Build a concise summary string for the turn starter message."""
        return self._generate_turn_summary(context)

    async def update_turn_summary(self, summary: str) -> None:
        """Update the current turn's thread starter message with a summary."""
        if self._router.active_thread_ref:
            if self._turn.user_snippet:
                text = (
                    f"\U0001f527 Turn {self._current_turn_number}: "
                    f"re: _{self._turn.user_snippet}_ | {summary}"
                )
            else:
                text = f"\U0001f527 Turn {self._current_turn_number}: {summary}"
            blocks = _build_turn_header_blocks(text)
            await self._router.update(self._router.active_thread_ref.ts, text, blocks=blocks)

    def _tool_file_summary_parts(self) -> list[str]:
        """Shared tool-call and file-touch summary parts for turn reporting."""
        parts: list[str] = []
        if self._turn.tool_call_count:
            suffix = "s" if self._turn.tool_call_count != 1 else ""
            parts.append(f"{self._turn.tool_call_count} tool call{suffix}")
        if self._turn.files_touched:
            short_names = [p.rsplit("/", 1)[-1] for p in self._turn.files_touched[:3]]
            if len(self._turn.files_touched) > 3:
                short_names.append(f"+{len(self._turn.files_touched) - 3} more")
            parts.append(", ".join(short_names))
        return parts

    def _build_stream_summary_blocks(self) -> list[dict[str, Any]] | None:
        """Build compact summary blocks for the stream's final ``stop()`` message.

        Uses turn state (tool count, files) which is available before context/cost.
        Returns ``None`` if no tools were called and no files were touched (nothing to summarize).
        """
        parts = self._tool_file_summary_parts()
        if not parts:
            return None
        summary = " \u00b7 ".join(parts)
        return [
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f":white_check_mark: {summary}"}],
            }
        ]

    def _generate_turn_summary(self, context: ContextUsage | None = None) -> str:
        """Build a concise summary string for the turn starter message."""
        parts = self._tool_file_summary_parts()
        if context is not None:
            ctx_k = context.total_tokens // 1000
            win_k = context.max_tokens // 1000
            parts.append(f"{ctx_k}k/{win_k}k ({context.percentage:.0f}%)")
        return " \u00b7 ".join(parts) if parts else "Processing..."

    def record_tool_call(self, tool_input: dict[str, Any]) -> None:
        """Track tool calls for turn summary generation."""
        self._turn.tool_call_count += 1
        for key in ("file_path", "path", "command"):
            if key in tool_input and isinstance(tool_input[key], str):
                path = tool_input[key]
                if "/" in path and path not in self._turn.files_touched:
                    self._turn.files_touched.append(path)

    async def _set_status(self, status: str) -> None:
        """Set the thread status indicator (best-effort)."""
        if self._turn.turn_thread_ts:
            await self._router.client.set_thread_status(self._turn.turn_thread_ts, status)

    # --- Chat stream (hybrid streaming for thread-based tool progress) ---

    def _can_stream(self) -> bool:
        """Return True if chat_stream is usable for this turn."""
        return (
            self._team_id is not None and self._user_id is not None and not self._turn.stream_failed
        )

    async def _ensure_stream(self) -> bool:
        """Open a chat stream for the active turn thread if not already open.

        Returns True if a stream is available, False if streaming is unavailable
        or failed (caller should fall back to chat_postMessage).
        """
        if self._turn.active_stream is not None:
            return True
        if not self._can_stream() or not self._turn.turn_thread_ts:
            return False
        try:
            self._turn.active_stream = await self._router.client.open_chat_stream(
                self._turn.turn_thread_ts,
                team_id=self._team_id,  # type: ignore[arg-type]  # guarded by _can_stream
                user_id=self._user_id,  # type: ignore[arg-type]
            )
            return True
        except Exception as e:
            logger.warning("Failed to open chat_stream — falling back to messages: %s", e)
            self._turn.stream_failed = True
            return False

    async def _stream_task_update(
        self, tool_use_id: str, tool_name: str, status: _TaskStatus
    ) -> None:
        """Emit a TaskUpdateChunk to the active stream (best-effort).

        Falls back silently on error — the tool use/result context blocks
        posted separately provide redundant visibility.
        """
        if self._turn.active_stream is None:
            return
        from slack_sdk.models.messages.chunk import TaskUpdateChunk  # noqa: PLC0415

        chunk = TaskUpdateChunk(id=tool_use_id, title=tool_name, status=status)
        try:
            await self._turn.active_stream.append(chunks=[chunk])
        except Exception as e:
            logger.debug("TaskUpdateChunk append failed: %s", e)
            await self._close_stream_on_error()

    async def _stop_stream(self, blocks: list[dict[str, Any]] | None = None) -> None:
        """Stop the active chat stream (best-effort). Clears active_stream."""
        stream = self._turn.active_stream
        if stream is None:
            return
        self._turn.active_stream = None
        try:
            await stream.stop(blocks=blocks)
        except Exception as e:
            logger.debug("stream.stop() failed: %s", e)

    async def _close_stream_on_error(self) -> None:
        """Mark streaming as failed and stop the current stream."""
        self._turn.stream_failed = True
        stream = self._turn.active_stream
        self._turn.active_stream = None
        if stream is not None:
            with contextlib.suppress(BaseException):
                await stream.stop()

    # --- Streaming ---

    async def stream_with_flush(self, messages: AsyncIterator) -> StreamResult | None:  # noqa: PLR0912, PLR0915
        """Stream with a background flush task for periodic Slack updates."""
        # Preserve fields set by start_turn() across the per-stream reset
        saved_snippet = self._turn.user_snippet
        saved_thread_ts = self._turn.turn_thread_ts
        self._turn = _TurnState(user_snippet=saved_snippet, turn_thread_ts=saved_thread_ts)
        if self._bridge is not None:
            self._bridge.clear()
        result: ResultMessage | None = None
        stop_flush = asyncio.Event()

        async def flush_loop() -> None:
            while not stop_flush.is_set():
                await asyncio.sleep(_FLUSH_INTERVAL_S)
                if self._turn.buffer:
                    await self._flush_buffer()

        flush_task = asyncio.create_task(flush_loop())

        keys_before = set(self._pending_agent_verifications)
        try:
            async for message in messages:
                if isinstance(message, AssistantMessage):
                    await self._handle_assistant_message(message)
                elif isinstance(message, RateLimitEvent):
                    logger.info("Rate limit event received: %s", message)
                elif isinstance(message, TaskNotificationMessage):
                    if message.tool_use_id in self._pending_agent_verifications:
                        if message.status == "completed":
                            agent_input = self._pending_agent_verifications.pop(message.tool_use_id)
                            if self._on_subagent_return is not None:
                                self._spawn_background(
                                    self._on_subagent_return(agent_input, message.summary or "")
                                )
                        else:
                            self._pending_agent_verifications.pop(message.tool_use_id)
                            logger.debug(
                                "Subagent %s %s — skipping verification",
                                message.tool_use_id,
                                message.status,
                            )
                elif isinstance(message, ResultMessage):
                    result = message
                    if message.errors:
                        safe = redact_secrets(str(message.errors))
                        logger.warning("SDK ResultMessage errors: %s", safe)
                    break
        finally:
            stop_flush.set()
            flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flush_task
            # On aborted turns (no ResultMessage), clean up entries added this
            # turn — the subagent will never send a notification. On normal
            # completion, entries persist for cross-turn resolution.
            if result is None:
                added_this_turn = set(self._pending_agent_verifications) - keys_before
                for stale_key in added_this_turn:
                    self._pending_agent_verifications.pop(stale_key, None)
                with contextlib.suppress(BaseException):
                    await self._stop_stream()

        # Final flush, then stop stream with summary blocks
        if self._turn.thinking_buffer:
            await self._flush_thinking()
        if self._turn.buffer:
            await self._flush_buffer()
        if result:
            await self._stop_stream(blocks=self._build_stream_summary_blocks())
            await self._post_result_summary(result)
            # Clear thread status at turn end
            await self._set_status("")
            model = self._turn.resolved_model
            return StreamResult(result=result, model=model)

        return None

    async def _handle_assistant_message(self, message: AssistantMessage) -> None:
        """Process content blocks from an AssistantMessage."""
        if not self._turn.resolved_model and getattr(message, "model", None):
            self._turn.resolved_model = message.model
        if message.usage:
            logger.debug("Per-turn usage: %s", message.usage)
        parent_id = message.parent_tool_use_id

        for block in message.content:
            if isinstance(block, ThinkingBlock):
                await self._handle_thinking_block(block)
            elif isinstance(block, TextBlock):
                await self._flush_thinking()
                await self._handle_text_block(block, parent_id)
            elif isinstance(block, ToolUseBlock):
                await self._flush_thinking()
                await self._handle_tool_use_block(block, parent_id)
            elif isinstance(block, ToolResultBlock):
                await self._handle_tool_result_block(block, parent_id)

    async def _handle_text_block(self, block: TextBlock, parent_id: str | None) -> None:
        """Route a TextBlock based on context (subagent, pre-tool, post-tool)."""
        if parent_id:
            await self._flush_buffer()
            await self._post_to_subagent(parent_id, block.text)
        elif self._turn.has_seen_tool_use:
            # Flush any pending pre-tool text before switching
            await self._flush_buffer()
            # Eager: post immediately to turn thread for real-time visibility
            await self._router.post_to_active_thread(block.text)
            # Track for conclusion: final segment goes to main with @mention
            self._turn.last_intermediate_text += block.text
            # No _set_status — thread post auto-clears, next block sets its own
        else:
            self._turn.posting_to_thread = False
            await self._append_text(block.text)

    async def _handle_tool_use_block(self, block: ToolUseBlock, parent_id: str | None) -> None:
        """Route a ToolUseBlock to the correct thread."""
        if self._turn.buffer:
            await self._flush_buffer()

        self._turn.has_seen_tool_use = True
        self._turn.tool_names[block.id] = block.name
        self.record_tool_call(block.input or {})

        if block.name == "Task":
            description = _extract_task_description(block.input or {})
            await self._router.start_subagent_thread(block.id, description)
            self._pending_agent_verifications[block.id] = block.input or {}

        if block.name == "EnterWorktree" and self._on_worktree_entered is not None:
            inp = block.input or {}
            wt_name = inp.get("name") or ""
            wt_path = inp.get("path") or ""
            if wt_name and wt_path:
                logger.warning(
                    "EnterWorktree has both name=%r and path=%r; using name",
                    wt_name,
                    wt_path,
                )
                wt_path = ""
            self._turn.pending_worktree_names[block.id] = (wt_name, wt_path)

        # Await approval before posting — skip bridge for subagent tool calls.
        # The SDK may not invoke can_use_tool for tools in Task subagent messages
        # (parent_id != None). If the callback is never called, the Future would
        # never resolve, causing a bridge_timeout_s hang.
        approval: ApprovalInfo | None = None
        if self._bridge is not None and parent_id is None and block.name not in _BRIDGE_SKIP_TOOLS:
            try:
                fut = self._bridge.create_future(block.name)
                approval = await asyncio.wait_for(fut, timeout=self._bridge_timeout_s or None)
            except (TimeoutError, asyncio.CancelledError):
                logger.warning(
                    "Approval bridge timeout for %s — posting without label",
                    block.name,
                )

        if approval is not None and approval.is_denial:
            self._turn.denied_tool_use_ids.add(block.id)

        await self._post_tool_use(block, parent_id, approval=approval)
        # Set status AFTER posting — thread post auto-clears any previous status,
        # so this persists during actual tool execution until the result arrives.
        await self._set_status(f"Running {block.name}...")

        # Hybrid streaming: emit TaskUpdateChunk(in_progress) for non-subagent, non-denied tools
        if (
            parent_id is None
            and block.id not in self._turn.denied_tool_use_ids
            and self._can_stream()
            and await self._ensure_stream()
        ):
            await self._stream_task_update(block.id, block.name, "in_progress")

    async def _handle_tool_result_block(
        self, block: ToolResultBlock, parent_id: str | None
    ) -> None:
        """Route a ToolResultBlock to the correct thread."""
        wt_entry = self._turn.pending_worktree_names.pop(block.tool_use_id, None)
        if wt_entry is not None and not block.is_error and self._on_worktree_entered is not None:
            wt_name, wt_path = wt_entry
            await self._on_worktree_entered(wt_name, wt_path)
        # Pop before early-return so denied/errored results don't leave stale entries
        pending_fc = self._turn.pending_file_changes.pop(block.tool_use_id, None)
        if self._mcp_health is not None:
            tool_name = self._turn.tool_names.get(block.tool_use_id)
            if tool_name:
                error_text = redact_secrets(str(block.content)[:500]) if block.is_error else None
                await self._mcp_health.record_tool_result(
                    tool_name, is_error=block.is_error, error_content=error_text
                )
        # Suppress redundant task cards and :x: for denied tools — the denial label
        # on the tool use block already communicates the outcome.
        denied = block.tool_use_id in self._turn.denied_tool_use_ids

        # Hybrid streaming: emit TaskUpdateChunk(complete/error) for non-subagent, non-denied
        if parent_id is None and self._turn.active_stream is not None and not denied:
            tool_name = self._turn.tool_names.get(block.tool_use_id, "tool")
            status = "error" if block.is_error else "complete"
            await self._stream_task_update(block.tool_use_id, tool_name, status)
        if denied and block.is_error:
            return
        if pending_fc is not None and not block.is_error and not denied:
            fc_tool_name, filepath, old_str, new_str = pending_fc
            # For Edit tools, compute unified_diff once and share it with both
            # _schedule_file_change (additions/deletions count) and _upload_diff (Slack content).
            edit_diff_lines = (
                list(
                    difflib.unified_diff(
                        old_str.splitlines(keepends=True),
                        new_str.splitlines(keepends=True),
                        fromfile=f"a/{filepath}",
                        tofile=f"b/{filepath}",
                    )
                )
                if fc_tool_name in ("Edit", "str_replace_editor") and old_str
                else []
            )
            self._schedule_file_change(
                filepath, old_str, new_str, precomputed_diff=edit_diff_lines or None
            )
            # Fire deferred diff/content upload
            try:
                thread_ts = self._resolve_upload_thread(parent_id)
            except RuntimeError:
                pass
            else:
                if fc_tool_name in ("Edit", "str_replace_editor"):
                    self._spawn_background(
                        self._upload_diff(
                            old_str,
                            new_str,
                            filepath,
                            thread_ts,
                            precomputed_diff=edit_diff_lines,
                        )
                    )
                elif fc_tool_name == "Write" and filepath and new_str:
                    if filepath.endswith(".md"):
                        rendered = self._turn.md_rendered_paths
                        self._spawn_background(
                            self._render_md_write(filepath, new_str, thread_ts, rendered)
                        )
                    else:
                        basename = PurePosixPath(filepath).name
                        self._spawn_background(self._upload_write(new_str, basename, thread_ts))
        await self._post_tool_result(block, parent_id)
        # No _set_status — thread post auto-clears "Running {tool}...",
        # and the next block (tool use or text) arrives quickly.

    async def _handle_thinking_block(self, block: ThinkingBlock) -> None:
        """Handle a ThinkingBlock — accumulate if showing, always update status."""
        await self._set_status("Thinking deeply...")
        if self._show_thinking:
            self._turn.thinking_buffer += block.thinking

    async def _flush_thinking(self) -> None:
        """Flush accumulated thinking content to the turn thread."""
        if not self._turn.thinking_buffer:
            return
        text = self._turn.thinking_buffer
        self._turn.thinking_buffer = ""
        if len(text) > self._max_inline_chars:
            await self._router.upload_to_active_thread(text, "thinking.md")
        else:
            prefix = ":thought_balloon: "
            chunk_limit = 3000 - len(prefix)  # Slack context element limit
            chunks = split_text(text, chunk_limit)
            for chunk in chunks:
                blocks = [
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"{prefix}{chunk}",
                            }
                        ],
                    }
                ]
                await self._router.post_to_active_thread("Thinking...", blocks=blocks)
        # No _set_status — thread post auto-clears, next block sets its own

    async def _flush_conclusion_to_main(self) -> None:
        """Flush accumulated intermediate text to the main channel as conclusion."""
        if self._turn.last_intermediate_text.strip():
            text = self._turn.last_intermediate_text
            self._turn.last_intermediate_text = ""
            self._turn.posted_conclusion = True
            chunks = split_text(text, _MAX_MESSAGE_CHARS)
            for i, chunk in enumerate(chunks):
                post_text = f"<@{self._user_id}> {chunk}" if i == 0 and self._user_id else chunk
                ref = await self._router.post_to_main(post_text)
                self._turn.last_message_ts = ref.ts

    async def _append_text(self, text: str) -> None:
        """Append text to the buffer, splitting into new messages at the limit."""
        self._turn.buffer += text
        if len(self._turn.buffer) >= _MAX_MESSAGE_CHARS - _FLUSH_HEADROOM_CHARS:
            await self._flush_buffer()

    async def _flush_buffer(self) -> None:
        """Write the current buffer to Slack (create or update message)."""
        if not self._turn.buffer.strip():
            self._turn.buffer = ""
            return

        text = self._turn.buffer
        self._turn.buffer = ""

        if self._turn.posting_to_thread:
            await self._flush_to_thread(text)
        else:
            await self._flush_to_main(text)

    async def _flush_to_destination(
        self,
        text: str,
        stored_ts: str | None,
        post_fn,
        ts_attr: str,
    ) -> None:
        """Shared flush logic for main channel and turn thread.

        Args:
            text: Text to flush.
            stored_ts: Current message ts being updated.
            post_fn: Async callable that posts a new message and returns a ``MessageRef``.
            ts_attr: ``_turn`` attribute for the new ts (``"main_ts"`` or ``"thread_ts"``).
        """
        if stored_ts and len(text) > _MAX_MESSAGE_CHARS:
            chunks = split_text(text, _MAX_MESSAGE_CHARS)
            for chunk in chunks:
                ref = await post_fn(chunk)
                self._turn.last_message_ts = ref.ts
            setattr(self._turn, ts_attr, None)
            return

        if stored_ts:
            try:
                await self._router.update(stored_ts, text)
            except Exception as e:
                logger.warning("Failed to update message: %s — posting new", e)
                ref = await post_fn(text)
                setattr(self._turn, ts_attr, ref.ts)
                self._turn.last_message_ts = ref.ts
        else:
            ref = await post_fn(text)
            setattr(self._turn, ts_attr, ref.ts)
            self._turn.last_message_ts = ref.ts

    async def _flush_to_main(self, text: str) -> None:
        """Flush text to the main channel."""
        self._turn.posted_text_to_main = True
        await self._flush_to_destination(
            text, self._turn.main_ts, self._router.post_to_main, "main_ts"
        )

    async def _flush_to_thread(self, text: str) -> None:
        """Flush text to the current turn thread.

        When a chat stream is active, appends text to the stream instead of
        posting individual messages.  Falls back to chat_postMessage on error.

        Text is sanitized before streaming — the stream path bypasses
        ``SlackClient.post()`` which normally handles redaction.
        """
        if self._turn.active_stream is not None:
            try:
                sanitized = redact_secrets(text)
                sanitized, sec_warnings = validate_agent_output(sanitized)
                if sec_warnings:
                    for w in sec_warnings:
                        logger.warning("Stream output validation: %s", w)
                await self._turn.active_stream.append(markdown_text=sanitized)
                return
            except Exception as e:
                logger.warning("stream.append failed — falling back to messages: %s", e)
                await self._close_stream_on_error()
                # Text already appended to the stream before the failure remains
                # visible as an orphan message. The fallback below posts this chunk
                # as a new message — partial duplication is preferred over data loss.
        await self._flush_to_destination(
            text, self._turn.thread_ts, self._router.post_to_active_thread, "thread_ts"
        )

    async def _post_to_subagent(self, tool_use_id: str, text: str) -> None:
        """Post text to a subagent's dedicated thread."""
        chunks = split_text(text, _MAX_MESSAGE_CHARS)
        for chunk in chunks:
            await self._router.post_to_subagent_thread(tool_use_id, chunk)

    async def _post_tool_use(
        self,
        block: ToolUseBlock,
        parent_id: str | None = None,
        approval: ApprovalInfo | None = None,
    ) -> None:
        """Post a tool use context block to the appropriate thread."""
        tool_name = block.name
        input_data = block.input or {}
        summary = _format_tool_summary(tool_name, input_data)
        blocks = self._make_tool_use_blocks(tool_name, summary, approval=approval)
        if parent_id:
            await self._router.post_to_subagent_thread(
                parent_id, f"Tool: {tool_name}", blocks=blocks
            )
        else:
            await self._router.post_to_active_thread(f"Tool: {tool_name}", blocks=blocks)
            self._turn.thread_ts = None

        # Defer diff/content upload for Edit tools until ToolResultBlock confirms success
        if tool_name in ("Edit", "str_replace_editor") and "old_string" in input_data:
            filename = input_data.get("path", input_data.get("file_path", "file"))
            old_str = input_data.get("old_string", "")
            new_str = input_data.get("new_string", "")
            self._turn.pending_file_changes[block.id] = (tool_name, filename, old_str, new_str)

        # Defer content upload for Write until ToolResultBlock confirms success
        elif tool_name == "Write":
            filepath = input_data.get("file_path", input_data.get("path", ""))
            content = input_data.get("content", "")
            self._turn.pending_file_changes[block.id] = (tool_name, filepath, "", content)

    async def _post_tool_result(self, block: ToolResultBlock, parent_id: str | None = None) -> None:
        """Post a brief tool result summary to the appropriate thread."""
        text, blocks = _format_tool_result(block)
        if blocks:
            if parent_id:
                await self._router.post_to_subagent_thread(parent_id, text, blocks=blocks)
            else:
                await self._router.post_to_active_thread(text, blocks=blocks)
                self._turn.thread_ts = None

    async def _post_result_summary(self, result: ResultMessage) -> None:
        """Flush conclusion text and/or result.result to main channel."""
        if self._turn.last_intermediate_text.strip():
            await self._flush_conclusion_to_main()
            return

        if self._turn.posted_text_to_main:
            return

        if result.result:
            await self._router.post_to_main(result.result)

    async def post_turn_footer(self, footer: str) -> None:
        """Post a turn footer (cost + context) as a context block to main."""
        blocks: list[dict[str, Any]] = [
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": footer},
                ],
            },
        ]
        await self._router.post_to_main(footer, blocks=blocks)

    def _make_tool_use_blocks(
        self,
        tool_name: str,
        summary: str,
        approval: ApprovalInfo | None = None,
    ) -> list[dict[str, Any]]:
        """Build Block Kit blocks for a tool use context message."""
        # Build approval label suffix
        label_suffix = ""
        if approval is not None:
            if approval.reason:
                safe_reason = _sanitize_approval_reason(approval.reason)
                approval_text = f"{approval.label}: {safe_reason}"
            else:
                approval_text = approval.label
            label_suffix = f" _({approval_text})_"

        is_denied = approval is not None and approval.is_denial
        emoji = ":no_entry_sign:" if is_denied else ":hammer_and_wrench:"
        separator = " " if summary else ""
        text = f"{emoji} *{tool_name}*{separator}{summary}{label_suffix}"

        return [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": text,
                    }
                ],
            }
        ]

    def _spawn_background(self, coro: Awaitable[None]) -> None:
        """Schedule a fire-and-forget task with a strong reference to prevent GC."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_done)

    def _on_background_done(self, task: asyncio.Task[None]) -> None:
        """Clean up completed background task and suppress unhandled exception warnings."""
        self._background_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.debug("Background task failed: %s", exc)

    def _resolve_upload_thread(self, parent_id: str | None) -> str:
        """Resolve thread_ts for file uploads, respecting subagent threads."""
        if parent_id:
            ts = self._router.subagent_threads.get(parent_id)
            if ts:
                return ts
        ts = self._router.active_thread_ts
        if not ts:
            raise RuntimeError("No active thread for upload")
        return ts

    async def _upload_diff(
        self,
        old_string: str,
        new_string: str,
        filename: str,
        thread_ts: str,
        *,
        precomputed_diff: list[str] | None = None,
    ) -> None:
        """Upload a unified diff as a snippet_type=diff file (fire-and-forget)."""
        diff_lines: list[str] = []
        try:
            diff_lines = (
                precomputed_diff
                if precomputed_diff is not None
                else list(
                    difflib.unified_diff(
                        old_string.splitlines(keepends=True),
                        new_string.splitlines(keepends=True),
                        fromfile=f"a/{filename}",
                        tofile=f"b/{filename}",
                    )
                )
            )
            if not diff_lines:
                await self._router.post_to_thread(
                    f"*No changes in `{filename}`*", thread_ts=thread_ts
                )
                return

            diff_text = "".join(diff_lines)
            basename = PurePosixPath(filename).name
            await self._router.upload(
                diff_text,
                f"{basename}.diff",
                title=f"Edit: {basename}",
                thread_ts=thread_ts,
                snippet_type="diff",
            )
        except Exception:
            # Fallback: post diff inline
            logger.warning("Diff upload failed for %s, using inline fallback", filename)
            if not diff_lines:
                return
            try:
                diff_text = "".join(diff_lines)
                combined = f"**Edit:** `{filename}`\n```\n{diff_text}\n```"
                chunks = split_text(combined, _MAX_MESSAGE_CHARS)
                for chunk in chunks:
                    await self._router.post_to_thread(chunk, thread_ts=thread_ts)
            except Exception:
                logger.warning("Diff inline fallback also failed for %s", filename)

    async def _render_md_write(
        self,
        filepath: str,
        content: str,
        thread_ts: str,
        rendered_paths: set[str],
    ) -> None:
        """Render a Write-created .md file with type: markdown blocks.

        Args:
            thread_ts: Captured at task creation time to avoid race with turn reset.
            rendered_paths: Reference to the originating turn's md_rendered_paths set.
        """
        basename = PurePosixPath(filepath).name
        n_chars = len(content)

        # Claim slot immediately (before any await) to prevent duplicate renders
        already_rendered = filepath in rendered_paths
        rendered_paths.add(filepath)

        if already_rendered:
            try:
                await self._router.post_to_thread(
                    f":page_facing_up: **Updated:** `{basename}` ({n_chars} chars)",
                    thread_ts=thread_ts,
                )
            except Exception:
                logger.warning("Failed to post .md update notice for %s", basename)
            return

        # Post context header
        try:
            await self._router.post_to_thread(
                f":page_facing_up: **Created:** `{basename}` ({n_chars} chars)",
                thread_ts=thread_ts,
            )
        except Exception:
            logger.warning("Failed to post .md header for %s", basename)

        # Split and post markdown blocks
        chunks = split_markdown(content, limit=12000)
        for chunk in chunks:
            try:
                await self._router.post_markdown_to_thread(chunk, thread_ts=thread_ts)
            except Exception:
                # Fallback: post as mrkdwn-converted text if markdown blocks fail
                logger.warning("Markdown block failed for %s, using mrkdwn fallback", basename)
                try:
                    await self._router.post_to_thread(chunk, thread_ts=thread_ts)
                except Exception:
                    logger.warning("mrkdwn fallback also failed for %s", basename)

    async def _upload_write(
        self,
        content: str,
        basename: str,
        thread_ts: str,
    ) -> None:
        """Upload written file content (fire-and-forget)."""
        try:
            ext = PurePosixPath(basename).suffix.lstrip(".")
            await self._router.upload(
                content,
                basename,
                title=f"Written: {basename}",
                thread_ts=thread_ts,
                snippet_type=snippet_type_for_extension(ext),
            )
        except Exception:
            logger.warning("Write content upload failed for %s", basename)

    def _schedule_file_change(
        self,
        filepath: str,
        old_content: str,
        new_content: str,
        *,
        precomputed_diff: list[str] | None = None,
    ) -> None:
        """Schedule on_file_change callback as a fire-and-forget task."""
        if not self._on_file_change or not filepath:
            return
        new_lines = new_content.splitlines() if new_content else []
        change_type: ChangeType = "modified" if old_content else "created"
        if old_content:
            # Count actual changed lines via unified diff.
            # Use precomputed_diff when available to avoid a second unified_diff call.
            diff_iter = (
                iter(precomputed_diff)
                if precomputed_diff is not None
                else difflib.unified_diff(old_content.splitlines(), new_content.splitlines())
            )
            additions = 0
            deletions = 0
            for line in diff_iter:
                if line.startswith("+") and not line.startswith("+++"):
                    additions += 1
                elif line.startswith("-") and not line.startswith("---"):
                    deletions += 1
        else:
            additions = len(new_lines)
            deletions = 0
        change = FileChange(
            path=filepath,
            change_type=change_type,
            additions=additions,
            deletions=deletions,
            timestamp=datetime.now(UTC),
            turn_number=self._current_turn_number,
        )
        self._spawn_background(self._on_file_change(change))


def _build_turn_header_blocks(text: str) -> list[dict[str, Any]]:
    """Build Block Kit blocks for a turn header message with an overflow menu accessory."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
            "accessory": {
                "type": "overflow",
                "action_id": "turn_overflow",
                "options": [
                    {
                        "text": {"type": "plain_text", "text": "Stop Turn"},
                        "value": "turn_stop",
                    },
                    {
                        "text": {"type": "plain_text", "text": "Copy Session ID"},
                        "value": "turn_copy_sid",
                    },
                    {
                        "text": {"type": "plain_text", "text": "View Cost"},
                        "value": "turn_view_cost",
                    },
                ],
            },
        }
    ]


def _format_tool_result(block: ToolResultBlock) -> tuple[str, list[dict[str, Any]]]:
    """Format a ToolResultBlock into (text, blocks). Returns empty blocks if nothing to show."""
    content = block.content
    if not content:
        return "", []
    if block.is_error:
        if isinstance(content, str):
            # Redact a generous window (500 chars) to catch secrets near the
            # 200-char display boundary, then truncate for display.
            redacted = redact_secrets(content[:500])
            preview = redacted[:200] + ("..." if len(redacted) > 200 else "")
            text = f":x: Tool error: {preview}"
        else:
            text = ":x: Tool error"
    elif isinstance(content, str):
        redacted = redact_secrets(content[:500])
        preview = redacted[:200] + ("..." if len(redacted) > 200 else "")
        text = f":white_check_mark: {preview}"
    else:
        text = ":white_check_mark: Tool completed"

    blocks: list[dict[str, Any]] = [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": text}],
        }
    ]
    return "Tool result", blocks


def _extract_task_description(input_data: dict[str, Any]) -> str:
    """Extract a description from a Task tool invocation."""
    desc = input_data.get("description", "")
    if desc:
        # Take first line, max 60 chars
        first_line = desc.split("\n", 1)[0]
        return first_line[:60] + ("..." if len(first_line) > 60 else "")
    prompt = input_data.get("prompt", "")
    if prompt:
        first_line = prompt.split("\n", 1)[0]
        return first_line[:60] + ("..." if len(first_line) > 60 else "")
    return "Running subagent"


def _format_tool_summary(tool_name: str, input_data: dict) -> str:
    """Create a concise summary of a tool invocation."""
    arg = get_tool_primary_arg(tool_name, input_data)
    return f"`{arg}`" if arg else ""
