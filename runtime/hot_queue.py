from __future__ import annotations

import datetime as dt
import heapq
import itertools
import math
import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from config.config import CFG
from runtime.candidate_priority import candidate_priority_score
from runtime.runner_priority import runner_priority_generation, learned_runner_priorities
from analytics.inference_scope import inference_scope
from analytics.token_time import compute_age_minutes
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
        self._evaluated_signals: dict[str, tuple[Any, ...]] = {}
        self._evaluated_generations: dict[str, str] = {}
        self._priority_generation: str | None = None
        self._priority_refreshes = 0
        self._last_priority_refresh_at: float | None = None
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
        # Do not let equal-priority newcomers repeatedly displace candidates
        # already waiting for evaluation.
        lowest_idx = max(range(len(self._heap)), key=lambda idx: self._heap[idx][:2])
        neg_score, _, dropped = self._heap.pop(lowest_idx)
        self._pending.pop(str(dropped.get("address") or dropped.get("mint") or ""), None)
        self._restore_evaluated_history(str(dropped.get("address") or dropped.get("mint") or ""))
        heapq.heapify(self._heap)
        score = -float(neg_score)
        self._count_drop("max_size", score)
        self._event("hot_queue_drop", dropped, str(dropped.get("source") or source), score, "max_size")

    def _restore_evaluated_history(self, address: str) -> None:
        """An admission/eviction is not an evaluation of its market signal."""
        stamp, signal = self._evaluated_at.get(address), self._evaluated_signals.get(address)
        if stamp is None or signal is None:
            self._seen.pop(address, None)
            self._signals.pop(address, None)
        else:
            self._seen[address], self._signals[address] = stamp, signal

    def _expired(self, token: dict[str, Any], score: float, now: dt.datetime) -> bool:
        priority_age = (
            float(getattr(CFG, "HOT_QUEUE_HIGH_PRIORITY_MAX_AGE_MIN", self.max_age_min) or self.max_age_min)
            if score >= float(getattr(CFG, "HOT_QUEUE_HIGH_PRIORITY_MIN_SCORE", 75.0) or 75.0)
            else float(getattr(CFG, "HOT_QUEUE_LOW_PRIORITY_MAX_AGE_MIN", self.max_age_min) or self.max_age_min)
        )
        max_age = min(self.max_age_min, priority_age) if self.max_age_min > 0 and priority_age > 0 else max(self.max_age_min, priority_age)
        return max_age > 0 and _age_minutes(token, now) > max_age

    def _prune_expired(self, now: dt.datetime, *, except_address: str) -> None:
        self._compact_heap()
        for neg_score, sequence, token in self._heap:
            address = str(token.get("address") or token.get("mint") or "")
            if address == except_address or not self._expired(token, -float(neg_score), now):
                continue
            self._pending.pop(address, None)
            self._restore_evaluated_history(address)
            self._count_drop("max_age", -float(neg_score))
            self._event("hot_queue_drop", token, str(token.get("source") or "hot"), -float(neg_score), "max_age")
        self._compact_heap()

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
                self._evaluated_signals.pop(address, None)
                self._evaluated_generations.pop(address, None)
        self._last_prune_at = now

    def add(self, token: dict[str, Any], *, source: str = "pumpfun", reason: str = "hot_candidate") -> bool:
        if not str(token.get("address") or token.get("mint") or "").strip():
            return False
        # This is scheduling, not the already pinned generation of an entry.
        # All waiting candidates and the newcomer see one detached family.
        with inference_scope(record_observations=False):
            now = self._now()
            generation = runner_priority_generation()
            self._refresh_priorities(now, generation, force=False)
            return self._add(token, source=source, reason=reason, now=now, generation=generation)

    def _score(self, token: dict[str, Any], *, source: str, now: dt.datetime,
               generation: str, reuse: bool) -> float:
        view = self._priority_view(token, now)
        cached = token.get("learned_runner_priority") if reuse and token.get("_hot_queue_priority_generation") == generation else None
        score = candidate_priority_score(view, source=source, now=now,
            learned_priority=cached if isinstance(cached, dict) else None)
        if isinstance(view.get("learned_runner_priority"), dict):
            token["learned_runner_priority"] = view["learned_runner_priority"]
        token["_hot_queue_priority_generation"] = generation
        return float(score) if not isinstance(score, bool) and math.isfinite(float(score)) else 0.

    def _priority_view(self, token: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
        view = dict(token)
        # A measured age advances from its original observation. Queue waiting
        # must not turn missing birth age into a measured/fabricated age.
        birth_view = {k: v for k, v in view.items() if k not in ("age_minutes", "age_min", "token_age_min")}
        if compute_age_minutes(birth_view, now=now) is None:
            measured = compute_age_minutes(view, now=now)
            if measured is not None:
                observed = float(token.get("_hot_queue_observed_at", now.timestamp()))
                view["age_minutes"] = measured + max(0., now.timestamp() - observed) / 60
        return view

    def _refresh_priorities(self, timestamp: float, generation: str, *, force: bool = True) -> None:
        """One bounded heap rebuild, no reenqueue, evaluation or dedup reset.

        Checked model bytes are captured once per queue operation. Predictions
        are reused only for the same generation/unchanged feed snapshot; age and
        deterministic priority are refreshed at every actual pop and at most
        one second apart during admission bursts (never delay a new generation).
        A generation change recomputes the advisory snapshot rankings together.
        """
        changed = generation != self._priority_generation
        if (not force and not changed and self._last_priority_refresh_at is not None
                and 0 <= timestamp - self._last_priority_refresh_at < 1.):
            return
        self._compact_heap()
        now = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)
        if changed:
            # Fixed chunks bound the batch even if an operator configures a
            # larger queue. One family scope covers every chunk and target.
            for start in range(0, len(self._heap), 1000):
                tokens = [entry[2] for entry in self._heap[start:start+1000]]
                rankings = learned_runner_priorities([self._priority_view(token, now) for token in tokens], now=now)
                for token, ranking in zip(tokens, rankings):
                    token["learned_runner_priority"] = ranking
                    token["_hot_queue_priority_generation"] = generation
        self._heap = [(-self._score(token, source=str(token.get("source") or token.get("discovered_via") or "hot"),
            now=now, generation=generation, reuse=True), sequence, token)
            for _, sequence, token in self._heap]
        heapq.heapify(self._heap)
        self._priority_generation = generation
        self._last_priority_refresh_at = timestamp
        self._priority_refreshes += 1
        if changed and self._heap:
            self._event("hot_queue_reprice", {"address": ""}, "hot", 0., f"checked_generation_changed:{len(self._heap)}")

    def _add(self, token: dict[str, Any], *, source: str, reason: str, now: float, generation: str) -> bool:
        address = str(token.get("address") or token.get("mint") or "").strip()
        # Internal clocks and learned cache identities are queue-owned.
        token = deepcopy({k: v for k, v in token.items() if not k.startswith("_hot_queue_") and k != "learned_runner_priority"})
        self._prune_history(now)
        last_seen = self._seen.get(address)
        pending_version = self._pending.get(address)
        previous = next((item[2] for item in self._heap if item[1] == pending_version), None) if pending_version is not None else None
        self._prune_expired(dt.datetime.fromtimestamp(now, dt.timezone.utc), except_address=address)
        incoming_has_age = any(_valid_age(token.get(key)) for key in ("age_minutes", "age_min"))
        token = {**(previous or {}), **token}
        token["address"] = address
        token.setdefault("source", source)
        token.setdefault("discovered_via", source)
        signal = _market_signal(token)
        changed = signal != self._signals.get(address)
        if previous is not None:
            changed |= _snapshot_signal(token) != _snapshot_signal(previous)
        else:
            changed |= generation != self._evaluated_generations.get(address)
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
        score = self._score(token, source=source, now=dt.datetime.fromtimestamp(now, dt.timezone.utc),
            generation=generation, reuse=False)
        if self._expired(token, score, dt.datetime.fromtimestamp(now, dt.timezone.utc)):
            self._pending.pop(address, None)
            self._restore_evaluated_history(address)
            self._compact_heap()
            self._count_drop("max_age", score)
            self._event("hot_queue_drop", token, source, score, "max_age")
            return False
        self._seen[address] = now
        self._signals[address] = _market_signal(token)
        # Stable waiting order is separate from a changed market snapshot.
        sequence = pending_version if previous is not None else next(self._counter)
        if previous is not None:
            self._heap = [item for item in self._heap if item[1] != sequence]
            heapq.heapify(self._heap)
        self._pending[address] = sequence
        heapq.heappush(self._heap, (-score, sequence, token))
        self._event("hot_queue_update" if previous else "hot_queue_add", token, source, score, reason)
        while len(self._pending) > self.max_size:
            self._drop_lowest_priority(source)
        if len(self._heap) > self.max_size * 2:
            self._compact_heap()
        return self._pending.get(address) == sequence

    def pop_batch(self, limit: int | None = None, *, expand: bool = True) -> list[dict[str, Any]]:
        if not self._pending:
            return []
        with inference_scope(record_observations=False):
            generation = runner_priority_generation()
            self._refresh_priorities(self._now(), generation)
            return self._pop_batch(limit, expand=expand, generation=generation)

    def _pop_batch(self, limit: int | None, *, expand: bool, generation: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        max_items = max(1, int(limit or getattr(CFG, "HOT_QUEUE_BATCH_SIZE", 12) or 12))
        if expand and bool(getattr(CFG, "HOT_QUEUE_DYNAMIC_BATCH_ENABLED", True)) and len(self._pending) > max_items * 2:
            max_items = min(max_items * 3, len(self._pending), 100)
        now = dt.datetime.fromtimestamp(self._now(), dt.timezone.utc)
        while self._heap and len(out) < max_items:
            neg_score, sequence, token = heapq.heappop(self._heap)
            address = str(token.get("address") or token.get("mint") or "")
            if self._pending.get(address) != sequence:
                continue
            self._pending.pop(address, None)
            source = str(token.get("source") or token.get("discovered_via") or "hot")
            score = -float(neg_score)
            if self._expired(token, score, now):
                self._restore_evaluated_history(address)
                self._count_drop("max_age", score)
                self._event("hot_queue_drop", token, source, score, "max_age")
                continue
            self._event("hot_queue_eval", token, source, score, "green_candidate")
            self._evaluated_at[address] = now.timestamp()
            self._evaluated_signals[address] = _market_signal(token)
            self._evaluated_generations[address] = generation
            self._seen[address] = now.timestamp()
            out.append(token)
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": bool(getattr(CFG, "HOT_QUEUE_ENABLED", True)),
            "size": len(self._pending),
            "max_size": self.max_size,
            "max_age_min": self.max_age_min,
            "drop_counts": dict(self._drop_counts),
            "priority_generation": self._priority_generation,
            "priority_refreshes": self._priority_refreshes,
            "recent_events": [asdict(event) for event in self._events[-50:]],
        }

    def events(self) -> list[dict[str, Any]]:
        return [asdict(event) for event in self._events]


