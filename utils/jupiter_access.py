"""Current Jupiter gateway access and process-wide, cross-loop request budgets.

Keyless != disabled. Local budgets do not prove provider access, organisation-
wide quota, network freshness or profitability. No secret is logged or stored.
"""
from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


class BudgetUnavailable(RuntimeError):
    """This local request has not been dispatched; never a trade no-fill proof."""


def endpoint(value, operation):
    paths = {"quote": "/swap/v1/quote", "swap": "/swap/v1/swap", "price": "/price/v3"}
    canonical = f"https://api.jup.ag{paths[operation]}"
    p = urlsplit(value or canonical)
    if (not p.hostname or p.username is not None or p.password is not None or p.fragment
            or (p.scheme != "https" and not (p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError("Unsupported Jupiter observation/build endpoint")
    if p.port is not None and not 1 <= p.port <= 65535:
        raise ValueError("Invalid Jupiter endpoint port")
    if p.hostname in {"api.jup.ag", "lite-api.jup.ag", "quote-api.jup.ag"}:
        known = {paths[operation]}
        if operation != "price": known.add(f"/v6/{operation}")
        if p.scheme != "https" or p.port not in (None, 443) or p.query or p.path not in known:
            raise ValueError("Unsupported official Jupiter endpoint contract")
        return canonical
    # Explicit self-hosted adapters stay possible, with no Jupiter credentials.
    return urlunsplit(p)


def headers(api_key, url):
    p = urlsplit(url)
    result = {"accept": "application/json"}
    if not isinstance(api_key, str) or len(api_key) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in api_key):
        raise ValueError("Invalid Jupiter credential transport")
    if (api_key and p.scheme == "https" and p.hostname == "api.jup.ag"
            and p.port in (None, 443) and p.username is None and p.password is None):
        result["x-api-key"] = api_key
    return result


def official(url):
    p = urlsplit(url)
    return p.scheme == "https" and p.hostname == "api.jup.ag" and p.port in (None, 443) and p.username is None and p.password is None


def rates(keyed):
    raw = os.getenv("JUP_API_RPS", "").strip()
    try:
        main = float(raw) if raw else (1.0 if keyed else .5)
    except ValueError:
        raise ValueError("Invalid configured Jupiter request budget") from None
    if not math.isfinite(main) or not 0 < main <= 150:
        raise ValueError("Invalid configured Jupiter request budget")
    if not keyed: main = min(main, .5)
    return main, (100.0 if keyed and main > 1 else 50.0 if keyed else 20.0)


@dataclass
class _Bucket:
    admissions: deque = field(default_factory=deque)
    last: float | None = None
    cooldown: float = 0.0
    tickets: dict = field(default_factory=dict)
    granted: int = 0
    refused: int = 0
    cancelled: int = 0
    statuses: dict = field(default_factory=dict)


class AccessBudget:
    """Pacings + 60s sliding windows, bounded priority queues, no asyncio lock.

    Main Swap/Price callers share one mode bucket, not one bucket per API key.
    Execute has its own bucket, so background quotes cannot delay a signed POST.
    Exit quotes/orders (priority0) precede queued entries(1), probes(2), prices(3).
    Already admitted requests are not preempted. This is process-local only.
    """
    def __init__(self, *, clock=time.monotonic, wall=time.time, sleep=asyncio.sleep):
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self._lock = threading.Lock()
        self._buckets = {}
        self._serial = 0

    def _bucket(self, keyed, execute):
        return self._buckets.setdefault((bool(keyed), bool(execute)), _Bucket())

    async def acquire(self, *, keyed, execute=False, priority=2, max_wait=6):
        if type(priority) is not int or not 0 <= priority <= 3:
            raise ValueError("Invalid Jupiter request priority")
        if isinstance(max_wait, bool) or not isinstance(max_wait, (int, float)) or not math.isfinite(max_wait) or not 0 < max_wait <= 60:
            raise ValueError("Invalid Jupiter request wait bound")
        main_rate, execute_rate = rates(keyed)
        rate = execute_rate if execute else main_rate
        window_limit = max(1, math.floor(rate * 60))
        start = self.clock()
        with self._lock:
            bucket = self._bucket(keyed, execute)
            if len(bucket.tickets) >= 128:
                bucket.refused += 1
                raise BudgetUnavailable("Jupiter local request queue is full")
            self._serial += 1
            ticket = self._serial
            bucket.tickets[ticket] = (priority, ticket)
        try:
            while True:
                now = self.clock()
                with self._lock:
                    while bucket.admissions and bucket.admissions[0] <= now - 60:
                        bucket.admissions.popleft()
                    due = max(bucket.cooldown, bucket.last + 1 / rate if bucket.last is not None else now)
                    if len(bucket.admissions) >= window_limit:
                        due = max(due, bucket.admissions[0] + 60)
                    first = min(bucket.tickets, key=bucket.tickets.get) == ticket
                    if first and due <= now and now - start <= max_wait:
                        bucket.admissions.append(now)
                        bucket.last = now
                        bucket.granted += 1
                        return
                    remaining = max_wait - (now - start)
                    if remaining <= 0 or due - now > remaining:
                        bucket.refused += 1
                        raise BudgetUnavailable("Jupiter local request budget unavailable within deadline")
                    delay = min(.1, remaining, max(.001, due - now) if first else .1)
                await self.sleep(delay)
        except asyncio.CancelledError:
            with self._lock: bucket.cancelled += 1
            raise
        finally:
            with self._lock: bucket.tickets.pop(ticket, None)

    def observe(self, *, keyed, execute=False, status, headers):
        if type(status) is not int:
            return
        with self._lock:
            bucket = self._bucket(keyed, execute)
            bucket.statuses[status] = bucket.statuses.get(status, 0) + 1
        if status not in {200, 429}:
            return  # Permission failure never drops credentials or approves a retry.
        def number(name):
            value = headers.get(name) if headers is not None else None
            if not isinstance(value, str) or len(value) > 64:
                return None
            try: result = float(value)
            except ValueError: return None
            return result if math.isfinite(result) else None
        remaining, reset = number("x-ratelimit-remaining"), number("x-ratelimit-reset")
        if status == 429 or remaining is not None and remaining <= 0:
            # Gateway reset is Unix seconds. Never interpret it as a duration,
            # clear all local reservations, or expand quota from peer headers.
            delay = max(1.0, reset - self.wall()) if reset is not None else 1.0
            if delay > 120: delay = 120.0  # Bound corrupt/server-clock-skew metadata.
            with self._lock:
                bucket.cooldown = max(bucket.cooldown, self.clock() + delay)

    def snapshot(self):
        with self._lock:
            return {f"{'keyed' if mode else 'keyless'}_{'execute' if execute else 'main'}":
                {"queued": len(b.tickets), "granted": b.granted, "refused": b.refused,
                    "cancelled": b.cancelled, "statuses": dict(b.statuses)}
                for (mode, execute), b in self._buckets.items()}


_BUDGET = AccessBudget()


async def acquire(url, *, api_key, priority=2, max_wait=6):
    if official(url):
        await _BUDGET.acquire(keyed=bool(api_key), execute=urlsplit(url).path == "/swap/v2/execute",
            priority=priority, max_wait=max_wait)


def observe(url, *, api_key, response):
    if official(url):
        _BUDGET.observe(keyed=bool(api_key), execute=urlsplit(url).path == "/swap/v2/execute",
            status=response.status, headers=getattr(response, "headers", None))
