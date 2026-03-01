"""IPC framing protocol for the single-Bolt daemon architecture.

Messages are framed with a 4-byte big-endian length prefix followed by a
JSON-encoded payload.  This avoids the ambiguity of newline-delimited framing
(newlines may appear inside JSON string values) while remaining simple enough
to implement on both ends with stdlib primitives.

Wire format:
    [4-byte big-endian uint32 length][JSON payload bytes]

The maximum allowed message size is 64 KiB (65_536 bytes).  Any message
that exceeds this limit is rejected by recv_msg to guard against runaway
senders or corrupt streams.  This constant is also used as the
``asyncio.StreamReader`` buffer limit so the two stay in sync.
"""

from __future__ import annotations

import asyncio
import json
import struct

MAX_MESSAGE_SIZE = 65_536  # 64 KiB


async def send_msg(writer: asyncio.StreamWriter, data: dict) -> None:  # type: ignore[type-arg]
    """Encode *data* as JSON and write it to *writer* with a 4-byte length prefix.

    The write is flushed immediately via ``drain()`` so the caller does not
    need to manage buffering.
    """
    payload: bytes = json.dumps(data).encode()
    writer.write(struct.pack(">I", len(payload)) + payload)
    await writer.drain()


_RECV_TIMEOUT: float = 30.0
"""Maximum seconds to wait for a complete IPC message.

Prevents stalled clients from holding a connection indefinitely.
"""


async def recv_msg(reader: asyncio.StreamReader) -> dict:  # type: ignore[type-arg]
    """Read a length-prefixed message from *reader* and return the decoded dict.

    Raises:
        ValueError: If the declared message length exceeds MAX_MESSAGE_SIZE.
        asyncio.IncompleteReadError: If the connection closes before a full
            message is received.
        TimeoutError: If the message is not fully received within ``_RECV_TIMEOUT``.
    """
    header: bytes = await asyncio.wait_for(reader.readexactly(4), timeout=_RECV_TIMEOUT)
    length: int = struct.unpack(">I", header)[0]
    if length > MAX_MESSAGE_SIZE:
        raise ValueError(f"Message too large: {length} bytes (max {MAX_MESSAGE_SIZE})")
    payload: bytes = await asyncio.wait_for(reader.readexactly(length), timeout=_RECV_TIMEOUT)
    return json.loads(payload)  # type: ignore[no-any-return]
