from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from analytics.social_signal import (
    SocialSignal,
    apply_social_signal_to_token,
    checked_social_receipt,
    flag_suspicious_links,
    record_social_links,
    record_social_signal,
    social_cache_ttl_s,
    social_profile_identity,
    unknown_social_signal,
)
from config.config import CFG
from fetcher.socials import fetch_social_profile
from utils.runtime_telemetry import record_runtime_event

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SocialEnrichmentRequest:
    address: str
    symbol: str | None = None
    lane: str | None = None
    requested_at_s: float = 0.0
    observation: SocialSignal | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SocialEnrichmentQueue:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, SocialSignal]] = {}
        self._inflight: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._semaphore: asyncio.Semaphore | None = None

    def _cache_ttl_s(self) -> float:
        return social_cache_ttl_s()

    def _max_concurrent(self) -> int:
        return max(1, int(getattr(CFG, "SOCIALS_MAX_CONCURRENT", 4) or 4))

    def get_cached(self, address: str) -> SocialSignal | None:
        cached = self._cache.get(str(address))
        if not cached:
            return None
        ts, signal = cached
        checked = checked_social_receipt(signal, str(address), max_age_s=min(self._cache_ttl_s(), social_cache_ttl_s(signal)))
        if checked is None or ts != checked.received_at:
            self._cache.pop(str(address), None)
            return None
        return checked

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
        if address in self._inflight:
            return False
        cached = self.get_cached(address)
        observed = checked_social_receipt(token.get("social_signal"), address, max_age_s=self._cache_ttl_s())
        observed = observed if observed is not None and observed.social_ok is not None else None
        if cached is not None and (observed is None or social_profile_identity(observed) == social_profile_identity(cached)):
            return False
        request = SocialEnrichmentRequest(
            address=address,
            symbol=str(token.get("symbol") or "") or None,
            lane=lane or str(token.get("entry_lane") or token.get("gate_profile") or "") or None,
            requested_at_s=time.time(),
            observation=observed,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._inflight.add(address)
        task = loop.create_task(self._run(request), name=f"social-enrichment:{address[:8]}")
        self._tasks.add(task)
        task.add_done_callback(lambda done: self._finish(done, address))
        record_runtime_event(
            "social_enrichment_scheduled", address, lane=request.lane, symbol=request.symbol,
        )
        return True

    def _finish(self, task: asyncio.Task, address: str) -> None:
        self._tasks.discard(task)
        self._inflight.discard(address)
        if not task.cancelled():
            try:
                task.result()
            except Exception as exc:
                log.warning("Social enrichment task failed: %s", type(exc).__name__)

    async def stop(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._inflight.clear()
        self._semaphore = None

    async def _run(self, request: SocialEnrichmentRequest) -> SocialSignal:
        address = request.address
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrent())
        try:
            async with self._semaphore:
                signal = request.observation if request.observation is not None else await fetch_social_profile(address)
                signal = checked_social_receipt(signal, address, max_age_s=self._cache_ttl_s())
                if signal is None:
                    raise ValueError("Missing, expired or conflicting social HTTP receipt")
                # Disk-history scanning does not run on the entry/event-loop
                # thread. Cancellation cannot publish the abandoned result.
                signal = await asyncio.to_thread(flag_suspicious_links, signal, address=address, symbol=request.symbol)
                signal = checked_social_receipt(signal, address, max_age_s=self._cache_ttl_s())
                if signal is None:
                    raise ValueError("Social receipt expired or changed during risk enrichment")
                self._cache[address] = (signal.received_at, signal)
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
                    received_at=signal.received_at,
                    source=signal.source,
                    basis=signal.basis,
                )
                return signal
        except Exception as exc:  # noqa: BLE001
            signal = unknown_social_signal(source="social_enrichment_queue", address=address, received_at=time.time())
            self._cache[address] = (signal.received_at, signal)
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


def consume_social_enrichment(token: dict[str, Any]) -> SocialSignal:
    """Nonblocking, identity-bound inputs selected once for this decision."""
    address = str(token.get("address") or "")
    candidates = []
    cached = None
    if bool(getattr(CFG, "SOCIALS_ENABLED", True)):
        snapshot = checked_social_receipt(token.get("social_signal"), address, max_age_s=social_cache_ttl_s())
        cached = GLOBAL_SOCIAL_ENRICHMENT_QUEUE.get_cached(address)
        candidates = [signal for signal in (snapshot, cached) if signal is not None]
    signal = max(candidates, key=lambda item: item.received_at) if candidates else unknown_social_signal()
    # A newer unchanged raw profile is not a reason to discard a checked risk
    # result. Keep the complete older, still-fresh receipt, without re-stamping.
    if candidates and cached is not None and cached.risk_checked_at is not None:
        if social_profile_identity(signal) == social_profile_identity(cached):
            signal = cached
    apply_social_signal_to_token(token, signal)
    return signal


async def stop_background_tasks() -> None:
    await GLOBAL_SOCIAL_ENRICHMENT_QUEUE.stop()


__all__ = [
    "GLOBAL_SOCIAL_ENRICHMENT_QUEUE",
    "SocialEnrichmentQueue",
    "SocialEnrichmentRequest",
    "schedule_social_enrichment",
    "consume_social_enrichment",
    "stop_background_tasks",
]
