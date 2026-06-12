"""WikiClient tests: auth selection, re-login, and batch resolution."""

from __future__ import annotations

import types

import pytest
from mwclient.errors import APIError, LoginError

from wg21_wiki_mcp import wiki_client as wc
from wg21_wiki_mcp.config import Config, Credentials
from wg21_wiki_mcp.models import AuthError


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


class FakeSite:
    def __init__(self, *, login_fails=False, clientlogin_status="FAIL", saml_fails=False, api_func=None):
        self.login_fails = login_fails
        self.clientlogin_status = clientlogin_status
        self._api_func = api_func
        self.login_count = 0

        def _conn_get(*_a, **_k):
            if saml_fails:
                raise ConnectionError("no network in test")
            raise AssertionError("SAML path not expected in this test")

        self.connection = types.SimpleNamespace(get=_conn_get, post=lambda *a, **k: None, cookies={})

    def login(self, _u, _p):
        self.login_count += 1
        if self.login_fails:
            raise LoginError(self, "Failed", "bad creds")

    def get_token(self, _t):
        return "tok"

    def post(self, action, **_kw):
        if action == "clientlogin":
            return {"clientlogin": {"status": self.clientlogin_status}}
        return {}

    def site_init(self):
        pass

    def api(self, action, **params):
        if action == "query" and params.get("meta") == "userinfo":
            return {"query": {"userinfo": {"name": "Acct"}}}
        if self._api_func is not None:
            return self._api_func(action, params)
        return {"ok": 1}


def _patch_sites(monkeypatch, client, sites):
    seq = iter(sites)
    monkeypatch.setattr(client, "_new_site", lambda: next(seq))


# --- url helpers ----------------------------------------------------------
def test_url_helpers(tmp_path):
    client = wc.WikiClient(_config(tmp_path))
    assert client.canonical_url("A B").endswith("title=A_B")
    assert client.canonical_url("Ns:Page").endswith("title=Ns:Page")
    assert client.oldid_url("A B", 7).endswith("&oldid=7")
    assert client.oldid_url("A", None) is None


# --- auth selection -------------------------------------------------------
def test_bot_login_succeeds(tmp_path, monkeypatch):
    client = wc.WikiClient(_config(tmp_path, bot=True))
    _patch_sites(monkeypatch, client, [FakeSite()])
    client.login()
    assert client.active_label == "bot"


def test_falls_back_to_user_clientlogin(tmp_path, monkeypatch):
    client = wc.WikiClient(_config(tmp_path, bot=True, user=True))
    _patch_sites(
        monkeypatch,
        client,
        [
            FakeSite(login_fails=True),  # bot attempt
            FakeSite(clientlogin_status="PASS"),  # user attempt via clientlogin
        ],
    )
    client.login()
    assert client.active_label == "user"


def test_all_paths_fail_raises(tmp_path, monkeypatch):
    client = wc.WikiClient(_config(tmp_path, bot=True, user=True))
    _patch_sites(
        monkeypatch,
        client,
        [
            FakeSite(login_fails=True),  # bot
            FakeSite(clientlogin_status="FAIL", saml_fails=True),  # user clientlogin fail + SAML fail
        ],
    )
    with pytest.raises(AuthError):
        client.login()


def test_relogin_on_readapidenied(tmp_path, monkeypatch):
    calls = {"n": 0}

    def api_func(action, params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise APIError("readapidenied", "need read", {})
        return {"ok": "after-relogin"}

    good = FakeSite(api_func=api_func)
    client = wc.WikiClient(_config(tmp_path, bot=True))
    _patch_sites(monkeypatch, client, [good, FakeSite(api_func=api_func)])
    client.login()
    result = client.api("query")
    assert result == {"ok": "after-relogin"}
    assert calls["n"] == 2  # failed once, retried after re-login


# --- batch resolution -----------------------------------------------------
def test_fetch_pages_maps_normalized_redirects_missing(tmp_path, monkeypatch):
    def api_func(action, params):
        return {
            "query": {
                "normalized": [{"from": "foo_bar", "to": "Foo bar"}],
                "redirects": [{"from": "Foo bar", "to": "Target"}],
                "pages": {
                    "1": {
                        "title": "Target",
                        "revisions": [
                            {
                                "revid": 11,
                                "timestamp": "2026-06-01T00:00:00Z",
                                "size": 4,
                                "slots": {"main": {"*": "body"}},
                            }
                        ],
                    },
                    "2": {"title": "Gone", "missing": ""},
                },
            }
        }

    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [FakeSite(api_func=api_func)])
    client.login()
    out = client.fetch_pages(["foo_bar", "Gone"])
    assert out["foo_bar"].title == "Target"
    assert out["foo_bar"].redirected_from == "foo_bar"
    assert out["foo_bar"].content == "body"
    assert out["Gone"].missing is True


def test_page_revisions(tmp_path, monkeypatch):
    def api_func(action, params):
        return {"query": {"pages": {"1": {"title": "P", "revisions": [{"revid": 99}]}}}}

    client = wc.WikiClient(_config(tmp_path))
    _patch_sites(monkeypatch, client, [FakeSite(api_func=api_func)])
    client.login()
    assert client.page_revisions(["P"]) == {"P": 99}
