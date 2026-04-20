"""File classification, download, and content preparation for inbound Slack file uploads.

Handles text files (injected as formatted code blocks) and images (base64 multimodal
content blocks for the Claude API).  All download URLs are used transiently — they
are never logged or stored.
"""

from __future__ import annotations

import base64
import logging
from pathlib import PurePosixPath
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Maximum file size to download (hard cap — reject before download)
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Warn but allow files larger than this
WARN_FILE_SIZE = 1 * 1024 * 1024  # 1 MB

# Maximum decoded chars to include in text injection (prevent prompt flooding)
_MAX_TEXT_CHARS = 100_000

# Supported text file extensions
TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".md",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".csv",
        ".log",
        ".sh",
        ".html",
        ".css",
        ".xml",
        ".sql",
        ".go",
        ".rs",
        ".rb",
        ".java",
        ".kt",
        ".swift",
        ".c",
        ".cpp",
        ".h",
        ".jsx",
        ".tsx",
        ".mjs",
        ".cjs",
        ".env",
        ".cfg",
        ".ini",
        ".conf",
        ".tf",
        ".hcl",
    }
)

# Supported image extensions (Claude vision types)
IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

# Map extension to MIME type for content blocks
_IMAGE_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def classify_file(filename: str, mimetype: str) -> str:
    """Classify a file as 'text', 'image', or 'unsupported'.

    Uses extension first, falls back to MIME type prefix for images.
    """
    ext = PurePosixPath(filename).suffix.lower()
    if ext in TEXT_EXTENSIONS:
        return "text"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    # MIME type fallback for images (e.g. Slack snippets)
    if mimetype.startswith("image/"):
        return "image"
    if mimetype.startswith("text/"):
        return "text"
    return "unsupported"


def sanitize_filename(filename: str) -> str:
    """Strip path separators, newlines, and truncate to 200 chars."""
    safe = filename.replace("/", "_").replace("\\", "_").replace("\n", "").replace("\r", "")
    return safe[:200]


async def download_file(url_private: str, token: str, max_size: int = MAX_FILE_SIZE) -> bytes:
    """Download a Slack private file URL with bot token auth.

    Streams with a hard cap at max_size bytes — raises ValueError if exceeded.
    The token is only used in the Authorization header and never logged.
    """
    headers = {"Authorization": f"Bearer {token}"}
    chunks: list[bytes] = []
    total = 0

    async with (
        aiohttp.ClientSession() as session,
        session.get(url_private, headers=headers) as resp,
    ):
        resp.raise_for_status()
        async for chunk in resp.content.iter_chunked(65536):
            total += len(chunk)
            if total > max_size:
                raise ValueError(f"File exceeds maximum size ({max_size // (1024 * 1024)} MB)")
            chunks.append(chunk)

    return b"".join(chunks)


def prepare_text_content(filename: str, content_bytes: bytes) -> str:
    """Format file bytes as a text block for injection into Claude.

    Decodes as UTF-8 (replacing errors) and wraps in a fenced code block.
    Truncates at _MAX_TEXT_CHARS to avoid prompt flooding.
    Never includes the download URL.
    """
    safe_name = sanitize_filename(filename)
    try:
        text = content_bytes.decode("utf-8", errors="replace")
    except Exception:
        text = content_bytes.decode("latin-1", errors="replace")

    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS] + "\n...[truncated]"

    return f"User shared file: {safe_name}\n```\n{text}\n```"


def prepare_image_content(
    filename: str, content_bytes: bytes, mimetype: str
) -> list[dict[str, Any]]:
    """Prepare a multimodal content block list for a Claude API image message.

    Returns a list of two content blocks:
    - A text block describing the file
    - An image block with base64-encoded data

    The returned list is suitable for use as the ``content`` field of an
    Anthropic API user message.
    """
    safe_name = sanitize_filename(filename)
    ext = PurePosixPath(filename).suffix.lower()
    # Prefer known MIME from extension; fall back to provided mimetype
    media_type = _IMAGE_MIME.get(ext, mimetype)

    encoded = base64.standard_b64encode(content_bytes).decode("ascii")
    return [
        {
            "type": "text",
            "text": f"User shared image: {safe_name}",
        },
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": encoded,
            },
        },
    ]
