# API stability policy (pre-1.0)

This package is at **0.x** and classified as **Alpha** on PyPI. Until **1.0.0**,
the public MCP surface is documented here so downstream MCP hosts and agent
configs can upgrade without silent breakage.

See also [CHANGELOG.md](CHANGELOG.md) for release-by-release notes and
[README.md](README.md#versioning-and-stability) for a short consumer summary.

## Semantic versioning in 0.x

We follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html) with
these project-specific rules while `MAJOR == 0`:

| Bump | Meaning |
| --- | --- |
| **0.Y.0** (minor) | New tools, parameters, or response fields. Existing stable items may be **deprecated** but not removed in the same release. |
| **0.Y.Z** (patch) | Bug fixes, performance, docs, and internal changes that do not alter the stable contract below. |
| **1.0.0** (future) | First release with a long-term stability promise; breaking changes require a major bump. |

During 0.x, **minor releases may introduce breaking changes to unstable tiers**
(see below). Stable-tier breaking changes require deprecation for at least one
full minor version first.

## Stability tiers

### Stable (MCP host contract)

These are safe to hard-code in MCP host configs, agent prompts, and automation.
Breaking changes require deprecation for **at least one minor version** with a
`DeprecationWarning` (via `wg21_wiki_mcp.deprecation.warn_deprecated`) and a
`### Deprecated` entry in [CHANGELOG.md](CHANGELOG.md) before removal.

| Category | Stable items |
| --- | --- |
| **Tool names** | `search_wiki`, `get_page`, `list_pages`, `list_namespaces`, `list_meetings`, `get_meeting_overview`, `get_meeting_sessions`, `get_recent_changes`, `wiki_status` |
| **Tool parameters** | Parameter **names** and **types** on each tool (see [server.py](src/wg21_wiki_mcp/server.py)). Optional parameters may gain new defaults; required parameters will not be renamed or removed without deprecation. |
| **Error codes** | Application codes `1`–`4` (`PAGE_NOT_FOUND`, `AUTH_ERROR`, `FETCH_ERROR`, `CONFIG_ERROR`) and pagination code `-32602` (`INVALID_PARAMS`). Codes are never reused for different meanings. |
| **Provenance fields** | All fields on `Provenance` ([models.py](src/wg21_wiki_mcp/models.py)): `requested_title`, `title`, `redirected_from`, `revid`, `last_modified`, `fetched_at`, `url`, `oldid_url`, `from_cache`; plus `PageContent.content` and `PageContent.section`. Together these define the verifiable-content contract. Any other **required** field on `Provenance` or `PageContent` is stable even if not named here. |
| **Env vars** | `WIKI_BOT_USERNAME`, `WIKI_BOT_PASSWORD`, `WIKI_USER_USERNAME`, `WIKI_USER_PASSWORD`, and documented optional tuning vars in [`.env.example`](.env.example). |

### Unstable (may change in any 0.x release)

Callers should treat these as implementation details. They may change in a minor
or patch release without a deprecation period.

| Category | Examples |
| --- | --- |
| **Response model internals** | Optional fields on Pydantic models, field ordering, new fields on existing models (additive changes are forward-compatible but not guaranteed to stay optional). |
| **Pagination cursors** | Opaque `next_cursor` strings — not stable across versions; do not persist long-term. |
| **Cache format** | SQLite schema, file layout under `~/.isocpp.wiki/`, TTL heuristics, lock file names. |
| **Python import paths** | Internal modules (`cache`, `fetch`, `wiki_client`, …). Only `wg21_wiki_mcp.server`, documented tool functions in `tools`, and public models/errors are intended for programmatic use. |
| **Log messages** | Wording and structure of log lines (not credentials or page content — see [SECURITY.md](SECURITY.md)). |
| **Server instructions string** | The FastMCP `instructions` text may be refined. |

### Explicitly not guaranteed pre-1.0

- Binary or on-disk cache compatibility across upgrades (cache is rebuildable).
- Identical latency or batching behavior under load.
- Identical meeting-discovery heuristics when the wiki structure changes.

## Deprecation process

1. **Mark in code** — call `warn_deprecated(...)` at the deprecated entry point
   (see [deprecation.py](src/wg21_wiki_mcp/deprecation.py)).
2. **Document in CHANGELOG** — add an entry under `### Deprecated` in the
   `[Unreleased]` section describing what to use instead.
3. **Wait one minor version** — the deprecated API remains callable (with
   warning) for at least the next **0.Y.0** release.
4. **Remove in a later minor** — delete the API and move the CHANGELOG entry to
   `### Removed`.

Deprecation warnings use Python's `DeprecationWarning` category. MCP hosts that
run the server as a subprocess may not surface them to the agent; rely on
CHANGELOG and pinned versions for operational awareness.

## Pinning recommendations

- Pin a **git tag** (e.g. `@v0.3.1`) or PyPI version in MCP host config for
  reproducible agent behavior.
- Read [CHANGELOG.md](CHANGELOG.md) before bumping the pinned version.
- Treat `[Unreleased]` on `develop` as preview-only, not production.

## After 1.0.0 (planned)

At 1.0.0 the stable tier above becomes the long-term contract. Breaking changes
to stable items will require a **major** version bump and a documented migration
path in CHANGELOG.
