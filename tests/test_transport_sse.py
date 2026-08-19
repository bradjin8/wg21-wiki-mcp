"""HTTP/SSE transport prototype tests (no real network)."""

from __future__ import annotations

import asyncio
import json
import socket
import time

import anyio
from conftest import FakeCalendar, FakePage, FakeWikiClient, make_config
from mcp import ClientSession
from mcp.client.sse import sse_client

from wg21_wiki_mcp import server
from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp.fetch import PageFetcher


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_until_listening(host: str, port: int, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return
        except OSError:
            await anyio.sleep(0.05)
    raise TimeoutError(f"SSE listener not ready on {host}:{port}")


def _fake_ctx(tmp_path) -> ServerContext:
    client = FakeWikiClient()
    client.pages["2026-06 Alpha"] = FakePage("home", 1)
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


async def _run_sse_probe(monkeypatch, tmp_path) -> None:
    ctx = _fake_ctx(tmp_path)
    monkeypatch.setattr(server, "get_context", lambda: ctx)
    host = "127.0.0.1"
    port = _free_port()

    try:

        async def _serve_sse() -> None:
            await server.mcp.run_sse_async(host=host, port=port)

        async with anyio.create_task_group() as tg:
            tg.start_soon(_serve_sse)
            await _wait_until_listening(host, port)

            async with sse_client(f"http://{host}:{port}/sse", timeout=10) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    status = await session.call_tool("wiki_status", {})
                    search = await session.call_tool("search_wiki", {"query": "q"})

            tg.cancel_scope.cancel()

        assert not status.is_error
        status_payload = json.loads(status.content[0].text)
        assert status_payload["authenticated"] is True

        assert not search.is_error
        search_payload = json.loads(search.content[0].text)
        assert search_payload["hits"][0]["title"] == "Hit"
        assert search_payload["hits"][0]["snippet"] is None
    finally:
        ctx.close()


def test_sse_wiki_status_and_search_wiki(monkeypatch, tmp_path):
    """Prototype: read-only tools work over the SDK's SSE transport."""
    asyncio.run(_run_sse_probe(monkeypatch, tmp_path))
