# MCP tool API reference

Stable MCP host contract for `wg21-wiki-mcp` **0.2.x**. Tool names, parameter
names/types, error codes, and provenance fields are governed by
[STABILITY.md](../STABILITY.md). FastMCP also exposes JSON schemas to MCP
clients at runtime.

All tools return structured Pydantic models (see [models.py](../src/wg21_wiki_mcp/models.py)).
Errors surface as `McpError` with application codes documented in
[README.md](../README.md#error-contract).

## `search_wiki`

Full-text search the wiki.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | `str` | required | Search string. |
| `limit` | `int` | `10` | Max hits per page of results. |
| `namespace` | `int \| None` | `None` | Restrict to a namespace id (`list_namespaces`). |
| `cursor` | `str \| None` | `None` | Opaque pagination cursor from a prior response. |

**Returns:** `SearchResults` — titles, API snippets (non-verbatim), URLs, optional `next_cursor`.

## `get_page`

Verbatim wikitext for a page or section.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `title` | `str` | required | Page title. |
| `section` | `int \| None` | `None` | Section index for partial fetch; `None` = full page. |
| `max_bytes` | `int` | `49152` | Max UTF-8 bytes per chunk. |
| `cursor` | `str \| None` | `None` | Resume a chunked response. |
| `refresh` | `bool` | `False` | Bypass cache and re-fetch from the wiki. |

**Returns:** `PageContent` — `content`, `section`, `has_more`, `next_cursor`, `provenance`.

## `list_pages`

Enumerate page titles in a namespace.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `namespace` | `int` | required | Namespace id. |
| `prefix` | `str \| None` | `None` | Title prefix filter. |
| `limit` | `int` | `50` | Page size. |
| `cursor` | `str \| None` | `None` | Pagination cursor. |

**Returns:** `PageList` — `pages[]` with `title`, `namespace`, `namespace_name`, optional `next_cursor`.

## `list_namespaces`

List content namespaces and numeric ids.

**Parameters:** none.

**Returns:** `list[NamespaceInfo]` — `id`, `name`, `canonical`.

## `list_meetings`

List discovered meetings (newest first).

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `limit` | `int` | `10` | Page size. |
| `cursor` | `str \| None` | `None` | Pagination cursor. |

**Returns:** `MeetingList` — `meetings[]` with `title`, `active`, `window_start`, `window_end`, optional `next_cursor`.

## `get_meeting_overview`

Meeting landing page plus subpage outlink index.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `meeting` | `str \| None` | `None` | Meeting title; `None` = latest meeting. |

**Returns:** `MeetingOverview` — `meeting`, `home` (`PageContent`), `outlinks[]`.

## `get_meeting_sessions`

Raw materials to compose a meeting schedule (server does not compose schedules).

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `meeting` | `str \| None` | `None` | Meeting title; `None` = latest. |
| `groups` | `list[str] \| None` | `None` | Filter working-group prefixes. |
| `include_wikitext` | `bool` | `True` | Include page bodies in the bundle. |
| `max_page_bytes` | `int` | `8192` | Per-page byte cap when `include_wikitext` is true. |

**Returns:** `SessionBundle` — `meeting`, `time_slots[]`, `pages[]`, provenance metadata.

## `get_recent_changes`

Recent edits and new pages.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `namespace` | `int \| None` | `None` | Scope to one namespace. |
| `since` | `str \| None` | `None` | ISO-8601 lower bound. |
| `limit` | `int` | `50` | Page size. |
| `cursor` | `str \| None` | `None` | Pagination cursor. |

**Returns:** `RecentChanges` — `changes[]`, optional `next_cursor`.

## `wiki_status`

Operational status without wiki content.

**Parameters:** none.

**Returns:** `WikiStatus` — `authenticated`, `auth_path`, meeting TTL mode, cache counts.

## Programmatic access

Tool implementations live in [tools.py](../src/wg21_wiki_mcp/tools.py). Build a
[ServerContext](../src/wg21_wiki_mcp/context.py) and call the same functions the
MCP layer wraps:

```python
from wg21_wiki_mcp.config import Config
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp import tools

ctx = ServerContext.create(Config.from_env())
tools.get_page(ctx, "Main Page")
```

See [README.md](../README.md#quickstart-programmatic) for a minimal example.
