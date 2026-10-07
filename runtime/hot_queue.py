from __future__ import annotations

import datetime as dt
import heapq
import itertools
import math
from dataclasses import asdict, dataclass
from typing import Any

from config.config import CFG
from runtime.candidate_priority import candidate_priority_score
from utils.runtime_telemetry import record_runtime_event


@dataclass(frozen=True)
class HotQueueEvent:
    event: str
    address: str
    source: str
    priority_score: float
    reason: str
    ts_utc: str


class HotQueue:
    def __init__(
        self,
        *,
        max_size: int = 300,
        max_age_min: float = 20.0,
        dedup_ttl_s: int = 1800,
        persist_events: bool = False,
    ) -> None:
        self.max_size = max(1, int(max_size))
        self.max_age_min = max(0.0, float(max_age_min))
        self.dedup_ttl_s = max(1, int(dedup_ttl_s))
        self.persist_events = bool(persist_events)
        self._heap: list[tuple[float, int, dict[str, Any]]] = []
        self._counter = itertools.count()
        self._seen: dict[str, float] = {}
        self._pending: dict[str, int] = {}
        self._signals: dict[str, tuple[Any, ...]] = {}
        self._evaluated_at: dict[str, float] = {}
        self._last_prune_at = 0.0
        self._events: list[HotQueueEvent] = []
        self._drop_counts: dict[str, int] = {}

    def _now(self) -> float:
        return dt.datetime.now(dt.timezone.utc).timestamp()

    def _event(self, event: str, token: dict[str, Any], source: str, score: float, reason: str) -> None:
        address = str(token.get("address") or token.get("mint") or "")
        self._events.append(
            HotQueueEvent(
                event=event,
                address=address,
                source=source,
                priority_score=float(score),
                reason=reason,
                ts_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        )
        if len(self._events) > 1000:
            self._events = self._events[-1000:]
        if self.persist_events:
            try:
                record_runtime_event(
                    event,
                    address,
                    source=source,
                    priority_score=float(score),
                    reason=str(reason),
                )
            except Exception:
                pass

    def _count_drop(self, reason: str, score: float) -> None:
        key = "dropped_high_priority" if score >= float(getattr(CFG, "HOT_QUEUE_HIGH_PRIORITY_MIN_SCORE", 75.0) or 75.0) else "dropped_low_priority"
        self._drop_counts[key] = self._drop_counts.get(key, 0) + 1
        self._drop_counts[f"reason:{reason}"] = self._drop_counts.get(f"reason:{reason}", 0) + 1

    def _drop_lowest_priority(self, source: str) -> None:
        self._compact_heap()
        if not self._heap:
            return
        # Heap entries store negative scores so the default heappop returns the
        # highest-priority candidate. Overflow eviction must remove the lowest.
        lowest_idx = max(range(len(self._heap)), key=lambda idx: self._heap[idx][0])
        neg_score, _, dropped = self._heap.pop(lowest_idx)
        self._pending.pop(str(dropped.get("address") or dropped.get("mint") or ""), None)
        heapq.heapify(self._heap)
        score = -float(neg_score)
        self._count_drop("max_size", score)
        self._event("hot_queue_drop", dropped, str(dropped.get("source") or source), score, "max_size")

    def _compact_heap(self) -> None:
        self._heap = [item for item in self._heap
                      if self._pending.get(str(item[2].get("address") or item[2].get("mint") or "")) == item[1]]
        heapq.heapify(self._heap)

    def _prune_history(self, now: float) -> None:
        if now - self._last_prune_at < 60:
            return
        for address, timestamp in list(self._seen.items()):
            if now - timestamp >= self.dedup_ttl_s and address not in self._pending:
                self._seen.pop(address, None)
                self._signals.pop(address, None)
                self._evaluated_at.pop(address, None)
        self._last_prune_at = now

    def add(self, token: dict[str, Any], *, source: str = "pumpfun", reason: str = "hot_candidate") -> bool:
        address = str(token.get("address") or token.get("mint") or "").strip()
        if not address:
            return False
        now = self._now()
        self._prune_history(now)
        last_seen = self._seen.get(address)
        pending_version = self._pending.get(address)
        previous = next((item[2] for item in self._heap if item[1] == pending_version), None) if pending_version is not None else None
        incoming_has_age = any(key in token for key in ("age_minutes", "age_min"))
        token = {**(previous or {}), **token}
        signal = _market_signal(token)
        changed = signal != self._signals.get(address)
        interval = max(1.0, float(getattr(CFG, "HOT_QUEUE_RECHECK_INTERVAL_S", 15.0)))
        if last_seen is not None and now - last_seen < self.dedup_ttl_s and (
            not changed or (pending_version is None and now - self._evaluated_at.get(address, last_seen) < interval)
        ):
            self._event("hot_queue_drop", token, source, 0.0, "dedup")
            return False
        first_enqueued = previous.get("_hot_queue_enqueued_at", now) if previous else now
        token = dict(token)
        token["address"] = address
        token.setdefault("source", source)
        token.setdefault("discovered_via", source)
        token["_hot_queue_enqueued_at"] = first_enqueued
        token["_hot_queue_observed_at"] = now if incoming_has_age or not previous else previous.get("_hot_queue_observed_at", now)
        score = candidate_priority_score(token, source=source, now=dt.datetime.fromtimestamp(now, dt.timezone.utc))
        self._seen[address] = now
        self._signals[address] = _market_signal(token)
        sequence = next(self._counter)
        self._pending[address] = sequence
        heapq.heappush(self._heap, (-score, sequence, token))
        self._event("hot_queue_update" if previous else "hot_queue_add", token, source, score, reason)
        while len(self._pending) > self.max_size:
            self._drop_lowest_priority(source)
        if len(self._heap) > self.max_size * 2:
            self._compact_heap()
        return True

    def pop_batch(self, limit: int | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        max_items = max(1, int(limit or getattr(CFG, "HOT_QUEUE_BATCH_SIZE", 12) or 12))
        if bool(getattr(CFG, "HOT_QUEUE_DYNAMIC_BATCH_ENABLED", True)) and len(self._pending) > max_items * 2:
            max_items = min(max_items * 3, len(self._pending), 100)
        now = dt.datetime.fromtimestamp(self._now(), dt.timezone.utc)
        while self._heap and len(out) < max_items:
            neg_score, sequence, token = heapq.heappop(self._heap)
            address = str(token.get("address") or token.get("mint") or "")
            if self._pending.get(address) != sequence:
                continue
            self._pending.pop(address, None)
            age_min = _age_minutes(token, now)
            source = str(token.get("source") or token.get("discovered_via") or "hot")
            score = -float(neg_score)
            priority_age = (
                float(getattr(CFG, "HOT_QUEUE_HIGH_PRIORITY_MAX_AGE_MIN", self.max_age_min) or self.max_age_min)
                if score >= float(getattr(CFG, "HOT_QUEUE_HIGH_PRIORITY_MIN_SCORE", 75.0) or 75.0)
                else float(getattr(CFG, "HOT_QUEUE_LOW_PRIORITY_MAX_AGE_MIN", self.max_age_min) or self.max_age_min)
            )
            max_age = min(self.max_age_min, priority_age) if self.max_age_min > 0 and priority_age > 0 else max(self.max_age_min, priority_age)
            if max_age > 0 and age_min > max_age:
                self._count_drop("max_age", score)
                self._event("hot_queue_drop", token, source, score, "max_age")
                continue
            self._event("hot_queue_eval", token, source, score, "green_candidate")
            self._evaluated_at[address] = now.timestamp()
            out.append(token)
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": bool(getattr(CFG, "HOT_QUEUE_ENABLED", True)),
            "size": len(self._pending),
            "max_size": self.max_size,
            "max_age_min": self.max_age_min,
            "drop_counts": dict(self._drop_counts),
            "recent_events": [asdict(event) for event in self._events[-50:]],
        }

    def events(self) -> list[dict[str, Any]]:
        return [asdict(event) for event in self._events]


def _age_minutes(token: dict[str, Any], now: dt.datetime) -> float:
    waited = max(0.0, (now.timestamp() - float(token.get("_hot_queue_enqueued_at", now.timestamp()))) / 60)
    created = token.get("created_at") or token.get("createdAt")
    if isinstance(created, str):
        try:
            created = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        except Exception:
            created = None
    if isinstance(created, dt.datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        return max(waited, (now - created).total_seconds() / 60.0)
    for key in ("age_minutes", "age_min"):
        try:
            if token.get(key) is not None:
                age = float(token[key])
                observed = float(token.get("_hot_queue_observed_at", now.timestamp()))
                if math.isfinite(age) and age >= 0:
                    return max(waited, age + max(0.0, now.timestamp() - observed) / 60)
        except Exception:
            continue
    return waited


def _market_signal(token: dict[str, Any]) -> tuple[Any, ...]:
    def numeric(key, *, step=1.0, logarithmic=False):
        try:
            value = float(token[key])
            if not math.isfinite(value):
                return None
            if logarithmic:
                return math.floor(math.log(value) / math.log(1.10)) if value > 0 else None
            return math.floor(value / step)
        except (KeyError, TypeError, ValueError):
            return None
    return (numeric("price_pct_5m", step=5), numeric("price_usd", logarithmic=True), numeric("txns_last_5m", step=25),
            numeric("liquidity_usd", logarithmic=True), numeric("market_cap_usd", logarithmic=True),
            str(token.get("has_jupiter_route", "unknown")).lower(),
            str(token.get("dex_id") or token.get("dexId") or "").lower())


GLOBAL_HOT_QUEUE = HotQueue(
    max_size=int(getattr(CFG, "HOT_QUEUE_MAX_SIZE", 300) or 300),
    max_age_min=float(getattr(CFG, "HOT_QUEUE_MAX_AGE_MIN", 20.0) or 20.0),
    dedup_ttl_s=int(getattr(CFG, "HOT_QUEUE_DEDUP_TTL_S", 1800) or 1800),
    persist_events=True,
)


__all__ = ["GLOBAL_HOT_QUEUE", "HotQueue", "HotQueueEvent"]
