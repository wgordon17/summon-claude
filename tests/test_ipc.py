"""Tests for summon_claude.ipc — IPC framing protocol."""

from __future__ import annotations

import asyncio
import socket
import struct

import pytest

from summon_claude.ipc import MAX_MESSAGE_SIZE, recv_msg, send_msg


class _StreamPair:
    """Holds a connected (reader, writer) pair and keeps all transports alive.

    socket.socketpair() creates two connected OS-level sockets (rsock, wsock).
    asyncio.open_connection(sock=...) wraps each socket in a (StreamReader,
    StreamWriter) pair.  Writing to wsock's StreamWriter sends data through the
    kernel to rsock's StreamReader.

    The unused halves (_r_writer and _w_reader) must be kept alive for the
    duration of the test; if they are garbage-collected the underlying transport
    is closed, which causes readexactly() to see EOF immediately.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        _r_writer: asyncio.StreamWriter,
        _w_reader: asyncio.StreamReader,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self._r_writer = _r_writer
        self._w_reader = _w_reader


async def _make_stream_pair() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Return a (reader, writer) pair connected via an in-process socketpair.

    Data written to *writer* can be read back from *reader*, making this
    suitable for testing the IPC framing primitives without a real network or
    Unix socket on disk.
    """
    rsock, wsock = socket.socketpair()
    # Open rsock for reading; keep its writer half alive via _StreamPair.
    reader, _r_writer = await asyncio.open_connection(sock=rsock)
    # Open wsock for writing; keep its reader half alive via _StreamPair.
    _w_reader, writer = await asyncio.open_connection(sock=wsock)
    # Pin the unused halves onto the writer so they survive until writer.close().
    pair = _StreamPair(reader, writer, _r_writer, _w_reader)
    writer._test_pair = pair  # type: ignore[attr-defined]
    return reader, writer


class TestSendRecvRoundTrip:
    """Round-trip tests: send_msg then recv_msg returns the same data."""

    async def test_simple_dict(self):
        """A basic dict survives the send/recv round-trip."""
        reader, writer = await _make_stream_pair()
        data = {"type": "hello", "session_id": "abc-123", "value": 42}

        await send_msg(writer, data)
        result = await recv_msg(reader)

        assert result == data
        writer.close()

    async def test_empty_dict(self):
        """An empty dict is a valid message."""
        reader, writer = await _make_stream_pair()

        await send_msg(writer, {})
        result = await recv_msg(reader)

        assert result == {}
        writer.close()

    async def test_nested_dict(self):
        """Nested structures and unicode survive round-trip."""
        reader, writer = await _make_stream_pair()
        data = {
            "event": "message",
            "payload": {"text": "Hello \u2603", "numbers": [1, 2, 3]},
        }

        await send_msg(writer, data)
        result = await recv_msg(reader)

        assert result == data
        writer.close()

    async def test_multiple_messages_in_sequence(self):
        """Multiple messages can be sent and received in order."""
        reader, writer = await _make_stream_pair()
        messages = [
            {"seq": 0, "data": "first"},
            {"seq": 1, "data": "second"},
            {"seq": 2, "data": "third"},
        ]

        for msg in messages:
            await send_msg(writer, msg)

        for expected in messages:
            result = await recv_msg(reader)
            assert result == expected

        writer.close()

    async def test_string_with_embedded_newlines(self):
        """Strings with embedded newlines are preserved (framing uses length, not delimiters)."""
        reader, writer = await _make_stream_pair()
        data = {"text": "line one\nline two\r\nline three"}

        await send_msg(writer, data)
        result = await recv_msg(reader)

        assert result == data
        writer.close()

    async def test_boolean_and_null_values(self):
        """JSON booleans and null survive the round-trip."""
        reader, writer = await _make_stream_pair()
        data = {"active": True, "done": False, "nothing": None}

        await send_msg(writer, data)
        result = await recv_msg(reader)

        assert result == data
        writer.close()

    async def test_integer_and_float_values(self):
        """Numeric values (int and float) survive the round-trip."""
        reader, writer = await _make_stream_pair()
        data = {"count": 42, "rate": 3.14, "negative": -7}

        await send_msg(writer, data)
        result = await recv_msg(reader)

        assert result == data
        writer.close()

    async def test_large_message_well_within_limit(self):
        """A 100 KB message round-trips correctly."""
        reader, writer = await _make_stream_pair()
        data = {"payload": "x" * (100 * 1024)}

        await send_msg(writer, data)
        result = await recv_msg(reader)

        assert result == data
        writer.close()


