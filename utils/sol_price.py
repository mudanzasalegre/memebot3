"""Bounded SOL/USD observations; old or assumed rates are not fresh prices.

Provider time and the original client receipt have independent age limits. A
failed refresh never turns a last-good value into current money. Scalar paper,
research and execution callers share this contract.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import logging
import math
import os
import threading
import time
from typing import Any

import aiohttp

logger = logging.getLogger("sol_price")

_COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=solana&vs_currencies=usd&include_last_updated_at=true&precision=full"
)
MAX_BODY_BYTES = 65536
RETRY_COOLDOWN_S = 10.0
FUTURE_TOLERANCE_S = 5.0


def _setting(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        if math.isfinite(value) and minimum <= value <= maximum:
            return value
    except (TypeError, ValueError, OverflowError):
        pass
    logger.warning("[sol_price] Invalid %s; using bounded default", name)
    return default


_TTL_OK = _setting("COINGECKO_SOL_TTL", 60.0, 1.0, 60.0)
_MAX_MARKET_AGE_S = _setting("COINGECKO_SOL_MAX_MARKET_AGE_S", 120.0, 1.0, 300.0)
_TIMEOUT = _setting("COINGECKO_TIMEOUT", 6.0, 0.1, 30.0)
_DEMO_API_KEY = (os.getenv("COINGECKO_DEMO_API_KEY", "") or "").strip()
try:
    _SOL_USD_OVERRIDE = float((os.getenv("SOL_USD_OVERRIDE", "") or "").strip())
except (TypeError, ValueError, OverflowError):
    _SOL_USD_OVERRIDE = 0.0


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class SolUsdObservation:
    status: str
    price_usd: float | None = None
    received_at: float | None = None
    market_updated_at: float | None = None
    source: str = "coingecko_simple_price"
    reason: str = "unavailable"
    assumed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fresh_sol_usd(observation: Any, *, now: float | None = None) -> float | None:
    """Check a rate; this does not certify swap fills or exchange liquidity."""
    if not isinstance(observation, SolUsdObservation):
        return None
    price = _number(observation.price_usd)
    received = _number(observation.received_at)
    updated = _number(observation.market_updated_at)
    stamp = _number(time.time() if now is None else now)
    if (observation.status != "OK" or observation.assumed is not False
            or observation.source != "coingecko_simple_price" or price is None or price <= 0
            or received is None or received <= 0 or updated is None or updated <= 0
            or stamp is None or stamp <= 0):
        return None
    if not 0 <= stamp - received <= _TTL_OK:
        return None
    if not -FUTURE_TOLERANCE_S <= received - updated <= _MAX_MARKET_AGE_S:
        return None
    if not -FUTURE_TOLERANCE_S <= stamp - updated <= _MAX_MARKET_AGE_S:
        return None
    return price


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _bad_constant(_value):
    raise ValueError("Non-finite JSON constant")


def parse_sol_usd_body(body: bytes, *, received_at: float) -> SolUsdObservation:
    """Validate the requested coin/currency and both clocks without HTTP."""
    try:
        if not isinstance(body, bytes) or not 0 < len(body) <= MAX_BODY_BYTES:
            raise ValueError("Invalid body size")
        payload = json.loads(body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object, parse_constant=_bad_constant)
        if not isinstance(payload, dict) or set(payload) != {"solana"}:
            raise ValueError("Wrong response population")
        point = payload["solana"]
        if not isinstance(point, dict) or set(point) != {"usd", "last_updated_at"}:
            raise ValueError("Wrong price fields")
        price, updated = _number(point["usd"]), _number(point["last_updated_at"])
        observation = SolUsdObservation("OK", price, received_at, updated, reason="checked_provider_timestamp")
        if fresh_sol_usd(observation, now=received_at) is None:
            return SolUsdObservation("ERR", received_at=received_at, reason="invalid_or_stale_point")
        return observation
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        return SolUsdObservation("ERR", reason="invalid_body")


async def _read_body(response) -> bytes:
    length = response.headers.get("Content-Length")
    if length is not None and (not str(length).isdigit() or int(length) > MAX_BODY_BYTES):
        raise ValueError("Invalid Content-Length")
    chunks, size = [], 0
    while size <= MAX_BODY_BYTES:
        chunk = await response.content.read(min(16384, MAX_BODY_BYTES + 1 - size))
        if not isinstance(chunk, bytes):
            raise ValueError("Invalid transport body")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
    raise ValueError("Response body exceeds limit")


async def _fetch_observation() -> SolUsdObservation:
    try:
        headers = {"x-cg-demo-api-key": _DEMO_API_KEY} if _DEMO_API_KEY else {}
        async with aiohttp.ClientSession() as session:
            async with session.get(_COINGECKO_URL, headers=headers, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=_TIMEOUT)) as response:
                if response.status != 200:
                    logger.warning("[sol_price] CoinGecko HTTP %s", response.status)
                    return SolUsdObservation("ERR", reason="http_unavailable")
                body = await _read_body(response)
                received_at = time.time()  # before parsing or context teardown
                return parse_sol_usd_body(body, received_at=received_at)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Exception strings may contain URLs/headers; never log credential data.
        logger.warning("[sol_price] CoinGecko observation unavailable (%s)", type(exc).__name__)
        return SolUsdObservation("ERR", reason="transport_or_body_unavailable")


_CACHE: tuple[SolUsdObservation, float] | None = None
_FAILURE: SolUsdObservation | None = None
_RETRY_AT = 0.0
_GENERATION = 0
class _LoopNeutralLock:
    """Process-wide single flight without binding a lock to one event loop.

    Non-blocking acquisition yields in each caller's own loop. Cancellation
    before acquisition never owns the lock; context exit releases an owner.
    """
    def __init__(self):
        self._lock = threading.Lock()

    async def __aenter__(self):
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(.01)
        return self

    async def __aexit__(self, *args):
        self._lock.release()

    def locked(self):
        return self._lock.locked()


_LOCK = _LoopNeutralLock()


def _cached() -> SolUsdObservation | None:
    cached = _CACHE  # detached immutable tuple also safe during another loop's refresh
    if (cached is not None and time.monotonic() < cached[1]
            and fresh_sol_usd(cached[0]) is not None):
        return cached[0]
    return None


async def get_sol_usd_observation(*, force_refresh: bool = False) -> SolUsdObservation:
    global _CACHE, _FAILURE, _RETRY_AT, _GENERATION
    if math.isfinite(_SOL_USD_OVERRIDE) and _SOL_USD_OVERRIDE > 0:
        # A scenario constant has no provider or client clock proof.
        return SolUsdObservation("ASSUMED", _SOL_USD_OVERRIDE, source="configured_override",
            reason="fixed_scenario_not_market_evidence", assumed=True)
    cached = _cached()
    if cached is not None and not force_refresh:
        return cached
    generation = _GENERATION
    async with _LOCK:
        cached = _cached()
        if cached is not None and (not force_refresh or generation != _GENERATION):
            return cached  # concurrent callers share the original receipt
        if time.monotonic() < _RETRY_AT and _FAILURE is not None:
            return _FAILURE
        _CACHE = None  # failed refresh cannot resurrect the previous price
        observation = await _fetch_observation()
        _GENERATION += 1
        if fresh_sol_usd(observation) is not None:
            remaining = max(0.0, _TTL_OK - (time.time() - observation.received_at))
            _CACHE = (observation, time.monotonic() + remaining)
            _FAILURE, _RETRY_AT = None, 0.0
            return observation
        _FAILURE = SolUsdObservation("ERR", reason="refresh_unavailable_or_expired")
        _RETRY_AT = time.monotonic() + RETRY_COOLDOWN_S
        return _FAILURE


async def get_sol_usd(*, force_refresh: bool = False, allow_assumed: bool = False) -> float | None:
    observation = await get_sol_usd_observation(force_refresh=force_refresh)
    if allow_assumed is True and observation.status == "ASSUMED" and observation.assumed is True:
        return observation.price_usd
    return fresh_sol_usd(observation)


async def _fetch_sol_usd() -> float | None:
    """Legacy direct-fetch adapter; preserves point age checks."""
    return fresh_sol_usd(await _fetch_observation())


async def amount_sol_to_usd(amount_sol: float) -> float | None:
    amount = _number(amount_sol)
    if amount is None or amount < 0:
        return None
    if amount == 0:
        return 0.0
    sol_usd = await get_sol_usd()
    if sol_usd is None:
        return None
    result = amount * sol_usd
    return result if math.isfinite(result) and result > 0 else None


__all__ = ["SolUsdObservation", "fresh_sol_usd", "parse_sol_usd_body",
           "get_sol_usd_observation", "get_sol_usd", "amount_sol_to_usd"]
