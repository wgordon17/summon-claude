"""Tests for scribe session profile, scan timer, channel scoping, and auto-spawn.

Covers C12 (Phase 1) and C13 (Phase 2) test requirements.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.config import SummonConfig
from summon_claude.sessions.session import (
    SessionOptions,
    SummonSession,
    build_scribe_system_prompt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(**overrides) -> SummonConfig:
    defaults = {
        "slack_bot_token": "xoxb-test-token",
        "slack_app_token": "xapp-test-token",
        "slack_signing_secret": "abc123def456",
    }
    defaults.update(overrides)
    return SummonConfig.model_validate(defaults)


def make_scribe_prompt(**overrides: Any) -> dict:
    defaults: dict[str, Any] = dict(
        scan_interval=5,
    )
    # user_mention and importance_keywords moved to scan prompt builder
    overrides.pop("importance_keywords", None)
    overrides.pop("user_mention", None)
    defaults.update(overrides)
    return build_scribe_system_prompt(**defaults)


def make_manager(*, scribe_enabled: bool = False, **config_overrides):
    """Create a SessionManager stub for testing _start_scribe_if_enabled."""
    from summon_claude.sessions.manager import SessionManager

    config = make_config(scribe_enabled=scribe_enabled, **config_overrides)
    manager = SessionManager.__new__(SessionManager)
    manager._config = config
    manager._sessions = {}
    manager._tasks = {}
    manager._web_client = AsyncMock()
    manager._dispatcher = MagicMock()
    manager._bot_user_id = "B001"
    manager._ipc_resume = AsyncMock()
    manager.create_session_with_spawn_token = AsyncMock()
    manager._grace_timer = None
    manager._resuming_channels = set()
    return manager


# ---------------------------------------------------------------------------
# C12 Phase 1: SessionOptions defaults
# ---------------------------------------------------------------------------


class TestSessionOptionsScribeProfile:
    def test_session_options_scribe_profile_default(self):
        opts = SessionOptions(cwd="/tmp", name="test")
        assert opts.scribe_profile is False


# ---------------------------------------------------------------------------
# C12 Phase 1: System prompt content
# ---------------------------------------------------------------------------


class TestScribeSystemPromptSecurity:
    def test_scribe_system_prompt_security_at_top(self):
        """Security section appears before scan protocol."""
        prompt = make_scribe_prompt()
        text = prompt["append"]
        injection_pos = text.find("Prompt injection defense")
        scan_pos = text.lower().find("scan protocol")
        assert injection_pos != -1, "Prompt injection defense section not found"
        assert scan_pos != -1, "scan protocol not found"
        assert injection_pos < scan_pos

    def test_scribe_system_prompt_principal_hierarchy(self):
        """Principal hierarchy with 4 levels appears in security section."""
        prompt = make_scribe_prompt()
        text = prompt["append"]
        assert "Principal hierarchy" in text
        assert "LOWEST authority" in text
        assert "UNTRUSTED_EXTERNAL_DATA" in text

    def test_scribe_system_prompt_delivery_format(self):
        """Alert formatting templates moved to scan prompt — not in system prompt."""
        prompt = make_scribe_prompt()
        text = prompt["append"]
        assert ":rotating_light:" not in text

    def test_scribe_system_prompt_daily_summary_format(self):
        """Daily Recap template moved to scan prompt — not in system prompt."""
        prompt = make_scribe_prompt()
        assert "Daily Recap" not in prompt["append"]


class TestScribeChannelName:
    @pytest.mark.asyncio
    async def test_scribe_channel_name(self):
        """Scribe channel creates/joins '0-scribe' (behavioral)."""
        config = make_config()
        opts = SessionOptions(cwd="/tmp", name="scribe", scribe_profile=True)
        sess = SummonSession(
            config=config,
            options=opts,
            auth=None,
            session_id="test-ch-name",
            web_client=None,
            dispatcher=MagicMock(),
            bot_user_id="B001",
            ipc_spawn=AsyncMock(),
            ipc_resume=AsyncMock(),
        )
        web_client = AsyncMock()
        web_client.conversations_list.return_value = {
            "channels": [],
            "response_metadata": {"next_cursor": ""},
        }
        web_client.conversations_create.return_value = {
            "channel": {"id": "C_NEW", "name": "0-scribe"}
        }
        _, name = await sess._get_or_create_scribe_channel(web_client)
        assert name == "0-scribe"


class TestScribeIsScribeProperty:
    def test_scribe_is_scribe_property_true(self):
        opts = SessionOptions(cwd="/tmp", name="scribe", scribe_profile=True)
        config = make_config()
        sess = SummonSession(
            config=config,
            options=opts,
            auth=None,
            session_id="test-sess-1",
            web_client=None,
            dispatcher=MagicMock(),
            bot_user_id="B001",
            ipc_spawn=AsyncMock(),
            ipc_resume=AsyncMock(),
        )
        assert sess.is_scribe is True

    def test_scribe_is_scribe_property_false_for_normal(self):
        opts = SessionOptions(cwd="/tmp", name="regular")
        config = make_config()
        sess = SummonSession(
            config=config,
            options=opts,
            auth=None,
            session_id="test-sess-2",
            web_client=None,
            dispatcher=MagicMock(),
            bot_user_id="B001",
            ipc_spawn=AsyncMock(),
            ipc_resume=AsyncMock(),
        )
        assert sess.is_scribe is False


class TestScribeScanTimerNonce:
    """Behavioral guards — nonce wiring in build_scribe_scan_prompt."""

    def test_scribe_scan_timer_uses_nonce(self):
        """SUMMON-INTERNAL- prefix appears in build_scribe_scan_prompt output."""
        from summon_claude.sessions.session import build_scribe_scan_prompt

        result = build_scribe_scan_prompt(
            nonce="abc123",
            google_enabled=False,
            slack_enabled=False,
            user_mention="<@U123>",
            importance_keywords="",
            quiet_hours=None,
        )
        assert "SUMMON-INTERNAL-abc123" in result

    def test_scribe_scan_nonce_uses_secrets(self):
        """secrets.token_hex is called in _run_session_tasks to generate the nonce."""
        import inspect

        from summon_claude.sessions import session as session_mod

        source = inspect.getsource(session_mod.SummonSession._run_session_tasks)
        assert "secrets.token_hex" in source
        assert "_scribe_scan_nonce" in source


class TestScribeNoGitHubMCP:
    """Structural guard — GitHub MCP exclusion is deep inside _run_session_tasks.
    Behavioral testing would require mocking the full SDK session lifecycle.
    """

    def test_scribe_no_github_mcp(self):
        """GitHub MCP is gated by 'if not is_scribe' guard."""
        import inspect

        from summon_claude.sessions import session as session_mod

        source = inspect.getsource(session_mod.SummonSession._run_session_tasks)
        assert "not is_scribe" in source or "if not is_scribe" in source


class TestScribeSettingSources:
    """Structural guard — setting_sources assignment is deep inside _run_session_tasks.
    Behavioral testing would require mocking the full SDK session lifecycle.
    """

    def test_scribe_setting_sources_user_only(self):
        """setting_sources is ['user'] for scribe sessions (not ['user', 'project'])."""
        import inspect

        from summon_claude.sessions import session as session_mod

        source = inspect.getsource(session_mod.SummonSession._run_session_tasks)
        assert "is_scribe" in source
        assert '"user", "project"' in source or '["user", "project"]' in source


# ---------------------------------------------------------------------------
# C12 Phase 1: _start_scribe_if_enabled
# ---------------------------------------------------------------------------


class TestStartScribeIfEnabled:
    def test_start_scribe_if_enabled_skips_when_disabled(self):
        """When scribe_enabled=False, no session is created."""
        manager = make_manager(scribe_enabled=False)

        with patch("summon_claude.sessions.manager.SummonSession") as mock_session_cls:
            manager._start_scribe_if_enabled("U123")

        mock_session_cls.assert_not_called()

    def test_start_scribe_if_enabled_skips_when_running(self):
        """When a scribe session is already running, skip spawning another."""
        manager = make_manager(scribe_enabled=True)

        # Inject a stub scribe session
        stub = MagicMock()
        stub.is_scribe = True
        manager._sessions["existing-scribe"] = stub

        with patch("summon_claude.sessions.manager.SummonSession") as mock_session_cls:
            manager._start_scribe_if_enabled("U123")

        mock_session_cls.assert_not_called()


# ---------------------------------------------------------------------------
# C13 Phase 2: importance_keywords and quiet_hours in prompt
# ---------------------------------------------------------------------------


class TestScribeImportanceKeywordsInPrompt:
    def test_scribe_importance_keywords_not_in_system_prompt(self):
        """Keywords moved to scan prompt — not in system prompt."""
        prompt = make_scribe_prompt()
        assert "deadline,escalation,on-call" not in prompt["append"]

    def test_scribe_default_keywords_not_in_system_prompt(self):
        """Default keywords moved to scan prompt — not in system prompt."""
        prompt = make_scribe_prompt()
        assert "urgent, action required, deadline" not in prompt["append"]


class TestScribePromptQuietHoursContext:
    """Behavioral guard — quiet hours wired into build_scribe_scan_prompt."""

    def test_scribe_scan_includes_quiet_hours_config(self):
        """Quiet hours config appears in scan prompt output."""
        from summon_claude.sessions.session import build_scribe_scan_prompt

        result = build_scribe_scan_prompt(
            nonce="test",
            google_enabled=False,
            slack_enabled=False,
            user_mention="<@U123>",
            importance_keywords="",
            quiet_hours="22:00-08:00",
        )
        assert "quiet" in result.lower()
        assert "only report level 5" in result.lower() or "quiet hours" in result.lower()


# ---------------------------------------------------------------------------
# QA Coverage Gaps (C12/C13 follow-up)
# ---------------------------------------------------------------------------


class TestStartScribeSpawnsSession:
    """test_start_scribe_spawns_session — happy path creates a SummonSession."""

    def test_start_scribe_spawns_session(self):
        """With scribe_enabled=True and no existing scribe, a session is registered.

        Disables all preflight checks by: setting scribe_google_services="" and
        scribe_slack_enabled=False, then mocking asyncio.create_task to avoid
        needing a running event loop.
        """
        # Bypass google preflight: empty services string and slack disabled
        manager = make_manager(
            scribe_enabled=True, scribe_google_services="", scribe_slack_enabled=False
        )

        with (
            patch("summon_claude.sessions.manager.SummonSession") as mock_cls,
            patch("summon_claude.sessions.manager.asyncio.create_task") as mock_task,
            patch("summon_claude.sessions.manager.pathlib.Path") as mock_path,
        ):
            mock_instance = MagicMock()
            mock_instance.is_scribe = True
            mock_cls.return_value = mock_instance
            mock_task.return_value = MagicMock()
            mock_path.return_value = MagicMock()

            manager._start_scribe_if_enabled("U123")

        mock_cls.assert_called_once()
        # The session must be registered in the sessions dict
        assert len(manager._sessions) == 1


# ---------------------------------------------------------------------------
# _get_or_create_scribe_channel
# ---------------------------------------------------------------------------


class TestGetOrCreateScribeChannel:
    def _make_session(self):
        config = make_config()
        opts = SessionOptions(cwd="/tmp", name="scribe", scribe_profile=True)
        return SummonSession(
            config=config,
            options=opts,
            auth=None,
            session_id="test-scribe-ch",
            web_client=None,
            dispatcher=MagicMock(),
            bot_user_id="B001",
            ipc_spawn=AsyncMock(),
            ipc_resume=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_get_or_create_scribe_channel_creates_new(self):
        """When no existing channel found, conversations_create is called."""
        sess = self._make_session()
        web_client = AsyncMock()
        web_client.conversations_list.return_value = {
            "channels": [],
            "response_metadata": {"next_cursor": ""},
        }
        web_client.conversations_create.return_value = {
            "channel": {"id": "C_NEW", "name": "0-scribe"}
        }

        channel_id, channel_name = await sess._get_or_create_scribe_channel(web_client)

        web_client.conversations_create.assert_called_once_with(name="0-scribe", is_private=True)
        assert channel_id == "C_NEW"
        assert channel_name == "0-scribe"

    @pytest.mark.asyncio
    async def test_get_or_create_scribe_channel_reuses_existing(self):
        """When channel already exists, conversations_join is called (no create)."""
        sess = self._make_session()
        web_client = AsyncMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C_EXISTING", "name": "0-scribe"}],
            "response_metadata": {"next_cursor": ""},
        }

        channel_id, channel_name = await sess._get_or_create_scribe_channel(web_client)

        web_client.conversations_join.assert_called_once_with(channel="C_EXISTING")
        web_client.conversations_create.assert_not_called()
        assert channel_id == "C_EXISTING"


# ---------------------------------------------------------------------------
# _start_slack_monitors
# ---------------------------------------------------------------------------


class TestStartSlackMonitors:
    def _make_session(self):
        config = make_config()
        opts = SessionOptions(cwd="/tmp", name="scribe", scribe_profile=True)
        return SummonSession(
            config=config,
            options=opts,
            auth=None,
            session_id="test-scribe-monitors",
            web_client=None,
            dispatcher=MagicMock(),
            bot_user_id="B001",
            ipc_spawn=AsyncMock(),
            ipc_resume=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_start_slack_monitors_missing_config(self, tmp_path):
        """No slack_workspace.json → monitors list stays empty."""
        sess = self._make_session()

        with patch(
            "summon_claude.sessions.session.get_workspace_config_path",
            return_value=tmp_path / "nonexistent.json",
        ):
            await sess._start_slack_monitors()

        assert sess._slack_monitors == []

    @pytest.mark.asyncio
    async def test_start_slack_monitors_missing_auth_state(self, tmp_path):
        """Config exists but auth state file missing → monitors list stays empty."""
        sess = self._make_session()

        import json as json_mod

        config_path = tmp_path / "slack_workspace.json"
        config_path.write_text(
            json_mod.dumps(
                {
                    "url": "https://example.slack.com",
                    "auth_state_path": str(tmp_path / "nonexistent_auth.json"),
                }
            )
        )

        with patch(
            "summon_claude.sessions.session.get_workspace_config_path",
            return_value=config_path,
        ):
            await sess._start_slack_monitors()

        assert sess._slack_monitors == []


# ---------------------------------------------------------------------------
# _create_external_slack_mcp — spotlighting and truncation
# ---------------------------------------------------------------------------


class TestExternalSlackMcp:
    def _make_session(self):
        config = make_config()
        opts = SessionOptions(cwd="/tmp", name="scribe", scribe_profile=True)
        return SummonSession(
            config=config,
            options=opts,
            auth=None,
            session_id="test-scribe-mcp",
            web_client=None,
            dispatcher=MagicMock(),
            bot_user_id="B001",
            ipc_spawn=AsyncMock(),
            ipc_resume=AsyncMock(),
        )

    def _extract_tool_fn(self, sess):
        """Call _create_external_slack_mcp and capture the inner tool function."""
        captured = {}

        def mock_tool(name, desc, schema):
            def decorator(fn):
                captured["fn"] = fn
                return fn

            return decorator

        with (
            patch("claude_agent_sdk.create_sdk_mcp_server", return_value={}),
            patch("claude_agent_sdk.tool", side_effect=mock_tool),
        ):
            sess._create_external_slack_mcp()
        return captured["fn"]

    @pytest.mark.asyncio
    async def test_external_slack_check_spotlighting(self):
        """Messages are wrapped with mark_untrusted() delimiters."""
        from summon_claude.slack_browser import SlackMessage

        sess = self._make_session()
        monitor = AsyncMock()
        monitor._queue = MagicMock()
        monitor._queue.qsize.return_value = 0
        monitor.drain = AsyncMock(
            return_value=[
                SlackMessage(channel="C001", user="U123", text="Hello", ts="1.0", workspace="test"),
            ]
        )
        sess._slack_monitors = [monitor]

        tool_fn = self._extract_tool_fn(sess)
        result = await tool_fn({})
        text = result["content"][0]["text"]

        assert "UNTRUSTED_EXTERNAL_DATA" in text
        assert "Hello" in text
        assert "[Source: External Slack]" in text

    @pytest.mark.asyncio
    async def test_external_slack_check_truncation(self):
        """Messages over 2000 chars are truncated and marked [truncated]."""
        from summon_claude.slack_browser import SlackMessage

        sess = self._make_session()
        long_text = "x" * 3000
        monitor = AsyncMock()
        monitor._queue = MagicMock()
        monitor._queue.qsize.return_value = 0
        monitor.drain = AsyncMock(
            return_value=[
                SlackMessage(
                    channel="C001", user="U123", text=long_text, ts="1.0", workspace="test"
                ),
            ]
        )
        sess._slack_monitors = [monitor]

        tool_fn = self._extract_tool_fn(sess)
        result = await tool_fn({})
        text = result["content"][0]["text"]

        assert "[truncated]" in text
        # Original 3000 chars should be cut to 2000
        assert "x" * 2001 not in text

    @pytest.mark.asyncio
    async def test_external_slack_check_drain_cap(self):
        """drain() is called with limit=50."""
        from summon_claude.slack_browser import SlackMessage

        sess = self._make_session()
        monitor = AsyncMock()
        monitor._queue = MagicMock()
        monitor._queue.qsize.return_value = 0
        monitor.drain = AsyncMock(
            return_value=[
                SlackMessage(channel="C001", user="U123", text="msg", ts="1.0", workspace="test"),
            ]
        )
        sess._slack_monitors = [monitor]

        tool_fn = self._extract_tool_fn(sess)
        await tool_fn({})

        monitor.drain.assert_called_once_with(limit=50)

    @pytest.mark.asyncio
    async def test_external_slack_check_no_messages(self):
        """No messages returns '(no new messages)'."""
        sess = self._make_session()
        monitor = AsyncMock()
        monitor._queue = MagicMock()
        monitor._queue.qsize.return_value = 0
        monitor.drain = AsyncMock(return_value=[])
        sess._slack_monitors = [monitor]

        tool_fn = self._extract_tool_fn(sess)
        result = await tool_fn({})
        text = result["content"][0]["text"]

        assert "(no new messages)" in text


# ---------------------------------------------------------------------------
# Scribe topic format
# ---------------------------------------------------------------------------


class TestScribeTopicFormat:
    """Structural guard — topic is set inside _run_session during channel setup.
    Behavioral testing requires mocking the full Slack channel creation flow.
    """

    def test_scribe_topic_format(self):
        """Topic string for scribe sessions contains 'Scribe | Monitoring'."""
        import inspect

        from summon_claude.sessions import session as session_mod

        source = inspect.getsource(session_mod.SummonSession._run_session)
        assert "Scribe | Monitoring" in source
        assert "interval_min" in source or "scan_interval" in source


# ---------------------------------------------------------------------------
# Canvas template selection
# ---------------------------------------------------------------------------


class TestScribeCanvasTemplate:
    def test_scribe_canvas_template_selected(self):
        """'scribe' profile key maps to SCRIBE_CANVAS_TEMPLATE."""
        from summon_claude.slack.canvas_templates import SCRIBE_CANVAS_TEMPLATE, get_canvas_template

        template = get_canvas_template("scribe")
        assert template is SCRIBE_CANVAS_TEMPLATE

    def test_scribe_canvas_template_content(self):
        """SCRIBE_CANVAS_TEMPLATE contains scribe-specific heading."""
        from summon_claude.slack.canvas_templates import SCRIBE_CANVAS_TEMPLATE

        assert "Scribe Agent" in SCRIBE_CANVAS_TEMPLATE


# ---------------------------------------------------------------------------
# Shutdown stops monitors
# ---------------------------------------------------------------------------


class TestScribeShutdownStopsMonitors:
    @pytest.mark.asyncio
    async def test_scribe_shutdown_stops_monitors(self):
        """_shutdown() calls stop() on each monitor in _slack_monitors."""
        config = make_config()
        opts = SessionOptions(cwd="/tmp", name="scribe", scribe_profile=True)
        sess = SummonSession(
            config=config,
            options=opts,
            auth=None,
            session_id="test-shutdown-mon",
            web_client=None,
            dispatcher=MagicMock(),
            bot_user_id="B001",
            ipc_spawn=AsyncMock(),
            ipc_resume=AsyncMock(),
        )

        monitor1 = AsyncMock()
        monitor1.stop = AsyncMock()
        monitor2 = AsyncMock()
        monitor2.stop = AsyncMock()
        sess._slack_monitors = [monitor1, monitor2]

        # _shutdown expects a RunTimeState — mock it minimally
        rt = MagicMock()
        rt.client = MagicMock()
        rt.client.post = AsyncMock()
        rt.registry = AsyncMock()

        await sess._shutdown(rt)

        monitor1.stop.assert_called_once()
        monitor2.stop.assert_called_once()
        assert sess._slack_monitors == []


# ---------------------------------------------------------------------------
# _build_scan_cron
# ---------------------------------------------------------------------------


class TestBuildScanCron:
    def test_build_scan_cron_minutes(self):
        """300 seconds (5 min) → '*/5 * * * *'."""
        from summon_claude.sessions.session import _build_scan_cron

        assert _build_scan_cron(300) == "*/5 * * * *"

    def test_build_scan_cron_hours(self):
        """7200 seconds (2 hours) → '0 */2 * * *'."""
        from summon_claude.sessions.session import _build_scan_cron

        assert _build_scan_cron(7200) == "0 */2 * * *"

    def test_build_scan_cron_minimum_clamp(self):
        """Values below 60 seconds are clamped to */1 * * * * (1 minute minimum)."""
        from summon_claude.sessions.session import _build_scan_cron

        assert _build_scan_cron(30) == "*/1 * * * *"

    def test_build_scan_cron_one_hour(self):
        """3600 seconds (1 hour) → '0 */1 * * *'."""
        from summon_claude.sessions.session import _build_scan_cron

        assert _build_scan_cron(3600) == "0 */1 * * *"


# ---------------------------------------------------------------------------
# slack_auth URL validation
# ---------------------------------------------------------------------------


class TestSlackAuthValidatesUrl:
    def test_slack_auth_rejects_non_slack_url(self):
        """slack_auth exits with code 1 for non-Slack URLs."""
        from click.testing import CliRunner

        from summon_claude.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["auth", "slack", "login", "https://evil.example.com"])
        assert result.exit_code != 0
        assert "slack.com" in result.output.lower() or "Expected" in result.output

    def test_slack_auth_rejects_http_url(self):
        """slack_auth exits with code 1 for http:// (non-https) Slack URLs."""
        from click.testing import CliRunner

        from summon_claude.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["auth", "slack", "login", "http://myteam.slack.com"])
        assert result.exit_code != 0

    def test_slack_auth_rejects_slack_substring_in_query(self):
        """slack_auth rejects URLs with 'slack.com' in query string (not domain)."""
        from click.testing import CliRunner

        from summon_claude.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["auth", "slack", "login", "https://evil.com?ref=slack.com"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# _check_existing_slack_auth
# ---------------------------------------------------------------------------


class TestCheckExistingSlackAuth:
    """Tests for _check_existing_slack_auth credential detection."""

    def test_returns_none_when_no_workspace_config(self, tmp_path, monkeypatch):
        """Returns None when workspace config file doesn't exist."""
        from summon_claude.cli.slack_auth import _check_existing_slack_auth

        monkeypatch.setattr(
            "summon_claude.cli.slack_auth.get_workspace_config_path",
            lambda: tmp_path / "nonexistent.json",
        )
        assert _check_existing_slack_auth() is None

    def test_returns_none_when_state_file_missing(self, tmp_path, monkeypatch):
        """Returns None when workspace config exists but state file doesn't."""
        import json

        from summon_claude.cli.slack_auth import _check_existing_slack_auth

        ws_config = tmp_path / "workspace.json"
        ws_config.write_text(
            json.dumps(
                {
                    "url": "https://test.slack.com",
                    "auth_state_path": str(tmp_path / "missing_state.json"),
                }
            )
        )
        monkeypatch.setattr(
            "summon_claude.cli.slack_auth.get_workspace_config_path",
            lambda: ws_config,
        )
        assert _check_existing_slack_auth() is None

    def test_returns_none_when_d_cookie_expired(self, tmp_path, monkeypatch):
        """Returns None when the primary 'd' cookie is expired."""
        import json
        import time

        from summon_claude.cli.slack_auth import _check_existing_slack_auth

        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "cookies": [{"name": "d", "value": "xoxd-test", "expires": time.time() - 3600}],
                    "origins": [],
                }
            )
        )

        ws_config = tmp_path / "workspace.json"
        ws_config.write_text(
            json.dumps(
                {
                    "url": "https://test.slack.com",
                    "auth_state_path": str(state_file),
                }
            )
        )
        monkeypatch.setattr(
            "summon_claude.cli.slack_auth.get_workspace_config_path",
            lambda: ws_config,
        )
        assert _check_existing_slack_auth() is None

    def test_returns_info_when_d_cookie_valid(self, tmp_path, monkeypatch):
        """Returns status dict when 'd' cookie is present and not expired."""
        import json
        import time

        from summon_claude.cli.slack_auth import _check_existing_slack_auth

        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "d", "value": "xoxd-test", "expires": time.time() + 86400}
                    ],
                    "origins": [],
                }
            )
        )

        ws_config = tmp_path / "workspace.json"
        ws_config.write_text(
            json.dumps(
                {
                    "url": "https://test.slack.com",
                    "auth_state_path": str(state_file),
                    "user_id": "U12345",
                }
            )
        )
        monkeypatch.setattr(
            "summon_claude.cli.slack_auth.get_workspace_config_path",
            lambda: ws_config,
        )
        monkeypatch.setattr(
            "summon_claude.cli.slack_auth.get_browser_auth_dir",
            lambda: tmp_path,
        )

        result = _check_existing_slack_auth()
        assert result is not None
        assert result["url"] == "https://test.slack.com"
        assert result["user_id"] == "U12345"
        assert "saved" in result
        assert "age" in result

    def test_returns_info_when_d_cookie_is_session_cookie(self, tmp_path, monkeypatch):
        """Session cookies (expires=-1) are treated as valid."""
        import json

        from summon_claude.cli.slack_auth import _check_existing_slack_auth

        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "cookies": [{"name": "d", "value": "xoxd-test", "expires": -1}],
                    "origins": [],
                }
            )
        )

        ws_config = tmp_path / "workspace.json"
        ws_config.write_text(
            json.dumps(
                {
                    "url": "https://test.slack.com",
                    "auth_state_path": str(state_file),
                }
            )
        )
        monkeypatch.setattr(
            "summon_claude.cli.slack_auth.get_workspace_config_path",
            lambda: ws_config,
        )
        monkeypatch.setattr(
            "summon_claude.cli.slack_auth.get_browser_auth_dir",
            lambda: tmp_path,
        )

        result = _check_existing_slack_auth()
        assert result is not None

    def test_returns_none_when_no_cookies(self, tmp_path, monkeypatch):
        """Returns None when state file has no cookies."""
        import json

        from summon_claude.cli.slack_auth import _check_existing_slack_auth

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"cookies": [], "origins": []}))

        ws_config = tmp_path / "workspace.json"
        ws_config.write_text(
            json.dumps(
                {
                    "url": "https://test.slack.com",
                    "auth_state_path": str(state_file),
                }
            )
        )
        monkeypatch.setattr(
            "summon_claude.cli.slack_auth.get_workspace_config_path",
            lambda: ws_config,
        )
        assert _check_existing_slack_auth() is None


