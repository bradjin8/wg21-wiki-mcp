"""The centralized page-fetch chokepoint.

Every tool that needs page wikitext goes through :class:`PageFetcher` - no tool
calls the wiki client directly. This guarantees uniform caching, provenance,
re-login, and load control. Cache-missing fetches are:

* **batched** - many titles per ``titles=`` query (the primary parallelization,
  cutting both latency and server load),
* **single-flighted** - a per-page file lock (cross-process) plus an in-process
  lock map prevents two fetches of the same page at once,
* **revalidated cheaply** - a stale cache entry is confirmed via a revid check
  before a full re-fetch, so unchanged pages cost almost nothing.
"""

from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone

from filelock import FileLock, Timeout

from .cache import Cache, CacheEntry, title_hash
from .deadlines import timeout_remaining
from .log import get_logger
from .log_safety import safe_exception_summary
from .models import FetchError
from .wiki_client import FetchedPage, WikiClient

logger = get_logger("fetch")

_LOCK_TIMEOUT_S = 60
# Composite tools (e.g. get_meeting_sessions) cap total wait for lock + network.
DEFAULT_COMPOSITE_MAX_WAIT_S = 30.0
# Idle in-process lock slots are evicted once the map exceeds this size.
_MAX_INPROC_LOCK_ENTRIES = 256
_SECTION_KEY_SEP = "\0section="


def section_cache_key(title: str, section: int) -> str:
    """Return the cache/lock key for a page section (distinct from full-page keys)."""
    return f"{title}{_SECTION_KEY_SEP}{section}"


@dataclass
class _InprocLockSlot:
    """Per-title in-process lock with a user count for safe map eviction."""

    lock: threading.Lock
    users: int = 0


@dataclass(frozen=True)
class FetchOutcome:
    """Resolved page result, whether served from cache or freshly fetched."""

    requested_title: str
    title: str
    redirected_from: str | None
    revid: int | None
    timestamp: str | None
    size: int | None
    content: str | None
    fetched_at: str
    from_cache: bool
    missing: bool