def _valid_age(value: Any) -> bool:
    try:
        return not isinstance(value, bool) and value is not None and math.isfinite(float(value)) and float(value) >= 0
    except (TypeError, ValueError, OverflowError):
        return False


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
                if _valid_age(token[key]):
                    return max(waited, age + max(0.0, now.timestamp() - observed) / 60)
        except Exception:
            continue
    return waited


def _market_signal(token: dict[str, Any]) -> tuple[Any, ...]:
    def numeric(key, *, step=1.0, logarithmic=False):
        try:
            if isinstance(token[key], bool):
                return None
            value = float(token[key])
            if not math.isfinite(value):
                return None
            if logarithmic:
                return math.floor(math.log(value) / math.log(1.10)) if value > 0 else None
            return math.floor(value / step)
        except (KeyError, TypeError, ValueError):
            return None
    from runtime.candidate_priority import _normalize_score
    rank = token.get("rank_score") if token.get("rank_score") is not None else token.get("research_rank_score")
    return (numeric("price_pct_5m", step=5), numeric("price_usd", logarithmic=True), numeric("txns_last_5m", step=25),
            numeric("liquidity_usd", logarithmic=True), numeric("market_cap_usd", logarithmic=True),
            _normalize_score(rank, -1.0),
            str(token.get("has_jupiter_route", "unknown")).lower(),
            str(token.get("dex_id") or token.get("dexId") or "").lower(), _learning_signal(token))


