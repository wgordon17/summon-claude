"""Generate documentation screenshots using a real summon session + Playwright.

This script is a LOCAL developer tool — it cannot run in CI because it requires
a running Claude CLI, authenticated Anthropic account, and real Slack workspace.

The flow:
  1. Registers a project and starts a PM via `summon project up`
  2. Authenticates via `/summon CODE` in the Slack web UI (Playwright)
  3. Sends a task to the PM agent
  4. Waits for Claude to respond
  5. Screenshots the channel, thread, permission UI, and canvas
  6. Stops the project and archives the channel

Sections:
  slack-setup   — validates manually-captured Slack setup screenshots
  session-ux    — end-to-end Playwright screenshots of a real session
  terminal      — captures real CLI terminal output and injects into docs markdown

Environment variables:
  SUMMON_TEST_SLACK_BOT_TOKEN      Bot token for channel discovery, cleanup, and team ID

Browser auth: Uses saved Playwright state from `summon auth slack login` (preferred).
  Falls back to SUMMON_TEST_SLACK_COOKIE env var (raw `d` cookie) if no saved state.

Usage:
  uv run python scripts/docs-screenshots.py [OPTIONS]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import click

DEFAULT_OUTPUT_DIR = Path("docs/assets/screenshots")
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 900
TOP_NAV_HEIGHT = 44  # Slack workspace-level toolbar (search bar)
SANDBOX_BANNER_HEIGHT = 44  # developer sandbox banner at bottom
_SESSION_BASE = "docs-screenshots"
_SCROLL_TO_BOTTOM_JS = "document.querySelector('[data-qa=\"slack_kit_list\"]')?.scrollTo(0, 999999)"


def _redact_paths(page) -> None:
    """Replace filesystem paths in the visible DOM with sanitized versions.

    Replaces the full CWD first (catches worktree paths like
    ``/Users/.../worktrees/branch-name``), then replaces any remaining
    ``$HOME`` fragments so no absolute paths leak into screenshots.
    """
    cwd = str(Path.cwd())
    home = str(Path.home())
    page.evaluate(
        """([cwd, home]) => {
        const sel = 'td, span, div, a, code, pre';
        document.querySelectorAll(sel).forEach(el => {
            const t = el.textContent;
            if (el.children.length === 0 && (t.includes(cwd) || t.includes(home))) {
                el.textContent = t.replaceAll(cwd, '~/project').replaceAll(home, '~');
            }
        });
    }""",
        [cwd, home],
    )


def _unique_suffix() -> str:
    """Short timestamp suffix for unique channel/project names."""
    return str(int(time.time()))[-6:]


# Generated once per run so every function sees the same names.
_run_suffix = _unique_suffix()
SESSION_NAME = f"{_SESSION_BASE}-{_run_suffix}"
PROJECT_NAME = f"{_SESSION_BASE}-{_run_suffix}"

MANUAL_SCREENSHOTS = [
    {"name": "slack-setup-create-app.png", "description": "Create New App dialog at api.slack.com"},
    {"name": "slack-setup-app-workspace.png", "description": "Pick a workspace dialog"},
    {"name": "slack-setup-manifest.png", "description": "Paste manifest YAML screen"},
    {"name": "slack-setup-oauth-install.png", "description": "OAuth & Permissions install page"},
    {"name": "slack-setup-workspace-allow.png", "description": "Workspace permission consent"},
    {"name": "slack-setup-tokens.png", "description": "Bot User OAuth Token location"},
    {"name": "slack-setup-app-token.png", "description": "App-Level Tokens section"},
    {"name": "slack-setup-app-token-generate.png", "description": "Generate app-level token"},
    {"name": "slack-setup-app-token-properties.png", "description": "Generated token properties"},
    {"name": "slack-setup-socket-mode.png", "description": "Socket Mode toggle enabled"},
]


# ---------------------------------------------------------------------------
# Slack auth discovery
# ---------------------------------------------------------------------------


def _find_slack_auth_state() -> str | None:
    """Find saved Playwright state from ``summon auth slack login``.

    Reads the workspace config file and returns the auth_state_path if the
    state file exists on disk. Returns None if no saved auth is available.
    """
    try:
        from summon_claude.config import get_data_dir
    except ImportError:
        return None

    workspace_config = get_data_dir() / "slack_workspace.json"
    if not workspace_config.is_file():
        return None

    try:
        config = json.loads(workspace_config.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    state_path = config.get("auth_state_path", "")
    if state_path and Path(state_path).is_file():
        return state_path
    return None


# ---------------------------------------------------------------------------
# summon session lifecycle
# ---------------------------------------------------------------------------


def start_project_session() -> tuple[subprocess.Popen | None, str]:
    """Register a project and run `summon project up`, extract auth code."""
    env = _make_env()

    click.echo("  Registering screenshot project...")
    subprocess.run(
        ["summon", "project", "add", PROJECT_NAME, "."],
        capture_output=True,
        env=env,
    )

    click.echo("  Starting project PM...")
    proc = subprocess.Popen(
        ["summon", "project", "up"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    # Read stdout lines until we find the auth code
    # Matches both "SUMMON CODE: abc123" and "/summon abc123" formats
    code_pattern = re.compile(r"(?:SUMMON CODE:\s*|/summon\s+)(\w+)")
    short_code = None
    deadline = time.time() + 60  # PM takes longer to initialize

    if proc.stdout is None:  # guaranteed non-None by stdout=PIPE
        raise RuntimeError("proc.stdout is None despite stdout=PIPE")
    stdout = proc.stdout
    while time.time() < deadline:
        line = stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
            continue
        click.echo(f"    summon: {line.rstrip()}")
        m = code_pattern.search(line)
        if m:
            short_code = m.group(1)
            break

    if not short_code:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise RuntimeError("Failed to extract auth code from summon project up")

    click.echo(f"  Auth code: {short_code}")
    return proc, short_code


def stop_project_session(proc: subprocess.Popen | None) -> None:
    """Stop the project and clean up."""
    env = _make_env()
    click.echo("  Stopping project sessions...")
    try:
        subprocess.run(
            ["summon", "project", "down"],
            capture_output=True,
            timeout=15,
            env=env,
        )
    except Exception as exc:
        click.echo(f"  WARNING: project down failed: {exc}", err=True)

    click.echo("  Removing screenshot project...")
    try:
        subprocess.run(
            ["summon", "project", "remove", PROJECT_NAME, "--yes"],
            capture_output=True,
            timeout=15,
            env=env,
        )
    except Exception as exc:
        click.echo(f"  WARNING: project remove failed: {exc}", err=True)

    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _slack_api_call_with_retry(fn, *, max_retries: int = 5):  # type: ignore[no-untyped-def]
    """Call a Slack API function with retry on rate limiting."""
    from slack_sdk.errors import SlackApiError

    for attempt in range(max_retries + 1):
        try:
            return fn()
        except SlackApiError as exc:
            if exc.response.get("error") == "ratelimited" and attempt < max_retries:
                retry_after = int(exc.response.headers.get("Retry-After", 10))
                wait = max(retry_after, 5)
                click.echo(
                    f"  Rate limited, waiting {wait}s (attempt {attempt + 1}/{max_retries})..."
                )
                time.sleep(wait)
            else:
                raise
    return None  # unreachable, but satisfies type checker


def find_session_channel(bot_token: str) -> str | None:
    """Find the session channel by querying the summon DB for matching sessions.

    Avoids Slack API rate limits by reading directly from the local session
    registry database rather than paginating ``conversations.list``.
    Falls back to Slack API only if DB lookup fails.
    """
    import sqlite3

    try:
        from summon_claude.config import get_data_dir

        db_path = get_data_dir() / "registry.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                # Join sessions with projects to find the PM session for our project.
                # PROJECT_NAME may be truncated in channel_prefix/session_name, so
                # match by project name directly.
                rows = conn.execute(
                    "SELECT s.slack_channel_id FROM sessions s "
                    "JOIN projects p ON s.project_id = p.project_id "
                    "WHERE p.name = ? AND s.slack_channel_id IS NOT NULL "
                    "AND s.status IN ('active', 'pending_auth') "
                    "ORDER BY s.started_at DESC LIMIT 1",
                    (PROJECT_NAME,),
                ).fetchall()
                if rows and rows[0][0]:
                    return rows[0][0]
            finally:
                conn.close()
    except Exception as exc:
        click.echo(f"  DB lookup failed ({exc}), trying Slack API...", err=True)

    # Fallback: Slack API (slow, rate-limit prone)
    from slack_sdk import WebClient

    client = WebClient(token=bot_token)
    cursor = None
    while True:
        kwargs = {"types": "public_channel,private_channel", "limit": 200, "exclude_archived": True}
        if cursor:
            kwargs["cursor"] = cursor
        call_kwargs = dict(kwargs)
        resp = _slack_api_call_with_retry(lambda _kw=call_kwargs: client.conversations_list(**_kw))
        if resp is None:
            break
        for ch in resp.get("channels", []):
            if SESSION_NAME in ch.get("name", ""):
                return ch["id"]
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(3)
    return None


def get_team_id(bot_token: str) -> str:
    from slack_sdk import WebClient

    resp = WebClient(token=bot_token).auth_test()
    return str(resp["team_id"])


def archive_channel(bot_token: str, channel_id: str) -> None:
    from slack_sdk import WebClient

    try:
        WebClient(token=bot_token).conversations_archive(channel=channel_id)
        click.echo(f"  Archived channel: {channel_id}")
    except Exception as exc:
        click.echo(f"  WARNING: archive failed: {exc}", err=True)


# ---------------------------------------------------------------------------
# Playwright capture
# ---------------------------------------------------------------------------


def _crop_to_last_messages(page, dest: Path, *, padding: int = 16) -> None:
    """Screenshot only the last few visible messages in the channel.

    Uses JS to find the bounding rect of the last message containers,
    then clips the screenshot to that region.  Falls back to a bottom-
    of-viewport crop if the selectors don't match.
    """
    bbox = page.evaluate(
        """(padding) => {
        // Slack wraps each message in a [data-qa="virtual-list-item"] element
        const items = document.querySelectorAll('[data-qa="virtual-list-item"]');
        if (items.length === 0) return null;
        // Take the last 3 items (or fewer if less exist)
        const last = Array.from(items).slice(-3);
        let top = Infinity, bottom = 0, left = Infinity, right = 0;
        for (const el of last) {
            const r = el.getBoundingClientRect();
            if (r.top < top) top = r.top;
            if (r.bottom > bottom) bottom = r.bottom;
            if (r.left < left) left = r.left;
            if (r.right > right) right = r.right;
        }
        return {
            x: Math.max(0, left - padding),
            y: Math.max(0, top - padding),
            width: right - left + padding * 2,
            height: bottom - top + padding * 2,
        };
    }""",
        padding,
    )
    if bbox:
        page.screenshot(path=str(dest), clip=bbox)
    else:
        # Fallback: bottom slice of viewport
        click.echo("  WARNING: message elements not found, using viewport fallback", err=True)
        clip = {
            "x": 0,
            "y": VIEWPORT_HEIGHT - 300,
            "width": VIEWPORT_WIDTH,
            "height": 250,
        }
        page.screenshot(path=str(dest), clip=clip)


def _dismiss_overlays(page) -> None:
    """Click the first visible close/dismiss button, if any."""
    try:
        selectors = '[data-qa="explore_ai_dismiss"], button:has-text("Close"), [aria-label="Close"]'
        close_btn = page.locator(selectors)
        if close_btn.count() > 0:
            close_btn.first.click(timeout=3_000)
            click.echo("  Dismissed overlay")
            page.wait_for_timeout(1_000)
    except Exception:
        pass  # Overlay may not be present — safe to skip


def _inject_screenshot_css(page, css: str) -> None:
    """Inject a ``<style>`` tag with ``!important`` overrides into Slack's DOM.

    Using a ``<style>`` tag (rather than inline ``element.style``) lets us
    override Slack's own CSS rules via ``!important``.  The tag is replaced
    on each call so successive screenshots can use different CSS.
    """
    page.evaluate(
        """(css) => {
        document.getElementById('summon-screenshot-overrides')?.remove();
        const style = document.createElement('style');
        style.id = 'summon-screenshot-overrides';
        style.textContent = css;
        document.head.appendChild(style);
    }""",
        css,
    )
    page.wait_for_timeout(500)


# CSS that hides sidebar + thread panel, overlays the primary channel view
# at full viewport size via position:fixed (bypasses Slack's grid layout).
_CHANNEL_VIEW_CSS = """
    .p-tab_rail,
    .p-channel_sidebar {
        display: none !important;
    }
    .p-view_contents:not(.p-view_contents--primary):not(.p-view_contents--secondary) {
        display: none !important;
    }
    .p-view_contents--secondary,
    .p-flexpane {
        display: none !important;
    }
    .p-view_contents--primary {
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        width: 100vw !important;
        height: 100vh !important;
        z-index: 99999 !important;
    }
    /* Hide "N new messages" jump banner — multiple selectors for resilience
       since Slack changes these across versions */
    .c-message_list__unread_banner,
    .c-message_list__unread_banner__separator,
    .c-message_list__jump_to_unread,
    [data-qa="unread_banner"],
    [data-qa="unread_messages_separator_button"],
    [data-qa="channel_page_new_messages_banner"] {
        display: none !important;
    }
