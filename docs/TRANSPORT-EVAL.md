# Transport evaluation: stdio vs HTTP/SSE vs streamable HTTP

This document records the evaluation and prototype for optional HTTP transports
in `wg21-wiki-mcp`. It supports issue
[#45](https://github.com/cppalliance/wg21-wiki-mcp/issues/45).

**Decision (2026-07-01): defer production adoption; ship an opt-in prototype.**
Stdio remains the default and only supported deployment path for IDE hosts.
`sse` and `streamable-http` are gated behind `WG21_TRANSPORT` for evaluation and
future remote-deployment experiments. Production hardening (auth, TLS, rate
limiting) is tracked in the follow-up issue draft below.

## What the MCP Python SDK provides

The project pins `mcp>=1.27,<2` (`pyproject.toml`). `FastMCP.run()` accepts:

| Transport | SDK support | Default bind | Endpoints (defaults) |
|-----------|-------------|--------------|----------------------|
| `stdio` | `run_stdio_async()` | n/a (stdin/stdout) | n/a |
| `sse` | `run_sse_async(mount_path?)` via **uvicorn** + Starlette | `127.0.0.1:8000` | SSE: `/sse`, messages: `/messages/` |
| `streamable-http` | `run_streamable_http_async()` via uvicorn | `127.0.0.1:8000` | `/mcp` |

Both HTTP modes use **uvicorn** (a transitive dependency of `mcp`) and Starlette
routing. The SDK exposes host/port on `FastMCP.settings` (`host`, `port`,
`sse_path`, `message_path`, `streamable_http_path`).

**Streamable HTTP** is the newer MCP transport (replacing the older SSE split
endpoint pattern in many clients). It is available in our pinned SDK version and
is the better long-term HTTP choice when we invest in remote deployment.

**SSE** remains useful for clients that only implement the legacy SSE transport
(e.g. early `mcp-client` examples, `curl` probing of the event stream).

Out of the box the SDK also supports optional **Bearer token auth**
(`settings.auth`, `AuthenticationMiddleware`) and **DNS rebinding protection**
(`transport_security` with `allowed_hosts` / `allowed_origins` defaulting to
localhost). We do not enable auth in the prototype.

### Prototype wiring

```bash
# Default — unchanged
wg21-wiki-mcp

# Experimental HTTP (bind 127.0.0.1:8000 by default)
WG21_TRANSPORT=sse wg21-wiki-mcp
WG21_TRANSPORT=streamable-http wg21-wiki-mcp

# Optional bind overrides
WG21_HTTP_HOST=127.0.0.1 WG21_HTTP_PORT=9000 WG21_TRANSPORT=sse wg21-wiki-mcp
```

`Config.transport`, `Config.http_host`, and `Config.http_port` are read from
`WG21_TRANSPORT`, `WG21_HTTP_HOST`, and `WG21_HTTP_PORT` in `config.py`.
`server.main()` applies HTTP settings to `mcp.settings` before calling
`mcp.run(transport=...)`.

Offline tests in `tests/test_transport_sse.py` start an SSE listener with a
fake `ServerContext` and call `wiki_status` and `search_wiki` through the MCP
client SDK.

## Singleton `_state` / `get_context()` with concurrent HTTP clients

`server.py` keeps a process-global `ServerContext` in `_state`, guarded by
`_state_lock`. `get_context()` lazily builds one context from `Config.from_env()`
and caches it for the process lifetime. The `_lifespan` hook logs in once at
server start and tears the context down on shutdown.

### Implications for HTTP transports

| Concern | Stdio (today) | HTTP (prototype) |
|---------|---------------|------------------|
| Clients per process | One MCP host subprocess ≈ one client | Multiple TCP connections / MCP sessions |
| `ServerContext` | One per process — natural fit | **One shared context** for all sessions |
| Wiki session (`WikiClient`) | Single pinned auth path | Same — all clients share one MediaWiki session |
| SQLite cache | Shared (by design) | Shared (by design) |
| `get_context()` races | Mitigated by `_state_lock` (W26) | Same lock; first caller wins initialization |
| Login / re-login | Serialized via `WikiClient` RW lock | **All HTTP clients share re-login side effects** |

For **read-only tools** (`wiki_status`, `search_wiki`, `get_page`, …) a shared
context is acceptable: the cache and wiki session are already designed for
multi-agent local use (shared `~/.isocpp.wiki` cache directory).

**Risks for write-capable futures** (none today — server is read-only):

- Concurrent `login()` during lifespan startup is already serialized.
- A mid-flight `_relogin()` on `readapidenied` affects all connected clients;
  with the reader-writer lock, in-flight read queries may wait for the write
  lock during re-login.
- There is **no per-client credential isolation**: all HTTP clients inherit the
  same env-configured bot/user credentials. Multi-tenant remote hosting would
  require per-session auth and context, not this singleton.

The `_lifespan` manager runs at **process** start/stop (FastMCP invokes it when
the HTTP server boots), not per SSE connection — so we do not re-login on every
HTTP connect. That matches stdio behavior and avoids login storms.

## Security implications

| Topic | Risk | Prototype mitigation | Production requirement |
|-------|------|----------------------|------------------------|
| **Credential exposure** | Wiki passwords in env vars; HTTP exposes an attack surface on the host | Default bind `127.0.0.1`; not documented for WAN | Secrets via vault; never expose raw wiki creds to clients |
| **Transport encryption** | HTTP is cleartext | Localhost-only defaults | TLS termination (reverse proxy or uvicorn SSL) |
| **Authentication** | Anyone who can reach the port can call tools | Localhost bind; SDK DNS-rebinding defaults | MCP-layer Bearer/OAuth + network ACLs |
| **CORS** | Browser-origin clients could call the API | SDK `allowed_origins` defaults to localhost | Explicit origin allowlist if browser clients are needed |
| **Rate limiting** | Unbounded tool calls → wiki API abuse | None in prototype | Per-client or global rate limits; cache-first already helps |
| **Information disclosure** | Tools return verbatim wiki content | Same as stdio — intended for authorized users | Access control at MCP boundary |

**Confidentiality note:** Remote HTTP deployment does not change the server's
logging or caching rules, but it **does** widen who can trigger fetches. Treat
HTTP mode as carrying the same classification as handing someone your wiki
credentials.

## Recommendation

| Option | Verdict |
|--------|---------|
| **Adopt now as default** | **Reject** — stdio matches IDE MCP hosts; no user demand for default HTTP |
| **Defer with opt-in prototype** | **Accept** — implemented via `WG21_TRANSPORT`; stdio unchanged |
| **Reject HTTP entirely** | **Reject** — SDK support is mature; remote CI/team-server scenarios are plausible |

**Preferred HTTP transport when we harden:** `streamable-http` first, with `sse`
as a compatibility fallback.

## Follow-up issue (draft)

Use this text to open a tracking issue when prioritizing production HTTP
deployment:

---

**Title:** HTTP transport production hardening (auth, TLS, rate limits)

**Repository:** wg21-wiki-mcp

**Depends on:** #45 (transport evaluation + prototype)

### Problem

The opt-in `WG21_TRANSPORT=sse|streamable-http` prototype binds a local HTTP
listener with no MCP-layer authentication, no TLS, and no rate limiting. A
shared `ServerContext` serves all connected clients with one wiki credential
pair. This is adequate for localhost experiments but not for team servers,
CI gateways, or WAN exposure.

### Acceptance Criteria

- [ ] MCP-layer authentication (Bearer token or OAuth) required when
      `WG21_TRANSPORT` is not `stdio`; document token issuance for operators
- [ ] TLS documented with example reverse-proxy (nginx/caddy) or native uvicorn
      SSL configuration; cleartext WAN bind rejected by default
- [ ] Rate limiting on tool dispatch (configurable requests/minute per client id)
- [ ] Runbook section in `docs/RUNBOOK.md` for remote deployment threat model
- [ ] Live or integration test proving authenticated HTTP transport against a
      mock verifier
- [ ] Default remains `stdio`; HTTP modes stay opt-in

### Implementation Notes

- Prefer `streamable-http` as the primary hardened transport; keep `sse` if
  client compatibility still requires it
- Consider per-session `ServerContext` only if multi-tenant credentials are
  required; otherwise document single-tenant shared-context semantics
- Reuse SDK `settings.auth` and `transport_security` where possible

---

## References

- Issue [#45](https://github.com/cppalliance/wg21-wiki-mcp/issues/45)
- MCP Python SDK: [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk)
- Current stdio entry: `server.py` (`main()`, `mcp.run()` default)
- Singleton context: `server.py` (`_state`, `get_context()`, `_lifespan`)
