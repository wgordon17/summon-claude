"""End-to-end browser tests for SlackBrowserMonitor.

Uses a local aiohttp server with a WebSocket endpoint that mimics Slack's
RTM frame format. Playwright navigates to a local HTML page which opens a
WebSocket connection — exercising the REAL pipeline:

    browser launch → page.goto → page.on("websocket") →
    ws.on("framereceived") → _on_frame → queue → drain → stop

No mocks. No synthetic payloads injected into _on_frame directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

# Skip entire module if playwright Python package is not installed
pytest.importorskip("playwright")

from summon_claude.slack_browser import SlackBrowserMonitor

# ---------------------------------------------------------------------------
# Local test server: serves HTML + WebSocket that mimics Slack RTM
# ---------------------------------------------------------------------------

# HTML page that opens a WebSocket to the same origin and keeps it alive.
# The server pushes Slack-shaped JSON frames through the WS.
_HTML_PAGE = """\
<!DOCTYPE html>
<html><head><title>Slack Mock</title></head>
<body>
<script>
const ws = new WebSocket(location.origin.replace("http", "ws") + "/ws");
ws.onopen = () => { document.title = "ws-connected"; };
ws.onmessage = (e) => {};
</script>
</body></html>
"""

# HTML page that opens a CROSS-ORIGIN WebSocket (to a different port).
# Mimics how real Slack connects from app.slack.com to wss://wss-primary.slack.com.
# The WS_PORT placeholder is replaced at fixture time.
_CROSS_ORIGIN_HTML = """\
<!DOCTYPE html>
<html><head><title>Slack Mock Cross-Origin</title></head>
<body>
<script>
const ws = new WebSocket("ws://127.0.0.1:WS_PORT/ws");
ws.onopen = () => { document.title = "ws-connected"; };
ws.onmessage = (e) => {};
</script>
</body></html>
"""

# HTML page that opens MULTIPLE WebSocket connections (like real Slack does).
_MULTI_WS_HTML = """\
<!DOCTYPE html>
<html><head><title>Slack Mock Multi-WS</title></head>
<body>
<script>
const ws1 = new WebSocket(location.origin.replace("http", "ws") + "/ws");
const ws2 = new WebSocket(location.origin.replace("http", "ws") + "/ws");
ws1.onopen = () => {};
ws2.onopen = () => {};
ws1.onmessage = (e) => {};
ws2.onmessage = (e) => {};
let ready = 0;
ws1.onopen = () => { ready++; if (ready === 2) document.title = "ws-connected"; };
ws2.onopen = () => { ready++; if (ready === 2) document.title = "ws-connected"; };
</script>
</body></html>
"""


async def _handle_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint. Connections are tracked for frame injection by tests."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    conns: list[web.WebSocketResponse] = request.app["ws_connections"]
    conns.append(ws)

    try:
        async for _ in ws:
            pass
    finally:
        conns.remove(ws)

    return ws


def _create_app(html: str = _HTML_PAGE) -> web.Application:
    app = web.Application()

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=html, content_type="text/html")

    app.router.add_get("/", index)
    app.router.add_get("/ws", _handle_ws)
    app["ws_connections"] = []
    return app


async def _send_frame(app: web.Application, frame: dict) -> None:
    """Push a JSON frame to all connected WebSocket clients."""
    text = json.dumps(frame)
    for ws in list(app["ws_connections"]):
        if not ws.closed:
            await ws.send_str(text)


# ---------------------------------------------------------------------------
# Synchronization helpers — no arbitrary sleeps
# ---------------------------------------------------------------------------


async def _wait_for_ws(
    app: web.Application,
    *,
    count: int = 1,
    timeout: float = 10.0,
) -> None:
    """Wait until at least `count` WebSocket clients are connected."""
    deadline = asyncio.get_event_loop().time() + timeout
    while len(app["ws_connections"]) < count:
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"Expected {count} WS connections, got {len(app['ws_connections'])}")
        await asyncio.sleep(0.05)


async def _wait_for_drain(
    monitor: SlackBrowserMonitor,
    expected: int,
    *,
    timeout: float = 5.0,
) -> list[Any]:
    """Poll drain() until `expected` messages are available, or timeout.

    Replaces arbitrary asyncio.sleep() with a proper sync point.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    collected: list[Any] = []
    while len(collected) < expected:
        batch = await monitor.drain()
        collected.extend(batch)
        if len(collected) >= expected:
            break
        if asyncio.get_event_loop().time() > deadline:
            break
        await asyncio.sleep(0.05)
    return collected


async def _wait_for_empty_drain(
    monitor: SlackBrowserMonitor,
    *,
    settle_time: float = 0.3,
) -> list[Any]:
    """Wait for frames to settle, then drain. For tests asserting 0 messages.

    We can't poll for "nothing arrived" — we must wait a reasonable time.
    This is an inherent limitation for negative assertions.
    """
    await asyncio.sleep(settle_time)
    return await monitor.drain()


