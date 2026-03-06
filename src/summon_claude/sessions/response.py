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
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from summon_claude.sessions.context import ContextUsage, compute_context_usage
from summon_claude.slack.router import ThreadRouter

logger = logging.getLogger(__name__)

_MAX_MESSAGE_CHARS = 3000
_FLUSH_HEADROOM_CHARS = 100
_FLUSH_INTERVAL_S = 2.0  # 2 seconds to stay under Slack Tier 3 rate limits

# Maps tool names to the keys where their primary argument lives (tried in order).
_TOOL_PATH_KEYS: dict[str, tuple[str, ...]] = {
    "Read": ("file_path", "path"),
    "Cat": ("file_path", "path"),
    "Edit": ("path", "file_path"),
    "str_replace_editor": ("path", "file_path"),
    "Write": ("file_path", "path"),
    "Glob": ("pattern",),
    "Grep": ("pattern",),
    "NotebookEdit": ("notebook_path",),
}

_BASH_PREVIEW_CHARS = 120


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
    text_after_tools: str = ""
    posted_conclusion: bool = False
    main_ts: str | None = None
    thread_ts: str | None = None
    buffer: str = ""
    last_message_ts: str | None = None
    posting_to_thread: bool = False
    resolved_model: str | None = None
    # Turn lifecycle fields
    tool_call_count: int = 0
    files_touched: list[str] = field(default_factory=list)