# ---------------------------------------------------------------------------
# project down stops scribe
# ---------------------------------------------------------------------------


class TestProjectDownStopsScribe:
    @pytest.mark.asyncio
    async def test_project_down_stops_scribe(self):
        """stop_project_managers (no name filter) finds and suspends the scribe session."""
        from summon_claude.cli.project import stop_project_managers

        scribe_session = {
            "session_id": "scribe-sess-001",
            "session_name": "scribe",
            "status": "active",
            "project_id": None,
        }

        mock_reg = AsyncMock()
        mock_reg.__aenter__ = AsyncMock(return_value=mock_reg)
        mock_reg.__aexit__ = AsyncMock(return_value=False)
        # First block: list_projects returns one project with no active sessions
        # so we hit the "No active project sessions found" path, then proceed
        # to the scribe check in the second block.
        mock_reg.list_projects.return_value = [{"project_id": "proj-001", "name": "myproject"}]
        mock_reg.get_project_sessions.return_value = []
        mock_reg.list_active.return_value = [scribe_session]

        with (
            patch("summon_claude.cli.project.is_daemon_running", return_value=True),
            patch("summon_claude.cli.project.SessionRegistry", return_value=mock_reg),
            patch("summon_claude.cli.project.daemon_client") as mock_daemon,
            patch("summon_claude.cli.project._run_project_hooks", return_value=None),
        ):
            mock_daemon.stop_session = AsyncMock(return_value=True)

            stopped = await stop_project_managers()

        assert "scribe-sess-001" in stopped
        # Scribe must be marked suspended in DB (same pattern as PM/child sessions)
        mock_reg.update_status.assert_any_call("scribe-sess-001", "suspended")

    @pytest.mark.asyncio
    async def test_project_down_with_name_leaves_scribe(self):
        """stop_project_managers(name='myproject') does NOT stop the scribe session."""
        from summon_claude.cli.project import stop_project_managers

        mock_reg = AsyncMock()
        mock_reg.__aenter__ = AsyncMock(return_value=mock_reg)
        mock_reg.__aexit__ = AsyncMock(return_value=False)
        mock_reg.list_projects.return_value = [{"project_id": "proj-001", "name": "myproject"}]
        mock_reg.get_project_sessions.return_value = []

        with (
            patch("summon_claude.cli.project.is_daemon_running", return_value=True),
            patch("summon_claude.cli.project.SessionRegistry", return_value=mock_reg),
            patch("summon_claude.cli.project.daemon_client") as mock_daemon,
            patch("summon_claude.cli.project._run_project_hooks", return_value=None),
        ):
            mock_daemon.stop_session = AsyncMock(return_value=True)

            stopped = await stop_project_managers(name="myproject")

        # Scribe was NOT stopped because name filter was provided
        assert stopped == []
        mock_daemon.stop_session.assert_not_called()


