"""Pagination cursor + UTF-8 chunking tests."""

from __future__ import annotations

import pytest
from mcp.shared.exceptions import McpError

from wg21_wiki_mcp.pagination import chunk_utf8, cursor_offset, decode_cursor, encode_cursor


def test_cursor_roundtrip():
    payload = {"o": 1234, "c": "abc"}
    assert decode_cursor(encode_cursor(payload)) == payload


def test_decode_none_is_empty():
    assert decode_cursor(None) == {}


def test_invalid_cursor_raises_invalid_params():
    with pytest.raises(McpError) as exc:
        decode_cursor("!!!not-base64!!!")
    assert exc.value.error.code == -32602


def test_non_dict_cursor_rejected():
    import base64
    import json

    bad = base64.urlsafe_b64encode(json.dumps([1, 2]).encode()).decode()
    with pytest.raises(McpError):
        decode_cursor(bad)


def test_cursor_offset_defaults_to_zero():
    assert cursor_offset(None) == 0
    assert cursor_offset(encode_cursor({})) == 0


def test_cursor_offset_roundtrip():
    assert cursor_offset(encode_cursor({"o": 42})) == 42


@pytest.mark.parametrize("default", [-1, True])
def test_cursor_offset_rejects_invalid_default(default):
    with pytest.raises(McpError) as exc:
        cursor_offset(None, default=default)
    assert exc.value.error.code == -32602


@pytest.mark.parametrize(
    "payload",
    [
        {"o": "abc"},
        {"o": -1},
        {"o": 1.5},
        {"o": True},
    ],
)
def test_cursor_offset_rejects_invalid_values(payload):
    with pytest.raises(McpError) as exc:
        cursor_offset(encode_cursor(payload))
    assert exc.value.error.code == -32602


def test_chunk_reassembly_is_byte_for_byte():
    text = "Heading\n" + "café \u4e2d\u6587 \U0001f600 " * 50
    pieces: list[str] = []
    start = 0
    while True:
        chunk, _bs, end, total, has_more = chunk_utf8(text, start=start, max_bytes=7)
        pieces.append(chunk)
        # Each slice must itself be valid UTF-8 round-tripping cleanly.
        assert chunk.encode("utf-8").decode("utf-8") == chunk
        start = end
        if not has_more:
            assert end == total
            break
    assert "".join(pieces) == text


def test_chunk_never_splits_codepoint():
    text = "\U0001f600\U0001f601\U0001f602"  # each is 4 UTF-8 bytes
    chunk, _bs, end, _total, has_more = chunk_utf8(text, start=0, max_bytes=2)
    # max_bytes < one char: must still emit exactly one whole char.
    assert chunk == "\U0001f600"
    assert end == 4
    assert has_more


def test_chunk_zero_max_bytes_progresses():
    chunk, _bs, end, _total, _has_more = chunk_utf8("abc", start=0, max_bytes=0)
    assert chunk == "a" and end == 1  # clamped to at least one byte


def test_chunk_start_beyond_end():
    text = "abc"
    chunk, bs, end, total, has_more = chunk_utf8(text, start=100, max_bytes=10)
    assert chunk == "" and bs == 3 and end == 3 and total == 3 and has_more is False
