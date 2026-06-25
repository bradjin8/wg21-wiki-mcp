"""Tool-level tests: outputs, provenance, pagination, fidelity, session bundle."""

from __future__ import annotations

import logging
import time

import pytest
from conftest import FakeCalendar, FakePage

from wg21_wiki_mcp import tools
from wg21_wiki_mcp.models import FetchError, PageNotFound


def _stale_outlink_fetched_at(ctx, *, extra_seconds: int = 3600) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=ctx.current_ttl() + extra_seconds)).isoformat()


def _seed_stale_outlink_cache(ctx, title: str, links: list[str]) -> str:
    import json

    key = tools.outlinks_cache_key(title)
    ctx.cache.put(
        requested_title=key,
        title=title,
        redirected_from=None,
        revid=None,
        timestamp=None,
        size=None,
        content=json.dumps(links),
        fetched_at=_stale_outlink_fetched_at(ctx),
    )
    return key


# --- search ---------------------------------------------------------------
def test_search_returns_hits_and_snippet_warning(fake_client, make_ctx):
    fake_client.search_results = [
        {
            "title": "Topic A",
            "ns": 0,
            "size": 10,
            "wordcount": 2,
            "timestamp": "2026-06-01T00:00:00Z",
            "snippet": "<b>A</b>",
        },
    ]
    ctx = make_ctx(fake_client)
    res = tools.search_wiki(ctx, "topic", limit=5)
    assert res.hits[0].title == "Topic A"
    assert res.hits[0].snippet_warning == "mediawiki_generated_not_verbatim"
    assert res.hits[0].url.endswith("title=Topic_A")
    assert res.next_cursor is None


def test_search_pagination_cursor(fake_client, make_ctx):
    fake_client.search_results = [{"title": f"T{i}", "ns": 0} for i in range(7)]
    ctx = make_ctx(fake_client)
    page1 = tools.search_wiki(ctx, "q", limit=3)
    assert len(page1.hits) == 3 and page1.next_cursor
    page2 = tools.search_wiki(ctx, "q", limit=3, cursor=page1.next_cursor)
    assert page1.hits[0].title != page2.hits[0].title


# --- get_page -------------------------------------------------------------
def test_get_page_verbatim_and_provenance(fake_client, make_ctx):
    fake_client.pages["My Page"] = FakePage("exact body \u00e9\u4e2d", 42)
    ctx = make_ctx(fake_client)
    page = tools.get_page(ctx, "My Page")
    assert page.content == "exact body \u00e9\u4e2d"  # byte-for-byte
    assert page.provenance.revid == 42
    assert page.provenance.url.endswith("title=My_Page")
    assert page.provenance.oldid_url.endswith("oldid=42")
    assert page.chunk.has_more is False


def test_get_page_fidelity_across_chunks(fake_client, make_ctx):
    body = "x\u00e9" * 500 + "\U0001f600" * 20
    fake_client.pages["P"] = FakePage(body, 1)
    ctx = make_ctx(fake_client)
    collected = ""
    cursor = None
    while True:
        page = tools.get_page(ctx, "P", max_bytes=1024, cursor=cursor)
        collected += page.content
        cursor = page.chunk.next_cursor
        if not page.chunk.has_more:
            break
    assert collected == body  # reassembled == original


def test_get_page_not_found(fake_client, make_ctx):
    ctx = make_ctx(fake_client)
    with pytest.raises(PageNotFound):
        tools.get_page(ctx, "Ghost")


def test_get_page_redirect_surfaced(fake_client, make_ctx):
    fake_client.pages["Real"] = FakePage("real body", 5)
    fake_client.redirects["Alias"] = "Real"
    ctx = make_ctx(fake_client)
    page = tools.get_page(ctx, "Alias")
    assert page.provenance.title == "Real"
    assert page.provenance.redirected_from == "Alias"
    assert page.content == "real body"


def test_get_page_normalization_surfaced(fake_client, make_ctx):
    fake_client.pages["Foo Bar"] = FakePage("body", 9)
    fake_client.normalized["Foo_Bar"] = "Foo Bar"
    ctx = make_ctx(fake_client)
    page = tools.get_page(ctx, "Foo_Bar")
    assert page.provenance.title == "Foo Bar"


