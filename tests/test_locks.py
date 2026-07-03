"""Unit tests for EvictableLockMap."""

from __future__ import annotations

import pytest

from wg21_wiki_mcp import locks
from wg21_wiki_mcp.locks import EvictableLockMap


def _users(lock_map: EvictableLockMap, key: str) -> int:
    slot = lock_map.slots.get(key)
    return 0 if slot is None else slot.users


def _fake_lock_factory(*, acquire_result: bool = True, acquire_error: BaseException | None = None):
    """Return a Lock stand-in whose acquire() outcome is fully controlled.

    The first instance is the map guard (always succeeds); later instances are
    per-key slot locks that honour ``acquire_result`` / ``acquire_error``.
    """

    class FakeLock:
        _next_id = 0

        def __init__(self) -> None:
            self._id = FakeLock._next_id
            FakeLock._next_id += 1
            self._held = False

        def __enter__(self) -> FakeLock:
            self.acquire()
            return self

        def __exit__(self, *_exc: object) -> bool:
            self.release()
            return False

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            if self._id == 0:
                self._held = True
                return True
            if acquire_error is not None:
                raise acquire_error
            if acquire_result:
                self._held = True
            return acquire_result

        def release(self) -> None:
            self._held = False

        def locked(self) -> bool:
            return self._held

    return FakeLock


def test_acquire_success_and_release_clears_slot():
    lock_map = EvictableLockMap(max_entries=256)
    lock = lock_map.acquire("k1")
    try:
        assert lock.locked()
        assert _users(lock_map, "k1") == 1
    finally:
        lock_map.release("k1", lock)
    assert "k1" not in lock_map.slots


def test_acquire_timeout_false_rolls_back_users(monkeypatch):
    monkeypatch.setattr(locks.threading, "Lock", _fake_lock_factory(acquire_result=False))
    lock_map = EvictableLockMap(max_entries=256)
    with pytest.raises(TimeoutError, match="Timed out acquiring lock"):
        lock_map.acquire("k1", timeout=0.01)
    assert _users(lock_map, "k1") == 0


def test_acquire_timeout_calls_on_timeout_factory(monkeypatch):
    monkeypatch.setattr(locks.threading, "Lock", _fake_lock_factory(acquire_result=False))
    lock_map = EvictableLockMap(max_entries=256)
    with pytest.raises(ValueError, match="custom timeout"):
        lock_map.acquire(
            "k1",
            timeout=0.01,
            on_timeout=lambda: ValueError("custom timeout"),
        )
    assert _users(lock_map, "k1") == 0


def test_acquire_exception_rolls_back_users(monkeypatch):
    monkeypatch.setattr(
        locks.threading,
        "Lock",
        _fake_lock_factory(acquire_error=RuntimeError("acquire blew up")),
    )
    lock_map = EvictableLockMap(max_entries=256)
    with pytest.raises(RuntimeError, match="acquire blew up"):
        lock_map.acquire("k1", timeout=0.01)
    assert _users(lock_map, "k1") == 0


def test_acquire_contention_timeout_leaves_holder_user_count():
    """A contended lock.acquire(timeout=0) failure rolls back only the waiter."""
    lock_map = EvictableLockMap(max_entries=256)
    held = lock_map.acquire("k1")
    try:
        with pytest.raises(TimeoutError, match="Timed out acquiring lock"):
            lock_map.acquire("k1", timeout=0.0)
        assert _users(lock_map, "k1") == 1
    finally:
        lock_map.release("k1", held)
    assert _users(lock_map, "k1") == 0
