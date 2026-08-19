"""Tool implementations.

Each function takes a :class:`~wg21_wiki_mcp.context.ServerContext` and returns
a structured Pydantic model. All page content flows through the centralized
``PageFetcher``; the only transform applied before a response leaves the server
is legacy ``wiki.edg.com`` URL hygiene (see :mod:`wg21_wiki_mcp.url_hygiene`).
List tools paginate with opaque cursors; ``get_page`` chunks long pages on UTF-8
boundaries.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal

from .cache import CacheEntry, outlinks_key, title_hash
from .context import ServerContext
from .deadlines import MEETING_TOOL_TIMEOUT_MSG, composite_deadline, timeout_remaining
from .fetch import DEFAULT_COMPOSITE_MAX_WAIT_S
from .locks import DEFAULT_MAX_LOCK_ENTRIES, EvictableLockMap
from .log import get_logger
from .log_safety import safe_exception_summary
from .models import (
    BundledPage,
    Chunk,
    FetchError,
    IsoSlot,
    MeetingList,
    MeetingOverview,
    MeetingRef,
    NamespaceInfo,
    PageContent,
    PageList,
    PageNotFound,
    PageRef,
    RecentChange,
    RecentChanges,
    SearchHit,
    SearchResults,
    SessionBundle,
    WikiStatus,
)
from .pagination import (
    body_digest,
    chunk_utf8,
    cursor_offset,
    decode_cursor,
    encode_cursor,
    encode_page_chunk_cursor,
    page_chunk_offset,
)
from .url_hygiene import HygieneBudget, sanitize_legacy_edg_urls, strip_cirrus_highlight_markup
from .wikitext import extract_iso_slots, has_agenda_signal

logger = get_logger("tools")

_MEETING_TITLE_RE = re.compile(r"^\d{4}-\d{2} .+$")
_DEFAULT_PAGE_MAX_BYTES = 48 * 1024
_DEFAULT_BUNDLE_PAGE_MAX_BYTES = 8 * 1024
_MAX_LIST_LIMIT = 50
_MAX_NS_PAGE_LIMIT = 500
_MAX_OUTLINKS_LOCK_ENTRIES = DEFAULT_MAX_LOCK_ENTRIES


_outlinks_map = EvictableLockMap(max_entries=lambda: _MAX_OUTLINKS_LOCK_ENTRIES)


def _remaining(deadline: float | None) -> float | None:
    return timeout_remaining(deadline, on_exceeded=MEETING_TOOL_TIMEOUT_MSG)


def _acquire_outlinks_lock(key: str, deadline: float | None) -> threading.Lock:
    # Resolve the remaining budget before touching the lock map so an
    # already-exhausted deadline can never leave a slot referenced.
    timeout = _remaining(deadline) if deadline is not None else None
    return _outlinks_map.acquire(
        key,
        timeout=timeout,
        on_timeout=lambda: FetchError(MEETING_TOOL_TIMEOUT_MSG),
    )


def _release_outlinks_lock(key: str, lock: threading.Lock) -> None:
    _outlinks_map.release(key, lock)


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(value, hi))


def _sanitize_client_content(
    ctx: ServerContext,
    content: str,
    *,
    deadline: float | None,
    budget: HygieneBudget,
    refresh: bool = False,
    discover_meetings: Callable[[], list[str]] | None = None,
) -> str:
    """Rewrite legacy ``wiki.edg.com`` links before returning wikitext to clients.

    Hygiene is best-effort: the page itself was already retrieved successfully, so
    a failure in the secondary existence check must not discard that response.
    """
    try:
        return sanitize_legacy_edg_urls(
            content,
            config=ctx.config,
            cache=ctx.cache,
            client=ctx.client,
            fetcher=ctx.fetcher,
            budget=budget,
            deadline=deadline,
            cache_ttl_s=ctx.calendar.ttl_seconds(),
            refresh=refresh,
            discover_meetings=discover_meetings,
        )
    except Exception as exc:  # noqa: BLE001 - never fail an otherwise-good response
        logger.warning("URL hygiene skipped: %s", safe_exception_summary(exc))
        return content


def _memoized_meeting_discoverer(
    ctx: ServerContext,
    deadline: float | None,
) -> Callable[[], list[str]]:
    """Return a per-tool-call meeting discoverer that runs at most once."""
    state: dict[str, list[str]] = {}

    def discover() -> list[str]:
        if "meetings" not in state:
            state["meetings"] = _discover_meetings(ctx, deadline=deadline)
        return state["meetings"]

    return discover


def _outlinks_cache_key(title: str) -> str:
    """Return the cache key for a meeting page's outlink index (owned by ``cache``)."""
    return outlinks_key(title)


