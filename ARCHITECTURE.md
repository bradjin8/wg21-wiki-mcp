# Architecture

`wg21-wiki-mcp` is a local stdio MCP server that serves the WG21 committee
MediaWiki as a verifiable source of truth. This document explains how it is
built, the load/correctness policies it follows, what can break, and where it
could go next.

## Component map

```
server.py        FastMCP server; registers tools; lifespan logs in.
  tools.py       Tool logic (structured outputs). All page content via the fetcher.
    context.py   ServerContext: wires config + client + calendar + cache + fetcher.
      fetch.py       PageFetcher: cache-first, batched, single-flight retrieval.
        cache.py     SQLite (WAL) shared cache in ~/.isocpp.wiki/ + per-page lock paths.
        wiki_client.py  Authenticated MediaWiki client (bot/user SSO, re-login, batch).
        locks.py     EvictableLockMap: bounded per-key lock map for fetch/outlinks.
      meetings.py  MeetingCalendar: public-calendar -> meeting-aware cache TTL.
  deadlines.py   Composite timeout budgets for API/fetch/tool calls.
  log.py         Package logger factory; installs LogSafetyFilter on all package loggers.
  log_safety.py  Credential/page-content redaction for logs and auth error messages.
  models.py      Pydantic response models; re-exports error types from errors.py.
  errors.py      Error hierarchy, documented codes, and to_mcp_error() mapping.
  pagination.py  Opaque cursors + UTF-8-safe chunking.
  wikitext.py    Deterministic agenda time-slot extraction (the only content parse).
  url_hygiene.py Legacy wiki.edg.com URL rewrite/annotation at the tool boundary.
  config.py      Environment-driven configuration; re-exports ConfigError from errors.py.
  deprecation.py warn_deprecated() helper for future API removals.
```

## Request data flow

1. A tool is called (e.g. `get_page`).
2. It asks `PageFetcher` for the title(s) with the current meeting-aware TTL.
3. The fetcher serves fresh cache entries directly. For misses it takes a
   per-page lock (cross-process + in-process), re-checks the cache, cheaply
   revalidates stale-but-unchanged pages by `revid`, and batch-fetches the rest
   via the `WikiClient` (`titles=A|B|...`, up to 50 per request).
4. Results are stored verbatim and returned with provenance (URL, `oldid` URL,
   `revid`, timestamps). The tool chunks long content on UTF-8 boundaries.

## Authentication

`WikiClient` auto-selects the credential path: it tries the **bot password**
first (default), and on login failure falls back to **user SSO**
(`clientlogin`, else the headless SimpleSAMLphp form-flow). If neither works it
raises immediately. The successful path is **pinned** and reused for every later
re-login. `api()` retries transient errors and, on `readapidenied` (a dropped
session), transparently re-logs-in via the pinned path.

## Correctness: source of truth

- Page content is returned **byte-for-byte** from the wiki through fetch and
  cache; at the tool boundary legacy ``wiki.edg.com`` links in page text,
  search snippets (when opted in), and recent-change comments are rewritten to
  ``wiki.isocpp.org`` (or marked stale) before the MCP response.
- Every result carries verifiable provenance; redirects and title normalization
  are surfaced so content is never misattributed.
- Long pages are chunked only on UTF-8 boundaries; partiality is always signaled
  (`has_more`/`next_cursor`), and reassembling chunks reproduces the page exactly.
- Missing pages, fetch failures, and auth failures are distinct, explicit
  outcomes; nothing is fabricated, and `refresh=True` forces a live re-fetch.

## Parse-vs-offload policy

The server emits **structured data only when it is API-provided or mechanically
deterministic (~100%)**; everything else is returned verbatim for the calling
LLM to interpret. This was chosen after surveying the wiki's real formats across
many meetings (agendas, room tables, and page roles vary widely by year).

- Structured (safe): search results, `allpages`, namespaces, recent changes,
  page links, revision metadata. Search snippets are omitted by default; when opted in via
  `include_snippet`, they are API-generated excerpts, not verbatim page text.
- Deterministic extraction (only when the exact signal is present): agenda
  `session-start`/`session-end` ISO slots; meeting-title detection
  (`^\d{4}-\d{2} .+$`). Each reports an extraction status and degrades to "not
  found" rather than guessing.
- Never parsed into truth: room/day tables, composed schedules, slot<->group
  <->paper mapping, working-group and evening-session bodies. `get_meeting_sessions`
  therefore returns a **bundle** (deterministic time slots + relevant pages
  verbatim + provenance), and the LLM composes the schedule.
- The one sanctioned content parser is the public meeting-calendar TTL parser,
  because a misparse only changes cache freshness, never returned content.

## Shared cache and TTL