"""

# CSS that hides sidebar only (keeps both primary and secondary visible).
# Thread screenshots use element screenshot on the secondary view instead
# of viewport clipping, so we only need to remove the sidebar.
_THREAD_VIEW_CSS = """
    .p-tab_rail,
    .p-channel_sidebar {
        display: none !important;
    }
    .p-view_contents:not(.p-view_contents--primary):not(.p-view_contents--secondary) {
        display: none !important;
    }
"""


def create_browser_context(
    pw,
    *,
    state_file: str | None = None,
    cookie_value: str | None = None,
    headless: bool = True,
):
    """Create a Playwright browser context with Slack auth.

    Prefers ``state_file`` (full Playwright state from ``summon auth slack login``).
    Falls back to injecting a raw ``d`` cookie from ``cookie_value``.
    """
    browser = pw.chromium.launch(headless=headless)
    if state_file:
        context = browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            storage_state=state_file,
        )
    elif cookie_value:
        context = browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        )
        context.add_cookies(
            [
                {
                    "name": "d",
                    "value": cookie_value,
                    "domain": ".slack.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                },
            ]
        )
    else:
        msg = "Either state_file or cookie_value is required"
        raise ValueError(msg)
    return browser, context


def authenticate_via_slack(page, team_id: str, short_code: str) -> None:
    """Type `/summon CODE` in the Slack web UI to authenticate."""
    slack_url = f"https://app.slack.com/client/{team_id}"
    click.echo(f"  Authenticating in Slack: /summon {short_code}")
    click.echo(f"  Navigating to: {slack_url}")
    page.goto(slack_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(12_000)

    # Debug screenshot to tempdir (not docs dir) to see page state
    debug_path = Path(tempfile.gettempdir()) / "_debug_slack_auth_page.png"
    page.screenshot(path=str(debug_path), full_page=False)
    click.echo(f"  Debug screenshot saved: {debug_path}")

    _dismiss_overlays(page)

    # Find the message input and type the slash command
    # Use press_sequentially (not fill) so Slack's JS recognizes the / prefix as a slash command
    composer = page.locator('[data-qa="texty_input"]')
    composer.click(timeout=15_000)
    page.wait_for_timeout(500)
    composer.press_sequentially(f"/summon {short_code}", delay=50)
    page.wait_for_timeout(1_000)
    page.keyboard.press("Enter")
    click.echo("  Sent /summon command, waiting for auth...")
    page.wait_for_timeout(8_000)


def wait_for_session_channel(bot_token: str, timeout: int = 60) -> str:
    """Poll for the session channel to be created by the daemon."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        channel_id = find_session_channel(bot_token)
        if channel_id:
            click.echo(f"  Session channel found: {channel_id}")
            return channel_id
        time.sleep(10)
    raise RuntimeError(f"Session channel not found within {timeout}s")


