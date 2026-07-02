"""Cache throughput benchmarks (pytest-benchmark).

Synthetic payloads only; isolated SQLite under ``tmp_path``. Run selectively::

    pytest tests/test_benchmark.py --benchmark-only
"""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from wg21_wiki_mcp.cache import Cache

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.benchmark

_BULK_ENTRIES = 100
_CONCURRENT_WORKERS = 8
# ~10 KiB generic wikitext-shaped placeholder (not real wiki content).
_WIKITEXT_10KB = "={{ synthetic | section }}\n" + ("lorem ipsum dolor sit amet " * 400)


def _put_entry(cache: Cache, title: str, *, content: str = _WIKITEXT_10KB) -> None:
    cache.put(
        requested_title=title,
        title=title,
        redirected_from=None,
        revid=1,
        timestamp="2026-06-01T00:00:00Z",
        size=len(content),
        content=content,
    )


@pytest.fixture
def bench_cache(tmp_path: Path) -> Cache:
    """Isolated cache database for a single benchmark test."""
    with Cache(tmp_path / "bench") as cache:
        yield cache


@pytest.mark.benchmark(group="cache")
def test_benchmark_cache_put_get_cycle(benchmark, bench_cache: Cache) -> None:
    """Single-entry put followed by get (hot-path read after write)."""

    def cycle() -> None:
        _put_entry(bench_cache, "Bench:Alpha")
        entry = bench_cache.get("Bench:Alpha")
        assert entry is not None

    benchmark(cycle)


@pytest.mark.benchmark(group="cache")
def test_benchmark_cache_count(benchmark, bench_cache: Cache) -> None:
    """Row count over a populated cache."""
    for i in range(_BULK_ENTRIES):
        _put_entry(bench_cache, f"Bench:Page:{i}")

    benchmark(bench_cache.count)


@pytest.mark.benchmark(group="cache")
def test_benchmark_cache_bulk_random_reads(benchmark, tmp_path: Path) -> None:
    """Bulk insert then random-access reads (cache-warm lookup path)."""
    with Cache(tmp_path / "bulk") as cache:
        for i in range(_BULK_ENTRIES):
            _put_entry(cache, f"Bench:Page:{i}")
        rng = random.Random(0)
        titles = [f"Bench:Page:{rng.randint(0, _BULK_ENTRIES - 1)}" for _ in range(50)]

        def random_reads() -> None:
            for title in titles:
                assert cache.get(title) is not None

        benchmark(random_reads)


@pytest.mark.benchmark(group="cache")
def test_benchmark_cache_concurrent_reads(benchmark, tmp_path: Path) -> None:
    """Concurrent read throughput via thread-local SQLite connections."""
    with Cache(tmp_path / "concurrent") as cache:
        for i in range(_BULK_ENTRIES):
            _put_entry(cache, f"Bench:Page:{i}")
        titles = [f"Bench:Page:{i}" for i in range(_BULK_ENTRIES)]

        def concurrent_reads() -> None:
            with ThreadPoolExecutor(max_workers=_CONCURRENT_WORKERS) as pool:
                results = list(pool.map(cache.get, titles))
            assert all(r is not None for r in results)

        benchmark(concurrent_reads)
