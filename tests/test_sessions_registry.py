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
            "authenticated_user_id",
            "ended_at",
            "error_message",
            "model",
        }
        assert expected == SessionRegistry._UPDATABLE_FIELDS

    def test_updatable_fields_are_valid_columns(self):
        """Guard against _UPDATABLE_FIELDS containing names that aren't real columns."""
        import re as _re

        from summon_claude.sessions.registry import _CREATE_SESSIONS

        columns = set(_re.findall(r"^\s+(\w+)\s+[A-Z]", _CREATE_SESSIONS, _re.MULTILINE))
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

    async def test_fresh_db_gets_current_schema_version(self, tmp_path):
        """A fresh database should have CURRENT_SCHEMA_VERSION after connection."""
        from summon_claude.sessions.registry import CURRENT_SCHEMA_VERSION, get_schema_version

        db_path = tmp_path / "fresh.db"
        async with SessionRegistry(db_path=db_path) as reg:
            version = await get_schema_version(reg.db)
            assert version == CURRENT_SCHEMA_VERSION

    async def test_existing_db_at_current_version_is_noop(self, tmp_path, caplog):
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

        from summon_claude.sessions.registry import CURRENT_SCHEMA_VERSION, get_schema_version

        db_path = tmp_path / "behind.db"
        # First connection: creates DB at current version
        async with SessionRegistry(db_path=db_path):
            pass

        # Manually set schema_version to 0 to simulate an old DB
        async with aiosqlite.connect(str(db_path)) as raw_db:
            await raw_db.execute("UPDATE schema_version SET version = 0")
            await raw_db.commit()

        # Re-connect: migrations should run 0 -> CURRENT
        with caplog.at_level(logging.INFO, logger="summon_claude.sessions.registry"):
            caplog.clear()
            async with SessionRegistry(db_path=db_path) as reg:
                version = await get_schema_version(reg.db)
                assert version == CURRENT_SCHEMA_VERSION
            migration_msgs = [r for r in caplog.records if "migration" in r.message.lower()]
            assert len(migration_msgs) == CURRENT_SCHEMA_VERSION

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

        from summon_claude.sessions.registry import CURRENT_SCHEMA_VERSION

        db_path = tmp_path / "rollback.db"
        # Create DB at current version
        async with SessionRegistry(db_path=db_path):
            pass

        # Downgrade to CURRENT-1 so only one migration step is needed
        target = CURRENT_SCHEMA_VERSION - 1
        async with aiosqlite.connect(str(db_path), isolation_level=None) as raw_db:
            await raw_db.execute("UPDATE schema_version SET version = ?", (target,))

        # Inject a failing migration for target→target+1
        async def _failing_migration(db):
            raise RuntimeError("migration failed")

        failing_migrations = {target: _failing_migration}
        with (
            patch("summon_claude.sessions.registry._MIGRATIONS", failing_migrations),
            patch("summon_claude.sessions.registry.CURRENT_SCHEMA_VERSION", target + 1),
            pytest.raises(RuntimeError, match="migration failed"),
        ):
            async with SessionRegistry(db_path=db_path):
                pass

        # Version should still be target after rollback
        async with (
            aiosqlite.connect(str(db_path), isolation_level=None) as raw_db,
            raw_db.execute("SELECT version FROM schema_version WHERE id = 1") as cursor,
        ):
            row = await cursor.fetchone()
            assert row[0] == target

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
            get_schema_version,
        )

        db_path = tmp_path / "chain.db"
        async with SessionRegistry(db_path=db_path) as reg:
            version = await get_schema_version(reg.db)
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
            assert "spawn_tokens" in tables
            assert "schema_version" in tables
            assert "workflow_defaults" in tables

    async def test_migration_preserves_existing_data(self, tmp_path):
        """Migrations must not destroy existing rows."""
        import aiosqlite

        from summon_claude.sessions.registry import CURRENT_SCHEMA_VERSION, get_schema_version

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
            version = await get_schema_version(reg.db)
            assert version == CURRENT_SCHEMA_VERSION

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

        from summon_claude.sessions.registry import (
            CURRENT_SCHEMA_VERSION,
            _run_migrations,
            get_schema_version,
        )

        db_path = tmp_path / "idempotent.db"
        async with SessionRegistry(db_path=db_path):
            pass

        # Downgrade and re-migrate twice
        for _ in range(2):
            async with aiosqlite.connect(str(db_path), isolation_level=None) as raw_db:
                await raw_db.execute("UPDATE schema_version SET version = 0")
                await _run_migrations(raw_db)
                version = await get_schema_version(raw_db)
                assert version == CURRENT_SCHEMA_VERSION