A single SQLite (WAL) database in `~/.isocpp.wiki/` is shared by all of the
user's local agents. WAL gives concurrent readers; a per-page `filelock`
provides cross-process single-flight so the same page is never fetched twice at
once. The TTL is **meeting-aware**: pages live for a week normally and are
re-checked hourly during the three-times-a-year meetings, detected from the
public meetings calendar (with a conservative bias to the short TTL on any parse
failure).

### Meeting-time performance

During a meeting window the cache TTL is one hour (vs one week normally), so
cache misses are more frequent and composite tools do more upstream work. The
server mitigates stall amplification as follows:

| Path | Normal TTL | Meeting TTL | Notes |
|------|------------|-------------|-------|
| Single-page tools (`get_page`, etc.) | cache hit: ~ms | cache hit: ~ms | Miss: one batched API call per title (≤50 titles/request). |
| `get_meeting_overview` | home page + outlink index cached with TTL | same | Home fetch and outlink discovery share one **30s** composite wait (`DEFAULT_COMPOSITE_MAX_WAIT_S`). |
| `get_meeting_sessions` | outlink index cached + page bundle | same | Outlink discovery is cached separately from page bodies; page fetch capped at **30s** total wait (`DEFAULT_COMPOSITE_MAX_WAIT_S`). |
| `WikiClient.api()` | read-only ``query`` calls may run concurrently (reader lock); retries release lock during backoff | same | Optional per-call `timeout=` bounds all retries; raises `FetchError` when exceeded. Login/re-login use an exclusive writer lock. |

**Expected latency (order of magnitude, cache-cold, typical meeting with ~40 subpages):**

- First `get_meeting_sessions` in an hour: 1–5 outlink API calls + 1 batched page fetch (often 2–8s on a healthy wiki; longer if the wiki is lagging).
- Repeat within the same TTL window: 0 outlink calls + cache hits for unchanged pages (sub-second locally).
- Concurrent read-only ``query`` calls: multiple tools may share the session in
  parallel under a reader lock. During retry backoff, other callers proceed
  because the lock is released before sleep.

Under sustained lag (`maxlag` / slow responses), per-call timeouts surface as `FETCH_ERROR` rather than blocking all tools indefinitely.

## What may break

- **SSO form drift.** Headless user login parses the SimpleSAMLphp login form;
  a markup change there breaks the user path (the bot path is unaffected). It is
  isolated in `WikiClient._saml_login`.
- **Session drops.** The wiki drops sessions on long runs (`readapidenied`);
  handled by automatic re-login, but heavy concurrent use re-authenticates more.
- **Agendas without `session-start`.** Most historical meetings lack the
  machine-readable agenda; `iso_slots` is then empty (`extraction: not_found`)
  and the raw page is bundled instead - by design.
- **Wiki format drift across years.** Room/agenda/evening formats vary; the
  server never depends on them beyond the agenda signal.
- **Public meetings-page format change.** Would degrade TTL accuracy only
  (conservative fallback keeps data fresh); override via `ISOCPP_WIKI_MEETING_WINDOWS`.

## Error contract

Every domain error is converted to a structured `McpError` / `ErrorData` with a
distinct code **before** it crosses the tool boundary. The single mapping
function lives in `errors.py`; `server._wrap()` calls it around every tool
invocation.

| Code | Name | Meaning |
|------|------|---------|
| `1` | `PAGE_NOT_FOUND` | The requested page does not exist on the wiki. |
| `2` | `AUTH_ERROR` | Authentication failed for every configured credential path. |
| `3` | `FETCH_ERROR` | A network or API error prevented retrieval after all retries. Raw `mwclient.APIError` that escapes the client layer is wrapped here too. |
| `4` | `CONFIG_ERROR` | Required server configuration is missing or invalid (credentials env vars not set). |
| `-32602` | `INVALID_PARAMS` | A pagination cursor is malformed or expired. Defined by the MCP / JSON-RPC protocol layer in `pagination.py`. |

**Safety invariants:**
- Auth-error messages are fixed strings; they never reflect the underlying
  login-exception text, which could carry credential-adjacent information.
- All messages are actionable and contain no wiki page content.
- `McpError` instances (including `INVALID_PARAMS`) pass through `_wrap` unchanged.

## Dependencies

MediaWiki HTTP/API access is delegated to [`mwclient`](https://pypi.org/project/mwclient/)
inside `wiki_client.py`. Supply-chain risk, evaluated alternatives, the
replacement boundary, and succession triggers are documented in
[docs/DEPENDENCY-RISK.md](docs/DEPENDENCY-RISK.md). CI runs a PyPI release-age
check (`scripts/check_mwclient_release_age.py`) on every push/PR.

## Future work

- Attachment/file (PDF) retrieval (currently wikitext pages only).
- Richer deterministic extraction if/when meeting formats stabilize.
- SAML ECP profile for user auth if the IdP enables it (more robust than form-flow).
- Optional negative caching of missing pages.