def send_message_via_slack(page, message: str) -> None:
    """Type a message in the Slack message composer and send it."""
    click.echo(f"  Sending message: {message[:50]}...")
    composer = page.locator('[data-qa="texty_input"]')
    composer.click()
    page.wait_for_timeout(500)
    # Use fill for regular messages (not slash commands)
    composer.fill(message)
    page.wait_for_timeout(500)
    page.keyboard.press("Enter")


def wait_for_claude_response(bot_token: str, channel_id: str, timeout: int = 120) -> str | None:
    """Poll for Claude's response (a message from the bot after the user's message)."""
    from slack_sdk import WebClient

    client = WebClient(token=bot_token)
    deadline = time.time() + timeout

    # Get current message count
    resp = client.conversations_history(channel=channel_id, limit=50)
    baseline_count = len(resp.get("messages", []))
    click.echo(f"  Waiting for Claude response (baseline: {baseline_count} messages)...")

    while time.time() < deadline:
        time.sleep(5)
        resp = _slack_api_call_with_retry(
            lambda: client.conversations_history(channel=channel_id, limit=50)
        )
        if resp is None:
            continue
        messages = resp.get("messages", [])
        if len(messages) > baseline_count:
            # Look for a message that looks like a turn completion (has the checkered_flag footer)
            for msg in messages:
                if ":checkered_flag:" in msg.get("text", ""):
                    click.echo("  Claude response detected (turn footer found)")
                    return msg.get("ts")
        elapsed = int(time.time() - (deadline - timeout))
        if elapsed % 15 == 0 and elapsed > 0:
            click.echo(f"  Still waiting... ({elapsed}s)")

    click.echo("  WARNING: Timed out waiting for Claude response", err=True)
    return None


def _post_mock_permission(bot_token: str, channel_id: str) -> str | None:
    """Post a mock permission request message for screenshot purposes.

    Real permission requests can't be triggered reliably because Claude Code's
    SDK mode auto-approves most tools within the project directory. This posts
    a realistic-looking permission message using the same Block Kit format that
    summon's PermissionHandler uses.

    Returns the message ts for use in milestone screenshot capture.
    """
    from slack_sdk import WebClient

    client = WebClient(token=bot_token)

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "Permission required: Claude wants to run:\n"
                    "`Bash` — `./deploy.sh --env staging`"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": "permission_mock",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "permission_approve_mock",
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Approve for session",
                    },
                    "action_id": "permission_approve_session_mock",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": "permission_deny_mock",
                },
            ],
        },
    ]

    resp = client.chat_postMessage(
        channel=channel_id,
        text="Permission required: Bash — ./deploy.sh --env staging",
        blocks=blocks,
    )
    ts = resp.get("ts")
    click.echo(f"  Posted mock permission message: ts={ts}")
    return ts


def _post_mock_overflow_turn(bot_token: str, channel_id: str) -> str | None:
    """Post a mock turn header with overflow menu for screenshot purposes."""
    from slack_sdk import WebClient

    from summon_claude.sessions.response import _build_turn_header_blocks

    client = WebClient(token=bot_token)
    text = "\U0001f527 Turn 3: re: _Review the auth module_..."
    blocks = _build_turn_header_blocks(text)

    resp = client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
    ts = resp.get("ts")
    click.echo(f"  Posted mock overflow turn: ts={ts}")
    return ts


def _post_mock_select_menu(bot_token: str, channel_id: str) -> str | None:
    """Post a mock AskUserQuestion with >4 options (select menu) for screenshots."""
    from slack_sdk import WebClient

    from summon_claude.sessions.permissions import _build_ask_user_blocks

    client = WebClient(token=bot_token)
    questions = [
        {
            "question": "Which testing framework should I use for the new integration tests?",
            "header": "Testing Framework",
            "options": [
                {"label": "pytest", "description": "Standard Python test framework"},
                {"label": "unittest", "description": "Built-in Python testing"},
                {"label": "hypothesis", "description": "Property-based testing"},
                {"label": "ward", "description": "Modern test framework"},
                {"label": "nox", "description": "Flexible test automation"},
                {"label": "tox", "description": "Virtualenv-based test automation"},
            ],
        }
    ]
    blocks = _build_ask_user_blocks("mock-req-001", questions)

    resp = client.chat_postMessage(
        channel=channel_id,
        text="Claude has a question for you",
        blocks=blocks,
    )
    ts = resp.get("ts")
    click.echo(f"  Posted mock select menu: ts={ts}")
    return ts


