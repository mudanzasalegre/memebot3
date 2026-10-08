from __future__ import annotations

import logging
import math
import time
from typing import Dict, Optional
from urllib.parse import quote

import aiohttp

from analytics.social_signal import (
    SocialSignal,
    checked_social_receipt,
    social_cache_ttl_s,
    social_signal_from_profile,
    unknown_social_signal,
)
from config import DEX_API_BASE
from config.config import CFG
from utils.simple_cache import cache_delete, cache_get, cache_set

log = logging.getLogger("socials")

BASE = DEX_API_BASE.rstrip("/")
TTL = 600


def _ok(profile: Dict) -> bool:
    signal = social_signal_from_profile(profile)
    return bool(signal.link_count)


async def fetch_social_profile(address: str) -> SocialSignal:
    ck = f"social_profile:v2:{address}"
    ttl = social_cache_ttl_s()
    if (hit := cache_get(ck)) is not None:
        if (signal := checked_social_receipt(hit, address, max_age_s=ttl)) is not None and checked_social_receipt(
                signal, address, max_age_s=social_cache_ttl_s(signal)) is not None:
            return signal
        cache_delete(ck)

    url = f"{BASE}/token-pairs/v1/solana/{quote(address, safe='')}"
    started = time.perf_counter()
    timeout_s = float(getattr(CFG, "SOCIALS_TIMEOUT_S", 2.0) or 2.0)
    try:
        async with aiohttp.ClientSession() as sess, sess.get(url, timeout=timeout_s) as resp:
            latency_ms = int((time.perf_counter() - started) * 1000)
            if resp.status != 200:
                signal = unknown_social_signal(source="dexscreener", latency_ms=latency_ms,
                    address=address, received_at=time.time())
                cache_set(ck, signal.to_dict(), ttl=min(60., ttl))
                return signal
            pairs = await resp.json()
            received_at = time.time()
            latency_ms = int((time.perf_counter() - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.perf_counter() - started) * 1000)
        log.debug("[socials] %s -> %s", address[:4], exc)
        signal = unknown_social_signal(source="dexscreener", latency_ms=latency_ms,
            address=address, received_at=time.time())
        cache_set(ck, signal.to_dict(), ttl=min(60., ttl))
        return signal

    matches = [pair for pair in pairs if isinstance(pair, dict) and pair.get("chainId") == "solana"
               and isinstance(pair.get("baseToken"), dict) and pair["baseToken"].get("address") == address] if isinstance(pairs, list) else []
    def liquidity(pair):
        value = pair.get("liquidity")
        try:
            value = value.get("usd") if isinstance(value, dict) else None
            number = float(value) if not isinstance(value, bool) else 0.
            return number if math.isfinite(number) and number >= 0 else 0.
        except (TypeError, ValueError, OverflowError):
            return 0.
    profile = max(matches, key=liquidity) if matches else None
    signal = social_signal_from_profile(profile, source="dexscreener", latency_ms=latency_ms,
        address=address, received_at=received_at)
    cache_set(ck, signal.to_dict(), ttl=social_cache_ttl_s(signal))
    return signal


async def has_socials(address: str) -> Optional[bool]:
    """
    True  -> hay socials
    False -> se consulto bien y no hay socials
    None  -> no pudimos determinarlo
    """
    signal = await fetch_social_profile(address)
    return signal.social_ok  # No second scalar cache that extends the original age.


__all__ = ["fetch_social_profile", "has_socials"]
