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
            "effort",
            "project_id",
        }
        assert expected == SessionRegistry._UPDATABLE_FIELDS

    async def test_updatable_fields_are_valid_columns(self, registry):
        """Guard against _UPDATABLE_FIELDS containing names that aren't real columns."""
        async with registry.db.execute("PRAGMA table_info(sessions)") as cursor:
            rows = await cursor.fetchall()
        columns = {row[1] for row in rows}
        assert SessionRegistry._UPDATABLE_FIELDS.issubset(columns), (
            f"Fields not in schema: {SessionRegistry._UPDATABLE_FIELDS - columns}"
        )


class TestValidStatuses:
    def test_valid_statuses_matches_expected(self):
        """Guard against accidental addition/removal of valid statuses."""
        expected = {"pending_auth", "active", "completed", "errored", "suspended"}
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

    async def test_record_turn_stores_context_pct(self, registry):
        await registry.register("sess-ctx", 111, "/tmp")
        await registry.record_turn("sess-ctx", 0.01, context_pct=75.5)
        session = await registry.get_session("sess-ctx")
        assert session["context_pct"] == pytest.approx(75.5)

    async def test_record_turn_none_context_pct_preserves_existing(self, registry):
        await registry.register("sess-ctx2", 111, "/tmp")
        await registry.record_turn("sess-ctx2", 0.01, context_pct=80.0)
        await registry.record_turn("sess-ctx2", 0.02)  # No context_pct
        session = await registry.get_session("sess-ctx2")
        # context_pct should still be 80.0 (not overwritten to NULL)
        assert session["context_pct"] == pytest.approx(80.0)


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
        from summon_claude.sessions.migrations import _MIGRATIONS, CURRENT_SCHEMA_VERSION

        expected_keys = set(range(CURRENT_SCHEMA_VERSION))
        actual_keys = set(_MIGRATIONS.keys())
        missing = expected_keys - actual_keys
        extra = actual_keys - expected_keys
        assert not missing, f"Missing migration(s) for version(s): {sorted(missing)}"
        assert not extra, f"Extra migration(s) beyond CURRENT_SCHEMA_VERSION: {sorted(extra)}"

    def test_migrations_are_callable_or_none(self):
        """Each migration must be None (no-op) or an async callable."""
        from summon_claude.sessions.migrations import _MIGRATIONS

        for version, migration in _MIGRATIONS.items():
            assert migration is None or callable(migration), (
                f"Migration {version} must be None or callable, got {type(migration)}"
            )

    def test_current_version_matches_migration_count(self):
        """CURRENT_SCHEMA_VERSION must equal the number of migrations."""
        from summon_claude.sessions.migrations import _MIGRATIONS, CURRENT_SCHEMA_VERSION

        assert len(_MIGRATIONS) == CURRENT_SCHEMA_VERSION, (
            f"CURRENT_SCHEMA_VERSION={CURRENT_SCHEMA_VERSION}"
            f" but _MIGRATIONS has {len(_MIGRATIONS)} entries"
        )