def wait_for_help_response(bot_token: str, channel_id: str, timeout: int = 30) -> str | None:
    """Poll for the !help thread reply using conversations_replies.

    The !help response is posted as a thread reply to the user's !help message,
    NOT as a top-level channel message. So conversations_history won't see the
    response — we must use conversations_replies.

    Returns the !help message ts if a reply was detected, None on timeout.
    """
    from slack_sdk import WebClient

    client = WebClient(token=bot_token)
    # Each phase gets its own deadline to prevent starvation — if finding
    # the message is slow, the reply poll still gets its full budget.
    phase_timeout = timeout // 2
    find_deadline = time.time() + phase_timeout

    # Find the !help message in conversations_history (it IS a top-level message)
    help_ts = None
    while time.time() < find_deadline:
        resp = client.conversations_history(channel=channel_id, limit=10)
        for msg in resp.get("messages", []):
            if msg.get("text", "").strip() == "!help":
                help_ts = msg["ts"]
                break
        if help_ts:
            break
        time.sleep(2)

    if not help_ts:
        click.echo("  WARNING: Could not find !help message in channel", err=True)
        return None

    click.echo(f"  Found !help message: ts={help_ts}")

    # Poll conversations_replies for the daemon's thread reply
    reply_deadline = time.time() + phase_timeout
    while time.time() < reply_deadline:
        resp = client.conversations_replies(channel=channel_id, ts=help_ts)
        messages = resp.get("messages", [])
        # First message is the parent (!help itself), replies follow
        if len(messages) > 1:
            click.echo("  !help response detected in thread")
            return help_ts
        time.sleep(2)

    click.echo("  WARNING: Timed out waiting for !help thread reply", err=True)
    return help_ts  # Return ts anyway so we can still try to screenshot


def _make_snap(page, output_dir: Path, captured: list[str]):
    """Create a reusable ``snap()`` helper bound to the given page and output dir.

    Returns ``(snap, nav, clip_region)`` so callers can capture screenshots
    at any point during the session flow — not just at the end.
    """
    clip_region = {
        "x": 0,
        "y": TOP_NAV_HEIGHT,
        "width": VIEWPORT_WIDTH,
        "height": VIEWPORT_HEIGHT - TOP_NAV_HEIGHT - SANDBOX_BANNER_HEIGHT,
    }

    def snap(name: str, *, thread_view: bool = False) -> None:
        """Capture a clean screenshot.

        When *thread_view* is True the sidebar is hidden and the thread
        panel is captured via element screenshot (natural size).  Otherwise
        sidebar and thread panel are both hidden and the primary channel
        view is overlaid at full viewport width.
        """
        dest = output_dir / name
        if thread_view:
            _inject_screenshot_css(page, _THREAD_VIEW_CSS)
            secondary = page.locator(".p-view_contents--secondary")
            if secondary.count() > 0 and secondary.first.is_visible():
                secondary.first.screenshot(path=str(dest))
            else:
                click.echo(
                    f"  WARNING: thread panel not found, falling back to channel view for {name}",
                    err=True,
                )
                _inject_screenshot_css(page, _CHANNEL_VIEW_CSS)
                page.screenshot(path=str(dest), clip=clip_region)
        else:
            _inject_screenshot_css(page, _CHANNEL_VIEW_CSS)
            page.screenshot(path=str(dest), clip=clip_region)
        click.echo(f"  captured: {dest}")
        captured.append(name)

    def nav(url: str, wait_ms: int = 8_000) -> None:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(wait_ms)

    return snap, nav, clip_region


# ---------------------------------------------------------------------------
# Terminal output capture — registry-based CLI output capture system
# ---------------------------------------------------------------------------

TERMINAL_SESSION_NAME = "docs-terminal"


@dataclass
class CaptureSpec:
    """Specification for capturing terminal output from a CLI command.

    Each spec maps a CLI command to a marker in a markdown file.  The runner
    executes the command, optionally post-processes the output, and injects
    the result between ``<!-- terminal:MARKER -->`` / ``<!-- /terminal:MARKER -->``
    comment pairs in the target file.
    """

    marker: str  # marker name in <!-- terminal:MARKER -->
    md_file: str  # relative path from repo root
    command: list[str]  # CLI command args (prefixed with "uv run" automatically)
    fence: str = "text"  # code fence language / attributes
    timeout: int = 15  # capture timeout in seconds
    capture_fn: Callable[[], str] | None = None  # custom capture (overrides command)
    post_process: Callable[[str], str] | None = None  # transform captured output
    extra_md: str = ""  # markdown appended after code fence, before closing marker


# -- Post-processors -------------------------------------------------------


def _sanitize_paths(text: str) -> str:
    """Replace the user's home directory with ``~`` for portability."""
    return text.replace(str(Path.home()), "~")


def _add_start_annotations(banner: str) -> str:
    """Add Material for MkDocs annotations to the ``summon start`` banner."""
    out = []
    for line in banner.split("\n"):
        if "SUMMON CODE:" in line:
            out.append(f"{line}  # (1)")
        elif "Expires in" in line:
            out.append(f"{line}  # (2)")
        else:
            out.append(line)
    return "\n".join(out)


# -- Custom capture functions -----------------------------------------------


def _read_banner_from_proc(proc: subprocess.Popen, deadline: float) -> list[str]:
    """Read stdout from *proc* until the closing ``====`` border.

    Returns the captured banner lines (including borders).  The caller
    is responsible for terminating *proc* and running cleanup commands.
    """
    lines: list[str] = []
    border_re = re.compile(r"^={10,}$")
    in_banner = False

    if proc.stdout is None:  # guaranteed non-None by stdout=PIPE
        raise RuntimeError("proc.stdout is None despite stdout=PIPE")
    stdout = proc.stdout

    while time.time() < deadline:
        line = stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
            continue

        stripped = line.rstrip()
        if not stripped:
            if in_banner:
                lines.append(stripped)
            continue

        if border_re.match(stripped):
            lines.append(stripped)
            if in_banner:
                break  # Closing border — done
            in_banner = True
        elif in_banner:
            lines.append(stripped)

    return lines


