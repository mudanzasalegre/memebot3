from __future__ import annotations

import datetime as dt
import heapq
import itertools
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
        self._events: list[HotQueueEvent] = []

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

    def add(self, token: dict[str, Any], *, source: str = "pumpfun", reason: str = "hot_candidate") -> bool:
        address = str(token.get("address") or token.get("mint") or "").strip()
        if not address:
            return False
        now = self._now()
        last_seen = self._seen.get(address)
        if last_seen is not None and now - last_seen < self.dedup_ttl_s:
            self._event("hot_queue_drop", token, source, 0.0, "dedup")
            return False
        token = dict(token)
        token.setdefault("address", address)
        token.setdefault("source", source)
        token.setdefault("discovered_via", source)
        score = candidate_priority_score(token, source=source)
        self._seen[address] = now
        heapq.heappush(self._heap, (-score, next(self._counter), token))
        self._event("hot_queue_add", token, source, score, reason)
        while len(self._heap) > self.max_size:
            _, _, dropped = heapq.heappop(self._heap)
            self._event("hot_queue_drop", dropped, str(dropped.get("source") or source), 0.0, "max_size")
        return True

    def pop_batch(self, limit: int | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        max_items = max(1, int(limit or getattr(CFG, "HOT_QUEUE_BATCH_SIZE", 12) or 12))
        now = dt.datetime.now(dt.timezone.utc)
        while self._heap and len(out) < max_items:
            neg_score, _, token = heapq.heappop(self._heap)
            age_min = _age_minutes(token, now)
            source = str(token.get("source") or token.get("discovered_via") or "hot")
            score = -float(neg_score)
            if self.max_age_min > 0 and age_min > self.max_age_min:
                self._event("hot_queue_drop", token, source, score, "max_age")
                continue
            self._event("hot_queue_eval", token, source, score, "green_candidate")
            out.append(token)
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": bool(getattr(CFG, "HOT_QUEUE_ENABLED", True)),
            "size": len(self._heap),
            "max_size": self.max_size,
            "max_age_min": self.max_age_min,
            "recent_events": [asdict(event) for event in self._events[-50:]],
        }

    def events(self) -> list[dict[str, Any]]:
        return [asdict(event) for event in self._events]


def _age_minutes(token: dict[str, Any], now: dt.datetime) -> float:
    created = token.get("created_at") or token.get("createdAt")
    if isinstance(created, str):
        try:
            created = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        except Exception:
            created = None
    if isinstance(created, dt.datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (now - created).total_seconds() / 60.0)
    for key in ("age_minutes", "age_min"):
        try:
            if token.get(key) is not None:
                return max(0.0, float(token[key]))
        except Exception:
            continue
    return 0.0


GLOBAL_HOT_QUEUE = HotQueue(
    max_size=int(getattr(CFG, "HOT_QUEUE_MAX_SIZE", 300) or 300),
    max_age_min=float(getattr(CFG, "HOT_QUEUE_MAX_AGE_MIN", 20.0) or 20.0),
    dedup_ttl_s=int(getattr(CFG, "HOT_QUEUE_DEDUP_TTL_S", 1800) or 1800),
    persist_events=True,
)


__all__ = ["GLOBAL_HOT_QUEUE", "HotQueue", "HotQueueEvent"]