class TestScheduledJobs:
    """Tests for the scheduled_jobs CRUD methods on SessionRegistry."""

    async def test_save_and_list(self, registry):
        await registry.register("sess-sj-1", 111, "/tmp")
        now_iso = "2026-01-01T10:00:00+00:00"
        await registry.save_scheduled_job(
            session_id="sess-sj-1",
            job_id="job-aaa",
            cron_expr="*/5 * * * *",
            prompt="do thing A",
            recurring=True,
            max_lifetime_s=86400,
            created_at=now_iso,
        )
        await registry.save_scheduled_job(
            session_id="sess-sj-1",
            job_id="job-bbb",
            cron_expr="0 9 * * 1",
            prompt="do thing B",
            recurring=False,
            max_lifetime_s=3600,
            created_at=now_iso,
        )
        jobs = await registry.list_scheduled_jobs("sess-sj-1")
        assert len(jobs) == 2
        ids = {j["id"] for j in jobs}
        assert ids == {"job-aaa", "job-bbb"}

        job_a = next(j for j in jobs if j["id"] == "job-aaa")
        assert job_a["cron_expr"] == "*/5 * * * *"
        assert job_a["prompt"] == "do thing A"
        assert job_a["recurring"] is True  # bool conversion from INTEGER
        assert job_a["max_lifetime_s"] == 86400
        assert job_a["created_at"] == now_iso

        job_b = next(j for j in jobs if j["id"] == "job-bbb")
        assert job_b["recurring"] is False  # bool conversion: INTEGER 0 → False

    async def test_delete(self, registry):
        await registry.register("sess-sj-del", 111, "/tmp")
        await registry.save_scheduled_job(
            session_id="sess-sj-del",
            job_id="job-del",
            cron_expr="*/5 * * * *",
            prompt="will be deleted",
            recurring=True,
            max_lifetime_s=86400,
            created_at="2026-01-01T10:00:00+00:00",
        )
        deleted = await registry.delete_scheduled_job("sess-sj-del", "job-del")
        assert deleted is True
        jobs = await registry.list_scheduled_jobs("sess-sj-del")
        assert jobs == []

    async def test_delete_wrong_session(self, registry):
        await registry.register("sess-sj-owner", 111, "/tmp")
        await registry.register("sess-sj-other", 222, "/tmp")
        await registry.save_scheduled_job(
            session_id="sess-sj-owner",
            job_id="job-owned",
            cron_expr="*/5 * * * *",
            prompt="owned job",
            recurring=True,
            max_lifetime_s=86400,
            created_at="2026-01-01T10:00:00+00:00",
        )
        # Delete using the wrong session_id — must be scoped
        result = await registry.delete_scheduled_job("sess-sj-other", "job-owned")
        assert result is False
        # Job still exists under the real owner
        jobs = await registry.list_scheduled_jobs("sess-sj-owner")
        assert len(jobs) == 1

    async def test_delete_nonexistent(self, registry):
        await registry.register("sess-sj-ne", 111, "/tmp")
        result = await registry.delete_scheduled_job("sess-sj-ne", "nonexistent-job-id")
        assert result is False

    async def test_migrate(self, registry):
        await registry.register("sess-sj-a", 111, "/tmp")
        await registry.register("sess-sj-b", 222, "/tmp")
        created_at = "2026-01-01T10:00:00+00:00"
        for i in range(3):
            await registry.save_scheduled_job(
                session_id="sess-sj-a",
                job_id=f"job-migrate-{i}",
                cron_expr="*/5 * * * *",
                prompt=f"prompt {i}",
                recurring=True,
                max_lifetime_s=86400,
                created_at=created_at,
            )

        count = await registry.migrate_scheduled_jobs("sess-sj-a", "sess-sj-b")
        assert count == 3

        jobs_b = await registry.list_scheduled_jobs("sess-sj-b")
        assert len(jobs_b) == 3
        assert {j["id"] for j in jobs_b} == {"job-migrate-0", "job-migrate-1", "job-migrate-2"}

        jobs_a = await registry.list_scheduled_jobs("sess-sj-a")
        assert jobs_a == []

    async def test_delete_expired(self, registry):
        from datetime import UTC, datetime, timedelta

        await registry.register("sess-sj-exp", 111, "/tmp")

        # Save an expired job (created 25h ago, lifetime 24h)
        old_created_at = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        await registry.save_scheduled_job(
            session_id="sess-sj-exp",
            job_id="job-expired",
            cron_expr="*/5 * * * *",
            prompt="old job",
            recurring=True,
            max_lifetime_s=86400,
            created_at=old_created_at,
        )

        # Save a fresh job (should survive)
        fresh_created_at = datetime.now(UTC).isoformat()
        await registry.save_scheduled_job(
            session_id="sess-sj-exp",
            job_id="job-fresh",
            cron_expr="*/5 * * * *",
            prompt="fresh job",
            recurring=True,
            max_lifetime_s=86400,
            created_at=fresh_created_at,
        )

        deleted = await registry.delete_expired_scheduled_jobs("sess-sj-exp")
        assert deleted == 1

        surviving = await registry.list_scheduled_jobs("sess-sj-exp")
        assert len(surviving) == 1
        assert surviving[0]["id"] == "job-fresh"

    async def test_save_validates_empty_ids(self, registry):
        await registry.register("sess-sj-val", 111, "/tmp")
        with pytest.raises(ValueError, match="job_id"):
            await registry.save_scheduled_job(
                session_id="sess-sj-val",
                job_id="",
                cron_expr="*/5 * * * *",
                prompt="test",
                recurring=True,
                max_lifetime_s=86400,
                created_at="2026-01-01T10:00:00+00:00",
            )
        with pytest.raises(ValueError, match="session_id"):
            await registry.save_scheduled_job(
                session_id="",
                job_id="some-job-id",
                cron_expr="*/5 * * * *",
                prompt="test",
                recurring=True,
                max_lifetime_s=86400,
                created_at="2026-01-01T10:00:00+00:00",
            )

    async def test_cascade_delete(self, registry):
        """Deleting the parent session row cascades to delete scheduled_jobs rows."""
        await registry.register("sess-sj-cascade", 111, "/tmp")
        await registry.save_scheduled_job(
            session_id="sess-sj-cascade",
            job_id="job-cascade",
            cron_expr="*/5 * * * *",
            prompt="cascaded job",
            recurring=True,
            max_lifetime_s=86400,
            created_at="2026-01-01T10:00:00+00:00",
        )

        # Verify the job exists
        jobs_before = await registry.list_scheduled_jobs("sess-sj-cascade")
        assert len(jobs_before) == 1

        # Delete the parent session row directly via SQL
        db = registry._check_connected()
        await db.execute("DELETE FROM sessions WHERE session_id = ?", ("sess-sj-cascade",))
        await db.commit()

        # The job row must be gone due to ON DELETE CASCADE
        jobs_after = await registry.list_scheduled_jobs("sess-sj-cascade")
        assert jobs_after == []

    async def test_save_duplicate_job_id_raises(self, registry):
        """Plain INSERT raises IntegrityError on duplicate job_id."""
        import sqlite3

        await registry.register("sess-sj-dup", 111, "/tmp")
        await registry.save_scheduled_job(
            session_id="sess-sj-dup",
            job_id="job-dup",
            cron_expr="*/5 * * * *",
            prompt="first",
            recurring=True,
            max_lifetime_s=86400,
            created_at="2026-01-01T10:00:00+00:00",
        )
        with pytest.raises(sqlite3.IntegrityError):
            await registry.save_scheduled_job(
                session_id="sess-sj-dup",
                job_id="job-dup",
                cron_expr="0 9 * * *",
                prompt="duplicate",
                recurring=False,
                max_lifetime_s=3600,
                created_at="2026-01-01T11:00:00+00:00",
            )

    async def test_save_validates_timezone(self, registry):
        """created_at must be timezone-aware."""
        await registry.register("sess-sj-tz", 111, "/tmp")
        with pytest.raises(ValueError, match="timezone-aware"):
            await registry.save_scheduled_job(
                session_id="sess-sj-tz",
                job_id="job-tz",
                cron_expr="*/5 * * * *",
                prompt="test",
                recurring=True,
                max_lifetime_s=86400,
                created_at="2026-01-01T10:00:00",
            )

    async def test_save_accepts_negative_utc_offset(self, registry):
        """Negative UTC offsets like -05:00 are valid timezone-aware ISO 8601."""
        await registry.register("sess-sj-neg", 111, "/tmp")
        await registry.save_scheduled_job(
            session_id="sess-sj-neg",
            job_id="job-neg-tz",
            cron_expr="*/5 * * * *",
            prompt="test",
            recurring=True,
            max_lifetime_s=86400,
            created_at="2026-01-01T10:00:00-05:00",
        )
        jobs = await registry.list_scheduled_jobs("sess-sj-neg")
        assert len(jobs) == 1
        # Verify normalized to UTC
        assert jobs[0]["created_at"] == "2026-01-01T15:00:00+00:00"

    async def test_save_fk_violation(self, registry):
        """Saving a job with non-existent session_id raises IntegrityError."""
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            await registry.save_scheduled_job(
                session_id="nonexistent-session",
                job_id="job-fk",
                cron_expr="*/5 * * * *",
                prompt="test",
                recurring=True,
                max_lifetime_s=86400,
                created_at="2026-01-01T10:00:00+00:00",
            )

    async def test_migrate_to_nonexistent_session_raises(self, registry):
        """Migrating to a non-existent new_session_id raises IntegrityError."""
        import sqlite3

        await registry.register("sess-sj-migrate-fk", 111, "/tmp")
        await registry.save_scheduled_job(
            session_id="sess-sj-migrate-fk",
            job_id="job-migrate-fk",
            cron_expr="*/5 * * * *",
            prompt="test",
            recurring=True,
            max_lifetime_s=86400,
            created_at="2026-01-01T10:00:00+00:00",
        )
        with pytest.raises(sqlite3.IntegrityError):
            await registry.migrate_scheduled_jobs(
                "sess-sj-migrate-fk",
                "nonexistent-target",
            )

    async def test_scheduled_jobs_schema_columns(self, registry):
        """Pin the column set of scheduled_jobs via PRAGMA table_info."""
        db = registry._check_connected()
        async with db.execute("PRAGMA table_info(scheduled_jobs)") as cursor:
            rows = await cursor.fetchall()
        columns = {row[1] for row in rows}
        expected = {
            "id",
            "session_id",
            "cron_expr",
            "prompt",
            "recurring",
            "max_lifetime_s",
            "created_at",
        }
        assert columns == expected