def _page_outlinks(
    ctx: ServerContext,
    title: str,
    *,
    cap: int = 500,
    deadline: float | None = None,
) -> list[str]:
    """All internal links on a page (paginated up to ``cap``)."""
    links: list[str] = []
    cont: str | None = None
    while len(links) < cap:
        batch_limit = min(500, cap - len(links))
        resp = ctx.client.page_links(
            title,
            limit=batch_limit,
            cont=cont,
            timeout=_remaining(deadline),
        )
        for page in resp.get("query", {}).get("pages", {}).values():
            for link in page.get("links", []):
                if link.get("ns", 0) >= 0:
                    links.append(link["title"])
                    if len(links) >= cap:
                        break
            if len(links) >= cap:
                break
        if len(links) >= cap:
            break
        cont = resp.get("continue", {}).get("plcontinue")
        if not cont:
            break
    return links


def _load_outlinks(entry: CacheEntry) -> list[str]:
    return json.loads(entry.content)


def _serve_stale_outlinks(
    entry: CacheEntry,
    title: str,
    *,
    reason: Literal["lock_contention", "discovery_timeout"],
) -> list[str]:
    links = _load_outlinks(entry)
    logger.debug(
        "Serving stale outlink index (title_hash=%s, reason=%s, link_count=%d, age_s=%.1f)",
        title_hash(title),
        reason,
        len(links),
        entry.age_seconds(),
    )
    return links


def _cached_page_outlinks(
    ctx: ServerContext,
    title: str,
    *,
    cap: int = 500,
    deadline: float | None = None,
) -> list[str]:
    """Outlink index for ``title``, cached for the current meeting-aware TTL."""
    ttl_seconds = ctx.calendar.ttl_seconds()
    key = _outlinks_cache_key(title)
    entry = ctx.cache.get(key)
    stale_entry = entry
    if entry is not None and entry.age_seconds() < ttl_seconds:
        return _load_outlinks(entry)

    try:
        lock = _acquire_outlinks_lock(key, deadline)
    except FetchError:
        if stale_entry is not None:
            return _serve_stale_outlinks(stale_entry, title, reason="lock_contention")
        raise
    try:
        entry = ctx.cache.get(key)
        if entry is not None:
            stale_entry = entry
        if entry is not None and entry.age_seconds() < ttl_seconds:
            return _load_outlinks(entry)
        try:
            links = _page_outlinks(ctx, title, cap=cap, deadline=deadline)
        except FetchError:
            if stale_entry is not None:
                return _serve_stale_outlinks(stale_entry, title, reason="discovery_timeout")
            raise
        fetched_at = datetime.now(timezone.utc).isoformat()
        ctx.cache.put(
            requested_title=key,
            title=title,
            redirected_from=None,
            revid=None,
            timestamp=None,
            size=None,
            content=json.dumps(links),
            fetched_at=fetched_at,
        )
        return links
    finally:
        _release_outlinks_lock(key, lock)


def _lookup_namespace_name(ctx: ServerContext, namespace_id: int) -> str | None:
    """Resolve a namespace id to its API-provided display name, if known."""
    try:
        namespaces = ctx.client.list_namespaces()
    except Exception:  # noqa: BLE001 - optional enrichment; list_pages must not fail
        return None
    for ns in namespaces:
        if ns.id == namespace_id:
            return ns.name
    return None


# --------------------------------------------------------------------------- #
# search_wiki
# --------------------------------------------------------------------------- #
def search_wiki(
    ctx: ServerContext,
    query: str,
    *,
    limit: int = 10,
    namespace: int | None = None,
    cursor: str | None = None,
    include_snippet: bool = False,
) -> SearchResults:
    """Search the wiki's full text via CirrusSearch.

    Snippets are API-generated excerpts with CirrusSearch highlight spans
    stripped; they are not authoritative — use ``get_page`` for authoritative
    text.
    """
    limit = _clamp(limit, 1, _MAX_LIST_LIMIT)
    offset = cursor_offset(cursor)
    page = ctx.client.search(query, limit=limit, namespace=namespace, offset=offset)
    deadline = composite_deadline(DEFAULT_COMPOSITE_MAX_WAIT_S)
    budget = HygieneBudget()
    discover_meetings = _memoized_meeting_discoverer(ctx, deadline)
    hits = []
    for item in page.results:
        snippet = item.snippet if include_snippet else None
        if snippet is not None:
            snippet = strip_cirrus_highlight_markup(snippet)
            snippet = _sanitize_client_content(
                ctx,
                snippet,
                deadline=deadline,
                budget=budget,
                discover_meetings=discover_meetings,
            )
        hits.append(
            SearchHit(
                title=item.title,
                namespace=item.namespace,
                size=item.size,
                wordcount=item.wordcount,
                timestamp=item.timestamp,
                snippet=snippet,
                url=ctx.client.canonical_url(item.title),
            )
        )
    next_cursor = encode_cursor({"o": page.next_offset}) if page.next_offset is not None else None
    return SearchResults(query=query, hits=hits, include_snippet=include_snippet, next_cursor=next_cursor)


