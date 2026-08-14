"""Tests for legacy wiki.edg.com URL hygiene."""

from __future__ import annotations

from unittest.mock import patch

from conftest import FakePage, FakeWikiClient, make_config

from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.url_hygiene import (
    edg_url_to_wiki_title,
    extract_edg_urls,
    is_edg_discontinued_html,
    meeting_prefix_for_edg_space,
    parse_edg_view_url,
    parse_isocpp_url_from_stub,
    sanitize_legacy_edg_urls,
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