def test_get_page_section(fake_client, make_ctx):
    fake_client.pages["P"] = FakePage("whole", 3)
    ctx = make_ctx(fake_client)
    page = tools.get_page(ctx, "P", section=1)
    assert page.section == 1
    assert "section 1" in page.content
    assert page.provenance.revid == 3
    assert fake_client.section_fetch_calls == 1


def test_get_page_section_cached(fake_client, make_ctx):
    fake_client.pages["P"] = FakePage("whole", 3)
    ctx = make_ctx(fake_client)
    tools.get_page(ctx, "P", section=1)
    tools.get_page(ctx, "P", section=1)
    assert fake_client.section_fetch_calls == 1


def test_get_page_section_not_found(fake_client, make_ctx):
    ctx = make_ctx(fake_client)
    with pytest.raises(PageNotFound):
        tools.get_page(ctx, "Ghost", section=2)


# --- list_pages / namespaces ---------------------------------------------
def test_list_pages_and_cursor(fake_client, make_ctx):
    fake_client.allpages = [{"title": f"Ns Page {i}", "ns": 0} for i in range(5)]
    fake_client.namespaces = {
        "0": {"*": ""},
        "4": {"*": "Project", "canonical": "Project"},
    }
    ctx = make_ctx(fake_client)
    res = tools.list_pages(ctx, 0, limit=2)
    assert len(res.pages) == 2 and res.next_cursor
    assert res.namespace_id == 0
    assert res.namespace_name == ""
    assert res.pages[0].url.endswith("title=Ns_Page_0")

    res_project = tools.list_pages(ctx, 4, limit=1)
    assert res_project.namespace_name == "Project"


def test_list_pages_namespace_lookup_failure(fake_client, make_ctx):
    fake_client.allpages = [{"title": "Page One", "ns": 0}]

    def _fail() -> dict:
        raise RuntimeError("namespace lookup timeout")

    fake_client.list_namespaces = _fail  # type: ignore[method-assign]
    ctx = make_ctx(fake_client)
    res = tools.list_pages(ctx, 0, limit=10)
    assert len(res.pages) == 1
    assert res.pages[0].title == "Page One"
    assert res.namespace_name is None


def test_list_namespaces(fake_client, make_ctx):
    fake_client.namespaces = {
        "-1": {"*": "Special"},
        "0": {"*": ""},
        "4": {"*": "Project", "canonical": "Project"},
    }
    ctx = make_ctx(fake_client)
    res = tools.list_namespaces(ctx)
    ids = {n.id for n in res}
    assert ids == {0, 4}  # negative namespaces excluded


# --- list_meetings --------------------------------------------------------
def test_list_meetings_flags_active(fake_client, make_ctx):
    fake_client.allpages = [
        {"title": "2026-06 Alpha", "ns": 0},
        {"title": "2026-03 Beta", "ns": 0},
        {"title": "Not A Meeting", "ns": 0},
    ]
    ctx = make_ctx(
        fake_client,
        calendar=FakeCalendar(
            active=True,
            mode="meeting",
            meeting_windows={"2026-06": ("2026-06-08", "2026-06-13")},
        ),
    )
    res = tools.list_meetings(ctx, limit=10)
    titles = [m.title for m in res.meetings]
    assert titles == ["2026-06 Alpha", "2026-03 Beta"]  # sorted desc, non-meetings excluded
    assert res.active_meeting == "2026-06 Alpha"
    assert res.meetings[0].is_active is True
    assert res.meetings[0].window_start == "2026-06-08"
    assert res.meetings[0].window_end == "2026-06-13"
    assert res.meetings[1].window_start is None


def test_list_meetings_none_active(fake_client, make_ctx):
    fake_client.allpages = [{"title": "2026-06 Alpha", "ns": 0}]
    ctx = make_ctx(fake_client, calendar=FakeCalendar(active=False))
    res = tools.list_meetings(ctx)
    assert res.active_meeting is None


# --- recent changes -------------------------------------------------------
def test_recent_changes(fake_client, make_ctx):
    fake_client.recent = [
        {
            "type": "edit",
            "title": "P1",
            "revid": 2,
            "old_revid": 1,
            "timestamp": "2026-06-10T00:00:00Z",
            "user": "u",
            "comment": "c",
        },
    ]
    ctx = make_ctx(fake_client)
    res = tools.get_recent_changes(ctx, limit=10)
    assert res.changes[0].title == "P1" and res.changes[0].url.endswith("title=P1")


