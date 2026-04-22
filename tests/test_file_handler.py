"""Tests for summon_claude.file_handler — classify, sanitize, download, prepare."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.file_handler import (
    MAX_FILE_SIZE,
    classify_file,
    download_file,
    prepare_image_content,
    prepare_text_content,
    sanitize_filename,
)

_SLACK_URL = "https://files.slack.com/files-pri/T0000-F0000/file"

# ---------------------------------------------------------------------------
# classify_file
# ---------------------------------------------------------------------------


class TestClassifyFile:
    @pytest.mark.parametrize(
        ("filename", "mimetype", "expected"),
        [
            ("script.py", "text/plain", "text"),
            ("data.json", "application/json", "text"),
            ("readme.md", "text/markdown", "text"),
            ("server.ts", "application/typescript", "text"),
            ("Makefile.sh", "text/x-sh", "text"),
            ("photo.png", "image/png", "image"),
            ("diagram.jpg", "image/jpeg", "image"),
            ("anim.gif", "image/gif", "image"),
            ("shot.webp", "image/webp", "image"),
            ("snapshot.jpeg", "image/jpeg", "image"),
            ("binary.exe", "application/octet-stream", "unsupported"),
            ("archive.zip", "application/zip", "unsupported"),
            ("unknown.xyz", "application/octet-stream", "unsupported"),
        ],
    )
    def test_extension_based_classification(self, filename, mimetype, expected):
        assert classify_file(filename, mimetype) == expected

    def test_image_mimetype_fallback(self):
        """Unknown extension but image/* MIME → image."""
        assert classify_file("screenshot.bmp", "image/bmp") == "image"

    def test_text_mimetype_fallback(self):
        """Unknown extension but text/* MIME → text."""
        assert classify_file("logfile", "text/plain") == "text"

    def test_exe_with_octet_stream_is_unsupported(self):
        """Unknown extension with non-text/non-image MIME type is unsupported."""
        assert classify_file("malware.exe", "application/octet-stream") == "unsupported"

    def test_empty_filename_falls_back_to_mimetype(self):
        """No extension → fall back to MIME type."""
        assert classify_file("noext", "image/png") == "image"
        assert classify_file("noext", "text/csv") == "text"
        assert classify_file("noext", "application/octet-stream") == "unsupported"


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    def test_replaces_forward_slash(self):
        assert "/" not in sanitize_filename("path/to/file.py")

    def test_replaces_backslash(self):
        assert "\\" not in sanitize_filename("path\\to\\file.py")

    def test_removes_newlines(self):
        result = sanitize_filename("file\nname.py")
        assert "\n" not in result
        assert "\r" not in result

    def test_truncates_at_200_chars(self):
        long_name = "a" * 250
        assert len(sanitize_filename(long_name)) == 200

    def test_short_filename_unchanged(self):
        assert sanitize_filename("hello.py") == "hello.py"

    def test_path_traversal_neutralised(self):
        result = sanitize_filename("../../etc/passwd")
        assert "/" not in result
        assert result == ".._.._etc_passwd"


# ---------------------------------------------------------------------------
# download_file
# ---------------------------------------------------------------------------


class TestDownloadFile:
    def _make_mock_session(self, chunks: list[bytes]) -> MagicMock:
        """Build a nested async context-manager mock simulating aiohttp."""
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()

        async def iter_chunks(chunk_size):
            for chunk in chunks:
                yield chunk

        mock_resp.content.iter_chunked = iter_chunks
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_get = MagicMock()
        mock_get.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_get.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_get)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        return mock_session

    async def test_downloads_and_returns_bytes(self):
        data = b"hello world"
        mock_session = self._make_mock_session([data])

        with patch("summon_claude.file_handler.aiohttp.ClientSession", return_value=mock_session):
            result = await download_file(_SLACK_URL, "xoxb-token")

        assert result == data

    async def test_concatenates_multiple_chunks(self):
        chunks = [b"foo", b"bar", b"baz"]
        mock_session = self._make_mock_session(chunks)

        with patch("summon_claude.file_handler.aiohttp.ClientSession", return_value=mock_session):
            result = await download_file(_SLACK_URL, "xoxb-token")

        assert result == b"foobarbaz"

    async def test_raises_on_size_exceeded(self):
        # One chunk that is exactly at limit, second chunk pushes over
        big_chunk = b"x" * (MAX_FILE_SIZE - 1)
        over_chunk = b"y" * 2
        mock_session = self._make_mock_session([big_chunk, over_chunk])

        with (
            patch("summon_claude.file_handler.aiohttp.ClientSession", return_value=mock_session),
            pytest.raises(ValueError, match="exceeds maximum size"),
        ):
            await download_file(_SLACK_URL, "xoxb-token")

    async def test_custom_max_size_enforced(self):
        """Custom max_size of 10 bytes is respected."""
        mock_session = self._make_mock_session([b"x" * 11])

        with (
            patch("summon_claude.file_handler.aiohttp.ClientSession", return_value=mock_session),
            pytest.raises(ValueError),
        ):
            await download_file(_SLACK_URL, "xoxb-token", max_size=10)

    async def test_rejects_non_slack_url(self):
        """download_file rejects URLs not on files.slack.com."""
        with pytest.raises(ValueError, match="Unexpected file URL scheme or host"):
            await download_file("https://evil.example.com/steal-token", "xoxb-token")

    async def test_rejects_http_url(self):
        """download_file rejects non-HTTPS Slack URLs."""
        with pytest.raises(ValueError, match="Unexpected file URL scheme or host"):
            await download_file("http://files.slack.com/files-pri/T0000-F0000/file", "xoxb-token")

    async def test_rejects_userinfo_authority_bypass(self):
        """download_file rejects @-authority URLs that spoof the host prefix."""
        with pytest.raises(ValueError, match="Unexpected file URL scheme or host"):
            await download_file("https://files.slack.com@evil.com/path", "xoxb-token")


# ---------------------------------------------------------------------------
# prepare_text_content
# ---------------------------------------------------------------------------


class TestPrepareTextContent:
    def test_wraps_in_code_fence(self):
        result = prepare_text_content("hello.py", b"print('hi')")
        assert "```" in result
        assert "print('hi')" in result

    def test_includes_sanitized_filename(self):
        result = prepare_text_content("path/to/file.py", b"x = 1")
        # Sanitized: slashes → underscores
        assert "path_to_file.py" in result

    def test_utf8_decode(self):
        result = prepare_text_content("f.txt", "hello café".encode())
        assert "café" in result

    def test_truncates_at_max_chars(self):
        big = b"a" * 200_000
        result = prepare_text_content("big.txt", big)
        assert "[truncated]" in result
        # Output must not vastly exceed the char cap
        assert len(result) < 200_000

    def test_short_file_not_truncated(self):
        result = prepare_text_content("small.py", b"x = 1")
        assert "[truncated]" not in result

    def test_invalid_utf8_replaced(self):
        # Invalid byte 0xff is replaced, not raised
        result = prepare_text_content("bad.txt", b"\xff\xfe")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# prepare_image_content
# ---------------------------------------------------------------------------


class TestPrepareImageContent:
    def test_returns_two_blocks(self):
        blocks = prepare_image_content("photo.png", b"\x89PNG", "image/png")
        assert len(blocks) == 2

    def test_first_block_is_text(self):
        blocks = prepare_image_content("photo.png", b"\x89PNG", "image/png")
        assert blocks[0]["type"] == "text"
        assert "photo.png" in blocks[0]["text"]

    def test_second_block_is_image(self):
        blocks = prepare_image_content("photo.png", b"\x89PNG", "image/png")
        assert blocks[1]["type"] == "image"
        src = blocks[1]["source"]
        assert src["type"] == "base64"

    def test_base64_encoded_correctly(self):
        data = b"fake image bytes"
        blocks = prepare_image_content("x.png", data, "image/png")
        encoded = blocks[1]["source"]["data"]
        assert base64.standard_b64decode(encoded) == data

    def test_media_type_from_extension(self):
        """Extension takes precedence over provided mimetype for MIME resolution."""
        blocks = prepare_image_content("img.png", b"data", "application/octet-stream")
        assert blocks[1]["source"]["media_type"] == "image/png"

    def test_media_type_fallback_defaults_to_png_for_unknown_mime(self):
        """Unknown extension with non-allowlisted MIME defaults to image/png."""
        blocks = prepare_image_content("img.bmp", b"data", "image/bmp")
        assert blocks[1]["source"]["media_type"] == "image/png"

    def test_media_type_fallback_allows_known_mime(self):
        """Unknown extension with allowlisted MIME is passed through."""
        blocks = prepare_image_content("img.bmp", b"data", "image/jpeg")
        assert blocks[1]["source"]["media_type"] == "image/jpeg"

    def test_filename_sanitized_in_text_block(self):
        blocks = prepare_image_content("path/to/img.png", b"data", "image/png")
        # Sanitized name (slashes replaced) appears in text block
        assert "/" not in blocks[0]["text"]
