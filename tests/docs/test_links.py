"""Guard tests: validate external links in documentation."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pytest

pytestmark = [pytest.mark.docs, pytest.mark.slow]

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
    """Fetch URL and return (status, final_url)."""
    req = Request(  # noqa: S310
        url, headers={"User-Agent": "summon-claude-docs-linkcheck/1.0"}, method=method
    )
    with urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
        return resp.status, resp.url


def _check_url(url: str) -> tuple[bool, str]:  # noqa: PLR0911
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
    except TimeoutError:
        return False, "timeout"
    if status >= 400:
        return False, f"status={status}"
    if final_url != url:
        return False, f"redirects to {final_url}"
    return True, f"status={status}"


_all_urls = sorted(u for u in _collect_urls() if not _should_skip(u))


@pytest.mark.parametrize("url", _all_urls, ids=lambda u: u[:80])
def test_external_link_reachable(url: str) -> None:
    """Each external URL in docs must be reachable (2xx/3xx)."""
    ok, detail = _check_url(url)
    assert ok, f"Broken link: {url} ({detail})"
