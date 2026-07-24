"""Reference-counted, bounded per-key lock map.

Both the page fetcher (per-title in-process locks) and the outlink cache
(per-meeting locks) need the same primitive: a map of ``threading.Lock``
objects keyed by a string, where each slot tracks how many callers currently
reference it so an idle slot can be safely evicted. Keeping a single
implementation prevents the two copies from drifting apart, which previously
led to duplicated reference-count leaks on timeout paths.

The user count is only ever incremented once acquisition is about to be
attempted, and is rolled back on *every* failure path (timeout or exception),
so a slot can never be stranded with a non-zero count.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

_DEFAULT_MAX_ENTRIES = 256
DEFAULT_MAX_LOCK_ENTRIES = _DEFAULT_MAX_ENTRIES


@dataclass
class _LockSlot:
    """A per-key lock plus the count of callers currently referencing it."""

    lock: threading.Lock
    users: int = 0


class EvictableLockMap:
    """A bounded map of per-key locks with safe reference-counted eviction."""

    def __init__(self, *, max_entries: int | Callable[[], int] = _DEFAULT_MAX_ENTRIES) -> None:
        """Create an empty lock map.

        ``max_entries`` bounds the number of retained slots; pass a callable to
        resolve the bound dynamically (e.g. to honour a monkeypatched module
        constant in tests).
        """
        self.slots: dict[str, _LockSlot] = {}
        self.guard = threading.Lock()
        self._max_entries = max_entries

    def acquire(
        self,
        key: str,
        *,
        timeout: float | None = None,
        on_timeout: Callable[[], BaseException] | None = None,
    ) -> threading.Lock:
        """Acquire the lock for ``key``, registering this caller as a user.

        ``timeout`` is the maximum seconds to wait (``None`` blocks forever).
        On timeout the user count is rolled back and ``on_timeout()`` is raised
        (or :class:`TimeoutError` if no factory is given). The user count is
        also rolled back if ``lock.acquire`` itself raises. Pass the returned
        lock back to :meth:`release`.
        """
        with self.guard:
            slot = self.slots.get(key)
            if slot is None:
                slot = _LockSlot(threading.Lock())
                self.slots[key] = slot
            slot.users += 1
            lock = slot.lock
        try:
            acquired = lock.acquire(timeout=timeout) if timeout is not None else lock.acquire()
        except BaseException:
            self._release_slot(key, lock)
            raise
        if not acquired:
            self._release_slot(key, lock)
            raise on_timeout() if on_timeout is not None else TimeoutError(f"Timed out acquiring lock for {key!r}")
        return lock

    def release(self, key: str, lock: threading.Lock) -> None:
        """Release a previously-acquired lock and drop this caller's user count."""
        lock.release()
        self._release_slot(key, lock)

    def _release_slot(self, key: str, lock: threading.Lock) -> None:
        """Decrement the user count for ``key`` and evict idle slots."""
        with self.guard:
            slot = self.slots.get(key)
            if slot is None or slot.lock is not lock:
                return
            slot.users -= 1
            if slot.users == 0 and not lock.locked():
                del self.slots[key]
            self._evict_idle()

    def _evict_idle(self) -> None:
        """Drop unreferenced, unlocked slots once the map exceeds its bound."""
        max_entries = self._max_entries() if callable(self._max_entries) else self._max_entries
        if len(self.slots) <= max_entries:
            return
        for key, slot in list(self.slots.items()):
            if slot.users == 0 and not slot.lock.locked():
                del self.slots[key]
            if len(self.slots) <= max_entries:
                return