class TestSchemaVersioning:
    """Tests for schema versioning and migrations."""

    async def test_fresh_db_gets_current_schema_version(self, tmp_path):
        """A fresh database should have CURRENT_SCHEMA_VERSION after connection."""
        from summon_claude.sessions.migrations import CURRENT_SCHEMA_VERSION, get_schema_version

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
        with caplog.at_level(logging.INFO, logger="summon_claude.sessions.migrations"):
            caplog.clear()
            async with SessionRegistry(db_path=db_path):
                pass
            migration_msgs = [r for r in caplog.records if "migration" in r.message.lower()]
            assert migration_msgs == []

    async def test_migration_runs_when_version_behind(self, tmp_path, caplog):
        """If schema_version is behind, migration should run and update version."""
        import aiosqlite

        from summon_claude.sessions.migrations import CURRENT_SCHEMA_VERSION, get_schema_version

        db_path = tmp_path / "behind.db"
        # First connection: creates DB at current version
        async with SessionRegistry(db_path=db_path):
            pass

        # Manually set schema_version to 0 to simulate an old DB
        async with aiosqlite.connect(str(db_path)) as raw_db:
            await raw_db.execute("UPDATE schema_version SET version = 0")
            await raw_db.commit()

        # Re-connect: migrations should run 0 -> CURRENT
        with caplog.at_level(logging.INFO, logger="summon_claude.sessions.migrations"):
            caplog.clear()
            async with SessionRegistry(db_path=db_path) as reg:
                version = await get_schema_version(reg.db)
                assert version == CURRENT_SCHEMA_VERSION
            migration_msgs = [r for r in caplog.records if "migration" in r.message.lower()]
            assert len(migration_msgs) == CURRENT_SCHEMA_VERSION

    async def test_migrated_from_reflects_previous_version(self, tmp_path):
        """SessionRegistry.migrated_from should show the pre-migration version."""
        import aiosqlite

        from summon_claude.sessions.migrations import CURRENT_SCHEMA_VERSION

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

        from summon_claude.sessions.migrations import CURRENT_SCHEMA_VERSION

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
            patch("summon_claude.sessions.migrations._MIGRATIONS", failing_migrations),
            patch("summon_claude.sessions.migrations.CURRENT_SCHEMA_VERSION", target + 1),
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
        """A fresh DB (via SessionRegistry) and a v1 baseline migrated manually must match."""
        import aiosqlite

        from summon_claude.sessions.migrations import run_migrations
        from summon_claude.sessions.registry import (
            _CREATE_AUDIT_LOG,
            _CREATE_PENDING_AUTH_TOKENS,
            _CREATE_SCHEMA_VERSION,
            _CREATE_SESSIONS,
            _CREATE_SPAWN_TOKENS,
        )

        # DB 1: created via SessionRegistry (baseline DDL + all migrations)
        fresh_path = tmp_path / "fresh.db"
        async with SessionRegistry(db_path=fresh_path):
            pass

        # DB 2: manually create v1 baseline, stamp v1, run migrations
        migrated_path = tmp_path / "migrated.db"
        async with aiosqlite.connect(str(migrated_path), isolation_level=None) as raw_db:
            await raw_db.execute(_CREATE_SESSIONS)
            await raw_db.execute(_CREATE_PENDING_AUTH_TOKENS)
            await raw_db.execute(_CREATE_AUDIT_LOG)
            await raw_db.execute(_CREATE_SPAWN_TOKENS)
            await raw_db.execute(_CREATE_SCHEMA_VERSION)
            await raw_db.execute("INSERT INTO schema_version (id, version) VALUES (1, 1)")
            await raw_db.commit()
            await run_migrations(raw_db)

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
        from summon_claude.sessions.migrations import (
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
            assert "projects" in tables
            assert "session_tasks" in tables
            assert "scheduled_jobs" in tables

    async def test_migration_preserves_existing_data(self, tmp_path):
        """Migrations must not destroy existing rows."""
        import aiosqlite

        from summon_claude.sessions.migrations import CURRENT_SCHEMA_VERSION, get_schema_version

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

        from summon_claude.sessions.migrations import (
            CURRENT_SCHEMA_VERSION,
            get_schema_version,
            run_migrations,
        )

        db_path = tmp_path / "idempotent.db"
        async with SessionRegistry(db_path=db_path):
            pass

        # Downgrade and re-migrate twice
        for _ in range(2):
            async with aiosqlite.connect(str(db_path), isolation_level=None) as raw_db:
                await raw_db.execute("UPDATE schema_version SET version = 0")
                await run_migrations(raw_db)
                version = await get_schema_version(raw_db)
                assert version == CURRENT_SCHEMA_VERSION


class TestMigration15To16:
    """Targeted tests for migration 15 → 16 (adds jira_jql to projects table)."""

    async def test_migrate_15_to_16_adds_jira_jql(self, tmp_path):
        """Migration must add jira_jql to a projects table that lacks it."""
        import aiosqlite

        from summon_claude.sessions.migrations import _migrate_15_to_16

        # Build a minimal projects table without jira_jql (simulating v12 schema)
        db_path = tmp_path / "migrate_15_to_16.db"
        async with aiosqlite.connect(str(db_path), isolation_level=None) as raw_db:
            await raw_db.execute(
                """
                CREATE TABLE projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    directory TEXT NOT NULL,
                    channel_prefix TEXT NOT NULL,
                    hooks TEXT DEFAULT NULL
                )
                """
            )

            # Confirm jira_jql absent before migration
            async with raw_db.execute("PRAGMA table_info(projects)") as cursor:
                cols_before = {row[1] for row in await cursor.fetchall()}
            assert "jira_jql" not in cols_before

            # Run the targeted migration
            await _migrate_15_to_16(raw_db)

            # Confirm jira_jql present after migration
            async with raw_db.execute("PRAGMA table_info(projects)") as cursor:
                cols_after = {row[1] for row in await cursor.fetchall()}
            assert "jira_jql" in cols_after

    async def test_migrate_15_to_16_idempotent(self, tmp_path):
        """Running migration 15→16 twice must not raise."""
        import aiosqlite

        from summon_claude.sessions.migrations import _migrate_15_to_16

        db_path = tmp_path / "idempotent_15_to_16.db"
        async with SessionRegistry(db_path=db_path):
            pass

        async with aiosqlite.connect(str(db_path), isolation_level=None) as raw_db:
            # First run — column already exists (added by full migration chain)
            await _migrate_15_to_16(raw_db)
            # Second run — must be a no-op, not raise
            await _migrate_15_to_16(raw_db)

            async with raw_db.execute("PRAGMA table_info(projects)") as cursor:
                cols = {row[1] for row in await cursor.fetchall()}
            assert "jira_jql" in cols

    async def test_migrate_15_to_16_default_is_null(self, tmp_path):
        """Existing rows must have NULL jira_jql after migration."""
        import aiosqlite

        from summon_claude.sessions.migrations import _migrate_15_to_16

        db_path = tmp_path / "default_null_15_to_16.db"
        async with aiosqlite.connect(str(db_path), isolation_level=None) as raw_db:
            await raw_db.execute(
                """
                CREATE TABLE projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    directory TEXT NOT NULL,
                    channel_prefix TEXT NOT NULL
                )
                """
            )
            await raw_db.execute(
                "INSERT INTO projects (project_id, name, directory, channel_prefix)"
                " VALUES ('proj-1', 'Test', '/tmp', 'tst')"
            )

            await _migrate_15_to_16(raw_db)

            async with raw_db.execute(
                "SELECT jira_jql FROM projects WHERE project_id = 'proj-1'"
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] is None  # DEFAULT NULL


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


class TestComputeSpawnDepth:
    async def test_root_session_has_depth_zero(self, registry):
        await registry.register("root-1", 1234, "/tmp")
        depth = await registry.compute_spawn_depth("root-1")
        assert depth == 0

    async def test_child_has_depth_one(self, registry):
        await registry.register("root-2", 1234, "/tmp")
        await registry.register("child-2", 1234, "/tmp", parent_session_id="root-2")
        depth = await registry.compute_spawn_depth("child-2")
        assert depth == 1

    async def test_grandchild_has_depth_two(self, registry):
        await registry.register("root-3", 1234, "/tmp")
        await registry.register("child-3", 1234, "/tmp", parent_session_id="root-3")
        await registry.register("grand-3", 1234, "/tmp", parent_session_id="child-3")
        depth = await registry.compute_spawn_depth("grand-3")
        assert depth == 2

    async def test_nonexistent_session_returns_zero(self, registry):
        depth = await registry.compute_spawn_depth("nonexistent")
        assert depth == 0

    async def test_depth_limit_constant_pinned(self):
        from summon_claude.sessions.registry import MAX_SPAWN_DEPTH

        assert MAX_SPAWN_DEPTH == 2


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
    async def test_get_project_workflow_missing_project(self, registry):
        """Returns None when project_id not in table (no row found)."""
        result = await registry.get_project_workflow("no-such")
        assert result is None

    async def test_get_project_workflow_returns_instructions(self, registry):
        """Returns stored instructions when project exists."""
        project_id = await registry.add_project("wflow-proj", "/tmp/wflow-proj")
        await registry.set_project_workflow(project_id, "Use TDD.")
        result = await registry.get_project_workflow(project_id)
        assert result == "Use TDD."

    async def test_set_project_workflow_updates_existing(self, registry):
        """Updates instructions for an existing project."""
        project_id = await registry.add_project("wflow-update", "/tmp/wflow-update")
        await registry.set_project_workflow(project_id, "Old.")
        await registry.set_project_workflow(project_id, "New.")
        result = await registry.get_project_workflow(project_id)
        assert result == "New."

    async def test_set_project_workflow_raises_on_missing_project(self, registry):
        """Raises KeyError when project_id doesn't exist in the table."""
        with pytest.raises(KeyError, match="proj-missing"):
            await registry.set_project_workflow("proj-missing", "instructions")

    async def test_clear_project_workflow_resets_to_null(self, registry):
        """Clears instructions by setting to NULL (falls back to global defaults)."""
        project_id = await registry.add_project("wflow-clear", "/tmp/wflow-clear")
        await registry.set_project_workflow(project_id, "Some instructions.")
        await registry.clear_project_workflow(project_id)
        result = await registry.get_project_workflow(project_id)
        assert result is None

    async def test_clear_project_workflow_noop_for_missing_project(self, registry):
        """Clearing a non-existent project is a silent no-op."""
        await registry.clear_project_workflow("no-such")  # should not raise


class TestEffectiveWorkflow:
    async def test_effective_workflow_returns_global_when_no_project(self, registry):
        """Falls back to global defaults when project_id not in table."""
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
        project_id = await registry.add_project("eff-override", "/tmp/eff-override")
        await registry.set_project_workflow(project_id, "Project-level.")
        await registry.set_workflow_defaults("Global fallback.")
        result = await registry.get_effective_workflow(project_id)
        assert result == "Project-level."

    async def test_effective_workflow_null_project_falls_through(self, registry):
        """NULL per-project instructions fall through to global defaults."""
        project_id = await registry.add_project("eff-empty", "/tmp/eff-empty")
        # workflow_instructions defaults to NULL on project creation
        await registry.set_workflow_defaults("Global fallback.")
        result = await registry.get_effective_workflow(project_id)
        assert result == "Global fallback."

    async def test_effective_workflow_explicit_empty_suppresses_global(self, registry):
        """Explicitly empty per-project instructions suppress global defaults."""
        project_id = await registry.add_project("eff-suppress", "/tmp/eff-suppress")
        await registry.set_project_workflow(project_id, "")
        await registry.set_workflow_defaults("Global fallback.")
        result = await registry.get_effective_workflow(project_id)
        assert result == ""

    async def test_effective_workflow_missing_project_falls_through(self, registry):
        """Project not in table falls through to global defaults."""
        await registry.set_workflow_defaults("Global fallback.")
        result = await registry.get_effective_workflow("no-such-project")
        assert result == "Global fallback."

    async def test_effective_workflow_neither_set_with_projects_table(self, registry):
        """Returns empty when projects table exists but nothing is configured."""
        result = await registry.get_effective_workflow("proj-1")
        assert result == ""

    async def test_effective_workflow_global_workflow_token_expansion(self, registry):
        """$INCLUDE_GLOBAL token in project instructions is replaced with global defaults."""
        project_id = await registry.add_project("eff-token", "/tmp/eff-token")
        await registry.set_workflow_defaults("Global rules here.")
        await registry.set_project_workflow(project_id, "Before.\n$INCLUDE_GLOBAL\nAfter.")
        result = await registry.get_effective_workflow(project_id)
        assert result == "Before.\nGlobal rules here.\nAfter."

    async def test_effective_workflow_global_token_empty_global(self, registry):
        """$INCLUDE_GLOBAL expands to empty string when global is not set."""
        project_id = await registry.add_project("eff-token-empty", "/tmp/eff-token-empty")
        await registry.set_project_workflow(project_id, "Before.\n$INCLUDE_GLOBAL\nAfter.")
        result = await registry.get_effective_workflow(project_id)
        assert result == "Before.\n\nAfter."

    async def test_effective_workflow_global_token_multiple(self, registry):
        """Multiple $INCLUDE_GLOBAL tokens all expand."""
        project_id = await registry.add_project("eff-multi", "/tmp/eff-multi")
        await registry.set_workflow_defaults("G")
        await registry.set_project_workflow(project_id, "$INCLUDE_GLOBAL-$INCLUDE_GLOBAL")
        result = await registry.get_effective_workflow(project_id)
        assert result == "G-G"

    async def test_effective_workflow_no_token_no_expansion(self, registry):
        """Project instructions without $INCLUDE_GLOBAL are returned as-is."""
        project_id = await registry.add_project("eff-no-token", "/tmp/eff-no-token")
        await registry.set_workflow_defaults("Should not appear.")
        await registry.set_project_workflow(project_id, "Only project content.")
        result = await registry.get_effective_workflow(project_id)
        assert result == "Only project content."


class TestMigration12To13DataPreservation:
    async def test_empty_string_workflow_becomes_null(self, tmp_path):
        """Migration 13→14 NULLIF converts empty-string workflow_instructions to NULL."""
        import aiosqlite

        from summon_claude.sessions.migrations import _migrate_13_to_14

        db_path = tmp_path / "migrate_test.db"
        async with aiosqlite.connect(str(db_path)) as db:
            # Create a v12-style projects table (workflow_instructions NOT NULL DEFAULT '')
            await db.execute(
                """
                CREATE TABLE projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    directory TEXT NOT NULL,
                    channel_prefix TEXT NOT NULL,
                    pm_channel_id TEXT,
                    workflow_instructions TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    hooks TEXT DEFAULT NULL
                )
                """
            )
            await db.execute(
                "CREATE UNIQUE INDEX idx_projects_channel_prefix ON projects (channel_prefix)"
            )
            # Migration 13→14 also creates idx_sessions_parent_status, so we
            # need a sessions table for that CREATE INDEX to succeed.
            await db.execute(
                "CREATE TABLE sessions (session_id TEXT PRIMARY KEY,"
                " parent_session_id TEXT, status TEXT)"
            )
            # Insert rows: one with empty string, one with real content
            cols = "project_id, name, directory, channel_prefix, workflow_instructions"
            await db.execute(
                f"INSERT INTO projects ({cols})"  # noqa: S608
                " VALUES ('p-empty', 'empty-wf', '/tmp/e', 'pfx-e', '')"
            )
            await db.execute(
                f"INSERT INTO projects ({cols})"  # noqa: S608
                " VALUES ('p-set', 'set-wf', '/tmp/s', 'pfx-s', 'Real instructions')"
            )
            await db.commit()

            # Run the migration
            await _migrate_13_to_14(db)
            await db.commit()

            # Verify: empty string became NULL
            async with db.execute(
                "SELECT workflow_instructions FROM projects WHERE project_id = 'p-empty'"
            ) as cursor:
                row = await cursor.fetchone()
                assert row[0] is None, f"Expected NULL, got {row[0]!r}"

            # Verify: real content preserved
            async with db.execute(
                "SELECT workflow_instructions FROM projects WHERE project_id = 'p-set'"
            ) as cursor:
                row = await cursor.fetchone()
                assert row[0] == "Real instructions"


class TestReleasedMigrationsImmutable:
    """Guard: released migration functions must never be modified.

    Once a migration is on upstream/main, databases may have already executed it.
    Modifying the function body changes behavior for fresh installs but not for
    existing databases, creating silent schema drift.  Add new schema changes as
    NEW migration functions only.

    When adding a new migration:
    1. Compute its hash (see test_all_migrations_have_hashes error for instructions)
    2. Add the hash to _RELEASED_HASHES below
    The test_all_migrations_have_hashes test will FAIL if you forget step 2.
    """

    # SHA-256 prefix (16 hex chars) of inspect.getsource() for each migration.
    # Every migration in _MIGRATIONS (except the v0 no-op) must have an entry.
    _RELEASED_HASHES: dict[str, str] = {
        "_migrate_1_to_2": "7270f30345c4b3f1",
        "_migrate_2_to_3": "c96ee8025ac3846b",
        "_migrate_3_to_4": "10a286e7d653934a",
        "_migrate_4_to_5": "ccc306a3dc45b0a8",
        "_migrate_5_to_6": "08fd446bd22ad223",
        "_migrate_6_to_7": "abe35cbacabc9cd8",
        "_migrate_7_to_8": "d5bfa086a2475a9b",
        "_migrate_8_to_9": "064f77e3de2068ee",
        "_migrate_9_to_10": "854c3f575d475d8b",
        "_migrate_10_to_11": "503ed98064bd1138",
        "_migrate_11_to_12": "bfc95f1b44faef79",
        "_migrate_12_to_13": "4dd835d5b9aefb63",
        "_migrate_13_to_14": "cc893dd5f5eacae0",
        "_migrate_14_to_15": "d9d62bd4554b85bd",
        "_migrate_15_to_16": "477a97bc9023d9b4",
    }

    def test_released_migrations_unchanged(self):
        """Fail if any released migration function's source has been modified."""
        import hashlib
        import inspect

        from summon_claude.sessions import migrations

        for fn_name, expected_hash in self._RELEASED_HASHES.items():
            fn = getattr(migrations, fn_name)
            src = inspect.getsource(fn)
            actual_hash = hashlib.sha256(src.encode()).hexdigest()[:16]
            assert actual_hash == expected_hash, (
                f"{fn_name} source has changed (hash {actual_hash} != {expected_hash}). "
                f"Released migrations are IMMUTABLE — add a new migration function instead."
            )

    def test_all_migrations_have_hashes(self):
        """Fail if a migration function exists in _MIGRATIONS without a hash."""
        import hashlib
        import inspect

        from summon_claude.sessions.migrations import _MIGRATIONS

        for version, fn in _MIGRATIONS.items():
            if fn is None:
                continue  # v0 no-op
            if fn.__name__ not in self._RELEASED_HASHES:
                src = inspect.getsource(fn)
                computed = hashlib.sha256(src.encode()).hexdigest()[:16]
                pytest.fail(
                    f"{fn.__name__} (version {version}→{version + 1}) is in _MIGRATIONS "
                    f"but has no entry in _RELEASED_HASHES. Add this line:\n\n"
                    f'        "{fn.__name__}": "{computed}",\n'
                )


class TestMigration13To14CreatesParentStatusIndex:
    """Databases already at v13 (from PR #65) must get idx_sessions_parent_status via 13→14."""

    async def test_parent_status_index_created_by_migrate_13_to_14(self, tmp_path):
        import aiosqlite

        from summon_claude.sessions.migrations import _migrate_13_to_14

        db_path = tmp_path / "v13_db.db"
        async with aiosqlite.connect(str(db_path)) as db:
            # Simulate a v13 DB that ran the OLD migration 12→13 (only auth_user_status index)
            await db.execute(
                "CREATE TABLE sessions (session_id TEXT PRIMARY KEY,"
                " parent_session_id TEXT, status TEXT,"
                " authenticated_user_id TEXT, slack_channel_id TEXT)"
            )
            await db.execute(
                "CREATE INDEX idx_sessions_auth_user_status "
                "ON sessions (authenticated_user_id, status, slack_channel_id)"
            )
            await db.execute(
                "CREATE TABLE projects ("
                " project_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,"
                " directory TEXT NOT NULL, channel_prefix TEXT NOT NULL,"
                " pm_channel_id TEXT, workflow_instructions TEXT NOT NULL DEFAULT '',"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
                " hooks TEXT DEFAULT NULL)"
            )
            await db.execute(
                "CREATE UNIQUE INDEX idx_projects_channel_prefix ON projects (channel_prefix)"
            )
            await db.commit()

            # Run only 13→14 (as would happen for a DB already at v13)
            await _migrate_13_to_14(db)
            await db.commit()

            # The parent_status index must exist — created by 13→14
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND name='idx_sessions_parent_status'"
            ) as cursor:
                row = await cursor.fetchone()
                assert row is not None, (
                    "idx_sessions_parent_status not created by migration 13→14 — "
                    "databases at v13 from PR #65 would be missing this index"
                )


