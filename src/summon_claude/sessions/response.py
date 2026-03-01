"""Response pipeline stub — get_tool_primary_arg and split_text.

Full ResponseStreamer + ContentDisplay merge happens in Task 2.3.
"""

from __future__ import annotations

from typing import Any

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

    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        # If text contains fences, leave headroom for a potential closure suffix.
        # Only reduce the limit when fences exist to avoid penalizing plain text.
        effective_limit = limit
        has_fences = "```" in text
        if has_fences:
            effective_limit = max(limit - _FENCE_OVERHEAD, 1)

        # Try to break at a newline boundary
        split_at = text.rfind("\n", 0, effective_limit)
        if split_at == -1:
            split_at = effective_limit
        chunk = text[:split_at]
        rest = text[split_at:]

        # Check if we're splitting inside an open code fence.
        # Count triple-backtick fences in the chunk — odd count means unclosed.
        if has_fences:
            fence_count = chunk.count("```")
            if fence_count % 2 == 1:
                # Close the code block at end of this chunk, re-open in next
                chunk += "\n```"
                rest = "```\n" + rest

        chunks.append(chunk)
        text = rest
    return chunks
