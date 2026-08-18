"""Legacy URL hygiene for wiki page content returned to MCP clients.

Wiki pages migrated from ``wiki.edg.com`` (XWiki) often still embed discontinued
host links. The fetch/cache path keeps wikitext byte-for-byte; this module
rewrites or annotates only those legacy URLs at the tool boundary.

Unresolvable links keep their original URL and gain the literal marker
``STALE_URL_MARKER``. The marker deliberately avoids ``[``/``]`` so it cannot
open a MediaWiki link sequence inside a field declared to hold wikitext.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import requests

from .cache import Cache
from .config import Config
from .deadlines import http_timeout, timeout_remaining
from .log import get_logger
from .log_safety import safe_exception_summary

if TYPE_CHECKING:
    from .fetch import PageFetcher
    from .wiki_client import WikiClient

logger = get_logger("url_hygiene")

_EDG_URL_RE = re.compile(r"https?://wiki\.edg\.com/bin/view/[^\s\]|<>\"'*)}]+", re.IGNORECASE)
# Sentence punctuation is never part of a wiki path but is routinely adjacent to
# a URL in prose, so it must not be captured into the match.
_URL_TRAILING_PUNCTUATION = ".,;:!?"
_EDG_DISCONTINUED_MARKERS = ("EDG Wiki Discontinued", "EDG Wiki - Discontinued")
_ISOCPP_STUB_LINK_RE = re.compile(
    r"page now located at:\s*<a\s[^>]*?href=[\"'](https?://wiki\.isocpp\.org/[^\"']+)[\"']",
    re.IGNORECASE,
)
_ISOCPP_LINK_RE = re.compile(r"https?://wiki\.isocpp\.org/[^\s\"'<>]+", re.IGNORECASE)
_EDG_SPACE_RE = re.compile(r"^Wg21(.+?)(\d{4})$", re.IGNORECASE)
# Characters after the discontinuation notice that may still hold the migration
# target; beyond this window a hit is navigation chrome, not the replacement.
_STUB_FALLBACK_WINDOW = 500
_NAV_STUB_TITLES = frozenset({"Main_Page", "Main Page"})

#: Literal marker appended to legacy URLs that could not be resolved.
STALE_URL_MARKER = "(stale URL)"
_STALE_SUFFIX = f" {STALE_URL_MARKER}"

#: Probe ceiling shared by every sanitized field in one tool response.
MAX_EDG_PROBES_PER_RESPONSE = 10
#: Distinct legacy URLs rewritten or annotated per sanitized field.
MAX_EDG_URLS_PER_FIELD = 50

# CirrusSearch wraps matched terms in this span; strip before URL extraction.
_CIRRUS_HIGHLIGHT_RE = re.compile(
    r"<span\s+class=[\"']searchmatch[\"']\s*>(.*?)</span>",
    re.IGNORECASE | re.DOTALL,
)


class ProbeOutcome(Enum):
    """Result of probing an EDG discontinuation stub."""

    RESOLVED = auto()
    NO_SUCCESSOR = auto()
    FAILED = auto()


@dataclass(frozen=True)
class ProbeResult:
    """Tri-state EDG stub probe result."""

    outcome: ProbeOutcome
    target: str | None = None


@dataclass
class HygieneBudget:
    """Probe allowance shared across every sanitized field of one tool response.

    A tool may sanitize many fields (one per search hit or recent change), so the
    budget is owned by the tool call rather than reset per field.
    """

    probes_remaining: int = MAX_EDG_PROBES_PER_RESPONSE

    def take_probe(self) -> bool:
        """Consume one probe slot, returning False when the budget is exhausted."""
        if self.probes_remaining <= 0:
            return False
        self.probes_remaining -= 1
        return True


def strip_cirrus_highlight_markup(content: str) -> str:
    """Remove CirrusSearch ``<span class="searchmatch">`` wrappers before URL extraction."""
    return _CIRRUS_HIGHLIGHT_RE.sub(r"\1", content)


def extract_edg_urls(content: str) -> list[str]:
    """Return unique ``wiki.edg.com`` view URLs found in ``content`` (order preserved)."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _EDG_URL_RE.finditer(content):
        url = match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out[:MAX_EDG_URLS_PER_FIELD]