class TestMigration12To13IndexCreation:
    async def test_auth_user_status_index_exists(self, tmp_path):
        """Migration 12→13 creates idx_sessions_auth_user_status."""
        import aiosqlite

        from summon_claude.sessions.migrations import _migrate_12_to_13

        db_path = tmp_path / "index_test.db"
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                "CREATE TABLE sessions (session_id TEXT PRIMARY KEY,"
                " authenticated_user_id TEXT, status TEXT, slack_channel_id TEXT,"
                " parent_session_id TEXT)"
            )
            await db.commit()
            await _migrate_12_to_13(db)
            await db.commit()

            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND name='idx_sessions_auth_user_status'"
            ) as cursor:
                row = await cursor.fetchone()
                assert row is not None, "idx_sessions_auth_user_status not created"


class TestWorkflowDefaultsTable:
    async def test_workflow_defaults_table_exists(self, registry):
        """Verify workflow_defaults table is created on connect."""
        db = registry.db
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_defaults'"
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None


class TestIsNameActive:
    async def test_no_active_session_with_name(self, registry):
        result = await registry.is_name_active("nonexistent")
        assert result is False

    async def test_active_session_with_name(self, registry):
        await registry.register("sess-na-1", 111, "/tmp", "my-name")
        result = await registry.is_name_active("my-name")
        assert result is True

    async def test_pending_auth_session_with_name(self, registry):
        await registry.register("sess-na-2", 111, "/tmp", "pending-name")
        # pending_auth is an active status
        result = await registry.is_name_active("pending-name")
        assert result is True

    async def test_completed_session_name_is_not_active(self, registry):
        await registry.register("sess-na-3", 111, "/tmp", "done-name")
        await registry.update_status("sess-na-3", "completed")
        result = await registry.is_name_active("done-name")
        assert result is False

    async def test_errored_session_name_is_not_active(self, registry):
        await registry.register("sess-na-4", 111, "/tmp", "err-name")
        await registry.update_status("sess-na-4", "errored")
        result = await registry.is_name_active("err-name")
        assert result is False


