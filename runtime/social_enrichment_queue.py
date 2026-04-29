from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Any

from analytics.social_signal import (
    SocialSignal,
    flag_suspicious_links,
    record_social_links,
    record_social_signal,
    unknown_social_signal,
)
from config.config import CFG
from fetcher.socials import fetch_social_profile
from utils.runtime_telemetry import record_runtime_event


@dataclass(frozen=True)
class SocialEnrichmentRequest:
    address: str
    symbol: str | None = None
    lane: str | None = None
    requested_at_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SocialEnrichmentQueue:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, SocialSignal]] = {}
        self._inflight: set[str] = set()
        self._semaphore: asyncio.Semaphore | None = None

    def _cache_ttl_s(self) -> float:
        return float(getattr(CFG, "SOCIALS_CACHE_TTL_S", 600) or 600)

    def _max_concurrent(self) -> int:
        return max(1, int(getattr(CFG, "SOCIALS_MAX_CONCURRENT", 4) or 4))

    def get_cached(self, address: str) -> SocialSignal | None:
        cached = self._cache.get(str(address))
        if not cached:
            return None
        ts, signal = cached
        if (time.time() - ts) > self._cache_ttl_s():
            self._cache.pop(str(address), None)
            return None
        return signal

    def snapshot(self) -> dict[str, Any]:
        return {
            "cached": len(self._cache),
            "inflight": len(self._inflight),
            "max_concurrent": self._max_concurrent(),
            "enabled": bool(getattr(CFG, "SOCIALS_ENABLED", True)),
            "async_only": bool(getattr(CFG, "SOCIALS_ASYNC_ONLY", True)),
            "hot_path_blocking": bool(getattr(CFG, "SOCIALS_HOT_PATH_BLOCKING", False)),
        }

    def schedule(self, token: dict[str, Any], *, lane: str | None = None) -> bool:
        if not bool(getattr(CFG, "SOCIALS_ENABLED", True)):
            return False
        address = str(token.get("address") or token.get("mint") or "").strip()
        if not address:
            return False
        if self.get_cached(address) is not None or address in self._inflight:
            return False
        request = SocialEnrichmentRequest(
            address=address,
            symbol=str(token.get("symbol") or "") or None,
            lane=lane or str(token.get("entry_lane") or token.get("gate_profile") or "") or None,
            requested_at_s=time.time(),
        )
        record_runtime_event(
            "social_enrichment_scheduled",
            address,
            lane=request.lane,
            symbol=request.symbol,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._inflight.add(address)
        loop.create_task(self._run(request))
        return True

    async def _run(self, request: SocialEnrichmentRequest) -> SocialSignal:
        address = request.address
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrent())
        try:
            async with self._semaphore:
                signal = await fetch_social_profile(address)
                signal = flag_suspicious_links(signal, address=address, symbol=request.symbol)
                self._cache[address] = (time.time(), signal)
                record_social_links(signal, address=address, symbol=request.symbol)
                record_social_signal(signal, address=address, symbol=request.symbol, lane=request.lane)
                record_runtime_event(
                    "social_enrichment_completed",
                    address,
                    lane=request.lane,
                    status=signal.status,
                    social_ok=signal.social_ok,
                    twitter_present=signal.twitter_present,
                    telegram_present=signal.telegram_present,
                    discord_present=signal.discord_present,
                    website_present=signal.website_present,
                    link_count=signal.link_count,
                    risk_flags=list(signal.risk_flags),
                    latency_ms=signal.latency_ms,
                )
                return signal
        except Exception as exc:  # noqa: BLE001
            signal = unknown_social_signal(source="social_enrichment_queue")
            self._cache[address] = (time.time(), signal)
            record_runtime_event(
                "social_enrichment_failed",
                address,
                lane=request.lane,
                error=str(exc),
            )
            return signal
        finally:
            self._inflight.discard(address)


GLOBAL_SOCIAL_ENRICHMENT_QUEUE = SocialEnrichmentQueue()


def schedule_social_enrichment(token: dict[str, Any], *, lane: str | None = None) -> bool:
    return GLOBAL_SOCIAL_ENRICHMENT_QUEUE.schedule(token, lane=lane)


__all__ = [
    "GLOBAL_SOCIAL_ENRICHMENT_QUEUE",
    "SocialEnrichmentQueue",
    "SocialEnrichmentRequest",
    "schedule_social_enrichment",
]
