"""SAML auth fault-injection tests (issue #46).

Each scenario mocks HTTP at a specific stage of the SSO flow and verifies
diagnostic :class:`~wg21_wiki_mcp.models.AuthError` messages with no partial
``_site`` state left on the client.
"""

from __future__ import annotations

import re
from pathlib import Path

import mwclient
import pytest
import requests
import responses
from mwclient.errors import APIError

from wg21_wiki_mcp import wiki_client as wc
from wg21_wiki_mcp.config import Config, Credentials
from wg21_wiki_mcp.models import AuthError

_FIXTURES = Path(__file__).parent / "fixtures" / "saml"
_BASE = "https://w.example"
_PLUGGABLE = re.compile(r"https://w\.example/index\.php\?title=Special:PluggableAuthLogin")
_IDP = re.compile(r"https://idp\.example/login")
_ACS = re.compile(r"https://w\.example/index\.php/Special:PluggableAuth/Assert")


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _user_config(tmp_path: Path) -> Config:
    return Config(
        base_url=_BASE,
        bot=None,
        user=Credentials("user", "Acct", "test-password-xyz"),
        cache_dir=tmp_path / "c",
    )


def _saml_site(client: wc.WikiClient) -> mwclient.Site:
    return mwclient.Site(
        client._host,
        path="/",
        scheme=client._scheme,
        clients_useragent=client._config.user_agent,
        max_lag=5,
        do_init=False,
    )


def _assert_no_site(client: wc.WikiClient) -> None:
    assert client._site is None


def _register_idp_through_saml_response() -> None:
    responses.add(
        responses.GET,
        _PLUGGABLE,
        status=302,
        headers={"Location": "https://idp.example/login?AuthState=abc123"},
    )
    responses.add(responses.GET, _IDP, body=_fixture("idp_login_form.html"), status=200)
    responses.add(responses.POST, _IDP, body=_fixture("saml_post_form.html"), status=200)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(wc.time, "sleep", lambda *_a, **_k: None)


@responses.activate
def test_sso_entry_http_500_raises_auth_error(tmp_path):
    responses.add(responses.GET, _PLUGGABLE, body="Internal Server Error", status=500)
    client = wc.WikiClient(_user_config(tmp_path))
    site = _saml_site(client)
    cred = client._config.user
    assert cred is not None
    _assert_no_site(client)
    with pytest.raises(AuthError, match=r"SAML SSO entry point returned HTTP error"):
        client._saml_login(site, cred)
    _assert_no_site(client)


@responses.activate
def test_idp_missing_password_field_raises_auth_error(tmp_path):
    responses.add(
        responses.GET,
        _PLUGGABLE,
        status=302,
        headers={"Location": "https://idp.example/login?AuthState=abc123"},
    )
    responses.add(responses.GET, _IDP, body=_fixture("no_password_field.html"), status=200)
    client = wc.WikiClient(_user_config(tmp_path))
    site = _saml_site(client)
    cred = client._config.user
    assert cred is not None
    _assert_no_site(client)
    with pytest.raises(AuthError, match="Could not locate username/password fields"):
        client._saml_login(site, cred)
    _assert_no_site(client)


@responses.activate
def test_idp_post_http_403_raises_auth_error(tmp_path):
    responses.add(
        responses.GET,
        _PLUGGABLE,
        status=302,
        headers={"Location": "https://idp.example/login?AuthState=abc123"},
    )
    responses.add(responses.GET, _IDP, body=_fixture("idp_login_form.html"), status=200)
    responses.add(responses.POST, _IDP, body="Forbidden", status=403)
    client = wc.WikiClient(_user_config(tmp_path))
    site = _saml_site(client)
    cred = client._config.user
    assert cred is not None
    _assert_no_site(client)
    with pytest.raises(AuthError, match=r"SAML IdP POST returned HTTP error.*status=403"):
        client._saml_login(site, cred)
    _assert_no_site(client)


@responses.activate
def test_idp_post_error_page_raises_auth_error(tmp_path):
    responses.add(
        responses.GET,
        _PLUGGABLE,
        status=302,
        headers={"Location": "https://idp.example/login?AuthState=abc123"},
    )
    responses.add(responses.GET, _IDP, body=_fixture("idp_login_form.html"), status=200)
    responses.add(responses.POST, _IDP, body=_fixture("mfa_challenge.html"), status=200)
    client = wc.WikiClient(_user_config(tmp_path))
    site = _saml_site(client)
    cred = client._config.user
    assert cred is not None
    _assert_no_site(client)
    with pytest.raises(AuthError, match="check credentials/MFA"):
        client._saml_login(site, cred)
    _assert_no_site(client)