# --- meeting overview -----------------------------------------------------
def test_meeting_overview(fake_client, make_ctx):
    fake_client.pages["2026-06 Alpha"] = FakePage("home body", 1)
    fake_client.allpages = [{"title": "2026-06 Alpha", "ns": 0}]
    fake_client.links["2026-06 Alpha"] = [
        {"title": "2026-06 Alpha:Agenda", "ns": 0},
        {"title": "Unrelated", "ns": 0},
    ]
    ctx = make_ctx(fake_client)
    res = tools.get_meeting_overview(ctx)
    assert res.meeting == "2026-06 Alpha"
    assert res.home.content == "home body"
    out_titles = [o.title for o in res.outlinks]
    assert "2026-06 Alpha:Agenda" in out_titles
    assert "Unrelated" not in out_titles  # filtered to meeting subpages


def test_meeting_overview_no_meetings_raises(fake_client, make_ctx):
    ctx = make_ctx(fake_client)
    with pytest.raises(PageNotFound, match="No meeting namespaces"):
        tools.get_meeting_overview(ctx)


# --- session bundle -------------------------------------------------------
def test_session_bundle(fake_client, make_ctx):
    agenda = (
        '<table id="agenda"><td session-start="2026-06-08T09:00+02:00" '
        'session-end="2026-06-08T10:15+02:00"><div class="info">(plenary)</div></td></table>'
    )
    fake_client.pages["2026-06 Alpha"] = FakePage("home", 1)
    fake_client.pages["2026-06 Alpha:Agenda"] = FakePage(agenda, 2)
    fake_client.pages["2026-06 Alpha:Rooms"] = FakePage("room table verbatim", 3)
    fake_client.pages["2026-06 Alpha:EWG"] = FakePage("ewg topics", 4)
    fake_client.allpages = [{"title": "2026-06 Alpha", "ns": 0}]
    fake_client.links["2026-06 Alpha"] = [
        {"title": "2026-06 Alpha:Agenda", "ns": 0},
        {"title": "2026-06 Alpha:Rooms", "ns": 0},
        {"title": "2026-06 Alpha:EWG", "ns": 0},
        {"title": "2026-06 Alpha:Ghost", "ns": 0},
    ]
    ctx = make_ctx(fake_client)
    bundle = tools.get_meeting_sessions(ctx, groups=["EWG"])

    assert bundle.meeting == "2026-06 Alpha"
    assert bundle.iso_slots_extraction == "success"
    assert bundle.iso_slots[0].start == "2026-06-08T09:00+02:00"
    assert "2026-06 Alpha:Ghost" in bundle.missing_pages

    by_title = {p.title: p for p in bundle.pages}
    # Agenda body included (it's the agenda); verbatim.
    assert by_title["2026-06 Alpha:Agenda"].role == "agenda"
    assert by_title["2026-06 Alpha:Agenda"].wikitext == agenda
    # Requested group body included.
    assert by_title["2026-06 Alpha:EWG"].role == "working_group"
    assert by_title["2026-06 Alpha:EWG"].wikitext == "ewg topics"
    # Non-requested page: manifest only (no body), still has provenance.
    rooms = by_title["2026-06 Alpha:Rooms"]
    assert rooms.role == "other"
    assert rooms.wikitext is None
    assert rooms.provenance.url.endswith("title=2026-06_Alpha:Rooms")


def test_session_bundle_manifest_only(fake_client, make_ctx):
    fake_client.pages["2026-06 Alpha"] = FakePage("home", 1)
    fake_client.pages["2026-06 Alpha:Agenda"] = FakePage("plain", 2)
    fake_client.allpages = [{"title": "2026-06 Alpha", "ns": 0}]
    fake_client.links["2026-06 Alpha"] = [{"title": "2026-06 Alpha:Agenda", "ns": 0}]
    ctx = make_ctx(fake_client)
    bundle = tools.get_meeting_sessions(ctx, include_wikitext=False)
    # No agenda signal and include_wikitext False -> manifest only.
    assert all(p.wikitext is None for p in bundle.pages)
    assert bundle.pages[0].size_bytes > 0


def test_session_bundle_no_agenda_signal(fake_client, make_ctx):
    fake_client.pages["2026-06 Alpha"] = FakePage("home", 1)
    fake_client.pages["2026-06 Alpha:Agenda"] = FakePage("== Agenda ==\nprose only", 2)
    fake_client.allpages = [{"title": "2026-06 Alpha", "ns": 0}]
    fake_client.links["2026-06 Alpha"] = [{"title": "2026-06 Alpha:Agenda", "ns": 0}]
    ctx = make_ctx(fake_client)
    bundle = tools.get_meeting_sessions(ctx)
    assert bundle.iso_slots_extraction == "not_found"
    assert bundle.iso_slots == []