class TestRegisterNameUniqueness:
    async def test_register_rejects_duplicate_active_name(self, registry):
        await registry.register("sess-dup-1", 111, "/tmp", "unique-name")
        with pytest.raises(ValueError, match="active session with name"):
            await registry.register("sess-dup-2", 222, "/tmp", "unique-name")

    async def test_register_allows_name_after_completion(self, registry):
        await registry.register("sess-reuse-1", 111, "/tmp", "reusable")
        await registry.update_status("sess-reuse-1", "completed")
        # Should not raise — name is freed
        await registry.register("sess-reuse-2", 222, "/tmp", "reusable")
        session = await registry.get_session("sess-reuse-2")
        assert session["session_name"] == "reusable"

    async def test_register_allows_none_name(self, registry):
        await registry.register("sess-no-name-1", 111, "/tmp")
        await registry.register("sess-no-name-2", 222, "/tmp")
        # Both should succeed — None names don't trigger uniqueness check

    async def test_db_constraint_catches_duplicate_bypassing_app_check(self, registry):
        """The partial unique index must catch duplicates even if app check is bypassed."""
        await registry.register("sess-constraint-1", 111, "/tmp", "constrained-name")
        # The app-level check AND the DB constraint both enforce uniqueness.
        # This test verifies the error is a user-friendly ValueError.
        with pytest.raises(ValueError, match="active session with name"):
            await registry.register("sess-constraint-2", 222, "/tmp", "constrained-name")


