"""FastMCP stdio server exposing the WG21 wiki tools.

Run via the ``wg21-wiki-mcp`` console script (or ``python -m
wg21_wiki_mcp``). Configuration comes from the environment (see README); the
MCP host supplies it through the server's launch ``env`` block.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from . import tools
from .config import Config
from .context import ServerContext
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

_state: dict[str, ServerContext] = {}


def get_context() -> ServerContext:
    """Return the shared server context, building it from the env on first use."""
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
        _state.pop("ctx", None)


mcp = FastMCP(
    "wg21-wiki",
    instructions=(
        "Read-only access to the WG21 (ISO C++) committee wiki as a verifiable "
        "source of truth. Page content is returned verbatim with a clickable URL "
        "and revision id; treat returned text as authoritative and search snippets "
        "as non-authoritative excerpts. The server never composes meeting schedules "
        "- use get_meeting_sessions to get the raw materials and compose them yourself."
    ),
    lifespan=_lifespan,
)


@mcp.tool()
def search_wiki(query: str, limit: int = 10, namespace: int | None = None, cursor: str | None = None) -> SearchResults:
    """Full-text search the wiki. Returns titles, API snippets (non-verbatim), and URLs."""
    return tools.search_wiki(get_context(), query, limit=limit, namespace=namespace, cursor=cursor)


@mcp.tool()
def get_page(
    title: str,
    section: int | None = None,
    max_bytes: int = 49152,
    cursor: str | None = None,
    refresh: bool = False,
) -> PageContent:
    """Return verbatim wikitext for a page (or one section), with provenance; chunked if large."""
    return tools.get_page(get_context(), title, section=section, max_bytes=max_bytes, cursor=cursor, refresh=refresh)


@mcp.tool()
def list_pages(namespace: int, prefix: str | None = None, limit: int = 50, cursor: str | None = None) -> PageList:
    """Enumerate page titles in a namespace (by numeric id; see list_namespaces)."""
    return tools.list_pages(get_context(), namespace, prefix=prefix, limit=limit, cursor=cursor)


@mcp.tool()
def list_namespaces() -> list[NamespaceInfo]:
    """List the wiki's content namespaces and their numeric ids."""
    return tools.list_namespaces(get_context())


@mcp.tool()
def list_meetings(limit: int = 10, cursor: str | None = None) -> MeetingList:
    """List discovered meetings (newest first) and flag the currently active one."""
    return tools.list_meetings(get_context(), limit=limit, cursor=cursor)


@mcp.tool()
def get_meeting_overview(meeting: str | None = None) -> MeetingOverview:
    """Return a meeting's landing page (verbatim) plus its subpage outlink index.

    Defaults to the latest meeting.
    """
    return tools.get_meeting_overview(get_context(), meeting)


@mcp.tool()
def get_meeting_sessions(
    meeting: str | None = None,
    groups: list[str] | None = None,
    include_wikitext: bool = True,
    max_page_bytes: int = 8192,
) -> SessionBundle:
    """Return raw materials to compose a meeting's schedule.

    Deterministic agenda time slots plus relevant pages verbatim. The server does
    not compose a schedule; the caller composes from the bundle. Defaults to the
    latest meeting.
    """
    return tools.get_meeting_sessions(
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
    return tools.get_recent_changes(get_context(), namespace=namespace, since=since, limit=limit, cursor=cursor)


@mcp.tool()
def wiki_status() -> WikiStatus:
    """Operational status: auth path, meeting-aware TTL state, and cache stats (no wiki content)."""
    return tools.wiki_status(get_context())


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
