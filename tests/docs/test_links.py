"""Guard tests: validate external links in documentation."""

from __future__ import annotations

import re
import time
import unittest.mock
from http.client import HTTPMessage
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pytest

pytestmark = [pytest.mark.docs]

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DOCS_DIR = _REPO_ROOT / "docs"

# Matches [text](url) and bare https:// URLs
_MARKDOWN_LINK_RE = re.compile(r"\[(?:[^\]]*)\]\((https?://[^)]+)\)")
_BARE_URL_RE = re.compile(r"(?<!\()(https?://[^\s)>\]]+)")

# URLs known to be unreliable, auth-required, or rate-limited in CI
SKIP_URLS: frozenset[str] = frozenset(
    {
        "https://api.githubcopilot.com/mcp/",  # Requires auth token
    }
)

# Domains that aggressively block automated requests
SKIP_DOMAINS: frozenset[str] = frozenset(
    {
        "docs.slack.dev",  # Returns 403 for non-browser User-Agents
    }
)

# Example/placeholder URL patterns (not real pages)
_EXAMPLE_URL_RE = re.compile(
    r"https?://("
    r"example\.com"
    r"|acme\."  # placeholder company
    r"|myteam\."  # placeholder team
    r"|workspace\.slack\.com"  # generic workspace placeholder
    r")"
)

# Placeholder GitHub org/owner names in path (github.com/<placeholder>/...)
_PLACEHOLDER_GITHUB_RE = re.compile(r"https://github\.com/(myorg|owner)/")

_TIMEOUT = 10  # seconds


def _collect_urls() -> set[str]:
    """Extract all unique external URLs from docs markdown files."""
    urls: set[str] = set()
    for md_file in sorted(_DOCS_DIR.rglob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        for match in _MARKDOWN_LINK_RE.finditer(content):
            urls.add(match.group(1))
        for match in _BARE_URL_RE.finditer(content):
            urls.add(match.group(0))
    # Strip trailing punctuation and fragments for dedup
    return {u.rstrip(".,;:!?`\"'") for u in urls}


def _should_skip(url: str) -> bool:
    """Check if URL should be skipped."""
    if url in SKIP_URLS:
        return True
    if _EXAMPLE_URL_RE.search(url) or _PLACEHOLDER_GITHUB_RE.search(url):
        return True
    parsed = urlparse(url)
    return parsed.hostname in SKIP_DOMAINS


def _fetch(url: str, method: str = "HEAD") -> tuple[int, str]:
    """Fetch URL and return (status, final_url) with retry on transient errors."""
    max_attempts = 4
    for attempt in range(max_attempts):
        try:
            req = Request(  # noqa: S310
                url, headers={"User-Agent": "summon-claude-docs-linkcheck/1.0"}, method=method
            )
            with urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
                return resp.status, resp.url
        except HTTPError as e:
            if e.code < 500:
                raise
            if attempt == max_attempts - 1:
                raise
        except URLError:
            if attempt == max_attempts - 1:
                raise
        except ConnectionError as e:
            if attempt == max_attempts - 1:
                raise URLError(str(e)) from e
        except TimeoutError as e:
            if attempt == max_attempts - 1:
                raise URLError("timeout") from e
        time.sleep(2 * (2**attempt))
    raise AssertionError("unreachable")


def _check_url(url: str) -> tuple[bool, str]:
    """Check if URL is reachable and canonical (no redirects). Returns (ok, detail)."""
    try:
        status, final_url = _fetch(url, "HEAD")
    except HTTPError as e:
        if e.code == 405:
            try:
                status, final_url = _fetch(url, "GET")
            except (HTTPError, URLError) as e2:
                return False, str(e2)
        else:
            return False, f"HTTP {e.code}"
    except URLError as e:
        return False, str(e.reason)
    if status >= 400:
        return False, f"status={status}"
    if final_url != url:
        return False, f"redirects to {final_url}"
    return True, f"status={status}"


_all_urls = sorted(u for u in _collect_urls() if not _should_skip(u))


# ---------------------------------------------------------------------------
# Unit tests for _fetch() retry logic — no network access (urlopen is mocked)
# ---------------------------------------------------------------------------


class TestFetchRetry:
    """Tests for the retry and backoff logic in _fetch() — no network access needed."""

    def test_retry_succeeds_after_transient_failure(self) -> None:
        """_fetch succeeds when urlopen raises 5xx twice then returns normally."""
        err_502 = HTTPError("http://example.com", 502, "Bad Gateway", HTTPMessage(), None)
        mock_resp = unittest.mock.MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = unittest.mock.MagicMock(return_value=False)
        mock_resp.status = 200
        mock_resp.url = "http://example.com"

        with (
            unittest.mock.patch(
                "tests.docs.test_links.urlopen",
                side_effect=[err_502, err_502, mock_resp],
            ),
            unittest.mock.patch("time.sleep"),
        ):
            status, final_url = _fetch("http://example.com")

        assert status == 200
        assert final_url == "http://example.com"

    def test_no_retry_on_client_error(self) -> None:
        """_fetch raises immediately on 4xx errors without sleeping."""
        err_404 = HTTPError("http://example.com", 404, "Not Found", HTTPMessage(), None)

        with (
            unittest.mock.patch(
                "tests.docs.test_links.urlopen",
                side_effect=err_404,
            ),
            unittest.mock.patch("time.sleep") as mock_sleep,
            pytest.raises(HTTPError) as exc_info,
        ):
            _fetch("http://example.com")

        assert exc_info.value.code == 404
        mock_sleep.assert_not_called()

    def test_connection_error_wrapping(self) -> None:
        """_fetch wraps ConnectionError in URLError after exhausting retries."""
        with (
            unittest.mock.patch(
                "tests.docs.test_links.urlopen",
                side_effect=ConnectionError("connection refused"),
            ),
            unittest.mock.patch("time.sleep"),
            pytest.raises(URLError),
        ):
            _fetch("http://example.com")

    def test_timeout_error_wrapping(self) -> None:
        """_fetch wraps TimeoutError in URLError after exhausting retries."""
        with (
            unittest.mock.patch(
                "tests.docs.test_links.urlopen",
                side_effect=TimeoutError("timed out"),
            ),
            unittest.mock.patch("time.sleep"),
            pytest.raises(URLError),
        ):
            _fetch("http://example.com")

    def test_backoff_timing(self) -> None:
        """_fetch sleeps with exponential backoff [2, 4, 8] across 3 retries."""
        err_502 = HTTPError("http://example.com", 502, "Bad Gateway", HTTPMessage(), None)

        with (
            unittest.mock.patch(
                "tests.docs.test_links.urlopen",
                side_effect=err_502,  # fails all 4 attempts
            ),
            unittest.mock.patch("time.sleep") as mock_sleep,
            pytest.raises(HTTPError),
        ):
            _fetch("http://example.com")

        assert mock_sleep.call_count == 3
        sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
        assert sleep_args == [2, 4, 8]


@pytest.mark.link_check
@pytest.mark.parametrize("url", _all_urls, ids=lambda u: u[:80])
def test_external_link_reachable(url: str) -> None:
    """Each external URL in docs must be reachable (2xx/3xx)."""
    ok, detail = _check_url(url)
    assert ok, f"Broken link: {url} ({detail})"
