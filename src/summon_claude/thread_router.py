"""ThreadRouter — decides where each message goes: main channel, turn thread, or subagent thread."""

from __future__ import annotations

from typing import Any

from summon_claude.providers.base import ChatProvider, MessageRef

_MAX_SUBAGENT_THREADS = 100


class ThreadRouter:
    """Central routing logic for threaded Slack UX.

    Tracks per-turn threads and per-subagent threads, routing messages
    to the correct context based on content type and parent_tool_use_id.
    """

    def __init__(self, provider: ChatProvider, channel_id: str) -> None:
        self._provider = provider
        self._channel_id = channel_id
        self._current_turn_ts: str | None = None
        self._current_turn_ref: MessageRef | None = None
        # tool_use_id -> thread_ts. Keys come from the Claude SDK (trusted).
        self._subagent_threads: dict[str, str] = {}
        self._current_turn_number: int = 0
        self._tool_call_count: int = 0
        self._files_touched: list[str] = []

    @property
    def channel_id(self) -> str:
        return self._channel_id

    @property
    def provider(self) -> ChatProvider:
        """Expose the underlying provider for direct access (e.g., MCP tools)."""
        return self._provider

    async def start_turn(self, turn_number: int) -> str:
        """Create turn thread starter message, return thread_ts."""
        self._current_turn_number = turn_number
        self._tool_call_count = 0
        self._files_touched = []
        ref = await self._provider.post_message(
            self._channel_id,
            f"\U0001f527 Turn {turn_number}: Processing...",
        )
        self._current_turn_ts = ref.ts
        self._current_turn_ref = ref
        return ref.ts

    async def update_turn_summary(self, summary: str) -> None:
        """Update the current turn's thread starter message with a summary."""
        if self._current_turn_ref:
            await self._provider.update_message(
                self._channel_id,
                self._current_turn_ref.ts,
                f"\U0001f527 Turn {self._current_turn_number}: {summary}",
            )

    async def start_subagent_thread(self, tool_use_id: str, description: str) -> str:
        """Create a dedicated subagent thread, return thread_ts."""
        # Evict oldest entries if we've hit the cap to prevent unbounded growth
        if len(self._subagent_threads) >= _MAX_SUBAGENT_THREADS:
            # dict preserves insertion order (Python 3.7+); drop oldest half
            keys = list(self._subagent_threads)
            for key in keys[: len(keys) // 2]:
                del self._subagent_threads[key]

        ref = await self._provider.post_message(
            self._channel_id,
            f"\U0001f916 Subagent: {description}",
        )
        self._subagent_threads[tool_use_id] = ref.ts
        return ref.ts

    async def post_to_main(
        self, text: str, *, blocks: list[dict[str, Any]] | None = None
    ) -> MessageRef:
        """Post directly to the main channel (no thread)."""
        return await self._provider.post_message(self._channel_id, text, blocks=blocks)

    async def post_to_turn_thread(
        self, text: str, *, blocks: list[dict[str, Any]] | None = None
    ) -> MessageRef:
        """Post to the current turn's thread."""
        if not self._current_turn_ts:
            return await self.post_to_main(text, blocks=blocks)
        return await self._provider.post_message(
            self._channel_id,
            text,
            blocks=blocks,
            thread_ts=self._current_turn_ts,
        )

    async def post_to_subagent_thread(
        self,
        tool_use_id: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> MessageRef:
        """Post to a subagent's dedicated thread."""
        thread_ts = self._subagent_threads.get(tool_use_id)
        if not thread_ts:
            return await self.post_to_turn_thread(text, blocks=blocks)
        return await self._provider.post_message(
            self._channel_id, text, blocks=blocks, thread_ts=thread_ts
        )

    async def post_permission(
        self,
        text: str,
        blocks: list[dict[str, Any]],
        *,
        thread_ts: str | None = None,
    ) -> MessageRef:
        """Post permission request with reply_broadcast=True and <!channel>."""
        ts = thread_ts or self._current_turn_ts
        return await self._provider.post_message(
            self._channel_id,
            f"<!channel> {text}",
            blocks=blocks,
            thread_ts=ts,
            reply_broadcast=bool(ts),
        )

    async def post_permission_ephemeral(
        self,
        user_id: str,
        text: str,
        blocks: list[dict[str, Any]],
    ) -> None:
        """Post an ephemeral permission/question prompt visible only to user_id."""
        await self._provider.post_ephemeral(self._channel_id, user_id, text, blocks=blocks)

    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Update an existing message (delegates to provider)."""
        await self._provider.update_message(channel, ts, text, blocks=blocks)

    async def add_reaction(self, channel: str, ts: str, emoji: str) -> None:
        """Add a reaction to a message (delegates to provider)."""
        await self._provider.add_reaction(channel, ts, emoji)

    def record_tool_call(self, _tool_name: str, tool_input: dict[str, Any]) -> None:
        """Track tool calls for turn summary generation."""
        self._tool_call_count += 1
        for key in ("file_path", "path", "command"):
            if key in tool_input and isinstance(tool_input[key], str):
                path = tool_input[key]
                if "/" in path and path not in self._files_touched:
                    self._files_touched.append(path)

    def generate_turn_summary(self) -> str:
        """Build a concise summary string for the turn starter message."""
        parts: list[str] = []
        if self._tool_call_count:
            suffix = "s" if self._tool_call_count != 1 else ""
            parts.append(f"{self._tool_call_count} tool call{suffix}")
        if self._files_touched:
            short_names = [p.rsplit("/", 1)[-1] for p in self._files_touched[:3]]
            if len(self._files_touched) > 3:
                short_names.append(f"+{len(self._files_touched) - 3} more")
            parts.append(", ".join(short_names))
        return " \u00b7 ".join(parts) if parts else "Processing..."

    async def upload_to_turn_thread(
        self,
        content: str,
        filename: str,
        *,
        title: str | None = None,
    ) -> None:
        """Upload a file to the current turn's thread."""
        await self._provider.upload_file(
            self._channel_id,
            content,
            filename,
            title=title,
            thread_ts=self._current_turn_ts,
        )
