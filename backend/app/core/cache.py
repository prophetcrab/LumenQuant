"""Tiny TTL cache for read-heavy, low-churn queries.

Deliberately not thread-safe beyond CPython dict atomicity: values are immutable
snapshots, and a duplicate recompute under a race is harmless.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class TTLCache:
    def __init__(self, ttl_seconds: float = 1.0, max_entries: int = 512) -> None:
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._data: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> object | None:
        item = self._data.get(key)
        if item is None:
            self.misses += 1
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, key: str, value: object) -> None:
        if len(self._data) >= self.max_entries:
            # Cheap eviction: drop the oldest third by expiry.
            for stale_key in sorted(self._data, key=lambda k: self._data[k][0])[: self.max_entries // 3]:
                self._data.pop(stale_key, None)
        self._data[key] = (time.monotonic() + self.ttl, value)

    def get_or_load(self, key: str, loader: Callable[[], T]) -> T:
        cached = self.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        # Collapse a thundering herd on expiry: one loader per key at a time.
        lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            cached = self.get(key)
            if cached is not None:
                return cached  # type: ignore[return-value]
            value = loader()
            self.put(key, value)
            return value

    def clear(self) -> None:
        self._data.clear()

    def stats(self) -> dict[str, int | float]:
        total = self.hits + self.misses
        return {
            "entries": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
        }