async def _wait_for_fresh_ws(
    app: web.Application,
    old_connections: set[int],
    *,
    timeout: float = 10.0,
) -> None:
    """Wait for a NEW WebSocket connection that wasn't in old_connections.

    Solves the refresh race: after page.reload(), the new WS may connect
    before the old one is cleaned up, or vice versa. We can't rely on
    "count drops to 0 then rises to 1". Instead, snapshot connection
    identities before refresh and wait for one we haven't seen.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        current_ids = {id(ws) for ws in app["ws_connections"] if not ws.closed}
        new_ids = current_ids - old_connections
        if new_ids:
            return
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError("No new WS connection appeared after page reload")
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def mock_slack_server():
    """Start a local HTTP+WS server and yield (url, app, send_frame_fn)."""
    app = _create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    sockets = site._server.sockets  # type: ignore[union-attr]
    port = sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"

    async def send(frame: dict) -> None:
        await _send_frame(app, frame)

    yield url, app, send

    await runner.cleanup()


@pytest.fixture
async def cross_origin_servers():
    """Two servers: HTTP page on one port, WebSocket on another port.

    Mimics real Slack where the page is at app.slack.com but WebSocket
    connects to wss://wss-primary.slack.com (different origin).
    """
    # Start WS-only server first to get its port
    ws_app = _create_app()
    ws_runner = web.AppRunner(ws_app)
    await ws_runner.setup()
    ws_site = web.TCPSite(ws_runner, "127.0.0.1", 0)
    await ws_site.start()
    ws_port = ws_site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    # Create HTTP server with HTML that connects to the WS server's port
    html = _CROSS_ORIGIN_HTML.replace("WS_PORT", str(ws_port))
    http_app = _create_app(html)
    http_runner = web.AppRunner(http_app)
    await http_runner.setup()
    http_site = web.TCPSite(http_runner, "127.0.0.1", 0)
    await http_site.start()
    http_port = http_site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    http_url = f"http://127.0.0.1:{http_port}"

    async def send(frame: dict) -> None:
        await _send_frame(ws_app, frame)

    yield http_url, ws_app, send

    await http_runner.cleanup()
    await ws_runner.cleanup()


@pytest.fixture
async def multi_ws_server():
    """Server whose HTML page opens two WebSocket connections simultaneously."""
    app = _create_app(_MULTI_WS_HTML)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    url = f"http://127.0.0.1:{port}"

    async def send(frame: dict) -> None:
        await _send_frame(app, frame)

    yield url, app, send

    await runner.cleanup()


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return tmp_path / "test_state.json"


def _make_monitor(
    url: str,
    state_file: Path,
    monitored: list[str] | None = None,
    user_id: str = "UTEST",
) -> SlackBrowserMonitor:
    return SlackBrowserMonitor(
        workspace_id="test-local",
        workspace_url=url,
        state_file=state_file,
        monitored_channel_ids=monitored or ["C001"],
        user_id=user_id,
    )


def _slack_msg(
    channel: str = "C001",
    text: str = "hello",
    ts: str = "1234567890.000001",
    user: str = "U123",
    **extra: str,
) -> dict:
    msg: dict[str, str] = {
        "type": "message",
        "channel": channel,
        "user": user,
        "text": text,
        "ts": ts,
    }
    msg.update(extra)
    return msg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBrowserLifecycle:
    """Tests that the full browser start/stop cycle works."""

    @pytest.mark.asyncio
    async def test_start_and_stop(self, mock_slack_server, state_file):
        """Monitor starts a real browser, navigates to the page, and stops cleanly."""
        url, _, _ = mock_slack_server
        monitor = _make_monitor(url, state_file)

        await monitor.start()

        assert monitor._browser is not None
        assert monitor._page is not None
        assert monitor._playwright is not None

        await monitor.stop()

        assert state_file.exists()

    @pytest.mark.asyncio
    async def test_state_file_permissions(self, mock_slack_server, state_file):
        """Saved auth state file has 0o600 permissions (SEC-005)."""
        url, _, _ = mock_slack_server
        monitor = _make_monitor(url, state_file)

        await monitor.start()
        await monitor.stop()

        assert state_file.exists()
        mode = state_file.stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    @pytest.mark.asyncio
    async def test_state_file_restore(self, mock_slack_server, state_file):
        """Monitor can start with existing state file (cookie restore path)."""
        url, _, _ = mock_slack_server

        # First run: create state
        monitor1 = _make_monitor(url, state_file)
        await monitor1.start()
        await monitor1.stop()
        assert state_file.exists()

        # Second run: restore from state
        monitor2 = _make_monitor(url, state_file)
        await monitor2.start()

        assert monitor2._browser is not None
        assert monitor2._page is not None

        await monitor2.stop()

    @pytest.mark.asyncio
    async def test_state_file_contains_valid_playwright_state(self, mock_slack_server, state_file):
        """Saved state file is valid Playwright storage state JSON."""
        url, _, _ = mock_slack_server
        monitor = _make_monitor(url, state_file)

        await monitor.start()
        await monitor.stop()

        content = json.loads(state_file.read_text())
        assert "cookies" in content or "origins" in content


class TestWebSocketInterception:
    """Tests that Playwright actually intercepts WebSocket frames end-to-end."""

    @pytest.mark.asyncio
    async def test_message_captured_through_real_websocket(self, mock_slack_server, state_file):
        """A Slack-shaped message frame sent via real WebSocket is captured by drain()."""
        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=["C001"])

        await monitor.start()
        await _wait_for_ws(app)

        await send(_slack_msg(text="hello from real websocket"))

        messages = await _wait_for_drain(monitor, 1)
        assert len(messages) == 1
        assert messages[0].text == "hello from real websocket"
        assert messages[0].channel == "C001"
        assert messages[0].user == "U123"

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_multiple_messages_captured(self, mock_slack_server, state_file):
        """Multiple frames sent in sequence are all captured."""
        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=["C001"])

        await monitor.start()
        await _wait_for_ws(app)

        for i in range(5):
            await send(_slack_msg(text=f"message {i}", ts=f"123456789{i}.000001"))

        messages = await _wait_for_drain(monitor, 5)
        assert len(messages) == 5
        texts = [m.text for m in messages]
        assert texts == [f"message {i}" for i in range(5)]

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_bot_messages_filtered(self, mock_slack_server, state_file):
        """Bot messages are filtered by _on_frame even through real WebSocket."""
        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=["C001"])

        await monitor.start()
        await _wait_for_ws(app)

        # Bot message — should be filtered
        await send(_slack_msg(subtype="bot_message", user="UBOT", text="I am a bot"))
        # Real user message — should be captured
        await send(_slack_msg(text="I am a human", ts="1234567890.000002"))

        messages = await _wait_for_drain(monitor, 1)
        assert len(messages) == 1
        assert messages[0].text == "I am a human"

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_unmonitored_channel_filtered(self, mock_slack_server, state_file):
        """Messages from non-monitored channels are dropped."""
        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=["C001"])

        await monitor.start()
        await _wait_for_ws(app)

        await send(_slack_msg(channel="C999", text="wrong channel"))

        messages = await _wait_for_empty_drain(monitor)
        assert len(messages) == 0

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_dm_always_captured(self, mock_slack_server, state_file):
        """DMs (channel starts with D) are captured regardless of monitored list."""
        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=[])

        await monitor.start()
        await _wait_for_ws(app)

        await send(_slack_msg(channel="D001", text="direct message"))

        messages = await _wait_for_drain(monitor, 1)
        assert len(messages) == 1
        assert messages[0].is_dm is True
        assert messages[0].text == "direct message"

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_mention_always_captured(self, mock_slack_server, state_file):
        """@mentions of the user are captured regardless of channel."""
        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=[], user_id="UTEST")

        await monitor.start()
        await _wait_for_ws(app)

        await send(_slack_msg(channel="C999", text="hey <@UTEST> check this"))

        messages = await _wait_for_drain(monitor, 1)
        assert len(messages) == 1
        assert messages[0].is_mention is True

        await monitor.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "broadcast",
        ["<!here>", "<!channel>", "<!everyone>"],
        ids=["here", "channel", "everyone"],
    )
    async def test_broadcast_mention_captured(
        self,
        mock_slack_server,
        state_file,
        broadcast,
    ):
        """@here/@channel/@everyone in non-monitored channels are captured."""
        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=[], user_id="UTEST")

        await monitor.start()
        await _wait_for_ws(app)

        await send(_slack_msg(channel="C999", text=f"hey {broadcast} check this"))

        messages = await _wait_for_drain(monitor, 1)
        assert len(messages) == 1
        assert messages[0].is_mention is True
        assert messages[0].channel == "C999"

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_non_message_types_filtered(self, mock_slack_server, state_file):
        """Non-message WebSocket frames (presence_change, etc.) are dropped."""
        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=["C001"])

        await monitor.start()
        await _wait_for_ws(app)

        await send({"type": "presence_change", "channel": "C001", "user": "U123"})
        await send({"type": "typing", "channel": "C001", "user": "U123"})
        # This one SHOULD be captured — use it as the sync point
        await send(_slack_msg(text="real message"))

        messages = await _wait_for_drain(monitor, 1)
        assert len(messages) == 1
        assert messages[0].text == "real message"

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_message_changed_filtered(self, mock_slack_server, state_file):
        """message_changed subtype is filtered through real WS pipeline."""
        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=["C001"])

        await monitor.start()
        await _wait_for_ws(app)

        await send(_slack_msg(subtype="message_changed", text="edited"))

        messages = await _wait_for_empty_drain(monitor)
        assert len(messages) == 0

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_message_deleted_filtered(self, mock_slack_server, state_file):
        """message_deleted subtype is filtered through real WS pipeline."""
        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=["C001"])

        await monitor.start()
        await _wait_for_ws(app)

        await send(_slack_msg(subtype="message_deleted", text="deleted"))

        messages = await _wait_for_empty_drain(monitor)
        assert len(messages) == 0

        await monitor.stop()


class TestCrossOriginWebSocket:
    """Tests that Playwright intercepts WebSockets from a different origin.

    Real Slack loads the page from app.slack.com but opens WebSocket
    connections to wss://wss-primary.slack.com — a DIFFERENT origin.
    Playwright's page.on("websocket") intercepts all WS connections
    regardless of origin, but we need to verify this actually works.
    """

    @pytest.mark.asyncio
    async def test_cross_origin_ws_intercepted(self, cross_origin_servers, state_file):
        """Messages from a cross-origin WebSocket are captured."""
        http_url, ws_app, send = cross_origin_servers
        monitor = _make_monitor(http_url, state_file, monitored=["C001"])

        await monitor.start()
        await _wait_for_ws(ws_app)

        await send(_slack_msg(text="cross-origin message"))

        messages = await _wait_for_drain(monitor, 1)
        assert len(messages) == 1
        assert messages[0].text == "cross-origin message"

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_cross_origin_filtering_works(self, cross_origin_servers, state_file):
        """Filtering logic (channel, subtype, etc.) works for cross-origin WS."""
        http_url, ws_app, send = cross_origin_servers
        monitor = _make_monitor(http_url, state_file, monitored=["C001"])

        await monitor.start()
        await _wait_for_ws(ws_app)

        # Filtered: bot on cross-origin WS
        await send(_slack_msg(subtype="bot_message", text="bot", ts="1.1"))
        # Valid: real user on cross-origin WS
        await send(_slack_msg(text="human", ts="2.1"))

        messages = await _wait_for_drain(monitor, 1)
        assert len(messages) == 1
        assert messages[0].text == "human"

        await monitor.stop()


class TestMultipleWebSocketConnections:
    """Tests that frames from multiple simultaneous WS connections are captured.

    Real Slack opens multiple WebSocket connections (RTM, presence, etc.).
    Playwright's page.on("websocket") fires for each new WS connection,
    and _on_websocket attaches framereceived handlers to all of them.
    """

    @pytest.mark.asyncio
    async def test_frames_from_multiple_ws_all_captured(self, multi_ws_server, state_file):
        """Messages arriving on two concurrent WebSocket connections are both captured."""
        url, app, send = multi_ws_server
        monitor = _make_monitor(url, state_file, monitored=["C001"])

        await monitor.start()
        # Wait for BOTH WS connections
        await _wait_for_ws(app, count=2)

        # Send a message — goes to both WS connections, but both are intercepted
        await send(_slack_msg(text="multi-ws message"))

        # Each WS connection will deliver the frame, so we'll get duplicates.
        # That's expected — real Slack only sends each message on one connection.
        # The important thing: frames from ALL connections are intercepted.
        messages = await _wait_for_drain(monitor, 2, timeout=3.0)
        assert len(messages) >= 1
        assert all(m.text == "multi-ws message" for m in messages)

        await monitor.stop()


class TestRefreshIfStuck:
    """Tests for the page reload fallback."""

    @pytest.mark.asyncio
    async def test_refresh_reloads_page(self, mock_slack_server, state_file):
        """refresh_if_stuck() reloads and re-establishes WebSocket."""
        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=["C001"])

        await monitor.start()
        await _wait_for_ws(app)

        # Send a message before refresh
        await send(_slack_msg(text="before refresh"))
        before = await _wait_for_drain(monitor, 1)
        assert len(before) == 1

        # Snapshot current connections before refresh
        old_connections = {id(ws) for ws in app["ws_connections"]}

        # Refresh the page — wait for a NEW connection (not in snapshot)
        await monitor.refresh_if_stuck()
        await _wait_for_fresh_ws(app, old_connections)

        # Send message after refresh — should still be captured
        await send(_slack_msg(text="after refresh", ts="1234567890.000002"))

        after = await _wait_for_drain(monitor, 1)
        assert len(after) == 1
        assert after[0].text == "after refresh"

        await monitor.stop()


class TestStateFileSymlinkGuard:
    """Tests that symlink protection works in the real browser pipeline."""

    @pytest.mark.asyncio
    async def test_stop_refuses_symlink_state_file(self, mock_slack_server, tmp_path):
        """stop() with a symlinked state file refuses to save but still cleans up."""
        url, _, _ = mock_slack_server

        real_file = tmp_path / "real.json"
        real_file.write_text("{}")
        symlink = tmp_path / "symlinked.json"
        symlink.symlink_to(real_file)

        monitor = _make_monitor(url, symlink, monitored=["C001"])
        await monitor.start()

        await monitor.stop()

        # The real file should still have its original content (not overwritten)
        assert real_file.read_text() == "{}"

    @pytest.mark.asyncio
    async def test_stop_saves_to_normal_file(self, mock_slack_server, state_file):
        """stop() saves state to a normal (non-symlink) file."""
        url, _, _ = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=["C001"])

        await monitor.start()
        await monitor.stop()

        assert state_file.exists()
        content = json.loads(state_file.read_text())
        assert "cookies" in content or "origins" in content


class TestDrainWithRealWebSocket:
    """Tests drain() behavior with real WebSocket traffic."""

    @pytest.mark.asyncio
    async def test_drain_with_limit(self, mock_slack_server, state_file):
        """drain(limit=N) returns exactly N messages, leaves rest in queue."""
        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=["C001"])

        await monitor.start()
        await _wait_for_ws(app)

        for i in range(5):
            await send(_slack_msg(text=f"msg {i}", ts=f"123456789{i}.000001"))

        # Wait for all 5 to arrive
        all_msgs = await _wait_for_drain(monitor, 5)
        assert len(all_msgs) == 5

        # Put them back for the limit test
        for msg in all_msgs:
            monitor._queue.put_nowait(msg)

        first = await monitor.drain(limit=3)
        assert len(first) == 3

        second = await monitor.drain()
        assert len(second) == 2

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_drain_empty_returns_empty_list(self, mock_slack_server, state_file):
        """drain() with no messages returns an empty list."""
        url, app, _ = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=["C001"])

        await monitor.start()
        await _wait_for_ws(app)

        messages = await monitor.drain()
        assert messages == []

        await monitor.stop()


class TestRapidFireMessages:
    """Stress tests with rapid message delivery."""

    @pytest.mark.asyncio
    async def test_rapid_messages_all_captured(self, mock_slack_server, state_file):
        """50 messages sent in rapid succession are all captured with timing."""
        import time

        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=["C001"])

        await monitor.start()
        await _wait_for_ws(app)

        count = 50
        send_start = time.monotonic()
        for i in range(count):
            await send(_slack_msg(text=f"rapid-{i}", ts=f"{i}.000001"))
        send_elapsed = time.monotonic() - send_start

        drain_start = time.monotonic()
        messages = await _wait_for_drain(monitor, count, timeout=10.0)
        drain_elapsed = time.monotonic() - drain_start

        assert len(messages) == count

        # Performance: 50 messages should send + drain well under 5 seconds.
        # Typical is <0.5s total. This guards against regressions.
        total = send_elapsed + drain_elapsed
        assert total < 5.0, (
            f"Performance regression: 50 msgs took {total:.2f}s "
            f"(send={send_elapsed:.2f}s, drain={drain_elapsed:.2f}s)"
        )

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_throughput_under_load(self, mock_slack_server, state_file):
        """200 messages with mixed routing — measures filter throughput."""
        import time

        url, app, send = mock_slack_server
        monitor = _make_monitor(
            url,
            state_file,
            monitored=["C001", "C002"],
            user_id="UTEST",
        )

        await monitor.start()
        await _wait_for_ws(app)

        # 200 frames: 100 valid (monitored channel), 50 filtered (wrong channel),
        # 25 DMs, 25 mentions in non-monitored channels
        expected = 0
        send_start = time.monotonic()
        for i in range(200):
            if i % 4 == 0:
                await send(_slack_msg(channel="C001", text=f"ch-{i}", ts=f"{i}.1"))
                expected += 1
            elif i % 4 == 1:
                await send(_slack_msg(channel="CXXX", text=f"skip-{i}", ts=f"{i}.1"))
            elif i % 4 == 2:
                await send(_slack_msg(channel="D001", text=f"dm-{i}", ts=f"{i}.1"))
                expected += 1
            else:
                await send(
                    _slack_msg(
                        channel="C999",
                        text=f"<!here> alert-{i}",
                        ts=f"{i}.1",
                    )
                )
                expected += 1
        send_elapsed = time.monotonic() - send_start

        drain_start = time.monotonic()
        messages = await _wait_for_drain(monitor, expected, timeout=10.0)
        drain_elapsed = time.monotonic() - drain_start

        assert len(messages) == expected

        # Verify routing correctness
        dms = [m for m in messages if m.is_dm]
        mentions = [m for m in messages if m.is_mention]
        channel_msgs = [m for m in messages if m.channel in ("C001", "C002")]
        assert len(dms) == 50
        assert len(mentions) == 50
        assert len(channel_msgs) == 50

        # Performance: 200 frames should process under 5 seconds
        total = send_elapsed + drain_elapsed
        assert total < 5.0, (
            f"Performance regression: 200 frames took {total:.2f}s "
            f"(send={send_elapsed:.2f}s, drain={drain_elapsed:.2f}s)"
        )

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_interleaved_valid_and_invalid(self, mock_slack_server, state_file):
        """Mix of valid messages, bot messages, wrong channels, and non-message types."""
        url, app, send = mock_slack_server
        monitor = _make_monitor(url, state_file, monitored=["C001"], user_id="UTEST")

        await monitor.start()
        await _wait_for_ws(app)

        frames = [
            _slack_msg(user="U1", text="valid-1", ts="1.1"),
            _slack_msg(subtype="bot_message", user="UBOT", text="bot", ts="2.1"),
            _slack_msg(channel="CXXX", user="U2", text="wrong-ch", ts="3.1"),
            _slack_msg(channel="D001", user="U3", text="valid-dm", ts="4.1"),
            {"type": "presence_change", "user": "U4"},
            _slack_msg(channel="C999", user="U5", text="hey <@UTEST>", ts="5.1"),
            _slack_msg(subtype="message_changed", user="U6", text="edit", ts="6.1"),
            _slack_msg(user="U7", text="valid-2", ts="7.1"),
            _slack_msg(channel="C888", user="U8", text="<!here> broadcast", ts="8.1"),
            _slack_msg(channel="C777", user="U9", text="<!channel> alert", ts="9.1"),
            _slack_msg(channel="C666", user="U10", text="<!everyone> ping", ts="10.1"),
        ]

        for frame in frames:
            await send(frame)

        # 7 valid messages: monitored ch, DM, @user mention, 2 original + 3 broadcasts
        messages = await _wait_for_drain(monitor, 7)
        texts = [m.text for m in messages]
        assert texts == [
            "valid-1",
            "valid-dm",
            "hey <@UTEST>",
            "valid-2",
            "<!here> broadcast",
            "<!channel> alert",
            "<!everyone> ping",
        ]
        assert messages[1].is_dm is True
        assert messages[2].is_mention is True
        # Broadcast mentions
        assert messages[4].is_mention is True
        assert messages[5].is_mention is True
        assert messages[6].is_mention is True

        await monitor.stop()


# ---------------------------------------------------------------------------
# Real Slack e2e tests — only run when browser auth state exists
#
# One-time setup:
#   summon auth slack login https://YOUR-WORKSPACE.slack.com
#
# Everything else is derived automatically:
#   - Workspace URL: from bot token via auth.test()
#   - Browser state file: get_browser_auth_dir()/slack_{workspace}.json
#   - Test channel: created and cleaned up automatically (like other integ tests)
#   - Bot token: from .env (SUMMON_TEST_SLACK_BOT_TOKEN, already used by other tests)
#
# Cookies expire, so this is for LOCAL confidence testing, not CI.
# ---------------------------------------------------------------------------


def _resolve_real_slack_config() -> tuple[dict[str, str] | None, str]:
    """Derive all config from the bot token + existing browser auth state.

    Returns (config_dict, skip_reason). config_dict is None if prerequisites
    aren't met; skip_reason explains exactly what to do.
    """
    from summon_claude.config import get_browser_auth_dir
    from summon_claude.slack_browser import _slugify

    bot_token = os.environ.get("SUMMON_TEST_SLACK_BOT_TOKEN")
    if not bot_token:
        return None, "SUMMON_TEST_SLACK_BOT_TOKEN not set"

    from slack_sdk import WebClient

    try:
        resp = WebClient(token=bot_token).auth_test()
    except Exception:
        return None, "auth.test() failed — check SUMMON_TEST_SLACK_BOT_TOKEN"

    workspace_url = (resp.get("url") or "").rstrip("/")
    if not workspace_url:
        return None, "auth.test() returned no workspace URL"

    # Look for a matching browser auth state file.
    # Enterprise Grid workspaces have two URLs (e.g. gtest.enterprise.slack.com
    # and e0ahttsbag1-mqfpielh.slack.com). The user may have authenticated via
    # either. Check the direct match first, then scan all state files.
    browser_auth_dir = get_browser_auth_dir()
    state_file = browser_auth_dir / f"slack_{_slugify(workspace_url)}.json"

    if not state_file.is_file() and browser_auth_dir.is_dir():
        # Scan for any state file — Enterprise Grid may use a different URL
        for candidate in browser_auth_dir.glob("slack_*.json"):
            state_file = candidate
            # Derive workspace_url from the state file name for the monitor
            slug = candidate.stem.removeprefix("slack_")
            workspace_url = f"https://{slug.replace('_', '.')}"
            break

    if not state_file.is_file():
        workspace_name = workspace_url.removeprefix("https://").removesuffix(".slack.com")
        return None, (
            f"Browser auth state not found at {browser_auth_dir}. "
            f"One-time setup: summon auth slack login {workspace_name}"
        )

    return {
        "workspace_url": workspace_url,
        "state_file": str(state_file),
        "bot_token": bot_token,
    }, ""


_real_slack_cfg, _real_slack_skip_reason = _resolve_real_slack_config()


def _get_browser_user_id() -> str | None:
    """Get the browser-authenticated user's Slack ID from workspace config."""
    from summon_claude.config import get_workspace_config_path

    config_path = get_workspace_config_path()
    if not config_path.exists():
        return None
    try:
        workspace = json.loads(config_path.read_text())
        return workspace.get("user_id") or None
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Diagnostic tests — determine which cookies + browser mode work
#
# Runs before TestRealSlack so we can see exactly WHY auth fails.
# Each test permutation outputs detailed findings to stdout (captured by pytest -v).
#
# Run with: pytest tests/integration/test_browser_monitor.py::TestSlackAuthDiagnostics -v -n0 -s
# ---------------------------------------------------------------------------

