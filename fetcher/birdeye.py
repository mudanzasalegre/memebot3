"""Solana Birdeye adapters, using documented token/pair/creation contracts.

Market HTTP receipts are not provider market-as-of guarantees. Mint creation
uses a separate, explicit request and is provider-reported, not independently
verified on chain. Ordinary price collection never adds that request or cost.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import datetime as dt
import logging
import math
import os
import time
from typing import Any, Callable

import aiohttp

from analytics.token_time import parse_event_clock, venue_clock_snapshot
from utils.data_utils import sanitize_token_data
from utils.market_observation import MARKET_FIELDS, market_number, stamp_market_observation
from utils.simple_cache import cache_delete, cache_get, cache_set

_API_KEY = os.getenv("BIRDEYE_API_KEY")
_BASE_URL = "https://public-api.birdeye.so"
_TOKEN_EP = "/defi/token_overview"
_POOL_EP = "/defi/v3/pair/overview/single"
_CREATION_EP = "/defi/token_creation_info"


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(int(os.getenv(name, str(default))), 1)
    except (TypeError, ValueError, OverflowError):
        return default


_RPM = _positive_int_env("BIRDEYE_RPM", 60)
_MIN_INTERVAL = 60.0 / _RPM
_TTL_NIL_SHORT = _positive_int_env("BIRDEYE_TTL_NIL_SHORT", 90)
_TTL_NIL_MAX = max(_TTL_NIL_SHORT, _positive_int_env("BIRDEYE_TTL_NIL_MAX", 300))
_SENTINEL_NIL = object()
_last_call_ts = 0.0
_lock = asyncio.Lock()
_fail_count: dict[str, int] = {}
log = logging.getLogger("birdeye")
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_bytes(value: Any, size: int) -> bool:
    """Strict shape even when the optional base58 dependency is unavailable."""
    if not isinstance(value, str) or not value or len(value) > size * 2:
        return False
    number = 0
    for char in value:
        digit = _BASE58.find(char)
        if digit < 0:
            return False
        number = number * 58 + digit
    leading = len(value) - len(value.lstrip("1"))
    return leading + (number.bit_length() + 7) // 8 == size


def _request_address(value: Any, *, token: bool) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > 60:
        return None
    if _base58_bytes(raw, 32):
        return raw  # A legitimate mint may itself end in "pump".
    if token and raw.lower().endswith("pump"):
        cleaned = raw[:-4].rstrip("._- \t:/")
        if _base58_bytes(cleaned, 32):
            return cleaned
    return None


async def _throttle() -> None:
    global _last_call_ts
    async with _lock:
        wait_for = _MIN_INTERVAL - (time.monotonic() - _last_call_ts)
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        _last_call_ts = time.monotonic()


def _register_fail(key: str) -> None:
    fails = _fail_count.get(key, 0) + 1
    _fail_count[key] = fails
    ttl = _TTL_NIL_MAX if fails >= 4 else _TTL_NIL_SHORT
    cache_set(key, _SENTINEL_NIL, ttl=ttl)
    log.debug("[birdeye] %s unavailable (TTL=%ss, failures=%d)", key, ttl, fails)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value.replace(",", "") if isinstance(value, str) else value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _first_number(*values: Any) -> float | None:
    return next((n for value in values if (n := _safe_float(value)) is not None), None)


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _identity_matches(data: dict, address: str, field: str) -> bool:
    # Never repair/relabel a provider identity or silently change network.
    return (data.get(field) == address and _base58_bytes(data.get(field), 32)
            and all(data.get(key) == "solana" for key in ("chain", "chainId", "network") if key in data))


async def _fetch(endpoint: str, cache_key: str, *, address: str,
                 identity_field: str = "address", force_refresh: bool = False,
                 ttl: int = 60, validator: Callable[[dict], bool] | None = None) -> dict | None:
    if not _API_KEY:
        return None
    if force_refresh:
        cache_delete(cache_key)
    hit = None if force_refresh else cache_get(cache_key)
    if hit is not None:
        if hit is _SENTINEL_NIL:
            return None
        if (isinstance(hit, dict) and _identity_matches(hit, address, identity_field)
                and (validator is None or validator(hit))):
            return deepcopy(hit)
        cache_delete(cache_key)
    await _throttle()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(_BASE_URL + endpoint, params={"address": address},
                    headers={"X-API-KEY": _API_KEY, "x-chain": "solana", "Accept": "application/json"},
                    allow_redirects=False) as response:
                if response.status == 200:
                    payload = await response.json()
                    data = payload.get("data") if isinstance(payload, dict) and payload.get("success") is True else None
                    if (isinstance(data, dict) and _identity_matches(data, address, identity_field)
                            and (validator is None or validator(data))):
                        data = deepcopy(data)
                        # Overwrite any provider-supplied local receipt marker.
                        data["_market_received_at"] = time.time()
                        _fail_count.pop(cache_key, None)
                        cache_set(cache_key, deepcopy(data), ttl=ttl)
                        return data
                log.debug("[birdeye] %s HTTP/contract unavailable (HTTP %s)", endpoint, response.status)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Exceptions can contain headers/URLs: log only their type.
        log.debug("[birdeye] %s request error (%s)", endpoint, type(exc).__name__)
    _register_fail(cache_key)
    return None


def _add_legacy_aliases(token: dict) -> dict:
    out = deepcopy(token)
    out["liquidity"] = {"usd": out.get("liquidity_usd")}
    out["volume24h"] = out.get("volume_24h_usd")
    out["fdv"] = out.get("market_cap_usd")
    return out


def _finish_market(out: dict, raw: dict) -> dict:
    # Sanitization must not overwrite chosen nullable values with raw aliases.
    normalized = {key: deepcopy(out.get(key)) for key in MARKET_FIELDS}
    out = sanitize_token_data(out)
    out.update(normalized)
    out = _add_legacy_aliases(out)
    received = raw.get("_market_received_at")
    out.pop("_market_received_at", None)
    out.pop("market_observation", None)
    out.pop("token_birth_observation", None)
    return stamp_market_observation(out, "birdeye", received_at=received) if received is not None else out


def _normalize_token_payload(address: str, raw: dict) -> dict:
    liquidity, volume, price_info = (_mapping(raw.get(key)) for key in ("liquidity", "volume", "priceInfo"))
    fields = {
        "price_usd": _first_number(raw.get("price"), raw.get("priceUsd"), price_info.get("priceUsd")),
        "liquidity_usd": _first_number(raw.get("liquidity"), raw.get("liquidityUsd"), liquidity.get("usd"), raw.get("tvlUsd")),
        "volume_24h_usd": _first_number(raw.get("v24hUSD"), raw.get("volume24hUsd"), raw.get("v24hUsd"), volume.get("h24"), volume.get("usd")),
        "market_cap_usd": _first_number(raw.get("marketCap"), raw.get("marketCapUsd"), raw.get("fdv"), raw.get("fdvUsd")),
        "txns_last_5m": raw.get("trade5m"), "txns_last_5m_buys": raw.get("buy5m"),
        "txns_last_5m_sells": raw.get("sell5m"), "holders": raw.get("holder"),
        "price_pct_1m": raw.get("priceChange1mPercent"),
        "price_pct_5m": raw.get("priceChange5mPercent"), "volume_pct_5m": raw.get("v5mChangePercent"),
    }
    fields = {key: market_number(value, key) for key, value in fields.items()}
    for key in ("txns_last_5m", "txns_last_5m_buys", "txns_last_5m_sells", "holders"):
        if fields[key] is not None and not fields[key].is_integer():
            fields[key] = None
    out = {**deepcopy(raw), **fields, "address": address, "pair_address": None,
           "symbol": raw.get("symbol") or raw.get("baseSymbol") or raw.get("name")}
    # Overview does not document original mint birth. Preserve untyped clocks
    # for diagnostics, never promote them (or a provider's age) into predictors.
    out = venue_clock_snapshot(out, created_at=None, kind="untyped_token_metadata", source="birdeye")
    return _finish_market(out, raw)


def _normalize_pool_payload(address: str, raw: dict) -> dict:
    base = _mapping(raw.get("base"))
    liquidity, volume = _mapping(raw.get("liquidity")), _mapping(raw.get("volume"))
    fields = {
        "price_usd": _first_number(raw.get("price"), raw.get("priceUsd")),
        "liquidity_usd": _first_number(raw.get("liquidity"), raw.get("tvlUsd"), liquidity.get("usd"), raw.get("liquidityUsd")),
        "volume_24h_usd": _first_number(raw.get("volume_24h"), raw.get("volume24hUsd"), volume.get("h24"), volume.get("usd")),
        "market_cap_usd": _first_number(raw.get("marketCap"), raw.get("fdv"), raw.get("fdvUsd")),
    }
    out = {**deepcopy(raw), "address": base.get("address") or raw.get("baseMint") or raw.get("baseToken") or address,
           "pair_address": address, "symbol": base.get("symbol") or raw.get("symbol") or raw.get("name"),
           **{key: market_number(value, key) for key, value in fields.items()}}
    event = next((raw[key] for key in ("created_at", "createdAt", "createUnixTime") if raw.get(key) is not None), None)
    out = venue_clock_snapshot(out, created_at=event, kind="pool", source="birdeye")
    return _finish_market(out, raw)


def _creation_time(data: dict) -> dt.datetime | None:
    value = data.get("blockUnixTime")
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0 or value >= 1e11 or value != int(value)):
        return None
    # Documented seconds, not heuristic milliseconds or provider update time.
    created = parse_event_clock(value)
    if "blockHumanTime" in data and parse_event_clock(data["blockHumanTime"]) != created:
        return None
    return created


def _valid_creation(data: dict) -> bool:
    slot = data.get("slot")
    return (_creation_time(data) is not None and type(slot) is int and slot >= 0
            and _base58_bytes(data.get("txHash"), 64))


async def get_token_info(address: str, *, force_refresh: bool = False) -> dict | None:
    address = _request_address(address, token=True)
    if address is None:
        return None
    data = await _fetch(_TOKEN_EP, f"be:v2:token:{address}", address=address, force_refresh=force_refresh)
    return _normalize_token_payload(address, data) if data is not None else None


async def get_pool_info(address: str, *, force_refresh: bool = False) -> dict | None:
    address = _request_address(address, token=False)
    if address is None:
        return None
    def valid_base(data: dict) -> bool:
        return _base58_bytes(_mapping(data.get("base")).get("address"), 32)
    data = await _fetch(_POOL_EP, f"be:v2:pool:{address}", address=address,
                        force_refresh=force_refresh, validator=valid_base)
    return _normalize_pool_payload(address, data) if data is not None else None


async def get_token_creation_info(address: str, *, force_refresh: bool = False) -> dict | None:
    """Explicit cached creation lookup, never an implicit extra price request."""
    address = _request_address(address, token=True)
    if address is None:
        return None
    data = await _fetch(_CREATION_EP, f"be:v2:creation:{address}", address=address,
                        identity_field="tokenAddress", force_refresh=force_refresh,
                        ttl=86400, validator=_valid_creation)
    if data is None:
        return None
    created_at = _creation_time(data)
    return {"address": address, "created_at": created_at, "token_birth_observation": {
        "version": "birdeye_mint_creation_receipt_v1", "source": "birdeye", "chain": "solana",
        "address": address, "created_at": created_at.isoformat(), "slot": data["slot"],
        "tx_hash": data["txHash"], "received_at": data["_market_received_at"],
        "basis": "provider_reported_mint_creation_not_independent_chain_verification",
    }}


__all__ = ["get_token_info", "get_pool_info", "get_token_creation_info"]
