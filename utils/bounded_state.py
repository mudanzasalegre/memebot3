"""Thread-safe bounded local state; no provider entitlement or freshness proof."""
from collections import OrderedDict
from collections.abc import MutableMapping
import heapq
import itertools
import math
import os
import threading
import time


def bounded_int(value, default, low=1, high=65536):
    try:
        parsed = int(value)
        return default if isinstance(value, bool) else max(low, min(high, parsed))
    except (ValueError, TypeError, OverflowError):
        return default


class TTLStore:
    """LRU entries plus a bounded expiry index; receipts/values are unchanged.

    Expired unqueried entries are swept on ordinary operations. Auxiliary heap
    records compact at max(64,2*capacity), including repeated same-key writes.
    A backwards/invalid local clock invalidates state, never refreshes it.
    Values are not deep-copied here: adapters own copy/sentinel semantics.
    """
    def __init__(self, max_entries=8192, *, clock=None):
        self.max_entries = bounded_int(max_entries, 8192)
        self.clock = clock or time.time
        self.entries = OrderedDict()
        self._expiry = []
        self._sequence = itertools.count()
        self._lock = threading.RLock()
        self._last_now = None
        self._expired = self._evicted = self._clock_resets = 0

    def _now(self):
        try:
            now = float(self.clock())
            if not math.isfinite(now): raise ValueError("invalid local clock")
        except (ValueError, TypeError, OverflowError):
            now = None
        if now is None or self._last_now is not None and now < self._last_now:
            self.entries.clear()
            self._expiry.clear()
            self._clock_resets += 1
        self._last_now = now
        return now

    def _compact(self):
        if len(self._expiry) > max(64, 2*self.max_entries):
            self._expiry = [(expiry, next(self._sequence), key)
                for key, (expiry, _) in self.entries.items()]
            heapq.heapify(self._expiry)

    def _prune(self, now):
        if now is None: return
        while self._expiry and self._expiry[0][0] <= now:
            expiry, _, key = heapq.heappop(self._expiry)
            current = self.entries.get(key)
            if current is not None and current[0] == expiry:
                self.entries.pop(key, None)
                self._expired += 1
        self._compact()

    def get(self, key, default=None):
        with self._lock:
            now = self._now()
            self._prune(now)
            record = self.entries.get(key)
            if record is None: return default
            expiry, value = record
            if now is None or expiry <= now:
                self.entries.pop(key, None)
                return default
            self.entries.move_to_end(key)
            return value

    def set(self, key, value, ttl=60):
        with self._lock:
            now = self._now()
            self._prune(now)
            try:
                seconds = float(ttl)
                expiry = now + seconds if now is not None else float("nan")
                valid = not isinstance(ttl, bool) and math.isfinite(seconds) and seconds > 0 and math.isfinite(expiry) and expiry > now
            except (ValueError, TypeError, OverflowError):
                valid = False
            if not valid:
                self.entries.pop(key, None)
                self._compact()
                return
            self.entries[key] = (expiry, value)
            self.entries.move_to_end(key)
            heapq.heappush(self._expiry, (expiry, next(self._sequence), key))
            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)
                self._evicted += 1
            self._compact()

    def delete(self, key):
        with self._lock:
            self.entries.pop(key, None)
            self._compact()

    def clear(self):
        with self._lock:
            self.entries.clear()
            self._expiry.clear()
            self._last_now = None

    def snapshot(self):
        with self._lock:
            self._prune(self._now())
            return {"entries": len(self.entries), "capacity": self.max_entries,
                "expiry_records": len(self._expiry), "expired": self._expired,
                "capacity_evictions": self._evicted, "clock_resets": self._clock_resets}


class BoundedFailureCounter(MutableMapping):
    """Recent per-key consecutive failure state, capped at escalation threshold4."""
    def __init__(self, max_entries=None, retention_s=None, *, clock=None):
        capacity = os.getenv("PROVIDER_FAILURE_MAX_ENTRIES", "8192") if max_entries is None else max_entries
        retention = os.getenv("PROVIDER_FAILURE_RETENTION_S", "3600") if retention_s is None else retention_s
        self.retention_s = bounded_int(retention, 3600, 1, 86400)
        self._store = TTLStore(capacity, clock=clock or time.monotonic)

    def __getitem__(self, key):
        value = self._store.get(key)
        if value is None: raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        self._store.set(key, bounded_int(value, 1, 1, 4), self.retention_s)

    def __delitem__(self, key):
        with self._store._lock:
            if self._store.get(key) is None: raise KeyError(key)
            self._store.delete(key)

    def __iter__(self):
        with self._store._lock:
            self._store.snapshot()
            return iter(tuple(self._store.entries))

    def __len__(self): return self._store.snapshot()["entries"]
    def clear(self): self._store.clear()
    def snapshot(self): return {**self._store.snapshot(), "retention_s": self.retention_s, "max_count": 4}

    def increment(self, key):
        with self._store._lock:
            count = min(4, self._store.get(key, 0)+1)
            self._store.set(key, count, self.retention_s)
            return count


def increment_failure(counter, key):
    """Atomic for production counters; preserves plain-mapping test adapters."""
    if isinstance(counter, BoundedFailureCounter): return counter.increment(key)
    count = min(4, counter.get(key, 0)+1)
    counter[key] = count
    return count
