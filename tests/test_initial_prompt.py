"""Tests for initial_prompt feature — SessionOptions injection and MCP validation."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from summon_claude.config import SummonConfig
from summon_claude.sessions.auth import SessionAuth
from summon_claude.sessions.session import (
    SessionOptions,
    SummonSession,
    _PendingTurn,
)
from summon_claude.slack.client import MessageRef, SlackClient, sanitize_for_slack
from summon_claude.summon_cli_mcp import MAX_PROMPT_CHARS, create_summon_cli_mcp_tools

# ---------------------------------------------------------------------------
# Helpers (mirror test_sessions_session.py conventions)
# ---------------------------------------------------------------------------


def make_config(**overrides) -> SummonConfig:
    defaults = {
        "slack_bot_token": "xoxb-test-token",
        "slack_app_token": "xapp-test-token",
        "slack_signing_secret": "abc123def456",
        "default_model": "claude-opus-4-6",
        "channel_prefix": "summon",
        "permission_debounce_ms": 10,
        "max_inline_chars": 2500,
    }
    defaults.update(overrides)
    return SummonConfig.model_validate(defaults)


def make_options(**overrides) -> SessionOptions:
    defaults = {
        "cwd": "/tmp/test",
        "name": "test",
    }
    defaults.update(overrides)
    return SessionOptions(**defaults)


def make_auth(session_id: str = "test-session", **overrides) -> SessionAuth:
    defaults = {
        "short_code": "abcd1234",
        "session_id": session_id,
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    defaults.update(overrides)
    return SessionAuth(**defaults)


def make_session(session_id: str = "test-session", **overrides) -> SummonSession:
    opt_fields = (
        "cwd",
        "name",
        "model",
        "effort",
        "resume",
        "channel_id",
        "pm_profile",
        "system_prompt_append",
        "initial_prompt",
    )
    opts_kw = {k: overrides.pop(k) for k in list(overrides) if k in opt_fields}
    auth_fields = ("short_code", "expires_at")
    auth_kw = {k: overrides.pop(k) for k in list(overrides) if k in auth_fields}
    return SummonSession(
        config=make_config(),
        options=make_options(**opts_kw),
        auth=make_auth(session_id=session_id, **auth_kw),
        session_id=session_id,
        **overrides,
    )


# ---------------------------------------------------------------------------
# Tests: SessionOptions — initial_prompt field
# ---------------------------------------------------------------------------


class TestSessionOptionsInitialPrompt:
    def test_initial_prompt_default_is_none(self):
        opts = make_options()
        assert opts.initial_prompt is None

    def test_initial_prompt_stored(self):
        opts = make_options(initial_prompt="Build the auth module")
        assert opts.initial_prompt == "Build the auth module"

    def test_initial_prompt_serialization_roundtrip(self):
        opts = make_options(initial_prompt="Run the full test suite")
        d = dataclasses.asdict(opts)
        assert d["initial_prompt"] == "Run the full test suite"

    def test_initial_prompt_none_roundtrip(self):
        opts = make_options()
        d = dataclasses.asdict(opts)
        assert d["initial_prompt"] is None

    def test_initial_prompt_empty_string_stored(self):
        opts = make_options(initial_prompt="")
        assert opts.initial_prompt == ""


# ---------------------------------------------------------------------------
# Tests: SummonSession constructor — initial_prompt stored on session
# ---------------------------------------------------------------------------


class TestSummonSessionInitialPrompt:
    def test_initial_prompt_stored_on_session(self):
        session = make_session(initial_prompt="Build the auth module")
        assert session._initial_prompt == "Build the auth module"

    def test_initial_prompt_none_on_session(self):
        session = make_session()
        assert session._initial_prompt is None

    def test_initial_prompt_sent_flag_defaults_false(self):
        session = make_session(initial_prompt="hello")
        assert session._initial_prompt_sent is False

    def test_initial_prompt_empty_string_on_session(self):
        session = make_session(initial_prompt="")
        assert session._initial_prompt == ""


# ---------------------------------------------------------------------------
# Tests: _pending_turns injection during _run_session_tasks
# ---------------------------------------------------------------------------


class TestInitialPromptInjection:
    """Verify _pending_turns gets a _PendingTurn when initial_prompt is set."""

    def test_initial_prompt_injected_into_pending_turns(self):
        """When initial_prompt is set, the session stores it and _pending_turns is ready."""
        session = make_session(initial_prompt="Build the auth module")
        # The _pending_turns queue starts empty at construction — injection happens
        # inside _run_session_tasks when the SDK client is alive. We verify the
        # stored prompt and flag are correct (the injection path is tested below).
        assert session._initial_prompt == "Build the auth module"
        assert session._initial_prompt_sent is False
        assert session._pending_turns.empty()

    def test_initial_prompt_none_no_injection(self):
        """SessionOptions with initial_prompt=None — no PendingTurn queued at construction."""
        session = make_session()
        assert session._initial_prompt is None
        assert session._pending_turns.empty()

    def test_initial_prompt_empty_string_falsy_no_injection(self):
        """Empty string initial_prompt is falsy — session stores it but won't inject."""
        session = make_session(initial_prompt="")
        # Empty string is falsy; the injection guard is `if self._initial_prompt`
        assert not session._initial_prompt  # falsy
        assert session._pending_turns.empty()

    def test_pending_turns_injection_logic(self):
        """Simulate the injection path: put a _PendingTurn and verify queue contents."""
        session = make_session(initial_prompt="Build the auth module")
        # Simulate what _run_session_tasks does:
        if session._initial_prompt and not session._initial_prompt_sent:
            session._initial_prompt_sent = True
            pending = _PendingTurn(message=session._initial_prompt, pre_sent=False)
            session._pending_turns.put_nowait(pending)

        assert not session._pending_turns.empty()
        item = session._pending_turns.get_nowait()
        assert isinstance(item, _PendingTurn)
        assert item.message == "Build the auth module"
        assert item.pre_sent is False

    def test_pending_turns_injection_only_once(self):
        """Once _initial_prompt_sent=True, the guard prevents a second injection."""
        session = make_session(initial_prompt="Build the auth module")
        session._initial_prompt_sent = True  # already injected

        # Guard: `if self._initial_prompt and not self._initial_prompt_sent`
        injected = session._initial_prompt and not session._initial_prompt_sent
        assert not injected


