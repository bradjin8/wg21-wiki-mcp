"""Tests for legacy wiki.edg.com URL hygiene."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import requests
from conftest import FakePage, FakeWikiClient, make_config

from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.url_hygiene import (
    _MAX_EDG_PROBES_PER_PAGE,
    _apply_replacements,
    _probe_edg_stub,
    _remap_fresh,
    _titles_exist,
    edg_url_to_wiki_title,
    extract_edg_urls,
    is_edg_discontinued_html,
    meeting_prefix_for_edg_space,
    parse_edg_view_url,
    parse_isocpp_url_from_stub,
    sanitize_legacy_edg_urls,
    wiki_title_from_isocpp_url,
)

BASE = "https://wiki.example.org"
EDG_US207 = "https://wiki.edg.com/bin/view/Wg21kona2025/US207"
ISOCPP_US207 = f"{BASE}/index.php?title=2025-11_Kona:US207"

EDG_STUB_HTML = """
<html><head><title>EDG Wiki - Discontinued</title></head>
<body><h1>EDG Wiki Discontinued</h1>
<p>page now located at: <a href="https://wiki.isocpp.org/2025-11_Kona:US207">link</a></p>
</body></html>
"""


def test_extract_edg_urls_unique_and_ordered():
    body = f"see {EDG_US207} and {EDG_US207} again"
    assert extract_edg_urls(body) == [EDG_US207]


def test_parse_edg_view_url():
    assert parse_edg_view_url(EDG_US207) == ("Wg21kona2025", "US207")


def test_parse_edg_view_url_rejects_bad_hosts_and_paths():
    assert parse_edg_view_url("https://wiki.example.org/bin/view/Wg21/foo") is None
    assert parse_edg_view_url("https://wiki.edg.com/other/Wg21/foo") is None
    assert parse_edg_view_url("https://wiki.edg.com/bin/view/OnlySpace") is None
    assert parse_edg_view_url("https://wiki.edg.com/bin/view//US207") is None


def test_parse_isocpp_url_from_stub_fallback_link():
    html = "<a href='https://wiki.isocpp.org/2025-11_Kona:P999'>x</a>"
    assert parse_isocpp_url_from_stub(html) == "https://wiki.isocpp.org/2025-11_Kona:P999"
    assert parse_isocpp_url_from_stub("<p>no links</p>") is None


def test_wiki_title_from_isocpp_url():
    assert (
        wiki_title_from_isocpp_url(
            "https://wiki.isocpp.org/2025-11_Kona:US207",
            BASE,
        )
        == "2025-11_Kona:US207"
    )
    assert (
        wiki_title_from_isocpp_url(
            f"{BASE}/index.php?title=Foo%2FBar",
            BASE,
        )
        == "Foo/Bar"
    )
    assert wiki_title_from_isocpp_url("https://evil.example/x", BASE) is None
    assert wiki_title_from_isocpp_url(f"{BASE}/index.php", BASE) is None


def test_meeting_prefix_for_edg_space_no_match():
    assert meeting_prefix_for_edg_space("Wg21kona2025", ["2024-03 Tokyo"]) is None
    assert meeting_prefix_for_edg_space("NotASpace", ["2025-11 Kona"]) is None


def test_meeting_prefix_for_edg_space_multiple_candidates():
    meetings = ["2025-11 Kona", "2025-11 Kona Workshop"]
    assert meeting_prefix_for_edg_space("Wg21kona2025", meetings) == "2025-11_Kona"


def test_remap_fresh_and_stale():
    fresh = (ISOCPP_US207, datetime.now(timezone.utc).isoformat())
    assert _remap_fresh(fresh, 3600) is True
    assert _remap_fresh((None, "not-a-date"), 3600) is False
    naive = (ISOCPP_US207, "2020-01-01T00:00:00")
    assert _remap_fresh(naive, 3600) is False


def test_probe_edg_stub_paths():
    class Ok:
        status_code = 200
        text = EDG_STUB_HTML

    with patch(
        "wg21_wiki_mcp.url_hygiene.requests.get",
        side_effect=requests.Timeout("slow"),
    ):
        assert _probe_edg_stub(EDG_US207, user_agent="test", timeout=1.0) is None

    with patch("wg21_wiki_mcp.url_hygiene.requests.get", return_value=Ok()):
        assert _probe_edg_stub(EDG_US207, user_agent="test", timeout=1.0) == (
            "https://wiki.isocpp.org/2025-11_Kona:US207"
        )

    class BadStatus:
        status_code = 404
        text = ""

    with patch("wg21_wiki_mcp.url_hygiene.requests.get", return_value=BadStatus()):
        assert _probe_edg_stub(EDG_US207, user_agent="test", timeout=1.0) is None

    class NotStub:
        status_code = 200
        text = "<html>still alive</html>"

    with patch("wg21_wiki_mcp.url_hygiene.requests.get", return_value=NotStub()):
        assert _probe_edg_stub(EDG_US207, user_agent="test", timeout=1.0) is None

    class Redirect:
        status_code = 302
        headers = {"Location": "https://wiki.isocpp.org/2025-11_Kona:US207"}

    with patch(
        "wg21_wiki_mcp.url_hygiene.requests.get",
        side_effect=[Redirect(), Ok()],
    ):
        assert _probe_edg_stub(EDG_US207, user_agent="test", timeout=1.0) == (
            "https://wiki.isocpp.org/2025-11_Kona:US207"
        )

    class UnsafeRedirect:
        status_code = 302
        headers = {"Location": "http://127.0.0.1/stub"}

    with patch("wg21_wiki_mcp.url_hygiene.requests.get", return_value=UnsafeRedirect()):
        assert _probe_edg_stub(EDG_US207, user_agent="test", timeout=1.0) is None

    with patch(
        "wg21_wiki_mcp.url_hygiene.requests.get",
        side_effect=requests.Timeout("slow"),
    ):
        assert _probe_edg_stub(EDG_US207, user_agent="test", timeout=1.0) is None


def test_titles_exist_empty():
    client = FakeWikiClient()
    assert _titles_exist(client, [], deadline=None) == {}


def test_apply_replacements_longest_first():
    content = "prefix-aa-suffix"
    out = _apply_replacements(content, {"prefix-a": "X", "prefix": "Y"})
    assert out == "Xa-suffix"


def test_sanitize_returns_content_when_no_edg_urls(tmp_path):
    config = make_config(tmp_path)
    client = FakeWikiClient()
    with Cache(config.cache_dir) as cache:
        assert sanitize_legacy_edg_urls("plain text", config=config, cache=cache, client=client) == "plain text"


def test_sanitize_uses_cached_stale_marker(tmp_path):
    config = make_config(tmp_path)
    client = FakeWikiClient()
    with Cache(config.cache_dir) as cache:
        cache.put_url_remap(EDG_US207, None)
        out = sanitize_legacy_edg_urls(EDG_US207, config=config, cache=cache, client=client)
    assert out.startswith("[stale URL: ")


def test_sanitize_heuristic_missing_page_falls_through(tmp_path):
    client = FakeWikiClient()
    config = make_config(tmp_path)
    with Cache(config.cache_dir) as cache:
        with patch("wg21_wiki_mcp.url_hygiene._probe_edg_stub", return_value=None):
            out = sanitize_legacy_edg_urls(
                EDG_US207,
                config=config,
                cache=cache,
                client=client,
                discover_meetings=lambda: ["2025-11 Kona"],
            )
    assert out.startswith("[stale URL: ")
    assert client.fetch_calls >= 1


def test_sanitize_probe_cap_marks_excess_stale(tmp_path):
    client = FakeWikiClient()
    config = make_config(tmp_path)
    urls = [f"https://wiki.edg.com/bin/view/Wg21unk{i}2025/P{i}" for i in range(12)]
    body = " ".join(urls)
    with Cache(config.cache_dir) as cache:
        with patch("wg21_wiki_mcp.url_hygiene._probe_edg_stub", return_value=None) as mock_probe:
            out = sanitize_legacy_edg_urls(
                body,
                config=config,
                cache=cache,
                client=client,
                discover_meetings=lambda: [],
            )
    assert mock_probe.call_count == _MAX_EDG_PROBES_PER_PAGE
    assert out.count("[stale URL:") == 12


def test_sanitize_stub_url_when_title_not_on_wiki(tmp_path):
    client = FakeWikiClient()
    config = make_config(tmp_path)
    stub_url = "https://wiki.isocpp.org/2025-11_Kona:Ghost"
    with Cache(config.cache_dir) as cache:
        with patch("wg21_wiki_mcp.url_hygiene._probe_edg_stub", return_value=stub_url):
            out = sanitize_legacy_edg_urls(
                EDG_US207,
                config=config,
                cache=cache,
                client=client,
                discover_meetings=lambda: [],
            )
        assert stub_url not in out
        assert out.startswith("[stale URL: ")
        cached = cache.get_url_remap(EDG_US207)
        assert cached is not None and cached[0] is None


def test_edg_url_to_wiki_title_unmapped():
    assert edg_url_to_wiki_title("https://wiki.edg.com/bin/view/OnlySpace", []) is None


def test_meeting_prefix_for_edg_space():
    meetings = ["2025-11 Kona", "2024-03 Tokyo"]
    assert meeting_prefix_for_edg_space("Wg21kona2025", meetings) == "2025-11_Kona"


def test_edg_url_to_wiki_title():
    meetings = ["2025-11 Kona"]
    assert edg_url_to_wiki_title(EDG_US207, meetings) == "2025-11_Kona:US207"


def test_is_edg_discontinued_html():
    assert is_edg_discontinued_html(EDG_STUB_HTML)


def test_parse_isocpp_url_from_stub():
    assert parse_isocpp_url_from_stub(EDG_STUB_HTML) == "https://wiki.isocpp.org/2025-11_Kona:US207"


def test_sanitize_rewrites_via_meeting_heuristic(tmp_path):
    client = FakeWikiClient()
    client.pages["2025-11_Kona:US207"] = FakePage("minutes", 1)
    client.allpages = [{"title": "2025-11 Kona", "ns": 0}]
    config = make_config(tmp_path)
    with Cache(config.cache_dir) as cache:
        out = sanitize_legacy_edg_urls(
            f"minutes at {EDG_US207}",
            config=config,
            cache=cache,
            client=client,
            discover_meetings=lambda: ["2025-11 Kona"],
        )
        assert EDG_US207 not in out
        assert ISOCPP_US207 in out
        cached = cache.get_url_remap(EDG_US207)
        assert cached is not None and cached[0] == ISOCPP_US207


def test_sanitize_uses_cached_remap_without_fetch(tmp_path):
    client = FakeWikiClient()
    config = make_config(tmp_path)
    with Cache(config.cache_dir) as cache:
        cache.put_url_remap(EDG_US207, ISOCPP_US207)
        out = sanitize_legacy_edg_urls(
            EDG_US207,
            config=config,
            cache=cache,
            client=client,
        )
    assert out == ISOCPP_US207
    assert client.fetch_calls == 0


def test_sanitize_marks_stale_when_unmapped(tmp_path):
    client = FakeWikiClient()
    config = make_config(tmp_path)
    with Cache(config.cache_dir) as cache:
        with patch("wg21_wiki_mcp.url_hygiene._probe_edg_stub", return_value=None):
            out = sanitize_legacy_edg_urls(
                EDG_US207,
                config=config,
                cache=cache,
                client=client,
                discover_meetings=lambda: [],
            )
    assert out.startswith("[stale URL: ")
    assert EDG_US207 in out


def test_sanitize_probes_edg_stub_when_heuristic_fails(tmp_path):
    client = FakeWikiClient()
    client.pages["2025-11_Kona:US207"] = FakePage("minutes", 1)
    config = make_config(tmp_path)

    class StubResponse:
        status_code = 200
        text = EDG_STUB_HTML

    with Cache(config.cache_dir) as cache:
        with patch("wg21_wiki_mcp.url_hygiene.requests.get", return_value=StubResponse()):
            out = sanitize_legacy_edg_urls(
                EDG_US207,
                config=config,
                cache=cache,
                client=client,
                discover_meetings=lambda: [],
            )
    assert ISOCPP_US207 in out


def test_cache_url_remap_roundtrip(tmp_path):
    with Cache(tmp_path / "c") as cache:
        cache.put_url_remap("https://wiki.edg.com/a", "https://wiki.example.org/b")
        entry = cache.get_url_remap("https://wiki.edg.com/a")
        assert entry is not None
        assert entry[0] == "https://wiki.example.org/b"
        cache.put_url_remap("https://wiki.edg.com/stale", None)
        stale = cache.get_url_remap("https://wiki.edg.com/stale")
        assert stale is not None and stale[0] is None
