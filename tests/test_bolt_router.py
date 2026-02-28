"""Tests for summon_claude.bolt_router — BoltRouter lifecycle and handler registration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.config import SummonConfig


def make_config(**overrides) -> SummonConfig:
    defaults = {
        "slack_bot_token": "xoxb-test-token",
        "slack_app_token": "xapp-test-token",
        "slack_signing_secret": "test-secret",
    }
    defaults.update(overrides)
    return SummonConfig.model_validate(defaults)


def _mock_app() -> MagicMock:
    """Return a MagicMock that behaves like AsyncApp for handler registration."""
    app = MagicMock()
    app.command = MagicMock(return_value=lambda f: f)
    app.event = MagicMock(return_value=lambda f: f)
    app.action = MagicMock(return_value=lambda f: f)
    return app


def _make_router(config: SummonConfig | None = None):
    """Return a BoltRouter with all Slack SDK constructors patched out."""
    from summon_claude.bolt_router import BoltRouter

    cfg = config or make_config()
    with (
        patch("summon_claude.bolt_router.AsyncApp") as patched_app_cls,
        patch("summon_claude.bolt_router.AsyncWebClient"),
        patch("summon_claude.bolt_router.AsyncSocketModeHandler") as patched_handler_cls,
    ):
        mock_a = _mock_app()
        patched_app_cls.return_value = mock_a

        mock_h = AsyncMock()
        patched_handler_cls.return_value = mock_h

        router = BoltRouter(cfg)
        # Expose the mocks for inspection
        router._mock_app = mock_a  # type: ignore[attr-defined]
        router._mock_handler = mock_h  # type: ignore[attr-defined]

    # Make auth_test() awaitable so start() works in tests
    router._client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
    return router


class TestBoltRouterInit:
    """Tests for BoltRouter construction and initial state."""

    def test_provider_is_slack_chat_provider(self):
        """provider property returns a SlackChatProvider instance."""
        from summon_claude.providers.slack import SlackChatProvider

        router = _make_router()
        assert isinstance(router.provider, SlackChatProvider)

    def test_provider_is_stable(self):
        """provider returns the same object on repeated access."""
        router = _make_router()
        assert router.provider is router.provider

    def test_dispatcher_starts_as_none(self):
        """dispatcher is None until set_dispatcher() is called."""
        router = _make_router()
        assert router._dispatcher is None

    def test_session_manager_starts_as_none(self):
        """session_manager is None until set_session_manager() is called."""
        router = _make_router()
        assert router._session_manager is None

    def test_handlers_registered_at_construction(self):
        """Handler registration methods are called during __init__."""
        router = _make_router()
        app = router._mock_app  # type: ignore[attr-defined]
        app.command.assert_called_with("/summon")
        assert app.event.call_count >= 2  # message + reaction_added
        assert app.action.call_count >= 3  # permission_approve, permission_deny, ask_user pattern


class TestDeferredWiring:
    """Tests for set_dispatcher() and set_session_manager()."""

    def test_set_dispatcher(self):
        """set_dispatcher stores the dispatcher."""
        router = _make_router()
        mock_dispatcher = MagicMock()
        router.set_dispatcher(mock_dispatcher)
        assert router._dispatcher is mock_dispatcher

    def test_set_session_manager(self):
        """set_session_manager stores the session manager."""
        router = _make_router()
        mock_sm = MagicMock()
        router.set_session_manager(mock_sm)
        assert router._session_manager is mock_sm

    def test_set_dispatcher_replaces_previous(self):
        """set_dispatcher overwrites a previous value."""
        router = _make_router()
        first = MagicMock()
        second = MagicMock()
        router.set_dispatcher(first)
        router.set_dispatcher(second)
        assert router._dispatcher is second


class TestLifecycle:
    """Tests for start() and stop()."""

    async def test_start_calls_connect_async(self):
        """start() calls connect_async() on the socket handler."""
        router = _make_router()
        await router.start()
        router._socket_handler.connect_async.assert_awaited_once()

    async def test_stop_calls_close_async(self):
        """stop() calls close_async() on the socket handler."""
        router = _make_router()
        await router.stop()
        router._socket_handler.close_async.assert_awaited_once()

    async def test_stop_tolerates_close_error(self):
        """stop() does not propagate exceptions from close_async()."""
        router = _make_router()
        router._socket_handler.close_async.side_effect = RuntimeError("already closed")
        await router.stop()  # must not raise


class TestReconnect:
    """Tests for reconnect() — fresh AsyncApp creation."""

    async def test_reconnect_creates_new_app(self):
        """reconnect() closes the old handler and creates a new AsyncApp."""
        from summon_claude.bolt_router import BoltRouter

        cfg = make_config()
        mock_a1 = _mock_app()
        mock_a2 = _mock_app()
        mock_h1 = AsyncMock()
        mock_h2 = AsyncMock()

        with (
            patch("summon_claude.bolt_router.AsyncApp") as patched_app_cls,
            patch("summon_claude.bolt_router.AsyncWebClient"),
            patch("summon_claude.bolt_router.AsyncSocketModeHandler") as patched_handler_cls,
        ):
            patched_app_cls.side_effect = [mock_a1, mock_a2]
            patched_handler_cls.side_effect = [mock_h1, mock_h2]

            router = BoltRouter(cfg)
            assert router._app is mock_a1

            await router.reconnect()

        mock_h1.close_async.assert_awaited_once()
        assert router._app is mock_a2
        mock_h2.connect_async.assert_awaited_once()

    async def test_reconnect_re_registers_handlers(self):
        """reconnect() registers handlers on the new app."""
        from summon_claude.bolt_router import BoltRouter

        cfg = make_config()
        mock_a1 = _mock_app()
        mock_a2 = _mock_app()

        with (
            patch("summon_claude.bolt_router.AsyncApp") as patched_app_cls,
            patch("summon_claude.bolt_router.AsyncWebClient"),
            patch("summon_claude.bolt_router.AsyncSocketModeHandler") as patched_handler_cls,
        ):
            patched_app_cls.side_effect = [mock_a1, mock_a2]
            patched_handler_cls.return_value = AsyncMock()

            router = BoltRouter(cfg)
            await router.reconnect()

        assert mock_a2.event.call_count >= 2
        assert mock_a2.command.call_count >= 1

    async def test_reconnect_tolerates_old_close_error(self):
        """reconnect() continues even if closing the old handler raises."""
        from summon_claude.bolt_router import BoltRouter

        cfg = make_config()
        mock_h1 = AsyncMock()
        mock_h1.close_async.side_effect = RuntimeError("dead")
        mock_h2 = AsyncMock()

        with (
            patch("summon_claude.bolt_router.AsyncApp") as patched_app_cls,
            patch("summon_claude.bolt_router.AsyncWebClient"),
            patch("summon_claude.bolt_router.AsyncSocketModeHandler") as patched_handler_cls,
        ):
            patched_app_cls.return_value = _mock_app()
            patched_handler_cls.side_effect = [mock_h1, mock_h2]

            router = BoltRouter(cfg)
            await router.reconnect()  # must not raise

        mock_h2.connect_async.assert_awaited_once()


class TestSummonCommandHandler:
    """Tests for the /summon slash command Bolt handler."""

    async def _invoke_summon(self, router, command: dict) -> dict:
        """Extract the /summon handler from BoltRouter and call it directly."""
        registered_fn = None

        def capture_command(path):
            def decorator(fn):
                nonlocal registered_fn
                if path == "/summon":
                    registered_fn = fn
                return fn

            return decorator

        mock_a = MagicMock()
        mock_a.command = capture_command
        mock_a.event = MagicMock(return_value=lambda f: f)
        mock_a.action = MagicMock(return_value=lambda f: f)
        router._register_handlers(mock_a)

        assert registered_fn is not None, "No /summon handler was registered"

        ack = AsyncMock()
        respond = AsyncMock()
        await registered_fn(ack=ack, command=command, respond=respond)
        return {"ack": ack, "respond": respond}

    async def test_summon_acks_immediately(self):
        """The /summon handler calls ack() before any other processing."""
        router = _make_router()
        result = await self._invoke_summon(router, {"user_id": "U001", "text": "abc123"})
        result["ack"].assert_awaited_once()

    async def test_summon_empty_text_responds_with_usage(self):
        """/summon with no code text responds with usage instructions."""
        router = _make_router()
        result = await self._invoke_summon(router, {"user_id": "U001", "text": ""})
        respond_call = result["respond"].call_args
        text = respond_call.kwargs.get("text", "")
        assert "Usage" in text

    async def test_summon_no_session_manager_responds_not_ready(self):
        """/summon without session_manager wired responds with 'not ready' message."""
        router = _make_router()
        assert router._session_manager is None
        result = await self._invoke_summon(router, {"user_id": "U001", "text": "abc123"})
        text = result["respond"].call_args.kwargs.get("text", "")
        assert "not ready" in text.lower() or ":x:" in text

    async def test_summon_delegates_to_session_manager(self):
        """/summon with session_manager wired calls handle_summon_command."""
        router = _make_router()
        mock_sm = MagicMock()
        mock_sm.handle_summon_command = AsyncMock()
        router.set_session_manager(mock_sm)

        result = await self._invoke_summon(router, {"user_id": "U001", "text": "mycode"})

        mock_sm.handle_summon_command.assert_awaited_once_with(
            user_id="U001",
            code="mycode",
            respond=result["respond"],
        )

    async def test_summon_rate_limited_user_gets_cooldown_message(self):
        """/summon rate-limits repeated calls from the same user."""
        router = _make_router()
        # Exhaust the rate-limit token directly
        router._rate_limiter.check("U001")
        result = await self._invoke_summon(router, {"user_id": "U001", "text": "code"})
        text = result["respond"].call_args.kwargs.get("text", "")
        assert "wait" in text.lower()


class TestMessageHandlerRouting:
    """Tests that message/reaction/action events are routed to dispatcher."""

    def _extract_event_handler(self, router, event_type: str):
        """Re-register handlers on a capturing mock and return the named handler."""
        captured: dict = {}

        def capture_event(ev_type):
            def decorator(fn):
                captured[ev_type] = fn
                return fn

            return decorator

        mock_a = MagicMock()
        mock_a.command = MagicMock(return_value=lambda f: f)
        mock_a.event = capture_event
        mock_a.action = MagicMock(return_value=lambda f: f)
        router._register_handlers(mock_a)
        return captured.get(event_type)

    def _extract_action_handler(self, router, action_id: str):
        """Re-register handlers and return the handler for a given action_id."""
        captured: dict = {}

        def capture_action(action_id_or_pattern):
            key = str(action_id_or_pattern)

            def decorator(fn):
                captured[key] = fn
                return fn

            return decorator

        mock_a = MagicMock()
        mock_a.command = MagicMock(return_value=lambda f: f)
        mock_a.event = MagicMock(return_value=lambda f: f)
        mock_a.action = capture_action
        router._register_handlers(mock_a)
        return captured.get(action_id)

    async def test_message_event_routes_to_dispatcher(self):
        """message events call dispatcher.dispatch_message."""
        router = _make_router()
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch_message = AsyncMock()
        router.set_dispatcher(mock_dispatcher)

        handler = self._extract_event_handler(router, "message")
        assert handler is not None

        event = {"type": "message", "channel": "C001", "text": "hello"}
        await handler(event=event, say=AsyncMock())

        mock_dispatcher.dispatch_message.assert_awaited_once_with(event)

    async def test_reaction_added_routes_to_dispatcher(self):
        """reaction_added events call dispatcher.dispatch_reaction."""
        router = _make_router()
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch_reaction = AsyncMock()
        router.set_dispatcher(mock_dispatcher)

        handler = self._extract_event_handler(router, "reaction_added")
        assert handler is not None

        event = {"type": "reaction_added", "item": {"channel": "C001"}}
        await handler(event=event)

        mock_dispatcher.dispatch_reaction.assert_awaited_once_with(event)

    async def test_message_without_dispatcher_does_not_raise(self):
        """message events are silently dropped when dispatcher is not set."""
        router = _make_router()
        assert router._dispatcher is None

        handler = self._extract_event_handler(router, "message")
        await handler(event={"type": "message", "channel": "C001"}, say=AsyncMock())

    async def test_permission_approve_action_calls_dispatcher(self):
        """permission_approve action calls dispatcher.dispatch_action (after ack)."""
        router = _make_router()
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch_action = AsyncMock()
        router.set_dispatcher(mock_dispatcher)

        handler = self._extract_action_handler(router, "permission_approve")
        assert handler is not None

        ack = AsyncMock()
        action = {"action_id": "permission_approve", "value": "approve:b1"}
        body = {"channel": {"id": "C001"}, "user": {"id": "U001"}, "response_url": ""}
        await handler(ack=ack, action=action, body=body)

        ack.assert_awaited_once()
        mock_dispatcher.dispatch_action.assert_awaited_once_with(action, body)