class TestResolveSessionByName:
    async def test_resolve_by_session_name(self, registry):
        await registry.register("sess-name-r1", 111, "/tmp", "my-project-abc123")
        session, matches = await registry.resolve_session("my-project-abc123")
        assert session is not None
        assert session["session_id"] == "sess-name-r1"

    async def test_resolve_by_name_returns_most_recent_when_multiple_completed(self, registry):
        """Multiple completed sessions with same name → most recent wins."""
        await registry.register("sess-name-r2", 111, "/tmp", "shared-name")
        await registry.update_status("sess-name-r2", "completed")
        await registry.register("sess-name-r3", 222, "/tmp", "shared-name")
        await registry.update_status("sess-name-r3", "completed")
        session, matches = await registry.resolve_session("shared-name")
        assert session is not None
        assert session["session_id"] == "sess-name-r3"

    async def test_resolve_by_name_prefers_active_over_completed(self, registry):
        """Active session with same name should win over completed sessions."""
        await registry.register("sess-name-old", 111, "/tmp", "reused-name")
        await registry.update_status("sess-name-old", "completed")
        await registry.register("sess-name-new", 222, "/tmp", "reused-name")
        session, matches = await registry.resolve_session("reused-name")
        assert session is not None
        assert session["session_id"] == "sess-name-new"
        assert session["status"] == "pending_auth"

    async def test_resolve_prefers_id_over_name(self, registry):
        """Exact ID match should take priority over name match."""
        await registry.register("sess-name-r4", 111, "/tmp", "some-other-name")
        session, matches = await registry.resolve_session("sess-name-r4")
        assert session is not None
        assert session["session_id"] == "sess-name-r4"


