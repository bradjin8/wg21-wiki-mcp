# Security Policy

## Credentials

This server authenticates to the WG21 wiki with credentials supplied through the
environment (the MCP host's launch `env` block, or a local `.env` for
development). Credentials are:

- read only from the process environment at startup,
- held in memory for the lifetime of the process,
- never written to logs, tool output, the cache, or any file,
- never embedded in source code or tests.

Use a **bot password** (`Special:BotPasswords`, Read grant) where possible: it is
scoped and revocable. User SSO credentials are supported as a fallback.

## Confidentiality of wiki content

The committee wiki is access-restricted and its contents are confidential. This
project treats all wiki content as confidential:

- No wiki page titles, namespace names, or page content are embedded in source,
  tests, fixtures, or documentation. The wiki structure is discovered at runtime.
- Only the **public** meeting schedule (from isocpp.org) and generic placeholders
  appear in the repository.
- The local cache database and any dumps are git-ignored.

Tool output returned to an authenticated user may contain wiki content (they
already have wiki access); logs and committed artifacts must not.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the maintainers via a
GitHub security advisory rather than a public issue.