def test_session_bundle_outlinks_cached_within_ttl(fake_client, make_ctx):
    """Repeated get_meeting_sessions calls reuse cached outlink discovery."""
    fake_client.pages["2026-06 Alpha"] = FakePage("home", 1)
    fake_client.pages["2026-06 Alpha:Agenda"] = FakePage("plain", 2)
    fake_client.allpages = [{"title": "2026-06 Alpha", "ns": 0}]
    fake_client.links["2026-06 Alpha"] = [{"title": "2026-06 Alpha:Agenda", "ns": 0}]
    ctx = make_ctx(fake_client, calendar=FakeCalendar(active=True, mode="meeting"))
    tools.get_meeting_sessions(ctx)
    assert fake_client.page_links_calls == 1
    tools.get_meeting_sessions(ctx)
    assert fake_client.page_links_calls == 1


def test_meeting_overview_outlinks_cached(fake_client, make_ctx):
    fake_client.pages["2026-06 Alpha"] = FakePage("home body", 1)
    fake_client.allpages = [{"title": "2026-06 Alpha", "ns": 0}]
    fake_client.links["2026-06 Alpha"] = [{"title": "2026-06 Alpha:Agenda", "ns": 0}]
    ctx = make_ctx(fake_client)
    tools.get_meeting_overview(ctx)
    assert fake_client.page_links_calls == 1
    tools.get_meeting_overview(ctx)
    assert fake_client.page_links_calls == 1


def test_cached_outlinks_stale_fallback_on_timeout(fake_client, make_ctx, monkeypatch):
    """When the composite budget is exhausted, return stale outlinks instead of blocking."""
    ctx = make_ctx(fake_client)
    stale = ["2026-06 Alpha:Cached"]
    _seed_stale_outlink_cache(ctx, "2026-06 Alpha", stale)
    fake_client.allpages = [{"title": "2026-06 Alpha", "ns": 0}]

    base = time.monotonic()

    def fake_monotonic():
        return base + 100.0

    monkeypatch.setattr("wg21_wiki_mcp.deadlines.time.monotonic", fake_monotonic)
    links = tools._cached_page_outlinks(ctx, "2026-06 Alpha", deadline=base + 0.5)
    assert links == stale
    assert fake_client.page_links_calls == 0


def test_outlinks_lock_map_bounded(fake_client, make_ctx, monkeypatch):
    monkeypatch.setattr(tools, "_MAX_OUTLINKS_LOCK_ENTRIES", 2)
    ctx = make_ctx(fake_client)
    for i in range(4):
        fake_client.links[f"2026-06 Meeting {i}"] = []
        fake_client.allpages = [{"title": f"2026-06 Meeting {i}", "ns": 0}]
        tools._cached_page_outlinks(ctx, f"2026-06 Meeting {i}")
    assert len(tools._outlinks_locks) <= 2


def test_cached_outlinks_raises_without_stale_on_timeout(fake_client, make_ctx, monkeypatch):
    ctx = make_ctx(fake_client)
    base = time.monotonic()
    monkeypatch.setattr("wg21_wiki_mcp.deadlines.time.monotonic", lambda: base + 100.0)
    with pytest.raises(FetchError, match="timed out"):
        tools._cached_page_outlinks(ctx, "2026-06 Alpha", deadline=base + 0.5)


def test_stale_outlinks_serve_logs_debug(fake_client, make_ctx, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="wg21_wiki_mcp.tools")
    ctx = make_ctx(fake_client)
    page_title = "2026-06 Alpha"
    stale = ["2026-06 Alpha:Cached"]
    _seed_stale_outlink_cache(ctx, page_title, stale)

    def fail_outlinks(*_a, **_k):
        raise FetchError("discovery timed out")

    monkeypatch.setattr(tools, "_page_outlinks", fail_outlinks)
    tools._cached_page_outlinks(ctx, page_title, deadline=time.monotonic() + 30)

    assert page_title not in caplog.text
    assert "stale outlink" in caplog.text.lower()
    assert "title_hash=" in caplog.text
    assert "discovery_timeout" in caplog.text


