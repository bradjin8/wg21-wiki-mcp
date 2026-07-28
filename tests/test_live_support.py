"""Unit tests for live/canary WAF-edge skip helpers."""

from __future__ import annotations

import logging
import re

import pytest
import responses
from live_support import (
    assert_live_requirements_met,
    auth_error_is_unreachable,
    auth_error_waf_status,
    ensure_wiki_login,
    live_credentials_configured,
    live_creds_required,
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("YES", True),
        ("0", False),
        ("", False),
        ("no", False),
    ],
)
def test_live_creds_required_truthy_parsing(monkeypatch, value: str, expected: bool):
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", value)
    assert live_creds_required() is expected


def test_live_creds_required_false_when_unset(monkeypatch):
    monkeypatch.delenv("CI_REQUIRE_LIVE_CREDS", raising=False)
    assert live_creds_required() is False


def test_assert_live_requirements_met_noop_when_not_required(monkeypatch):
    monkeypatch.delenv("CI_REQUIRE_LIVE_CREDS", raising=False)
    monkeypatch.delenv("WIKI_BOT_USERNAME", raising=False)
    monkeypatch.delenv("WIKI_BOT_PASSWORD", raising=False)
    assert_live_requirements_met()


def test_assert_live_requirements_met_fails_when_required_and_missing(monkeypatch):
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", "1")
    monkeypatch.setattr("live_support.live_credentials_configured", lambda: False)
    with pytest.raises(pytest.fail.Exception, match="CI_REQUIRE_LIVE_CREDS"):
        assert_live_requirements_met()


def test_live_credentials_configured_true_with_bot_creds(monkeypatch, tmp_path):
    monkeypatch.setenv("WIKI_BOT_USERNAME", "Acct@bot")
    monkeypatch.setenv("WIKI_BOT_PASSWORD", "secret")
    monkeypatch.setenv("ISOCPP_WIKI_CACHE_DIR", str(tmp_path / "cache"))
    assert live_credentials_configured() is True


@responses.activate
def test_ensure_wiki_login_fails_on_waf_probe_when_required(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", "1")
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    responses.add(responses.GET, re.compile(r"https://w\.example/api\.php"), status=403, body="blocked")
    try:
        with pytest.raises(pytest.fail.Exception, match="HTTP 403"):
            ensure_wiki_login(ctx)
    finally:
        ctx.close()


def test_ensure_wiki_login_fails_on_waf_auth_error_when_required(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", "1")
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    monkeypatch.setattr("live_support.probe_wiki_waf_block", lambda _config: None)

    def _blocked_login() -> None:
        raise AuthError("SAML SSO entry point returned HTTP error.; url=https://w.example; status=429")

    ctx.client.login = _blocked_login  # type: ignore[method-assign]
    try:
        with pytest.raises(pytest.fail.Exception, match="HTTP 429"):
            ensure_wiki_login(ctx)
    finally:
        ctx.close()


def test_ensure_wiki_login_fails_on_network_unreachable_when_required(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_REQUIRE_LIVE_CREDS", "1")
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    monkeypatch.setattr("live_support.probe_wiki_waf_block", lambda _config: None)

    def _unreachable_login() -> None:
        raise AuthError("Authentication failed (bot: ConnectionError); verify wiki credentials.")

    ctx.client.login = _unreachable_login  # type: ignore[method-assign]
    try:
        with pytest.raises(pytest.fail.Exception, match="unreachable"):
            ensure_wiki_login(ctx)
    finally:
        ctx.close()


def test_ensure_wiki_login_skips_on_network_unreachable_when_not_required(tmp_path, monkeypatch):
    monkeypatch.delenv("CI_REQUIRE_LIVE_CREDS", raising=False)
    config = _config(tmp_path)
    ctx = ServerContext.create(config)
    monkeypatch.setattr("live_support.probe_wiki_waf_block", lambda _config: None)

    def _unreachable_login() -> None:
        raise AuthError("Authentication failed (bot: ConnectionError); verify wiki credentials.")

    ctx.client.login = _unreachable_login  # type: ignore[method-assign]
    try:
        with pytest.raises(pytest.skip.Exception, match="unreachable"):
            ensure_wiki_login(ctx)
    finally:
        ctx.close()
