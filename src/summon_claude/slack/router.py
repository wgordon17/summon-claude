"""ThreadRouter — thread management for Slack message routing (Layer 2)."""

from __future__ import annotations

from typing import Any

from summon_claude.slack.client import MessageRef, SlackClient
from summon_claude.slack.formatting import markdown_to_mrkdwn

_MAX_SUBAGENT_THREADS = 100


class ThreadRouter:
    """Thread management for Slack message routing (Layer 2).

    Tracks active thread and subagent threads. ``client`` is public —
    callers may use it directly for simple pass-through operations.
    Knows about threads, NOT about turns.
    """

    def __init__(self, client: SlackClient) -> None:
        self.client: SlackClient = client
        self.active_thread_ts: str | None = None
        self.active_thread_ref: MessageRef | None = None
        self.subagent_threads: dict[str, str] = {}  # tool_use_id → thread_ts

    # --- Thread lifecycle ---

    def set_active_thread(self, ts: str, ref: MessageRef) -> None:
        """Record the active thread ts and ref."""
        self.active_thread_ts = ts
        self.active_thread_ref = ref

    # --- Raw posting (no conversion) ---

    async def _post_raw(
        self,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
    ) -> MessageRef:
        """Post directly via the client without any text conversion."""
        return await self.client.post(text, blocks=blocks, thread_ts=thread_ts)

    # --- Explicit-thread posting with mrkdwn conversion ---

    async def post_to_thread(
        self,
        text: str,
        *,
        thread_ts: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> MessageRef:
        """Post to an explicit thread with markdown-to-mrkdwn conversion."""
        return await self._post_raw(markdown_to_mrkdwn(text), blocks=blocks, thread_ts=thread_ts)

    # --- State-aware posting with mrkdwn conversion ---

    async def post_to_main(
        self,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> MessageRef:
        """Post directly to the main channel."""
        return await self._post_raw(markdown_to_mrkdwn(text), blocks=blocks)

    async def post_to_active_thread(
        self, text: str, *, blocks: list[dict[str, Any]] | None = None
    ) -> MessageRef:
        """Post to the current active thread; falls back to main if no active thread."""
        converted = markdown_to_mrkdwn(text)
        if not self.active_thread_ts:
            return await self._post_raw(converted, blocks=blocks)
        return await self._post_raw(converted, blocks=blocks, thread_ts=self.active_thread_ts)

    async def post_to_subagent_thread(
        self,
        tool_use_id: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> MessageRef:
        """Post to a subagent's dedicated thread."""
        converted = markdown_to_mrkdwn(text)
        thread_ts = self.subagent_threads.get(tool_use_id)
        if not thread_ts:
            if not self.active_thread_ts:
                return await self._post_raw(converted, blocks=blocks)
            return await self._post_raw(converted, blocks=blocks, thread_ts=self.active_thread_ts)
        return await self._post_raw(converted, blocks=blocks, thread_ts=thread_ts)

    async def update(
        self,
        ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Update an existing message with mrkdwn conversion."""
        await self.client.update(ts, markdown_to_mrkdwn(text), blocks=blocks)

    async def post_markdown_to_thread(
        self,
        markdown: str,
        *,
        thread_ts: str,
    ) -> MessageRef:
        """Post a type:markdown block with raw content (no mrkdwn conversion).

        The ``text`` fallback is the raw markdown — readable in notifications
        even without rendering.
        """
        blocks = [{"type": "markdown", "text": markdown}]
        return await self._post_raw(markdown, blocks=blocks, thread_ts=thread_ts)

    # --- File uploads (no text conversion — file content is not markdown) ---

    async def upload(
        self,
        content: str,
        filename: str,
        *,
        thread_ts: str,
        title: str | None = None,
        snippet_type: str | None = None,
    ) -> None:
        """Upload a file to an explicit thread."""
        await self.client.upload(
            content,
            filename,
            title=title or filename,
            thread_ts=thread_ts,
            snippet_type=snippet_type,
        )

    async def upload_to_active_thread(
        self,
        content: str,
        filename: str,
        *,
        title: str | None = None,
        snippet_type: str | None = None,
    ) -> None:
        """Upload a file to the current active thread."""
        await self.client.upload(
            content,
            filename,
            title=title or filename,
            thread_ts=self.active_thread_ts,
            snippet_type=snippet_type,
        )

    # --- Subagent management ---

    async def start_subagent_thread(self, tool_use_id: str, description: str) -> str:
        """Create a dedicated subagent thread, return thread_ts."""
        # Evict oldest entries if we've hit the cap to prevent unbounded growth
        if len(self.subagent_threads) >= _MAX_SUBAGENT_THREADS:
            # dict preserves insertion order (Python 3.7+); drop oldest half
            keys = list(self.subagent_threads)
            for key in keys[: len(keys) // 2]:
                del self.subagent_threads[key]

        # Convert description inline; post via _post_raw to avoid double conversion
        ref = await self._post_raw(
            f"\U0001f916 Subagent: {markdown_to_mrkdwn(description)}",
        )
        self.subagent_threads[tool_use_id] = ref.ts
        return ref.ts
