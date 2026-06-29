# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release section uses these headings when applicable: `Added`, `Changed`,
`Deprecated`, `Removed`, `Fixed`, `Security`. See [STABILITY.md](STABILITY.md)
for the pre-1.0 API stability policy and deprecation timeline.

## [Unreleased]

### Added
- `docs/API.md`: rendered MCP tool reference (parameters, return types, programmatic access).
- `docs/FIRST_PYPI_PUBLISH.md`: one-time Trusted Publisher checklist for the first PyPI release.
- Meeting-time **latency gate** CI step on `ubuntu-latest` / Python 3.12 (`pytest -m latency_gate`).
- Thread-safe `get_context()` / lifespan shutdown via `_state_lock` in `server.py`.
- PyPI publish workflow (`.github/workflows/publish.yml`) on `v*` tag push via
  Trusted Publisher; `requirements-lock.txt` with CI lockfile reproducibility
  gate; README PyPI install instructions and PyPI project URL in `pyproject.toml`.
- Release supply-chain hardening: GitHub Actions and pre-commit hooks pinned to
  full SHA digests; publish workflow generates a CycloneDX SBOM, signs
  distributions with Sigstore, and attaches the SBOM plus `.sigstore.json`
  bundles to the GitHub Release; Dependabot `pin-actions` group keeps action
  SHA pins current.
- CI canary smoke gate (`canary (secrets)`) and live tier wired to the `live-wiki`
  GitHub environment for personal secrets; per-module coverage floors, coverage
  XML artifact upload, and `@pytest.mark.canary` live smoke tests.
- `CODEOWNERS`, Dependabot config (weekly pip + GitHub Actions updates), CI
  pre-commit gate, and governance/branch-protection docs in CONTRIBUTING.md.
- `STABILITY.md`: pre-1.0 API stability tiers, SemVer rules for 0.x, and
  deprecation process (minimum one minor version with warning before removal).
- `wg21_wiki_mcp.deprecation.warn_deprecated()`: `DeprecationWarning` helper
  for future deprecations.
- `Development Status :: 3 - Alpha` PyPI classifier and README versioning section.
- Meeting-time stall mitigation: cached outlink discovery for composite meeting
  tools, `PageFetcher.get_pages(max_wait_s=…)` (default 30s for
  `get_meeting_sessions`), and optional per-call `WikiClient.api(timeout=…)`.
  Documented in ARCHITECTURE.md and docs/RUNBOOK.md.
- Debug log when stale outlink index is served after lock contention or discovery
  timeout (`title_hash` only; no page titles in logs).

### Changed
- `search_wiki`: new `include_snippet` parameter (default `False`); snippets are omitted
  unless explicitly requested. `SearchHit.snippet_warning` removed; `SearchResults.include_snippet`
  documents caller opt-in. Tool and server instructions strengthened to state snippets must
  not be cited as verbatim wiki content.
- `CODEOWNERS` and `CONTRIBUTING.md`: add `@wpak-ai` as co-maintainer on all owned paths.
- `CONTRIBUTING.md`: document ruleset `wg21-wiki-mcp-protection` (replaces legacy branch-protection UI wording).

### Fixed
- `WikiClient.api()`: release the RLock before retry backoff sleep so concurrent
  tool invocations are not blocked for the full sleep duration.
- `PageFetcher._resolve_network`: revalidation and batched network fetch no longer
  hold cross-process file locks for every title at once; file locks are acquired
  per-title only during cache write, reducing lock convoys in composite tools like
  `get_meeting_sessions`.

### Deprecated

_(none)_

## [0.2.0] - 2026-06-22

### Added
- `errors.py`: centralized error hierarchy (`WikiMcpError`, `AuthError`,
  `PageNotFound`, `FetchError`, `ConfigError`) with documented application
  error codes (`PAGE_NOT_FOUND=1`, `AUTH_ERROR=2`, `FETCH_ERROR=3`,
  `CONFIG_ERROR=4`) and a `to_mcp_error()` mapping function.
