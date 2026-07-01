"""Unit tests for live/canary WAF-edge skip helpers."""

from __future__ import annotations

import logging
import re

import pytest
import responses
from live_support import (
    auth_error_is_unreachable,
    auth_error_waf_status,
    ensure_wiki_login,
    probe_wiki_waf_block,
)

from wg21_wiki_mcp.config import Config, Credentials
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp.errors import AuthError


def _config(tmp_path) -> Config:
    return Config(
        base_url="https://w.example",
        bot=Credentials("bot", "Acct@bot", "secret"),
        user=None,
        cache_dir=tmp_path / "c",
    )


@pytest.mark.parametrize("status", [403, 429, 503])
def test_auth_error_waf_status_detects_diagnostic_status(status: int):
    exc = AuthError(f"SAML SSO entry point returned HTTP error.; url=https://w.example; status={status}")
    assert auth_error_waf_status(exc) == status


def test_auth_error_waf_status_ignores_non_waf_codes():
    exc = AuthError("SAML SSO entry point returned HTTP error.; url=https://w.example; status=500")
    assert auth_error_waf_status(exc) is None


def test_auth_error_waf_status_detects_legacy_http_phrase():
    exc = AuthError("SAML SSO entry point returned HTTP 403.")
    assert auth_error_waf_status(exc) == 403


def test_auth_error_waf_status_prefers_waf_code_in_aggregated_message():
    exc = AuthError(
        "clientlogin unavailable; SAML SSO entry point returned HTTP error.; "
        "url=https://w.example; status=500; status=403"
    )
    assert auth_error_waf_status(exc) == 403


def test_auth_error_is_unreachable_true_for_network_failures():
    exc = AuthError("Authentication failed (bot: ConnectionError, user: Timeout); verify wiki credentials.")
    assert auth_error_is_unreachable(exc) is True


def test_auth_error_is_unreachable_false_for_login_error():
    exc = AuthError("Authentication failed (bot: LoginError); verify wiki credentials.")
    assert auth_error_is_unreachable(exc) is False


@responses.activate
def test_probe_wiki_waf_block_detects_api_edge_block(tmp_path):
    config = _config(tmp_path)
    responses.add(
        responses.GET,
        re.compile(r"https://w\.example/api\.php"),
        status=403,
        body="blocked",
    )
    blocked = probe_wiki_waf_block(config)
    assert blocked is not None
    assert blocked[0] == 403
    assert blocked[1].startswith("https://w.example/api.php")


@responses.activate
def test_probe_wiki_waf_block_checks_pluggable_auth_when_api_ok(tmp_path):
    config = _config(tmp_path)
    responses.add(responses.GET, re.compile(r"https://w\.example/api\.php"), status=200, body="{}")
    responses.add(
        responses.GET,
        re.compile(r"https://w\.example/index\.php"),
        status=503,
        body="unavailable",
    )
    blocked = probe_wiki_waf_block(config)
    assert blocked is not None
    assert blocked[0] == 503


@responses.activate
def test_ensure_wiki_login_skips_on_waf_probe(tmp_path, caplog):
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    responses.add(responses.GET, re.compile(r"https://w\.example/api\.php"), status=429, body="rate limited")
    caplog.set_level(logging.WARNING)
    try:
        with pytest.raises(pytest.skip.Exception, match="HTTP 429"):
            ensure_wiki_login(ctx)
    finally:
        ctx.close()
    assert "HTTP 429" in caplog.text


def test_ensure_wiki_login_skips_on_waf_auth_error(tmp_path, caplog, monkeypatch):
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    monkeypatch.setattr("live_support.probe_wiki_waf_block", lambda _config: None)

    def _blocked_login() -> None:
        raise AuthError("SAML SSO entry point returned HTTP error.; url=https://w.example; status=403")

    ctx.client.login = _blocked_login  # type: ignore[method-assign]
    caplog.set_level(logging.WARNING)
    try:
        with pytest.raises(pytest.skip.Exception, match="HTTP 403"):
            ensure_wiki_login(ctx)
    finally:
        ctx.close()
    assert "HTTP 403" in caplog.text
