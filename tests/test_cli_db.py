"""Tests for the 'summon db' CLI subgroup and config check DB validation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from click.testing import CliRunner

from summon_claude.cli import cli
from summon_claude.sessions.registry import SessionRegistry


async def _create_db(db_path):
    async with SessionRegistry(db_path=db_path):
        pass


class TestDbStatus:
    def test_db_status_reports_current(self, tmp_path):
        """'db status' should report schema version, integrity, and row counts."""
        runner = CliRunner()
        db_path = tmp_path / "registry.db"

        # Pre-create DB at current version
        asyncio.run(_create_db(db_path))

        with patch(
            "summon_claude.sessions.registry._default_db_path",
            return_value=db_path,
        ):
            result = runner.invoke(cli, ["db", "status"])
        assert result.exit_code == 0
        assert "Schema version: 3" in result.output
        assert "Integrity:" in result.output
        assert "Sessions:" in result.output

    def test_db_status_reports_migration_applied(self, tmp_path):
        """'db status' should report migration when schema was behind."""
        import aiosqlite

        runner = CliRunner()
        db_path = tmp_path / "registry.db"

        # Create DB at version 1, then manually downgrade to 0
        async def _setup():
            async with SessionRegistry(db_path=db_path):
                pass
            async with aiosqlite.connect(str(db_path), isolation_level=None) as raw_db:
                await raw_db.execute("UPDATE schema_version SET version = 0")

        asyncio.run(_setup())

        with patch(
            "summon_claude.sessions.registry._default_db_path",
            return_value=db_path,
        ):
            result = runner.invoke(cli, ["db", "status"])
        assert result.exit_code == 0
        assert "Migrated schema from version 0" in result.output


class TestDbReset:
    def test_db_reset_with_yes_recreates(self, tmp_path):
        """'db reset --yes' should recreate the database."""
        runner = CliRunner()
        # Create an initial DB so reset has something to delete
        db_path = tmp_path / "registry.db"
        asyncio.run(_create_db(db_path))
        assert db_path.exists()

        with patch(
            "summon_claude.sessions.registry._default_db_path",
            return_value=db_path,
        ):
            result = runner.invoke(cli, ["db", "reset", "--yes"])
        assert result.exit_code == 0
        assert "Database recreated" in result.output
        assert "Schema version:" in result.output

    def test_db_reset_without_yes_aborts(self, tmp_path):
        """'db reset' without --yes should prompt and abort if not confirmed."""
        runner = CliRunner()
        db_path = tmp_path / "registry.db"
        with patch(
            "summon_claude.sessions.registry._default_db_path",
            return_value=db_path,
        ):
            result = runner.invoke(cli, ["db", "reset"], input="n\n")
        assert result.exit_code != 0
        assert "Aborted" in result.output


class TestDbVacuum:
    def test_db_vacuum_runs(self, tmp_path):
        """'db vacuum' should report integrity status and size."""
        runner = CliRunner()
        db_path = tmp_path / "registry.db"
        asyncio.run(_create_db(db_path))

        with patch(
            "summon_claude.sessions.registry._default_db_path",
            return_value=db_path,
        ):
            result = runner.invoke(cli, ["db", "vacuum"])
        assert result.exit_code == 0
        assert "Integrity: ok" in result.output
        assert "Size:" in result.output

    def test_db_vacuum_missing_db(self, tmp_path):
        """'db vacuum' should fail gracefully when DB doesn't exist."""
        runner = CliRunner()
        db_path = tmp_path / "nonexistent.db"

        with patch(
            "summon_claude.sessions.registry._default_db_path",
            return_value=db_path,
        ):
            result = runner.invoke(cli, ["db", "vacuum"])
        assert "Database not found" in result.output


