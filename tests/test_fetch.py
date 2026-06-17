"""PageFetcher tests: cache-first, batching, single-flight, revalidation."""

from __future__ import annotations

import threading

import pytest
from conftest import FakePage, FakeWikiClient

from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.fetch import PageFetcher


@pytest.fixture
def fetcher_stack(tmp_path):
    client = FakeWikiClient()
    cache = Cache(tmp_path / "c")
    fetcher = PageFetcher(client, cache)
    yield fetcher, client, cache
    cache.close()


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


def test_inproc_lock_retained_while_waiter_pending(fetcher_stack):
    """A waiting thread must not lose its lock object to a new map entry."""
    fetcher, client, _ = fetcher_stack
    client.pages["P"] = FakePage("body", 1)

    inside_fetch = threading.Event()
    allow_finish = threading.Event()
    real_fetch = client.fetch_pages

    def gated_fetch(titles: list[str]):
        inside_fetch.set()
        assert allow_finish.wait(timeout=5)
        return real_fetch(titles)

    client.fetch_pages = gated_fetch  # type: ignore[method-assign]

    holder_error: list[BaseException] = []
    waiter_error: list[BaseException] = []

    def holder() -> None:
        try:
            fetcher.get_page("P", ttl_seconds=1000)
        except BaseException as exc:
            holder_error.append(exc)

    def waiter() -> None:
        try:
            fetcher.get_page("P", ttl_seconds=1000)
        except BaseException as exc:
            waiter_error.append(exc)

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert inside_fetch.wait(timeout=5)

    with fetcher._inproc_guard:
        slot = fetcher._inproc_locks["P"]
        lock_while_held = slot.lock
        assert slot.users >= 1

    waiter_thread = threading.Thread(target=waiter)
    waiter_thread.start()

    # Wait until the waiter has registered on the same in-process slot.
    for _ in range(100):
        with fetcher._inproc_guard:
            slot = fetcher._inproc_locks.get("P")
            if slot is not None and slot.users >= 2 and slot.lock is lock_while_held:
                break
        threading.Event().wait(0.01)
    else:
        raise AssertionError("waiter never joined the in-process lock slot")

    allow_finish.set()
    holder_thread.join(timeout=5)
    waiter_thread.join(timeout=5)

    assert not holder_error
    assert not waiter_error
    assert client.fetch_calls == 1


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


def test_inproc_lock_map_bounded(tmp_path, monkeypatch):
    from wg21_wiki_mcp.fetch import PageFetcher

    monkeypatch.setattr("wg21_wiki_mcp.fetch._MAX_INPROC_LOCK_ENTRIES", 2)
    client = FakeWikiClient()
    cache = Cache(tmp_path / "c")
    fetcher = PageFetcher(client, cache)
    for i in range(4):
        client.pages[f"P{i}"] = FakePage(f"b{i}", i)
        fetcher.get_page(f"P{i}", ttl_seconds=1000)
    assert len(fetcher._inproc_locks) <= 2
    cache.close()