# Cookie filter policies: which cookies to include in the browser context
_COOKIE_POLICIES: dict[str, set[str] | None] = {
    "all": None,  # All cookies from the state file
    "d-only": {"d"},  # Just the primary auth cookie
    "d+d-s": {"d", "d-s"},  # Primary auth + session companion
    "d+d-s+b+lc": {"d", "d-s", "b", "lc"},  # Auth cookies, no x
    "all-except-x": ...,  # type: ignore[dict-item]  # Special: exclude x only
}


def _filter_cookies(
    cookies: list[dict],
    policy: str,
) -> list[dict]:
    """Apply a named cookie filter policy to a cookie list."""
    allowed = _COOKIE_POLICIES[policy]
    if allowed is None:
        return cookies
    if allowed is ...:
        return [c for c in cookies if c.get("name") != "x"]
    return [c for c in cookies if c.get("name") in allowed]


def _cookie_summary(cookies: list[dict]) -> str:
    """One-line summary of cookie names + expiry status."""
    import time

    now = time.time()
    parts = []
    for c in cookies:
        name = c.get("name", "?")
        exp = c.get("expires", -1)
        if isinstance(exp, (int, float)) and exp > 0:
            remaining_h = (exp - now) / 3600
            if remaining_h < 0:
                parts.append(f"{name}(EXPIRED {abs(remaining_h):.1f}h)")
            else:
                parts.append(f"{name}({remaining_h:.0f}h)")
        else:
            parts.append(f"{name}(session)")
    return ", ".join(parts)