def parse_edg_view_url(url: str) -> tuple[str, str] | None:
    """Parse ``/bin/view/{space}/{page_path}`` from a legacy EDG wiki URL."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "wiki.edg.com":
        return None
    path = unquote(parsed.path or "")
    prefix = "/bin/view/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix) :]
    if "/" not in rest:
        return None
    space, page_path = rest.split("/", 1)
    if not space or not page_path:
        return None
    return space, page_path


def is_edg_discontinued_html(body: str) -> bool:
    """Return whether ``body`` looks like the EDG wiki discontinuation stub."""
    return any(marker in body for marker in _EDG_DISCONTINUED_MARKERS)


def _meeting_underscore_to_spaces(segment: str) -> str:
    """Convert ``YYYY-MM_Location`` path segments to MediaWiki space-form titles."""
    if re.fullmatch(r"\d{4}-\d{2}_.+", segment):
        return segment[:7] + segment[7:].replace("_", " ")
    return segment


def _is_plausible_migration_target(url: str, base_url: str) -> bool:
    title = wiki_title_from_isocpp_url(url, base_url)
    if title is None:
        return False
    root = title.split(":", 1)[0]
    return root not in _NAV_STUB_TITLES


def parse_isocpp_url_from_stub(html: str, *, base_url: str) -> str | None:
    """Extract the replacement isocpp.org URL from an EDG discontinuation page."""
    match = _ISOCPP_STUB_LINK_RE.search(html)
    if match:
        target = match.group(1)
        return target if _is_plausible_migration_target(target, base_url) else None
    # Only trust a bare link when it sits next to a discontinuation notice;
    # a document-wide search would happily return a nav or footer link.
    for marker in _EDG_DISCONTINUED_MARKERS:
        start = 0
        while True:
            index = html.find(marker, start)
            if index == -1:
                break
            fallback = _ISOCPP_LINK_RE.search(html[index : index + _STUB_FALLBACK_WINDOW])
            if fallback:
                target = fallback.group(0)
                if _is_plausible_migration_target(target, base_url):
                    return target
            start = index + 1
    return None


def wiki_title_from_isocpp_url(url: str, base_url: str) -> str | None:
    """Return a MediaWiki title encoded in an isocpp.org URL, if present.

    Only exact host matches are accepted: the configured wiki base host and
    ``wiki.isocpp.org``. Suffix or substring matches (e.g. ``evil.wiki.isocpp.org``)
    are rejected.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    base_host = (urlparse(base_url).hostname or "").lower()
    allowed_hosts = {base_host, "wiki.isocpp.org"}
    if host and host not in allowed_hosts:
        return None
    if parsed.path.startswith("/index.php"):
        from urllib.parse import parse_qs

        title = parse_qs(parsed.query).get("title", [None])[0]
        return unquote(title) if title else None
    path = unquote(parsed.path or "").lstrip("/")
    if not path:
        return None
    if ":" in path:
        prefix, page = path.split(":", 1)
        return f"{_meeting_underscore_to_spaces(prefix)}:{page}"
    return _meeting_underscore_to_spaces(path)


def meeting_prefix_for_edg_space(space: str, meeting_titles: list[str]) -> str | None:
    """Map an XWiki space name (e.g. ``Wg21kona2025``) to a meeting wiki prefix.

    Returns ``None`` when the space matches several same-year meetings without an
    exact location match. A wrong prefix names a page that may well exist, so the
    existence check downstream cannot catch the mistake; probing is the safe path.
    """
    match = _EDG_SPACE_RE.fullmatch(space)
    if not match:
        return None
    loc_part, year = match.group(1).lower(), match.group(2)
    candidates: list[str] = []
    exact: list[str] = []
    for title in meeting_titles:
        parts = title.split(" ", 1)
        if len(parts) != 2 or not parts[0].startswith(year):
            continue
        location = parts[1].lower().replace("-", "").replace(" ", "")
        if location == loc_part:
            exact.append(title)
        elif location.startswith(loc_part) or loc_part.startswith(location[:4]):
            candidates.append(title)
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return None


