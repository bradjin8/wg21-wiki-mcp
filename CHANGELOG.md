# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-12

### Added
- Initial release: local stdio MCP server for the WG21 wiki.
- Auto-selecting authentication: bot password preferred, headless user SSO
  (SimpleSAMLphp) fallback, with the working path pinned for re-login.
- Cross-process shared SQLite cache in `~/.isocpp.wiki/` with per-page locking
  and a meeting-aware TTL driven by the public meeting calendar.
- Centralized `PageFetcher` with batched (`titles=`) coalescing, single-flight
  de-duplication, and bounded concurrency.
- Tools: `search_wiki`, `get_page`, `list_pages`, `list_meetings`,
  `get_meeting_overview`, `get_meeting_sessions`, `get_recent_changes`,
  `wiki_status`, with opaque cursor pagination.
- Verbatim, provenance-bearing responses (canonical + `oldid` URLs, `revid`).

[Unreleased]: https://github.com/cppalliance/wg21-wiki-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/cppalliance/wg21-wiki-mcp/releases/tag/v0.1.0