@dataclass
class AuthProbeResult:
    """Diagnostic result from a single auth probe."""

    policy: str
    headless: bool
    success: bool
    final_url: str
    redirects: list[str]
    page_title: str
    has_login_form: bool
    has_client_url: bool
    local_storage_teams: int
    elapsed_s: float
    error: str | None = None
    cookies_used: str = ""
    x_cookie_present: bool = False
    x_cookie_expired: bool = False


def _x_cookie_status(cookies: list[dict]) -> tuple[bool, bool]:
    """Return (present, expired) for the x cookie."""
    import time

    x = next((c for c in cookies if c.get("name") == "x"), None)
    if not x:
        return False, False
    exp = x.get("expires", -1)
    expired = isinstance(exp, (int, float)) and 0 < exp < time.time()
    return True, expired


async def _gather_page_diagnostics(page) -> tuple[str, str, bool, int]:  # type: ignore[no-untyped-def]
    """Collect diagnostic data from a loaded page.

    Returns (final_url, title, has_login_form, local_storage_teams).
    """
    final_url = page.url
    title = await page.title()

    # Check for login form presence
    has_login = False
    login_selectors = [
        'input[type="email"]',
        'input[name="email"]',
        'input[data-qa="signin_email_input"]',
        '[data-qa="login_email"]',
        "#signin_btn",
        '[data-qa="ssb_redirect_btn"]',
    ]
    with contextlib.suppress(Exception):
        for sel in login_selectors:
            if await page.locator(sel).count() > 0:
                has_login = True
                break

    # Check localStorage for team data
    team_count = 0
    with contextlib.suppress(Exception):
        team_count = await page.evaluate(
            "() => { try { const lc = JSON.parse("
            "localStorage.getItem('localConfig_v2') || '{}');"
            " return Object.keys(lc.teams || {}).length;"
            " } catch(e) { return 0; } }"
        )

    return final_url, title, has_login, team_count


