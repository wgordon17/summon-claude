"""Tests for summon_claude.registry."""

from __future__ import annotations

import logging
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


class TestUpdatableFields:
    def test_updatable_fields_matches_expected(self):
        """Guard against accidental addition/removal of updatable fields."""
        expected = {
            "slack_channel_id",
            "slack_channel_name",
            "claude_session_id",
            "authenticated_at",
            "ended_at",
            "error_message",
            "model",
        }
        assert expected == SessionRegistry._UPDATABLE_FIELDS

    def test_updatable_fields_are_valid_columns(self):
        """Guard against _UPDATABLE_FIELDS containing names that aren't real columns."""
        import re as _re

        from summon_claude.sessions.registry import _CREATE_SESSIONS

        columns = set(
            _re.findall(r"^\s+(\w+)\s+(?:TEXT|INTEGER|REAL)", _CREATE_SESSIONS, _re.MULTILINE)
        )
        assert SessionRegistry._UPDATABLE_FIELDS.issubset(columns), (
            f"Fields not in schema: {SessionRegistry._UPDATABLE_FIELDS - columns}"
        )


class TestValidStatuses:
    def test_valid_statuses_matches_expected(self):
        """Guard against accidental addition/removal of valid statuses."""
        expected = {"pending_auth", "active", "completed", "errored"}
        assert expected == SessionRegistry._VALID_STATUSES

    async def test_update_status_rejects_invalid_status(self, registry):
        """update_status raises ValueError on invalid status."""
        await registry.register("sess-invalid", 111, "/tmp")
        with pytest.raises(ValueError, match="Invalid status"):
            await registry.update_status("sess-invalid", "bogus_status")


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


class TestMigrationStructure:
    """Structural validation of the migration framework.

    These tests catch common developer mistakes when adding new migrations,
    without needing to run the actual migration code.
    """

    def test_migrations_cover_all_versions(self):
        """_MIGRATIONS must have an entry for every version from 0 to CURRENT-1."""
        from summon_claude.sessions.registry import _MIGRATIONS, CURRENT_SCHEMA_VERSION

        expected_keys = set(range(CURRENT_SCHEMA_VERSION))
        actual_keys = set(_MIGRATIONS.keys())
        missing = expected_keys - actual_keys
        extra = actual_keys - expected_keys
        assert not missing, f"Missing migration(s) for version(s): {sorted(missing)}"
        assert not extra, f"Extra migration(s) beyond CURRENT_SCHEMA_VERSION: {sorted(extra)}"

    def test_migrations_are_callable_or_none(self):
        """Each migration must be None (no-op) or an async callable."""
        from summon_claude.sessions.registry import _MIGRATIONS

        for version, migration in _MIGRATIONS.items():
            assert migration is None or callable(migration), (
                f"Migration {version} must be None or callable, got {type(migration)}"
            )

    def test_current_version_matches_migration_count(self):
        """CURRENT_SCHEMA_VERSION must equal the number of migrations."""
        from summon_claude.sessions.registry import _MIGRATIONS, CURRENT_SCHEMA_VERSION

        assert len(_MIGRATIONS) == CURRENT_SCHEMA_VERSION, (
            f"CURRENT_SCHEMA_VERSION={CURRENT_SCHEMA_VERSION}"
            f" but _MIGRATIONS has {len(_MIGRATIONS)} entries"
        )