def test_cached_outlinks_stale_on_discovery_failure(fake_client, make_ctx, monkeypatch):
    ctx = make_ctx(fake_client)
    stale = ["2026-06 Alpha:Cached"]
    _seed_stale_outlink_cache(ctx, "2026-06 Alpha", stale)

    def fail_outlinks(*_a, **_k):
        raise FetchError("API call timed out.")

    monkeypatch.setattr(tools, "_page_outlinks", fail_outlinks)
    assert tools._cached_page_outlinks(ctx, "2026-06 Alpha", deadline=time.monotonic() + 30) == stale


def test_cached_outlinks_raises_on_discovery_failure_without_stale(fake_client, make_ctx, monkeypatch):
    ctx = make_ctx(fake_client)

    def fail_outlinks(*_a, **_k):
        raise FetchError("API call timed out.")

    monkeypatch.setattr(tools, "_page_outlinks", fail_outlinks)
    with pytest.raises(FetchError, match="timed out"):
        tools._cached_page_outlinks(ctx, "2026-06 Alpha", deadline=time.monotonic() + 30)


def test_cached_outlinks_stale_on_lock_contention(fake_client, make_ctx):
    """When the outlink lock cannot be taken in time, serve stale cache if present."""
    import threading

    ctx = make_ctx(fake_client)
    stale = ["2026-06 Alpha:Cached"]
    key = tools.outlinks_cache_key("2026-06 Alpha")
    _seed_stale_outlink_cache(ctx, "2026-06 Alpha", stale)

    held = tools._acquire_outlinks_lock(key, deadline=None)
    results: list[list[str]] = []

    def worker() -> None:
        results.append(tools._cached_page_outlinks(ctx, "2026-06 Alpha", deadline=time.monotonic() + 0.05))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)
    tools._release_outlinks_lock(key, held)
    thread.join(timeout=2)
    assert results == [stale]


def test_page_outlinks_respects_cap(fake_client, make_ctx):
    ctx = make_ctx(fake_client)
    fake_client.links["2026-06 Alpha"] = [{"title": f"2026-06 Alpha:Link{i}", "ns": 0} for i in range(20)]
    links = tools._page_outlinks(ctx, "2026-06 Alpha", cap=5)
    assert links == [f"2026-06 Alpha:Link{i}" for i in range(5)]
    assert fake_client.page_links_calls == 1


def test_page_outlinks_client_unlocked_between_pagination(fake_client, make_ctx):
    """Each pagination iteration completes before the next page_links call starts."""
    import threading

    links = [{"title": f"2026-06 Alpha:Link{i}", "ns": 0} for i in range(600)]
    fake_client.links["2026-06 Alpha"] = links
    client_lock = threading.RLock()
    first_batch_done = threading.Event()
    concurrent_ok = threading.Event()
    calls = {"n": 0}

    def locking_page_links(title: str, *, limit: int, cont: str | None, timeout: float | None = None) -> dict:
        with client_lock:
            calls["n"] += 1
            n = calls["n"]
            start = int(cont) if cont else 0
            window = links[start : start + limit]
            resp: dict = {"query": {"pages": {"1": {"links": window}}}}
            if start + limit < len(links):
                resp["continue"] = {"plcontinue": str(start + limit)}
        if n == 1:
            first_batch_done.set()
            if not concurrent_ok.wait(timeout=5):
                raise AssertionError("concurrent page_links did not run between pagination calls")
        return resp

    fake_client.page_links = locking_page_links
    ctx = make_ctx(fake_client)

    def concurrent() -> None:
        assert first_batch_done.wait(timeout=5)
        fake_client.page_links("Other Page", limit=10, cont=None)
        concurrent_ok.set()

    thread = threading.Thread(target=concurrent)
    thread.start()
    tools._page_outlinks(ctx, "2026-06 Alpha", cap=600)
    thread.join(timeout=5)
    assert calls["n"] >= 2


# --- wiki_status ----------------------------------------------------------
def test_wiki_status(fake_client, make_ctx):
    ctx = make_ctx(fake_client, calendar=FakeCalendar(active=True, mode="meeting"))
    status = tools.wiki_status(ctx)
    assert status.authenticated is True
    assert status.auth_mode == "bot"
    assert status.ttl_mode == "meeting"
    assert status.current_ttl_s == status.ttl_meeting_s
    assert status.cache_entries == 0