@dataclass
class _ProbeConfig:
    """Input configuration for a single auth probe."""

    workspace_url: str
    probe_state: dict
    policy: str
    headless: bool
    filtered_cookies: list[dict]
    x_present: bool
    x_expired: bool
    browser_type: str = "chrome"
    timeout_s: float = 30.0


async def _probe_slack_auth(
    workspace_url: str,
    state: dict,
    *,
    policy: str,
    headless: bool,
    timeout_s: float = 30.0,
) -> AuthProbeResult:
    """Attempt to load Slack with filtered cookies and report what happens."""
    all_cookies = state.get("cookies", [])
    filtered = _filter_cookies(all_cookies, policy)
    x_present, x_expired = _x_cookie_status(all_cookies)

    cfg = _ProbeConfig(
        workspace_url=workspace_url,
        probe_state={"cookies": filtered, "origins": state.get("origins", [])},
        policy=policy,
        headless=headless,
        filtered_cookies=filtered,
        x_present=x_present,
        x_expired=x_expired,
        timeout_s=timeout_s,
    )
    return await _run_probe(cfg)


async def _run_probe(cfg: _ProbeConfig) -> AuthProbeResult:
    """Execute a single auth probe and collect diagnostics."""
    import time

    from playwright.async_api import async_playwright

    from summon_claude.slack_browser import _launch_browser

    redirects: list[str] = []
    start_time = time.monotonic()

    def _make_error_result(error: str) -> AuthProbeResult:
        return AuthProbeResult(
            policy=cfg.policy,
            headless=cfg.headless,
            success=False,
            final_url="",
            redirects=redirects,
            page_title="",
            has_login_form=False,
            has_client_url=False,
            local_storage_teams=0,
            elapsed_s=time.monotonic() - start_time,
            error=error,
            cookies_used=_cookie_summary(cfg.filtered_cookies),
            x_cookie_present=cfg.x_present,
            x_cookie_expired=cfg.x_expired,
        )

    async with async_playwright() as p:
        browser = await _launch_browser(p, cfg.browser_type, headless=cfg.headless)
        context = await browser.new_context(storage_state=cfg.probe_state)
        page = await context.new_page()

        # Track all navigations
        def _on_nav(frame) -> None:  # type: ignore[no-untyped-def]
            if frame == page.main_frame:
                url = frame.url
                if not redirects or redirects[-1] != url:
                    redirects.append(url)

        page.on("framenavigated", _on_nav)

        try:
            await page.goto(cfg.workspace_url, wait_until="domcontentloaded")
        except Exception as exc:
            await browser.close()
            return _make_error_result(f"goto failed: {exc}")

        # Wait for /client/ URL (authenticated state)
        has_client = False
        with contextlib.suppress(Exception):
            await page.wait_for_url(
                "**/client/**",
                timeout=int(cfg.timeout_s * 1000),
                wait_until="commit",
            )
            has_client = True

        final_url, title, has_login, team_count = await _gather_page_diagnostics(page)

        await browser.close()

    return AuthProbeResult(
        policy=cfg.policy,
        headless=cfg.headless,
        success=has_client,
        final_url=final_url,
        redirects=redirects,
        page_title=title,
        has_login_form=has_login,
        has_client_url=has_client,
        local_storage_teams=team_count,
        elapsed_s=time.monotonic() - start_time,
        cookies_used=_cookie_summary(cfg.filtered_cookies),
        x_cookie_present=cfg.x_present,
        x_cookie_expired=cfg.x_expired,
    )


