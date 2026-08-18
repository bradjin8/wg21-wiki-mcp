"""FastMCP server exposing the WG21 wiki tools.

Run via the ``wg21-wiki-mcp`` console script (or ``python -m
wg21_wiki_mcp``). Configuration comes from the environment (see README); the
MCP host supplies it through the server's launch ``env`` block. The default
transport is stdio; set ``WG21_TRANSPORT=sse`` or ``streamable-http`` for an
experimental HTTP listener (see ``docs/TRANSPORT-EVAL.md``).
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError

from . import tools
from .config import DEFAULT_HTTP_HOST, DEFAULT_TRANSPORT, Config
from .context import ServerContext
from .errors import WikiMcpError, to_mcp_error
from .log import get_logger
from .models import (
    MeetingList,
    MeetingOverview,
    NamespaceInfo,
    PageContent,
    PageList,
    RecentChanges,
    SearchResults,
    SessionBundle,
    WikiStatus,
)

_T = TypeVar("_T")


def _wrap(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Call ``fn(*args, **kwargs)`` and convert any domain error to ``McpError``.

    This is the single chokepoint where ``WikiMcpError`` subclasses
    (``AuthError``, ``PageNotFound``, ``FetchError``, ``ConfigError``) and raw
    ``mwclient.APIError`` are mapped to structured ``McpError`` with a
    distinct, documented code before reaching the MCP transport layer.
    ``McpError`` instances (including cursor ``INVALID_PARAMS``) pass through
    unchanged.
    """
    try:
        return fn(*args, **kwargs)
    except McpError:
        raise
    except WikiMcpError as exc:
        raise to_mcp_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 — catch raw APIError and anything else
        raise to_mcp_error(exc) from exc


_state: dict[str, ServerContext] = {}
_state_lock = threading.Lock()
_shutting_down = False


def _prime_context(cfg: Config) -> None:
    """Seed the shared context from config already parsed at process entry."""
    with _state_lock:
        if _state.get("ctx") is None:
            _state["ctx"] = ServerContext.create(cfg)


def _drop_primed_context() -> None:
    """Close and remove a startup-primed context after a failed ``mcp.run()``."""
    with _state_lock:
        ctx = _state.pop("ctx", None)
    if ctx is not None:
        ctx.close()


def get_context() -> ServerContext:
    """Return the shared server context, building it from the env on first use."""
    with _state_lock:
        if _shutting_down:
            raise RuntimeError("Server is shutting down; cannot create a new context.")
        ctx = _state.get("ctx")
        if ctx is None:
            ctx = ServerContext.create(Config.from_env())
            _state["ctx"] = ctx
        return ctx


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[dict]:
    # Build and authenticate up front so misconfiguration fails fast at startup.
    ctx = get_context()
    ctx.login()
    try:
        yield {}
    finally:
        global _shutting_down
        with _state_lock:
            _shutting_down = True
            shutdown_ctx = _state.pop("ctx", None)
        try:
            if shutdown_ctx is not None:
                shutdown_ctx.close()
        finally:
            with _state_lock:
                _shutting_down = False


mcp = FastMCP(
    "wg21-wiki",
    instructions=(
        "Read-only access to the WG21 (ISO C++) committee wiki as a verifiable "
        "source of truth. Page content is returned as stored on the wiki, with a "
        "clickable URL and revision id, except that legacy wiki.edg.com links "
        "embedded in wikitext, search snippets, or edit comments are rewritten to "
        "wiki.isocpp.org (or annotated with the literal marker '(stale URL)') "
        "before the response is returned. Search snippets are API-generated "
        "excerpts (truncated, reformatted, with highlight markup) and must not be "
        "cited as verbatim wiki content — use get_page for authoritative text. The "
        "server never composes meeting schedules — use get_meeting_sessions to get "
        "the raw materials and compose them yourself."
    ),
    lifespan=_lifespan,
)