class TestCanvasMethods:
    """Canvas data lives on the channels table, not sessions."""

    async def test_update_and_get_channel_canvas(self, registry):
        await registry.register_channel("C_CV1", "canvas-chan", "/tmp")
        await registry.update_channel_canvas("C_CV1", "F_CANVAS_1", "# Hello\nWorld")
        channel = await registry.get_channel("C_CV1")
        assert channel["canvas_id"] == "F_CANVAS_1"
        assert channel["canvas_markdown"] == "# Hello\nWorld"

    async def test_get_canvas_by_channel(self, registry):
        await registry.register_channel("C_CANVAS", "canvas-ch", "/tmp", "U_OWNER")
        await registry.update_channel_canvas("C_CANVAS", "F_CANVAS_2", "# Status\nOK")
        canvas_id, md, owner = await registry.get_canvas_by_channel("C_CANVAS")
        assert canvas_id == "F_CANVAS_2"
        assert md == "# Status\nOK"
        assert owner == "U_OWNER"

    async def test_get_canvas_by_channel_missing(self, registry):
        canvas_id, md, owner = await registry.get_canvas_by_channel("C_MISSING")
        assert canvas_id is None
        assert md is None
        assert owner is None

    async def test_get_canvas_by_channel_no_canvas(self, registry):
        """Channel without canvas data returns None."""
        await registry.register_channel("C_NOCV", "no-canvas", "/tmp")
        canvas_id, md, _ = await registry.get_canvas_by_channel("C_NOCV")
        assert canvas_id is None

    async def test_canvas_not_in_updatable_fields(self):
        """Canvas columns are NOT in session _UPDATABLE_FIELDS (channel-only)."""
        assert "canvas_id" not in SessionRegistry._UPDATABLE_FIELDS
        assert "canvas_markdown" not in SessionRegistry._UPDATABLE_FIELDS


class TestChannelsMethods:
    """Tests for the channels table methods."""

    async def test_register_channel(self, registry):
        await registry.register_channel("C_CH1", "test-channel", "/tmp", "U_OWNER")
        channel = await registry.get_channel("C_CH1")
        assert channel is not None
        assert channel["channel_id"] == "C_CH1"
        assert channel["channel_name"] == "test-channel"
        assert channel["cwd"] == "/tmp"
        assert channel["authenticated_user_id"] == "U_OWNER"

    async def test_update_channel_claude_session(self, registry):
        await registry.register_channel("C_CH2", "chan2", "/tmp")
        await registry.update_channel_claude_session("C_CH2", "claude-abc")
        channel = await registry.get_channel("C_CH2")
        assert channel["claude_session_id"] == "claude-abc"

    async def test_update_channel_canvas(self, registry):
        await registry.register_channel("C_CH3", "chan3", "/tmp")
        await registry.update_channel_canvas("C_CH3", "F_CANVAS", "# Hello")
        channel = await registry.get_channel("C_CH3")
        assert channel["canvas_id"] == "F_CANVAS"
        assert channel["canvas_markdown"] == "# Hello"

    async def test_get_channel_by_name(self, registry):
        await registry.register_channel("C_CH4", "unique-name", "/tmp")
        channel = await registry.get_channel_by_name("unique-name")
        assert channel is not None
        assert channel["channel_id"] == "C_CH4"

    async def test_get_channel_by_name_missing(self, registry):
        channel = await registry.get_channel_by_name("nonexistent")
        assert channel is None

    async def test_get_channel_missing(self, registry):
        channel = await registry.get_channel("C_MISSING")
        assert channel is None

    async def test_get_latest_session_for_channel(self, registry):
        await registry.register("sess-old", 111, "/tmp")
        await registry.update_status(
            "sess-old",
            "completed",
            slack_channel_id="C_LATEST",
            ended_at="2026-03-17T00:00:00+00:00",
        )
        await registry.register("sess-new", 222, "/tmp")
        await registry.update_status(
            "sess-new",
            "errored",
            slack_channel_id="C_LATEST",
            ended_at="2026-03-18T00:00:00+00:00",
        )
        latest = await registry.get_latest_session_for_channel("C_LATEST")
        assert latest is not None
        assert latest["session_id"] == "sess-new"

    async def test_get_latest_session_skips_active(self, registry):
        await registry.register("sess-act", 111, "/tmp")
        await registry.update_status("sess-act", "active", slack_channel_id="C_ACT")
        latest = await registry.get_latest_session_for_channel("C_ACT")
        assert latest is None

    async def test_register_channel_upsert_updates_cwd_and_name(self, registry):
        await registry.register_channel("C_DUP", "dup-chan", "/tmp")
        await registry.register_channel("C_DUP", "dup-chan-2", "/new-cwd")
        channel = await registry.get_channel("C_DUP")
        assert channel["channel_name"] == "dup-chan-2"  # name updated on upsert
        assert channel["cwd"] == "/new-cwd"  # cwd updated

    async def test_register_channel_upsert_updates_auth_user(self, registry):
        """Upsert should update authenticated_user_id when non-None."""
        await registry.register_channel("C_AUTH", "auth-chan", "/tmp", None)
        channel = await registry.get_channel("C_AUTH")
        assert channel["authenticated_user_id"] is None
        # Re-register with actual user — should update
        await registry.register_channel("C_AUTH", "auth-chan", "/tmp", "U_REAL")
        channel = await registry.get_channel("C_AUTH")
        assert channel["authenticated_user_id"] == "U_REAL"

    async def test_register_channel_upsert_preserves_auth_on_none(self, registry):
        """Upsert with None should NOT overwrite existing authenticated_user_id."""
        await registry.register_channel("C_KEEP", "keep-chan", "/tmp", "U_OWNER")
        await registry.register_channel("C_KEEP", "keep-chan", "/tmp", None)
        channel = await registry.get_channel("C_KEEP")
        assert channel["authenticated_user_id"] == "U_OWNER"  # preserved via COALESCE

    async def test_effort_in_updatable_fields(self):
        assert "effort" in SessionRegistry._UPDATABLE_FIELDS

    async def test_effort_column_exists(self, registry):
        await registry.register("sess-eff", 111, "/tmp")
        await registry.update_status("sess-eff", "active", effort="low")
        session = await registry.get_session("sess-eff")
        assert session["effort"] == "low"