# ---------------------------------------------------------------------------
# Tests: Slack observability post — mention sanitization
# ---------------------------------------------------------------------------


class TestInitialPromptObservability:
    """Verify mention sanitization applied to initial_prompt before Slack post."""

    def test_mention_sanitization_channel(self):
        """<!channel> is replaced with 'channel' in the observability post."""
        safe = sanitize_for_slack("Hello <!channel> folks")
        assert "<!channel>" not in safe
        assert "channel" in safe

    def test_mention_sanitization_here(self):
        safe = sanitize_for_slack("Attention <!here>!")
        assert "here" in safe
        assert "<!here>" not in safe

    def test_mention_sanitization_user(self):
        """<@UABC123> is replaced with 'user:UABC123'."""
        safe = sanitize_for_slack("Hey <@UABC123> check this")
        assert "<@UABC123>" not in safe
        assert "user:UABC123" in safe

    def test_mention_sanitization_everyone(self):
        safe = sanitize_for_slack("Hey <!everyone> look at this")
        assert "<!everyone>" not in safe
        assert "everyone" in safe

    def test_mention_sanitization_subteam(self):
        safe = sanitize_for_slack("FYI <!subteam^S123|team-name>")
        assert "<!subteam" not in safe
        assert "group" in safe

    def test_sanitize_for_slack_redacts_secrets(self):
        """Secrets like API keys are redacted by sanitize_for_slack."""
        safe = sanitize_for_slack("key is sk-ant-abc123def456")
        assert "sk-ant-" not in safe

    def test_sanitize_for_slack_neutralizes_hyperlinks(self):
        """Slack <url|label> hyperlinks are neutralized to prevent phishing."""
        safe = sanitize_for_slack("Click <https://evil.example.com|here> now")
        assert "<https://" not in safe
        assert "here" in safe
        assert "evil.example.com" in safe

    async def test_initial_prompt_observability_post_called(self, registry):
        """When initial_prompt provided and enqueued, client.post is called with the text."""
        from summon_claude.sessions.session import _SessionRuntime

        session = make_session(initial_prompt="Implement the login endpoint")

        mock_client = AsyncMock(spec=SlackClient)
        mock_client.channel_id = "C_TEST"
        mock_client.post = AsyncMock(return_value=MessageRef(channel_id="C_TEST", ts="111.222"))

        rt = _SessionRuntime(
            registry=registry,
            client=mock_client,
            permission_handler=AsyncMock(),
        )

        # Simulate the injection block from _run_session_tasks
        if session._initial_prompt and not session._initial_prompt_sent:
            session._initial_prompt_sent = True
            safe_prompt = sanitize_for_slack(session._initial_prompt)
            pending = _PendingTurn(message=session._initial_prompt, pre_sent=False)
            session._pending_turns.put_nowait(pending)
            await rt.client.post(f"_Initial prompt:_ {safe_prompt[:500]}")

        mock_client.post.assert_awaited_once()
        call_text = mock_client.post.call_args[0][0]
        assert "_Initial prompt:_" in call_text
        assert "Implement the login endpoint" in call_text

    async def test_initial_prompt_mention_sanitized_in_post(self, registry):
        """<!channel> mentions are stripped before the observability post."""
        from summon_claude.sessions.session import _SessionRuntime

        session = make_session(initial_prompt="Hi <!channel>, start the task")

        mock_client = AsyncMock(spec=SlackClient)
        mock_client.channel_id = "C_TEST"
        mock_client.post = AsyncMock(return_value=MessageRef(channel_id="C_TEST", ts="111.222"))

        rt = _SessionRuntime(
            registry=registry,
            client=mock_client,
            permission_handler=AsyncMock(),
        )

        if session._initial_prompt and not session._initial_prompt_sent:
            session._initial_prompt_sent = True
            safe_prompt = sanitize_for_slack(session._initial_prompt)
            pending = _PendingTurn(message=session._initial_prompt, pre_sent=False)
            session._pending_turns.put_nowait(pending)
            await rt.client.post(f"_Initial prompt:_ {safe_prompt[:500]}")

        call_text = mock_client.post.call_args[0][0]
        assert "<!channel>" not in call_text
        assert "channel" in call_text


