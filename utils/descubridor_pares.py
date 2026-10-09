"""Source-scoped Dex profile discovery, not a token-birth or buying decision.

Latest and recently updated profiles provide candidate mint identities. Their
listing/profile/pair clocks do not establish mint birth. Common market
preparation and entry/risk/age guards remain the owners of buy eligibility.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from itertools import zip_longest
import logging
import math
import time
from typing import List, Optional

import aiohttp

from utils.time import utc_now, parse_iso_utc
from utils.solana_addr import normalize_mint
from config import DEX_API_BASE
from config.config import CFG

log = logging.getLogger("descubridor")
log.setLevel(logging.DEBUG)
_SPARSE_LOG_UNTIL: dict[str, float] = {}
_SPARSE_LOG_TTL_S = 300.0

DEX = DEX_API_BASE.rstrip("/")
# These are profile feeds, not an undocumented chain-wide pair listing.
# No invented chain/limit query params: filter each returned identity locally.
URLS = [
    f"{DEX}/token-profiles/latest/v1",
    f"{DEX}/token-profiles/recent-updates/v1",
]


async def _json(s: aiohttp.ClientSession, url: str):
    try:
        value = getattr(CFG, "DEX_HTTP_TIMEOUT", 20)
        try:
            timeout_s = float(value) if not isinstance(value, bool) else 20.
        except (TypeError, ValueError, OverflowError):
            timeout_s = 20.
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            timeout_s = 20.
        async with s.get(
            url, timeout=timeout_s,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        ) as response:
            if response.status == 404:
                return None
            response.raise_for_status()
            return await response.json()
    except Exception:
        # Cancellation is not converted to an empty provider response.
        return None


def _items(raw) -> list | None:
    """Distinguish a valid empty collection from an unknown envelope."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if "chainId" in raw and "tokenAddress" in raw:
            return [raw]
        for key in ("tokens", "dexTokens", "data"):
            if isinstance(raw.get(key), list):
                return raw[key]
    return None


def _calc_age_days(tok: dict) -> float | None:
    """Legacy clock diagnostic only; never a profile candidate birth filter.

    A declared listing/pair timestamp measures that event's elapsed time, not
    mint age. Retained for private compatibility/diagnostics. Unknown, invalid
    or future first declared clocks remain unknown, not zero.
    """
    if not isinstance(tok, dict):
        return None
    for key in ("listedAt", "createdAt", "pairCreatedAt"):
        value = tok.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            return None
        created = None
        if isinstance(value, dt.datetime):
            created = value
        elif isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                created = parse_iso_utc(value)
                if created is None:
                    return None
        if created is None:
            try:
                seconds = float(value)
                if not math.isfinite(seconds) or seconds <= 0:
                    return None
                if seconds >= 1e11:
                    seconds /= 1000.
                created = dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
            except (TypeError, ValueError, OverflowError, OSError):
                return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        elapsed = (utc_now() - created.astimezone(dt.timezone.utc)).total_seconds() / 86400.
        return elapsed if math.isfinite(elapsed) and elapsed >= 0 else None
    for key in ("ageDays", "age"):
        value = tok.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            return None
        try:
            days = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return days if math.isfinite(days) and days >= 0 else None
    return None


def _extract_addr(tok: dict) -> Optional[str]:
    """Profile tokenAddress is a mint; pair/base/address aliases are not."""
    value = tok.get("tokenAddress")
    return value.strip() if isinstance(value, str) else None


def _is_solana_address(addr: str) -> bool:
    return bool(addr and not addr.startswith("0x") and 30 <= len(addr) <= 50)


def _is_chain_solana(tok: dict) -> bool:
    """A cross-chain profile feed must explicitly identify Solana."""
    value = tok.get("chainId")
    return isinstance(value, str) and value.strip().lower() == "solana"


def _debug_sparse(key: str, msg: str, *args: object) -> None:
    now = time.monotonic()
    if now < _SPARSE_LOG_UNTIL.get(key, 0.):
        return
    _SPARSE_LOG_UNTIL[key] = now + _SPARSE_LOG_TTL_S
    log.debug(msg, *args)


def _candidate_limit() -> int:
    value = getattr(CFG, "MAX_CANDIDATES", 0)
    if value is None or isinstance(value, bool):
        return 0
    try:
        limit = int(value)
        if isinstance(value, float) and value != limit:
            return 0
        return max(0, limit)
    except (TypeError, ValueError, OverflowError):
        return 0


async def fetch_candidate_pairs() -> List[str]:
    """Discover distinct Solana profile mints, not birth-certified entries.

    Combine both documented feeds even if the first is nonempty, non-Solana
    or malformed. Round-robin preserves each feed's original row order without
    starving updates under the candidate cap. Count only unique valid mints.
    No profile metadata or synthetic age reaches the existing entry guards.
    """
    collections = []
    async with aiohttp.ClientSession() as session:
        for url in URLS:
            items = _items(await _json(session, url))
            if items is not None:
                collections.append(items)
                log.debug("Dex profile feed OK -> %s", url.split(DEX, 1)[-1])
            else:
                _debug_sparse("feed_unknown:" + url, "Dex profile feed unavailable/unknown -> %s",
                              url.split(DEX, 1)[-1])
    if not collections:
        # Let the supervised source owner retain its previous success clock,
        # rather than certify an outage as a healthy empty opportunity feed.
        raise RuntimeError("Dex profile discovery: no usable feed response")

    out: list[str] = []
    seen: set[str] = set()
    limit = _candidate_limit()
    for group in zip_longest(*collections):
        for token in group:
            if not isinstance(token, dict) or not _is_chain_solana(token):
                continue
            raw_addr = _extract_addr(token)
            if not raw_addr:
                continue
            address = normalize_mint(raw_addr)
            if not address or not _is_solana_address(address):
                _debug_sparse("invalid_profile_mint", "Invalid Dex profile mint skipped")
                continue
            if address in seen:
                continue
            seen.add(address)
            out.append(address)
            if limit and len(out) >= limit:
                log.info("Descubridor: %s profile candidates (cap=%s)", len(out), limit)
                return out
    log.info("Descubridor: %s profile candidates (cap=%s)", len(out), limit or "unlimited")
    return out


if __name__ == "__main__":  # pragma: no cover
    async def _demo():
        candidates = await fetch_candidate_pairs()
        print(len(candidates), candidates[:10])

    asyncio.run(_demo())
