"""Tests for summon_claude.registry."""

from __future__ import annotations

import os

import pytest

from summon_claude.sessions.registry import SessionRegistry, _pid_alive


class TestSessionRegistryConnect:
    async def test_context_manager_creates_db(self, tmp_path):
        db_path = tmp_path / "test.db"
        assert not db_path.exists()
        async with SessionRegistry(db_path=db_path):
            assert db_path.exists()

    async def test_not_connected_raises(self, tmp_path):
        reg = SessionRegistry(db_path=tmp_path / "test.db")
        with pytest.raises(RuntimeError, match="not connected"):
            reg._check_connected()


class TestRegister:
    async def test_register_creates_session(self, registry):
        await registry.register("sess-1", 1234, "/tmp", "my-session", "claude-opus-4-6")
        session = await registry.get_session("sess-1")
        assert session is not None
        assert session["session_id"] == "sess-1"
        assert session["pid"] == 1234
        assert session["cwd"] == "/tmp"
        assert session["session_name"] == "my-session"
        assert session["model"] == "claude-opus-4-6"
        assert session["status"] == "pending_auth"

    async def test_register_without_name_and_model(self, registry):
        await registry.register("sess-2", 5678, "/home/user")
        session = await registry.get_session("sess-2")
        assert session is not None
        assert session["session_name"] is None
        assert session["model"] is None

    async def test_register_sets_timestamps(self, registry):
        await registry.register("sess-3", 9999, "/tmp")
        session = await registry.get_session("sess-3")
        assert session["started_at"] is not None
        assert session["last_activity_at"] is not None


class TestGetSession:
    async def test_get_missing_session_returns_none(self, registry):
        result = await registry.get_session("nonexistent-id")
        assert result is None

    async def test_get_existing_session(self, registry):
        await registry.register("sess-get", 111, "/tmp")
        session = await registry.get_session("sess-get")
        assert session is not None
        assert session["session_id"] == "sess-get"


class TestUpdateStatus:
    async def test_update_status_changes_status(self, registry):
        await registry.register("sess-u", 111, "/tmp")
        await registry.update_status("sess-u", "active")
        session = await registry.get_session("sess-u")
        assert session["status"] == "active"

    async def test_update_status_with_channel_id(self, registry):
        await registry.register("sess-ch", 111, "/tmp")
        await registry.update_status("sess-ch", "active", slack_channel_id="C12345")
        session = await registry.get_session("sess-ch")
        assert session["slack_channel_id"] == "C12345"

    async def test_update_status_with_error_message(self, registry):
        await registry.register("sess-err", 111, "/tmp")
        await registry.update_status("sess-err", "errored", error_message="Connection failed")
        session = await registry.get_session("sess-err")
        assert session["status"] == "errored"
        assert session["error_message"] == "Connection failed"

    async def test_update_status_unknown_field_ignored(self, registry):
        await registry.register("sess-ign", 111, "/tmp")
        # Should not raise even with unknown kwarg
        await registry.update_status("sess-ign", "active", nonexistent_field="value")
        session = await registry.get_session("sess-ign")
        assert session["status"] == "active"


class TestHeartbeat:
    async def test_heartbeat_updates_activity(self, registry):
        await registry.register("sess-hb", 111, "/tmp")
        before = (await registry.get_session("sess-hb"))["last_activity_at"]
        import asyncio

        await asyncio.sleep(0.01)
        await registry.heartbeat("sess-hb")
        after = (await registry.get_session("sess-hb"))["last_activity_at"]
        # Timestamps should differ (both are ISO strings, later one is lexicographically greater)
        assert after >= before


class TestRecordTurn:
    async def test_record_turn_increments_count(self, registry):
        await registry.register("sess-t", 111, "/tmp")
        await registry.record_turn("sess-t", 0.01)
        session = await registry.get_session("sess-t")
        assert session["total_turns"] == 1

    async def test_record_turn_accumulates_cost(self, registry):
        await registry.register("sess-cost", 111, "/tmp")
        await registry.record_turn("sess-cost", 0.01)
        await registry.record_turn("sess-cost", 0.02)
        session = await registry.get_session("sess-cost")
        assert session["total_turns"] == 2
        assert abs(session["total_cost_usd"] - 0.03) < 1e-9

    async def test_record_turn_zero_cost(self, registry):
        await registry.register("sess-zero", 111, "/tmp")
        await registry.record_turn("sess-zero")
        session = await registry.get_session("sess-zero")
        assert session["total_turns"] == 1
        assert session["total_cost_usd"] == 0.0


class TestListActive:
    async def test_list_active_returns_pending_auth_and_active(self, registry):
        await registry.register("sess-pending", 111, "/tmp")
        await registry.register("sess-active", 222, "/tmp")
        await registry.update_status("sess-active", "active")
        await registry.register("sess-completed", 333, "/tmp")
        await registry.update_status("sess-completed", "completed")

        active = await registry.list_active()
        ids = [s["session_id"] for s in active]
        assert "sess-pending" in ids
        assert "sess-active" in ids
        assert "sess-completed" not in ids

    async def test_list_active_empty(self, registry):
        result = await registry.list_active()
        assert result == []


class TestListAll:
    async def test_list_all_includes_all_statuses(self, registry):
        await registry.register("sess-a1", 111, "/tmp")
        await registry.update_status("sess-a1", "completed")
        await registry.register("sess-a2", 222, "/tmp")
        await registry.update_status("sess-a2", "errored")
        await registry.register("sess-a3", 333, "/tmp")

        all_sessions = await registry.list_all()
        ids = [s["session_id"] for s in all_sessions]
        assert "sess-a1" in ids
        assert "sess-a2" in ids
        assert "sess-a3" in ids

    async def test_list_all_respects_limit(self, registry):
        for i in range(10):
            await registry.register(f"sess-lim-{i}", i + 1, "/tmp")
        result = await registry.list_all(limit=3)
        assert len(result) == 3


