"""Public documented summary API; higher normalised score is higher risk.

Receipt age is local HTTP receipt age, not a claim about report generation time.
No paid forced refresh and no hot-path retry/backoff chain.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from urllib.parse import quote

import aiohttp

from config import RUGCHECK_API_BASE, RUGCHECK_API_KEY
from utils.simple_cache import cache_get, cache_set
from utils.auxiliary_observation import observation, checked_auxiliary_observation, whole_number, observation_clock

log = logging.getLogger("rugcheck")
TIMEOUT = 4.
HEADERS = {"Authorization": f"Bearer {RUGCHECK_API_KEY}"} if RUGCHECK_API_KEY else {}


async def fetch_observation(address: str) -> dict:
    key = f"rug:receipt:v1:{address}"
    if (hit := checked_auxiliary_observation(cache_get(key), address, "rug")) is not None:
        return hit
    result = observation("rug", address, reason="request_failed_or_unavailable")
    if not RUGCHECK_API_BASE:
        return result
    base = RUGCHECK_API_BASE.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    url = f"{base}/tokens/{quote(address, safe='')}/report/summary"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
            async with session.get(url, headers=HEADERS) as response:
                if response.status != 200:
                    cache_set(key, deepcopy(result), ttl=15)
                    return result
                data = await response.json()
                received = observation_clock()
        # Single-token responses need not include mint; conflicting identity is
        # rejected. The client's requested route binds the proof to this mint.
        if (isinstance(data, dict) and data.get("mint") in (None, "", address)
                and type(data.get("score_normalised")) is int
                and whole_number(data["score_normalised"], maximum=100) is not None
                and type(data.get("score")) is int
                and whole_number(data["score"], maximum=2**63 - 1) is not None):
            candidate = observation("rug", address, data["score_normalised"],
                source="rugcheck_report_summary", observed_at=received,
                inputs={"score": data["score"], "score_normalised": data["score_normalised"]})
            result = checked_auxiliary_observation(candidate, address, "rug") or result
    except Exception as exc:
        log.debug("RugCheck unavailable (%s)", type(exc).__name__)
    cache_set(key, deepcopy(result), ttl=120 if result["value"] is not None else 15)
    return result


async def check_token(address: str) -> int | None:
    return (await fetch_observation(address))["value"]
