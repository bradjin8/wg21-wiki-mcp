"""PageFetcher tests: cache-first, batching, single-flight, revalidation."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakePage, FakeWikiClient

from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.fetch import PageFetcher
from wg21_wiki_mcp.models import FetchError


@pytest.fixture
def fetcher_stack(tmp_path):
    client = FakeWikiClient()
    cache = Cache(tmp_path / "c")
    fetcher = PageFetcher(client, cache)
    yield fetcher, client, cache
    cache.close()


def _backdate_cache_entry(cache: Cache, title: str, *, age_seconds: int = 3600) -> None:
    entry = cache.get(title)
    assert entry is not None
    old = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    cache.put(
        requested_title=entry.requested_title,
        title=entry.title,
        redirected_from=entry.redirected_from,
        revid=entry.revid,
        timestamp=entry.timestamp,
        size=entry.size,
        content=entry.content,
        fetched_at=old,
    )


def test_cache_miss_then_hit(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)

    first = fetcher.get_page("P", ttl_seconds=1000)
    assert first.content == "body" and first.from_cache is False
    second = fetcher.get_page("P", ttl_seconds=1000)
    assert second.from_cache is True
    assert client.fetch_calls == 1  # second served from cache


def test_batched_coalescing(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    for i in range(5):
        client.pages[f"P{i}"] = FakePage(f"b{i}", i)
    out = fetcher.get_pages([f"P{i}" for i in range(5)], ttl_seconds=1000)
    assert len(out) == 5
    assert client.fetch_calls == 1  # one batched request, not five
    assert client.fetch_title_batches[0] == [f"P{i}" for i in range(5)]


def test_single_flight_dedup(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)
    results = []

    def worker():
        results.append(fetcher.get_page("P", ttl_seconds=1000).content)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["body"] * 8
    assert client.fetch_calls == 1  # concurrent callers coalesced into one fetch


def test_refresh_bypasses_cache(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("v1", 1)
    fetcher.get_page("P", ttl_seconds=1000)
    client.pages["P"] = FakePage("v2", 2)
    out = fetcher.get_page("P", ttl_seconds=1000, refresh=True)
    assert out.content == "v2" and out.from_cache is False


def test_revalidation_unchanged_revid(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)
    fetcher.get_page("P", ttl_seconds=0)  # store
    # ttl=0 forces staleness; revid unchanged -> revalidate, no content refetch.
    before = client.fetch_calls
    out = fetcher.get_page("P", ttl_seconds=0)
    assert out.from_cache is True
    assert client.revision_calls >= 1
    assert client.fetch_calls == before  # no extra content fetch


def test_revalidation_changed_revid_refetches(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("v1", 1)
    fetcher.get_page("P", ttl_seconds=0)
    client.pages["P"] = FakePage("v2", 2)
    before = client.fetch_calls
    out = fetcher.get_page("P", ttl_seconds=0)
    assert out.content == "v2" and out.from_cache is False
    assert client.fetch_calls == before + 1


def test_missing_page(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    out = fetcher.get_page("Ghost", ttl_seconds=1000)
    assert out.missing is True and out.content is None


def test_section_cache_miss_then_hit(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("whole", 3)

    first = fetcher.get_page_section("P", 1, ttl_seconds=1000)
    assert first.content == "== section 1 ==\nwhole"
    assert first.from_cache is False
    assert first.requested_title == "P"

    second = fetcher.get_page_section("P", 1, ttl_seconds=1000)
    assert second.from_cache is True
    assert client.section_fetch_calls == 1


def test_section_revalidation_unchanged_revid(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)
    fetcher.get_page_section("P", 1, ttl_seconds=0)
    before = client.section_fetch_calls
    out = fetcher.get_page_section("P", 1, ttl_seconds=0)
    assert out.from_cache is True
    assert client.revision_calls >= 1
    assert client.section_fetch_calls == before


def test_section_full_page_cached_separately(fetcher_stack):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)

    fetcher.get_page("P", ttl_seconds=1000)
    fetcher.get_page_section("P", 2, ttl_seconds=1000)
    assert client.fetch_calls == 1
    assert client.section_fetch_calls == 1


def test_inproc_lock_map_bounded(tmp_path, monkeypatch):
    from wg21_wiki_mcp.fetch import PageFetcher

    monkeypatch.setattr("wg21_wiki_mcp.fetch._MAX_INPROC_LOCK_ENTRIES", 2)
    client = FakeWikiClient()
    cache = Cache(tmp_path / "c")
    fetcher = PageFetcher(client, cache)
    try:
        for i in range(4):
            client.pages[f"P{i}"] = FakePage(f"b{i}", i)
            fetcher.get_page(f"P{i}", ttl_seconds=1000)
        assert len(fetcher._inproc_locks) <= 2
    finally:
        cache.close()


def test_get_pages_max_wait_raises_when_deadline_exceeded(fetcher_stack, monkeypatch):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)
    base = time.monotonic()
    ticks = {"n": 0}

    def fake_monotonic():
        ticks["n"] += 1
        if ticks["n"] <= 2:
            return base
        return base + 100.0

    monkeypatch.setattr("wg21_wiki_mcp.fetch.time.monotonic", fake_monotonic)
    with pytest.raises(FetchError, match="timed out"):
        fetcher.get_pages(["P"], ttl_seconds=1000, max_wait_s=0.5)


def test_fetch_releases_file_locks_during_network(fetcher_stack, monkeypatch):
    """Cross-process file locks are not held for the full batched network fetch."""
    from filelock import FileLock

    fetcher, client, _ = fetcher_stack
    for i in range(10):
        client.pages[f"P{i}"] = FakePage(f"b{i}", i)

    held = {"current": 0, "max": 0}
    real_acquire = FileLock.acquire
    real_release = FileLock.release

    def tracking_acquire(self, timeout=-1):
        real_acquire(self, timeout=timeout)
        held["current"] += 1
        held["max"] = max(held["max"], held["current"])

    def tracking_release(self, force=False):
        if not force:
            held["current"] -= 1
        real_release(self)

    original_fetch = client.fetch_pages

    def slow_fetch(titles, *, timeout=None):
        assert held["max"] == 0, "file locks must not be held during network fetch"
        time.sleep(0.05)
        return original_fetch(titles, timeout=timeout)

    monkeypatch.setattr(FileLock, "acquire", tracking_acquire)
    monkeypatch.setattr(FileLock, "release", tracking_release)
    monkeypatch.setattr(client, "fetch_pages", slow_fetch)

    fetcher.get_pages([f"P{i}" for i in range(10)], ttl_seconds=1000)
    assert held["max"] == 1
    assert held["current"] == 0


def test_resolve_network_recheck_serves_fresh_cache(fetcher_stack):
    """Simulate a peer process refreshing the cache before the network path runs."""
    fetcher, client, cache = fetcher_stack
    client.pages["P"] = FakePage("body", 1)
    fetcher.get_page("P", ttl_seconds=1000)
    before = client.fetch_calls
    _backdate_cache_entry(cache, "P")

    resolve_called = False
    original = fetcher._resolve_network

    def touch_stale_then_resolve(
        titles: list[str],
        stale: dict,
        ttl_seconds: int,
        refresh: bool,
        outcomes: dict,
        deadline: float | None = None,
    ) -> None:
        nonlocal resolve_called
        resolve_called = True
        for title in titles:
            if title in stale:
                cache.touch(title)
        original(titles, stale, ttl_seconds, refresh, outcomes, deadline)

    fetcher._resolve_network = touch_stale_then_resolve  # type: ignore[method-assign]
    out = fetcher.get_page("P", ttl_seconds=1000)
    assert resolve_called
    assert out.from_cache is True
    assert client.fetch_calls == before


def test_cross_process_lock_timeout_with_deadline_raises(fetcher_stack, monkeypatch):
    from filelock import FileLock, Timeout

    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)

    def always_timeout(self, timeout=-1):
        raise Timeout(self)

    monkeypatch.setattr(FileLock, "acquire", always_timeout)
    with pytest.raises(FetchError, match="timed out"):
        fetcher.get_pages(["P"], ttl_seconds=1000, max_wait_s=30.0)


def test_inproc_lock_timeout_with_deadline_raises(fetcher_stack, monkeypatch):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)
    fetcher.get_page("P", ttl_seconds=1000)
    started = threading.Event()
    original = client.fetch_pages

    def slow_fetch(titles, *, timeout=None):
        started.set()
        time.sleep(1.0)
        return original(titles, timeout=timeout)

    monkeypatch.setattr(client, "fetch_pages", slow_fetch)

    errors: list[FetchError] = []

    def holder():
        fetcher.get_pages(["P"], ttl_seconds=1000, refresh=True)

    def waiter():
        try:
            fetcher.get_pages(["P"], ttl_seconds=1000, refresh=True, max_wait_s=0.05)
        except FetchError as exc:
            errors.append(exc)

    holder_thread = threading.Thread(target=holder)
    waiter_thread = threading.Thread(target=waiter)
    holder_thread.start()
    started.wait(timeout=2.0)
    waiter_thread.start()
    holder_thread.join(timeout=5.0)
    waiter_thread.join(timeout=5.0)
    assert len(errors) == 1
    assert "timed out" in str(errors[0]).lower()


def test_section_served_from_cache_under_lock(fetcher_stack, monkeypatch):
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)
    started = threading.Event()
    original = client.fetch_page_section

    def slow_section(title: str, section: int):
        started.set()
        time.sleep(0.5)
        return original(title, section)

    monkeypatch.setattr(client, "fetch_page_section", slow_section)

    second: list = []

    def first():
        fetcher.get_page_section("P", 1, ttl_seconds=1000)

    def waiter():
        started.wait(timeout=2.0)
        second.append(fetcher.get_page_section("P", 1, ttl_seconds=1000))

    threads = [threading.Thread(target=first), threading.Thread(target=waiter)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)
    assert second[0].from_cache is True
    assert client.section_fetch_calls == 1


def test_store_loop_serves_fresh_cache_after_network(fetcher_stack, monkeypatch):
    fetcher, client, cache = fetcher_stack
    client.pages["P"] = FakePage("body", 1)
    fetcher.get_page("P", ttl_seconds=1000)
    _backdate_cache_entry(cache, "P")
    # Force past cheap revalidation so _fetch_and_store runs fetch_pages.
    client.pages["P"] = FakePage("body", 2)
    original = client.fetch_pages

    fetch_called = False

    def touch_then_fetch(titles, *, timeout=None):
        nonlocal fetch_called
        fetch_called = True
        for title in titles:
            cache.touch(title)
        return original(titles, timeout=timeout)

    monkeypatch.setattr(client, "fetch_pages", touch_then_fetch)
    out = fetcher.get_page("P", ttl_seconds=1000)
    assert fetch_called
    assert out.from_cache is True
