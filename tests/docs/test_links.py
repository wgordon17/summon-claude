"""Guard tests: validate external links in documentation."""

from __future__ import annotations

import os
import re
import time
from http.client import HTTPMessage
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pytest

from tests.docs.conftest import DOCS_DIR  # public constant, not a fixture

# GitHub token for authenticated requests (raises rate limit from 60 to 5000/hr)
_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

pytestmark = pytest.mark.docs

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
    for md_file in sorted(DOCS_DIR.rglob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        for match in _MARKDOWN_LINK_RE.finditer(content):
            urls.add(match.group(1))
        for match in _BARE_URL_RE.finditer(content):
            urls.add(match.group(0))
    # Strip trailing punctuation and URL fragments for dedup
    return {u.rstrip(".,;:!?`\"'").split("#")[0] for u in urls}


def _should_skip(url: str) -> bool:
    """Check if URL should be skipped."""
    if url in SKIP_URLS:
        return True
    if _EXAMPLE_URL_RE.search(url) or _PLACEHOLDER_GITHUB_RE.search(url):
        return True
    parsed = urlparse(url)
    return parsed.hostname in SKIP_DOMAINS


_MAX_RETRIES = 3  # retries after first attempt (4 total attempts)


def _is_github_url(url: str) -> bool:
    return urlparse(url).hostname == "github.com"


def _fetch(url: str, method: str = "HEAD") -> tuple[int, str]:
    """Fetch URL and return (status, final_url) with retry on transient errors."""
    github_auth = _GITHUB_TOKEN and _is_github_url(url)
    for attempt in range(_MAX_RETRIES + 1):
        try:
            req = Request(  # noqa: S310
                url, headers={"User-Agent": "summon-claude-docs-linkcheck/1.0"}, method=method
            )
            if github_auth:
                # Use unredirected_header so the token is NOT forwarded on
                # cross-domain redirects (urllib forwards regular headers).
                req.add_unredirected_header("Authorization", f"token {_GITHUB_TOKEN}")
            with urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
                return resp.status, resp.url
        except HTTPError as e:
            if e.code < 500:
                raise
            if attempt == _MAX_RETRIES:
                raise
        except URLError:
            if attempt == _MAX_RETRIES:
                raise
        except ConnectionError as e:
            if attempt == _MAX_RETRIES:
                raise URLError(str(e)) from e
        except TimeoutError as e:
            if attempt == _MAX_RETRIES:
                raise URLError("timeout") from e
        time.sleep(2 * (2**attempt))
    raise AssertionError("unreachable")


def _check_url(url: str) -> tuple[bool, str]:
    """Check if URL is reachable and canonical (no redirects). Returns (ok, detail)."""
    try:
        status, final_url = _fetch(url, "HEAD")
    except HTTPError as e:
        if e.code == 405 or (e.code >= 500 and _is_github_url(url)):
            # 405: server doesn't support HEAD; retry with GET.
            # 5xx on github.com: HEAD retries are already exhausted here (re-raised
            # by _fetch); GET uses a separate retry budget and often succeeds via
            # GitHub's different web frontend code path for GET vs HEAD.
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
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200
        mock_resp.url = "http://example.com"

        with (
            patch(
                "tests.docs.test_links.urlopen",
                side_effect=[err_502, err_502, mock_resp],
            ),
            patch("time.sleep"),
        ):
            status, final_url = _fetch("http://example.com")

        assert status == 200
        assert final_url == "http://example.com"

    def test_no_retry_on_client_error(self) -> None:
        """_fetch raises immediately on 4xx errors without sleeping."""
        err_404 = HTTPError("http://example.com", 404, "Not Found", HTTPMessage(), None)

        with (
            patch(
                "tests.docs.test_links.urlopen",
                side_effect=err_404,
            ),
            patch("time.sleep") as mock_sleep,
            pytest.raises(HTTPError) as exc_info,
        ):
            _fetch("http://example.com")

        assert exc_info.value.code == 404
        mock_sleep.assert_not_called()

    def test_connection_error_wrapping(self) -> None:
        """_fetch wraps ConnectionError in URLError after exhausting retries."""
        with (
            patch(
                "tests.docs.test_links.urlopen",
                side_effect=ConnectionError("connection refused"),
            ) as mock_urlopen,
            patch("time.sleep") as mock_sleep,
            pytest.raises(URLError),
        ):
            _fetch("http://example.com")

        assert mock_urlopen.call_count == 4
        assert mock_sleep.call_count == 3

    def test_timeout_error_wrapping(self) -> None:
        """_fetch wraps TimeoutError in URLError after exhausting retries."""
        with (
            patch(
                "tests.docs.test_links.urlopen",
                side_effect=TimeoutError("timed out"),
            ) as mock_urlopen,
            patch("time.sleep") as mock_sleep,
            pytest.raises(URLError),
        ):
            _fetch("http://example.com")

        assert mock_urlopen.call_count == 4
        assert mock_sleep.call_count == 3

    def test_backoff_timing(self) -> None:
        """_fetch sleeps with exponential backoff [2, 4, 8] across 3 retries."""
        err_502 = HTTPError("http://example.com", 502, "Bad Gateway", HTTPMessage(), None)

        with (
            patch(
                "tests.docs.test_links.urlopen",
                side_effect=err_502,  # fails all 4 attempts
            ),
            patch("time.sleep") as mock_sleep,
            pytest.raises(HTTPError),
        ):
            _fetch("http://example.com")

        assert mock_sleep.call_count == 3
        sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
        assert sleep_args == [2, 4, 8]


# ---------------------------------------------------------------------------
# Unit tests for _check_url() — no network access (_fetch is mocked)
# ---------------------------------------------------------------------------


class TestCheckUrl:
    """Tests for _check_url() — _fetch is mocked, no network access needed."""

    def test_head_200_no_redirect(self) -> None:
        """Normal 2xx response with matching URL returns (True, ...)."""
        with patch("tests.docs.test_links._fetch", return_value=(200, "http://example.com/page")):
            ok, detail = _check_url("http://example.com/page")
        assert ok is True
        assert "200" in detail

    def test_head_405_fallback_get_200(self) -> None:
        """HEAD 405 triggers GET fallback; GET 200 returns (True, ...)."""
        err_405 = HTTPError("http://example.com", 405, "Method Not Allowed", HTTPMessage(), None)

        def _fake_fetch(url: str, method: str = "HEAD") -> tuple[int, str]:
            if method == "HEAD":
                raise err_405
            return 200, url

        with patch("tests.docs.test_links._fetch", side_effect=_fake_fetch):
            ok, detail = _check_url("http://example.com/page")
        assert ok is True
        assert "200" in detail

    def test_head_405_fallback_get_fails(self) -> None:
        """HEAD 405 then GET fails returns (False, ...)."""
        err_405 = HTTPError("http://example.com", 405, "Method Not Allowed", HTTPMessage(), None)
        err_500 = HTTPError("http://example.com", 500, "Server Error", HTTPMessage(), None)

        def _fake_fetch(url: str, method: str = "HEAD") -> tuple[int, str]:
            if method == "HEAD":
                raise err_405
            raise err_500

        with patch("tests.docs.test_links._fetch", side_effect=_fake_fetch):
            ok, detail = _check_url("http://example.com/page")
        assert ok is False
        assert "500" in detail or "Server Error" in detail

    def test_head_405_fallback_get_url_error(self) -> None:
        """HEAD 405 then GET raises URLError returns (False, ...)."""
        err_405 = HTTPError("http://example.com", 405, "Method Not Allowed", HTTPMessage(), None)

        def _fake_fetch(url: str, method: str = "HEAD") -> tuple[int, str]:
            if method == "HEAD":
                raise err_405
            raise URLError("Name resolution failed")

        with patch("tests.docs.test_links._fetch", side_effect=_fake_fetch):
            ok, detail = _check_url("http://example.com/page")
        assert ok is False
        assert "Name resolution failed" in detail

    def test_head_405_fallback_get_redirects(self) -> None:
        """HEAD 405 then GET with redirect returns (False, redirect detail)."""
        err_405 = HTTPError("http://example.com", 405, "Method Not Allowed", HTTPMessage(), None)

        def _fake_fetch(url: str, method: str = "HEAD") -> tuple[int, str]:
            if method == "HEAD":
                raise err_405
            return 200, "http://example.com/canonical"

        with patch("tests.docs.test_links._fetch", side_effect=_fake_fetch):
            ok, detail = _check_url("http://example.com/old")
        assert ok is False
        assert "redirect" in detail

    def test_redirect_detected(self) -> None:
        """Redirect (final_url != url) returns (False, detail mentioning redirect)."""
        with patch(
            "tests.docs.test_links._fetch",
            return_value=(200, "http://example.com/canonical"),
        ):
            ok, detail = _check_url("http://example.com/old")
        assert ok is False
        assert "redirect" in detail

    def test_status_400_or_above_fails(self) -> None:
        """Defensive: _check_url rejects status >= 400 even if _fetch returns it."""
        with patch("tests.docs.test_links._fetch", return_value=(404, "http://example.com/page")):
            ok, detail = _check_url("http://example.com/page")
        assert ok is False
        assert "404" in detail

    def test_url_error_returns_false(self) -> None:
        """URLError from _fetch returns (False, reason string)."""
        with patch(
            "tests.docs.test_links._fetch",
            side_effect=URLError("Name or service not known"),
        ):
            ok, detail = _check_url("http://example.com/page")
        assert ok is False
        assert "Name or service" in detail

    def test_non_405_http_error_returns_false(self) -> None:
        """Non-405 HTTPError (e.g. 403) returns (False, 'HTTP 403')."""
        err_403 = HTTPError("http://example.com", 403, "Forbidden", HTTPMessage(), None)
        with patch("tests.docs.test_links._fetch", side_effect=err_403):
            ok, detail = _check_url("http://example.com/page")
        assert ok is False
        assert "403" in detail

    def test_github_head_504_fallback_get_200(self) -> None:
        """HEAD 504 on github.com triggers GET fallback; GET 200 returns (True, ...)."""
        err_504 = HTTPError(
            "https://github.com/org/repo", 504, "Gateway Timeout", HTTPMessage(), None
        )

        def _fake_fetch(url: str, method: str = "HEAD") -> tuple[int, str]:
            if method == "HEAD":
                raise err_504
            return 200, url

        with patch("tests.docs.test_links._fetch", side_effect=_fake_fetch):
            ok, detail = _check_url("https://github.com/org/repo")
        assert ok is True
        assert "200" in detail

    def test_non_github_head_504_no_fallback(self) -> None:
        """HEAD 504 on non-GitHub URLs does NOT trigger GET fallback."""
        err_504 = HTTPError("http://example.com/page", 504, "Gateway Timeout", HTTPMessage(), None)
        with patch("tests.docs.test_links._fetch", side_effect=err_504) as mock_fetch:
            ok, detail = _check_url("http://example.com/page")
        assert ok is False
        assert "504" in detail
        mock_fetch.assert_called_once()  # GET fallback was NOT triggered


@pytest.mark.link_check
@pytest.mark.parametrize("url", _all_urls, ids=lambda u: u[:80])
def test_external_link_reachable(url: str) -> None:
    """Each external URL in docs must be reachable with 2xx and no redirect."""
    ok, detail = _check_url(url)
    assert ok, f"Broken link: {url} ({detail})"
