"""GeckoTerminal public-v2 token/pool receipts, not market-as-of or fill proof."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
import math
import os
import re
import threading
import time
from urllib.parse import urlsplit

import requests
try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

from analytics.token_birth import base58_size
from analytics.token_time import venue_clock_snapshot
from config.config import GECKO_API_URL
from utils.bounded_state import BoundedFailureCounter, bounded_int, increment_failure
from utils.data_utils import sanitize_token_data
from utils.market_observation import MARKET_FIELDS, market_number, stamp_market_observation
from utils.rate_limiter import GECKO_LIMITER
from utils.request_ownership import OwnedRequests, SyncOwnedRequests
from utils.simple_cache import cache_get, cache_set, cache_delete

logger = logging.getLogger("fetcher.geckoterminal")
USE_GECKO_TERMINAL = os.getenv("USE_GECKO_TERMININAL", os.getenv("USE_GECKO_TERMINAL", "true")).lower() == "true"
_BASE_URL = GECKO_API_URL.rstrip("/")
_CONTRACT = "gecko_public_token_pool_v1"
_HEADERS = {"Accept": "application/json;version=20230203", "User-Agent": "memebot3/1.0"}
_TTL_NIL_SHORT = bounded_int(os.getenv("GECKO_TTL_NIL_SHORT", "120"), 120, 1, 3600)
_TTL_NIL_MAX = max(_TTL_NIL_SHORT, bounded_int(os.getenv("GECKO_TTL_NIL_MAX", "600"), 600, 1, 86400))
_SENTINEL_NIL = object()
_fail_count = BoundedFailureCounter()
_REQUESTS = OwnedRequests(name="gecko-http-request")
_SYNC_REQUESTS = SyncOwnedRequests(name="gecko-sync-http-request")
_state_lock = threading.RLock()
_latest_request = {}
_active_requests = 0
_MAX_ACTIVE = 16
_diagnostics = {name:0 for name in ("success", "invalid_resource", "http_unavailable",
    "request_error", "rate_limited", "cancelled", "capacity_rejected", "cleanup_failed")}
_last_call_ts = 0.0
_last_observed_ts = None
_min_interval_s = 60.0 / max(1, min(10, GECKO_LIMITER.max_calls))
_cooldown_on_429_s = bounded_int(os.getenv("GECKO_COOLDOWN_ON_429_S", "60"), 60, 5, 300)
_cooldown_until = 0.0
_cooldown_lock = threading.Lock()


def _context(network, address):
    if not isinstance(network, str) or not isinstance(address, str):
        return None
    network, raw = network.strip().lower(), address.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", network) or len(raw) > 64:
        return None
    if network == "solana":
        if not base58_size(raw, 32):
            raw = re.sub(r"[._\-\s:/]*pump$", "", raw, flags=re.IGNORECASE)
        if not base58_size(raw, 32):
            return None
    elif re.fullmatch(r"0x[0-9a-fA-F]{40}", raw):
        raw = raw.lower()
    else:
        return None
    try:
        base = urlsplit(_BASE_URL)
    except (TypeError, ValueError):
        return None
    if (base.scheme != "https" or base.netloc != "api.geckoterminal.com"
            or base.path.rstrip("/") != "/api/v2" or base.query or base.fragment):
        return None  # Public protocol is not a paid/credentialed gateway.
    return network, raw


def _build_endpoint(network, address):
    return f"{_BASE_URL}/networks/{network}/tokens/{address}"


def _number(value, field):
    if isinstance(value, str):
        value = value.replace(",", "")
    return market_number(value, field)


def _to_float(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value.replace(",", "") if isinstance(value, str) else value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _count(value):
    number = _number(value, "count")
    return int(number) if number is not None and number.is_integer() else None


def _pick_created_at(attrs):
    from analytics.token_time import parse_event_clock
    for key in ("pool_created_at", "pair_created_at", "pool_created_at_timestamp", "pair_created_at_timestamp"):
        if attrs.get(key) is not None:
            return parse_event_clock(attrs[key])
    return None


def _epoch_to_dt(value):
    from analytics.token_time import parse_event_clock
    return parse_event_clock(value)


def _relation(node, name):
    relationships = node.get("relationships")
    value = relationships.get(name) if isinstance(relationships, dict) else None
    return value.get("data") if isinstance(value, dict) else None


def _selected_pool(data, included, network, address):
    top = _relation(data, "top_pools")
    if not isinstance(top, list) or not top or not isinstance(top[0], dict):
        return None
    reference = top[0]
    ident = reference.get("id")
    prefix = network + "_"
    if (reference.get("type") != "pool" or not isinstance(ident, str)
            or not ident.startswith(prefix) or not isinstance(included, list)):
        return None
    matches = [node for node in included if isinstance(node, dict) and node.get("id") == ident]
    if len(matches) != 1:
        return None
    pool = matches[0]
    attrs = pool.get("attributes")
    pair = ident[len(prefix):]
    context = _context(network, pair)
    if (pool.get("type") != "pool" or not isinstance(attrs, dict) or context is None
            or context[1] != pair or attrs.get("address") != pair):
        return None
    base, quote = _relation(pool, "base_token"), _relation(pool, "quote_token")
    token_id = prefix + address
    for role, link in (("base", base), ("quote", quote)):
        if isinstance(link, dict) and link.get("type") == "token" and link.get("id") == token_id:
            other = quote if role == "base" else base
            if (not isinstance(other, dict) or other.get("type") != "token"
                    or not isinstance(other.get("id"), str) or not other["id"].startswith(prefix)
                    or other["id"] == token_id
                    or _context(network, other["id"][len(prefix):]) is None):
                return None
            return pair, attrs, role
    return None


def _normalize_attributes(addr, attrs, *, pool=None, token_context=False):
    """Private projection; public callers first require exact JSON resource identity."""
    if not isinstance(attrs, dict):
        return None
    pool_attrs = pool[1] if pool is not None else {}
    volume = attrs.get("volume_usd")
    volume = volume if isinstance(volume, dict) else {}
    price = _number(attrs.get("price_usd"), "price_usd")
    cap = _number(attrs.get("market_cap_usd"), "market_cap_usd")
    fdv = _number(attrs.get("fdv_usd"), "market_cap_usd")
    if pool is not None:
        liquidity = _number(pool_attrs.get("reserve_in_usd"), "liquidity_usd")
        proxy, basis = False, "selected_pool_reserve"
    else:
        liquidity = _number(attrs.get("total_reserve_in_usd"), "liquidity_usd")
        proxy, basis = True, "token_total_reserve_not_route_liquidity"
        if not token_context and "reserve_in_usd" in attrs:
            liquidity = _number(attrs["reserve_in_usd"], "liquidity_usd")
            proxy, basis = False, "private_pool_attribute_projection"
    changes = pool_attrs.get("price_change_percentage")
    changes = changes if isinstance(changes, dict) else {}
    activity = pool_attrs.get("transactions")
    activity = activity.get("m5") if isinstance(activity, dict) else None
    activity = activity if isinstance(activity, dict) else {}
    # Pool buy/sell and change fields are not assumed to describe its quote token.
    base_role = pool is not None and pool[2] == "base"
    buys = _count(activity.get("buys")) if base_role else None
    sells = _count(activity.get("sells")) if base_role else None
    total = buys + sells if buys is not None and sells is not None else None
    normalized = {key: None for key in MARKET_FIELDS}
    normalized.update(price_usd=price, liquidity_usd=liquidity, market_cap_usd=cap,
        volume_24h_usd=_number(volume.get("h24"), "volume_24h_usd"),
        txns_last_5m=total, txns_last_5m_buys=buys, txns_last_5m_sells=sells,
        price_pct_5m=_number(changes.get("m5"), "price_pct_5m") if base_role else None)
    out = {**deepcopy(attrs), **normalized, "address":addr,
        "symbol":attrs.get("symbol") if isinstance(attrs.get("symbol"), str) else None,
        "pair_address":pool[0] if pool is not None else None,
        "fdv_usd":fdv, "fdv":fdv, "liquidity_usd_is_proxy":proxy,
        "liquidity_is_proxy":proxy, "liquidity_basis":basis, "gecko_contract_version":_CONTRACT}
    event_attrs = pool_attrs if token_context else attrs
    out = venue_clock_snapshot(out, created_at=_pick_created_at(event_attrs),
        kind="pool_metadata", source="geckoterminal")
    out = sanitize_token_data(out)
    out.update(normalized)  # Alias coercion cannot resurrect missing canonical values.
    out["liquidity"] = {"usd":liquidity}
    out["volume24h"] = normalized["volume_24h_usd"]
    out["volume_usd_24h"] = normalized["volume_24h_usd"]
    out["total_reserve_in_usd"] = _number(attrs.get("total_reserve_in_usd"), "liquidity_usd")
    out["fdv_usd"] = out["fdv"] = fdv
    return out


def _decode_response(payload, network, address, received_at):
    if not isinstance(payload, dict) or "errors" in payload or "error" in payload:
        return None
    data = payload.get("data")
    attrs = data.get("attributes") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get("type") != "token"
            or data.get("id") != f"{network}_{address}" or not isinstance(attrs, dict)
            or attrs.get("address") != address):
        return None
    pool = _selected_pool(data, payload.get("included"), network, address)
    return stamp_market_observation(_normalize_attributes(address, attrs, pool=pool, token_context=True),
        "geckoterminal", received_at=received_at)


def _throttle_wait():
    global _last_call_ts, _last_observed_ts
    with _state_lock:
        # Sampling before this lock can manufacture a backwards clock when
        # an older thread resumes after a newer thread has already admitted.
        now = time.monotonic()
        try:
            valid = type(now) in (int, float) and math.isfinite(now)
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            raise ValueError("Invalid local monotonic clock")
        if _last_observed_ts is not None and now < _last_observed_ts:
            _last_call_ts = now
        _last_observed_ts = now
        wait = max(0., _min_interval_s - (now - _last_call_ts))
        if not wait:
            _last_call_ts = now
        return wait


def _throttle_internal():
    while (wait := _throttle_wait()) > 0:
        time.sleep(wait)


async def _throttle_async():
    while (wait := _throttle_wait()) > 0:
        await asyncio.sleep(wait)


def _cooldown_remaining():
    with _cooldown_lock:
        return max(0., _cooldown_until - time.monotonic())


def _register_global_cooldown(ttl_s=None):
    global _cooldown_until
    value = _to_float(ttl_s)
    ttl = max(_cooldown_on_429_s, min(300., value)) if value is not None and value >= 0 else _cooldown_on_429_s
    with _cooldown_lock:
        _cooldown_until = max(_cooldown_until, time.monotonic() + ttl)


def _wait_for_global_cooldown_sync():
    while (wait := _cooldown_remaining()) > 0:
        time.sleep(wait)


async def _wait_for_global_cooldown_async():
    while (wait := _cooldown_remaining()) > 0:
        await asyncio.sleep(wait)


def _acquire_sync():
    GECKO_LIMITER.acquire_sync()


def _register_fail(key):
    fails = increment_failure(_fail_count, key)
    cache_set(key, _SENTINEL_NIL, ttl=_TTL_NIL_MAX if fails >= 4 else _TTL_NIL_SHORT)


def _reset_fail(key):
    _fail_count.pop(key, None)


class _RequestCapacityUnavailable(Exception):
    """Shared adapter capacity rejected before any child or cache mutation."""


def _begin_request(key, *, force_refresh=False):
    global _active_requests
    with _state_lock:
        if _active_requests >= _MAX_ACTIVE:
            _diagnostics["capacity_rejected"] += 1
            return None
        if force_refresh:
            cache_delete(key)
        generation = object()
        _active_requests += 1
        _latest_request[key] = generation
        return generation


def _record_outcome(name):
    with _state_lock:
        _diagnostics[name] += 1


def _end_request(key, generation):
    global _active_requests
    with _state_lock:
        _active_requests -= 1
        if _latest_request.get(key) is generation:
            _latest_request.pop(key, None)


def _publish(key, generation, result):
    with _state_lock:
        if _latest_request.get(key) is not generation:
            return
        if result is None:
            _register_fail(key)
        else:
            _reset_fail(key)
            cache_set(key, deepcopy(result), ttl=60)


def _cached(key, address):
    hit = cache_get(key)
    if hit is _SENTINEL_NIL:
        return True, None
    if isinstance(hit, dict) and hit.get("address") == address and hit.get("gecko_contract_version") == _CONTRACT:
        return True, deepcopy(hit)
    return False, None


def _timeout(value):
    result = _to_float(value)
    return result if result is not None and result > 0 else None


def get_token_data(network, address, session=None, timeout=5, *, force_refresh=False):
    context = _context(network, address) if USE_GECKO_TERMINAL else None
    deadline = _timeout(timeout)
    if context is None or deadline is None:
        return None
    network, address = context
    key = f"gt:{_CONTRACT}:{network}:{address}"
    if not force_refresh:
        found, hit = _cached(key, address)
        if found:
            return hit
    generation = None

    def admitted():
        nonlocal generation
        generation = _begin_request(key, force_refresh=force_refresh)
        if generation is None:
            raise _RequestCapacityUnavailable

    try:
        result = _SYNC_REQUESTS.run((key, id(session), deadline),
            lambda:_fetch_sync(network, address, key, session, deadline, generation),
            force_refresh=force_refresh, on_admit=admitted, join_timeout=deadline)
        return deepcopy(result)
    except _RequestCapacityUnavailable:
        return None
    finally:
        if generation is not None:
            _end_request(key, generation)


def _fetch_sync(network, address, key, session, deadline, generation):
    client, response = None, None
    result = None
    try:
        _wait_for_global_cooldown_sync()
        _throttle_internal()
        _acquire_sync()
        client = session if session is not None else requests.Session()
        response = client.get(_build_endpoint(network, address), headers=dict(_HEADERS),
            params={"include":"top_pools"}, timeout=deadline, allow_redirects=False)
        if response.status_code == 429:
            _record_outcome("rate_limited")
            _register_global_cooldown(getattr(response, "headers", {}).get("Retry-After"))
        if response.status_code == 200:
            body = response.json()
            received = time.time()
            result = _decode_response(body, network, address, received)
            _record_outcome("success" if result is not None else "invalid_resource")
        else:
            _record_outcome("http_unavailable")
    except Exception:
        _record_outcome("request_error")
        result = None
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            _record_outcome("cleanup_failed")
            result = None
        finally:
            try:
                if session is None and client is not None:
                    client.close()
            except Exception:
                _record_outcome("cleanup_failed")
                result = None
            finally:
                _publish(key, generation, result)
    return result


async def _fetch_async(network, address, key, session, timeout, generation):
    client = None
    result = None
    try:
        await _wait_for_global_cooldown_async()
        await _throttle_async()
        async with GECKO_LIMITER:
            client = session if session is not None else aiohttp.ClientSession()
            try:
                async with client.get(_build_endpoint(network, address), headers=dict(_HEADERS),
                        params={"include":"top_pools"}, timeout=timeout, allow_redirects=False) as response:
                    if response.status == 429:
                        _record_outcome("rate_limited")
                        _register_global_cooldown(getattr(response, "headers", {}).get("Retry-After"))
                    if response.status == 200:
                        body = await response.json()
                        received = time.time()
                        result = _decode_response(body, network, address, received)
                        _record_outcome("success" if result is not None else "invalid_resource")
                    else:
                        _record_outcome("http_unavailable")
            finally:
                if session is None and client is not None:
                    try:
                        await client.close()
                    except Exception:
                        _record_outcome("cleanup_failed")
                        raise
        _publish(key, generation, result)
        return result
    except asyncio.CancelledError:
        _record_outcome("cancelled")
        raise
    except Exception:
        _record_outcome("request_error")
        _publish(key, generation, None)
        return None


async def get_token_data_async(network, address, session=None, timeout=5, *, force_refresh=False):
    context = _context(network, address) if USE_GECKO_TERMINAL else None
    deadline = _timeout(timeout)
    if context is None or deadline is None or (session is None and aiohttp is None):
        return None
    network, address = context
    key = f"gt:{_CONTRACT}:{network}:{address}"
    if not force_refresh:
        found, hit = _cached(key, address)
        if found:
            return hit
    # Injected callers share work only when they own the same session object.
    ownership_key = (key, id(session), deadline)
    generation = None

    def admitted():
        nonlocal generation
        # OwnedRequests calls this synchronously only for accepted owners,
        # before scheduling. Shared sync/async capacity and force invalidation
        # belong to the same original admission, not child execution order.
        generation = _begin_request(key, force_refresh=force_refresh)
        if generation is None:
            raise _RequestCapacityUnavailable

    try:
        result = await _REQUESTS.run(ownership_key,
            lambda:_fetch_async(network, address, key, session, deadline, generation),
            force_refresh=force_refresh, on_admit=admitted)
        return deepcopy(result)
    except _RequestCapacityUnavailable:
        return None
    finally:
        # A canceled child can fail to start, so only its admitted caller can
        # reliably release the lease. OwnedRequests drains child cleanup first.
        if generation is not None:
            _end_request(key, generation)


def request_runtime_snapshot():
    with _state_lock:
        state = {"active_requests":_active_requests, "max_active":_MAX_ACTIVE,
            "latest_generations":len(_latest_request), "diagnostic_outcomes":dict(_diagnostics)}
    return {**state, "requests":_REQUESTS.snapshot(), "sync_requests":_SYNC_REQUESTS.snapshot(),
        "failure_keys":_fail_count.snapshot(),
        "public_max_calls_per_minute":min(10, GECKO_LIMITER.max_calls),
        "basis":"local_admission_not_actual_provider_quota_or_wire_billing"}


__all__ = ["get_token_data", "get_token_data_async", "USE_GECKO_TERMINAL"]
