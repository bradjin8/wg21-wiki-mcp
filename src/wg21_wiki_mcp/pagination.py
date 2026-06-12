"""Opaque cursor encoding and UTF-8-safe content chunking.

Cursors are opaque base64url tokens (callers must not parse them). Page content
is chunked on UTF-8 character boundaries so a slice is always valid text and
reassembling all slices reproduces the page byte-for-byte.
"""

from __future__ import annotations

import base64
import json

from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ErrorData


def encode_cursor(payload: dict) -> str:
    """Encode a small dict as an opaque base64url cursor."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str | None) -> dict:
    """Decode an opaque cursor. Returns {} for None; raises -32602 if malformed."""
    if cursor is None:
        return {}
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="Invalid or expired cursor.")) from exc
    if not isinstance(value, dict):
        raise McpError(ErrorData(code=INVALID_PARAMS, message="Invalid cursor payload."))
    return value


def _floor_utf8_boundary(data: bytes, index: int) -> int:
    """Move ``index`` left to the start of a UTF-8 character (or 0)."""
    if index >= len(data):
        return len(data)
    while index > 0 and (data[index] & 0xC0) == 0x80:
        index -= 1
    return index


def _ceil_utf8_boundary(data: bytes, index: int) -> int:
    """Move ``index`` right to the start of the next UTF-8 character (or end)."""
    while index < len(data) and (data[index] & 0xC0) == 0x80:
        index += 1
    return index


def chunk_utf8(content: str, *, start: int, max_bytes: int) -> tuple[str, int, int, int, bool]:
    """Return (slice, byte_start, byte_end, total_bytes, has_more).

    Splits only on character boundaries. If a single character is larger than
    ``max_bytes``, the whole character is still returned so progress is made.
    """
    data = content.encode("utf-8")
    total = len(data)
    start = max(0, min(start, total))
    start = _ceil_utf8_boundary(data, start)

    if max_bytes <= 0:
        max_bytes = 1
    end = min(start + max_bytes, total)
    end = _floor_utf8_boundary(data, end)
    if end <= start and start < total:
        # A single character exceeds max_bytes; include it whole to progress.
        end = _ceil_utf8_boundary(data, start + 1)

    chunk = data[start:end].decode("utf-8")
    return chunk, start, end, total, end < total
