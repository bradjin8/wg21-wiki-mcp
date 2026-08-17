"""Tests for legacy wiki.edg.com URL hygiene."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import requests
from conftest import FakePage, FakeWikiClient, make_config

from wg21_wiki_mcp.cache import Cache, UrlRemap
from wg21_wiki_mcp.fetch import PageFetcher
from wg21_wiki_mcp.url_hygiene import (
    MAX_EDG_PROBES_PER_RESPONSE,
    STALE_URL_MARKER,
    HygieneBudget,
    _apply_replacements,
    _probe_edg_stub,
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
WEEK_S = 7 * 24 * 60 * 60

EDG_STUB_HTML = """
<html><head><title>EDG Wiki - Discontinued</title></head>
<body><h1>EDG Wiki Discontinued</h1>
<p>page now located at: <a href="https://wiki.isocpp.org/2025-11_Kona:US207">link</a></p>
</body></html>
"""


def _sanitize(content, *, config, cache, client, **kwargs):
    """Call the sanitizer with test defaults for the required plumbing arguments."""
    kwargs.setdefault("fetcher", PageFetcher(client, cache))
    kwargs.setdefault("budget", HygieneBudget())
    kwargs.setdefault("deadline", None)
    kwargs.setdefault("cache_ttl_s", WEEK_S)
    return sanitize_legacy_edg_urls(content, config=config, cache=cache, client=client, **kwargs)


def test_extract_edg_urls_unique_and_ordered():
    body = f"see {EDG_US207} and {EDG_US207} again"
    assert extract_edg_urls(body) == [EDG_US207]


def test_extract_edg_urls_strips_trailing_sentence_punctuation():
    for punctuation in ".,;:!?":
        assert extract_edg_urls(f"see {EDG_US207}{punctuation}") == [EDG_US207]
    assert extract_edg_urls(f"see {EDG_US207}...") == [EDG_US207]


def test_parse_edg_view_url():
    assert parse_edg_view_url(EDG_US207) == ("Wg21kona2025", "US207")


def test_parse_edg_view_url_rejects_bad_hosts_and_paths():
    assert parse_edg_view_url("https://wiki.example.org/bin/view/Wg21/foo") is None
    assert parse_edg_view_url("https://wiki.edg.com/other/Wg21/foo") is None
    assert parse_edg_view_url("https://wiki.edg.com/bin/view/OnlySpace") is None
    assert parse_edg_view_url("https://wiki.edg.com/bin/view//US207") is None


def test_parse_isocpp_url_from_stub_fallback_link():
    html = "EDG Wiki Discontinued <a href='https://wiki.isocpp.org/2025-11_Kona:P999'>x</a>"
    assert parse_isocpp_url_from_stub(html) == "https://wiki.isocpp.org/2025-11_Kona:P999"
    assert parse_isocpp_url_from_stub("<p>no links</p>") is None


def test_parse_isocpp_url_from_stub_ignores_distant_and_decoy_links():
    # A nav link ahead of the notice must not win over the labelled target.
    decoy = '<a href="https://wiki.isocpp.org/Main_Page">Home</a>'
    html = f"<nav>{decoy}</nav>{EDG_STUB_HTML}"
    assert parse_isocpp_url_from_stub(html) == "https://wiki.isocpp.org/2025-11_Kona:US207"

    # Without the labelled link, only hits near the notice are trusted.
    far = "EDG Wiki Discontinued" + ("x" * 2000) + ' <a href="https://wiki.isocpp.org/Main_Page">Home</a>'
    assert parse_isocpp_url_from_stub(far) is None


def test_parse_isocpp_url_from_stub_allows_extra_attributes():
    html = "page now located at: <a class='ext' href='https://wiki.isocpp.org/2025-11_Kona:US207'>go</a>"
    assert parse_isocpp_url_from_stub(html) == "https://wiki.isocpp.org/2025-11_Kona:US207"


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


def test_meeting_prefix_for_edg_space_prefers_exact_location():
    # Discovery order must not decide which same-year meeting wins.
    for meetings in (["2025-11 Kona", "2025-11 Kona Workshop"], ["2025-11 Kona Workshop", "2025-11 Kona"]):
        assert meeting_prefix_for_edg_space("Wg21kona2025", meetings) == "2025-11_Kona"


def test_meeting_prefix_for_edg_space_ambiguous_returns_none():
    # Two inexact same-year candidates: a wrong prefix names a page that may
    # exist, so the existence check cannot catch it. Fall through to a probe.
    meetings = ["2025-11 Konakai", "2025-11 Konaville"]
    assert meeting_prefix_for_edg_space("Wg21kona2025", meetings) is None
    # Two exact matches (same location, two windows in one year) are also ambiguous.
    assert meeting_prefix_for_edg_space("Wg21kona2025", ["2025-11 Kona", "2025-06 Kona"]) is None


def test_meeting_prefix_for_edg_space_single_inexact_candidate():
    assert meeting_prefix_for_edg_space("Wg21kona2025", ["2025-11 Kona Workshop"]) == "2025-11_Kona_Workshop"


def test_meeting_prefix_for_edg_space_year_filter_rejects_other_years():
    # Only the year gate separates these; without it Kona maps onto Sofia.
    assert meeting_prefix_for_edg_space("Wg21kona2025", ["2025-06 Sofia"]) is None


def test_url_remap_age_seconds():
    now = datetime.now(timezone.utc)
    fresh = UrlRemap(EDG_US207, ISOCPP_US207, now.isoformat())
    assert fresh.age_seconds() < 5
    old = UrlRemap(EDG_US207, None, (now - timedelta(hours=2)).isoformat())
    assert old.age_seconds() == pytest.approx(7200, abs=60)
    assert UrlRemap(EDG_US207, None, "not-a-date").age_seconds() == float("inf")


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


def _redirect_to(location):
    class Redirect:
        status_code = 302
        headers = {"Location": location}

    return Redirect


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/stub",  # loopback via plain http
        "https://evil.example.com/stub",  # host outside the allowlist
        "https://wiki.isocpp.org:99999/stub",  # port that makes urlparse.port raise
        "https://wiki.isocpp.org:8443/stub",  # allowlisted host, non-443 port
    ],
)
def test_probe_edg_stub_rejects_unsafe_redirect_without_second_request(location):
    with patch("wg21_wiki_mcp.url_hygiene.requests.get", return_value=_redirect_to(location)()) as mock_get:
        assert _probe_edg_stub(EDG_US207, user_agent="test", timeout=1.0) is None
        assert mock_get.call_count == 1


def test_probe_edg_stub_redirect_without_location_header():
    class NoLocation:
        status_code = 302
        headers: dict[str, str] = {}

    with patch("wg21_wiki_mcp.url_hygiene.requests.get", return_value=NoLocation()) as mock_get:
        assert _probe_edg_stub(EDG_US207, user_agent="test", timeout=1.0) is None
        assert mock_get.call_count == 1


def test_probe_edg_stub_malformed_location_is_contained():
    # urljoin raises ValueError on an unparseable IPv6 literal; the probe must
    # absorb it rather than let it escape to the tool boundary.
    with patch("wg21_wiki_mcp.url_hygiene.requests.get", return_value=_redirect_to("https://[oops/x")()):
        assert _probe_edg_stub(EDG_US207, user_agent="test", timeout=1.0) is None


def test_probe_edg_stub_redirect_loop_stops_early():
    responses = {
        "https://wiki.edg.com/a": _redirect_to("https://wiki.isocpp.org/b")(),
        "https://wiki.isocpp.org/b": _redirect_to("https://wiki.edg.com/a")(),
    }

    def fake_get(url, **_kwargs):
        return responses[url]

    with patch("wg21_wiki_mcp.url_hygiene.requests.get", side_effect=fake_get) as mock_get:
        assert _probe_edg_stub("https://wiki.edg.com/a", user_agent="test", timeout=5.0) is None
        # Loop detection stops on the hop back to an already-visited URL.
        assert mock_get.call_count == 2


def test_probe_edg_stub_hop_exhaustion():
    counter = {"n": 0}

    def fake_get(_url, **_kwargs):
        counter["n"] += 1
        return _redirect_to(f"https://wiki.isocpp.org/hop{counter['n']}")()

    with patch("wg21_wiki_mcp.url_hygiene.requests.get", side_effect=fake_get) as mock_get:
        assert _probe_edg_stub(EDG_US207, user_agent="test", timeout=30.0) is None
        assert mock_get.call_count == 11


def test_probe_edg_stub_stops_when_timeout_budget_is_spent():
    def slow_redirect(_url, **_kwargs):
        time.sleep(0.05)
        return _redirect_to("https://wiki.isocpp.org/next")()

    with patch("wg21_wiki_mcp.url_hygiene.requests.get", side_effect=slow_redirect) as mock_get:
        assert _probe_edg_stub(EDG_US207, user_agent="test", timeout=0.04) is None
        # The chain stops on the spent budget, well before the hop cap of 11.
        assert mock_get.call_count == 1


def test_probe_edg_stub_shares_one_timeout_budget_across_hops():
    seen: list[float] = []
    clock = {"t": 1000.0}

    def fake_monotonic() -> float:
        return clock["t"]

    def fake_get(_url, **kwargs):
        seen.append(kwargs["timeout"])
        clock["t"] += 1.0
        return _redirect_to(f"https://wiki.isocpp.org/hop{len(seen)}")()

    with (
        patch("wg21_wiki_mcp.url_hygiene.time.monotonic", side_effect=fake_monotonic),
        patch("wg21_wiki_mcp.url_hygiene.requests.get", side_effect=fake_get),
    ):
        _probe_edg_stub(EDG_US207, user_agent="test", timeout=5.0)
    assert seen[:3] == [5.0, 4.0, 3.0]


def test_titles_exist_empty(tmp_path):
    client = FakeWikiClient()
    with Cache(tmp_path / "c") as cache:
        fetcher = PageFetcher(client, cache)  # type: ignore[arg-type]
        assert _titles_exist(fetcher, [], ttl_seconds=WEEK_S, deadline=None) == {}


def test_titles_exist_uses_fetcher_cache(tmp_path):
    client = FakeWikiClient()
    client.pages["2025-11_Kona:US207"] = FakePage("minutes", 1)
    with Cache(tmp_path / "c") as cache:
        fetcher = PageFetcher(client, cache)  # type: ignore[arg-type]
        first = _titles_exist(fetcher, ["2025-11_Kona:US207"], ttl_seconds=WEEK_S, deadline=None)
        second = _titles_exist(fetcher, ["2025-11_Kona:US207"], ttl_seconds=WEEK_S, deadline=None)
    assert first == second == {"2025-11_Kona:US207": True}
    # The second lookup is served from the shared cache, not a second network call.
    assert client.fetch_calls == 1


def test_apply_replacements_is_single_pass():
    # A stale marker re-embeds the source URL, so a shorter source that is a
    # prefix of a longer one must not match inside the text already written.
    long_url = "https://wiki.edg.com/bin/view/Wg21kona2025/US207"
    short_url = "https://wiki.edg.com/bin/view/Wg21kona2025/US20"
    content = f"see {long_url} and {short_url}"
    out = _apply_replacements(
        content,
        {
            long_url: f"{long_url} {STALE_URL_MARKER}",
            short_url: ISOCPP_US207,
        },
    )
    assert out == f"see {long_url} {STALE_URL_MARKER} and {ISOCPP_US207}"
    assert out.count(STALE_URL_MARKER) == 1
    assert ISOCPP_US207 + "7" not in out


def test_apply_replacements_longest_first():
    content = "prefix-aa-suffix"
    out = _apply_replacements(content, {"prefix-a": "X", "prefix": "Y"})
    assert out == "Xa-suffix"
    assert _apply_replacements(content, {}) == content


def test_stale_marker_does_not_open_a_wikitext_link(tmp_path):
    config = make_config(tmp_path)
    client = FakeWikiClient()
    content = f"See [{EDG_US207} the minutes]"
    with Cache(config.cache_dir) as cache:
        cache.put_url_remap(EDG_US207, None)
        out = _sanitize(content, config=config, cache=cache, client=client)
    assert out == f"See [{EDG_US207} {STALE_URL_MARKER} the minutes]"
    assert "[[" not in out


def test_sanitize_returns_content_when_no_edg_urls(tmp_path):
    config = make_config(tmp_path)
    client = FakeWikiClient()
    with Cache(config.cache_dir) as cache:
        assert _sanitize("plain text", config=config, cache=cache, client=client) == "plain text"


def test_sanitize_uses_cached_stale_marker(tmp_path):
    config = make_config(tmp_path)
    client = FakeWikiClient()
    with Cache(config.cache_dir) as cache:
        cache.put_url_remap(EDG_US207, None)
        out = _sanitize(EDG_US207, config=config, cache=cache, client=client)
    assert out == f"{EDG_US207} {STALE_URL_MARKER}"


def test_sanitize_negative_remap_expires_with_meeting_ttl(tmp_path):
    config = make_config(tmp_path)
    client = FakeWikiClient()
    client.pages["2025-11_Kona:US207"] = FakePage("minutes", 1)
    with Cache(config.cache_dir) as cache:
        cache.put_url_remap(EDG_US207, None)
        # A one-hour meeting TTL must retire a stale verdict written a day ago.
        with patch(
            "wg21_wiki_mcp.cache._age_seconds",
            return_value=86400.0,
        ):
            out = _sanitize(
                EDG_US207,
                config=config,
                cache=cache,
                client=client,
                cache_ttl_s=3600,
                discover_meetings=lambda: ["2025-11 Kona"],
            )
    assert out == ISOCPP_US207


def test_sanitize_refresh_bypasses_remap_cache(tmp_path):
    config = make_config(tmp_path)
    client = FakeWikiClient()
    client.pages["2025-11_Kona:US207"] = FakePage("minutes", 1)
    with Cache(config.cache_dir) as cache:
        cache.put_url_remap(EDG_US207, None)
        out = _sanitize(
            EDG_US207,
            config=config,
            cache=cache,
            client=client,
            refresh=True,
            discover_meetings=lambda: ["2025-11 Kona"],
        )
    assert out == ISOCPP_US207


def test_sanitize_heuristic_missing_page_falls_through(tmp_path):
    client = FakeWikiClient()
    config = make_config(tmp_path)
    with Cache(config.cache_dir) as cache:
        with patch("wg21_wiki_mcp.url_hygiene._probe_edg_stub", return_value=None):
            out = _sanitize(
                EDG_US207,
                config=config,
                cache=cache,
                client=client,
                discover_meetings=lambda: ["2025-11 Kona"],
            )
    assert out == f"{EDG_US207} {STALE_URL_MARKER}"
    assert client.fetch_calls >= 1


def test_sanitize_probe_cap_marks_excess_stale(tmp_path):
    client = FakeWikiClient()
    config = make_config(tmp_path)
    urls = [f"https://wiki.edg.com/bin/view/Wg21unk{i}2025/P{i}" for i in range(12)]
    body = " ".join(urls)
    with Cache(config.cache_dir) as cache:
        with patch("wg21_wiki_mcp.url_hygiene._probe_edg_stub", return_value=None) as mock_probe:
            out = _sanitize(
                body,
                config=config,
                cache=cache,
                client=client,
                discover_meetings=lambda: [],
            )
    assert mock_probe.call_count == MAX_EDG_PROBES_PER_RESPONSE
    assert out.count(STALE_URL_MARKER) == 12


def test_probe_budget_is_shared_across_calls(tmp_path):
    client = FakeWikiClient()
    config = make_config(tmp_path)
    budget = HygieneBudget()
    with Cache(config.cache_dir) as cache:
        with patch("wg21_wiki_mcp.url_hygiene._probe_edg_stub", return_value=None) as mock_probe:
            for i in range(12):
                _sanitize(
                    f"https://wiki.edg.com/bin/view/Wg21unk{i}2025/P{i}",
                    config=config,
                    cache=cache,
                    client=client,
                    budget=budget,
                    discover_meetings=lambda: [],
                )
    assert mock_probe.call_count == MAX_EDG_PROBES_PER_RESPONSE


def test_sanitize_stub_url_when_title_not_on_wiki(tmp_path):
    client = FakeWikiClient()
    config = make_config(tmp_path)
    stub_url = "https://wiki.isocpp.org/2025-11_Kona:Ghost"
    with Cache(config.cache_dir) as cache:
        with patch("wg21_wiki_mcp.url_hygiene._probe_edg_stub", return_value=stub_url):
            out = _sanitize(
                EDG_US207,
                config=config,
                cache=cache,
                client=client,
                discover_meetings=lambda: [],
            )
        assert stub_url not in out
        assert out == f"{EDG_US207} {STALE_URL_MARKER}"
        cached = cache.get_url_remap(EDG_US207)
        assert cached is not None and cached.target_url is None


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
    # The predicate is the only thing stopping a live EDG page from being mined.
    assert not is_edg_discontinued_html("<html><body>US207 minutes</body></html>")


def test_parse_isocpp_url_from_stub():
    assert parse_isocpp_url_from_stub(EDG_STUB_HTML) == "https://wiki.isocpp.org/2025-11_Kona:US207"


def test_sanitize_rewrites_via_meeting_heuristic(tmp_path):
    client = FakeWikiClient()
    client.pages["2025-11_Kona:US207"] = FakePage("minutes", 1)
    client.allpages = [{"title": "2025-11 Kona", "ns": 0}]
    config = make_config(tmp_path)
    with Cache(config.cache_dir) as cache:
        with patch("wg21_wiki_mcp.url_hygiene._probe_edg_stub") as mock_probe:
            out = _sanitize(
                f"minutes at {EDG_US207}",
                config=config,
                cache=cache,
                client=client,
                discover_meetings=lambda: ["2025-11 Kona"],
            )
        # The heuristic must resolve this on its own; a fallback probe would
        # mask a broken heuristic because the stub resolves to the same title.
        mock_probe.assert_not_called()
        assert EDG_US207 not in out
        assert ISOCPP_US207 in out
        cached = cache.get_url_remap(EDG_US207)
        assert cached is not None and cached.target_url == ISOCPP_US207


def test_sanitize_uses_cached_remap_without_fetch(tmp_path):
    client = FakeWikiClient()
    config = make_config(tmp_path)
    with Cache(config.cache_dir) as cache:
        cache.put_url_remap(EDG_US207, ISOCPP_US207)
        out = _sanitize(EDG_US207, config=config, cache=cache, client=client)
    assert out == ISOCPP_US207
    assert client.fetch_calls == 0


def test_sanitize_marks_stale_when_unmapped(tmp_path):
    client = FakeWikiClient()
    config = make_config(tmp_path)
    with Cache(config.cache_dir) as cache:
        with patch("wg21_wiki_mcp.url_hygiene._probe_edg_stub", return_value=None):
            out = _sanitize(
                EDG_US207,
                config=config,
                cache=cache,
                client=client,
                discover_meetings=lambda: [],
            )
    assert out == f"{EDG_US207} {STALE_URL_MARKER}"


def test_sanitize_probes_edg_stub_when_heuristic_fails(tmp_path):
    client = FakeWikiClient()
    client.pages["2025-11_Kona:US207"] = FakePage("minutes", 1)
    config = make_config(tmp_path)

    class StubResponse:
        status_code = 200
        text = EDG_STUB_HTML

    with Cache(config.cache_dir) as cache:
        with patch("wg21_wiki_mcp.url_hygiene.requests.get", return_value=StubResponse()):
            out = _sanitize(
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
        assert entry.target_url == "https://wiki.example.org/b"
        cache.put_url_remap("https://wiki.edg.com/stale", None)
        stale = cache.get_url_remap("https://wiki.edg.com/stale")
        assert stale is not None and stale.target_url is None