def _terminate_proc(proc: subprocess.Popen) -> None:
    """Terminate a subprocess, escalating to kill on timeout."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _capture_summon_start_banner() -> str:
    """Run ``summon start`` and capture just the auth banner.

    Starts a real session, reads stdout until the closing ``====`` border,
    then terminates and cleans up.
    """
    env = _make_env()

    click.echo("    Starting summon session for banner capture...")
    proc = subprocess.Popen(
        ["uv", "run", "summon", "start", "--name", TERMINAL_SESSION_NAME],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    try:
        lines = _read_banner_from_proc(proc, time.time() + 30)
    finally:
        _terminate_proc(proc)
        try:
            subprocess.run(
                ["uv", "run", "summon", "stop", TERMINAL_SESSION_NAME],
                capture_output=True,
                timeout=15,
                env=env,
            )
        except Exception:
            pass

    if len(lines) < 3:
        raise RuntimeError(f"Failed to capture auth banner (got {len(lines)} lines): {lines}")

    click.echo(f"    Captured {len(lines)} lines of banner output")
    return "\n".join(lines)


def _capture_project_up_banner() -> str:
    """Run summon project up and capture just the auth banner."""
    env = _make_env()

    # Ensure project exists
    subprocess.run(
        ["uv", "run", "summon", "project", "add", TERMINAL_SESSION_NAME, "."],
        capture_output=True,
        env=env,
    )

    click.echo("    Starting project for banner capture...")
    proc = subprocess.Popen(
        ["uv", "run", "summon", "project", "up"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    try:
        lines = _read_banner_from_proc(proc, time.time() + 60)
    finally:
        _terminate_proc(proc)
        try:
            subprocess.run(
                ["uv", "run", "summon", "project", "down"],
                capture_output=True,
                timeout=15,
                env=env,
            )
            subprocess.run(
                ["uv", "run", "summon", "project", "remove", TERMINAL_SESSION_NAME, "--yes"],
                capture_output=True,
                timeout=15,
                env=env,
            )
        except Exception:
            pass

    if len(lines) < 3:
        raise RuntimeError(f"Failed to capture project up banner (got {len(lines)} lines): {lines}")

    click.echo(f"    Captured {len(lines)} lines of banner output")
    return "\n".join(lines)


# -- Capture registry -------------------------------------------------------
#
# Add new entries here to auto-capture more CLI commands.  The runner iterates
# this list and skips any captures whose commands fail (missing config, etc.).

_START_EXTRA_MD = (
    "\n"
    "1. This is a one-time code. Type it exactly as shown in any Slack channel.\n"
    "2. Codes expire after 5 minutes. Run `summon start` again if it expires."
)

_PROJECT_UP_EXTRA_MD = (
    "\n"
    "1. This is a one-time code. Type it exactly as shown in any Slack channel.\n"
    "2. Codes expire after 5 minutes. Run `summon project up` again if it expires."
)

CAPTURES: list[CaptureSpec] = [
    # -- Getting started ----------------------------------------------------
    CaptureSpec(
        marker="summon-version",
        md_file="docs/getting-started/installation.md",
        command=["summon", "version"],
        post_process=_sanitize_paths,
    ),
    # -- Guide: sessions ----------------------------------------------------
    CaptureSpec(
        marker="summon-version",
        md_file="docs/guide/sessions.md",
        command=["summon", "version"],
        post_process=_sanitize_paths,
    ),
    CaptureSpec(
        marker="summon-start",
        md_file="docs/guide/sessions.md",
        command=[],
        fence="{ .text .annotate }",
        timeout=30,
        capture_fn=_capture_summon_start_banner,
        post_process=_add_start_annotations,
        extra_md=_START_EXTRA_MD,
    ),
    CaptureSpec(
        marker="project-up",
        md_file="docs/getting-started/quickstart.md",
        command=[],
        fence="{ .text .annotate }",
        timeout=60,
        capture_fn=_capture_project_up_banner,
        post_process=_add_start_annotations,
        extra_md=_PROJECT_UP_EXTRA_MD,
    ),
    # -- Guide: configuration -----------------------------------------------
    CaptureSpec(
        marker="config-check",
        md_file="docs/getting-started/configuration.md",
        command=["summon", "config", "check"],
        timeout=30,
        post_process=_sanitize_paths,
    ),
]


# ---------------------------------------------------------------------------
# Schema extraction — queries the live DB for current CREATE TABLE statements
# ---------------------------------------------------------------------------

_DATABASE_MD = "docs/concepts/database.md"

_SCHEMA_TABLES = [
    "sessions",
    "channels",
    "pending_auth_tokens",
    "spawn_tokens",
    "projects",
    "workflow_defaults",
    "session_tasks",
    "audit_log",
    "schema_version",
]


def _extract_table_sql(table_name: str) -> str:
    """Extract the CREATE TABLE statement for a table from the live DB."""
    import sqlite3

    from summon_claude.config import get_data_dir

    db_path = get_data_dir() / "registry.db"
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if not row or not row[0]:
            return f"-- table '{table_name}' not found"
        return str(row[0])
    finally:
        conn.close()


def _extract_schema_version() -> str:
    """Get the current schema version from the live DB."""
    import sqlite3

    from summon_claude.config import get_data_dir

    db_path = get_data_dir() / "registry.db"
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        return str(row[0]) if row else "unknown"
    finally:
        conn.close()


def _extract_migration_history() -> str:
    """Extract migration history from source code."""
    from summon_claude.sessions.migrations import _MIGRATIONS, CURRENT_SCHEMA_VERSION

    lines = []
    for from_ver in sorted(_MIGRATIONS.keys()):
        to_ver = from_ver + 1
        fn = _MIGRATIONS[from_ver]
        if fn is None:
            desc = "Baseline (no-op)"
        else:
            desc = (fn.__doc__ or "").strip().split("\n")[0] or "(undocumented)"
        lines.append(f"| {from_ver} → {to_ver} | {desc} |")
    lines.append(f"\nCurrent schema version: **{CURRENT_SCHEMA_VERSION}**")
    return "\n".join(lines)


def run_schema_section(dry_run: bool = False) -> bool:
    """Extract live schema from DB and inject into database.md.

    Returns True if at least one schema was injected, False if all failed.
    """
    click.echo("\n[schema] Extracting live schema from database...")

    if dry_run:
        for table in _SCHEMA_TABLES:
            click.echo(f"  (schema) {table} → {_DATABASE_MD}")
        click.echo(f"  (schema) migration-history → {_DATABASE_MD}")
        click.echo(f"  (schema) schema-version → {_DATABASE_MD}")
        return True

    succeeded = 0
    failed = 0
    md_path = Path(_DATABASE_MD)

    # Extract each table's CREATE TABLE
    for table in _SCHEMA_TABLES:
        click.echo(f"  Extracting: {table}...")
        try:
            sql = _extract_table_sql(table)
        except Exception as exc:
            click.echo(f"    WARNING: failed to extract {table}: {exc}", err=True)
            failed += 1
            continue

        if _inject_terminal_block(md_path, f"schema-{table}", sql, fence="sql", prefix="schema"):
            click.echo(f"    → injected into {_DATABASE_MD}")
            succeeded += 1
        else:
            click.echo(
                f"    WARNING: no <!-- schema:schema-{table} --> marker in {_DATABASE_MD}",
                err=True,
            )
            failed += 1

    # Extract migration history
    click.echo("  Extracting: migration-history...")
    try:
        history = _extract_migration_history()
        if _inject_terminal_block(md_path, "migration-history", history, fence="", prefix="schema"):
            click.echo(f"    → injected into {_DATABASE_MD}")
            succeeded += 1
        else:
            failed += 1
    except Exception as exc:
        click.echo(f"    WARNING: failed to extract migration history: {exc}", err=True)
        failed += 1

    # Extract schema version
    click.echo("  Extracting: schema-version...")
    try:
        version = _extract_schema_version()
        if _inject_terminal_block(md_path, "schema-version", version, fence="", prefix="schema"):
            click.echo(f"    → injected into {_DATABASE_MD}")
            succeeded += 1
        else:
            failed += 1
    except Exception as exc:
        click.echo(f"    WARNING: failed to extract schema version: {exc}", err=True)
        failed += 1

    click.echo(f"\n  Done: {succeeded} schemas extracted, {failed} skipped/failed.")
    return succeeded > 0


# ---------------------------------------------------------------------------
# Prompt extraction — delegates to scripts/generate_prompt_docs.py
# ---------------------------------------------------------------------------


def run_prompts_section(dry_run: bool = False) -> bool:
    """Regenerate prompts in docs via the dedicated generation script.

    Delegates to ``scripts/generate_prompt_docs.py`` which is the single
    source of truth for prompt extraction and marker injection.

    Returns True on success, False on failure.
    """
    from generate_prompt_docs import generate, get_source_prompts

    prompts = get_source_prompts()
    doc_path = Path("docs/reference/prompts.md")

    click.echo(f"\n[prompts] Regenerating {doc_path} ({len(prompts)} prompts)...")

    if dry_run:
        for marker in sorted(prompts):
            click.echo(f"  (prompt) {marker} → {doc_path}")
        return True

    content = doc_path.read_text(encoding="utf-8")
    updated = generate(content, prompts)
    doc_path.write_text(updated, encoding="utf-8")
    click.echo(f"  → {len(prompts)} prompts injected into {doc_path}")
    return True


# -- Runner -----------------------------------------------------------------


def _make_env() -> dict[str, str]:
    """Build a subprocess environment with CLAUDECODE and test credentials unset."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("SUMMON_TEST_SLACK_BOT_TOKEN", None)
    env.pop("SUMMON_TEST_SLACK_COOKIE", None)
    return env


