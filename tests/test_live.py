"""Live tier: exercises the real wiki using configured secrets.

These tests are skipped automatically when credentials are absent (forks/local
stay green). They assert structurally and NEVER print or store wiki content, so
nothing confidential leaks into logs or CI artifacts.
"""

from __future__ import annotations

import os

import pytest

from wg21_wiki_mcp import tools
from wg21_wiki_mcp.config import Config
from wg21_wiki_mcp.context import ServerContext

pytestmark = pytest.mark.live

_HAS_CREDS = bool(
    (os.environ.get("WIKI_BOT_USERNAME") and os.environ.get("WIKI_BOT_PASSWORD"))
    or (os.environ.get("WIKI_USER_USERNAME") and os.environ.get("WIKI_USER_PASSWORD"))
)

skip_no_creds = pytest.mark.skipif(not _HAS_CREDS, reason="no wiki credentials configured")


@pytest.fixture(scope="module")
def live_ctx(tmp_path_factory):
    cache_dir = tmp_path_factory.mktemp("live-cache")
    os.environ.setdefault("ISOCPP_WIKI_CACHE_DIR", str(cache_dir))
    ctx = ServerContext.create(Config.from_env())
    ctx.login()
    return ctx


@skip_no_creds
def test_login_succeeds(live_ctx):
    status = tools.wiki_status(live_ctx)
    assert status.authenticated is True
    assert status.auth_mode in {"bot", "user"}


@skip_no_creds
def test_fetch_main_page_has_provenance(live_ctx):
    page = tools.get_page(live_ctx, "Main Page")
    # Structural assertions only - do not print or store the content.
    assert isinstance(page.content, str) and page.content != ""
    assert page.provenance.revid is not None
    assert page.provenance.url.startswith(live_ctx.config.base_url)
    assert page.provenance.oldid_url and "oldid=" in page.provenance.oldid_url


@skip_no_creds
def test_recent_changes_reachable(live_ctx):
    changes = tools.get_recent_changes(live_ctx, limit=1)
    assert isinstance(changes.changes, list)