class PageFetcher:
    """Cache-first, batched, single-flight page retrieval."""

    def __init__(self, client: WikiClient, cache: Cache) -> None:
        """Wrap a wiki client and shared cache behind the single fetch chokepoint."""
        self._client = client
        self._cache = cache
        self._inproc_locks: dict[str, _InprocLockSlot] = {}
        self._inproc_guard = threading.Lock()

    def get_page(
        self,
        title: str,
        *,
        ttl_seconds: int,
        refresh: bool = False,
        max_wait_s: float | None = None,
    ) -> FetchOutcome:
        """Resolve a single page (cache-first, then fetch). See :meth:`get_pages`."""
        return self.get_pages([title], ttl_seconds=ttl_seconds, refresh=refresh, max_wait_s=max_wait_s)[title]

    def get_page_section(
        self,
        title: str,
        section: int,
        *,
        ttl_seconds: int,
        refresh: bool = False,
    ) -> FetchOutcome:
        """Resolve one page section (cache-first, then ``rvsection`` fetch)."""
        key = section_cache_key(title, section)
        now = datetime.now(timezone.utc)
        entry = None if refresh else self._cache.get(key)
        if entry is not None and entry.age_seconds(now) < ttl_seconds:
            return _section_outcome(_from_entry(entry, from_cache=True), title)

        stale = entry
        with ExitStack() as stack:
            self._acquire_inproc(stack, [key], deadline=None)
            self._acquire_cross_process(stack, [key], deadline=None)

            if not refresh:
                entry = self._cache.get(key)
                if entry is not None and entry.age_seconds() < ttl_seconds:
                    return _section_outcome(_from_entry(entry, from_cache=True), title)
                if entry is not None:
                    stale = entry

            if stale is not None and not refresh and stale.revid is not None:
                current = self._client.page_revisions([title])
                if current.get(title) is not None and current[title] == stale.revid:
                    self._cache.touch(key)
                    refreshed = self._cache.get(key) or stale
                    return _section_outcome(_from_entry(refreshed, from_cache=True), title)

            fetched_at = datetime.now(timezone.utc).isoformat()
            page = self._client.fetch_page_section(title, section)
            if page.missing or page.content is None:
                return _section_outcome(_missing(title, page, fetched_at), title)

            entry = self._cache.put(
                requested_title=key,
                title=page.title,
                redirected_from=page.redirected_from,
                revid=page.revid,
                timestamp=page.timestamp,
                size=page.size,
                content=page.content,
                fetched_at=fetched_at,
            )
            return _section_outcome(_from_entry(entry, from_cache=False), title)

    def get_pages(
        self,
        titles: list[str],
        *,
        ttl_seconds: int,
        refresh: bool = False,
        max_wait_s: float | None = None,
    ) -> dict[str, FetchOutcome]:
        """Resolve many titles at once, cache-first then batched network fetch."""
        deadline = time.monotonic() + max_wait_s if max_wait_s is not None else None
        now = datetime.now(timezone.utc)
        outcomes: dict[str, FetchOutcome] = {}
        need_network: list[str] = []
        stale: dict[str, CacheEntry] = {}

        for title in dict.fromkeys(titles):  # de-dupe, preserve order
            entry = None if refresh else self._cache.get(title)
            if entry is not None and entry.age_seconds(now) < ttl_seconds:
                outcomes[title] = _from_entry(entry, from_cache=True)
            else:
                need_network.append(title)
                if entry is not None:
                    stale[title] = entry

        if need_network:
            self._resolve_network(need_network, stale, ttl_seconds, refresh, outcomes, deadline)
        return outcomes

    def _resolve_network(
        self,
        titles: list[str],
        stale: dict[str, CacheEntry],
        ttl_seconds: int,
        refresh: bool,
        outcomes: dict[str, FetchOutcome],
        deadline: float | None = None,
    ) -> None:
        timeout_remaining(deadline)

        # Re-check the cache without locks: another process may have filled it.
        still: list[str] = []
        for title in titles:
            if not refresh:
                entry = self._cache.get(title)
                if entry is not None and entry.age_seconds() < ttl_seconds:
                    outcomes[title] = _from_entry(entry, from_cache=True)
                    continue
            still.append(title)

        to_fetch = self._revalidate(still, stale, refresh, outcomes, deadline)
        self._fetch_and_store(
            sorted(to_fetch),
            outcomes,
            deadline,
            ttl_seconds=ttl_seconds,
            refresh=refresh,
        )

    def _revalidate(
        self,
        titles: list[str],
        stale: dict[str, CacheEntry],
        refresh: bool,
        outcomes: dict[str, FetchOutcome],
        deadline: float | None = None,
    ) -> set[str]:
        """Cheap revid check: unchanged stale entries are touched and served."""
        to_fetch = set(titles)
        candidates = [t for t in titles if not refresh and t in stale and stale[t].revid is not None]
        if not candidates:
            return to_fetch
        current = self._client.page_revisions(candidates, timeout=timeout_remaining(deadline))
        for title in candidates:
            entry = stale[title]
            if current.get(title) is not None and current[title] == entry.revid:
                with ExitStack() as stack:
                    self._acquire_cross_process(stack, [title], deadline)
                    self._cache.touch(title)
                    refreshed = self._cache.get(title) or entry
                outcomes[title] = _from_entry(refreshed, from_cache=True)
                to_fetch.discard(title)
        return to_fetch

    def _fetch_and_store(
        self,
        titles: list[str],
        outcomes: dict[str, FetchOutcome],
        deadline: float | None = None,
        *,
        ttl_seconds: int,
        refresh: bool,
    ) -> None:
        """Fetch and store pages; file locks are held per-title during cache write only."""
        if not titles:
            return

        leaders: list[str] = []
        inproc_stacks: list[ExitStack] = []
        try:
            for title in titles:
                timeout_remaining(deadline)
                stack = ExitStack()
                try:
                    self._acquire_inproc(stack, [title], deadline)
                    if not refresh:
                        entry = self._cache.get(title)
                        if entry is not None and entry.age_seconds() < ttl_seconds:
                            outcomes[title] = _from_entry(entry, from_cache=True)
                            stack.close()
                            continue
                    leaders.append(title)
                    inproc_stacks.append(stack)
                except BaseException:
                    stack.close()
                    raise

            if not leaders:
                return

            fetched_at = datetime.now(timezone.utc).isoformat()
            fetched = self._client.fetch_pages(leaders, timeout=timeout_remaining(deadline))

            for title, inproc_stack in zip(leaders, inproc_stacks, strict=True):
                try:
                    with ExitStack() as file_stack:
                        self._acquire_cross_process(file_stack, [title], deadline)
                        if not refresh:
                            entry = self._cache.get(title)
                            if entry is not None and entry.age_seconds() < ttl_seconds:
                                outcomes[title] = _from_entry(entry, from_cache=True)
                                continue
                        page = fetched.get(title)
                        if page is None or page.missing or page.content is None:
                            outcomes[title] = _missing(title, page, fetched_at)
                            continue
                        entry = self._cache.put(
                            requested_title=title,
                            title=page.title,
                            redirected_from=page.redirected_from,
                            revid=page.revid,
                            timestamp=page.timestamp,
                            size=page.size,
                            content=page.content,
                            fetched_at=fetched_at,
                        )
                        outcomes[title] = _from_entry(entry, from_cache=False)
                finally:
                    inproc_stack.close()
        except BaseException:
            for stack in inproc_stacks:
                stack.close()
            raise

    # -- locking helpers ----------------------------------------------------
    def _acquire_inproc(self, stack: ExitStack, titles: list[str], deadline: float | None) -> None:
        for title in titles:
            with self._inproc_guard:
                slot = self._inproc_locks.get(title)
                if slot is None:
                    slot = _InprocLockSlot(threading.Lock())
                    self._inproc_locks[title] = slot
                slot.users += 1
                lock = slot.lock
            if deadline is not None:
                remaining = timeout_remaining(deadline)
                assert remaining is not None
                if not lock.acquire(timeout=remaining):
                    raise FetchError("Page fetch timed out waiting for the wiki.")
            else:
                lock.acquire()
            stack.callback(self._release_inproc, title, lock)

    def _release_inproc(self, title: str, lock: threading.Lock) -> None:
        lock.release()
        with self._inproc_guard:
            slot = self._inproc_locks.get(title)
            if slot is None or slot.lock is not lock:
                return
            slot.users -= 1
            if slot.users == 0 and not lock.locked():
                del self._inproc_locks[title]
            self._evict_idle_inproc_locks()

    def _evict_idle_inproc_locks(self) -> None:
        """Drop unused lock slots when the map grows past its capacity bound."""
        if len(self._inproc_locks) <= _MAX_INPROC_LOCK_ENTRIES:
            return
        for key, slot in list(self._inproc_locks.items()):
            if slot.users == 0 and not slot.lock.locked():
                del self._inproc_locks[key]
            if len(self._inproc_locks) <= _MAX_INPROC_LOCK_ENTRIES:
                return

    def _acquire_cross_process(self, stack: ExitStack, titles: list[str], deadline: float | None) -> None:
        for title in titles:
            lock = FileLock(str(self._cache.lock_path(title)))
            lock_timeout = timeout_remaining(deadline) if deadline is not None else _LOCK_TIMEOUT_S
            try:
                lock.acquire(timeout=lock_timeout)
                stack.enter_context(_released(lock))
            except Timeout as exc:
                if deadline is not None:
                    raise FetchError("Page fetch timed out waiting for the wiki.") from exc
                # Another process is taking unusually long; proceed without the
                # cross-process lock rather than hang. The re-check after this
                # still prevents redundant work in the common case.
                logger.warning(
                    "Cross-process lock timeout (title_hash=%s): %s",
                    title_hash(title),
                    safe_exception_summary(exc),
                )
                continue


