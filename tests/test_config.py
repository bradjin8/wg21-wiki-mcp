"""Config resolution tests."""

from __future__ import annotations

import pytest

from wg21_wiki_mcp.config import WIKI_BASE_URL, Config, ConfigError

_KEYS = [
    "WIKI_BOT_USERNAME",
    "WIKI_BOT_PASSWORD",
    "WIKI_USER_USERNAME",
    "WIKI_USER_PASSWORD",
    "ISOCPP_WIKI_CACHE_DIR",
    "ISOCPP_WIKI_TTL_NORMAL",
    "ISOCPP_WIKI_TTL_MEETING",
    "ISOCPP_WIKI_MEETING_WINDOWS",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)


def test_requires_some_credentials(monkeypatch):
    with pytest.raises(ConfigError):
        Config.from_env(load_env_file=False)


def test_base_url_is_fixed(monkeypatch):
    # The base URL is centralized, not read from the environment.
    monkeypatch.setenv("WIKI_BASE_URL", "https://ignored.example")
    monkeypatch.setenv("WIKI_BOT_USERNAME", "Acct@bot")
    monkeypatch.setenv("WIKI_BOT_PASSWORD", "secret")
    cfg = Config.from_env(load_env_file=False)
    assert cfg.base_url == WIKI_BASE_URL == "https://wiki.isocpp.org"
    assert cfg.api_url == "https://wiki.isocpp.org/api.php"
    assert [c.label for c in cfg.ordered_credentials] == ["bot"]


def test_both_credentials_bot_first(monkeypatch):
    monkeypatch.setenv("WIKI_BOT_USERNAME", "Acct@bot")
    monkeypatch.setenv("WIKI_BOT_PASSWORD", "secret")
    monkeypatch.setenv("WIKI_USER_USERNAME", "Acct")
    monkeypatch.setenv("WIKI_USER_PASSWORD", "pw")
    cfg = Config.from_env(load_env_file=False)
    assert [c.label for c in cfg.ordered_credentials] == ["bot", "user"]


def test_user_only(monkeypatch):
    monkeypatch.setenv("WIKI_USER_USERNAME", "Acct")
    monkeypatch.setenv("WIKI_USER_PASSWORD", "pw")
    cfg = Config.from_env(load_env_file=False)
    assert [c.label for c in cfg.ordered_credentials] == ["user"]


def test_ttl_and_windows(monkeypatch):
    monkeypatch.setenv("WIKI_USER_USERNAME", "Acct")
    monkeypatch.setenv("WIKI_USER_PASSWORD", "pw")
    monkeypatch.setenv("ISOCPP_WIKI_TTL_NORMAL", "100")
    monkeypatch.setenv("ISOCPP_WIKI_TTL_MEETING", "10")
    monkeypatch.setenv(
        "ISOCPP_WIKI_MEETING_WINDOWS",
        "2026-06-08/2026-06-13, bad, 9999-99-99/2026-01-01, 2026-01-01/2025-01-01",
    )
    cfg = Config.from_env(load_env_file=False)
    assert cfg.ttl_normal_s == 100 and cfg.ttl_meeting_s == 10
    assert cfg.meeting_window_overrides == [
        (__import__("datetime").date(2026, 6, 8), __import__("datetime").date(2026, 6, 13))
    ]


def test_invalid_ttl_falls_back(monkeypatch):
    monkeypatch.setenv("WIKI_USER_USERNAME", "Acct")
    monkeypatch.setenv("WIKI_USER_PASSWORD", "pw")
    monkeypatch.setenv("ISOCPP_WIKI_TTL_NORMAL", "not-a-number")
    monkeypatch.setenv("ISOCPP_WIKI_TTL_MEETING", "-5")
    cfg = Config.from_env(load_env_file=False)
    assert cfg.ttl_normal_s == 604800 and cfg.ttl_meeting_s == 3600