# --------------------------------------------------------------------------- #
# get_page
# --------------------------------------------------------------------------- #
def get_page(
    ctx: ServerContext,
    title: str,
    *,
    section: int | None = None,
    max_bytes: int = _DEFAULT_PAGE_MAX_BYTES,
    cursor: str | None = None,
    refresh: bool = False,
    max_wait_s: float | None = None,
) -> PageContent:
    """Return a page's wikitext (or one section), chunked if large.

    Legacy ``wiki.edg.com`` links are rewritten or annotated before the response.

    Raises:
        PageNotFound: if the page does not exist.
    """
    max_bytes = _clamp(max_bytes, 1024, 256 * 1024)
    deadline = composite_deadline(max_wait_s if max_wait_s is not None else DEFAULT_COMPOSITE_MAX_WAIT_S)

    if section is not None:
        outcome = ctx.fetcher.get_page_section(
            title,
            section,
            ttl_seconds=ctx.calendar.ttl_seconds(),
            refresh=refresh,
        )
    else:
        outcome = ctx.fetcher.get_page(
            title,
            ttl_seconds=ctx.calendar.ttl_seconds(),
            refresh=refresh,
            max_wait_s=max_wait_s,
        )
    if outcome.missing or outcome.content is None:
        if section is not None:
            raise PageNotFound(f"Page or section not found: {title!r} section {section}")
        raise PageNotFound(f"Page not found: {title!r}")
    prov = ctx.provenance(outcome)
    content = _sanitize_client_content(
        ctx,
        outcome.content,
        deadline=deadline,
        budget=HygieneBudget(),
        refresh=refresh,
        discover_meetings=_memoized_meeting_discoverer(ctx, deadline),
    )

    data = content.encode("utf-8")
    total_bytes = len(data)
    digest = body_digest(data)
    start = page_chunk_offset(cursor, revid=prov.revid, total_bytes=total_bytes, digest=digest)
    chunk_text, byte_start, byte_end, total, has_more = chunk_utf8(content, start=start, max_bytes=max_bytes)
    next_cursor = (
        encode_page_chunk_cursor(byte_end, revid=prov.revid, total_bytes=total, digest=digest) if has_more else None
    )
    return PageContent(
        provenance=prov,
        section=section,
        content=chunk_text,
        chunk=Chunk(
            byte_start=byte_start,
            byte_end=byte_end,
            total_bytes=total,
            has_more=has_more,
            next_cursor=next_cursor,
        ),
    )