@responses.activate
def test_acs_post_http_403_raises_auth_error(tmp_path):
    _register_idp_through_saml_response()
    responses.add(responses.POST, _ACS, body="Forbidden", status=403)
    client = wc.WikiClient(_user_config(tmp_path))
    site = _saml_site(client)
    cred = client._config.user
    assert cred is not None
    _assert_no_site(client)
    with pytest.raises(AuthError, match=r"ACS endpoint rejected.*status=403"):
        client._saml_login(site, cred)
    _assert_no_site(client)


@responses.activate
def test_sso_get_timeout_propagates(tmp_path, monkeypatch):
    client = wc.WikiClient(_user_config(tmp_path))
    site = _saml_site(client)
    cred = client._config.user
    assert cred is not None

    def _timeout_get(*_args, **_kwargs):
        raise requests.Timeout("connection timed out")

    monkeypatch.setattr(site.connection, "get", _timeout_get)
    _assert_no_site(client)
    with pytest.raises(AuthError, match="SAML SSO request failed: Timeout"):
        client._saml_login(site, cred)
    _assert_no_site(client)


@responses.activate
def test_idp_post_timeout_raises_auth_error(tmp_path, monkeypatch):
    responses.add(
        responses.GET,
        _PLUGGABLE,
        status=302,
        headers={"Location": "https://idp.example/login?AuthState=abc123"},
    )
    responses.add(responses.GET, _IDP, body=_fixture("idp_login_form.html"), status=200)
    client = wc.WikiClient(_user_config(tmp_path))
    site = _saml_site(client)
    cred = client._config.user
    assert cred is not None

    def _timeout_on_idp_post(url, *args, **kwargs):
        raise requests.Timeout("connection timed out")

    monkeypatch.setattr(site.connection, "post", _timeout_on_idp_post)
    _assert_no_site(client)
    with pytest.raises(AuthError, match="SAML SSO request failed: Timeout"):
        client._saml_login(site, cred)
    _assert_no_site(client)


@responses.activate
def test_acs_post_connection_error_raises_auth_error(tmp_path, monkeypatch):
    _register_idp_through_saml_response()
    client = wc.WikiClient(_user_config(tmp_path))
    site = _saml_site(client)
    cred = client._config.user
    assert cred is not None
    original_post = site.connection.post
    post_calls = {"n": 0}

    def _fail_acs_post(url, *args, **kwargs):
        post_calls["n"] += 1
        if post_calls["n"] == 1:
            return original_post(url, *args, **kwargs)
        raise requests.ConnectionError("connection reset")

    monkeypatch.setattr(site.connection, "post", _fail_acs_post)
    _assert_no_site(client)
    with pytest.raises(AuthError, match="SAML SSO request failed: ConnectionError"):
        client._saml_login(site, cred)
    _assert_no_site(client)


class _ClientloginFailSite:
    """Site stub: clientlogin API error, real requests session for SAML HTTP."""

    def __init__(self, client: wc.WikiClient):
        self.connection = _saml_site(client).connection

    def get_token(self, _kind: str) -> str:
        return "login-token"

    def post(self, action: str, **_kw):
        if action == "clientlogin":
            raise APIError("loginfailed", "bad", {})
        return {}

    def site_init(self) -> None:
        pass


@responses.activate
def test_clientlogin_then_saml_failure_includes_both_reasons(tmp_path, monkeypatch):
    responses.add(responses.GET, _PLUGGABLE, body=_fixture("no_form.html"), status=200)
    client = wc.WikiClient(_user_config(tmp_path))
    monkeypatch.setattr(client, "_new_site", lambda: _ClientloginFailSite(client))
    cred = client._config.user
    assert cred is not None
    _assert_no_site(client)
    with pytest.raises(AuthError) as exc_info:
        client._user_login(cred)
    message = str(exc_info.value)
    assert "clientlogin unavailable" in message
    assert "SAML IdP login form not found" in message
    _assert_no_site(client)


@responses.activate
def test_user_login_anonymous_after_saml_raises_without_site(tmp_path, monkeypatch):
    _register_idp_through_saml_response()
    responses.add(responses.POST, _ACS, status=302, headers={"Location": "https://w.example/"})
    responses.add(responses.GET, re.compile(r"https://w\.example/?$"), status=200, body="ok")
    client = wc.WikiClient(_user_config(tmp_path))
    site = _saml_site(client)
    monkeypatch.setattr(client, "_new_site", lambda: site)
    monkeypatch.setattr(mwclient.Site, "site_init", lambda self: None)
    monkeypatch.setattr(client, "_try_clientlogin", lambda *_a, **_k: False)
    monkeypatch.setattr(client, "_is_authenticated", lambda _site: False)
    cred = client._config.user
    assert cred is not None
    _assert_no_site(client)
    with pytest.raises(AuthError, match="anonymous session"):
        client._user_login(cred)
    _assert_no_site(client)
