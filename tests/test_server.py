"""Server wiring tests (no network)."""

from __future__ import annotations

import asyncio
import threading

import pytest
from conftest import FakeCalendar, FakePage, FakeWikiClient, make_config

from wg21_wiki_mcp import server
from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp.fetch import PageFetcher


def test_all_tools_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {
        "search_wiki",
        "get_page",
        "list_pages",
        "list_namespaces",
        "list_meetings",
        "get_meeting_overview",
        "get_meeting_sessions",
        "get_recent_changes",
        "wiki_status",
    }


def test_get_context_builds_from_env(monkeypatch, tmp_path):
    with server._state_lock:
        ctx = server._state.pop("ctx", None)
    if ctx is not None:
        ctx.close()
    monkeypatch.setenv("WIKI_BOT_USERNAME", "Acct@bot")
    monkeypatch.setenv("WIKI_BOT_PASSWORD", "secret")
    monkeypatch.setenv("ISOCPP_WIKI_CACHE_DIR", str(tmp_path / "c"))
    try:
        ctx = server.get_context()
        assert ctx.config.base_url == "https://wiki.isocpp.org"
        assert server.get_context() is ctx  # cached
    finally:
        with server._state_lock:
            ctx = server._state.pop("ctx", None)
        if ctx is not None:
            ctx.close()


def test_get_context_refuses_during_shutdown(monkeypatch, tmp_path):
    with server._state_lock:
        ctx = server._state.pop("ctx", None)
        server._shutting_down = True
    if ctx is not None:
        ctx.close()
    monkeypatch.setenv("WIKI_BOT_USERNAME", "Acct@bot")
    monkeypatch.setenv("WIKI_BOT_PASSWORD", "secret")
    monkeypatch.setenv("ISOCPP_WIKI_CACHE_DIR", str(tmp_path / "c"))
    try:
        with pytest.raises(RuntimeError, match="shutting down"):
            server.get_context()
    finally:
        with server._state_lock:
            server._shutting_down = False


def test_get_context_thread_safe_initialization(monkeypatch, tmp_path):
    with server._state_lock:
        ctx = server._state.pop("ctx", None)
    if ctx is not None:
        ctx.close()
    monkeypatch.setenv("WIKI_BOT_USERNAME", "Acct@bot")
    monkeypatch.setenv("WIKI_BOT_PASSWORD", "secret")
    monkeypatch.setenv("ISOCPP_WIKI_CACHE_DIR", str(tmp_path / "c"))
    contexts: list[ServerContext] = []
    ready = threading.Barrier(8)

    def worker() -> None:
        ready.wait(timeout=5)
        contexts.append(server.get_context())

    try:
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not any(thread.is_alive() for thread in threads)
        assert len(contexts) == 8
        assert len({id(ctx) for ctx in contexts}) == 1
    finally:
        with server._state_lock:
            ctx = server._state.pop("ctx", None)
        if ctx is not None:
            ctx.close()


def _fake_ctx(tmp_path) -> ServerContext:
    client = FakeWikiClient()
    client.pages["2026-06 Alpha"] = FakePage("home", 1)
    client.allpages = [{"title": "2026-06 Alpha", "ns": 0}]
    client.search_results = [{"title": "Hit", "ns": 0, "snippet": "<b>Hit</b>"}]
    config = make_config(tmp_path)
    cache = Cache(config.cache_dir)
    return ServerContext(
        config=config,
        client=client,
        calendar=FakeCalendar(),  # type: ignore[arg-type]
        cache=cache,
        fetcher=PageFetcher(client, cache),  # type: ignore[arg-type]
    )


def test_tool_wrappers_delegate(monkeypatch, tmp_path):
    ctx = _fake_ctx(tmp_path)
    monkeypatch.setattr(server, "get_context", lambda: ctx)
    try:
        assert server.search_wiki("q").hits[0].title == "Hit"
        assert server.search_wiki("q").hits[0].snippet is None
        assert server.search_wiki("q", include_snippet=True).hits[0].snippet is not None
        assert server.get_page("2026-06 Alpha").content == "home"
        assert server.list_pages(0).pages[0].title == "2026-06 Alpha"
        assert isinstance(server.list_namespaces(), list)
        assert server.list_meetings().meetings[0].title == "2026-06 Alpha"
        assert server.get_meeting_overview().meeting == "2026-06 Alpha"
        assert server.get_meeting_sessions().meeting == "2026-06 Alpha"
        assert server.get_recent_changes().changes == []
        assert server.wiki_status().authenticated is True
    finally:
        ctx.close()