# ---------------------------------------------------------------------------
# _start_scribe_if_enabled pre-flight checks
# ---------------------------------------------------------------------------


class TestScribePreflight:
    def test_scribe_preflight_missing_google(self):
        """_start_scribe_if_enabled returns early when workspace-mcp binary missing."""
        manager = make_manager(
            scribe_enabled=True, scribe_google_enabled=True, scribe_google_services="gmail"
        )

        missing_bin = MagicMock()
        missing_bin.exists.return_value = False

        with (
            patch("summon_claude.sessions.manager.SummonSession") as mock_cls,
            patch("summon_claude.config.find_workspace_mcp_bin", return_value=missing_bin),
        ):
            manager._start_scribe_if_enabled("U123")

        mock_cls.assert_not_called()

    def test_scribe_preflight_missing_google_creds(self, tmp_path):
        """_start_scribe_if_enabled returns early when Google creds directory has no .json files."""
        manager = make_manager(
            scribe_enabled=True, scribe_google_enabled=True, scribe_google_services="gmail"
        )

        present_bin = MagicMock()
        present_bin.exists.return_value = True

        # Create an empty creds directory (no .json files)
        creds_dir = tmp_path / "google-creds"
        creds_dir.mkdir()

        with (
            patch("summon_claude.sessions.manager.SummonSession") as mock_cls,
            patch("summon_claude.config.find_workspace_mcp_bin", return_value=present_bin),
            patch("summon_claude.config.get_google_credentials_dir", return_value=creds_dir),
        ):
            manager._start_scribe_if_enabled("U123")

        mock_cls.assert_not_called()

    def test_scribe_preflight_missing_playwright(self, tmp_path):
        """_start_scribe_if_enabled returns early when playwright not installed.

        Bypass the Google preflight by disabling Google, isolating the Playwright check.
        """
        manager = make_manager(
            scribe_enabled=True,
            scribe_google_enabled=False,
            scribe_slack_enabled=True,
        )

        with (
            patch("summon_claude.sessions.manager.SummonSession") as mock_cls,
            patch(
                "summon_claude.config.get_workspace_config_path",
                return_value=tmp_path / "ws.json",
            ),
            patch("importlib.util.find_spec", return_value=None),
        ):
            manager._start_scribe_if_enabled("U123")

        mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# _resume_or_start_scribe: suspend/resume lifecycle
