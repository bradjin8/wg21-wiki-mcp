"""Helpers for the live/canary wiki test tier (no confidential content)."""

from __future__ import annotations

import logging
import re

import pytest
import requests

from wg21_wiki_mcp.config import Config
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp.errors import AuthError

_log = logging.getLogger(__name__)

WAF_EDGE_HTTP_CODES = frozenset({403, 429, 503})

_HTTP_STATUS_RE = re.compile(r"status=(\d{3})")
_HTTP_STATUS_LEGACY_RE = re.compile(r"HTTP (\d{3})")

_NETWORK_AUTH_FAILURE_TYPES = frozenset(
    {
        "ConnectionError",
        "ConnectionResetError",
        "ConnectTimeout",
        "NewConnectionError",
        "OSError",
        "ProtocolError",
        "ReadTimeout",
        "SSLError",
        "Timeout",
        "TimeoutError",
    }
)
_AUTH_PATH_FAILURE_RE = re.compile(r"\b(?:bot|user): (\w+)")


def auth_error_waf_status(exc: AuthError) -> int | None:
    """Return a WAF/edge block status code embedded in ``exc``, if any."""
    message = str(exc)
    for pattern in (_HTTP_STATUS_RE, _HTTP_STATUS_LEGACY_RE):
        match = pattern.search(message)
        if match is not None:
            status = int(match.group(1))
            if status in WAF_EDGE_HTTP_CODES:
                return status
    return None


def auth_error_is_unreachable(exc: AuthError) -> bool:
    """True when every failed auth path hit a network error (not bad credentials)."""
    failure_types = _AUTH_PATH_FAILURE_RE.findall(str(exc))
    return bool(failure_types) and all(name in _NETWORK_AUTH_FAILURE_TYPES for name in failure_types)


def probe_wiki_waf_block(config: Config) -> tuple[int, str] | None:
    """Probe the wiki edge; return ``(status, url)`` when a WAF/CDN block is seen."""
    probes = (
        f"{config.base_url}/api.php?action=query&meta=siteinfo&siprop=general&format=json",
        f"{config.base_url}/index.php?title=Special:PluggableAuthLogin",
    )
    headers = {"User-Agent": config.user_agent}
    for url in probes:
        try:
            resp = requests.get(url, timeout=15, allow_redirects=True, headers=headers)
        except requests.RequestException:
            continue
        if resp.status_code in WAF_EDGE_HTTP_CODES:
            return resp.status_code, resp.url
    return None


def log_waf_edge_skip(status: int, *, url: str) -> None:
    _log.warning(
        "Skipping live/canary tests: wiki edge returned HTTP %s for %s "
        "(WAF/CDN block of this runner; not a credential fault)",
        status,
        url,
    )


def skip_reason_waf_edge(status: int, *, url: str) -> str:
    return f"wiki edge returned HTTP {status} for {url} (WAF/CDN block of this runner; not a credential fault)"


def ensure_wiki_login(ctx: ServerContext) -> None:
    """Log in, or skip live tests on WAF blocks / network faults (not bad credentials)."""
    blocked = probe_wiki_waf_block(ctx.config)
    if blocked is not None:
        status, url = blocked
        log_waf_edge_skip(status, url=url)
        pytest.skip(skip_reason_waf_edge(status, url=url))

    try:
        ctx.login()
    except AuthError as exc:
        waf_status = auth_error_waf_status(exc)
        if waf_status is not None:
            log_waf_edge_skip(waf_status, url=ctx.config.base_url)
            pytest.skip(skip_reason_waf_edge(waf_status, url=ctx.config.base_url))
        if auth_error_is_unreachable(exc):
            pytest.skip(
                "wiki.isocpp.org is unreachable from this shell (network error on all auth paths); "
                "credentials loaded but TCP/TLS failed — retry when the wiki is reachable or check VPN/proxy"
            )
        raise