@mcp.tool()
def search_wiki(
    query: str,
    limit: int = 10,
    namespace: int | None = None,
    cursor: str | None = None,
    include_snippet: bool = False,
) -> SearchResults:
    """Full-text search the wiki. Returns titles and URLs.

    Snippets are API-generated excerpts (truncated, reformatted, with highlight
    markup) and must not be cited as verbatim wiki content — use ``get_page`` for
    authoritative text. Pass ``include_snippet=True`` only when you need those
    excerpts for disambiguation; they are omitted by default.
    """
    return _wrap(
        tools.search_wiki,
        get_context(),
        query,
        limit=limit,
        namespace=namespace,
        cursor=cursor,
        include_snippet=include_snippet,
    )


@mcp.tool()
def get_page(
    title: str,
    section: int | None = None,
    max_bytes: int = tools._DEFAULT_PAGE_MAX_BYTES,
    cursor: str | None = None,
    refresh: bool = False,
) -> PageContent:
    """Wikitext for a page (or section); legacy wiki.edg.com links rewritten or marked ``(stale URL)``."""
    return _wrap(
        tools.get_page,
        get_context(),
        title,
        section=section,
        max_bytes=max_bytes,
        cursor=cursor,
        refresh=refresh,
    )


@mcp.tool()
def list_pages(namespace: int, prefix: str | None = None, limit: int = 50, cursor: str | None = None) -> PageList:
    """Enumerate page titles in a namespace (by numeric id; see list_namespaces)."""
    return _wrap(tools.list_pages, get_context(), namespace, prefix=prefix, limit=limit, cursor=cursor)


@mcp.tool()
def list_namespaces() -> list[NamespaceInfo]:
    """List the wiki's content namespaces and their numeric ids."""
    return _wrap(tools.list_namespaces, get_context())


@mcp.tool()
def list_meetings(limit: int = 10, cursor: str | None = None) -> MeetingList:
    """List discovered meetings (newest first) and flag the currently active one."""
    return _wrap(tools.list_meetings, get_context(), limit=limit, cursor=cursor)


@mcp.tool()
def get_meeting_overview(meeting: str | None = None) -> MeetingOverview:
    """Return a meeting's landing page plus its subpage outlink index.

    Defaults to the latest meeting.
    """
    return _wrap(tools.get_meeting_overview, get_context(), meeting)


@mcp.tool()
def get_meeting_sessions(
    meeting: str | None = None,
    groups: list[str] | None = None,
    include_wikitext: bool = True,
    max_page_bytes: int = tools._DEFAULT_BUNDLE_PAGE_MAX_BYTES,
) -> SessionBundle:
    """Schedule bundle; bundled wikitext may have legacy wiki.edg.com links rewritten or marked ``(stale URL)``."""
    return _wrap(
        tools.get_meeting_sessions,
        get_context(),
        meeting,
        groups=groups,
        include_wikitext=include_wikitext,
        max_page_bytes=max_page_bytes,
    )


@mcp.tool()
def get_recent_changes(
    namespace: int | None = None,
    since: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> RecentChanges:
    """Recent edits and new pages, optionally scoped to a namespace or since an ISO timestamp."""
    return _wrap(tools.get_recent_changes, get_context(), namespace=namespace, since=since, limit=limit, cursor=cursor)


@mcp.tool()
def wiki_status() -> WikiStatus:
    """Operational status: auth path, meeting-aware TTL state, and cache stats (no wiki content)."""
    return _wrap(tools.wiki_status, get_context())


def main() -> None:
    """Console-script entry point: run the MCP server (stdio by default)."""
    cfg = Config.from_env()
    _prime_context(cfg)
    try:
        if cfg.transport == DEFAULT_TRANSPORT:
            mcp.run()
            return
        if cfg.http_host != DEFAULT_HTTP_HOST:
            get_logger("server").warning(
                "HTTP listener binding to %s (not %s); shared ServerContext is exposed beyond localhost",
                cfg.http_host,
                DEFAULT_HTTP_HOST,
            )
        mcp.settings.host = cfg.http_host
        mcp.settings.port = cfg.http_port
        mcp.run(transport=cfg.transport)
    except BaseException:
        _drop_primed_context()
        raise


if __name__ == "__main__":
    main()