def edg_url_to_wiki_title(url: str, meeting_titles: list[str]) -> str | None:
    """Derive a candidate MediaWiki title for a legacy EDG URL."""
    parsed = parse_edg_view_url(url)
    if parsed is None:
        return None
    space, page_path = parsed
    prefix = meeting_prefix_for_edg_space(space, meeting_titles)
    if prefix is None:
        return None
    return f"{prefix}:{page_path}"


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECT_HOPS = 10
_ALLOWED_REDIRECT_HOSTS = frozenset({"wiki.edg.com", "wiki.isocpp.org"})


def _is_safe_redirect_target(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        # A malformed host or out-of-range port is attacker-controlled input;
        # the guard must reject it rather than raise through the tool boundary.
        return False
    if parsed.scheme != "https":
        return False
    if host not in _ALLOWED_REDIRECT_HOSTS:
        return False
    return port is None or port == 443


def _https_probe_url(url: str) -> str | None:
    """Return an https probe URL for ``wiki.edg.com``, or ``None`` if disallowed."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "wiki.edg.com":
        return None
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http":
        return urlunparse(parsed._replace(scheme="https"))
    return None


def _probe_edg_stub(
    url: str,
    *,
    user_agent: str,
    timeout: float,
    base_url: str,
) -> ProbeResult:
    """Fetch ``url`` and classify the EDG discontinuation stub outcome.

    ``timeout`` bounds the whole redirect chain, not each hop.
    """
    probe_url = _https_probe_url(url)
    if probe_url is None:
        return ProbeResult(ProbeOutcome.NO_SUCCESSOR)

    deadline = time.monotonic() + timeout
    current_url = probe_url
    visited = {probe_url}
    for _ in range(_MAX_REDIRECT_HOPS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.debug("EDG probe budget exhausted for %s", url)
            return ProbeResult(ProbeOutcome.FAILED)
        try:
            resp = requests.get(
                current_url,
                timeout=remaining,
                headers={"User-Agent": user_agent},
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            logger.debug("EDG probe failed for %s: %s", url, safe_exception_summary(exc))
            return ProbeResult(ProbeOutcome.FAILED)
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("Location")
            if not location:
                return ProbeResult(ProbeOutcome.FAILED)
            try:
                next_url = urljoin(current_url, location)
            except ValueError as exc:
                logger.debug("EDG probe got malformed Location for %s: %s", url, safe_exception_summary(exc))
                return ProbeResult(ProbeOutcome.FAILED)
            if not _is_safe_redirect_target(next_url):
                logger.debug("EDG probe blocked unsafe redirect for %s", url)
                return ProbeResult(ProbeOutcome.FAILED)
            if next_url in visited:
                logger.debug("EDG probe detected redirect loop for %s", url)
                return ProbeResult(ProbeOutcome.FAILED)
            visited.add(next_url)
            current_url = next_url
            continue
        if resp.status_code >= 400:
            return ProbeResult(ProbeOutcome.NO_SUCCESSOR)
        body = resp.text
        if not is_edg_discontinued_html(body):
            return ProbeResult(ProbeOutcome.NO_SUCCESSOR)
        target = parse_isocpp_url_from_stub(body, base_url=base_url)
        if target is None:
            return ProbeResult(ProbeOutcome.NO_SUCCESSOR)
        return ProbeResult(ProbeOutcome.RESOLVED, target)
    logger.debug("EDG probe exceeded redirect hops for %s", url)
    return ProbeResult(ProbeOutcome.FAILED)


def _titles_exist(
    fetcher: PageFetcher,
    titles: list[str],
    *,
    ttl_seconds: int,
    deadline: float | None,
) -> dict[str, bool]:
    """Resolve title existence through the shared fetcher (cache-first, single-flight)."""
    if not titles:
        return {}
    fetched = fetcher.get_pages(
        titles,
        ttl_seconds=ttl_seconds,
        max_wait_s=timeout_remaining(deadline),
    )
    return {title: not fetched[title].missing for title in titles}


def _apply_replacements(content: str, replacements: dict[str, str]) -> str:
    """Substitute every source URL in one pass.

    Sequential :meth:`str.replace` calls would rescan text that already holds a
    substituted value, so a shorter source URL could match inside the annotation
    written for a longer one.
    """
    if not replacements:
        return content
    # Longest first so a shared prefix never wins over the full URL.
    pattern = re.compile("|".join(re.escape(source) for source in sorted(replacements, key=len, reverse=True)))
    return pattern.sub(lambda match: replacements[match.group(0)], content)


def _stale(source: str) -> str:
    return f"{source}{_STALE_SUFFIX}"


def sanitize_legacy_edg_urls(
    content: str,
    *,
    config: Config,
    cache: Cache,
    client: WikiClient,
    fetcher: PageFetcher,
    budget: HygieneBudget,
    deadline: float | None,
    cache_ttl_s: int,
    refresh: bool = False,
    discover_meetings: Callable[[], list[str]] | None = None,
) -> str:
    """Rewrite or annotate legacy ``wiki.edg.com`` URLs before serving ``content``."""
    sources = extract_edg_urls(content)
    if not sources:
        return content

    positive_ttl = config.url_remap_ttl_s
    # A "no successor" verdict must expire with the meeting-aware page TTL, or a
    # marker written during a meeting hides a successor page created an hour later.
    negative_ttl = min(config.url_remap_ttl_s, cache_ttl_s)
    replacements: dict[str, str] = {}
    pending_probe: list[str] = []
    candidate_titles: dict[str, str] = {}
    meeting_titles: list[str] | None = None

    for source in sources:
        cached = None if refresh else cache.get_url_remap(source)
        if cached is not None:
            ttl = positive_ttl if cached.target_url is not None else negative_ttl
            if cached.age_seconds() < ttl:
                replacements[source] = cached.target_url if cached.target_url is not None else _stale(source)
                continue

        if meeting_titles is None and discover_meetings is not None:
            meeting_titles = discover_meetings()
        title = edg_url_to_wiki_title(source, meeting_titles or [])
        if title is not None:
            candidate_titles[source] = title
        else:
            pending_probe.append(source)

    if candidate_titles:
        titles = list(candidate_titles.values())
        exists = _titles_exist(fetcher, titles, ttl_seconds=cache_ttl_s, deadline=deadline)
        for source, title in candidate_titles.items():
            if exists.get(title):
                target = client.canonical_url(title)
                replacements[source] = target
                cache.put_url_remap(source, target)
            else:
                pending_probe.append(source)

    for source in pending_probe:
        if not budget.take_probe():
            replacements[source] = _stale(source)
            continue
        probe_timeout = http_timeout(deadline, cap=float(config.url_hygiene_timeout_s))
        probe = _probe_edg_stub(
            source,
            user_agent=config.user_agent,
            timeout=probe_timeout,
            base_url=config.base_url,
        )
        probed_target: str | None = None
        if probe.outcome is ProbeOutcome.RESOLVED and probe.target is not None:
            title = wiki_title_from_isocpp_url(probe.target, config.base_url)
            if title is not None and _titles_exist(
                fetcher,
                [title],
                ttl_seconds=cache_ttl_s,
                deadline=deadline,
            ).get(title):
                probed_target = client.canonical_url(title)
        replacements[source] = probed_target if probed_target is not None else _stale(source)
        if probe.outcome is ProbeOutcome.RESOLVED:
            cache.put_url_remap(source, probed_target)
        elif probe.outcome is ProbeOutcome.NO_SUCCESSOR:
            cache.put_url_remap(source, None)

    return _apply_replacements(content, replacements)
