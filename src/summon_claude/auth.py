"""Auth token generation and verification for session authentication."""

from __future__ import annotations

import hmac
import logging
import secrets
import string
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from summon_claude.registry import _MAX_FAILED_ATTEMPTS, SessionRegistry

logger = logging.getLogger(__name__)

_SHORT_CODE_CHARS = string.ascii_uppercase + string.digits
_SHORT_CODE_LEN = 6
_TOKEN_TTL_MINUTES = 5


@dataclass(frozen=True)
class SessionAuth:
    """Authentication token pair for a summon session."""

    token: str
    short_code: str
    session_id: str
    expires_at: datetime


async def generate_session_token(
    registry: SessionRegistry,
    session_id: str,
    cwd: str,
) -> SessionAuth:
    """Generate a cryptographic token and human-friendly short code for a session.

    Stores the mapping in SQLite so any process can verify the short code
    (since Socket Mode load-balances slash commands across connections).
    """
    token = secrets.token_urlsafe(32)
    short_code = _generate_short_code()
    expires_at = datetime.now(UTC) + timedelta(minutes=_TOKEN_TTL_MINUTES)

    await registry.store_pending_token(
        short_code=short_code,
        token=token,
        session_id=session_id,
        cwd=cwd,
        expires_at=expires_at.isoformat(),
    )

    logger.debug("Generated auth token for session %s", session_id)
    return SessionAuth(
        token=token,
        short_code=short_code,
        session_id=session_id,
        expires_at=expires_at,
    )


async def verify_short_code(registry: SessionRegistry, code: str) -> SessionAuth | None:
    """Verify a short code and return the SessionAuth if valid.

    Uses constant-time comparison across all pending tokens to prevent
    timing side-channel attacks. Atomically deletes the token on success —
    concurrent callers cannot both succeed for the same code (no TOCTOU race).
    """
    code = code.strip().upper()
    now = datetime.now(UTC)

    # Fetch all non-expired pending tokens (small set, typically 0-3)
    all_pending = await registry.get_all_pending_tokens()

    # Iterate ALL entries unconditionally — no early exit — to prevent
    # timing side-channels from revealing which entry matched.
    match = None
    found_expired = False
    for entry in all_pending:
        codes_equal = hmac.compare_digest(entry["short_code"].encode(), code.encode())
        if codes_equal:
            if datetime.fromisoformat(entry["expires_at"]) > now:
                if entry.get("failed_attempts", 0) < _MAX_FAILED_ATTEMPTS:
                    match = entry
            else:
                found_expired = True
            # Do NOT break — always iterate all entries for constant-time behavior

    if not match:
        if found_expired:
            await registry.delete_pending_token(code)
        else:
            await registry.record_failed_auth_attempt(code)
        logger.debug("Auth short code not found, expired, or locked out")
        return None

    # Atomically consume it — concurrent callers cannot both succeed
    consumed = await registry.atomic_consume_pending_token(code, now.isoformat())
    if not consumed:
        return None

    expires_at = datetime.fromisoformat(consumed["expires_at"])
    logger.info("Auth short code verified for session %s", consumed["session_id"])

    return SessionAuth(
        token=consumed["token"],
        short_code=code,
        session_id=consumed["session_id"],
        expires_at=expires_at,
    )


def _generate_short_code() -> str:
    """Generate a random 6-character alphanumeric code."""
    return "".join(secrets.choice(_SHORT_CODE_CHARS) for _ in range(_SHORT_CODE_LEN))