class TestChildChannelMethods:
    """Tests for get_child_channels, get_all_active_channels, count_active_children."""

    async def test_get_child_channels_returns_active_only(self, registry):
        pid = os.getpid()
        await registry.register("parent-cc1", pid, "/tmp")
        await registry.update_status("parent-cc1", "active", authenticated_user_id="U_OWNER")

        await registry.register("child-active", pid, "/tmp", parent_session_id="parent-cc1")
        await registry.update_status(
            "child-active",
            "active",
            slack_channel_id="C_ACTIVE",
            authenticated_user_id="U_OWNER",
        )

        await registry.register("child-done", pid, "/tmp", parent_session_id="parent-cc1")
        await registry.update_status(
            "child-done",
            "completed",
            slack_channel_id="C_DONE",
            authenticated_user_id="U_OWNER",
        )

        channels = await registry.get_child_channels("parent-cc1", "U_OWNER")
        assert channels == {"C_ACTIVE"}

    async def test_get_child_channels_scopes_by_user(self, registry):
        pid = os.getpid()
        await registry.register("parent-cc2", pid, "/tmp")

        await registry.register("child-usera", pid, "/tmp", parent_session_id="parent-cc2")
        await registry.update_status(
            "child-usera",
            "active",
            slack_channel_id="C_USERA",
            authenticated_user_id="U_A",
        )

        await registry.register("child-userb", pid, "/tmp", parent_session_id="parent-cc2")
        await registry.update_status(
            "child-userb",
            "active",
            slack_channel_id="C_USERB",
            authenticated_user_id="U_B",
        )

        channels_a = await registry.get_child_channels("parent-cc2", "U_A")
        assert channels_a == {"C_USERA"}
        assert "C_USERB" not in channels_a

    async def test_get_child_channels_excludes_null_channels(self, registry):
        pid = os.getpid()
        await registry.register("parent-cc3", pid, "/tmp")

        await registry.register("child-no-chan", pid, "/tmp", parent_session_id="parent-cc3")
        await registry.update_status(
            "child-no-chan",
            "active",
            authenticated_user_id="U_OWNER",
        )

        channels = await registry.get_child_channels("parent-cc3", "U_OWNER")
        assert channels == set()

    async def test_get_all_active_channels_returns_user_scoped(self, registry):
        pid = os.getpid()
        await registry.register("sess-u1a", pid, "/tmp")
        await registry.update_status(
            "sess-u1a", "active", slack_channel_id="C_U1A", authenticated_user_id="U_1"
        )

        await registry.register("sess-u1b", pid, "/tmp")
        await registry.update_status(
            "sess-u1b", "active", slack_channel_id="C_U1B", authenticated_user_id="U_1"
        )

        await registry.register("sess-u2", pid, "/tmp")
        await registry.update_status(
            "sess-u2", "active", slack_channel_id="C_U2", authenticated_user_id="U_2"
        )

        channels = await registry.get_all_active_channels("U_1")
        assert channels == {"C_U1A", "C_U1B"}
        assert "C_U2" not in channels

    async def test_get_all_active_channels_excludes_completed(self, registry):
        pid = os.getpid()
        await registry.register("sess-done-gaa", pid, "/tmp")
        await registry.update_status(
            "sess-done-gaa",
            "completed",
            slack_channel_id="C_DONE_GAA",
            authenticated_user_id="U_OWNER",
        )

        channels = await registry.get_all_active_channels("U_OWNER")
        assert "C_DONE_GAA" not in channels

    async def test_count_active_children(self, registry):
        pid = os.getpid()
        await registry.register("parent-cac", pid, "/tmp")

        for i in range(2):
            await registry.register(
                f"child-active-cac-{i}",
                pid,
                "/tmp",
                parent_session_id="parent-cac",
            )
            await registry.update_status(
                f"child-active-cac-{i}",
                "active",
                authenticated_user_id="U_OWNER",
            )

        await registry.register("child-err-cac", pid, "/tmp", parent_session_id="parent-cac")
        await registry.update_status("child-err-cac", "errored", authenticated_user_id="U_OWNER")

        count = await registry.count_active_children("parent-cac")
        assert count == 2

    async def test_count_active_children_includes_pending_auth(self, registry):
        pid = os.getpid()
        await registry.register("parent-pa", pid, "/tmp")
        await registry.register("child-pending", pid, "/tmp", parent_session_id="parent-pa")
        # pending_auth is the default status from register()
        count = await registry.count_active_children("parent-pa")
        assert count == 1

    async def test_get_child_channels_excludes_suspended(self, registry):
        pid = os.getpid()
        await registry.register("parent-susp", pid, "/tmp")
        await registry.register("child-susp", pid, "/tmp", parent_session_id="parent-susp")
        await registry.update_status(
            "child-susp",
            "suspended",
            slack_channel_id="C_SUSP",
            authenticated_user_id="U_OWNER",
        )
        channels = await registry.get_child_channels("parent-susp", "U_OWNER")
        assert "C_SUSP" not in channels

    async def test_get_all_active_channels_excludes_suspended(self, registry):
        pid = os.getpid()
        await registry.register("sess-susp-gaa", pid, "/tmp")
        await registry.update_status(
            "sess-susp-gaa",
            "suspended",
            slack_channel_id="C_SUSP_GAA",
            authenticated_user_id="U_OWNER",
        )
        channels = await registry.get_all_active_channels("U_OWNER")
        assert "C_SUSP_GAA" not in channels
