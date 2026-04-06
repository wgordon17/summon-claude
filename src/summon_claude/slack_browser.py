"""Playwright-based Slack WebSocket monitor for external workspace observation.

Captures DMs, @mentions, and monitored channel messages from external Slack
workspaces via browser automation. Auth state is stored at 0o600 permissions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from summon_claude.config import get_browser_auth_dir

logger = logging.getLogger(__name__)

_QUEUE_MAX = 10_000
# Maximum wait for login confirmation during interactive auth (seconds)
_AUTH_TIMEOUT_S = 300
# Slack's SPA client URL prefix — Enterprise Grid workspaces need this
_APP_SLACK_CLIENT = "https://app.slack.com/client/"


def _slugify(url: str) -> str:
    """Convert a workspace URL to a safe filesystem identifier.

    Examples::

        _slugify("https://myteam.slack.com") -> "myteam_slack_com"
        _slugify("https://acme-corp.slack.com/") -> "acme-corp_slack_com"
    """
    # Strip scheme and trailing slashes
    slug = re.sub(r"^https?://", "", url).rstrip("/")
    # Replace non-alphanumeric-or-hyphen chars with underscores
    slug = re.sub(r"[^\w\-]", "_", slug)
    # Collapse repeated underscores
    slug = re.sub(r"_+", "_", slug)
    # Remove leading/trailing underscores
    return slug.strip("_")


@dataclass
class SlackMessage:
    """A sanitised message captured from an external Slack workspace."""

    channel: str
    user: str
    text: str
    ts: str
    workspace: str
    is_dm: bool = False
    is_mention: bool = False


def _resolve_client_url(workspace_url: str, state_file: Path) -> str:
    """Resolve the Slack SPA client URL for headless navigation.

    Enterprise Grid workspaces serve a workspace picker at their enterprise
    URL (e.g. ``gtest.enterprise.slack.com``). The actual SPA lives at
    ``app.slack.com/client/{TEAM_ID}``. This function extracts the team ID
    from the saved Playwright state's localStorage and returns the direct
    client URL.

    Falls back to the original ``workspace_url`` if no team data is found.
    """
    if not state_file.is_file():
        return workspace_url

    try:
        state = json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return workspace_url

    # Extract team IDs from localConfig_v2 in app.slack.com localStorage
    for origin in state.get("origins", []):
        if "app.slack.com" not in origin.get("origin", ""):
            continue
        for item in origin.get("localStorage", []):
            if item.get("name") != "localConfig_v2":
                continue
            try:
                lc = json.loads(item.get("value", "{}"))
            except (json.JSONDecodeError, TypeError):
                break
            teams = lc.get("teams", {})
            if not teams:
                break

            # Prefer the team whose URL matches the workspace
            norm_ws = workspace_url.rstrip("/").lower()
            for team_id, info in teams.items():
                team_url = (info.get("url") or "").rstrip("/").lower()
                if team_url == norm_ws:
                    client_url = f"{_APP_SLACK_CLIENT}{team_id}"
                    logger.debug(
                        "Resolved Enterprise Grid client URL: %s (matched %s)",
                        client_url,
                        workspace_url,
                    )
                    return client_url

            # No exact match — use first team with a URL
            first_id = next(iter(teams))
            client_url = f"{_APP_SLACK_CLIENT}{first_id}"
            logger.debug(
                "Resolved client URL (first team): %s",
                client_url,
            )
            return client_url

    return workspace_url


async def _launch_browser(pw, browser_type: str, *, headless: bool = True):  # type: ignore[no-untyped-def]
    """Launch a Playwright browser by type name."""
    if browser_type == "chrome":
        launcher = pw.chromium
        kwargs: dict = {"channel": "chrome"}
    elif browser_type == "firefox":
        launcher = pw.firefox
        kwargs = {}
    else:
        launcher = pw.webkit
        kwargs = {}
    return await launcher.launch(headless=headless, **kwargs)


class SlackBrowserMonitor:
    """Monitor an external Slack workspace via Playwright WebSocket interception.

    Playwright's page.on('websocket') API intercepts all Slack RTM frames.
    Message filtering happens immediately on receipt so no raw frame data is
    stored or logged ([SEC-007]).

    The asyncio event loop is captured at ``start()`` time and used for
    thread-safe queue enqueue from Playwright's callback threads.
    """

    def __init__(
        self,
        workspace_id: str,
        workspace_url: str,
        state_file: Path,
        monitored_channel_ids: list[str],
        user_id: str,
    ) -> None:
        self._workspace_id = workspace_id
        self._workspace_url = workspace_url
        self._state_file = state_file
        self._monitored_channel_ids: set[str] = set(monitored_channel_ids)
        self._user_id = user_id

        self._queue: asyncio.Queue[SlackMessage] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._playwright = None  # type: ignore[assignment]
        self._browser = None  # type: ignore[assignment]
        self._context = None  # type: ignore[assignment]
        self._page = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, browser_type: str = "chrome") -> None:
        """Launch Playwright, load Slack, and attach WebSocket listener.

        For Enterprise Grid workspaces, resolves the ``app.slack.com/client/``
        URL from the saved state's localStorage to bypass the workspace picker
        page. Falls back to the configured workspace URL if no team data is found.
        """
        from playwright.async_api import async_playwright  # noqa: PLC0415

        self._loop = asyncio.get_running_loop()

        self._playwright = await async_playwright().start()
        self._browser = await _launch_browser(self._playwright, browser_type, headless=True)

        context_kwargs: dict = {}
        if self._state_file.is_file():
            context_kwargs["storage_state"] = str(self._state_file)

        self._context = await self._browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()
        self._page.on("websocket", self._on_websocket)

        # Resolve the actual client URL (handles Enterprise Grid workspace picker)
        nav_url = _resolve_client_url(self._workspace_url, self._state_file)
        await self._page.goto(nav_url)
        logger.info("SlackBrowserMonitor started for workspace %s", self._workspace_id)

    def _on_websocket(self, ws) -> None:  # type: ignore[no-untyped-def]
        """Attach a frame handler to each new WebSocket connection."""
        ws.on("framereceived", self._on_frame)

    def _on_frame(self, payload: str | bytes) -> None:
        """Parse a WebSocket frame and enqueue matching messages.

        [SEC-007] Filters by type IMMEDIATELY after JSON parse. Never logs raw
        frames. Only enqueues sanitised SlackMessage fields.
        """
        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            frame = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            # Not JSON — ignore silently (many Slack WS frames are non-JSON pings)
            return

        # [SEC-007] Filter by type immediately — discard everything else
        if frame.get("type") != "message":
            return

        # Skip bot messages and message edits (subtype indicates non-user content)
        subtype = frame.get("subtype", "")
        if subtype in ("bot_message", "message_changed", "message_deleted"):
            return

        channel = str(frame.get("channel", ""))
        user = str(frame.get("user", ""))
        text = str(frame.get("text", ""))
        ts = str(frame.get("ts", ""))

        # Determine message classification
        is_dm = channel.startswith("D")
        # Direct @user mention OR broadcast mentions (@here, @channel, @everyone)
        is_mention = (
            f"<@{self._user_id}>" in text
            or "<!here>" in text
            or "<!channel>" in text
            or "<!everyone>" in text
        )

        # Apply routing filter: only accept DMs, mentions, or monitored channels
        if not (is_dm or is_mention or channel in self._monitored_channel_ids):
            return

        msg = SlackMessage(
            channel=channel,
            user=user,
            text=text,
            ts=ts,
            workspace=self._workspace_id,
            is_dm=is_dm,
            is_mention=is_mention,
        )

        # Thread-safe enqueue via the captured event loop.
        # put_nowait is scheduled as a callback — QueueFull fires inside
        # the loop, not here. Wrap in a helper so overflow is logged.
        if self._loop is not None and not self._loop.is_closed():
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._safe_enqueue, msg)

    def _safe_enqueue(self, msg: SlackMessage) -> None:
        """Enqueue a message, dropping with a warning if the queue is full.

        Called via ``call_soon_threadsafe`` so the QueueFull exception is
        caught in the event loop thread where it actually fires.
        """
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning(
                "SlackBrowserMonitor queue full for workspace %s — dropping message",
                self._workspace_id,
            )

    async def drain(self, limit: int = 0) -> list[SlackMessage]:
        """Drain queued messages and return them.

        Args:
            limit: Maximum number of messages to drain. 0 means drain all.
        """
        messages: list[SlackMessage] = []
        while limit == 0 or len(messages) < limit:
            try:
                messages.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return messages

    async def refresh_if_stuck(self) -> None:
        """Reload the Slack page as a fallback if the WebSocket appears stuck."""
        if self._page is not None:
            logger.info("SlackBrowserMonitor: refreshing page for workspace %s", self._workspace_id)
            try:
                await self._page.reload()
            except Exception as exc:
                logger.warning(
                    "SlackBrowserMonitor: page reload failed for workspace %s: %s",
                    self._workspace_id,
                    exc,
                )

    async def stop(self) -> None:
        """Save auth state and close the browser."""
        if self._context is not None:
            # [SEC-R-002] Verify state file is not a symlink before writing
            if self._state_file.is_symlink():
                logger.error(
                    "SlackBrowserMonitor: refusing to save auth state — %s is a symlink",
                    self._state_file,
                )
            else:
                try:
                    await self._context.storage_state(path=str(self._state_file))
                    # [SEC-005] Ensure saved state file has restricted permissions
                    self._state_file.chmod(0o600)
                    logger.info(
                        "SlackBrowserMonitor: saved auth state for workspace %s",
                        self._workspace_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "SlackBrowserMonitor: failed to save auth state for %s: %s",
                        self._workspace_id,
                        exc,
                    )

            with contextlib.suppress(Exception):
                await self._context.close()

        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()

        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()

        logger.info("SlackBrowserMonitor stopped for workspace %s", self._workspace_id)


_USER_ID_JS = """\
(workspaceUrl) => {
    try {
        const lc = JSON.parse(localStorage.getItem('localConfig_v2') || '{}');
        const teams = lc.teams || {};
        const entries = Object.values(teams);
        // Prefer the team whose URL matches the workspace we authenticated against
        if (workspaceUrl) {
            const norm = workspaceUrl.replace(/\\/$/, '').toLowerCase();
            const match = entries.find(t =>
                t.url && t.url.replace(/\\/$/, '').toLowerCase() === norm
            );
            if (match?.user_id) return match.user_id;
        }
        // Fallback: first team with a user_id
        for (const team of entries) {
            if (team.user_id) return team.user_id;
        }
    } catch(e) {}
    return null;
}"""

# JS to extract sidebar channels via Slack's internal API.
# Uses the xoxc- token from localStorage + workspace URL from localConfig_v2
# to call users.channelSections.list (sidebar structure) and conversations.info
# (channel names). Returns channels grouped by sidebar section with mute status.
_CHANNELS_JS = """\
async (workspaceUrl) => {
    try {
        // Get xoxc- token from localStorage
        let token = null;
        for (let i = 0; i < localStorage.length; i++) {
            const val = localStorage.getItem(localStorage.key(i));
            if (val) {
                const m = val.match(/xoxc-[a-zA-Z0-9-]+/);
                if (m) { token = m[0]; break; }
            }
        }
        if (!token) return {error: 'no xoxc token in localStorage'};

        // Resolve workspace API base URL from localConfig_v2
        let apiBase = workspaceUrl;
        try {
            const lc = JSON.parse(localStorage.getItem('localConfig_v2') || '{}');
            for (const team of Object.values(lc.teams || {})) {
                const tUrl = (team.url || '').replace(/\\/$/, '').toLowerCase();
                if (workspaceUrl && tUrl === workspaceUrl.replace(/\\/$/, '').toLowerCase()) {
                    apiBase = team.url.replace(/\\/$/, '');
                    break;
                }
            }
        } catch(e) {}

        // Fetch sections + muted prefs in parallel (2 API calls)
        const headers = {'Content-Type': 'application/x-www-form-urlencoded'};
        const body = 'token=' + encodeURIComponent(token);
        const opts = {method: 'POST', headers, body, credentials: 'include'};

        const [sectionsResp, prefsResp] = await Promise.all([
            fetch(apiBase + '/api/users.channelSections.list', opts),
            fetch(apiBase + '/api/users.prefs.get', opts),
        ]);

        const sectionsData = await sectionsResp.json();
        if (!sectionsData.ok) {
            return {error: 'channelSections: ' + (sectionsData.error || 'unknown')};
        }

        let mutedSet = new Set();
        try {
            const prefsData = await prefsResp.json();
            if (prefsData.ok && prefsData.prefs?.muted_channels) {
                mutedSet = new Set(prefsData.prefs.muted_channels.split(','));
            }
        } catch(e) {}

        // Build grouped channel list from sections
        const sections = sectionsData.channel_sections || [];
        const channelSectionMap = sectionsData.channel_section_channels || [];
        const result = [];

        for (const section of sections) {
            const sectionChannels = channelSectionMap
                .filter(sc => sc.channel_section_id === section.channel_section_id)
                .map(sc => sc.channel_id)
                .filter(id => id && id.startsWith('C'));  // Only channels, not DMs

            if (sectionChannels.length === 0) continue;

            for (const chId of sectionChannels) {
                if (mutedSet.has(chId)) continue;
                result.push({
                    id: chId,
                    name: chId,  // Resolved below via conversations.list
                    section: section.name || 'Channels',
                });
            }
        }

        // Resolve channel names from the sidebar DOM (instant, no API calls).
        // Each sidebar channel has data-qa="channel_sidebar_name_CHANNELNAME".
        const nameEls = document.querySelectorAll(
            '[data-qa^="channel_sidebar_name_"]'
        );
        const domNames = new Map();
        for (const el of nameEls) {
            const name = el.textContent?.trim();
            if (!name) continue;
            // Walk up to find the treeitem and extract channel ID from it
            let item = el.closest('[role="treeitem"]');
            if (!item) continue;
            // Check child links/attrs for channel ID
            for (const child of item.querySelectorAll('[href], [data-qa]')) {
                const href = child.getAttribute('href') || '';
                const hm = href.match(/(C[A-Z0-9]{8,})/);
                if (hm) { domNames.set(hm[1], name); break; }
                const dqa = child.getAttribute('data-qa') || '';
                const dm = dqa.match(/(C[A-Z0-9]{8,})/);
                if (dm) { domNames.set(dm[1], name); break; }
            }
        }

        // Merge DOM names into results
        for (const ch of result) {
            ch.name = domNames.get(ch.id) || ch.id;
        }

        return result;
    } catch(e) {
        return {error: e.message || String(e)};
    }
}"""

# DOM fallback: scrape channel names from the sidebar tree.
# Less data than the API approach (no sections or mute status) but works
# even when API calls are blocked by CORS or auth issues.
_CHANNELS_DOM_FALLBACK_JS = """\
() => {
    const channels = [];
    const seen = new Set();
    // Sidebar channel items use data-qa="channel-sidebar-channel" or similar
    const items = document.querySelectorAll('[role="treeitem"]');
    for (const item of items) {
        // Get channel name from the visible text
        const nameEl = item.querySelector('[data-qa^="channel_sidebar_name_"]');
        const name = nameEl?.textContent?.trim();
        if (!name) continue;

        // Get channel ID from the item's navigation behavior.
        // Items are clickable and navigate to /client/TEAM_ID/CHANNEL_ID.
        // The data-qa attribute encodes the channel name, not ID.
        // Try to find the channel ID from any link or the item's own attributes.
        let channelId = null;

        // Check aria attributes and data attributes for channel ID
        const allEls = [item, ...item.querySelectorAll('*')];
        for (const el of allEls) {
            for (const attr of el.attributes) {
                const m = attr.value.match(/^(C[A-Z0-9]{8,})$/);
                if (m) { channelId = m[1]; break; }
            }
            if (channelId) break;
            // Check href
            const href = el.getAttribute('href') || '';
            const hm = href.match(/\\/(C[A-Z0-9]{8,})/);
            if (hm) { channelId = hm[1]; break; }
        }

        if (channelId && !seen.has(channelId)) {
            seen.add(channelId);
            channels.push({ id: channelId, name, section: 'Channels' });
        }
    }
    return channels;
}"""

# Seconds to wait for Slack's SPA boot to populate localStorage
_USER_ID_POLL_TIMEOUT = 10


async def _extract_user_id(page, workspace_url: str = "") -> str | None:  # type: ignore[no-untyped-def]
    """Extract the logged-in user's Slack ID from the browser page.

    Slack's web client stores team config in ``localConfig_v2`` in localStorage,
    including ``user_id`` per team. The key is written during SPA boot, which
    may not have completed when ``wait_for_url`` fires, so we poll with a
    short timeout rather than reading once.

    When ``workspace_url`` is provided, prefers the team whose URL matches
    (handles Enterprise Grid orgs with multiple teams).
    """
    import time  # noqa: PLC0415

    deadline = time.monotonic() + _USER_ID_POLL_TIMEOUT
    while time.monotonic() < deadline:
        try:
            result = await page.evaluate(_USER_ID_JS, workspace_url)
            if result:
                return result
        except Exception:
            return None
        await asyncio.sleep(0.5)
    return None


@dataclass
class SlackAuthResult:
    """Result of interactive Slack authentication."""

    state_file: Path
    user_id: str | None = None
    channels: list[dict[str, str]] | None = None  # [{"id": "C...", "name": "..."}]
    team_id: str | None = None  # Enterprise Grid team/org ID from /client/ URL


async def _extract_channels(page, workspace_url: str = "") -> list[dict[str, str]]:  # type: ignore[no-untyped-def]
    """Extract sidebar channels via Slack's internal API.

    Uses the ``xoxc-`` token from localStorage to call ``users.channelSections.list``
    (sidebar structure) and ``conversations.list`` (channel names). Filters out
    muted channels via ``users.prefs.get``.

    Falls back to DOM scraping if the API approach fails.

    Returns list of ``{"id": "C...", "name": "channel-name", "section": "..."}``,
    grouped by sidebar section order. Returns empty list on failure.
    """
    # Try API approach first (structured data with sections + mute filtering)
    try:
        result = await page.evaluate(_CHANNELS_JS, workspace_url)
        if isinstance(result, dict) and "error" in result:
            logger.info("Channel API extraction error: %s, trying DOM fallback", result["error"])
        elif isinstance(result, list) and len(result) > 0:
            return result
        else:
            logger.info("Channel API extraction returned empty, trying DOM fallback")
    except Exception as exc:
        logger.info("Channel API extraction failed (%s), trying DOM fallback", exc)

    # Fallback: scrape channel names from the sidebar DOM
    try:
        result = await page.evaluate(_CHANNELS_DOM_FALLBACK_JS)
        return result or []
    except Exception:
        return []


async def interactive_slack_auth(
    workspace_url: str,
    browser_type: str = "chrome",
) -> SlackAuthResult:
    """Open a browser for interactive Slack login and persist auth state.

    Waits up to 5 minutes for the user to complete login. Auth state is
    saved to ``get_browser_auth_dir() / "slack_{workspace_id}.json"``
    with 0o600 permissions ([SEC-005]).

    Returns a :class:`SlackAuthResult` with the state file path, auto-detected
    user ID, and channel name ↔ ID mappings from the sidebar.
    """
    workspace_id = _slugify(workspace_url)
    browser_auth_dir = get_browser_auth_dir()

    # [SEC-005] Validate that browser_auth/ is not a symlink before writing
    if browser_auth_dir.exists() and browser_auth_dir.is_symlink():
        raise RuntimeError(
            f"Security error: browser_auth directory {browser_auth_dir} is a symlink. "
            "Refusing to write auth state to a symlinked directory."
        )

    from playwright.async_api import async_playwright  # noqa: PLC0415

    # Create directory with 0o700 permissions ([SEC-005])
    browser_auth_dir.mkdir(parents=True, exist_ok=True)
    browser_auth_dir.chmod(0o700)

    # Add .gitignore to prevent accidental commits ([SEC-005])
    gitignore_path = browser_auth_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text("*\n")

    state_file = browser_auth_dir / f"slack_{workspace_id}.json"

    async with async_playwright() as p:
        browser = await _launch_browser(p, browser_type, headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(workspace_url, wait_until="domcontentloaded")

        # Auto-focus the email input on the login page.
        # On macOS Apple Silicon, headed-mode Chrome steals focus to the
        # address bar (playwright#31252). bring_to_front() + click() is
        # the only combo that reliably transfers OS-level focus.
        try:
            await page.bring_to_front()
            email_input = page.locator(
                'input[placeholder*="@"], input[type="email"], '
                'input[name="email"], input[data-qa="signin_email_input"]'
            ).first
            await email_input.click(timeout=3000)
        except Exception as exc:
            logger.debug("No email input to focus: %s", exc)

        logger.info(
            "Waiting for Slack login at %s (timeout %ds) ...",
            workspace_url,
            _AUTH_TIMEOUT_S,
        )

        # Wait for authenticated state — URL contains /client/ after login.
        # Use wait_until="commit" — Slack's SPA may never fire "load".
        try:
            await page.wait_for_url(
                "**/client/**",
                timeout=_AUTH_TIMEOUT_S * 1000,
                wait_until="commit",
            )
        except Exception as exc:
            await browser.close()
            # Playwright raises its own TimeoutError (not asyncio.TimeoutError).
            # Convert timeout to a clean message; re-raise all others as-is.
            from playwright.async_api import TimeoutError as PlaywrightTimeout  # noqa: PLC0415

            if isinstance(exc, PlaywrightTimeout):
                raise TimeoutError(
                    f"Slack login not completed within {_AUTH_TIMEOUT_S}s for {workspace_url}"
                ) from exc
            raise

        logger.info("Authenticated at %s", page.url)

        # Extract team ID from the /client/ URL.
        # URL format: https://app.slack.com/client/{TEAM_ID}/{CHANNEL_ID}
        team_id: str | None = None
        client_match = re.search(r"/client/([A-Z0-9]+)", page.url)
        if client_match:
            team_id = client_match.group(1)
            logger.info("Detected team ID: %s", team_id)

        # Extract user ID and channels in parallel.
        # User ID polls localStorage; channels wait for sidebar DOM
        # then call APIs + scrape. No dependency between them.
        async def _get_channels() -> list[dict[str, str]]:
            with contextlib.suppress(Exception):
                await page.wait_for_selector(
                    '[data-qa^="channel_sidebar_name_"]',
                    timeout=5000,
                )
            return await _extract_channels(page, workspace_url)

        user_id, channels = await asyncio.gather(
            _extract_user_id(page, workspace_url),
            _get_channels(),
        )

        # Save auth state with restricted permissions ([SEC-005])
        await context.storage_state(path=str(state_file))
        state_file.chmod(0o600)

        await browser.close()

    logger.info("Slack auth state saved to %s", state_file)
    return SlackAuthResult(
        state_file=state_file,
        user_id=user_id,
        channels=channels,
        team_id=team_id,
    )