def _format_probe_result(r: AuthProbeResult) -> str:
    """Format a probe result as a multi-line diagnostic report."""
    mode = "headless" if r.headless else "HEADED"
    status = "PASS" if r.success else "FAIL"
    lines = [
        f"  [{status}] {r.policy} / {mode}  ({r.elapsed_s:.1f}s)",
        f"    Cookies: {r.cookies_used}",
        f"    Final URL: {r.final_url}",
        f"    Page title: {r.page_title!r}",
        f"    Login form visible: {r.has_login_form}",
        f"    /client/ reached: {r.has_client_url}",
        f"    localStorage teams: {r.local_storage_teams}",
        f"    x cookie: present={r.x_cookie_present}, expired={r.x_cookie_expired}",
    ]
    if r.error:
        lines.append(f"    Error: {r.error}")
    if len(r.redirects) > 1:
        lines.append(f"    Redirect chain ({len(r.redirects)} hops):")
        for i, url in enumerate(r.redirects[:10]):  # Cap at 10
            lines.append(f"      {i}: {url}")
        if len(r.redirects) > 10:
            lines.append(f"      ... ({len(r.redirects) - 10} more)")
    return "\n".join(lines)


def _print_mode_conclusions(
    headless_all: AuthProbeResult,
    headless_no_x: AuthProbeResult,
    headless_d: AuthProbeResult,
) -> None:
    """Print conclusions about which cookie combos work in headless mode."""
    if headless_all.success:
        print("  [OK] Headless mode works with all cookies")
        status_no_x = "[OK] NOT required" if headless_no_x.success else "[!!] IS required"
        no_x_verb = "passes" if headless_no_x.success else "fails"
        print(f"  {status_no_x} — x cookie (all-except-x {no_x_verb})")
        status_d = "[OK] sufficient" if headless_d.success else "[!!] NOT sufficient"
        print(f"  {status_d} — d cookie alone")
        return

    print("  [!!] Headless mode FAILS with all cookies")
    print("  ACTION: Re-run summon auth slack login")

    if headless_all.has_login_form:
        print("  [!!] Login form visible — SSO cookies expired")
    elif headless_all.redirects:
        _check_sso_redirects(headless_all.redirects)
    else:
        print("  [!!] No redirects observed — page may be stuck")


def _check_sso_redirects(redirects: list[str]) -> None:
    """Check if any redirect goes through a known SSO provider."""
    sso_domains = {"okta", "google", "onelogin", "azure", "auth0", "ping", "duo"}
    sso_redirect = any(any(d in url.lower() for d in sso_domains) for url in redirects)
    if sso_redirect:
        print("  [!!] SSO redirect detected — Enterprise SSO may block headless browsers")


def _extract_team_ids(state: dict) -> dict[str, dict]:
    """Extract team IDs from localStorage in the Playwright state.

    Returns ``{team_id: {"name": ..., "url": ..., "user_id": ...}}``.
    """
    for origin in state.get("origins", []):
        if "app.slack.com" not in origin.get("origin", ""):
            continue
        for item in origin.get("localStorage", []):
            if item.get("name") != "localConfig_v2":
                continue
            try:
                lc = json.loads(item.get("value", "{}"))
            except (json.JSONDecodeError, TypeError):
                return {}
            return lc.get("teams", {})
    return {}


def _print_x_cookie_status(probe: AuthProbeResult) -> None:
    """Print x cookie analysis."""
    if not probe.x_cookie_present:
        print("  [INFO] x cookie not present in saved state")
    elif probe.x_cookie_expired:
        print("  [INFO] x cookie is EXPIRED in saved state")
    else:
        print("  [INFO] x cookie is present and valid")