- `server._wrap()`: converts every domain / transport exception to a structured
  `McpError` / `ErrorData` at the tool boundary; all nine tools go through it.
- ARCHITECTURE.md "Error contract" section enumerating codes and safety invariants.
- Adversarial and property-based offline tests (Hypothesis) for pagination
  cursors, UTF-8 chunking, wikitext slot parsing, and `WikiClient` HTTP/API
  edge paths; `hypothesis` added to the `dev` extra.
- `log.py`: library-style stdlib logging with a package `NullHandler`.
- `log_safety.py`: centralized log redaction (`register_redactions`,
  `sanitize_text`, `LogSafetyFilter`) and safe auth-error message helpers.
- Resource lifecycle: `Cache.close()` (context-manager supported),
  `WikiClient.close()`, `MeetingCalendar.close()`, and
  `ServerContext.close()` orchestrating teardown; wired into the FastMCP
  lifespan `finally` block.
- WARNING-level observability for calendar fetch/parse failure, `wiki_status`
  cache-count failure, and cross-process lock timeout (logs use `title_hash`,
  not page titles).
- `tests/test_lifecycle.py`: shutdown, lock-map, and swallowed-path log coverage.
- `PageFetcher.get_page_section()`: section reads use section-aware cache keys
  and the same cache-first / single-flight path as full-page fetches.
- SAML/SSO offline tests (`tests/test_wiki_client_saml.py`) with synthetic HTML
  fixtures under `tests/fixtures/saml/` and `responses`-mocked HTTP flows.
- CONTRIBUTING.md section on testing bot-password vs user SSO auth paths.
- `docs/RUNBOOK.md`: operator and agent-consumer troubleshooting (MCP host
  config, common failures, tool-selection guidance).
- `MeetingCalendar.window_for_meeting_title()`: maps meeting titles to public
  calendar ISO date windows.

### Changed
- `models.py` and `config.py` re-export their error types from `errors.py`;
  existing import paths are unchanged.
- `PageFetcher` in-process single-flight locks: per-title user refcount with
  capacity-bounded eviction of idle slots (fixes premature delete-on-release
  under concurrent fetches of the same title).
- `WikiClient.close()` is terminal; `login()` / `api()` reject use-after-close.
- `Cache.close()` guards against a connect/close race that could orphan SQLite
  connections.
- `ServerContext.close()` attempts all resource teardown even when one step fails.
- `get_page(section=...)` routes through `PageFetcher` instead of calling the
  wiki client directly, restoring the architectural fetch chokepoint invariant.
- `types-requests` added to the `dev` extra for mypy parity with CI.
- `list_pages` populates `PageList.namespace_name` from the namespaces API.
- `list_meetings` populates `MeetingRef.window_start` / `window_end` from the
  public meeting calendar when a year-month match exists.
- CHANGELOG 0.1.0 tool list corrected to include `list_namespaces`.

## [0.1.0] - 2026-06-12

### Added
- Initial release: local stdio MCP server for the WG21 wiki.
- Auto-selecting authentication: bot password preferred, headless user SSO
  (SimpleSAMLphp) fallback, with the working path pinned for re-login.
- Cross-process shared SQLite cache in `~/.isocpp.wiki/` with per-page locking
  and a meeting-aware TTL driven by the public meeting calendar.
- Centralized `PageFetcher` with batched (`titles=`) coalescing, single-flight
  de-duplication, and bounded concurrency.
- Tools: `search_wiki`, `get_page`, `list_pages`, `list_namespaces`,
  `list_meetings`, `get_meeting_overview`, `get_meeting_sessions`,
  `get_recent_changes`, `wiki_status`, with opaque cursor pagination.
- Verbatim, provenance-bearing responses (canonical + `oldid` URLs, `revid`).

[Unreleased]: https://github.com/cppalliance/wg21-wiki-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/cppalliance/wg21-wiki-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cppalliance/wg21-wiki-mcp/releases/tag/v0.1.0