class _released:
    """Context manager that releases an already-acquired FileLock on exit."""

    def __init__(self, lock: FileLock) -> None:
        self._lock = lock

    def __enter__(self) -> FileLock:
        return self._lock

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


def _from_entry(entry: CacheEntry, *, from_cache: bool) -> FetchOutcome:
    return FetchOutcome(
        requested_title=entry.requested_title,
        title=entry.title,
        redirected_from=entry.redirected_from,
        revid=entry.revid,
        timestamp=entry.timestamp,
        size=entry.size,
        content=entry.content,
        fetched_at=entry.fetched_at,
        from_cache=from_cache,
        missing=False,
    )


def _section_outcome(outcome: FetchOutcome, title: str) -> FetchOutcome:
    """Restore the caller's page title (cache keys embed the section suffix)."""
    return FetchOutcome(
        requested_title=title,
        title=outcome.title,
        redirected_from=outcome.redirected_from,
        revid=outcome.revid,
        timestamp=outcome.timestamp,
        size=outcome.size,
        content=outcome.content,
        fetched_at=outcome.fetched_at,
        from_cache=outcome.from_cache,
        missing=outcome.missing,
    )


def _missing(title: str, page: FetchedPage | None, fetched_at: str) -> FetchOutcome:
    return FetchOutcome(
        requested_title=title,
        title=page.title if page else title,
        redirected_from=page.redirected_from if page else None,
        revid=None,
        timestamp=None,
        size=None,
        content=None,
        fetched_at=fetched_at,
        from_cache=False,
        missing=True,
    )