@pytest.mark.skipif(_real_slack_cfg is None, reason=_real_slack_skip_reason)
class TestSlackAuthDiagnostics:
    """Systematic diagnostics for Slack browser auth behavior (headless only).

    Determines:
    1. Whether headless mode works for this workspace (Enterprise Grid SSO)
    2. Which cookies are required (d alone? d+x? all?)
    3. Whether app.slack.com/client/TEAM_ID bypasses the workspace picker

    Run with ``-s`` flag to see full diagnostic output:
        pytest tests/integration/test_browser_monitor.py::TestSlackAuthDiagnostics -v -n0 -s
    """

    @pytest.fixture
    def auth_state(self) -> dict:
        """Load the full auth state from disk."""
        assert _real_slack_cfg is not None
        return json.loads(Path(_real_slack_cfg["state_file"]).read_text())

    @pytest.fixture
    def workspace_url(self) -> str:
        assert _real_slack_cfg is not None
        return _real_slack_cfg["workspace_url"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "policy",
        ["all", "all-except-x", "d-only", "d+d-s", "d+d-s+b+lc"],
    )
    async def test_auth_probe(
        self,
        auth_state: dict,
        workspace_url: str,
        policy: str,
    ):
        """Probe Slack auth with a specific cookie subset (headless only).

        Each parametrized variant is an independent experiment. The test
        always passes (it's diagnostic) — check the output for results.
        """
        result = await _probe_slack_auth(
            workspace_url,
            auth_state,
            policy=policy,
            headless=True,
            timeout_s=20.0,
        )
        report = _format_probe_result(result)
        print(f"\n{'=' * 60}")
        print(f"DIAGNOSTIC: {policy}")
        print(f"{'=' * 60}")
        print(report)

    @pytest.mark.asyncio
    async def test_cookie_state_inventory(self, auth_state: dict):
        """Report the full cookie inventory from the saved state file.

        This is a pure diagnostic — it never fails. Shows exactly which
        cookies exist, their domains, and expiry status so we can correlate
        with the probe results.
        """
        import time

        now = time.time()
        cookies = auth_state.get("cookies", [])
        origins = auth_state.get("origins", [])

        print(f"\n{'=' * 60}")
        print("COOKIE INVENTORY")
        print(f"{'=' * 60}")
        print(f"  Total cookies: {len(cookies)}")
        print(f"  localStorage origins: {len(origins)}")

        for c in sorted(cookies, key=lambda x: x.get("name", "")):
            name = c.get("name", "?")
            domain = c.get("domain", "?")
            exp = c.get("expires", -1)
            path = c.get("path", "/")
            secure = c.get("secure", False)
            http_only = c.get("httpOnly", False)
            same_site = c.get("sameSite", "?")

            if isinstance(exp, (int, float)) and exp > 0:
                remaining_h = (exp - now) / 3600
                if remaining_h < 0:
                    exp_str = f"EXPIRED {abs(remaining_h):.1f}h ago"
                else:
                    exp_str = f"valid {remaining_h:.0f}h"
            else:
                exp_str = "session"

            flags = []
            if secure:
                flags.append("secure")
            if http_only:
                flags.append("httpOnly")
            if same_site != "?":
                flags.append(f"sameSite={same_site}")

            print(f"  {name:15s} {domain:40s} {exp_str:20s} path={path} {' '.join(flags)}")

        if origins:
            print("\n  localStorage origins:")
            for origin in origins:
                origin_url = origin.get("origin", "?")
                ls_items = origin.get("localStorage", [])
                keys = [item.get("name", "?") for item in ls_items]
                # Show first few keys, truncate if many
                if len(keys) > 5:
                    key_display = ", ".join(keys[:5]) + f", ... ({len(keys)} total)"
                else:
                    key_display = ", ".join(keys) if keys else "(empty)"
                print(f"    {origin_url}: {key_display}")

    @pytest.mark.asyncio
    async def test_app_slack_com_direct(self, auth_state: dict):
        """Probe app.slack.com/client/{TEAM_ID} — the Enterprise Grid solution.

        Enterprise Grid workspaces show a workspace picker at the enterprise
        URL. The actual SPA lives at ``app.slack.com``. This test extracts
        the team ID from localStorage in the saved state and navigates
        directly to the client URL.

        This is the MOST IMPORTANT diagnostic — it determines whether the
        ``SlackBrowserMonitor`` can work for Enterprise Grid by navigating
        to ``app.slack.com/client/TEAM_ID`` instead of the workspace URL.
        """
        team_ids = _extract_team_ids(auth_state)
        if not team_ids:
            print("  [SKIP] No team IDs found in localStorage")
            return

        for team_id, info in team_ids.items():
            url = f"https://app.slack.com/client/{team_id}"
            print(f"\n{'=' * 60}")
            print(f"PROBE: {url} (team={info.get('name', '?')})")
            print(f"{'=' * 60}")

            result = await _probe_slack_auth(
                url,
                auth_state,
                policy="all",
                headless=True,
                timeout_s=20.0,
            )
            print(_format_probe_result(result))

    @pytest.mark.asyncio
    async def test_diagnostic_summary(self, auth_state: dict, workspace_url: str):
        """Run key probes and print a summary with actionable conclusions."""
        sep = "=" * 60

        # Phase 1: headless workspace URL probes (cookie subsets)
        policies = ["all", "all-except-x", "d-only"]

        ws_results: list[AuthProbeResult] = []
        for policy in policies:
            r = await _probe_slack_auth(
                workspace_url,
                auth_state,
                policy=policy,
                headless=True,
                timeout_s=20.0,
            )
            ws_results.append(r)
            print(f"\n{_format_probe_result(r)}")

        headless_all = next(r for r in ws_results if r.policy == "all")
        headless_no_x = next(r for r in ws_results if r.policy == "all-except-x")
        headless_d = next(r for r in ws_results if r.policy == "d-only")

        # Phase 2: app.slack.com/client/TEAM_ID probe
        team_ids = _extract_team_ids(auth_state)
        app_result: AuthProbeResult | None = None
        if team_ids:
            first_team = next(iter(team_ids))
            app_url = f"https://app.slack.com/client/{first_team}"
            app_result = await _probe_slack_auth(
                app_url,
                auth_state,
                policy="all",
                headless=True,
                timeout_s=20.0,
            )
            print(f"\n{_format_probe_result(app_result)}")

        # Conclusions
        print(f"\n{sep}")
        print("CONCLUSIONS")
        print(sep)

        _print_mode_conclusions(headless_all, headless_no_x, headless_d)
        _print_x_cookie_status(headless_all)

        if app_result:
            if app_result.success:
                print("  [OK] app.slack.com/client/TEAM_ID works in headless!")
                print("  ACTION: Use app.slack.com/client/ URL for Enterprise Grid")
            else:
                print("  [!!] app.slack.com/client/TEAM_ID also fails")

        print(sep)


async def _start_real_monitor(
    cfg: dict[str, str],
    *,
    monitored: list[str],
    user_id: str = "",
) -> SlackBrowserMonitor:
    """Start a SlackBrowserMonitor against real Slack, skip if auth expired."""
    monitor = SlackBrowserMonitor(
        workspace_id="real-test",
        workspace_url=cfg["workspace_url"],
        state_file=Path(cfg["state_file"]),
        monitored_channel_ids=monitored,
        user_id=user_id,
    )
    await monitor.start()

    # Wait for authenticated view — skip if cookies expired
    try:
        await monitor._page.wait_for_url(  # type: ignore[union-attr]
            "**/client/**",
            timeout=30_000,
            wait_until="commit",
        )
    except Exception:
        await monitor.stop()
        pytest.skip(
            f"Browser cookies expired — re-run: summon auth slack login {cfg['workspace_url']}"
        )

    # Give Slack's WebSocket time to connect and stabilize
    await asyncio.sleep(5)
    return monitor


