"""Markdown-aware text splitting for Slack ``type: markdown`` blocks.

Splits markdown content at structural boundaries (headings, paragraphs,
code fences, tables) while respecting a per-chunk character limit.

Different from ``response.split_text`` which handles Slack ``mrkdwn`` format
with a 3K char limit and basic code-fence awareness.  ``split_markdown``
targets standard markdown in ``type: markdown`` blocks with a 12K default
limit and full heading/table/fence protection.  Both functions coexist.
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^#{1,6} ", re.MULTILINE)
_FENCE_RE = re.compile(r"^```", re.MULTILINE)
_TABLE_LINE_RE = re.compile(r"^\|", re.MULTILINE)


def split_markdown(text: str, limit: int = 12000) -> list[str]:
    """Split markdown text into chunks that each fit within *limit* chars.

    Respects markdown structure:
    - Headings stay with their content
    - Code fence blocks (````` ``` `````) are never split (unless > 2x limit)
    - Tables are kept whole
    - Paragraphs split at blank lines
    """
    if len(text) <= limit:
        return [text]

    blocks = _parse_blocks(text)
    return _pack_blocks(blocks, limit)


def _parse_blocks(text: str) -> list[str]:  # noqa: PLR0912, PLR0915
    """Parse text into structural blocks: headings+body, fences, tables, paragraphs."""
    blocks: list[str] = []
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # Code fence block
        if line.startswith("```"):
            fence_lines = [line]
            i += 1
            while i < len(lines):
                fence_lines.append(lines[i])
                if lines[i].startswith("```") and len(fence_lines) > 1:
                    i += 1
                    break
                i += 1
            blocks.append("\n".join(fence_lines))
            continue

        # Heading — collect heading + body until next heading of same/higher level
        if _HEADING_RE.match(line):
            level = len(line) - len(line.lstrip("#"))
            heading_lines = [line]
            i += 1
            while i < len(lines):
                next_line = lines[i]
                # Stop at next heading of same or higher level
                if _HEADING_RE.match(next_line):
                    next_level = len(next_line) - len(next_line.lstrip("#"))
                    if next_level <= level:
                        break
                # Stop at code fences (they become their own block)
                if next_line.startswith("```"):
                    break
                heading_lines.append(next_line)
                i += 1
            blocks.append("\n".join(heading_lines))
            continue

        # Table block
        if line.startswith("|"):
            table_lines = [line]
            i += 1
            while i < len(lines) and lines[i].startswith("|"):
                table_lines.append(lines[i])
                i += 1
            blocks.append("\n".join(table_lines))
            continue

        # Paragraph — collect until blank line, heading, fence, or table
        para_lines = [line]
        i += 1
        while i < len(lines):
            next_line = lines[i]
            if not next_line.strip():
                # Include the blank line in this block
                para_lines.append(next_line)
                i += 1
                break
            if next_line.startswith(("```", "|")):
                break
            if _HEADING_RE.match(next_line):
                break
            para_lines.append(next_line)
            i += 1
        blocks.append("\n".join(para_lines))

    return blocks


def _pack_blocks(blocks: list[str], limit: int) -> list[str]:
    """Greedily pack blocks into chunks up to *limit* chars."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for block in blocks:
        block_len = len(block)

        # Block fits in current chunk
        separator = 1 if current else 0
        if current_len + block_len + separator <= limit:
            current.append(block)
            current_len += block_len + separator
            continue

        # Flush current chunk if non-empty
        if current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0

        # Block fits alone in a new chunk
        if block_len <= limit:
            current.append(block)
            current_len = block_len
            continue

        # Oversized block — needs splitting
        split_parts = _split_oversized_block(block, limit)
        for part in split_parts[:-1]:
            chunks.append(part)
        # Last part starts the next chunk
        current.append(split_parts[-1])
        current_len = len(split_parts[-1])

    if current:
        chunks.append("\n".join(current))

    return chunks


def _split_oversized_block(block: str, limit: int) -> list[str]:
    """Split a block that exceeds *limit*.

    For code blocks: if < 2x limit, keep whole. Otherwise split at newlines
    with fence repair. For other blocks: split at blank lines, then newlines.
    """
    is_fence = block.startswith("```")

    # Code block under 2x limit — keep whole
    if is_fence and len(block) < 2 * limit:
        return [block]

    # Code block over 2x limit — split with fence repair
    if is_fence:
        return _split_code_block(block, limit)

    # Non-code block — split at paragraph boundaries, then newlines
    parts: list[str] = []
    remaining = block

    while len(remaining) > limit:
        # Try blank-line split
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at == -1:
            # Try newline split
            split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            # Character boundary
            split_at = limit

        parts.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    if remaining:
        parts.append(remaining)

    return parts


def _split_code_block(block: str, limit: int) -> list[str]:
    """Split an oversized code block with fence repair."""
    lines = block.split("\n")
    # First line is the opening fence (possibly with language)
    opener = lines[0]
    # Last line should be closing fence
    closer = "```"

    # Content lines (between fences)
    content_lines = lines[1:-1] if lines[-1].startswith("```") else lines[1:]

    parts: list[str] = []
    current_lines: list[str] = [opener]
    current_len = len(opener)

    # Reserve space for closing fence
    effective_limit = limit - len(closer) - 1

    for cline in content_lines:
        line_len = len(cline) + 1  # +1 for newline
        if current_len + line_len > effective_limit and len(current_lines) > 1:
            # Close this chunk
            current_lines.append(closer)
            parts.append("\n".join(current_lines))
            # Start new chunk with reopened fence
            current_lines = [opener]
            current_len = len(opener)
        current_lines.append(cline)
        current_len += line_len

    # Final chunk
    current_lines.append(closer)
    parts.append("\n".join(current_lines))

    return parts
