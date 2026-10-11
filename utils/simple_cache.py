"""Bounded thread-safe local TTL/LRU cache; original values are not restamped."""
import os
import time

from utils.bounded_state import TTLStore, bounded_int
from utils.request_ownership import OwnedRequests

_MAX_ENTRIES = bounded_int(os.getenv("MEMORY_CACHE_MAX_ENTRIES", "8192"), 8192)
_STORE = TTLStore(_MAX_ENTRIES, clock=lambda: time.time())
_CACHE = _STORE.entries  # Existing diagnostic/test tuple contract (expiry,value).
_LOADERS = OwnedRequests(name="local-cache-loader")


def cache_get(key): return _STORE.get(key)


def cache_set(key, value, ttl=60):
    # Allows bounded isolated fixtures; production capacity is parsed once.
    with _STORE._lock:
        _STORE.max_entries = bounded_int(_MAX_ENTRIES, 8192)
        _STORE.set(key, value, ttl)


def cache_delete(key): _STORE.delete(key)
def cache_clear(): _STORE.clear()
def cache_snapshot(): return {**_STORE.snapshot(), "loaders": _LOADERS.snapshot()}


async def cache_get_or_set(key, coro, ttl=60):
    hit = cache_get(key)
    if hit is not None: return hit
    async def load():
        value = await coro()
        cache_set(key, value, ttl)
        return value
    return await _LOADERS.run(key, load)
