from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from api.repositories.filesystem import load_jsonl_tail_rows
from config.config import CFG
from config.config import PROJECT_ROOT
from runtime.hot_queue import GLOBAL_HOT_QUEUE
from runtime.live_canary import snapshot as live_canary_snapshot
from runtime.social_enrichment_queue import GLOBAL_SOCIAL_ENRICHMENT_QUEUE


_JSON_CACHE: dict[Path, tuple[float, int, Any]] = {}
_FAST_AUDIT_TAIL_ROWS = 5_000


def _metrics_path(name: str) -> Path:
    return PROJECT_ROOT / "data" / "metrics" / name


def _read_json_cached(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        stat = path.stat()
    except Exception:
        return None
    cached = _JSON_CACHE.get(path)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None
    _JSON_CACHE[path] = (stat.st_mtime, stat.st_size, payload)
    return payload


def _row_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "data", "items"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _fast_sniper_audit() -> dict[str, Any]:
    runtime_rows = load_jsonl_tail_rows(_metrics_path("runtime_events.jsonl"), limit=_FAST_AUDIT_TAIL_ROWS)
    outcome_rows = load_jsonl_tail_rows(_metrics_path("candidate_outcomes.jsonl"), limit=_FAST_AUDIT_TAIL_ROWS)
    rejected: Counter[str] = Counter()
    shadowed: Counter[str] = Counter()
    bought_by_lane: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    pumpfun_seen = 0
    hot_seen = 0

    for row in runtime_rows + outcome_rows:
        event = str(row.get("event") or row.get("event_type") or row.get("action") or row.get("decision_action") or "").lower()
        action = str(row.get("action") or row.get("decision_action") or "").lower()
        reason = str(row.get("reason") or row.get("reject_reason") or row.get("stage") or "unknown")
        lane = str(row.get("entry_lane") or row.get("lane") or "unknown")
        source = str(row.get("source") or row.get("discovered_via") or "unknown").lower()
        source_counts[source] += 1
        if source in {"pumpfun", "pumpportal"}:
            pumpfun_seen += 1
            hot_seen += 1
        if "reject" in event or action == "rejected":
            rejected[reason] += 1
        if "shadow" in event or action == "shadow":
            shadowed[reason] += 1
        if "buy" in event or action in {"bought", "buy"}:
            bought_by_lane[lane] += 1

    return {
        "total_candidates_seen": len(runtime_rows) + len(outcome_rows),
        "pumpfun_candidates_seen": pumpfun_seen,
        "hot_candidates_seen": hot_seen,
        "rejected_by_reason": dict(rejected.most_common()),
        "shadowed_by_reason": dict(shadowed.most_common()),
        "bought_by_lane": dict(bought_by_lane.most_common()),
        "by_source": dict(source_counts),
        "avg_time_seen_to_eval_s": None,
        "avg_time_seen_to_buy_s": None,
        "sampled": True,
        "sample_rows": {
            "runtime": len(runtime_rows),
            "candidate_outcomes": len(outcome_rows),
        },
    }


def _cached_sniper_audit() -> dict[str, Any]:
    payload = _read_json_cached(_metrics_path("sniper_audit.json"))
    return payload if isinstance(payload, dict) else _fast_sniper_audit()


def _cached_current_missed_pumps(limit: int) -> list[dict[str, Any]]:
    current = _read_json_cached(_metrics_path("current_run_missed_pumps.json"))
    return _row_list(current)[:limit]


def _cached_missed_pumps(limit: int) -> list[dict[str, Any]]:
    current_rows = _cached_current_missed_pumps(limit)
    if current_rows:
        return current_rows
    historical = _read_json_cached(_metrics_path("missed_pumps.json"))
    return _row_list(historical)[:limit]


def _green_sniper_policy() -> dict[str, object]:
    return {
        "enabled": bool(getattr(CFG, "GREEN_SNIPER_ENABLED", True)),
        "paper_sniper_mode": bool(getattr(CFG, "PAPER_SNIPER_MODE", False)),
        "live_enabled": bool(getattr(CFG, "GREEN_SNIPER_LIVE_ENABLED", False)),
        "entry_lane": "pump_early_green_candle_sniper",
        "require_route_paper": bool(getattr(CFG, "GREEN_SNIPER_REQUIRE_ROUTE_PAPER", False)),
        "require_route_live": bool(getattr(CFG, "GREEN_SNIPER_REQUIRE_ROUTE_LIVE", True)),
        "allow_proxy_liquidity_paper": bool(getattr(CFG, "GREEN_SNIPER_ALLOW_PROXY_LIQUIDITY_PAPER", True)),
        "rank_guard_enabled": bool(getattr(CFG, "GREEN_SNIPER_RANK_GUARD_ENABLED", True)),
        "rank_guard_min_score": float(getattr(CFG, "GREEN_SNIPER_RANK_GUARD_MIN_SCORE", 45.0) or 45.0),
        "rank_guard_bypass_paper_birth_probe": bool(
            getattr(CFG, "GREEN_SNIPER_RANK_GUARD_BYPASS_PAPER_BIRTH_PROBE", False)
        ),
        "paper_birth_probe": {
            "enabled": bool(getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_ENABLED", True)),
            "shadow_first": bool(getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_SHADOW_FIRST", True)),
            "max_age_min": float(getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_AGE_MIN", 3.0) or 3.0),
            "min_liquidity_usd": float(
                getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_MIN_LIQUIDITY_USD", 1000.0) or 1000.0
            ),
            "max_price_impact_pct": float(
                getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_PRICE_IMPACT_PCT", 25.0) or 25.0
            ),
        },
        "ml_mode": str(getattr(CFG, "GREEN_SNIPER_ML_MODE", "sizing_only") or "sizing_only"),
        "ml_can_block": bool(getattr(CFG, "GREEN_SNIPER_ML_BLOCK_ENABLED", False)),
        "socials": {
            "enabled": bool(getattr(CFG, "SOCIALS_ENABLED", True)),
            "async_only": bool(getattr(CFG, "SOCIALS_ASYNC_ONLY", True)),
            "hot_path_blocking": bool(getattr(CFG, "SOCIALS_HOT_PATH_BLOCKING", False)),
            "require_socials": bool(getattr(CFG, "GREEN_SNIPER_REQUIRE_SOCIALS", False)),
            "suspicious_can_block": bool(getattr(CFG, "GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_BLOCK", False)),
        },
        "paper_size_sol": {
            "micro": float(getattr(CFG, "GREEN_SNIPER_SIZE_MICRO_SOL", 0.10) or 0.10),
            "core": float(getattr(CFG, "GREEN_SNIPER_SIZE_CORE_SOL", 0.10) or 0.10),
            "hot": float(getattr(CFG, "GREEN_SNIPER_SIZE_HOT_SOL", 0.10) or 0.10),
        },
        "live_size_sol": float(getattr(CFG, "GREEN_SNIPER_LIVE_SIZE_SOL", 0.01) or 0.01),
        "research_rank_canary": {
            "enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_LIVE_ENABLED", False)),
            "min_score": float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_SCORE", 61.15) or 61.15),
        },
        "risk_guards": {
            "green_sniper_risk_guard_enabled": bool(getattr(CFG, "GREEN_SNIPER_RISK_GUARD_ENABLED", True)),
            "liquidity_guard_enabled": bool(getattr(CFG, "GREEN_SNIPER_LIQ_GUARD_ENABLED", True)),
            "early_dump_enabled": bool(getattr(CFG, "GREEN_SNIPER_EARLY_DUMP_ENABLED", True)),
            "late_momentum_watch_enabled": bool(getattr(CFG, "LATE_MOMENTUM_WATCH_ENABLED", True)),
        },
    }


def sniper_status() -> dict[str, object]:
    audit = _cached_sniper_audit()
    hot = GLOBAL_HOT_QUEUE.snapshot()
    return {
        "hot_queue_size": hot["size"],
        "hot_queue": hot,
        "green_sniper_buys_today": audit.get("bought_by_lane", {}).get("pump_early_green_candle_sniper", 0),
        "green_sniper_shadows_today": audit.get("shadowed_by_reason", {}),
        "green_sniper_rejects_today": audit.get("rejected_by_reason", {}),
        "avg_time_to_eval_s": audit.get("avg_time_seen_to_eval_s"),
        "avg_time_to_buy_s": audit.get("avg_time_seen_to_buy_s"),
        "top_reject_reasons": list(audit.get("rejected_by_reason", {}).items())[:10],
        "missed_pumps_top10": _cached_current_missed_pumps(10),
        "live_canary": live_canary_snapshot(),
        "green_sniper_policy": _green_sniper_policy(),
        "social_enrichment": GLOBAL_SOCIAL_ENRICHMENT_QUEUE.snapshot(),
    }


def missed_pumps(limit: int = 50) -> dict[str, object]:
    rows = _cached_missed_pumps(max(1, min(int(limit), 250)))
    return {"count": len(rows), "items": rows}


def hot_queue_status() -> dict[str, object]:
    return GLOBAL_HOT_QUEUE.snapshot()


__all__ = ["hot_queue_status", "missed_pumps", "sniper_status"]