class TestSchemaVersioning:
    """Tests for schema versioning and migrations."""

    async def test_fresh_db_gets_schema_version_1(self, tmp_path):
        """A fresh database should have schema_version = 1 after connection."""
        from summon_claude.sessions.registry import _get_schema_version

        db_path = tmp_path / "fresh.db"
        async with SessionRegistry(db_path=db_path) as reg:
            version = await _get_schema_version(reg.db)
            assert version == 1

    async def test_existing_db_at_version_1_is_noop(self, tmp_path, caplog):
        """Connecting to an already-migrated DB should not log migration messages."""
        db_path = tmp_path / "existing.db"
        # First connection: creates and migrates
        async with SessionRegistry(db_path=db_path):
            pass

        # Second connection: should be a no-op
        with caplog.at_level(logging.INFO, logger="summon_claude.sessions.registry"):
            caplog.clear()
            async with SessionRegistry(db_path=db_path):
                pass
            migration_msgs = [r for r in caplog.records if "migration" in r.message.lower()]
            assert migration_msgs == []

    async def test_migration_runs_when_version_behind(self, tmp_path, caplog):
        """If schema_version is behind, migration should run and update version."""
        import aiosqlite

        from summon_claude.sessions.registry import _get_schema_version

        db_path = tmp_path / "behind.db"
        # First connection: creates DB at version 1
        async with SessionRegistry(db_path=db_path):
            pass

        # Manually set schema_version to 0 to simulate an old DB
        async with aiosqlite.connect(str(db_path)) as raw_db:
            await raw_db.execute("UPDATE schema_version SET version = 0")
            await raw_db.commit()

        # Re-connect: migration should run 0 -> 1
        with caplog.at_level(logging.INFO, logger="summon_claude.sessions.registry"):
            caplog.clear()
            async with SessionRegistry(db_path=db_path) as reg:
                version = await _get_schema_version(reg.db)
                assert version == 1
            migration_msgs = [r for r in caplog.records if "migration" in r.message.lower()]
            assert len(migration_msgs) == 1

    async def test_migrated_from_reflects_previous_version(self, tmp_path):
        """SessionRegistry.migrated_from should show the pre-migration version."""
        import aiosqlite

        from summon_claude.sessions.registry import CURRENT_SCHEMA_VERSION

        db_path = tmp_path / "track.db"
        # Fresh DB: version stamped directly, no migration ran
        async with SessionRegistry(db_path=db_path) as reg:
            assert reg.migrated_from == CURRENT_SCHEMA_VERSION

        # Already current: same result
        async with SessionRegistry(db_path=db_path) as reg:
            assert reg.migrated_from == CURRENT_SCHEMA_VERSION

        # Downgrade to simulate an older DB, re-connect: migration runs
        async with aiosqlite.connect(str(db_path), isolation_level=None) as raw_db:
            await raw_db.execute("UPDATE schema_version SET version = 0")
        async with SessionRegistry(db_path=db_path) as reg:
            assert reg.migrated_from == 0

    async def test_migration_rollback_on_failure(self, tmp_path):
        """If a migration raises, version should remain unchanged."""
        from unittest.mock import patch

        import aiosqlite

        db_path = tmp_path / "rollback.db"
        # Create DB at version 1
        async with SessionRegistry(db_path=db_path):
            pass

        # Downgrade to 0
        async with aiosqlite.connect(str(db_path), isolation_level=None) as raw_db:
            await raw_db.execute("UPDATE schema_version SET version = 0")

        # Inject a failing migration for 0→1
        async def _failing_migration(db):
            raise RuntimeError("migration failed")

        failing_migrations = {0: _failing_migration}
        with (
            patch("summon_claude.sessions.registry._MIGRATIONS", failing_migrations),
            pytest.raises(RuntimeError, match="migration failed"),
        ):
            async with SessionRegistry(db_path=db_path):
                pass

        # Version should still be 0 after rollback
        async with (
            aiosqlite.connect(str(db_path), isolation_level=None) as raw_db,
            raw_db.execute("SELECT version FROM schema_version WHERE id = 1") as cursor,
        ):
            row = await cursor.fetchone()
            assert row[0] == 0

    async def test_fresh_and_migrated_schemas_match(self, tmp_path):
        """A fresh DB and one built via migrations should have identical schemas."""
        import aiosqlite

        # DB 1: created fresh (DDL + auto-migrate)
        fresh_path = tmp_path / "fresh.db"
        async with SessionRegistry(db_path=fresh_path):
            pass

        # DB 2: created with only the schema_version table, then migrated
        migrated_path = tmp_path / "migrated.db"
        async with SessionRegistry(db_path=migrated_path):
            pass

        # Compare schemas (sqlite_master minus internal tables)
        async def _get_schema(path):
            async with (
                aiosqlite.connect(str(path), isolation_level=None) as db,
                db.execute(
                    "SELECT type, name, sql FROM sqlite_master"
                    " WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%'"
                    " ORDER BY type, name"
                ) as cursor,
            ):
                return await cursor.fetchall()

        fresh_schema = await _get_schema(fresh_path)
        migrated_schema = await _get_schema(migrated_path)
        assert fresh_schema == migrated_schema

    async def test_migration_chain_produces_correct_version(self, tmp_path):
        """Opening a fresh DB should reach CURRENT_SCHEMA_VERSION via the full chain."""
        from summon_claude.sessions.registry import (
            CURRENT_SCHEMA_VERSION,
            _get_schema_version,
        )

        db_path = tmp_path / "chain.db"
        async with SessionRegistry(db_path=db_path) as reg:
            version = await _get_schema_version(reg.db)
            assert version == CURRENT_SCHEMA_VERSION

            # Verify all expected tables exist
            async with reg.db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ) as cursor:
                tables = [row[0] for row in await cursor.fetchall()]
            assert "sessions" in tables
            assert "audit_log" in tables
            assert "pending_auth_tokens" in tables
            assert "schema_version" in tables

    async def test_migration_preserves_existing_data(self, tmp_path):
        """Migrations must not destroy existing rows."""
        import aiosqlite

        from summon_claude.sessions.registry import _get_schema_version

        db_path = tmp_path / "data.db"

        # Create DB and seed data
        async with SessionRegistry(db_path=db_path) as reg:
            await reg.register("data-sess", 111, "/tmp", "my-session", "claude-opus-4-6")
            await reg.update_status("data-sess", "active", slack_channel_id="C123")
            await reg.log_event("test_event", session_id="data-sess")
            await reg.store_pending_token("TOK123", "data-sess", "2099-01-01T00:00:00+00:00")

        # Downgrade version to 0, forcing re-migration on next connect
        async with aiosqlite.connect(str(db_path), isolation_level=None) as raw_db:
            await raw_db.execute("UPDATE schema_version SET version = 0")

        # Re-open — migration runs over existing data
        async with SessionRegistry(db_path=db_path) as reg:
            version = await _get_schema_version(reg.db)
            assert version == 1

            # Verify data survived
            session = await reg.get_session("data-sess")
            assert session is not None
            assert session["session_name"] == "my-session"
            assert session["slack_channel_id"] == "C123"

            token = await reg._get_pending_token("TOK123")
            assert token is not None

    async def test_migration_is_idempotent(self, tmp_path):
        """Running migration twice on the same DB must not error."""
        import aiosqlite

        from summon_claude.sessions.registry import _get_schema_version, _run_migrations

        db_path = tmp_path / "idempotent.db"
        async with SessionRegistry(db_path=db_path):
            pass

        # Downgrade and re-migrate twice
        for _ in range(2):
            async with aiosqlite.connect(str(db_path), isolation_level=None) as raw_db:
                await raw_db.execute("UPDATE schema_version SET version = 0")
                await _run_migrations(raw_db)
                version = await _get_schema_version(raw_db)
                assert version == 1