def _learning_signal(token: dict[str, Any]) -> str:
    # Keep market movement buckets, but do not suppress a changed learned input
    # (holders, volume, social/risk observation, context) for thirty minutes.
    from features.builder import ALLOWED_FEATURES
    fields = ALLOWED_FEATURES - {"age_minutes", "queue_age_minutes", "queue_attempts",
        "price_pct_5m", "txns_last_5m", "liquidity_usd", "market_cap_usd", "dex_id"}
    fields |= {"social_signal", "auxiliary_observations", "sniper_gate_profile", "liquidity_usd_is_proxy"}
    return json.dumps({k: token[k] for k in sorted(fields) if k in token}, sort_keys=True,
        separators=(",", ":"), default=str)


def _snapshot_signal(token: dict[str, Any]) -> str:
    """Exact pending snapshot updates, not only coarse reevaluation buckets."""
    view = {k: v for k, v in token.items() if not k.startswith("_hot_queue_")
            and k not in {"learned_runner_priority", "source", "discovered_via"}}
    return json.dumps(view, sort_keys=True, separators=(",", ":"), default=str)


GLOBAL_HOT_QUEUE = HotQueue(
    max_size=int(getattr(CFG, "HOT_QUEUE_MAX_SIZE", 300) or 300),
    max_age_min=float(getattr(CFG, "HOT_QUEUE_MAX_AGE_MIN", 20.0) or 20.0),
    dedup_ttl_s=int(getattr(CFG, "HOT_QUEUE_DEDUP_TTL_S", 1800) or 1800),
    persist_events=True,
)


__all__ = ["GLOBAL_HOT_QUEUE", "HotQueue", "HotQueueEvent"]
