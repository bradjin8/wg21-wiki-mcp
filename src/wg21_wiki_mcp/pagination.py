"""Opaque cursor encoding and UTF-8-safe content chunking.

Cursors are opaque base64url tokens (callers must not parse them). Page content
is chunked on UTF-8 character boundaries so a slice is always valid text and
reassembling all slices reproduces the page byte-for-byte.
"""

from __future__ import annotations

import base64
import hashlib
import json

from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS


def body_digest(data: bytes) -> str:
    """Short content digest that binds a chunk cursor to exact sanitized bytes.

    Guards against resuming a cursor into a *different* body that happens to share
    the same revision and byte length (e.g. two URL-hygiene results of equal
    length, or a section vs. full-page fetch that share the page revid).
    """
    return hashlib.blake2b(data, digest_size=16).hexdigest()


# Producer keys stamped into every cursor. A cursor is only honored by the tool
# that minted it: one mint produces several incompatible payload shapes (result
# offset ``o``, API continuation ``c``, page-chunk identity ``o/r/t/h``) behind
# identically typed ``next_cursor`` fields, so without a producer key a cursor
# handed to the wrong tool is silently misread (e.g. a page-chunk byte offset read
# as a search result offset) instead of raising the -32602 contract.
CURSOR_KIND_SEARCH = "search"
CURSOR_KIND_PAGES = "pages"
CURSOR_KIND_MEETINGS = "meetings"
CURSOR_KIND_CHANGES = "changes"
CURSOR_KIND_PAGE_CHUNK = "page"


def encode_cursor(payload: dict, *, kind: str) -> str:
    """Encode a small dict as an opaque base64url cursor stamped with a producer ``kind``.

    ``kind`` binds the token to the tool that minted it (see ``decode_cursor``).
    """
    envelope = {"k": kind, "p": payload}
    raw = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str | None, *, kind: str) -> dict:
    """Decode an opaque cursor minted by the same ``kind``.

    Returns ``{}`` for ``None``. Raises -32602 if the token is malformed, is not a
    dict envelope, or was minted by a different tool (producer ``kind`` mismatch),
    so a cursor passed to the wrong tool fails loudly instead of being misread.
    """
    if cursor is None:
        return {}
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        # RecursionError (a RuntimeError) is raised by json.loads on deeply nested
        # input; treat it like any other malformed cursor rather than a transient fault.
        raise MCPError(INVALID_PARAMS, "Invalid or expired cursor.") from exc
    if not isinstance(value, dict):
        raise MCPError(INVALID_PARAMS, "Invalid cursor payload.")
    if value.get("k") != kind:
        raise MCPError(INVALID_PARAMS, "Invalid or expired cursor.")
    payload = value.get("p")
    if not isinstance(payload, dict):
        raise MCPError(INVALID_PARAMS, "Invalid cursor payload.")
    return payload


def _validated_offset(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MCPError(INVALID_PARAMS, "Invalid cursor offset.")
    if value < 0:
        raise MCPError(INVALID_PARAMS, "Invalid cursor offset.")
    return value


def cursor_offset(cursor: str | None, *, kind: str, default: int = 0) -> int:
    """Return a non-negative integer offset from a ``kind``-scoped cursor payload.

    Raises:
        MCPError: if the cursor envelope is malformed, was minted by a different
            tool, or ``o`` is not a non-negative integer.
    """
    default = _validated_offset(default)
    payload = decode_cursor(cursor, kind=kind)
    if "o" not in payload:
        return default
    return _validated_offset(payload["o"])


def page_chunk_offset(
    cursor: str | None,
    *,
    revid: int | None,
    total_bytes: int,
    digest: str,
) -> int:
    """Return a byte offset bound to ``revid``, ``total_bytes`` and ``digest``.

    Sanitized page text can change (and even keep the same length) between calls,
    so the offset is only honored when the cursor's bound revision, byte length,
    and content digest all still match the body being chunked.
    """
    payload = decode_cursor(cursor, kind=CURSOR_KIND_PAGE_CHUNK)
    if not payload:
        return 0
    offset = _validated_offset(payload.get("o", 0))
    if payload.get("r") != revid or payload.get("t") != total_bytes or payload.get("h") != digest:
        raise MCPError(INVALID_PARAMS, "Invalid or expired cursor.")
    return offset


def encode_page_chunk_cursor(byte_end: int, *, revid: int | None, total_bytes: int, digest: str) -> str:
    """Encode a chunk cursor bound to the sanitized body identity (revid, length, digest)."""
    return encode_cursor({"o": byte_end, "r": revid, "t": total_bytes, "h": digest}, kind=CURSOR_KIND_PAGE_CHUNK)


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