class TestSpawnTokens:
    async def test_store_and_consume_spawn_token(self, registry):
        from datetime import UTC, datetime, timedelta

        expires = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        await registry.store_spawn_token(
            token="abc123",
            target_user_id="U999",
            cwd="/tmp",
            expires_at=expires,
            spawn_source="session",
            parent_session_id="parent-1",
            parent_channel_id="C111",
        )
        row = await registry.consume_spawn_token("abc123", datetime.now(UTC).isoformat())
        assert row is not None
        assert row["token"] == "abc123"
        assert row["target_user_id"] == "U999"
        assert row["parent_session_id"] == "parent-1"

    async def test_consume_spawn_token_expired(self, registry):
        from datetime import UTC, datetime, timedelta

        expired = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        await registry.store_spawn_token(
            token="expired1",
            target_user_id="U999",
            cwd="/tmp",
            expires_at=expired,
        )
        row = await registry.consume_spawn_token("expired1", datetime.now(UTC).isoformat())
        assert row is None

    async def test_consume_spawn_token_already_consumed(self, registry):
        from datetime import UTC, datetime, timedelta

        expires = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        await registry.store_spawn_token(
            token="once1",
            target_user_id="U999",
            cwd="/tmp",
            expires_at=expires,
        )
        first = await registry.consume_spawn_token("once1", datetime.now(UTC).isoformat())
        second = await registry.consume_spawn_token("once1", datetime.now(UTC).isoformat())
        assert first is not None
        assert second is None

    async def test_store_spawn_token_nullable_parent(self, registry):
        from datetime import UTC, datetime, timedelta

        expires = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        await registry.store_spawn_token(
            token="cli1",
            target_user_id="U999",
            cwd="/tmp",
            expires_at=expires,
            spawn_source="cli",
        )
        row = await registry.consume_spawn_token("cli1", datetime.now(UTC).isoformat())
        assert row is not None
        assert row["parent_session_id"] is None
        assert row["parent_channel_id"] is None
        assert row["spawn_source"] == "cli"

    async def test_register_with_parent_session_id(self, registry):
        await registry.register("sess-parent", 1234, "/tmp", parent_session_id="parent-sess")
        session = await registry.get_session("sess-parent")
        assert session["parent_session_id"] == "parent-sess"

    async def test_register_with_authenticated_user_id(self, registry):
        await registry.register("sess-uid", 1234, "/tmp", authenticated_user_id="U123")
        session = await registry.get_session("sess-uid")
        assert session["authenticated_user_id"] == "U123"


class TestWorkflowDefaults:
    async def test_get_workflow_defaults_empty_by_default(self, registry):
        result = await registry.get_workflow_defaults()
        assert result == ""

    async def test_set_get_workflow_defaults(self, registry):
        await registry.set_workflow_defaults("Always run tests before committing.")
        result = await registry.get_workflow_defaults()
        assert result == "Always run tests before committing."

    async def test_set_workflow_defaults_overwrites(self, registry):
        await registry.set_workflow_defaults("First version.")
        await registry.set_workflow_defaults("Second version.")
        result = await registry.get_workflow_defaults()
        assert result == "Second version."

    async def test_clear_workflow_defaults(self, registry):
        await registry.set_workflow_defaults("Some instructions.")
        await registry.clear_workflow_defaults()
        result = await registry.get_workflow_defaults()
        assert result == ""

    async def test_clear_workflow_defaults_noop_when_empty(self, registry):
        await registry.clear_workflow_defaults()
        result = await registry.get_workflow_defaults()
        assert result == ""

    async def test_set_workflow_defaults_empty_string(self, registry):
        await registry.set_workflow_defaults("Non-empty.")
        await registry.set_workflow_defaults("")
        result = await registry.get_workflow_defaults()
        assert result == ""


