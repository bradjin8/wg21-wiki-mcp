"""Tests for deadline helpers."""

from __future__ import annotations

import time

import pytest

from wg21_wiki_mcp.deadlines import (
    API_TIMEOUT_MSG,
    MEETING_TOOL_TIMEOUT_MSG,
    PAGE_FETCH_TIMEOUT_MSG,
    composite_deadline,
    http_timeout,
    timeout_remaining,
)
from wg21_wiki_mcp.models import FetchError


def test_timeout_remaining_none_deadline():
    assert timeout_remaining(None) is None


def test_timeout_remaining_raises_when_exhausted(monkeypatch):
    base = time.monotonic()
    monkeypatch.setattr("wg21_wiki_mcp.deadlines.time.monotonic", lambda: base + 10.0)
    with pytest.raises(FetchError, match="timed out"):
        timeout_remaining(base + 1.0, on_exceeded=PAGE_FETCH_TIMEOUT_MSG)


def test_http_timeout_uses_cap_when_no_deadline():
    assert http_timeout(None, cap=30.0) == 30.0


def test_distinct_timeout_messages():
    assert API_TIMEOUT_MSG != MEETING_TOOL_TIMEOUT_MSG
    assert "timed out" in PAGE_FETCH_TIMEOUT_MSG


def test_composite_deadline_is_future(monkeypatch):
    base = 100.0
    monkeypatch.setattr("wg21_wiki_mcp.deadlines.time.monotonic", lambda: base)
    assert composite_deadline(5.0) == 105.0
