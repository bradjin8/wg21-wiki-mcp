# Dependency risk: mwclient

This document records the supply-chain posture of [`mwclient`](https://pypi.org/project/mwclient/),
the sole third-party abstraction for MediaWiki API access in `WikiClient`
(`wiki_client.py`). It supports issue [#43](https://github.com/cppalliance/wg21-wiki-mcp/issues/43).

**Decision (2026-06-30): keep `mwclient` for now.** The library still matches our
read-only, authenticated API needs with minimal surface area. Migration cost is high
relative to current risk; monitoring and a documented succession boundary are the
mitigation. Revisit when a pinned release exceeds 24 months, a blocking CVE appears,
or MediaWiki API changes break our live tier.

## Upstream status

Figures below were verified on **2026-06-30** against PyPI and GitHub.

| Signal | Value |
|--------|-------|
| **Pinned version** | `0.11.0` (`pyproject.toml`: `>=0.11.0,<0.12`) |
| **Latest PyPI release** | `0.11.0` — **2024-08-12** (~22 months before this review) |
| **Last GitHub commit** | 2026-04-08 on `master` ([mwclient/mwclient](https://github.com/mwclient/mwclient)) |
| **Open issues / PRs** | 34 (GitHub search) |
| **Maintainer activity** | Low release cadence (5-year gap 0.10.1 → 0.11.0); intermittent commits; [1.0.0 milestone](https://github.com/mwclient/mwclient/issues/284) open since 2023 |
| **Known CVEs (mwclient)** | None listed in PyPI/OSV for `0.11.0` as of this review |
| **Transitive deps** | `requests` (already a direct dependency) |

### Risk summary

| Risk | Severity | Notes |
|------|----------|-------|
| Stale PyPI release | **Medium** | Security or API fixes may exist only on `master`; we install from PyPI via lockfile |
| Low bus factor | **Medium** | Small maintainer pool; long gaps between releases |
| API drift | **Low–Medium** | WG21 wiki is read-only; we use stable Action API query modules |
| Credential handling | **Low** | Credentials live in env vars; `mwclient` uses HTTPS via `requests` (0.11.0+) |

CI runs `scripts/check_mwclient_release_age.py` on every push/PR in two steps:

| Step | Threshold | Blocking? | Purpose |
|------|-----------|-----------|---------|
| **12-month review signal** | `--max-age-days 365 --fail-reason review` | No (`continue-on-error: true`) | Surfaces the quarterly review trigger from the succession table; currently **fails** (~22 months since `0.11.0`) but does not block merges |
| **24-month hard trigger** | `--max-age-days 730 --fail-reason hard` | **Yes** | Blocks merges when the documented migration boundary is breached (release age > 24 months or equivalent policy) |

A blocking CVE on the pinned release would also force migration per the succession table;
that case is handled by maintainer review and Dependabot/OSV monitoring until an
automated CVE gate is added. The hard age gate is **green** today (`0.11.0` is under
24 months).

Dependabot opens weekly runtime-dependency PRs (`/.github/dependabot.yml`), which
surface new `mwclient` releases when they ship.

## Alternatives evaluated

### 1. Keep `mwclient` (chosen)

**Pros:** Already integrated; handles bot login, tokens, `maxlag`, session cookies,
and `APIError` typing; SAML path reuses `site.connection` (`requests.Session`).
**Cons:** Slow releases; small maintainer pool.

### 2. Raw `requests` / `httpx` + hand-rolled Action API client

**Pros:** No third-party MediaWiki wrapper; full control over retries, timeouts, and
auth; `_saml_login` already performs multi-step HTTP via `site.connection`.
**Cons:** Must reimplement bot login, `clientlogin`, token/csrf handling, `maxlag`
backoff, continuation tokens, and error code mapping — roughly 300–500 lines and
ongoing MediaWiki churn. `httpx` adds a second HTTP stack unless `requests` is kept
for SAML.

**Verdict:** Viable succession path (see boundary below) but not justified while
`0.11.0` meets our API subset.

### 3. `pymediawiki` ([PyPI](https://pypi.org/project/pymediawiki/))

**Pros:** Active-ish releases; simple search/page API.
**Cons:** Oriented toward Wikipedia convenience wrappers, not low-level Action API
control; no `clientlogin`/SAML story; no `maxlag`; docs recommend Pywikibot for
serious automation. Would not drop in for `WikiClient.api()`.

**Verdict:** Rejected.

### 4. Pywikibot

**Pros:** Mature Wikimedia automation framework.
**Cons:** Heavy, opinionated, editing-oriented; poor fit for a read-only MCP server
with custom SSO and minimal footprint.

**Verdict:** Rejected.

## Abstraction boundary (succession contract)

A replacement for `mwclient` inside `WikiClient` must provide the following. All
call sites are in `src/wg21_wiki_mcp/wiki_client.py`.

| mwclient API | Call site | Required behavior |
|--------------|-----------|-------------------|
| `mwclient.Site(host, path, scheme, clients_useragent, max_lag)` | `_new_site()` | HTTPS session to wiki origin; honour `User-Agent`; respect `maxlag` on API errors |
| `site.login(username, password)` | `_bot_login()` | Bot-password login; persist session cookies |
| `site.get_token("login")` | `_try_clientlogin()` | CSRF/login token for `clientlogin` |
| `site.post(action, **params)` | `_try_clientlogin()` | POST to `api.php` with form fields |
| `site.site_init()` | `_user_login()`, `_try_clientlogin()` | Refresh siteinfo / user session after login |
| `site.api(action, **params)` | `_is_authenticated()`, `api()` hot path | Action API GET/POST; raise typed errors with `.code` |
| `site.connection` | `_saml_login()`, `close()` | Shared `requests.Session` for SSO HTML flow and cleanup |
| `mwclient.errors.APIError` | `api()`, `_try_clientlogin()` | `.code` in `readapidenied`, `maxlag`, `ratelimited`, etc. |
| `mwclient.errors.MwClientError` | `api()`, `_try_clientlogin()` | Transient / transport failures |

`WikiClient` adds retry, re-login, deadline timeouts, and batching on top of this
surface; a fork or replacement only needs to satisfy the table above.

### SAML note

`_saml_login` already bypasses mwclient for HTTP and uses `site.connection` directly.
A `requests`-only migration would extend that pattern to bot/`clientlogin` and
`api()` — the SAML path proves cookie persistence across manual HTTP steps.

## Succession plan

| Trigger | Action | Owner |
|---------|--------|-------|
| PyPI release age **> 12 months** | CI `dependency health` job reports failure; review this doc quarterly | Maintainer on-call |
| PyPI release age **> 24 months** OR blocking CVE | Evaluate fork (`cppalliance/mwclient` patch branch) or `requests`-only `WikiClient` rewrite behind the same table | Code owner (`CODEOWNERS`) |
| MediaWiki API break (live tier red) | Pin wiki version if possible; patch or replace client; run full live suite | Whoever owns the incident |
| `mwclient` **0.12+** or **1.0** ships | Dependabot PR; run offline + live tiers; tighten upper bound in `pyproject.toml` | Reviewer on Dependabot PR |

### Fork criteria

Fork upstream when **any** of:

1. Critical security fix is merged to `master` but not released within 30 days.
2. WG21 wiki requires an API behavior change we cannot implement without patching.
3. Project is archived and PyPI release is > 12 months old **and** no compatible fork exists.

Fork should track the abstraction boundary table and publish an internal tag only
(no PyPI publish required unless we split a reusable library).

## Monitoring

- **CI:** `dependency health (mwclient release age)` in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
- **Dependabot:** weekly pip updates for `mwclient`
- **Lockfile:** `requirements-lock.txt` pins exact `mwclient==0.11.0`; lockfile job fails if stale
- **Manual:** `python scripts/check_mwclient_release_age.py` (optional `--max-age-days N`,
  `--fail-reason review|hard`)

## References

- [mwclient PyPI](https://pypi.org/project/mwclient/)
- [mwclient GitHub](https://github.com/mwclient/mwclient)
- [MediaWiki Action API](https://www.mediawiki.org/wiki/API:Main_page)
- Eval finding: Test 26, cluster `dependencies-build`, issue `6045ed68`