class TestOversizedMessageRejection:
    """recv_msg must raise ValueError for messages claiming to exceed 1 MiB."""

    async def test_oversized_header_raises_value_error(self):
        """A 4-byte header declaring length > MAX_MESSAGE_SIZE raises ValueError."""
        reader = asyncio.StreamReader()
        oversized_length = MAX_MESSAGE_SIZE + 1
        # Feed just the header — recv_msg should raise before reading the payload
        reader.feed_data(struct.pack(">I", oversized_length))

        with pytest.raises(ValueError, match="Message too large"):
            await recv_msg(reader)

    async def test_exact_max_size_is_accepted(self):
        """A message exactly at MAX_MESSAGE_SIZE is accepted (boundary condition)."""
        # Build a JSON payload that is exactly MAX_MESSAGE_SIZE bytes.
        prefix = b'{"k": "'
        suffix = b'"}'
        padding = b"x" * (MAX_MESSAGE_SIZE - len(prefix) - len(suffix))
        payload = prefix + padding + suffix
        assert len(payload) == MAX_MESSAGE_SIZE

        # Feed the frame directly into a StreamReader (no socket needed for this test)
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", MAX_MESSAGE_SIZE) + payload)

        result = await recv_msg(reader)
        assert result["k"] == "x" * (MAX_MESSAGE_SIZE - len(prefix) - len(suffix))

    async def test_max_uint32_header_raises_value_error(self):
        """A header claiming 4 GiB (max uint32) is rejected without reading data."""
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", 0xFFFFFFFF))

        with pytest.raises(ValueError, match="Message too large"):
            await recv_msg(reader)

    async def test_error_message_includes_size_info(self):
        """ValueError message includes the declared size for diagnostics."""
        reader = asyncio.StreamReader()
        oversized = MAX_MESSAGE_SIZE + 100
        reader.feed_data(struct.pack(">I", oversized))

        with pytest.raises(ValueError) as exc_info:
            await recv_msg(reader)

        assert str(oversized) in str(exc_info.value)


class TestEmptyPayload:
    """Edge cases around minimal/empty JSON payloads."""

    async def test_connection_closed_before_header_raises(self):
        """IncompleteReadError is raised when the stream closes mid-header."""
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x00\x00")  # Only 2 bytes instead of 4
        reader.feed_eof()

        with pytest.raises(asyncio.IncompleteReadError):
            await recv_msg(reader)

    async def test_connection_closed_before_payload_raises(self):
        """IncompleteReadError is raised when the stream closes after header."""
        reader = asyncio.StreamReader()
        # Header says 10 bytes, but we only feed 3
        reader.feed_data(struct.pack(">I", 10) + b"abc")
        reader.feed_eof()

        with pytest.raises(asyncio.IncompleteReadError):
            await recv_msg(reader)

    async def test_connection_closed_with_no_data_raises(self):
        """IncompleteReadError is raised when the stream is immediately closed (clean EOF)."""
        reader = asyncio.StreamReader()
        reader.feed_eof()

        with pytest.raises(asyncio.IncompleteReadError):
            await recv_msg(reader)

    async def test_connection_closed_after_3_header_bytes_raises(self):
        """IncompleteReadError is raised for truncated headers of any length < 4."""
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x00\x00\x00")  # 3 bytes — one short of a full header
        reader.feed_eof()

        with pytest.raises(asyncio.IncompleteReadError):
            await recv_msg(reader)
