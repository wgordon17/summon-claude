"""Tests for summon_claude.cli.interactive helpers and CLI integration."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import click
from click.testing import CliRunner

from summon_claude.cli import cli
from summon_claude.cli.interactive import (
    format_log_option,
    format_session_option,
    interactive_multi_select,
    interactive_select,
    is_interactive,
)
from tests.conftest import ACTIVE_SESSION as _ACTIVE_SESSION
from tests.conftest import COMPLETED_SESSION as _COMPLETED_SESSION
from tests.conftest import mock_registry as _mock_registry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(*, no_interactive: bool = False, obj: dict | None = None) -> click.Context:
    """Build a minimal click.Context for testing."""
    ctx = click.Context(click.Command("test"))
    ctx.obj = obj if obj is not None else {"no_interactive": no_interactive}
    return ctx


# ---------------------------------------------------------------------------
# is_interactive
# ---------------------------------------------------------------------------


class TestIsInteractive:
    """Tests for is_interactive() TTY + flag check."""

    def test_returns_false_when_stdin_not_tty(self):
        ctx = _make_ctx()
        with patch("summon_claude.cli.interactive.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            assert is_interactive(ctx) is False

    def test_returns_false_when_no_interactive_flag_set(self):
        ctx = _make_ctx(no_interactive=True)
        with patch("summon_claude.cli.interactive.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            assert is_interactive(ctx) is False

    def test_returns_true_when_tty_and_no_flag(self):
        ctx = _make_ctx(no_interactive=False)
        with patch("summon_claude.cli.interactive.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            assert is_interactive(ctx) is True

    def test_handles_ctx_obj_none(self):
        ctx = click.Context(click.Command("test"))
        ctx.obj = None
        with patch("summon_claude.cli.interactive.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            # Should not raise, defaults to interactive
            assert is_interactive(ctx) is True

    def test_handles_missing_key_in_obj(self):
        ctx = click.Context(click.Command("test"))
        ctx.obj = {"other_key": True}
        with patch("summon_claude.cli.interactive.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            assert is_interactive(ctx) is True


# ---------------------------------------------------------------------------
# format_session_option
# ---------------------------------------------------------------------------


class TestFormatSessionOption:
    """Tests for format_session_option()."""

    def test_formats_active_session(self):
        result = format_session_option(_ACTIVE_SESSION)
        assert "aaaa1111" in result
        assert "my-proj" in result
        assert "[active]" in result

    def test_formats_completed_session(self):
        result = format_session_option(_COMPLETED_SESSION)
        assert "bbbb1111" in result
        assert "old-proj" in result
        assert "[completed]" in result

    def test_handles_missing_session_name(self):
        session = {"session_id": "cccc2222-3333-4444-5555-666677778888", "status": "active"}
        result = format_session_option(session)
        assert "cccc2222" in result
        assert "-" in result
        assert "[active]" in result

    def test_handles_none_session_name(self):
        session = {
            "session_id": "dddd3333-4444-5555-6666-777788889999",
            "session_name": None,
            "status": "errored",
        }
        result = format_session_option(session)
        assert "-" in result

    def test_handles_empty_dict(self):
        result = format_session_option({})
        assert "????????" in result
        assert "[?]" in result

    def test_truncates_id_to_8_chars(self):
        session = {"session_id": "abcdefgh-1234-5678-9012-345678901234", "status": "active"}
        result = format_session_option(session)
        assert "abcdefgh" in result
        assert "1234" not in result.split("  ")[0]


# ---------------------------------------------------------------------------
# format_log_option
# ---------------------------------------------------------------------------


class TestFormatLogOption:
    """Tests for format_log_option()."""

    def test_daemon_log(self, tmp_path):
        daemon = tmp_path / "daemon.log"
        daemon.write_text("test")
        result = format_log_option(daemon)
        assert result == "daemon    (daemon log)"

    def test_session_log_recent(self, tmp_path):
        log = tmp_path / "aaaa1111-2222-3333-4444-555566667777.log"
        log.write_text("test")
        result = format_log_option(log)
        assert "aaaa1111" in result
        assert "0m ago" in result

    def test_session_log_hours_old(self, tmp_path):
        log = tmp_path / "bbbb1111-2222-3333-4444-555566667777.log"
        log.write_text("test")
        with patch("summon_claude.cli.interactive.time.time", return_value=time.time() + 7200):
            result = format_log_option(log)
        assert "2h ago" in result

    def test_session_log_days_old(self, tmp_path):
        log = tmp_path / "cccc1111-2222-3333-4444-555566667777.log"
        log.write_text("test")
        with patch("summon_claude.cli.interactive.time.time", return_value=time.time() + 172800):
            result = format_log_option(log)
        assert "2d ago" in result

    def test_handles_string_path(self, tmp_path):
        log = tmp_path / "daemon.log"
        log.write_text("test")
        result = format_log_option(str(log))
        assert result == "daemon    (daemon log)"

    def test_handles_nonexistent_file(self, tmp_path):
        log = tmp_path / "missing.log"
        result = format_log_option(log)
        assert "(modified unknown)" in result


# ---------------------------------------------------------------------------
# interactive_select
# ---------------------------------------------------------------------------


class TestInteractiveSelect:
    """Tests for interactive_select()."""

    def test_returns_none_for_empty_options(self):
        ctx = _make_ctx()
        assert interactive_select([], "title", ctx) is None

    def test_non_interactive_fallback_with_input(self):
        """CliRunner provides non-TTY stdin, so fallback path fires."""
        ctx = _make_ctx()
        runner = CliRunner()
        result_container = {}

        @click.command()
        @click.pass_context
        def cmd(click_ctx):
            click_ctx.obj = ctx.obj
            result_container["result"] = interactive_select(
                ["Option A", "Option B"], "Pick one:", click_ctx
            )

        result = runner.invoke(cmd, input="2\n")
        assert result.exit_code == 0
        assert result_container["result"] == ("Option B", 1)
        assert "Pick one:" in result.output
        assert "1) Option A" in result.output
        assert "2) Option B" in result.output

    def test_interactive_calls_pick(self):
        ctx = _make_ctx()
        with (
            patch("summon_claude.cli.interactive.sys.stdin") as mock_stdin,
            patch("pick.pick", return_value=("Option B", 1)) as mock_pick,
        ):
            mock_stdin.isatty.return_value = True
            result = interactive_select(["Option A", "Option B"], "Pick one:", ctx)
        assert result == ("Option B", 1)
        mock_pick.assert_called_once_with(["Option A", "Option B"], "Pick one:", indicator=">")


# ---------------------------------------------------------------------------
# interactive_multi_select
# ---------------------------------------------------------------------------


class TestInteractiveMultiSelect:
    """Tests for interactive_multi_select()."""

    def test_returns_empty_for_empty_options(self):
        ctx = _make_ctx()
        assert interactive_multi_select([], "title", ctx) == []

    def test_non_interactive_fallback_comma_input(self):
        ctx = _make_ctx()
        runner = CliRunner()
        result_container = {}

        @click.command()
        @click.pass_context
        def cmd(click_ctx):
            click_ctx.obj = ctx.obj
            result_container["result"] = interactive_multi_select(
                ["A", "B", "C"], "Pick:", click_ctx
            )

        result = runner.invoke(cmd, input="1,3\n")
        assert result.exit_code == 0
        assert result_container["result"] == [("A", 0), ("C", 2)]

    def test_non_interactive_deduplicates(self):
        ctx = _make_ctx()
        runner = CliRunner()
        result_container = {}

        @click.command()
        @click.pass_context
        def cmd(click_ctx):
            click_ctx.obj = ctx.obj
            result_container["result"] = interactive_multi_select(["A", "B"], "Pick:", click_ctx)

        result = runner.invoke(cmd, input="1,1,2\n")
        assert result.exit_code == 0
        assert result_container["result"] == [("A", 0), ("B", 1)]

    def test_non_interactive_skips_invalid(self):
        ctx = _make_ctx()
        runner = CliRunner()
        result_container = {}

        @click.command()
        @click.pass_context
        def cmd(click_ctx):
            click_ctx.obj = ctx.obj
            result_container["result"] = interactive_multi_select(["A", "B"], "Pick:", click_ctx)

        result = runner.invoke(cmd, input="1,abc,99\n")
        assert result.exit_code == 0
        assert result_container["result"] == [("A", 0)]

    def test_interactive_calls_pick_multiselect(self):
        ctx = _make_ctx()
        with (
            patch("summon_claude.cli.interactive.sys.stdin") as mock_stdin,
            patch("pick.pick", return_value=[("A", 0), ("C", 2)]) as mock_pick,
        ):
            mock_stdin.isatty.return_value = True
            result = interactive_multi_select(["A", "B", "C"], "Pick:", ctx)
        assert result == [("A", 0), ("C", 2)]
        mock_pick.assert_called_once_with(
            ["A", "B", "C"], "Pick:", multiselect=True, min_selection_count=1, indicator=">"
        )


# ---------------------------------------------------------------------------
# CLI integration: --no-interactive flag
# ---------------------------------------------------------------------------


class TestNoInteractiveFlag:
    """Tests for the --no-interactive root CLI flag."""

    def test_flag_accepted(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--no-interactive", "--help"])
        assert result.exit_code == 0

    def test_flag_in_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert "--no-interactive" in result.output


# ---------------------------------------------------------------------------
# CLI integration: session logs interactive
# ---------------------------------------------------------------------------


class TestSessionLogsInteractive:
    """Tests for interactive session log selection."""

    def test_logs_no_args_non_interactive_lists_files(self, tmp_path):
        """Non-interactive (CliRunner) preserves old listing behavior."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "aaaa1111-2222-3333-4444-555566667777.log").write_text("line1\n")
        with patch("summon_claude.cli.get_data_dir", return_value=tmp_path):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "logs"])
        assert result.exit_code == 0
        assert "Available session logs:" in result.output

    def test_logs_no_args_daemon_log_exists(self, tmp_path):
        """When daemon.log exists, it appears in the listing."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "daemon.log").write_text("daemon line\n")
        (log_dir / "aaaa1111-2222-3333-4444-555566667777.log").write_text("session line\n")
        with patch("summon_claude.cli.get_data_dir", return_value=tmp_path):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "logs"])
        assert "daemon" in result.output
        assert "aaaa1111" in result.output


# ---------------------------------------------------------------------------
# CLI integration: stop no-args
# ---------------------------------------------------------------------------


class TestStopNoArgsInteractive:
    """Tests for interactive stop behavior with no session_id."""

    def test_stop_no_args_daemon_not_running(self):
        """No daemon → 'Daemon is not running.' exit 0."""
        runner = CliRunner()
        result = runner.invoke(cli, ["stop"])
        assert result.exit_code == 0
        assert "Daemon is not running" in result.output

    def test_stop_no_args_non_interactive_errors(self):
        """Non-interactive (CliRunner) + no args + daemon running → error exit 1.

        CliRunner is non-TTY, so is_interactive() returns False before
        list_sessions is ever called.
        """
        with patch("summon_claude.cli.is_daemon_running", return_value=True):
            runner = CliRunner()
            result = runner.invoke(cli, ["stop"])
        assert result.exit_code != 0
        assert "Provide SESSION_ID or --all" in result.output

    def test_stop_no_args_interactive_no_active_sessions(self):
        """Interactive mode, no active sessions → 'No active sessions.'."""
        with (
            patch("summon_claude.cli.is_daemon_running", return_value=True),
            patch("summon_claude.cli.is_interactive", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.list_sessions",
                new=AsyncMock(return_value=[]),
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["stop"])
        assert result.exit_code == 0
        assert "No active sessions" in result.output

    def test_stop_no_args_interactive_single_session_auto_selects(self):
        """Interactive mode, one active session → auto-selects and stops."""
        with (
            patch("summon_claude.cli.is_daemon_running", return_value=True),
            patch("summon_claude.cli.is_interactive", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.list_sessions",
                new=AsyncMock(return_value=[_ACTIVE_SESSION]),
            ),
            patch(
                "summon_claude.cli.daemon_client.stop_session",
                new=AsyncMock(return_value=True),
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["stop"])
        assert result.exit_code == 0
        assert "Auto-selecting" in result.output
        assert "Stop requested" in result.output


# ---------------------------------------------------------------------------
# CLI integration: cleanup multi-select
# ---------------------------------------------------------------------------


class TestCleanupInteractive:
    """Tests for interactive cleanup behavior."""

    def test_cleanup_non_interactive_cleans_all(self):
        """Non-interactive (CliRunner) cleans all stale sessions."""
        stale = [{**_ACTIVE_SESSION, "status": "active"}]
        mock_ctx = _mock_registry(stale=stale)
        with (
            patch("summon_claude.cli.SessionRegistry", return_value=mock_ctx),
            patch("summon_claude.cli.SummonConfig", side_effect=Exception("no config")),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "cleanup"])
        assert result.exit_code == 0
        assert "Cleaned up 1 stale session(s)." in result.output