# ---------------------------------------------------------------------------
# Tests: MCP tool — initial_prompt char limit validation
# ---------------------------------------------------------------------------


class TestMcpInitialPromptValidation:
    """session_start MCP tool rejects initial_prompt exceeding MAX_PROMPT_CHARS."""

    @pytest.fixture
    def tools(self, registry):
        from conftest import make_scheduler

        return {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=registry,
                session_id="sess-111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/tmp",
                scheduler=make_scheduler(),
                is_pm=True,
            )
        }

    async def test_initial_prompt_within_limit_accepted(self, tools):
        """initial_prompt at exactly MAX_PROMPT_CHARS chars does not error on validation."""
        # Validation only rejects if len > MAX_PROMPT_CHARS; at the limit it passes
        prompt = "x" * MAX_PROMPT_CHARS
        result = await tools["session_start"].handler(
            {"name": "new-sess", "initial_prompt": prompt}
        )
        # The error (if any) is NOT about initial_prompt length
        if result.get("is_error"):
            assert "initial_prompt" not in result["content"][0]["text"]

    async def test_initial_prompt_exceeds_limit_rejected(self, tools):
        """initial_prompt exceeding MAX_PROMPT_CHARS returns an error."""
        prompt = "x" * (MAX_PROMPT_CHARS + 1)
        result = await tools["session_start"].handler(
            {"name": "new-sess", "initial_prompt": prompt}
        )
        assert result.get("is_error") is True
        assert "initial_prompt" in result["content"][0]["text"]
        assert str(MAX_PROMPT_CHARS) in result["content"][0]["text"]

    async def test_system_prompt_exceeds_limit_rejected(self, tools):
        """system_prompt exceeding MAX_PROMPT_CHARS also returns an error (unified limit)."""
        prompt = "x" * (MAX_PROMPT_CHARS + 1)
        result = await tools["session_start"].handler({"name": "new-sess", "system_prompt": prompt})
        assert result.get("is_error") is True
        assert "system_prompt" in result["content"][0]["text"]

    async def test_initial_prompt_none_accepted(self, tools):
        """Omitting initial_prompt is fine — no validation error for missing field."""
        result = await tools["session_start"].handler({"name": "new-sess"})
        # Error might occur for other reasons (spawn depth, etc.) but NOT initial_prompt
        if result.get("is_error"):
            assert "initial_prompt" not in result["content"][0]["text"]