# Cache for custom capture functions — avoids running expensive captures
# (like summon start) multiple times when the same capture_fn is shared
# across registry entries.
_capture_cache: dict[Callable[[], str], str] = {}


def _run_capture(spec: CaptureSpec) -> str | None:
    """Execute a single capture spec and return its output, or None on failure."""
    if spec.capture_fn:
        if spec.capture_fn in _capture_cache:
            click.echo("    (cached)")
            return _capture_cache[spec.capture_fn]
        try:
            result = spec.capture_fn()
            _capture_cache[spec.capture_fn] = result
            return result
        except Exception as exc:
            click.echo(f"    WARNING: {spec.marker} custom capture failed: {exc}", err=True)
            return None

    try:
        result = subprocess.run(
            ["uv", "run", *spec.command],
            capture_output=True,
            text=True,
            timeout=spec.timeout,
            env=_make_env(),
        )
    except subprocess.TimeoutExpired:
        click.echo(f"    WARNING: {spec.marker} timed out after {spec.timeout}s", err=True)
        return None

    if result.returncode != 0:
        stderr_preview = result.stderr.strip()[:200]
        click.echo(
            f"    WARNING: {' '.join(spec.command)} exited {result.returncode}: {stderr_preview}",
            err=True,
        )
        return None

    return result.stdout.strip()


def _validate_content(content: str, marker: str, *, prefix: str = "terminal") -> str | None:
    """Check captured content for patterns that would corrupt the markdown.

    Returns an error message if the content is unsafe, or None if OK.
    """
    if f"<!-- {prefix}:{marker} -->" in content:
        return "content contains the opening marker comment"
    if f"<!-- /{prefix}:{marker} -->" in content:
        return "content contains the closing marker comment"
    if "```" in content:
        return "content contains triple backticks (would break fences)"
    return None


def _inject_terminal_block(  # noqa: PLR0913
    md_path: Path,
    marker: str,
    content: str,
    fence: str = "text",
    extra_md: str = "",
    *,
    prefix: str = "terminal",
) -> bool:
    """Replace content between ``<!-- {prefix}:MARKER -->`` markers."""
    md_text = md_path.read_text()
    esc = re.escape(marker)
    pattern = re.compile(
        rf"(<!-- {prefix}:{esc} -->)\n(.*?\n)?(<!-- /{prefix}:{esc} -->)",
        re.DOTALL,
    )

    block = f"```{fence}\n{content}\n```"
    if extra_md:
        block += extra_md

    # Use a function replacement to avoid backreference interpretation in content
    def _replacer(m: re.Match[str]) -> str:
        return f"{m.group(1)}\n{block}\n{m.group(3)}"

    new_text, count = pattern.subn(_replacer, md_text)
    if count == 0:
        return False

    md_path.write_text(new_text)
    return True


def run_terminal_section(dry_run: bool = False) -> bool:
    """Run all registered captures and inject results into docs.

    Returns True if at least one capture succeeded, False if all failed.
    """
    click.echo("\n[terminal] Capturing CLI terminal output...")

    if dry_run:
        for spec in CAPTURES:
            cmd_str = " ".join(spec.command) if spec.command else "(custom)"
            click.echo(f"  (capture) {cmd_str} → {spec.md_file}#{spec.marker}")
        return True

    succeeded = 0
    failed = 0

    for spec in CAPTURES:
        cmd_str = " ".join(spec.command) if spec.command else "(custom)"
        click.echo(f"  Capturing: {cmd_str} → {spec.marker}...")

        output = _run_capture(spec)
        if output is None:
            failed += 1
            continue

        if spec.post_process:
            output = spec.post_process(output)

        # Validate content won't corrupt the markdown
        error = _validate_content(output, spec.marker)
        if error:
            click.echo(f"    WARNING: skipping {spec.marker}: {error}", err=True)
            failed += 1
            continue

        md_path = Path(spec.md_file)
        if _inject_terminal_block(md_path, spec.marker, output, spec.fence, spec.extra_md):
            click.echo(f"    → injected into {spec.md_file}")
            succeeded += 1
        else:
            click.echo(
                f"    WARNING: no <!-- terminal:{spec.marker} --> marker in {spec.md_file}",
                err=True,
            )
            failed += 1

    click.echo(f"\n  Done: {succeeded} captured, {failed} skipped/failed.")
    return succeeded > 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command()
