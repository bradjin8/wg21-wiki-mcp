"""Wall-clock deadline helpers for bounded fetch, API, and composite tool waits."""

from __future__ import annotations

import time

from .models import FetchError

API_TIMEOUT_MSG = "API call timed out."
PAGE_FETCH_TIMEOUT_MSG = "Page fetch timed out waiting for the wiki."
MEETING_TOOL_TIMEOUT_MSG = "Meeting tool timed out waiting for the wiki."


def composite_deadline(max_wait_s: float) -> float:
    """Return a monotonic deadline ``max_wait_s`` seconds from now."""
    return time.monotonic() + max_wait_s


def timeout_remaining(
    deadline: float | None,
    *,
    on_exceeded: str = PAGE_FETCH_TIMEOUT_MSG,
) -> float | None:
    """Return seconds left until ``deadline``, or raise :class:`FetchError` if exhausted."""
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchError(on_exceeded)
    return remaining


def http_timeout(
    deadline: float | None,
    *,
    cap: float = 30.0,
    on_exceeded: str = API_TIMEOUT_MSG,
) -> float:
    """Per-request HTTP timeout derived from the remaining deadline (capped)."""
    remaining = timeout_remaining(deadline, on_exceeded=on_exceeded)
    if remaining is None:
        return cap
    return min(remaining, cap)
