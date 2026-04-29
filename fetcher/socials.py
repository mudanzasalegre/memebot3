from __future__ import annotations

import logging
import time
from typing import Dict, Optional

import aiohttp

from analytics.social_signal import (
    SocialSignal,
    social_signal_from_profile,
    unknown_social_signal,
)
from config import DEX_API_BASE
from config.config import CFG
from utils.simple_cache import cache_get, cache_set

log = logging.getLogger("socials")

BASE = DEX_API_BASE.rstrip("/")
TTL = 600


def _ok(profile: Dict) -> bool:
    signal = social_signal_from_profile(profile)
    return bool(signal.link_count)


async def fetch_social_profile(address: str) -> SocialSignal:
    ck = f"social_profile:{address}"
    if (hit := cache_get(ck)) is not None:
        if isinstance(hit, SocialSignal):
            return hit
        if isinstance(hit, dict):
            return SocialSignal(**hit)  # type: ignore[arg-type]

    url = f"{BASE}/token/solana/{address}"
    started = time.perf_counter()
    timeout_s = float(getattr(CFG, "SOCIALS_TIMEOUT_S", 2.0) or 2.0)
    try:
        async with aiohttp.ClientSession() as sess, sess.get(url, timeout=timeout_s) as resp:
            latency_ms = int((time.perf_counter() - started) * 1000)
            if resp.status != 200:
                signal = unknown_social_signal(source="dexscreener", latency_ms=latency_ms)
                cache_set(ck, signal.to_dict(), ttl=max(1, int(TTL // 2)))
                return signal
            profile = await resp.json()
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.perf_counter() - started) * 1000)
        log.debug("[socials] %s -> %s", address[:4], exc)
        signal = unknown_social_signal(source="dexscreener", latency_ms=latency_ms)
        cache_set(ck, signal.to_dict(), ttl=max(1, int(TTL // 2)))
        return signal

    signal = social_signal_from_profile(profile, source="dexscreener", latency_ms=latency_ms)
    cache_set(ck, signal.to_dict(), ttl=int(getattr(CFG, "SOCIALS_CACHE_TTL_S", TTL) or TTL))
    return signal


async def has_socials(address: str) -> Optional[bool]:
    """
    True  -> hay socials
    False -> se consulto bien y no hay socials
    None  -> no pudimos determinarlo
    """
    ck = f"social:{address}"
    if (hit := cache_get(ck)) is not None:
        return hit  # type: ignore[return-value]

    signal = await fetch_social_profile(address)
    ok = signal.social_ok
    cache_set(ck, ok, ttl=TTL)
    return ok


__all__ = ["fetch_social_profile", "has_socials"]