class TestDbPurge:
    def test_db_purge_deletes_old_rows(self, tmp_path):
        """'db purge --older-than 1' should delete sessions older than 1 day."""
        runner = CliRunner()
        db_path = tmp_path / "registry.db"
        old_ts = (datetime.now(UTC) - timedelta(days=5)).isoformat()

        async def _seed():
            async with SessionRegistry(db_path=db_path) as reg:
                await reg.register("old-sess-1", 111, "/tmp")
                await reg.update_status("old-sess-1", "completed")
                # Backdate the started_at to make it "old"
                await reg.db.execute(
                    "UPDATE sessions SET started_at = ? WHERE session_id = ?",
                    (old_ts, "old-sess-1"),
                )
                await reg.db.commit()

        asyncio.run(_seed())

        with patch(
            "summon_claude.sessions.registry._default_db_path",
            return_value=db_path,
        ):
            result = runner.invoke(cli, ["db", "purge", "--older-than", "1", "--yes"])
        assert result.exit_code == 0
        assert "Sessions:" in result.output
        # At least 1 session should have been purged
        assert "Sessions:     1" in result.output

    def test_db_purge_without_yes_aborts(self):
        """'db purge' without --yes should prompt and abort if not confirmed."""
        runner = CliRunner()
        result = runner.invoke(cli, ["db", "purge"], input="n\n")
        assert result.exit_code != 0
        assert "Aborted" in result.output

    def test_db_purge_keeps_recent(self, tmp_path):
        """'db purge' should not delete recent sessions."""
        runner = CliRunner()
        db_path = tmp_path / "registry.db"

        async def _seed():
            async with SessionRegistry(db_path=db_path) as reg:
                await reg.register("recent-sess", 222, "/tmp")
                await reg.update_status("recent-sess", "completed")

        asyncio.run(_seed())

        with patch(
            "summon_claude.sessions.registry._default_db_path",
            return_value=db_path,
        ):
            result = runner.invoke(cli, ["db", "purge", "--older-than", "30", "--yes"])
        assert result.exit_code == 0
        assert "Sessions:     0" in result.output

    def test_db_purge_deletes_old_audit_log(self, tmp_path):
        """'db purge' should delete audit log entries older than the cutoff."""
        runner = CliRunner()
        db_path = tmp_path / "registry.db"
        old_ts = (datetime.now(UTC) - timedelta(days=5)).isoformat()

        async def _seed():
            async with SessionRegistry(db_path=db_path) as reg:
                await reg.log_event("test_event", session_id="s1", details={"info": "old"})
                # Backdate the audit log entry
                await reg.db.execute(
                    "UPDATE audit_log SET timestamp = ?",
                    (old_ts,),
                )
                await reg.db.commit()

        asyncio.run(_seed())

        with patch(
            "summon_claude.sessions.registry._default_db_path",
            return_value=db_path,
        ):
            result = runner.invoke(cli, ["db", "purge", "--older-than", "1", "--yes"])
        assert result.exit_code == 0
        assert "Audit log:    1" in result.output

    def test_db_purge_deletes_expired_auth_tokens(self, tmp_path):
        """'db purge' should delete expired auth tokens older than the cutoff."""
        runner = CliRunner()
        db_path = tmp_path / "registry.db"
        old_expiry = (datetime.now(UTC) - timedelta(days=5)).isoformat()

        async def _seed():
            async with SessionRegistry(db_path=db_path) as reg:
                await reg.store_pending_token(
                    short_code="OLDTOKEN",
                    session_id="s1",
                    expires_at=old_expiry,
                )

        asyncio.run(_seed())

        with patch(
            "summon_claude.sessions.registry._default_db_path",
            return_value=db_path,
        ):
            result = runner.invoke(cli, ["db", "purge", "--older-than", "1", "--yes"])
        assert result.exit_code == 0
        assert "Auth tokens:  1" in result.output

    def test_db_purge_deletes_expired_spawn_tokens(self, tmp_path):
        """'db purge' should delete expired spawn tokens older than the cutoff."""
        runner = CliRunner()
        db_path = tmp_path / "registry.db"
        old_expiry = (datetime.now(UTC) - timedelta(days=5)).isoformat()

        async def _seed():
            async with SessionRegistry(db_path=db_path) as reg:
                await reg.store_spawn_token(
                    token="oldspawn1",
                    target_user_id="U999",
                    cwd="/tmp",
                    expires_at=old_expiry,
                )

        asyncio.run(_seed())

        with patch(
            "summon_claude.sessions.registry._default_db_path",
            return_value=db_path,
        ):
            result = runner.invoke(cli, ["db", "purge", "--older-than", "1", "--yes"])
        assert result.exit_code == 0
        assert "Spawn tokens: 1" in result.output


class TestConfigCheckDbValidation:
    """Tests for schema version and integrity checks in 'config check'."""

    def test_config_check_reports_schema_version(self, tmp_path):
        """'config check' should report schema version as PASS."""
        runner = CliRunner()
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-valid-token\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-valid-token\n"
            "SUMMON_SLACK_SIGNING_SECRET=abcd1234\n"
        )

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch("summon_claude.cli.config.get_data_dir", return_value=tmp_path),
        ):
            result = runner.invoke(cli, ["config", "check"])
        assert "[PASS] Schema version 3 (current)" in result.output

    def test_config_check_reports_integrity(self, tmp_path):
        """'config check' should report database integrity OK."""
        runner = CliRunner()
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-valid-token\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-valid-token\n"
            "SUMMON_SLACK_SIGNING_SECRET=abcd1234\n"
        )

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch("summon_claude.cli.config.get_data_dir", return_value=tmp_path),
        ):
            result = runner.invoke(cli, ["config", "check"])
        assert "[PASS] Database integrity OK" in result.output
