"""Legacy URL hygiene for wiki page content returned to MCP clients.

Wiki pages migrated from ``wiki.edg.com`` (XWiki) often still embed discontinued
host links. The fetch/cache path keeps wikitext byte-for-byte; this module
rewrites or annotates only those legacy URLs at the tool boundary.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import unquote, urljoin, urlparse

import requests

from .cache import Cache
from .config import Config
from .deadlines import http_timeout, timeout_remaining
from .log import get_logger
from .log_safety import safe_exception_summary

if TYPE_CHECKING:
    from .wiki_client import WikiClient

logger = get_logger("url_hygiene")

_EDG_URL_RE = re.compile(r"https?://wiki\.edg\.com/bin/view/[^\s\]|<>\"'*)]+", re.IGNORECASE)
_EDG_DISCONTINUED_MARKERS = ("EDG Wiki Discontinued", "EDG Wiki - Discontinued")
_ISOCPP_STUB_LINK_RE = re.compile(
    r"page now located at:\s*<a href=\"(https?://wiki\.isocpp\.org/[^\"]+)\"",
    re.IGNORECASE,
)
_ISOCPP_LINK_RE = re.compile(r"https?://wiki\.isocpp\.org/[^\s\"'<>]+", re.IGNORECASE)
_EDG_SPACE_RE = re.compile(r"^Wg21(.+?)(\d{4})$", re.IGNORECASE)
_MAX_EDG_PROBES_PER_PAGE = 10
_STALE_PREFIX = "[stale URL: "


def extract_edg_urls(content: str) -> list[str]:
    """Return unique ``wiki.edg.com`` view URLs found in ``content`` (order preserved)."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _EDG_URL_RE.finditer(content):
        url = match.group(0)
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


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


def parse_isocpp_url_from_stub(html: str) -> str | None:
    """Extract the replacement isocpp.org URL from an EDG discontinuation page."""
    match = _ISOCPP_STUB_LINK_RE.search(html)
    if match:
        return match.group(1)
    fallback = _ISOCPP_LINK_RE.search(html)
    return fallback.group(0) if fallback else None


def wiki_title_from_isocpp_url(url: str, base_url: str) -> str | None:
    """Return a MediaWiki title encoded in an isocpp.org URL, if present."""
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
    return path if path else None


def meeting_prefix_for_edg_space(space: str, meeting_titles: list[str]) -> str | None:
    """Map an XWiki space name (e.g. ``Wg21kona2025``) to a meeting wiki prefix."""
    match = _EDG_SPACE_RE.fullmatch(space)
    if not match:
        return None
    loc_part, year = match.group(1).lower(), match.group(2)
    candidates: list[str] = []
    for title in meeting_titles:
        parts = title.split(" ", 1)
        if len(parts) != 2 or not parts[0].startswith(year):
            continue
        location = parts[1].lower().replace("-", "").replace(" ", "")
        if location.startswith(loc_part) or loc_part.startswith(location[:4]):
            candidates.append(title.replace(" ", "_"))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    for candidate in candidates:
        if loc_part in candidate.lower():
            return candidate
    return candidates[0]


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


def _remap_fresh(entry: tuple[str | None, str], ttl_seconds: int) -> bool:
    target, fetched_at = entry
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    return age < ttl_seconds


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECT_HOPS = 10
_ALLOWED_REDIRECT_HOSTS = frozenset({"wiki.edg.com", "wiki.isocpp.org"})


def _is_safe_redirect_target(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_REDIRECT_HOSTS:
        return False
    port = parsed.port
    if port is not None and port != 443:
        return False
    return True


def _probe_edg_stub(
    url: str,
    *,
    user_agent: str,
    timeout: float,
) -> str | None:
    current_url = url
    for _ in range(_MAX_REDIRECT_HOPS + 1):
        try:
            resp = requests.get(
                current_url,
                timeout=timeout,
                headers={"User-Agent": user_agent},
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            logger.debug("EDG probe failed for %s: %s", url, safe_exception_summary(exc))
            return None
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("Location")
            if not location:
                return None
            next_url = urljoin(current_url, location)
            if not _is_safe_redirect_target(next_url):
                logger.debug("EDG probe blocked unsafe redirect to %s", next_url)
                return None
            current_url = next_url
            continue
        if resp.status_code >= 400:
            return None
        body = resp.text
        if not is_edg_discontinued_html(body):
            return None
        return parse_isocpp_url_from_stub(body)
    logger.debug("EDG probe exceeded redirect hops for %s", url)
    return None


def _titles_exist(
    client: WikiClient,
    titles: list[str],
    *,
    deadline: float | None,
) -> dict[str, bool]:
    if not titles:
        return {}
    timeout = http_timeout(deadline, cap=30.0)
    fetched = client.fetch_pages(titles, timeout=timeout)
    return {title: not fetched[title].missing for title in titles}


def _apply_replacements(content: str, replacements: dict[str, str]) -> str:
    if not replacements:
        return content
    # Longest URLs first so shared prefixes cannot partially overlap.
    for source in sorted(replacements, key=len, reverse=True):
        content = content.replace(source, replacements[source])
    return content


def sanitize_legacy_edg_urls(
    content: str,
    *,
    config: Config,
    cache: Cache,
    client: WikiClient,
    discover_meetings: Callable[[], list[str]] | None = None,
    deadline: float | None = None,
) -> str:
    """Rewrite or annotate legacy ``wiki.edg.com`` URLs before serving ``content``."""
    sources = extract_edg_urls(content)
    if not sources:
        return content

    ttl_seconds = config.url_remap_ttl_s
    replacements: dict[str, str] = {}
    pending_probe: list[str] = []
    candidate_titles: dict[str, str] = {}
    meeting_titles: list[str] | None = None

    for source in sources:
        cached = cache.get_url_remap(source)
        if cached is not None and _remap_fresh(cached, ttl_seconds):
            target = cached[0]
            replacements[source] = target if target is not None else f"{_STALE_PREFIX}{source}]"
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
        exists = _titles_exist(client, titles, deadline=deadline)
        title_to_canonical = {t: client.canonical_url(t) for t in titles}
        for source, title in candidate_titles.items():
            if exists.get(title):
                target = title_to_canonical[title]
                replacements[source] = target
                cache.put_url_remap(source, target)
            else:
                pending_probe.append(source)

    probes_done = 0
    for source in pending_probe:
        if source in replacements:
            continue
        if probes_done >= _MAX_EDG_PROBES_PER_PAGE:
            replacements[source] = f"{_STALE_PREFIX}{source}]"
            cache.put_url_remap(source, None)
            continue
        probe_timeout = http_timeout(
            deadline,
            cap=float(config.url_hygiene_timeout_s),
        )
        if deadline is not None:
            timeout_remaining(deadline)
        stub_target = _probe_edg_stub(
            source,
            user_agent=config.user_agent,
            timeout=probe_timeout,
        )
        probes_done += 1
        if stub_target is not None:
            title = wiki_title_from_isocpp_url(stub_target, config.base_url)
            if title is not None:
                exists = _titles_exist(client, [title], deadline=deadline)
                if exists.get(title):
                    target = client.canonical_url(title)
                    replacements[source] = target
                    cache.put_url_remap(source, target)
                    continue
            replacements[source] = f"{_STALE_PREFIX}{source}]"
            cache.put_url_remap(source, None)
            continue
        replacements[source] = f"{_STALE_PREFIX}{source}]"
        cache.put_url_remap(source, None)

    return _apply_replacements(content, replacements)
