"""Tests for summon_claude.auth."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from summon_claude.sessions.auth import (
    SessionAuth,
    SpawnAuth,
    generate_session_token,
    generate_spawn_token,
    verify_short_code,
    verify_spawn_token,
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


class TestGenerateSpawnToken:
    async def test_generates_valid_token(self, registry):
        result = await generate_spawn_token(registry, "U123", "/tmp", spawn_source="cli")
        assert isinstance(result, SpawnAuth)
        assert len(result.token) == 32
        assert result.target_user_id == "U123"
        assert result.cwd == "/tmp"
        assert result.spawn_source == "cli"

    async def test_cli_source(self, registry):
        result = await generate_spawn_token(registry, "U123", "/tmp", spawn_source="cli")
        assert result.spawn_source == "cli"
        assert result.parent_session_id is None
        assert result.parent_channel_id is None


class TestVerifySpawnToken:
    async def test_valid_roundtrip(self, registry):
        auth = await generate_spawn_token(registry, "U123", "/tmp", spawn_source="cli")
        result = await verify_spawn_token(registry, auth.token)
        assert result is not None
        assert result.target_user_id == "U123"

    async def test_roundtrip_preserves_parent_fields(self, registry):
        auth = await generate_spawn_token(
            registry,
            "U123",
            "/tmp",
            "session",
            parent_session_id="parent-sess-1",
            parent_channel_id="C_PARENT",
            parent_cwd="/tmp",
        )
        result = await verify_spawn_token(registry, auth.token)
        assert result is not None
        assert result.parent_session_id == "parent-sess-1"
        assert result.parent_channel_id == "C_PARENT"
        assert result.target_user_id == "U123"
        assert result.cwd == "/tmp"
        assert result.spawn_source == "session"

    async def test_invalid_token(self, registry):
        result = await verify_spawn_token(registry, "nonexistent")
        assert result is None

    async def test_expired_token(self, registry):
        # Generate token with very short TTL
        with patch("summon_claude.sessions.auth._SPAWN_TOKEN_TTL_SECONDS", 0):
            auth = await generate_spawn_token(registry, "U123", "/tmp", spawn_source="cli")
        # Wait briefly for it to expire
        await asyncio.sleep(0.1)
        result = await verify_spawn_token(registry, auth.token)
        assert result is None


class TestGenerateSpawnTokenValidation:
    async def test_rejects_empty_target_user_id(self, registry):
        with pytest.raises(ValueError, match="target_user_id"):
            await generate_spawn_token(registry, "", "/tmp", "cli")

    async def test_rejects_whitespace_target_user_id(self, registry):
        with pytest.raises(ValueError, match="target_user_id"):
            await generate_spawn_token(registry, "   ", "/tmp", "cli")

    async def test_rejects_empty_cwd(self, registry):
        with pytest.raises(ValueError, match="cwd"):
            await generate_spawn_token(registry, "U123", "", "cli")

    async def test_rejects_relative_cwd(self, registry):
        with pytest.raises(ValueError, match="cwd"):
            await generate_spawn_token(registry, "U123", "relative/path", "cli")

    async def test_rejects_empty_spawn_source(self, registry):
        with pytest.raises(ValueError, match="spawn_source"):
            await generate_spawn_token(registry, "U123", "/tmp", "")

    async def test_rejects_whitespace_spawn_source(self, registry):
        with pytest.raises(ValueError, match="spawn_source"):
            await generate_spawn_token(registry, "U123", "/tmp", "   ")

    async def test_rejects_invalid_spawn_source(self, registry):
        with pytest.raises(ValueError, match="must be one of"):
            await generate_spawn_token(registry, "U123", "/tmp", "other")

    def test_valid_spawn_sources_pinned(self):
        """Guard test: pin the set of valid spawn sources."""
        from summon_claude.sessions.auth import _VALID_SPAWN_SOURCES

        assert {"session", "cli"} == _VALID_SPAWN_SOURCES

    async def test_parent_cwd_rejects_breakout(self, registry):
        """CWD outside parent_cwd is rejected."""
        with pytest.raises(ValueError, match="not within parent"):
            await generate_spawn_token(
                registry, "U123", "/other/path", "session", parent_cwd="/home/user/proj"
            )

    async def test_parent_cwd_allows_descendant(self, registry):
        """CWD under parent_cwd is allowed."""
        result = await generate_spawn_token(
            registry, "U123", "/home/user/proj/sub", "session", parent_cwd="/home/user/proj"
        )
        assert result.cwd == "/home/user/proj/sub"

    async def test_parent_cwd_allows_same_dir(self, registry):
        """CWD equal to parent_cwd is allowed."""
        result = await generate_spawn_token(
            registry, "U123", "/home/user/proj", "session", parent_cwd="/home/user/proj"
        )
        assert result.cwd == "/home/user/proj"

    async def test_parent_cwd_none_skips_check_for_cli(self, registry):
        """CLI-originated spawns skip the ancestor check when parent_cwd is None."""
        result = await generate_spawn_token(registry, "U123", "/completely/different/path", "cli")
        assert result.cwd == "/completely/different/path"

    async def test_session_spawn_requires_parent_cwd(self, registry):
        """Session-originated spawns MUST provide parent_cwd."""
        with pytest.raises(ValueError, match="parent_cwd is required"):
            await generate_spawn_token(registry, "U123", "/tmp", "session")

    async def test_parent_cwd_symlink_escape_rejected(self, registry, tmp_path):
        """Symlink-based escape is blocked by resolve()."""
        parent = tmp_path / "proj"
        parent.mkdir()
        escape_target = tmp_path / "escape"
        escape_target.mkdir()
        link = parent / "sneaky"
        link.symlink_to(escape_target)

        with pytest.raises(ValueError, match="not within parent"):
            await generate_spawn_token(
                registry, "U123", str(link), "session", parent_cwd=str(parent)
            )