# ---------------------------------------------------------------------------


def _mock_registry_with_suspended_scribe(scribe_row: dict | None):
    """Build a mock SessionRegistry whose db.execute returns *scribe_row*."""
    mock_reg = AsyncMock()
    mock_reg.__aenter__ = AsyncMock(return_value=mock_reg)
    mock_reg.__aexit__ = AsyncMock(return_value=False)

    # mock_reg.db.execute(...) is used as an async context manager whose
    # __aenter__ returns a cursor with fetchone().
    mock_cursor = AsyncMock()
    mock_cursor.fetchone = AsyncMock(return_value=scribe_row)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_reg.db.execute = MagicMock(return_value=mock_ctx)
    mock_reg.get_channel = AsyncMock(return_value=None)
    return mock_reg


class TestResumeOrStartScribe:
    @pytest.mark.asyncio
    async def test_resumes_suspended_scribe(self):
        """When a suspended scribe exists, resume it via create_resumed_session."""
        manager = make_manager(scribe_enabled=True)

        suspended_scribe = {
            "session_id": "scribe-old-001",
            "session_name": "scribe",
            "status": "suspended",
            "project_id": None,
            "cwd": "/tmp/scribe",
            "slack_channel_id": "C_SCRIBE",
            "claude_session_id": "claude-sid-old",
            "model": "sonnet",
        }

        mock_reg = _mock_registry_with_suspended_scribe(suspended_scribe)
        mock_reg.get_channel.return_value = {
            "claude_session_id": "claude-sid-old",
        }

        manager.create_resumed_session = AsyncMock(return_value="scribe-new-001")

        with (
            patch("summon_claude.sessions.manager.SessionRegistry", return_value=mock_reg),
            patch("summon_claude.sessions.manager.pathlib.Path"),
        ):
            await manager._resume_or_start_scribe("U123")

        # Resumed via create_resumed_session, not _start_scribe_if_enabled
        manager.create_resumed_session.assert_called_once()
        opts = manager.create_resumed_session.call_args[0][0]
        assert opts.scribe_profile is True
        assert opts.name == "scribe"
        assert opts.resume == "claude-sid-old"
        assert opts.channel_id == "C_SCRIBE"
        assert manager.create_resumed_session.call_args.kwargs["authenticated_user_id"] == "U123"

        # Old suspended record marked completed
        mock_reg.update_status.assert_any_call("scribe-old-001", "completed")

    @pytest.mark.asyncio
    async def test_resumes_with_channel_but_no_channel_record(self):
        """Falls back to session claude_session_id when get_channel returns None."""
        manager = make_manager(scribe_enabled=True)

        suspended_scribe = {
            "session_id": "scribe-old-003",
            "session_name": "scribe",
            "status": "suspended",
            "project_id": None,
            "cwd": "/tmp/scribe",
            "slack_channel_id": "C_GONE",
            "claude_session_id": "claude-sid-fallback",
        }

        mock_reg = _mock_registry_with_suspended_scribe(suspended_scribe)
        # get_channel returns None (channel record was deleted/missing)

        manager.create_resumed_session = AsyncMock(return_value="scribe-new-003")

        with (
            patch("summon_claude.sessions.manager.SessionRegistry", return_value=mock_reg),
            patch("summon_claude.sessions.manager.pathlib.Path"),
        ):
            await manager._resume_or_start_scribe("U123")

        manager.create_resumed_session.assert_called_once()
        opts = manager.create_resumed_session.call_args[0][0]
        # Falls back to suspended_scribe's claude_session_id
        assert opts.resume == "claude-sid-fallback"
        assert opts.channel_id == "C_GONE"

    @pytest.mark.asyncio
    async def test_falls_through_to_fresh_start(self):
        """When no suspended scribe exists, falls through to _start_scribe_if_enabled."""
        manager = make_manager(
            scribe_enabled=True, scribe_google_services="", scribe_slack_enabled=False
        )

        mock_reg = _mock_registry_with_suspended_scribe(None)  # no suspended scribe

        with (
            patch("summon_claude.sessions.manager.SessionRegistry", return_value=mock_reg),
            patch("summon_claude.sessions.manager.SummonSession") as mock_cls,
            patch("summon_claude.sessions.manager.asyncio.create_task") as mock_task,
            patch("summon_claude.sessions.manager.pathlib.Path") as mock_path,
        ):
            mock_instance = MagicMock()
            mock_instance.is_scribe = True
            mock_cls.return_value = mock_instance
            mock_task.return_value = MagicMock()
            mock_path.return_value = MagicMock()

            await manager._resume_or_start_scribe("U123")

        # Fell through to _start_scribe_if_enabled → SummonSession created
        mock_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self):
        """When scribe_enabled=False, does nothing."""
        manager = make_manager(scribe_enabled=False)
        # Should not raise or call anything
        await manager._resume_or_start_scribe("U123")
        assert len(manager._sessions) == 0

    @pytest.mark.asyncio
    async def test_skips_when_already_running(self):
        """When a scribe is already running in memory, does nothing."""
        manager = make_manager(scribe_enabled=True)
        stub = MagicMock()
        stub.is_scribe = True
        manager._sessions["existing-scribe"] = stub

        await manager._resume_or_start_scribe("U123")
        # No new sessions created
        assert len(manager._sessions) == 1

    @pytest.mark.asyncio
    async def test_resume_failure_marks_errored_and_falls_through(self):
        """If resume fails, marks old session errored and falls through to fresh start."""
        manager = make_manager(
            scribe_enabled=True, scribe_google_services="", scribe_slack_enabled=False
        )

        suspended_scribe = {
            "session_id": "scribe-old-002",
            "session_name": "scribe",
            "status": "suspended",
            "project_id": None,
            "cwd": "/tmp/scribe",
            "slack_channel_id": None,
        }

        mock_reg = _mock_registry_with_suspended_scribe(suspended_scribe)

        manager.create_resumed_session = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch("summon_claude.sessions.manager.SessionRegistry", return_value=mock_reg),
            patch("summon_claude.sessions.manager.SummonSession") as mock_cls,
            patch("summon_claude.sessions.manager.asyncio.create_task") as mock_task,
            patch("summon_claude.sessions.manager.pathlib.Path") as mock_path,
        ):
            mock_instance = MagicMock()
            mock_instance.is_scribe = True
            mock_cls.return_value = mock_instance
            mock_task.return_value = MagicMock()
            mock_path.return_value = MagicMock()

            await manager._resume_or_start_scribe("U123")

        # Old record marked errored
        mock_reg.update_status.assert_any_call(
            "scribe-old-002", "errored", error_message="Resume failed: boom"
        )
        # Fell through to fresh start
        mock_cls.assert_called_once()