@click.option(
    "--output",
    "output_dir",
    default=str(DEFAULT_OUTPUT_DIR),
    show_default=True,
    help="Output directory for screenshots.",
    type=click.Path(file_okay=False, path_type=Path),
)
@click.option(
    "--section",
    default=None,
    type=click.Choice(["slack-setup", "session-ux", "terminal", "prompts", "schema"]),
    help="Capture only a specific section.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="List planned screenshots without capturing.",
)
@click.option(
    "--keep-channel",
    is_flag=True,
    default=False,
    help="Do not archive the session channel after capture.",
)
@click.option(
    "--message",
    default="Review the README.md and suggest improvements",
    help="Message to send to Claude for generating screenshot content.",
)
def main(
    output_dir: Path,
    section: str | None,
    dry_run: bool,
    keep_channel: bool,
    message: str,
) -> None:
    """Generate documentation screenshots using a real summon session.

    Starts a real summon session, authenticates via the Slack web UI,
    sends a message to Claude, waits for the response, then captures
    screenshots of the channel, turn threads, and permission UI.

    This is a LOCAL developer tool — requires Claude CLI, summon config,
    and Slack browser credentials. Cannot run in CI.

    Requires: SUMMON_TEST_SLACK_BOT_TOKEN (from .env or environment).
    """
    # Load .env so credentials work without manual export
    from dotenv import load_dotenv

    load_dotenv()

    output_dir.mkdir(parents=True, exist_ok=True)
    all_sections = ["slack-setup", "session-ux", "terminal", "prompts", "schema"]
    sections = [section] if section else all_sections

    if dry_run:
        for sec in sections:
            if sec == "terminal":
                run_terminal_section(dry_run=True)
                continue
            if sec == "prompts":
                run_prompts_section(dry_run=True)
                continue
            if sec == "schema":
                run_schema_section(dry_run=True)
                continue
            items = (
                MANUAL_SCREENSHOTS
                if sec == "slack-setup"
                else [
                    {"name": "quickstart-slack-auth.png", "description": "Auth/welcome message"},
                    {
                        "name": "quickstart-first-message.png",
                        "description": "First exchange (turn thread)",
                    },
                    {"name": "quickstart-help.png", "description": "!help output"},
                    {
                        "name": "quickstart-permission-request.png",
                        "description": "Permission buttons",
                    },
                    {"name": "permissions-approval.png", "description": "Permission approval"},
                    {
                        "name": "canvas-channel-tab.png",
                        "description": "Canvas tab with session status",
                    },
                    {
                        "name": "interactivity-overflow-menu.png",
                        "description": "Turn header with overflow menu (stop/copy/cost)",
                    },
                    {
                        "name": "interactivity-select-menu.png",
                        "description": "AskUserQuestion with select dropdown (>4 options)",
                    },
                ]
            )
            tag = "manual" if sec == "slack-setup" else "e2e (real session)"
            click.echo(f"\n[{sec}]")
            for shot in items:
                click.echo(f"  ({tag}) {output_dir / shot['name']}")
                click.echo(f"         {shot['description']}")
        return

    # Terminal capture (no Slack/Playwright prereqs needed)
    if "terminal" in sections and not run_terminal_section() and sections == ["terminal"]:
        raise SystemExit(1)

    # Prompt extraction (no prereqs needed)
    if "prompts" in sections and not run_prompts_section() and sections == ["prompts"]:
        raise SystemExit(1)

    # Schema extraction (needs local DB)
    if "schema" in sections and not run_schema_section() and sections == ["schema"]:
        raise SystemExit(1)

    # Validate manual screenshots
    if "slack-setup" in sections:
        click.echo("\n[slack-setup] Validating manual screenshots...")
        missing = [s["name"] for s in MANUAL_SCREENSHOTS if not (output_dir / s["name"]).exists()]
        if missing:
            click.echo(f"  WARNING: {len(missing)} manual screenshots missing:", err=True)
            for name in missing:
                click.echo(f"    {name}", err=True)
        else:
            click.echo(f"  All {len(MANUAL_SCREENSHOTS)} manual screenshots present.")

    if "session-ux" not in sections:
        return

    # Check prerequisites
    click.echo("\n[session-ux] End-to-end screenshot capture (real summon session)...")
    bot_token = os.environ.get("SUMMON_TEST_SLACK_BOT_TOKEN")
    if not bot_token:
        click.echo("  Skipping: missing SUMMON_TEST_SLACK_BOT_TOKEN", err=True)
        return

    # Browser auth: try saved Playwright state from `summon auth slack login`,
    # fall back to raw SUMMON_TEST_SLACK_COOKIE env var.
    slack_state_file = _find_slack_auth_state()
    cookie_value = os.environ.get("SUMMON_TEST_SLACK_COOKIE")
    if not slack_state_file and not cookie_value:
        click.echo(
            "  Skipping: no Slack browser auth found.\n"
            "  Run `summon auth slack login <workspace-url>` or set SUMMON_TEST_SLACK_COOKIE.",
            err=True,
        )
        return
    if slack_state_file:
        click.echo(f"  Using saved Slack auth: {slack_state_file}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        click.echo("  Skipping: playwright not installed.", err=True)
        return

    # Verify summon is installed
    try:
        subprocess.run(["summon", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        click.echo("  Skipping: `summon` CLI not found on PATH.", err=True)
        return

    team_id = get_team_id(bot_token)
    proc = None
    channel_id = None

    try:
        # 1. Start project session (PM-based)
        proc, short_code = start_project_session()

        # 2. Authenticate via Playwright
        with sync_playwright() as pw:
            browser, context = create_browser_context(
                pw, state_file=slack_state_file, cookie_value=cookie_value
            )
            page = context.new_page()

            authenticate_via_slack(page, team_id, short_code)

            # Set up reusable screenshot helpers
            captured: list[str] = []
            snap, nav, clip_region = _make_snap(page, output_dir, captured)

            # --- Milestone 1: Auth screenshot (/summon command + bot response)
            #     Capture immediately after authenticate_via_slack returns,
            #     while the page still shows the channel where /summon was typed.
            #     Crop tightly to just the last messages (the /summon ephemeral
            #     response) to exclude workspace onboarding clutter above.
            click.echo("  Capturing auth screenshot (slash command + bot response)...")
            _dismiss_overlays(page)
            _inject_screenshot_css(page, _CHANNEL_VIEW_CSS)
            # Scroll to bottom so the auth response is visible
            page.evaluate(_SCROLL_TO_BOTTOM_JS)
            page.wait_for_timeout(2_000)
            # Find the last few messages and crop to their bounding box
            auth_dest = output_dir / "quickstart-slack-auth.png"
            _crop_to_last_messages(page, auth_dest)
            click.echo(f"  captured: {auth_dest}")
            captured.append("quickstart-slack-auth.png")

            # 3. Wait for session channel
            channel_id = wait_for_session_channel(bot_token, timeout=180)
            channel_url = f"https://app.slack.com/client/{team_id}/{channel_id}"

            # 4. Navigate to session channel
            nav(channel_url)
            _dismiss_overlays(page)

            # Debug screenshot to tempdir (not docs dir) to verify channel loaded
            debug_path = Path(tempfile.gettempdir()) / "_debug_slack_channel_page.png"
            page.screenshot(path=str(debug_path), full_page=False)
            click.echo(f"  Debug screenshot saved: {debug_path}")

            # 5. Send task message
            send_message_via_slack(page, message)

            # 6. Wait for Claude to respond
            wait_for_claude_response(bot_token, channel_id, timeout=180)

            # --- Milestone 2: First message screenshot (welcome + task + response)
            click.echo("  Capturing first message exchange...")
            nav(channel_url, wait_ms=3_000)
            _redact_paths(page)
            page.evaluate(_SCROLL_TO_BOTTOM_JS)
            page.wait_for_timeout(2_000)
            snap("quickstart-first-message.png")

            # 7. Post a mock permission request for screenshot purposes.
            #    Real permission requests can't be triggered reliably because
            #    Claude Code SDK mode auto-approves most tools within the project.
            perm_ts = _post_mock_permission(bot_token, channel_id)

            # --- Milestone 3: Permission request screenshot
            if perm_ts:
                click.echo("  Capturing permission request screenshot...")
                page.wait_for_timeout(3_000)
                nav(channel_url, wait_ms=3_000)
                _redact_paths(page)
                page.evaluate(_SCROLL_TO_BOTTOM_JS)
                page.wait_for_timeout(1_000)
                snap("quickstart-permission-request.png")
                # permissions-approval.png shows the same view — copy instead of re-capturing
                shutil.copy2(
                    output_dir / "quickstart-permission-request.png",
                    output_dir / "permissions-approval.png",
                )
                click.echo(f"  copied: {output_dir / 'permissions-approval.png'}")
                captured.append("permissions-approval.png")
            else:
                click.echo("  No permission ts — skipping permission screenshots")

            # 8. Send !help and wait for thread reply
            send_message_via_slack(page, "!help")
            help_ts = wait_for_help_response(bot_token, channel_id, timeout=30)

            # --- Milestone 4: !help thread screenshot
            if help_ts:
                help_thread_url = f"{channel_url}/thread/{channel_id}-{help_ts}"
                click.echo(f"  Capturing !help thread: {help_thread_url}")
                try:
                    nav(help_thread_url, wait_ms=5_000)
                    snap("quickstart-help.png", thread_view=True)
                except Exception as exc:
                    click.echo(f"  WARNING: thread nav failed ({exc}), falling back", err=True)
                    nav(channel_url, wait_ms=3_000)
                    _redact_paths(page)
                    page.evaluate(_SCROLL_TO_BOTTOM_JS)
                    page.wait_for_timeout(2_000)
                    snap("quickstart-help.png")
            else:
                click.echo("  No !help thread ts — falling back to channel view")
                nav(channel_url, wait_ms=3_000)
                _redact_paths(page)
                page.evaluate(_SCROLL_TO_BOTTOM_JS)
                page.wait_for_timeout(2_000)
                snap("quickstart-help.png")

            # --- Milestone 5: Canvas screenshots
            click.echo("  Attempting canvas capture...")
            try:
                nav(channel_url, wait_ms=3_000)
                canvas_tab = page.locator('button[data-qa="canvas"][role="tab"]')
                canvas_tab.wait_for(state="visible", timeout=15_000)
                canvas_tab.click(timeout=5_000)
                page.wait_for_timeout(3_000)
                _redact_paths(page)
                snap("canvas-channel-tab.png")
            except Exception as exc:
                click.echo(f"  Canvas capture failed ({type(exc).__name__}): {exc}", err=True)

            # --- Milestone 6: Overflow menu on turn header (expanded)
            #     Click the "..." button to show the popup menu with options.
            #     Use snap() (full viewport) instead of _crop_to_last_messages
            #     because the popup renders as a portal outside message bounds.
            click.echo("  Capturing overflow menu screenshot...")
            overflow_ts = _post_mock_overflow_turn(bot_token, channel_id)
            if overflow_ts:
                page.wait_for_timeout(2_000)
                nav(channel_url, wait_ms=3_000)
                _redact_paths(page)
                page.evaluate(_SCROLL_TO_BOTTOM_JS)
                page.wait_for_timeout(1_000)
                # Use minimal CSS — _CHANNEL_VIEW_CSS repositions with position:fixed
                # which shifts the overflow button's anchor and breaks the popover.
                # Only hide sidebar; keep the primary view in natural layout.
                _inject_screenshot_css(page, _THREAD_VIEW_CSS)
                page.wait_for_timeout(500)
                # Click the overflow button to open the menu.
                # Slack renders Block Kit overflow as:
                #   data-qa="block_kit_overflow_element_button"
                #   aria-label="More options"
                #   aria-haspopup="menu"
                try:
                    btn = page.locator('[data-qa="block_kit_overflow_element_button"]').last
                    if btn.count() > 0:
                        btn.click(timeout=5_000)
                        page.wait_for_timeout(1_500)
                        click.echo("    Opened overflow menu")
                    else:
                        click.echo("  WARNING: could not find overflow button", err=True)
                except Exception as exc:
                    click.echo(f"  WARNING: could not click overflow button: {exc}", err=True)
                # Direct screenshot (no snap — CSS already injected, popover is open)
                overflow_dest = output_dir / "interactivity-overflow-menu.png"
                page.screenshot(path=str(overflow_dest), clip=clip_region)
                click.echo(f"  captured: {overflow_dest}")
                captured.append("interactivity-overflow-menu.png")
                # Dismiss the menu
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)

            # --- Milestone 7: AskUserQuestion with select menu (>4 options)
            click.echo("  Capturing select menu screenshot...")
            select_ts = _post_mock_select_menu(bot_token, channel_id)
            if select_ts:
                page.wait_for_timeout(2_000)
                try:
                    nav(channel_url, wait_ms=3_000)
                except Exception as exc:
                    click.echo(f"  WARNING: select menu nav failed ({exc})", err=True)
                _redact_paths(page)
                page.evaluate(_SCROLL_TO_BOTTOM_JS)
                page.wait_for_timeout(1_000)
                _inject_screenshot_css(page, _CHANNEL_VIEW_CSS)
                select_dest = output_dir / "interactivity-select-menu.png"
                _crop_to_last_messages(page, select_dest, padding=24)
                click.echo(f"  captured: {select_dest}")
                captured.append("interactivity-select-menu.png")

            # Clean up debug screenshots from previous runs
            for debug_file in output_dir.glob("_debug_*.png"):
                debug_file.unlink()
                click.echo(f"  Removed debug artifact: {debug_file.name}")

            click.echo(f"\n  Done: {len(captured)} screenshots captured.")

            context.close()
            browser.close()

    finally:
        if proc:
            stop_project_session(proc)
        if channel_id and not keep_channel:
            archive_channel(bot_token, channel_id)
        elif channel_id:
            click.echo(f"  Keeping channel: {channel_id} (--keep-channel)")


if __name__ == "__main__":
    main()  # type: ignore[call-arg]  # Click provides args from CLI
