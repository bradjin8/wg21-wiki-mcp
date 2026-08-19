"""MCP error-contract tests: domain errors map to structured ``MCPError`` codes."""

from __future__ import annotations

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

from wg21_wiki_mcp.errors import (
    AUTH_ERROR,
    CONFIG_ERROR,
    FETCH_ERROR,
    PAGE_NOT_FOUND,
    AuthError,
    ConfigError,
    FetchError,
    PageNotFound,
    to_mcp_error,
)
from wg21_wiki_mcp.pagination import encode_cursor


class TestToMCPError:
    def test_page_not_found_maps_to_code_1(self):
        err = to_mcp_error(PageNotFound("Page not found: 'Ghost'"))
        assert err.error.code == PAGE_NOT_FOUND
        assert "Ghost" in err.error.message

    def test_auth_error_maps_to_code_2(self):
        err = to_mcp_error(AuthError("All configured credential paths failed: bot: LoginError: bad"))
        assert err.error.code == AUTH_ERROR

    def test_auth_error_message_contains_no_credential_detail(self):
        secret = "supersecretpassword"
        err = to_mcp_error(AuthError(f"failed: {secret}"))
        assert secret not in err.error.message

    def test_fetch_error_maps_to_code_3(self):
        err = to_mcp_error(FetchError("API call 'query' failed after 6 retries: ConnectionError"))
        assert err.error.code == FETCH_ERROR

    def test_config_error_maps_to_code_4(self):
        err = to_mcp_error(ConfigError("No credentials configured: set WIKI_BOT_USERNAME/WIKI_BOT_PASSWORD"))
        assert err.error.code == CONFIG_ERROR
        assert "WIKI_BOT_USERNAME" in err.error.message

    def test_existing_mcp_error_passes_through_unchanged(self):
        original = MCPError(INVALID_PARAMS, "bad cursor")
        assert to_mcp_error(original) is original

    def test_raw_api_error_maps_to_fetch_error_code(self):
        try:
            from mwclient.errors import APIError

            err = to_mcp_error(APIError("protectedpage", "protectedpage", []))
            assert err.error.code == FETCH_ERROR
            assert "protectedpage" in err.error.message
        except ImportError:
            pytest.skip("mwclient not installed")

    def test_unknown_exception_maps_to_fetch_error_code(self):
        assert to_mcp_error(RuntimeError("something unexpected")).error.code == FETCH_ERROR


class TestMissingPageThroughToolBoundary:
    def test_server_wrap_converts_page_not_found(self, fake_client, make_ctx):
        from wg21_wiki_mcp import tools
        from wg21_wiki_mcp.server import _wrap

        with pytest.raises(MCPError) as exc_info:
            _wrap(tools.get_page, make_ctx(fake_client), "NonExistent")
        assert exc_info.value.error.code == PAGE_NOT_FOUND

    def test_section_not_found_raises_mcp_error(self, fake_client, make_ctx):
        from wg21_wiki_mcp import tools
        from wg21_wiki_mcp.server import _wrap

        with pytest.raises(MCPError) as exc_info:
            _wrap(tools.get_page, make_ctx(fake_client), "NonExistent", section=1)
        assert exc_info.value.error.code == PAGE_NOT_FOUND


class TestBadCursorErrorShape:
    def test_invalid_offset_through_server_wrap(self, fake_client, make_ctx):
        from wg21_wiki_mcp import tools
        from wg21_wiki_mcp.server import _wrap

        with pytest.raises(MCPError) as exc_info:
            _wrap(
                tools.search_wiki,
                make_ctx(fake_client),
                "topic",
                cursor=encode_cursor({"o": "not-an-int"}, kind="search"),
            )
        assert exc_info.value.error.code == INVALID_PARAMS

    def test_negative_offset_through_server_wrap(self, fake_client, make_ctx):
        from wg21_wiki_mcp import tools
        from wg21_wiki_mcp.server import _wrap

        with pytest.raises(MCPError) as exc_info:
            _wrap(tools.list_meetings, make_ctx(fake_client), cursor=encode_cursor({"o": -5}, kind="meetings"))
        assert exc_info.value.error.code == INVALID_PARAMS


class TestConfigErrorShape:
    def test_config_from_env_raises_config_error_without_credentials(self, monkeypatch):
        from wg21_wiki_mcp.config import Config

        for var in (
            "WIKI_BOT_USERNAME",
            "WIKI_BOT_PASSWORD",
            "WIKI_USER_USERNAME",
            "WIKI_USER_PASSWORD",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr("wg21_wiki_mcp.config._load_dotenv", None, raising=False)

        with pytest.raises(ConfigError) as exc_info:
            Config.from_env(load_env_file=False)
        assert to_mcp_error(exc_info.value).error.code == CONFIG_ERROR


class TestAuthFailureErrorShape:
    def test_server_wrap_converts_auth_error(self):
        from wg21_wiki_mcp.server import _wrap

        def _raise_auth():
            raise AuthError("simulated auth lapse")

        with pytest.raises(MCPError) as exc_info:
            _wrap(_raise_auth)
        assert exc_info.value.error.code == AUTH_ERROR

    def test_exhausted_auth_retries_emit_code_2_through_wrap(self, tmp_path, monkeypatch):
        import types

        from mwclient.errors import APIError

        from wg21_wiki_mcp import wiki_client as wc
        from wg21_wiki_mcp.config import Config, Credentials
        from wg21_wiki_mcp.server import _wrap

        monkeypatch.setattr(wc.time, "sleep", lambda *_a, **_k: None)

        class AlwaysAuthDeniedSite:
            connection = types.SimpleNamespace(cookies={}, close=lambda: None)

            def login(self, _u, _p):
                pass

            def api(self, action, **params):
                if params.get("meta") == "userinfo":
                    return {"query": {"userinfo": {"name": "Bot"}}}
                raise APIError("readapidenied", "denied", [])

        client = wc.WikiClient(
            Config(
                base_url="https://w.example",
                bot=Credentials("bot", "Bot@bot", "secret"),
                user=None,
                cache_dir=tmp_path / "c",
            )
        )
        monkeypatch.setattr(client, "_new_site", lambda: AlwaysAuthDeniedSite())
        client.login()

        with pytest.raises(MCPError) as exc_info:
            _wrap(client.api, "query", titles="SomePage")
        assert exc_info.value.error.code == AUTH_ERROR