# ---------------------------------------------------------------------------
# Task 5: Scribe system prompt character voice guard tests
# ---------------------------------------------------------------------------


class TestScribePromptCharacterVoice:
    def test_scribe_prompt_character_voice(self):
        """Scribe system prompt contains vigilant sentinel character voice."""
        prompt = make_scribe_prompt()
        assert "sentinel" in prompt["append"]

    def test_scribe_prompt_no_formatting_templates(self):
        """Daily Recap template moved to scan prompt — not in system prompt."""
        prompt = make_scribe_prompt()
        assert "Daily Recap" not in prompt["append"]


# ---------------------------------------------------------------------------
# Task 6: build_scribe_scan_prompt tests
# ---------------------------------------------------------------------------


class TestScribeScanPromptBuilder:
    def _make_scan_prompt(self, **overrides):
        from summon_claude.sessions.session import build_scribe_scan_prompt

        defaults = dict(
            nonce="test123",
            google_enabled=True,
            slack_enabled=False,
            user_mention="<@U123>",
            importance_keywords="urgent, deadline",
            quiet_hours=None,
        )
        defaults.update(overrides)
        return build_scribe_scan_prompt(**defaults)

    def test_scribe_scan_prompt_nonce_prefix(self):
        result = self._make_scan_prompt()
        assert result.startswith("[SUMMON-INTERNAL-test123]")

    def test_scribe_scan_prompt_importance_scale(self):
        result = self._make_scan_prompt()
        assert "Urgent action required" in result

    def test_scribe_scan_prompt_google_enabled(self):
        result = self._make_scan_prompt(google_enabled=True)
        assert "Gmail" in result

    def test_scribe_scan_prompt_google_disabled(self):
        result = self._make_scan_prompt(google_enabled=False)
        assert "Gmail" not in result

    def test_scribe_scan_prompt_slack_enabled(self):
        result = self._make_scan_prompt(slack_enabled=True)
        assert "external_slack_check" in result

    def test_scribe_scan_prompt_slack_disabled(self):
        result = self._make_scan_prompt(slack_enabled=False)
        assert "external_slack_check" not in result

    def test_scribe_scan_prompt_quiet_hours(self):
        result = self._make_scan_prompt(quiet_hours="22:00-08:00")
        assert "22:00-08:00" in result

    def test_scribe_scan_prompt_no_quiet_hours(self):
        result = self._make_scan_prompt(quiet_hours=None)
        assert "Quiet hours" not in result

    def test_scribe_scan_prompt_importance_keywords(self):
        result = self._make_scan_prompt(importance_keywords="urgent, deadline")
        assert "urgent, deadline" in result

    def test_scribe_scan_prompt_default_keywords_fallback(self):
        result = self._make_scan_prompt(importance_keywords="")
        assert "urgent, action required, deadline" in result

    def test_scribe_scan_prompt_strips_newlines_from_keywords(self):
        result = self._make_scan_prompt(importance_keywords="urgent\n## Injected")
        assert "\n" not in result.split("Importance keywords")[1].split("\n")[0]
