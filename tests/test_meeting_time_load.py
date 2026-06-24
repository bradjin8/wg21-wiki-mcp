"""Meeting-time load tests: concurrent session bundles without deadlock."""

from __future__ import annotations

import threading
import time

from conftest import FakeCalendar, FakePage

from wg21_wiki_mcp import tools


def _meeting_fixture(fake_client) -> None:
    fake_client.pages["2026-06 Alpha"] = FakePage("home", 1)
    for i in range(8):
        fake_client.pages[f"2026-06 Alpha:WG{i}"] = FakePage(f"body {i}", i + 2)
    fake_client.allpages = [{"title": "2026-06 Alpha", "ns": 0}]
    fake_client.links["2026-06 Alpha"] = [{"title": f"2026-06 Alpha:WG{i}", "ns": 0} for i in range(8)]


def test_concurrent_meeting_sessions_no_deadlock(fake_client, make_ctx):
    """Parallel get_meeting_sessions calls complete; outlinks enumerated once."""
    _meeting_fixture(fake_client)
    ctx = make_ctx(fake_client, calendar=FakeCalendar(active=True, mode="meeting", ttl_meeting=3600))
    tools.get_meeting_sessions(ctx)  # warm outlink cache + page cache
    assert fake_client.page_links_calls == 1

    errors: list[BaseException] = []
    done = threading.Barrier(4)

    def worker():
        try:
            done.wait(timeout=5)
            tools.get_meeting_sessions(ctx)
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    elapsed = time.monotonic() - start

    assert not errors, errors
    assert not any(t.is_alive() for t in threads)
    assert fake_client.page_links_calls == 1  # no re-enumeration under meeting TTL
    assert elapsed < 5  # cache-warm path should not serialize excessively


def test_concurrent_meeting_sessions_cold_miss(fake_client, make_ctx):
    """Concurrent cold get_meeting_sessions calls single-flight outlink discovery."""
    _meeting_fixture(fake_client)
    ctx = make_ctx(fake_client, calendar=FakeCalendar(active=True, mode="meeting", ttl_meeting=3600))
    errors: list[BaseException] = []
    ready = threading.Barrier(4)

    def worker():
        try:
            ready.wait(timeout=5)
            tools.get_meeting_sessions(ctx)
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    elapsed = time.monotonic() - start

    assert not errors, errors
    assert not any(t.is_alive() for t in threads)
    assert fake_client.page_links_calls == 1
    assert elapsed < 10


def test_meeting_sessions_warm_path_skips_outlinks(fake_client, make_ctx):
    """After the first call, a second call within TTL does not hit page_links."""
    _meeting_fixture(fake_client)
    ctx = make_ctx(fake_client, calendar=FakeCalendar(active=True, mode="meeting"))
    tools.get_meeting_sessions(ctx, include_wikitext=False)
    links_after_first = fake_client.page_links_calls
    tools.get_meeting_sessions(ctx, include_wikitext=False)
    assert fake_client.page_links_calls == links_after_first