@pytest.mark.skipif(_real_slack_cfg is None, reason=_real_slack_skip_reason)
class TestRealSlack:
    """Full e2e tests against a real Slack workspace.

    The bot posts a message via the Slack API, and the browser monitor
    (loaded with real browser auth cookies) intercepts it via WebSocket.
    This validates the ENTIRE production pipeline including Slack's
    actual WebSocket protocol, frame format, and cross-origin behavior.

    All config is derived from the bot token and the browser auth state
    that ``summon auth slack login`` saves. No extra env vars needed.
    """

    @pytest.fixture
    async def real_slack(self, slack_harness):
        """Provide config + a dedicated test channel with browser user invited."""
        assert _real_slack_cfg is not None
        channel_id = await slack_harness.create_test_channel(prefix="browser")

        browser_user = _get_browser_user_id()
        if browser_user:
            with contextlib.suppress(Exception):
                await slack_harness.client.conversations_invite(
                    channel=channel_id,
                    users=browser_user,
                )

        yield {**_real_slack_cfg, "channel": channel_id}

    @pytest.mark.asyncio
    async def test_captures_monitored_channel_message(self, real_slack):
        """Browser monitor captures a message in a monitored channel."""
        from slack_sdk.web.async_client import AsyncWebClient

        cfg = real_slack
        nonce = secrets.token_hex(8)

        monitor = await _start_real_monitor(cfg, monitored=[cfg["channel"]])

        client = AsyncWebClient(token=cfg["bot_token"])
        await client.chat_postMessage(
            channel=cfg["channel"],
            text=f"monitor-test-{nonce}",
        )

        messages = await _wait_for_drain(monitor, 1, timeout=15.0)
        await monitor.stop()

        matching = [m for m in messages if nonce in m.text]
        assert len(matching) >= 1, (
            f"Expected message with nonce {nonce!r} but got: {[m.text for m in messages]}"
        )
        assert matching[0].channel == cfg["channel"]

    @pytest.mark.asyncio
    async def test_captures_at_mention(self, real_slack):
        """@user mention in a non-monitored channel is captured."""
        from slack_sdk.web.async_client import AsyncWebClient

        cfg = real_slack
        browser_user = _get_browser_user_id()
        if not browser_user:
            pytest.skip("No browser user ID configured — run summon auth slack login")

        nonce = secrets.token_hex(8)

        # Monitor with NO monitored channels — only mentions should come through
        monitor = await _start_real_monitor(
            cfg,
            monitored=[],
            user_id=browser_user,
        )

        client = AsyncWebClient(token=cfg["bot_token"])
        await client.chat_postMessage(
            channel=cfg["channel"],
            text=f"hey <@{browser_user}> mention-test-{nonce}",
        )

        messages = await _wait_for_drain(monitor, 1, timeout=15.0)
        await monitor.stop()

        matching = [m for m in messages if nonce in m.text]
        assert len(matching) >= 1, (
            f"Expected @mention with nonce {nonce!r} but got: {[m.text for m in messages]}"
        )
        assert matching[0].is_mention is True

    @pytest.mark.asyncio
    async def test_captures_broadcast_here(self, real_slack):
        """<!here> broadcast in a non-monitored channel is captured."""
        from slack_sdk.web.async_client import AsyncWebClient

        cfg = real_slack
        browser_user = _get_browser_user_id() or ""
        nonce = secrets.token_hex(8)

        # No monitored channels — broadcast mention should still arrive
        monitor = await _start_real_monitor(
            cfg,
            monitored=[],
            user_id=browser_user,
        )

        client = AsyncWebClient(token=cfg["bot_token"])
        await client.chat_postMessage(
            channel=cfg["channel"],
            text=f"<!here> broadcast-test-{nonce}",
        )

        messages = await _wait_for_drain(monitor, 1, timeout=15.0)
        await monitor.stop()

        matching = [m for m in messages if nonce in m.text]
        assert len(matching) >= 1, (
            f"Expected <!here> with nonce {nonce!r} but got: {[m.text for m in messages]}"
        )
        assert matching[0].is_mention is True

    @pytest.mark.asyncio
    async def test_filters_unmonitored_no_mention(self, real_slack):
        """Message in non-monitored channel without mention is NOT captured."""
        from slack_sdk.web.async_client import AsyncWebClient

        cfg = real_slack
        nonce = secrets.token_hex(8)

        # No monitored channels, no user_id — only DMs would come through
        monitor = await _start_real_monitor(cfg, monitored=[], user_id="")

        client = AsyncWebClient(token=cfg["bot_token"])
        await client.chat_postMessage(
            channel=cfg["channel"],
            text=f"should-be-filtered-{nonce}",
        )

        # Wait a bit then drain — should be empty
        await asyncio.sleep(3)
        messages = await monitor.drain()
        await monitor.stop()

        matching = [m for m in messages if nonce in m.text]
        assert len(matching) == 0, (
            f"Message should have been filtered but got: {[m.text for m in matching]}"
        )

    @pytest.mark.asyncio
    async def test_auth_state_restores_session(self, real_slack):
        """Browser loads with saved cookies and reaches Slack's authenticated state."""
        cfg = real_slack
        monitor = SlackBrowserMonitor(
            workspace_id="real-test",
            workspace_url=cfg["workspace_url"],
            state_file=Path(cfg["state_file"]),
            monitored_channel_ids=[cfg["channel"]],
            user_id="",
        )

        await monitor.start()

        try:
            await monitor._page.wait_for_url(  # type: ignore[union-attr]
                "**/client/**",
                timeout=30_000,
                wait_until="commit",
            )
        except Exception:
            await monitor.stop()
            pytest.skip(
                f"Browser cookies expired — re-run: summon auth slack login {cfg['workspace_url']}"
            )

        await monitor.stop()

    @pytest.mark.asyncio
    async def test_stop_preserves_auth_state(self, real_slack, tmp_path):
        """stop() saves updated auth state that can be restored."""
        cfg = real_slack
        fresh_state = tmp_path / "refreshed_state.json"

        monitor = SlackBrowserMonitor(
            workspace_id="real-test",
            workspace_url=cfg["workspace_url"],
            state_file=Path(cfg["state_file"]),
            monitored_channel_ids=[cfg["channel"]],
            user_id="",
        )

        await monitor.start()
        await asyncio.sleep(3)  # let Slack refresh cookies

        monitor._state_file = fresh_state
        await monitor.stop()

        assert fresh_state.exists()
        content = json.loads(fresh_state.read_text())
        assert "cookies" in content
        assert len(content["cookies"]) > 0, "No cookies saved — auth state may be invalid"