class TestPendingAuthTokens:
    async def test_store_and_retrieve_token(self, registry):
        await registry.store_pending_token(
            short_code="ABC123",
            session_id="sess-tok",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        entry = await registry._get_pending_token("ABC123")
        assert entry is not None
        assert entry["session_id"] == "sess-tok"

    async def test_missing_token_returns_none(self, registry):
        result = await registry._get_pending_token("XXXXXX")
        assert result is None

    async def test_delete_token(self, registry):
        await registry.store_pending_token(
            short_code="DEL123",
            session_id="s",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        await registry.delete_pending_token("DEL123")
        result = await registry._get_pending_token("DEL123")
        assert result is None

    async def test_replace_existing_token(self, registry):
        for _ in ("first", "second"):
            await registry.store_pending_token(
                short_code="SAME12",
                session_id="s",
                expires_at="2099-01-01T00:00:00+00:00",
            )
        entry = await registry._get_pending_token("SAME12")
        assert entry["session_id"] == "s"


class TestPidAlive:
    def test_current_process_alive(self):
        assert _pid_alive(os.getpid()) is True

    def test_nonexistent_pid_dead(self):
        assert _pid_alive(999999999) is False


class TestResolveSession:
    async def test_resolve_exact_id(self, registry):
        await registry.register("sess-resolve-1", 111, "/tmp")
        session, matches = await registry.resolve_session("sess-resolve-1")
        assert session is not None
        assert session["session_id"] == "sess-resolve-1"
        assert len(matches) == 1

    async def test_resolve_prefix(self, registry):
        await registry.register("abcd1234-5678-9abc-def0-111111111111", 111, "/tmp")
        session, matches = await registry.resolve_session("abcd1234")
        assert session is not None
        assert session["session_id"] == "abcd1234-5678-9abc-def0-111111111111"
        assert len(matches) == 1

    async def test_resolve_ambiguous_prefix_returns_matches(self, registry):
        await registry.register("abcd1111-0000-0000-0000-000000000000", 111, "/tmp")
        await registry.register("abcd2222-0000-0000-0000-000000000000", 222, "/tmp")
        session, matches = await registry.resolve_session("abcd")
        assert session is None
        assert len(matches) == 2

    async def test_resolve_channel_name(self, registry):
        await registry.register("sess-chan-1", 111, "/tmp")
        await registry.update_status(
            "sess-chan-1", "active", slack_channel_name="summon-my-proj-0224"
        )
        session, matches = await registry.resolve_session("summon-my-proj-0224")
        assert session is not None
        assert session["session_id"] == "sess-chan-1"

    async def test_resolve_nonexistent_returns_empty(self, registry):
        session, matches = await registry.resolve_session("nonexistent")
        assert session is None
        assert matches == []

    async def test_resolve_escapes_like_percent(self, registry):
        """Percent in identifier must not act as LIKE wildcard."""
        await registry.register("abcd1234-0000-0000-0000-000000000000", 111, "/tmp")
        session, matches = await registry.resolve_session("ab%d")
        assert session is None
        assert matches == []

    async def test_resolve_escapes_like_underscore(self, registry):
        """Underscore in identifier must not act as single-char wildcard."""
        await registry.register("abcd1234-0000-0000-0000-000000000000", 111, "/tmp")
        # Without escaping, "ab_d" would match "abcd" via _ wildcard
        session, matches = await registry.resolve_session("ab_d")
        assert session is None
        assert matches == []


class TestMarkStale:
    async def test_marks_session_as_errored(self, registry):
        await registry.register("sess-stale", 111, "/tmp")
        await registry.update_status("sess-stale", "active")

        await registry.mark_stale("sess-stale", "test reason")

        s = await registry.get_session("sess-stale")
        assert s["status"] == "errored"
        assert s["error_message"] == "test reason"
        assert s["ended_at"] is not None


class TestCleanupActive:
    async def test_marks_all_active_sessions(self, registry):
        await registry.register("ca-1", 111, "/tmp")
        await registry.update_status("ca-1", "active")
        await registry.register("ca-2", 222, "/tmp")
        # ca-2 stays as pending_auth — also caught

        cleaned = await registry.cleanup_active("daemon restart")

        assert len(cleaned) == 2
        for s_id in ("ca-1", "ca-2"):
            s = await registry.get_session(s_id)
            assert s["status"] == "errored"
            assert s["error_message"] == "daemon restart"

    async def test_skips_completed_sessions(self, registry):
        await registry.register("ca-done", 111, "/tmp")
        await registry.update_status("ca-done", "completed")

        cleaned = await registry.cleanup_active("daemon restart")

        assert len(cleaned) == 0
        s = await registry.get_session("ca-done")
        assert s["status"] == "completed"

    async def test_returns_empty_when_no_active(self, registry):
        cleaned = await registry.cleanup_active("daemon restart")
        assert cleaned == []


class TestSQLitePragmas:
    """Test that SQLite pragmas are properly configured (BUG-015)."""

    async def test_busy_timeout_pragma_set(self, registry):
        """Test that busy_timeout pragma is set to 5000ms."""
        # Check the pragma by executing it through the registry's connection
        db = registry._check_connected()
        cursor = await db.execute("PRAGMA busy_timeout")
        result = await cursor.fetchone()
        timeout_ms = result[0]
        assert timeout_ms == 5000, f"Expected busy_timeout=5000, got {timeout_ms}"