def test_lifespan_runs(monkeypatch, tmp_path):
    ctx = _fake_ctx(tmp_path)

    def _get_ctx() -> ServerContext:
        with server._state_lock:
            server._state["ctx"] = ctx
        return ctx

    monkeypatch.setattr(server, "get_context", _get_ctx)

    async def run():
        async with server._lifespan(server.mcp):
            return True

    assert asyncio.run(run()) is True


def test_wrap_passthrough_mcp_error():
    from mcp.shared.exceptions import MCPError

    from wg21_wiki_mcp.server import _wrap

    def _raise_mcp() -> None:
        raise MCPError(-32602, "bad params")

    with pytest.raises(MCPError):
        _wrap(_raise_mcp)


def test_wrap_converts_unexpected_exception():
    from mcp.shared.exceptions import MCPError

    from wg21_wiki_mcp.server import _wrap

    def _boom() -> None:
        raise ValueError("unexpected")

    with pytest.raises(MCPError):
        _wrap(_boom)


def test_main_runs_mcp_stdio_default(monkeypatch):
    monkeypatch.setenv("WIKI_BOT_USERNAME", "Acct@bot")
    monkeypatch.setenv("WIKI_BOT_PASSWORD", "secret")
    captured: dict[str, object] = {}

    def _run(transport: str = "stdio", **kwargs: object) -> None:
        captured["transport"] = transport
        captured["kwargs"] = kwargs

    monkeypatch.setattr(server.mcp, "run", _run)
    try:
        server.main()
        assert captured["transport"] == "stdio"
    finally:
        with server._state_lock:
            ctx = server._state.pop("ctx", None)
        if ctx is not None:
            ctx.close()


def test_main_selects_sse_transport(monkeypatch):
    monkeypatch.setenv("WIKI_BOT_USERNAME", "Acct@bot")
    monkeypatch.setenv("WIKI_BOT_PASSWORD", "secret")
    monkeypatch.setenv("WG21_TRANSPORT", "sse")
    monkeypatch.setenv("WG21_HTTP_PORT", "8765")
    captured: dict[str, object] = {}

    def _run(transport: str = "stdio", **kwargs: object) -> None:
        captured["transport"] = transport
        captured["kwargs"] = kwargs

    monkeypatch.setattr(server.mcp, "run", _run)
    try:
        server.main()
        assert captured["transport"] == "sse"
        assert captured["kwargs"]["port"] == 8765
    finally:
        with server._state_lock:
            ctx = server._state.pop("ctx", None)
        if ctx is not None:
            ctx.close()


def test_main_clears_primed_context_on_run_failure(monkeypatch, tmp_path):
    with server._state_lock:
        ctx = server._state.pop("ctx", None)
    if ctx is not None:
        ctx.close()
    monkeypatch.setenv("WIKI_BOT_USERNAME", "Acct@bot")
    monkeypatch.setenv("WIKI_BOT_PASSWORD", "secret")
    monkeypatch.setenv("ISOCPP_WIKI_CACHE_DIR", str(tmp_path / "c"))

    def _fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("startup failed")

    monkeypatch.setattr(server.mcp, "run", _fail)
    with pytest.raises(RuntimeError, match="startup failed"):
        server.main()
    with server._state_lock:
        assert "ctx" not in server._state


def test_main_primes_context_without_re_parsing_env(monkeypatch, tmp_path):
    with server._state_lock:
        ctx = server._state.pop("ctx", None)
    if ctx is not None:
        ctx.close()
    monkeypatch.setenv("WIKI_BOT_USERNAME", "Acct@bot")
    monkeypatch.setenv("WIKI_BOT_PASSWORD", "secret")
    monkeypatch.setenv("ISOCPP_WIKI_CACHE_DIR", str(tmp_path / "c"))
    calls = 0
    real_from_env = server.Config.from_env

    def counting_from_env(*, load_env_file: bool = True):
        nonlocal calls
        calls += 1
        return real_from_env(load_env_file=load_env_file)

    monkeypatch.setattr(server.Config, "from_env", counting_from_env)
    monkeypatch.setattr(server.mcp, "run", lambda *args, **kwargs: None)
    try:
        server.main()
        primed = server._state.get("ctx")
        assert primed is not None
        assert calls == 1
        assert server.get_context() is primed
        assert calls == 1
    finally:
        with server._state_lock:
            ctx = server._state.pop("ctx", None)
        if ctx is not None:
            ctx.close()
