"""Additional WikiClient coverage: read methods, SAML flow, api() branches."""

from __future__ import annotations

import types

import pytest
from mwclient.errors import APIError, MwClientError

from wg21_wiki_mcp import wiki_client as wc
from wg21_wiki_mcp.config import Config, Credentials
from wg21_wiki_mcp.models import AuthError, FetchError


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(wc.time, "sleep", lambda *_a, **_k: None)


def _config(tmp_path, *, bot=True, user=False) -> Config:
    return Config(
        base_url="https://w.example",
        bot=Credentials("bot", "Acct@bot", "secret") if bot else None,
        user=Credentials("user", "Acct", "pw") if user else None,
        cache_dir=tmp_path / "c",
    )


class RecordingSite:
    """Captures the last api() action/params and returns canned responses."""

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, dict]] = []
        self.connection = types.SimpleNamespace(cookies={})

    def login(self, _u, _p):
        pass

    def api(self, action, **params):
        self.calls.append((action, params))
        if action == "query" and params.get("meta") == "userinfo":
            return {"query": {"userinfo": {"name": "Acct"}}}
        return self.responder(action, params)


def _client(tmp_path, monkeypatch, site, **cfg):
    client = wc.WikiClient(_config(tmp_path, **cfg))
    monkeypatch.setattr(client, "_new_site", lambda: site)
    client.login()
    return client


def test_read_methods_forward_params(tmp_path, monkeypatch):
    site = RecordingSite(lambda action, params: {"query": {}, "continue": {}})
    client = _client(tmp_path, monkeypatch, site)

    client.search("hello", limit=3, namespace=4, offset=6)
    client.list_pages(namespace=0, prefix="Pre", limit=10, cont="20")
    client.list_namespaces()
    client.recent_changes(namespace=2, since="2026-06-01T00:00:00Z", limit=5, cont="c1")
    client.page_links("Some Title", limit=50, cont="pl1")
    client.statistics()

    actions = [c[0] for c in site.calls]
    assert actions.count("query") == 6
    # spot-check forwarded params
    search_params = next(p for a, p in site.calls if p.get("list") == "search")
    assert search_params["srsearch"] == "hello" and search_params["srnamespace"] == 4
    rc_params = next(p for a, p in site.calls if p.get("list") == "recentchanges")
    assert rc_params["rcnamespace"] == 2 and rc_params["rcend"].startswith("2026")


def test_api_non_auth_error_raises(tmp_path, monkeypatch):
    def responder(action, params):
        raise APIError("permissiondenied", "nope", {})

    client = _client(tmp_path, monkeypatch, RecordingSite(responder))
    with pytest.raises(APIError):
        client.api("query")


def test_api_maxlag_then_success(tmp_path, monkeypatch):
    state = {"n": 0}

    def responder(action, params):
        state["n"] += 1
        if state["n"] == 1:
            raise APIError("maxlag", "lag", {})
        return {"ok": 1}

    client = _client(tmp_path, monkeypatch, RecordingSite(responder))
    assert client.api("query") == {"ok": 1}


def test_api_transient_then_success(tmp_path, monkeypatch):
    state = {"n": 0}

    def responder(action, params):
        state["n"] += 1
        if state["n"] == 1:
            raise ConnectionError("blip")
        return {"ok": 2}

    client = _client(tmp_path, monkeypatch, RecordingSite(responder))
    assert client.api("query") == {"ok": 2}


def test_api_exhausts_retries(tmp_path, monkeypatch):
    def responder(action, params):
        raise MwClientError("always broken")

    client = _client(tmp_path, monkeypatch, RecordingSite(responder))
    with pytest.raises(FetchError):
        client.api("query")


# --- SAML headless login --------------------------------------------------
class _Resp:
    def __init__(self, text, url):
        self.text = text
        self.url = url


class _SamlConn:
    def __init__(self):
        self.cookies = {}
        self._post_count = 0

    def get(self, url, **_kw):
        html = (
            '<form action="https://idp.example/login?AuthState=xyz" method="post">'
            '<input name="username"><input type="password" name="password"></form>'
        )
        return _Resp(html, "https://idp.example/login?AuthState=xyz")

    def post(self, url, **_kw):
        self._post_count += 1
        if self._post_count == 1:
            html = (
                '<form action="https://w.example/acs" method="post">'
                '<input name="SAMLResponse" value="abc">'
                '<input name="RelayState" value="r"></form>'
            )
            return _Resp(html, url)
        return _Resp("ok", url)


class SamlSite:
    def __init__(self):
        self.connection = _SamlConn()

    def get_token(self, _t):
        return "tok"

    def post(self, action, **_kw):
        return {"clientlogin": {"status": "FAIL"}}  # force SAML fallback

    def site_init(self):
        pass

    def api(self, action, **params):
        return {"query": {"userinfo": {"name": "Acct"}}}


def test_saml_login_success(tmp_path, monkeypatch):
    client = wc.WikiClient(_config(tmp_path, bot=False, user=True))
    monkeypatch.setattr(client, "_new_site", lambda: SamlSite())
    client.login()
    assert client.active_label == "user"


class SamlNoFormSite(SamlSite):
    class _Conn(_SamlConn):
        def get(self, url, **_kw):
            return _Resp("<html>no form here</html>", url)

    def __init__(self):
        self.connection = SamlNoFormSite._Conn()


def test_saml_login_no_form_raises(tmp_path, monkeypatch):
    client = wc.WikiClient(_config(tmp_path, bot=False, user=True))
    monkeypatch.setattr(client, "_new_site", lambda: SamlNoFormSite())
    with pytest.raises(AuthError):
        client.login()