@dataclass
class StreamResult:
    """Result of a stream_with_flush call, including context usage info."""

    result: ResultMessage
    context: ContextUsage | None
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

    def __init__(
        self,
        router: ThreadRouter,
        user_id: str | None = None,
    ) -> None:
        self._router = router
        self._user_id = user_id

        # Per-turn routing state (reset on each stream call)
        self._turn = _TurnState()
        # Turn number counter
        self._current_turn_number: int = 0

    # --- Turn lifecycle ---

    async def start_turn(self, turn_number: int) -> str:
        """Create turn thread starter message, return thread_ts."""
        self._current_turn_number = turn_number
        ref = await self._router.post_to_main(
            f"\U0001f527 Turn {turn_number}: Processing...",
        )
        self._router.set_active_thread(ref.ts, ref)
        return ref.ts

    def finalize_turn(self) -> str:
        """Build a concise summary string for the turn starter message."""
        return self._generate_turn_summary()

    async def update_turn_summary(self, summary: str) -> None:
        """Update the current turn's thread starter message with a summary."""
        if self._router.active_thread_ref:
            await self._router.client.update(
                self._router.active_thread_ref.ts,
                f"\U0001f527 Turn {self._current_turn_number}: {summary}",
            )

    def _generate_turn_summary(self) -> str:
        """Build a concise summary string for the turn starter message."""
        parts: list[str] = []
        if self._turn.tool_call_count:
            suffix = "s" if self._turn.tool_call_count != 1 else ""
            parts.append(f"{self._turn.tool_call_count} tool call{suffix}")
        if self._turn.files_touched:
            short_names = [p.rsplit("/", 1)[-1] for p in self._turn.files_touched[:3]]
            if len(self._turn.files_touched) > 3:
                short_names.append(f"+{len(self._turn.files_touched) - 3} more")
            parts.append(", ".join(short_names))
        return " \u00b7 ".join(parts) if parts else "Processing..."

    def record_tool_call(self, tool_input: dict[str, Any]) -> None:
        """Track tool calls for turn summary generation."""
        self._turn.tool_call_count += 1
        for key in ("file_path", "path", "command"):
            if key in tool_input and isinstance(tool_input[key], str):
                path = tool_input[key]
                if "/" in path and path not in self._turn.files_touched:
                    self._turn.files_touched.append(path)

    # --- Streaming ---

    async def stream_with_flush(self, messages: AsyncIterator) -> StreamResult | None:
        """Stream with a background flush task for periodic Slack updates."""
        self._turn = _TurnState()
        result: ResultMessage | None = None
        stop_flush = asyncio.Event()

        async def flush_loop() -> None:
            while not stop_flush.is_set():
                await asyncio.sleep(_FLUSH_INTERVAL_S)
                if self._turn.buffer:
                    await self._flush_buffer()

        flush_task = asyncio.create_task(flush_loop())

        try:
            async for message in messages:
                if isinstance(message, AssistantMessage):
                    await self._handle_assistant_message(message)
                elif isinstance(message, ResultMessage):
                    result = message
                    break
        finally:
            stop_flush.set()
            flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flush_task

        # Final flush
        if self._turn.buffer:
            await self._flush_buffer()
        if result:
            await self._flush_conclusion_to_main()
            await self._post_result_summary(result)
            model = self._turn.resolved_model
            context = compute_context_usage(result.usage, model)
            return StreamResult(result=result, context=context, model=model)

        return None

    async def _handle_assistant_message(self, message: AssistantMessage) -> None:
        """Process content blocks from an AssistantMessage."""
        if not self._turn.resolved_model and getattr(message, "model", None):
            self._turn.resolved_model = message.model
        parent_id = message.parent_tool_use_id

        for block in message.content:
            if isinstance(block, TextBlock):
                await self._handle_text_block(block, parent_id)
            elif isinstance(block, ToolUseBlock):
                await self._handle_tool_use_block(block, parent_id)
            elif isinstance(block, ToolResultBlock):
                await self._handle_tool_result_block(block, parent_id)

    async def _handle_text_block(self, block: TextBlock, parent_id: str | None) -> None:
        """Route a TextBlock based on context (subagent, pre-tool, post-tool)."""
        if parent_id:
            await self._flush_buffer()
            await self._post_to_subagent(parent_id, block.text)
        elif self._turn.has_seen_tool_use:
            # Flush any pending pre-tool text before switching to conclusion mode
            await self._flush_buffer()
            # Accumulate conclusion text for main channel only — do NOT add to buffer
            # This prevents duplication: text_after_tools goes to main via _flush_conclusion_to_main
            self._turn.text_after_tools += block.text
        else:
            self._turn.posting_to_thread = False
            await self._append_text(block.text)

    async def _handle_tool_use_block(self, block: ToolUseBlock, parent_id: str | None) -> None:
        """Route a ToolUseBlock to the correct thread."""
        if self._turn.buffer:
            await self._flush_buffer()

        self._turn.has_seen_tool_use = True
        self.record_tool_call(block.input or {})

        if block.name == "Task":
            description = _extract_task_description(block.input or {})
            await self._router.start_subagent_thread(block.id, description)

        await self._post_tool_use(block, parent_id)

    async def _handle_tool_result_block(
        self, block: ToolResultBlock, parent_id: str | None
    ) -> None:
        """Route a ToolResultBlock to the correct thread."""
        await self._post_tool_result(block, parent_id)

    async def _flush_conclusion_to_main(self) -> None:
        """Flush post-tool text to the main channel as the conclusion."""
        if self._turn.text_after_tools.strip():
            text = self._turn.text_after_tools
            self._turn.text_after_tools = ""
            self._turn.posted_conclusion = True
            # Post conclusion text to main channel
            chunks = split_text(text, _MAX_MESSAGE_CHARS)
            for i, chunk in enumerate(chunks):
                # Ping the user on the first conclusion chunk only
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
                await self._router.client.update(stored_ts, text)
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
        await self._flush_to_destination(
            text, self._turn.main_ts, self._router.post_to_main, "main_ts"
        )

    async def _flush_to_thread(self, text: str) -> None:
        """Flush text to the current turn thread."""
        await self._flush_to_destination(
            text, self._turn.thread_ts, self._router.post_to_active_thread, "thread_ts"
        )

    async def _post_to_subagent(self, tool_use_id: str, text: str) -> None:
        """Post text to a subagent's dedicated thread."""
        chunks = split_text(text, _MAX_MESSAGE_CHARS)
        for chunk in chunks:
            await self._router.post_to_subagent_thread(tool_use_id, chunk)

    async def _post_tool_use(self, block: ToolUseBlock, parent_id: str | None = None) -> None:
        """Post a tool use context block to the appropriate thread."""
        tool_name = block.name
        input_data = block.input or {}
        summary = _format_tool_summary(tool_name, input_data)
        blocks = self._make_tool_use_blocks(tool_name, summary, input_data)
        if parent_id:
            await self._router.post_to_subagent_thread(
                parent_id, f"Tool: {tool_name}", blocks=blocks
            )
        else:
            await self._router.post_to_active_thread(f"Tool: {tool_name}", blocks=blocks)
            self._turn.thread_ts = None

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
        """Post session summary after Claude finishes a turn."""
        # Only post result.result if we didn't already post the same content
        # via _flush_conclusion_to_main (which posts text_after_tools).
        if result.result and not self._turn.posted_conclusion:
            await self._router.post_to_main(result.result)

        cost = result.total_cost_usd
        cost_str = f"${cost:.4f}" if cost is not None else "unknown"
        turns = result.num_turns
        turns_str = f"{turns}" if turns is not None else "?"

        blocks: list[dict[str, Any]] = [
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f":checkered_flag: Turn complete"
                            f" | Cost: {cost_str} | Turns: {turns_str}"
                        ),
                    }
                ],
            },
        ]

        await self._router.post_to_main(f"Turn complete. Cost: {cost_str}", blocks=blocks)

        # Add a reaction to the last text message
        if self._turn.last_message_ts:
            try:
                await self._router.client.react(
                    self._turn.last_message_ts,
                    "white_check_mark",
                )
            except Exception:
                logger.debug("Reaction failed", exc_info=True)

    def _make_tool_use_blocks(
        self,
        tool_name: str,
        summary: str,
        input_data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Build Block Kit blocks for a tool use context message."""
        blocks: list[dict[str, Any]] = [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f":hammer_and_wrench: *{tool_name}* {summary}",
                    }
                ],
            }
        ]
        if tool_name in ("Edit", "str_replace_editor") and "old_string" in input_data:
            filename = input_data.get("path", input_data.get("file_path", "file"))
            diff_blocks = self._format_diff(
                input_data.get("old_string", ""),
                input_data.get("new_string", ""),
                filename=filename,
            )
            blocks.extend(diff_blocks)
        return blocks

    def _format_diff(
        self,
        old_string: str,
        new_string: str,
        filename: str = "file",
    ) -> list[dict[str, Any]]:
        """Format an edit as a unified diff in Slack Block Kit blocks."""
        diff_lines = list(
            difflib.unified_diff(
                old_string.splitlines(keepends=True),
                new_string.splitlines(keepends=True),
                fromfile=f"a/{filename}",
                tofile=f"b/{filename}",
            )
        )
        if not diff_lines:
            return [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"_No changes in `{filename}`_"},
                }
            ]

        diff_text = "".join(diff_lines)
        # Prepend header to the full diff text, then split the combined string.
        header = f"*Edit:* `{filename}`\n"
        combined = f"{header}```{diff_text}```"
        chunks = split_text(combined, _MAX_MESSAGE_CHARS)
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": chunk},
            }
            for chunk in chunks
        ]


def _format_tool_result(block: ToolResultBlock) -> tuple[str, list[dict[str, Any]]]:
    """Format a ToolResultBlock into (text, blocks). Returns empty blocks if nothing to show."""
    content = block.content
    if not content:
        return "", []
    if isinstance(content, str):
        preview = content[:200] + ("..." if len(content) > 200 else "")
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
