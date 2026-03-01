"""Tests for summon_claude.auth."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from summon_claude.sessions.auth import (
    SessionAuth,
    generate_session_token,
    verify_short_code,
)


class TestGenerateSessionToken:
    async def test_returns_session_auth(self, registry):
        auth = await generate_session_token(registry, "sess-1")
        assert isinstance(auth, SessionAuth)

    async def test_short_code_is_eight_hex_chars(self, registry):
        auth = await generate_session_token(registry, "sess-3")
        assert len(auth.short_code) == 8
        # Should be hex (0-9a-f)
        assert all(c in "0123456789abcdef" for c in auth.short_code)

    async def test_session_id_preserved(self, registry):
        auth = await generate_session_token(registry, "my-session-id")
        assert auth.session_id == "my-session-id"

    async def test_expires_in_five_minutes(self, registry):
        before = datetime.now(UTC)
        auth = await generate_session_token(registry, "sess-exp")
        after = datetime.now(UTC)
        # expires_at should be ~5 minutes from now
        min_expiry = before + timedelta(minutes=4, seconds=59)
        max_expiry = after + timedelta(minutes=5, seconds=1)
        assert min_expiry <= auth.expires_at <= max_expiry

    async def test_token_stored_in_registry(self, registry):
        auth = await generate_session_token(registry, "sess-stored")
        entry = await registry._get_pending_token(auth.short_code)
        assert entry is not None
        assert entry["session_id"] == "sess-stored"


class TestVerifyShortCode:
    async def test_valid_code_returns_session_auth(self, registry):
        auth = await generate_session_token(registry, "sess-v")
        result = await verify_short_code(registry, auth.short_code)
        assert result is not None
        assert result.session_id == "sess-v"

    async def test_invalid_code_returns_none(self, registry):
        result = await verify_short_code(registry, "XXXXXX")
        assert result is None

    async def test_code_is_one_time_use(self, registry):
        auth = await generate_session_token(registry, "sess-otu")
        first = await verify_short_code(registry, auth.short_code)
        second = await verify_short_code(registry, auth.short_code)
        assert first is not None
        assert second is None

    async def test_expired_code_returns_none(self, registry):
        auth = await generate_session_token(registry, "sess-exp")
        # Manually overwrite expiry to the past
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        await registry.store_pending_token(
            short_code=auth.short_code,
            session_id="sess-exp",
            expires_at=past,
        )
        result = await verify_short_code(registry, auth.short_code)
        assert result is None

    async def test_expired_code_is_deleted(self, registry):
        auth = await generate_session_token(registry, "sess-del")
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        await registry.store_pending_token(
            short_code=auth.short_code,
            session_id="sess-del",
            expires_at=past,
        )
        await verify_short_code(registry, auth.short_code)
        # Should be gone from the registry
        entry = await registry._get_pending_token(auth.short_code)
        assert entry is None

    async def test_code_verification_is_case_insensitive(self, registry):
        auth = await generate_session_token(registry, "sess-case")
        lowercase = auth.short_code.lower()
        result = await verify_short_code(registry, lowercase)
        assert result is not None
        assert result.session_id == "sess-case"

    async def test_code_verification_trims_whitespace(self, registry):
        auth = await generate_session_token(registry, "sess-ws")
        padded = f"  {auth.short_code}  "
        result = await verify_short_code(registry, padded)
        assert result is not None

    async def test_locked_out_token_returns_none_and_does_not_increment(self, registry):
        from summon_claude.sessions.registry import _MAX_FAILED_ATTEMPTS

        auth = await generate_session_token(registry, "sess-lock")
        # Drive up to exactly the max failed attempts
        for _ in range(_MAX_FAILED_ATTEMPTS):
            await registry.record_failed_auth_attempt(auth.short_code)

        entry_before = await registry._get_pending_token(auth.short_code)
        assert entry_before is not None
        assert entry_before["failed_attempts"] == _MAX_FAILED_ATTEMPTS

        # Attempt to verify — should be rejected (locked out)
        result = await verify_short_code(registry, auth.short_code)
        assert result is None

        # Counter must not have been incremented further
        entry_after = await registry._get_pending_token(auth.short_code)
        assert entry_after is not None
        assert entry_after["failed_attempts"] == _MAX_FAILED_ATTEMPTS