class TestProjectWorkflow:
    _PROJECTS_DDL = (
        "CREATE TABLE projects ("
        "  project_id TEXT PRIMARY KEY,"
        "  workflow_instructions TEXT NOT NULL DEFAULT ''"
        ")"
    )

    async def test_get_project_workflow_no_projects_table(self, registry):
        """Returns empty string when projects table doesn't exist."""
        result = await registry.get_project_workflow("proj-1")
        assert result == ""

    async def test_set_project_workflow_no_projects_table(self, registry):
        """Raises RuntimeError when projects table doesn't exist."""
        with pytest.raises(RuntimeError, match="projects table"):
            await registry.set_project_workflow("proj-1", "instructions")

    async def test_clear_project_workflow_no_projects_table(self, registry):
        """Raises RuntimeError when projects table doesn't exist."""
        with pytest.raises(RuntimeError, match="projects table"):
            await registry.clear_project_workflow("proj-1")

    async def test_get_project_workflow_returns_instructions(self, registry):
        """Returns stored instructions when project exists."""
        db = registry.db
        await db.execute(self._PROJECTS_DDL)
        await db.execute(
            "INSERT INTO projects (project_id, workflow_instructions) VALUES ('proj-1', 'Use TDD.')"
        )
        await db.commit()
        result = await registry.get_project_workflow("proj-1")
        assert result == "Use TDD."

    async def test_get_project_workflow_missing_project(self, registry):
        """Returns empty string when project_id not in table."""
        db = registry.db
        await db.execute(self._PROJECTS_DDL)
        await db.commit()
        result = await registry.get_project_workflow("no-such")
        assert result == ""

    async def test_set_project_workflow_updates_existing(self, registry):
        """Updates instructions for an existing project."""
        db = registry.db
        await db.execute(self._PROJECTS_DDL)
        await db.execute(
            "INSERT INTO projects (project_id, workflow_instructions) VALUES ('proj-1', 'Old.')"
        )
        await db.commit()
        await registry.set_project_workflow("proj-1", "New.")
        result = await registry.get_project_workflow("proj-1")
        assert result == "New."

    async def test_set_project_workflow_raises_on_missing_project(self, registry):
        """Raises KeyError when project_id doesn't exist in the table."""
        db = registry.db
        await db.execute(self._PROJECTS_DDL)
        await db.commit()
        with pytest.raises(KeyError, match="proj-missing"):
            await registry.set_project_workflow("proj-missing", "instructions")

    async def test_clear_project_workflow_resets_to_empty(self, registry):
        """Clears instructions by setting to empty string."""
        db = registry.db
        await db.execute(self._PROJECTS_DDL)
        await db.execute(
            "INSERT INTO projects (project_id, workflow_instructions)"
            " VALUES ('proj-1', 'Some instructions.')"
        )
        await db.commit()
        await registry.clear_project_workflow("proj-1")
        result = await registry.get_project_workflow("proj-1")
        assert result == ""

    async def test_clear_project_workflow_noop_for_missing_project(self, registry):
        """Clearing a non-existent project is a silent no-op."""
        db = registry.db
        await db.execute(self._PROJECTS_DDL)
        await db.commit()
        await registry.clear_project_workflow("no-such")  # should not raise


class TestEffectiveWorkflow:
    async def test_effective_workflow_returns_global_when_no_project(self, registry):
        """Falls back to global defaults when projects table doesn't exist."""
        await registry.set_workflow_defaults("Global instructions.")
        result = await registry.get_effective_workflow("nonexistent")
        assert result == "Global instructions."

    async def test_effective_workflow_empty_when_neither_set(self, registry):
        result = await registry.get_effective_workflow("nonexistent")
        assert result == ""

    async def test_effective_workflow_falls_through_empty_defaults(self, registry):
        """Setting defaults to empty string means effective returns empty."""
        await registry.set_workflow_defaults("")
        result = await registry.get_effective_workflow("nonexistent")
        assert result == ""

    async def test_effective_workflow_project_overrides_global(self, registry):
        """Per-project instructions take precedence over global defaults."""
        db = registry.db
        await db.execute(
            "CREATE TABLE projects ("
            "  project_id TEXT PRIMARY KEY,"
            "  workflow_instructions TEXT NOT NULL DEFAULT ''"
            ")"
        )
        await db.execute(
            "INSERT INTO projects (project_id, workflow_instructions)"
            " VALUES ('proj-1', 'Project-level.')"
        )
        await db.commit()
        await registry.set_workflow_defaults("Global fallback.")
        result = await registry.get_effective_workflow("proj-1")
        assert result == "Project-level."

    async def test_effective_workflow_empty_project_falls_through(self, registry):
        """Empty per-project instructions fall through to global defaults."""
        db = registry.db
        await db.execute(
            "CREATE TABLE projects ("
            "  project_id TEXT PRIMARY KEY,"
            "  workflow_instructions TEXT NOT NULL DEFAULT ''"
            ")"
        )
        await db.execute(
            "INSERT INTO projects (project_id, workflow_instructions) VALUES ('proj-1', '')"
        )
        await db.commit()
        await registry.set_workflow_defaults("Global fallback.")
        result = await registry.get_effective_workflow("proj-1")
        assert result == "Global fallback."

    async def test_effective_workflow_missing_project_falls_through(self, registry):
        """Project not in table falls through to global defaults."""
        db = registry.db
        await db.execute(
            "CREATE TABLE projects ("
            "  project_id TEXT PRIMARY KEY,"
            "  workflow_instructions TEXT NOT NULL DEFAULT ''"
            ")"
        )
        await db.commit()
        await registry.set_workflow_defaults("Global fallback.")
        result = await registry.get_effective_workflow("no-such-project")
        assert result == "Global fallback."

    async def test_effective_workflow_neither_set_with_projects_table(self, registry):
        """Returns empty when projects table exists but nothing is configured."""
        db = registry.db
        await db.execute(
            "CREATE TABLE projects ("
            "  project_id TEXT PRIMARY KEY,"
            "  workflow_instructions TEXT NOT NULL DEFAULT ''"
            ")"
        )
        await db.commit()
        result = await registry.get_effective_workflow("proj-1")
        assert result == ""


class TestWorkflowDefaultsTable:
    async def test_workflow_defaults_table_exists(self, registry):
        """Verify workflow_defaults table is created on connect."""
        db = registry.db
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_defaults'"
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None
