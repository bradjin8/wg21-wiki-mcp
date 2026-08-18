# wg21-wiki-mcp

A local [Model Context Protocol](https://modelcontextprotocol.io) (stdio) server
that gives an LLM agent read access to the **WG21 (ISO C++) committee wiki** at
`wiki.isocpp.org` as a **verifiable source of truth**.

The server authenticates with your credentials, fetches pages over the
MediaWiki API, and returns wiki text with a clickable URL and revision id. Text
is **exact** apart from one documented exception: links to the discontinued
`wiki.edg.com` host are rewritten or annotated at the tool boundary (see
[URL hygiene](#url-hygiene)). See [ARCHITECTURE.md](ARCHITECTURE.md) for design detail and
[SECURITY.md](SECURITY.md) for credential handling.

> Access requires WG21 membership. This tool stores nothing confidential in its
> source and never logs page content.

## Install

Install from [PyPI](https://pypi.org/project/wg21-wiki-mcp/) (recommended):

```bash
pipx install wg21-wiki-mcp
# or: pip install wg21-wiki-mcp==0.3.1
```

Or run without a manual install via [uv](https://docs.astral.sh/uv/):

```bash
uvx --from wg21-wiki-mcp==0.3.1 wg21-wiki-mcp
```

Install from GitHub when you need a specific commit or branch:

```bash
pipx install "git+https://github.com/cppalliance/wg21-wiki-mcp.git@v0.3.1"
# development branch:
uvx --refresh --from git+https://github.com/cppalliance/wg21-wiki-mcp.git@develop wg21-wiki-mcp
```

Or from a local clone (for development):

```bash
git clone https://github.com/cppalliance/wg21-wiki-mcp
cd wg21-wiki-mcp
pip install -e ".[dev]"
```

Requires Python 3.10+. Works on Windows, macOS, and Linux.

## Configure

The wiki base URL is fixed (`https://wiki.isocpp.org`); only credentials are
needed. Pass them in the MCP host's launch `env` block. Full host setup,
troubleshooting, and env-var reference: [docs/RUNBOOK.md](docs/RUNBOOK.md).

Minimal Cursor / `uvx` example:

```json
{
  "mcpServers": {
    "wg21-wiki": {
      "command": "uvx",
      "args": ["--from", "wg21-wiki-mcp==0.3.1", "wg21-wiki-mcp"],
      "env": {
        "WIKI_BOT_USERNAME": "YourAccount@yourbot",
        "WIKI_BOT_PASSWORD": "the-bot-password"
      }
    }
  }
}
```

See [.env.example](.env.example) for optional tuning (cache directory, TTLs,
meeting overrides, legacy URL rewrite timeouts).

### URL hygiene

Returned page text is verbatim wiki content except that discontinued
`wiki.edg.com` links are rewritten to `wiki.isocpp.org` in page bodies, bundled
meeting-session wikitext (when a body is included), optional search snippets,
and recent-change comments. A link with no known successor keeps its original
URL followed by the literal marker `(stale URL)`. Fetch and cache stay
byte-for-byte; the rewrite happens only at the tool boundary. Resolution is
best-effort and network-dependent: it is bounded by the per-response probe budget
and `ISOCPP_WIKI_URL_HYGIENE_TIMEOUT_S` (default **12** seconds). A `(stale URL)`
marker can also mean the probe budget was exhausted or hygiene was skipped after
an internal error, not only that no successor exists. Tune remap freshness with
`ISOCPP_WIKI_URL_REMAP_TTL` (default **604800** seconds / one week).

## Tools

Nine MCP tools expose search, page read, namespace browse, meeting discovery,
session bundles, recent changes, and operational status. Signatures and return
models: [docs/API.md](docs/API.md). Agent tool-selection guide:
[docs/RUNBOOK.md](docs/RUNBOOK.md#which-tool-when).

## Quickstart (programmatic)

```python
from wg21_wiki_mcp.config import Config
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp import tools

ctx = ServerContext.create(Config.from_env())

hits = tools.search_wiki(ctx, "some topic", limit=5)
page = tools.get_page(ctx, hits.hits[0].title)
print(page.provenance.url, page.provenance.revid)
print(page.content)  # sanitized wikitext (legacy EDG links rewritten)
```

## Error contract

Every tool error surfaces as a structured `McpError` with a distinct code.
Messages contain no credentials or wiki page content. Full code table and
invariants: [ARCHITECTURE.md#error-contract](ARCHITECTURE.md#error-contract).

## Versioning and stability

The package is **0.x Alpha**. Pin a version in your MCP config for reproducible
behavior. Stable tool names, parameter shapes, error codes, and provenance fields
are documented in [STABILITY.md](STABILITY.md). Release notes:
[CHANGELOG.md](CHANGELOG.md).

## Documentation

- [docs/API.md](docs/API.md) — MCP tool reference (generated from source)
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — host setup, troubleshooting, agent guide
- [ARCHITECTURE.md](ARCHITECTURE.md) — design, data flow, error contract
- [STABILITY.md](STABILITY.md) — API stability tiers and deprecation policy
- [SECURITY.md](SECURITY.md) — credential handling and confidentiality
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, tests, governance

## License

Boost Software License 1.0 — see [LICENSE](LICENSE).