# --------------------------------------------------------------------------- #
# list_pages
# --------------------------------------------------------------------------- #
def list_pages(
    ctx: ServerContext,
    namespace: int,
    *,
    prefix: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> PageList:
    """Enumerate page titles in a namespace (API-provided; no content)."""
    limit = _clamp(limit, 1, _MAX_NS_PAGE_LIMIT)
    cont = decode_cursor(cursor).get("c")
    page = ctx.client.list_pages(namespace=namespace, prefix=prefix, limit=limit, cont=cont)
    pages = [PageRef(title=p.title, namespace=p.namespace, url=ctx.client.canonical_url(p.title)) for p in page.items]
    next_cursor = encode_cursor({"c": page.next_cont}) if page.next_cont is not None else None
    return PageList(
        namespace_id=namespace,
        namespace_name=_lookup_namespace_name(ctx, namespace),
        pages=pages,
        next_cursor=next_cursor,
    )


# --------------------------------------------------------------------------- #
# list_namespaces
# --------------------------------------------------------------------------- #
def list_namespaces(ctx: ServerContext) -> list[NamespaceInfo]:
    """List all content namespaces (API-provided)."""
    out: list[NamespaceInfo] = []
    for ns in ctx.client.list_namespaces():
        if ns.id < 0:
            continue
        out.append(NamespaceInfo(id=ns.id, name=ns.name, canonical=ns.canonical))
    return out


# --------------------------------------------------------------------------- #
# meeting discovery
# --------------------------------------------------------------------------- #
def _discover_meetings(ctx: ServerContext, *, deadline: float | None = None) -> list[str]:
    """All ns0 titles that look like meetings (``YYYY-MM Location``), newest first."""
    titles: list[str] = []
    cont: str | None = None
    while True:
        _remaining(deadline)
        page = ctx.client.list_pages(namespace=0, prefix=None, limit=_MAX_NS_PAGE_LIMIT, cont=cont)
        for item in page.items:
            if _MEETING_TITLE_RE.match(item.title):
                titles.append(item.title)
        cont = page.next_cont
        if not cont:
            break
    return sorted(set(titles), reverse=True)


def _resolve_meeting(ctx: ServerContext, meeting: str | None) -> str:
    """Return the requested meeting, or the most recent discovered one."""
    if meeting:
        return meeting
    meetings = _discover_meetings(ctx)
    if not meetings:
        raise PageNotFound("No meeting namespaces were found on the wiki.")
    return meetings[0]


def list_meetings(
    ctx: ServerContext,
    *,
    limit: int = 10,
    cursor: str | None = None,
) -> MeetingList:
    """List discovered meetings (newest first); flags the active meeting.

    Pagination uses a numeric offset into the meeting list recomputed on each
    request. If ns0 meeting titles are added or removed between pages, later
    pages may skip or repeat entries; prefer a small ``limit`` or restart from
    the first page when the wiki changes during pagination.
    """
    limit = _clamp(limit, 1, _MAX_LIST_LIMIT)
    offset = cursor_offset(cursor)
    all_meetings = _discover_meetings(ctx)
    active = all_meetings[0] if (all_meetings and ctx.calendar.is_meeting_active()) else None
    window = all_meetings[offset : offset + limit]
    refs = []
    for t in window:
        window_start, window_end = ctx.calendar.window_for_meeting_title(t)
        refs.append(
            MeetingRef(
                title=t,
                url=ctx.client.canonical_url(t),
                is_active=(t == active),
                window_start=window_start,
                window_end=window_end,
            )
        )
    next_offset = offset + limit
    next_cursor = encode_cursor({"o": next_offset}) if next_offset < len(all_meetings) else None
    return MeetingList(meetings=refs, active_meeting=active, next_cursor=next_cursor)


# --------------------------------------------------------------------------- #
# get_recent_changes
# --------------------------------------------------------------------------- #
def get_recent_changes(
    ctx: ServerContext,
    *,
    namespace: int | None = None,
    since: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> RecentChanges:
    """Recent edits/new pages (API-provided); high value during meetings."""
    limit = _clamp(limit, 1, _MAX_LIST_LIMIT)
    cont = decode_cursor(cursor).get("c")
    page = ctx.client.recent_changes(namespace=namespace, since=since, limit=limit, cont=cont)
    deadline = composite_deadline(DEFAULT_COMPOSITE_MAX_WAIT_S)
    budget = HygieneBudget()
    discover_meetings = _memoized_meeting_discoverer(ctx, deadline)
    changes = []
    for c in page.items:
        comment = c.comment
        if comment is not None:
            comment = _sanitize_client_content(
                ctx,
                comment,
                deadline=deadline,
                budget=budget,
                discover_meetings=discover_meetings,
            )
        changes.append(
            RecentChange(
                type=c.type,
                title=c.title,
                revid=c.revid,
                old_revid=c.old_revid,
                timestamp=c.timestamp,
                user=c.user,
                comment=comment,
                url=ctx.client.canonical_url(c.title),
            )
        )
    next_cursor = encode_cursor({"c": page.next_cont}) if page.next_cont is not None else None
    return RecentChanges(changes=changes, next_cursor=next_cursor)


# --------------------------------------------------------------------------- #
# get_meeting_overview
# --------------------------------------------------------------------------- #
def get_meeting_overview(ctx: ServerContext, meeting: str | None = None) -> MeetingOverview:
    """Return a meeting's landing page plus its deterministic outlink index."""
    deadline = composite_deadline(DEFAULT_COMPOSITE_MAX_WAIT_S)
    title = _resolve_meeting(ctx, meeting)
    home = get_page(ctx, title, max_wait_s=_remaining(deadline))
    outlinks = [
        PageRef(title=t, url=ctx.client.canonical_url(t))
        for t in _cached_page_outlinks(ctx, title, deadline=deadline)
        if t.startswith(title)
    ]
    return MeetingOverview(meeting=title, home=home, outlinks=outlinks)


# --------------------------------------------------------------------------- #
# get_meeting_sessions  (session BUNDLE, not a parsed schedule)
# --------------------------------------------------------------------------- #
def get_meeting_sessions(
    ctx: ServerContext,
    meeting: str | None = None,
    *,
    groups: list[str] | None = None,
    include_wikitext: bool = True,
    max_page_bytes: int = _DEFAULT_BUNDLE_PAGE_MAX_BYTES,
) -> SessionBundle:
    """Bundle the raw pages needed to compose a meeting's schedule.

    The server does NOT compose a schedule (room/day tables are not reliably
    parseable). It returns deterministic ``iso_slots`` (agenda time boundaries,
    when present) plus the relevant pages; the calling LLM composes.
    """
    title = _resolve_meeting(ctx, meeting)
    max_page_bytes = _clamp(max_page_bytes, 512, 64 * 1024)
    group_tokens = [g.lower() for g in (groups or [])]
    deadline = composite_deadline(DEFAULT_COMPOSITE_MAX_WAIT_S)
    budget = HygieneBudget()
    discover_meetings = _memoized_meeting_discoverer(ctx, deadline)

    candidates = [t for t in _cached_page_outlinks(ctx, title, deadline=deadline) if t.startswith(title)]
    fetched = (
        ctx.fetcher.get_pages(
            candidates,
            ttl_seconds=ctx.calendar.ttl_seconds(),
            max_wait_s=_remaining(deadline),
        )
        if candidates
        else {}
    )

    iso_slots: list[IsoSlot] = []
    iso_status = "not_found"
    pages: list[BundledPage] = []
    missing: list[str] = []

    for cand in candidates:
        outcome = fetched.get(cand)
        if outcome is None or outcome.missing or outcome.content is None:
            missing.append(cand)
            continue
        content = outcome.content
        is_agenda = has_agenda_signal(content)
        if is_agenda and iso_status == "not_found":
            iso_slots, iso_status = extract_iso_slots(content, outcome.title)
        matched_group = any(tok in cand.lower() for tok in group_tokens)
        role = "agenda" if is_agenda else ("working_group" if matched_group else "other")

        include_body = include_wikitext and (is_agenda or matched_group)
        if include_body:
            sanitized = _sanitize_client_content(
                ctx,
                content,
                deadline=deadline,
                budget=budget,
                discover_meetings=discover_meetings,
            )
        else:
            sanitized = content
        body, body_cursor, truncated, body_total = _bundle_body(sanitized, include_body, max_page_bytes)
        pages.append(
            BundledPage(
                title=outcome.title,
                role=role,  # type: ignore[arg-type]
                provenance=ctx.provenance(outcome),
                wikitext=body,
                size_bytes=body_total if include_body else (outcome.size or len(content.encode("utf-8"))),
                truncated=truncated,
                next_cursor=body_cursor,
            )
        )

    return SessionBundle(
        meeting=title,
        iso_slots=iso_slots,
        iso_slots_extraction=iso_status,  # type: ignore[arg-type]
        pages=pages,
        missing_pages=missing,
    )


def _bundle_body(content: str, include: bool, max_bytes: int) -> tuple[str | None, str | None, bool, int]:
    if not include:
        return None, None, False, 0
    chunk, _start, end, total, has_more = chunk_utf8(content, start=0, max_bytes=max_bytes)
    cursor = encode_cursor({"o": end}) if has_more else None
    return chunk, cursor, has_more, total


# --------------------------------------------------------------------------- #
# wiki_status
# --------------------------------------------------------------------------- #
def wiki_status(ctx: ServerContext) -> WikiStatus:
    """Operational status. Contains no confidential wiki content."""
    calendar = ctx.calendar.status()
    try:
        cache_entries: int | None = ctx.cache.count()
    except Exception as exc:  # noqa: BLE001 - status must never fail hard
        logger.warning(
            "Cache count failed: %s",
            safe_exception_summary(exc),
        )
        cache_entries = None
    return WikiStatus(
        base_url=ctx.config.base_url,
        authenticated=ctx.client.active_label is not None,
        auth_mode=ctx.client.active_label,
        user=ctx.client.username,
        ttl_normal_s=ctx.config.ttl_normal_s,
        ttl_meeting_s=ctx.config.ttl_meeting_s,
        current_ttl_s=ctx.calendar.ttl_seconds(),
        ttl_mode=ctx.calendar.ttl_mode(),  # type: ignore[arg-type]
        calendar=calendar,
        cache_entries=cache_entries,
    )
