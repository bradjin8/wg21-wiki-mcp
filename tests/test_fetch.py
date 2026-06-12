"""PageFetcher tests: cache-first, batching, single-flight, revalidation."""

from __future__ import annotations

import threading

from conftest import FakePage, FakeWikiClient

from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.fetch import PageFetcher


def _fetcher(tmp_path) -> tuple[PageFetcher, FakeWikiClient, Cache]:
    client = FakeWikiClient()
    cache = Cache(tmp_path / "c")
    return PageFetcher(client, cache), client, cache


def test_cache_miss_then_hit(tmp_path):
    fetcher, client, _ = _fetcher(tmp_path)
    client.pages["P"] = FakePage("body", 1)

    first = fetcher.get_page("P", ttl_seconds=1000)
    assert first.content == "body" and first.from_cache is False
    second = fetcher.get_page("P", ttl_seconds=1000)
    assert second.from_cache is True
    assert client.fetch_calls == 1  # second served from cache


def test_batched_coalescing(tmp_path):
    fetcher, client, _ = _fetcher(tmp_path)
    for i in range(5):
        client.pages[f"P{i}"] = FakePage(f"b{i}", i)
    out = fetcher.get_pages([f"P{i}" for i in range(5)], ttl_seconds=1000)
    assert len(out) == 5
    assert client.fetch_calls == 1  # one batched request, not five
    assert client.fetch_title_batches[0] == [f"P{i}" for i in range(5)]


def test_single_flight_dedup(tmp_path):
    fetcher, client, _ = _fetcher(tmp_path)
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


def test_refresh_bypasses_cache(tmp_path):
    fetcher, client, _ = _fetcher(tmp_path)
    client.pages["P"] = FakePage("v1", 1)
    fetcher.get_page("P", ttl_seconds=1000)
    client.pages["P"] = FakePage("v2", 2)
    out = fetcher.get_page("P", ttl_seconds=1000, refresh=True)
    assert out.content == "v2" and out.from_cache is False


def test_revalidation_unchanged_revid(tmp_path):
    fetcher, client, _ = _fetcher(tmp_path)
    client.pages["P"] = FakePage("body", 1)
    fetcher.get_page("P", ttl_seconds=0)  # store
    # ttl=0 forces staleness; revid unchanged -> revalidate, no content refetch.
    before = client.fetch_calls
    out = fetcher.get_page("P", ttl_seconds=0)
    assert out.from_cache is True
    assert client.revision_calls >= 1
    assert client.fetch_calls == before  # no extra content fetch


def test_revalidation_changed_revid_refetches(tmp_path):
    fetcher, client, _ = _fetcher(tmp_path)
    client.pages["P"] = FakePage("v1", 1)
    fetcher.get_page("P", ttl_seconds=0)
    client.pages["P"] = FakePage("v2", 2)
    before = client.fetch_calls
    out = fetcher.get_page("P", ttl_seconds=0)
    assert out.content == "v2" and out.from_cache is False
    assert client.fetch_calls == before + 1


def test_missing_page(tmp_path):
    fetcher, client, _ = _fetcher(tmp_path)
    out = fetcher.get_page("Ghost", ttl_seconds=1000)
    assert out.missing is True and out.content is None
