"""Auth token generation and verification for session authentication."""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from summon_claude.sessions.registry import _MAX_FAILED_ATTEMPTS, SessionRegistry

logger = logging.getLogger(__name__)

_TOKEN_TTL_MINUTES = 5
_SPAWN_TOKEN_TTL_SECONDS = 30


@dataclass(frozen=True)
class SessionAuth:
    """Authentication token pair for a summon session."""

    short_code: str
    session_id: str
    expires_at: datetime


@dataclass(frozen=True)
class SpawnAuth:
    """Spawn token for machine-to-machine session creation."""

    token: str
    parent_session_id: str | None
    parent_channel_id: str | None
    target_user_id: str
    cwd: str
    spawn_source: str
    expires_at: datetime


async def generate_session_token(
    registry: SessionRegistry,
    session_id: str,
) -> SessionAuth:
    """Generate a cryptographic token and human-friendly short code for a session.

    Stores the mapping in SQLite so any process can verify the short code
    (since Socket Mode load-balances slash commands across connections).
    """
    short_code = secrets.token_hex(4)
    expires_at = datetime.now(UTC) + timedelta(minutes=_TOKEN_TTL_MINUTES)

    await registry.store_pending_token(
        short_code=short_code,
        session_id=session_id,
        expires_at=expires_at.isoformat(),
    )

    logger.debug("Generated auth token for session %s", session_id)
    return SessionAuth(
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
    code = code.strip().lower()
    now = datetime.now(UTC)

    # Fetch all non-expired pending tokens (small set, typically 0-3)
    all_pending = await registry.get_all_pending_tokens()

    # Iterate ALL entries unconditionally — no early exit — to prevent
    # timing side-channels from revealing which entry matched.
    match = None
    expired_entry: dict | None = None
    found_locked = False
    for entry in all_pending:
        codes_equal = hmac.compare_digest(entry["short_code"].encode(), code.encode())
        if codes_equal:
            if datetime.fromisoformat(entry["expires_at"]) > now:
                if entry.get("failed_attempts", 0) < _MAX_FAILED_ATTEMPTS:
                    match = entry
                else:
                    found_locked = True
            else:
                expired_entry = entry
            # Do NOT break — always iterate all entries for constant-time behavior

    if not match:
        if expired_entry is not None:
            # Clean up the expired token using the stored key (not user input)
            await registry.delete_pending_token(expired_entry["short_code"])
        elif not found_locked:
            # Only record a failure if the code is unknown; locked tokens are already
            # at max attempts and incrementing further would be incorrect.
            await registry.record_failed_auth_attempt(code)
        # If found_locked: do nothing — token is already at max failed attempts
        logger.debug("Auth short code not found, expired, or locked out")
        return None

    # Atomically consume it — concurrent callers cannot both succeed
    consumed = await registry.atomic_consume_pending_token(code, now.isoformat())
    if not consumed:
        return None

    expires_at = datetime.fromisoformat(consumed["expires_at"])
    logger.info("Auth short code verified for session %s", consumed["session_id"])

    return SessionAuth(
        short_code=code,
        session_id=consumed["session_id"],
        expires_at=expires_at,
    )


async def generate_spawn_token(
    registry: SessionRegistry,
    target_user_id: str,
    cwd: str,
    spawn_source: str = "session",
    parent_session_id: str | None = None,
    parent_channel_id: str | None = None,
) -> SpawnAuth:
    """Generate a spawn token for pre-authenticated session creation."""
    if not target_user_id or not target_user_id.strip():
        raise ValueError("target_user_id must be non-empty")
    if not cwd or not cwd.startswith("/"):
        raise ValueError("cwd must be a non-empty absolute path")
    if not spawn_source or not spawn_source.strip():
        raise ValueError("spawn_source must be non-empty")
    token = secrets.token_hex(16)  # 32-char hex, 128-bit entropy
    expires_at = datetime.now(UTC) + timedelta(seconds=_SPAWN_TOKEN_TTL_SECONDS)
    await registry.store_spawn_token(
        token=token,
        target_user_id=target_user_id,
        cwd=cwd,
        expires_at=expires_at.isoformat(),
        spawn_source=spawn_source,
        parent_session_id=parent_session_id,
        parent_channel_id=parent_channel_id,
    )
    logger.debug("Generated spawn token for user %s", target_user_id)
    return SpawnAuth(
        token=token,
        parent_session_id=parent_session_id,
        parent_channel_id=parent_channel_id,
        target_user_id=target_user_id,
        cwd=cwd,
        spawn_source=spawn_source,
        expires_at=expires_at,
    )


async def verify_spawn_token(registry: SessionRegistry, token: str) -> SpawnAuth | None:
    """Verify and consume a spawn token. Returns SpawnAuth or None if invalid/expired.

    Uses constant-time comparison across all spawn tokens to prevent
    timing side-channel attacks, mirroring the verify_short_code pattern.
    """
    now = datetime.now(UTC)
    all_tokens = await registry.get_all_spawn_tokens()

    # Iterate ALL entries unconditionally for constant-time behavior
    match = None
    for entry in all_tokens:
        tokens_equal = hmac.compare_digest(entry["token"].encode(), token.encode())
        if tokens_equal and datetime.fromisoformat(entry["expires_at"]) > now:
            match = entry
        # Do NOT break — always iterate all entries

    if match is None:
        logger.warning("Spawn token verification failed: no valid match")
        return None

    # Atomically consume using the stored token value (not user input)
    row = await registry.consume_spawn_token(match["token"], now.isoformat())
    if row is None:
        logger.warning("Spawn token consumed by concurrent caller")
        return None
    return SpawnAuth(
        token=row["token"],
        parent_session_id=row["parent_session_id"],
        parent_channel_id=row["parent_channel_id"],
        target_user_id=row["target_user_id"],
        cwd=row["cwd"],
        spawn_source=row["spawn_source"],
        expires_at=datetime.fromisoformat(row["expires_at"]),
    )
