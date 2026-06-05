from __future__ import annotations

import json
import os
import statistics
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.config import CFG, PROJECT_ROOT
from analytics.current_run import current_run_identity, filter_current_run_rows
from analytics.lane_policy_categories import POLICY_RESEARCH_RANK_CANARY
from analytics.report_utils import (
    fnum,
    is_severe_exit,
    load_candidate_outcomes,
    load_paper_positions,
    load_runtime_events,
    load_sqlite_positions,
    metrics_dir,
    write_json,
)
from ml.lane_taxonomy import LANE_RESEARCH_RANK_CANARY, LANE_RESEARCH_SNIPER, normalize_entry_lane


AUDIT_PATH = PROJECT_ROOT / "data" / "metrics" / "research_rank_canary_audit.json"
_AUDIT_LOCK = threading.Lock()


def _runtime_side_effects_disabled() -> bool:
    return bool(os.getenv("PYTEST_CURRENT_TEST")) or bool(os.getenv("MEMEBOT_DISABLE_RUNTIME_AUDIT"))


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _score_threshold(value: Any, default: float = 0.647) -> float:
    return normalize_score(value, default)[1]


def normalize_score(value: Any, default: float = 0.0) -> tuple[float, float, str]:
    raw = _float(value, default)
    if 0.0 < raw <= 1.0:
        return raw, raw * 100.0, "0_1"
    return raw, raw, "0_100"


def _field_float(token: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = token.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return _float(value, default)
    return default


@dataclass(frozen=True)
class ResearchRankCanaryDecision:
    allowed: bool
    entry_lane: str
    reason: str
    rank_score: float
    min_score: float
    amount_sol: float
    rank_score_raw: float = 0.0
    rank_score_scale: str = "0_100"
    min_score_raw: float = 0.0
    min_score_scale: str = "0_100"
    shadow_as_own_lane: bool = False
    executable: bool = True
    priority: bool = False
    pullback: bool = False
    elite_consolidation: bool = False
    pullback_tail_micro: bool = False


def _read_audit() -> dict[str, Any]:
    try:
        payload = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _record_audit(token: dict[str, Any], decision: ResearchRankCanaryDecision, *, dry_run: bool, live: bool) -> None:
    if _runtime_side_effects_disabled():
        return
    now = datetime.now(timezone.utc).isoformat()
    reason = str(decision.reason or "unknown")
    address = str(token.get("address") or token.get("token_address") or "")
    payload = {
        "updated_at_utc": now,
        "total_evaluations": 0,
        "evaluated": 0,
        "allowed": 0,
        "rejected": 0,
        "bought_as_own_lane": 0,
        "shadow_as_own_lane": 0,
        "mixed_lane_detected": 0,
        "reasons": {},
        "blocked_by_reason": {},
        "last_decision": {},
        "config": {
            "enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_LIVE_ENABLED", False)),
            "min_score_raw": decision.min_score_raw,
            "min_score_normalized": decision.min_score,
            "min_score_scale": decision.min_score_scale,
            "min_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_PRICE5M", 40.0), 40.0),
            "max_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_PRICE5M", 100.0), 100.0),
            "force_own_lane": bool(getattr(CFG, "RESEARCH_RANK_CANARY_FORCE_OWN_LANE", True)),
            "shadow_if_not_executable": bool(getattr(CFG, "RESEARCH_RANK_CANARY_SHADOW_IF_NOT_EXECUTABLE", True)),
            "require_route_paper": bool(getattr(CFG, "RESEARCH_RANK_CANARY_REQUIRE_ROUTE_PAPER", True)),
            "priority_mode": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MODE", True)),
            "paper_normal_buy_enabled": bool(
                getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_BUY_ENABLED", True)
            ),
            "pullback_mode": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MODE", True)),
            "stale_high_price5m_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_STALE_HIGH_PRICE5M_ENABLED", True)),
        },
    }
    try:
        with _AUDIT_LOCK:
            payload.update(_read_audit())
            payload["updated_at_utc"] = now
            payload["total_evaluations"] = int(payload.get("total_evaluations") or 0) + 1
            payload["evaluated"] = int(payload.get("evaluated") or 0) + 1
            if decision.allowed:
                payload["allowed"] = int(payload.get("allowed") or 0) + 1
            else:
                payload["rejected"] = int(payload.get("rejected") or 0) + 1
            reasons = payload.get("reasons")
            if not isinstance(reasons, dict):
                reasons = {}
            reasons[reason] = int(reasons.get(reason) or 0) + 1
            payload["reasons"] = reasons
            blocked = payload.get("blocked_by_reason")
            if not isinstance(blocked, dict):
                blocked = {}
            if not decision.allowed:
                blocked[reason] = int(blocked.get(reason) or 0) + 1
            payload["blocked_by_reason"] = blocked
            payload["config"] = {
                "enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_ENABLED", True)),
                "paper_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_ENABLED", True)),
                "live_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_LIVE_ENABLED", False)),
                "min_score_raw": decision.min_score_raw,
                "min_score_normalized": decision.min_score,
                "min_score_scale": decision.min_score_scale,
                "min_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_PRICE5M", 40.0), 40.0),
                "max_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_PRICE5M", 100.0), 100.0),
                "force_own_lane": bool(getattr(CFG, "RESEARCH_RANK_CANARY_FORCE_OWN_LANE", True)),
                "shadow_if_not_executable": bool(
                    getattr(CFG, "RESEARCH_RANK_CANARY_SHADOW_IF_NOT_EXECUTABLE", True)
                ),
                "require_route_paper": bool(getattr(CFG, "RESEARCH_RANK_CANARY_REQUIRE_ROUTE_PAPER", True)),
                "priority_mode": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MODE", True)),
                "paper_normal_buy_enabled": bool(
                    getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_BUY_ENABLED", True)
                ),
                "pullback_mode": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MODE", True)),
                "stale_high_price5m_enabled": bool(
                    getattr(CFG, "RESEARCH_RANK_CANARY_STALE_HIGH_PRICE5M_ENABLED", True)
                ),
            }
            payload["last_decision"] = {
                "ts_utc": now,
                "address": address,
                "allowed": bool(decision.allowed),
                "reason": reason,
                "dry_run": bool(dry_run),
                "live": bool(live),
                "entry_lane": str(token.get("entry_lane") or ""),
                "rank_score_raw": decision.rank_score_raw,
                "rank_score_normalized": decision.rank_score,
                "rank_score_scale": decision.rank_score_scale,
                "min_score_raw": decision.min_score_raw,
                "min_score_normalized": decision.min_score,
                "min_score_scale": decision.min_score_scale,
                "shadow_as_own_lane": bool(decision.shadow_as_own_lane),
                "executable": bool(decision.executable),
                "priority": bool(decision.priority),
                "pullback": bool(decision.pullback),
                "price_pct_5m": _field_float(token, "price_pct_5m", "buy_price_pct_5m", default=0.0),
                "market_cap_usd": _field_float(token, "market_cap_usd", "buy_market_cap_usd", default=0.0),
                "txns_last_5m": _field_float(token, "txns_last_5m", "buy_txns_last_5m", default=0.0),
                "liquidity_usd": _field_float(token, "liquidity_usd", "buy_liquidity_usd", default=0.0),
                "has_jupiter_route": _bool(token.get("has_jupiter_route")),
                "liquidity_is_proxy": _bool(
                    token.get("liquidity_is_proxy")
                    or token.get("liquidity_usd_is_proxy")
                    or token.get("buy_liquidity_is_proxy")
                ),
            }
            AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            AUDIT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        return


def _record_context_application(token: dict[str, Any], *, shadow: bool) -> None:
    if _runtime_side_effects_disabled():
        return
    try:
        with _AUDIT_LOCK:
            payload = _read_audit()
            if shadow:
                payload["shadow_as_own_lane"] = int(payload.get("shadow_as_own_lane") or 0) + 1
            else:
                payload["bought_as_own_lane"] = int(payload.get("bought_as_own_lane") or 0) + 1
            mixed = (
                str(token.get("entry_lane") or "") != LANE_RESEARCH_RANK_CANARY
                or str(token.get("gate_profile") or "") != "research_rank_canary"
                or str(token.get("profit_lane_tier") or "") != LANE_RESEARCH_RANK_CANARY
            )
            if mixed:
                payload["mixed_lane_detected"] = int(payload.get("mixed_lane_detected") or 0) + 1
            payload["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            AUDIT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        return


def _record_event(token: dict[str, Any], decision: ResearchRankCanaryDecision, *, dry_run: bool, live: bool) -> None:
    if _runtime_side_effects_disabled():
        return
    address = str(token.get("address") or token.get("token_address") or "")
    if not address:
        return
    try:
        from utils.runtime_telemetry import record_runtime_event

        record_runtime_event(
            "research_rank_canary_eval",
            address,
            allowed=bool(decision.allowed),
            reason=str(decision.reason),
            dry_run=bool(dry_run),
            live=bool(live),
            entry_lane=str(token.get("entry_lane") or ""),
            rank_score_raw=float(decision.rank_score_raw),
            rank_score_normalized=float(decision.rank_score),
            rank_score_scale=str(decision.rank_score_scale),
            min_score_raw=float(decision.min_score_raw),
            min_score_normalized=float(decision.min_score),
            min_score_scale=str(decision.min_score_scale),
            priority=bool(decision.priority),
            pullback=bool(decision.pullback),
        )
    except Exception:
        return


def evaluate_research_rank_canary(
    token: dict[str, Any],
    rank_info: dict[str, Any] | None,
    *,
    dry_run: bool,
    live: bool,
) -> ResearchRankCanaryDecision:
    min_score_raw, min_score, min_score_scale = normalize_score(
        getattr(CFG, "RESEARCH_RANK_CANARY_MIN_SCORE", 64.81),
        64.81,
    )
    amount = _float(getattr(CFG, "RESEARCH_RANK_CANARY_SIZE_SOL", 0.02), 0.02)
    max_amount = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_SIZE_SOL", 0.03), 0.03)
    if max_amount > 0.0:
        amount = min(amount, max_amount)
    amount = max(0.0, amount)
    rank_raw_value = (
        (rank_info or {}).get("rank_score")
        or (rank_info or {}).get("research_rank_score")
        or token.get("rank_score")
        or token.get("research_rank_score")
    )
    rank_score_raw, rank_score, rank_score_scale = normalize_score(rank_raw_value, 0.0)

    def decision(
        allowed: bool,
        reason: str,
        *,
        shadow_as_own_lane: bool = False,
        executable: bool = True,
        priority: bool = False,
        pullback: bool = False,
        elite_consolidation: bool = False,
        pullback_tail_micro: bool = False,
        amount_sol: float | None = None,
    ) -> ResearchRankCanaryDecision:
        out = ResearchRankCanaryDecision(
            allowed,
            LANE_RESEARCH_RANK_CANARY,
            reason,
            rank_score,
            min_score,
            amount if amount_sol is None else max(0.0, float(amount_sol)),
            rank_score_raw,
            rank_score_scale,
            min_score_raw,
            min_score_scale,
            shadow_as_own_lane,
            executable,
            priority,
            pullback,
            elite_consolidation,
            pullback_tail_micro,
        )
        _record_audit(token, out, dry_run=dry_run, live=live)
        _record_event(token, out, dry_run=dry_run, live=live)
        return out

    def not_executable(reason: str) -> ResearchRankCanaryDecision:
        if bool(getattr(CFG, "RESEARCH_RANK_CANARY_SHADOW_IF_NOT_EXECUTABLE", True)):
            return decision(
                False,
                f"research_rank_canary_not_executable:{reason}",
                shadow_as_own_lane=True,
                executable=False,
            )
        return decision(False, reason, executable=False)

    if not bool(getattr(CFG, "RESEARCH_RANK_CANARY_ENABLED", True)):
        return decision(False, "disabled")
    if normalize_entry_lane(token.get("entry_lane")) != LANE_RESEARCH_SNIPER:
        return decision(False, "not_research_sniper")
    if live and not bool(getattr(CFG, "RESEARCH_RANK_CANARY_LIVE_ENABLED", False)):
        return decision(False, "live_disabled")
    if dry_run and not bool(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_ENABLED", True)):
        return decision(False, "paper_disabled")
    if rank_score < min_score:
        return decision(False, "rank_below_min")
    price5m = _field_float(token, "price_pct_5m", "buy_price_pct_5m", default=0.0)
    min_price5m = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_PRICE5M", 40.0), 40.0)
    max_price5m = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_PRICE5M", 100.0), 100.0)
    liq = _field_float(token, "liquidity_usd", "buy_liquidity_usd", default=0.0)
    mcap = _field_float(token, "market_cap_usd", "buy_market_cap_usd", default=0.0)
    txns = _field_float(token, "txns_last_5m", "buy_txns_last_5m", default=0.0)
    age_minutes = _field_float(token, "age_minutes", "age_min", "token_age_min", default=0.0)
    queue_age_minutes = _field_float(token, "queue_age_minutes", default=0.0)
    volume_24h = _field_float(token, "volume_24h_usd", "buy_volume_24h_usd", "volume_usd_24h", default=0.0)
    proxy = _bool(token.get("liquidity_is_proxy") or token.get("liquidity_usd_is_proxy") or token.get("buy_liquidity_is_proxy"))
    has_route = _bool(token.get("has_jupiter_route"))
    min_mcap = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_MCAP_USD", 20_000.0), 20_000.0)
    max_mcap = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_MCAP_USD", 120_000.0), 120_000.0)
    priority_mode = bool(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MODE", True))
    priority_match = (
        priority_mode
        and rank_score >= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE", 70.0), 70.0)
        and txns >= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MIN_TXNS_5M", 1000), 1000.0)
        and liq >= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MIN_LIQUIDITY_USD", 20_000.0), 20_000.0)
        and _float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MIN_PRICE5M", 50.0), 50.0)
        <= price5m
        <= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MAX_PRICE5M", 120.0), 120.0)
        and min_mcap <= mcap <= max_mcap
        and not proxy
        and has_route
    )
    if priority_match:
        return decision(
            True,
            "research_rank_canary_priority",
            priority=True,
            amount_sol=_float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_SIZE_SOL", amount), amount),
        )
    stale_high_momentum = False
    if bool(getattr(CFG, "RESEARCH_RANK_CANARY_STALE_HIGH_PRICE5M_ENABLED", True)):
        stale_min_price = _float(getattr(CFG, "RESEARCH_RANK_CANARY_STALE_HIGH_PRICE5M_MIN", 50.0), 50.0)
        stale_max_age = _float(getattr(CFG, "RESEARCH_RANK_CANARY_STALE_HIGH_PRICE5M_MAX_AGE_MIN", 20.0), 20.0)
        stale_max_queue_age = _float(
            getattr(CFG, "RESEARCH_RANK_CANARY_STALE_HIGH_PRICE5M_MAX_QUEUE_AGE_MIN", 5.0),
            5.0,
        )
        priority_min_txns = _float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MIN_TXNS_5M", 1000), 1000.0)
        stale_high_momentum = (
            price5m >= stale_min_price
            and age_minutes > stale_max_age
            and queue_age_minutes > stale_max_queue_age
            and txns < priority_min_txns
        )
    paper_normal_min_price = _float(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_PRICE5M", 50.0), 50.0)
    paper_normal_max_price = _float(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_MAX_PRICE5M", 140.0), 140.0)
    paper_normal_stale_bypass_min = _float(
        getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_STALE_BYPASS_MIN_PRICE5M", 100.0),
        100.0,
    )
    paper_normal_stale_ok = (not stale_high_momentum) or price5m >= paper_normal_stale_bypass_min
    paper_normal_match = (
        dry_run
        and bool(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_BUY_ENABLED", True))
        and rank_score >= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_RANK_SCORE", min_score), min_score)
        and txns >= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_TXNS_5M", 100), 100.0)
        and liq >= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_LIQUIDITY_USD", 8_000.0), 8_000.0)
        and _float(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_MCAP_USD", min_mcap), min_mcap)
        <= mcap
        <= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_MAX_MCAP_USD", 250_000.0), 250_000.0)
        and paper_normal_min_price <= price5m <= paper_normal_max_price
        and paper_normal_stale_ok
        and not proxy
        and has_route
    )
    if paper_normal_match:
        paper_amount = min(
            _float(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_SIZE_SOL", 0.005), 0.005),
            _float(getattr(CFG, "PAPER_EXPLORATION_AMOUNT_SOL", 0.005), 0.005),
            max_amount if max_amount > 0.0 else 0.005,
        )
        return decision(True, "research_rank_canary_paper_normal", amount_sol=paper_amount)
    if bool(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_ONLY", True)):
        return decision(
            False,
            "shadow_rank_canary",
            shadow_as_own_lane=True,
            executable=False,
        )
    elite_mode = bool(getattr(CFG, "RESEARCH_RANK_CANARY_ELITE_CONSOLIDATION_MODE", True))
    elite_match = (
        elite_mode
        and rank_score
        >= _float(getattr(CFG, "RESEARCH_RANK_CANARY_ELITE_CONSOLIDATION_MIN_RANK_SCORE", 75.0), 75.0)
        and txns >= _float(getattr(CFG, "RESEARCH_RANK_CANARY_ELITE_CONSOLIDATION_MIN_TXNS_5M", 300), 300.0)
        and liq
        >= _float(getattr(CFG, "RESEARCH_RANK_CANARY_ELITE_CONSOLIDATION_MIN_LIQUIDITY_USD", 20_000.0), 20_000.0)
        and _float(getattr(CFG, "RESEARCH_RANK_CANARY_ELITE_CONSOLIDATION_MIN_PRICE5M", 0.0), 0.0)
        <= price5m
        <= _float(getattr(CFG, "RESEARCH_RANK_CANARY_ELITE_CONSOLIDATION_MAX_PRICE5M", 25.0), 25.0)
        and min_mcap
        <= mcap
        <= _float(getattr(CFG, "RESEARCH_RANK_CANARY_ELITE_CONSOLIDATION_MAX_MCAP_USD", 250_000.0), 250_000.0)
        and not proxy
        and has_route
    )
    if elite_match:
        return decision(True, "research_rank_canary_elite_consolidation", elite_consolidation=True)
    pullback_mode = bool(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MODE", True))
    pullback_min_price = _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MIN_PRICE5M", -10.0), -10.0)
    pullback_max_price = _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MAX_PRICE5M", 30.0), 30.0)
    pullback_min_rank = _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MIN_RANK_SCORE", 70.0), 70.0)
    pullback_alt_min_rank = _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_ALT_MIN_RANK_SCORE", 65.0), 65.0)
    pullback_min_txns = _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MIN_TXNS_5M", 300), 300.0)
    pullback_alt_min_txns = _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_ALT_MIN_TXNS_5M", 900), 900.0)
    pullback_min_liq = _float(
        getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MIN_LIQUIDITY_USD", 15_000.0),
        15_000.0,
    )
    pullback_max_mcap = _float(
        getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MAX_MCAP_USD", 350_000.0),
        350_000.0,
    )
    pullback_strength_ok = (rank_score >= pullback_min_rank and txns >= pullback_min_txns) or (
        rank_score >= pullback_alt_min_rank and txns >= pullback_alt_min_txns
    )
    pullback_match = (
        pullback_mode
        and pullback_min_price <= price5m <= pullback_max_price
        and pullback_strength_ok
        and liq >= pullback_min_liq
        and 0.0 < mcap <= pullback_max_mcap
        and not proxy
        and has_route
    )
    pullback_tail_amount = min(
        _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_TAIL_AMOUNT_SOL", 0.005), 0.005),
        _float(getattr(CFG, "PAPER_EXPLORATION_AMOUNT_SOL", 0.005), 0.005),
        _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_SIZE_SOL", 0.02), 0.02),
    )
    pullback_tail_match = (
        bool(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_TAIL_MICRO_MODE", True))
        and _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_TAIL_MIN_PRICE5M", -12.0), -12.0)
        <= price5m
        <= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_TAIL_MAX_PRICE5M", 0.0), 0.0)
        and rank_score >= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_TAIL_MIN_RANK_SCORE", 70.0), 70.0)
        and txns >= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_TAIL_MIN_TXNS_5M", 600), 600.0)
        and liq >= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_TAIL_MIN_LIQUIDITY_USD", 30_000.0), 30_000.0)
        and _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_TAIL_MIN_MCAP_USD", 150_000.0), 150_000.0)
        <= mcap
        <= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_TAIL_MAX_MCAP_USD", 250_000.0), 250_000.0)
        and volume_24h
        >= _float(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_TAIL_MIN_VOLUME_24H", 400_000.0), 400_000.0)
        and not proxy
        and has_route
    )
    if pullback_tail_match:
        return decision(
            True,
            "research_rank_canary_pullback_tail_micro",
            pullback=True,
            pullback_tail_micro=True,
            amount_sol=pullback_tail_amount,
        )
    if pullback_match:
        if bool(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_BUY_ENABLED", False)):
            return decision(True, "research_rank_canary_pullback", pullback=True)
        return decision(
            False,
            "research_rank_canary_pullback_shadow_only",
            shadow_as_own_lane=True,
            executable=False,
            pullback=True,
        )
    if stale_high_momentum:
        return decision(
            False,
            "research_rank_canary_stale_high_momentum",
            shadow_as_own_lane=True,
            executable=False,
        )
    if price5m < min_price5m:
        return decision(False, "price5m_below_min", shadow_as_own_lane=True, executable=False)
    if price5m > max_price5m:
        return decision(False, "price5m_out_of_band")
    if 40.0 <= price5m < 50.0:
        low_band_min_rank = _float(getattr(CFG, "RESEARCH_RANK_CANARY_LOW_BAND_MIN_RANK_SCORE", 70.0), 70.0)
        low_band_min_liq = _float(
            getattr(CFG, "RESEARCH_RANK_CANARY_LOW_BAND_MIN_LIQUIDITY_USD", 20_000.0),
            20_000.0,
        )
        if rank_score < low_band_min_rank and liq < low_band_min_liq:
            return decision(False, "price5m_40_50_requires_rank70_or_liq20k", shadow_as_own_lane=True, executable=False)
    if mcap < min_mcap or mcap > max_mcap:
        return decision(False, "mcap_out_of_band")
    min_txns = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_TXNS_5M", 300), 300.0)
    if txns < min_txns:
        return decision(False, "txns_below_min")
    min_liq = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_LIQUIDITY_USD", 2000.0), 2000.0)
    if liq < min_liq:
        return not_executable("liquidity_below_min")
    if proxy and bool(getattr(CFG, "RESEARCH_RANK_CANARY_PREFER_REAL_LIQUIDITY", True)):
        return decision(False, "proxy_liquidity")
    if dry_run and bool(getattr(CFG, "RESEARCH_RANK_CANARY_REQUIRE_ROUTE_PAPER", True)) and not has_route:
        return not_executable("no_route_paper")
    if live and bool(getattr(CFG, "RESEARCH_RANK_CANARY_REQUIRE_ROUTE_LIVE", True)) and not has_route:
        return not_executable("no_route_live")
    if not bool(getattr(CFG, "RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED", False)):
        return decision(
            False,
            "research_rank_canary_normal_shadow_only",
            shadow_as_own_lane=True,
            executable=False,
        )
    return decision(True, "research_rank_canary")


def apply_research_rank_canary_context(
    token: dict[str, Any],
    decision: ResearchRankCanaryDecision,
    *,
    record_audit: bool = True,
) -> dict[str, Any]:
    token["entry_lane"] = decision.entry_lane
    token["gate_profile"] = "research_rank_canary"
    token["profit_lane_tier"] = decision.entry_lane
    token["lane_policy_category"] = POLICY_RESEARCH_RANK_CANARY
    token["research_rank_canary_rank_score"] = decision.rank_score
    token["research_rank_canary_rank_score_raw"] = decision.rank_score_raw
    token["research_rank_canary_rank_score_scale"] = decision.rank_score_scale
    token["research_rank_canary_min_score"] = decision.min_score
    token["research_rank_canary_min_score_raw"] = decision.min_score_raw
    token["research_rank_canary_min_score_scale"] = decision.min_score_scale
    token["research_rank_canary_amount_sol"] = decision.amount_sol
    token["research_rank_canary_priority"] = int(bool(decision.priority))
    token["research_rank_canary_pullback"] = int(bool(decision.pullback))
    token["research_rank_canary_elite_consolidation"] = int(bool(decision.elite_consolidation))
    token["research_rank_canary_pullback_tail_micro"] = int(bool(decision.pullback_tail_micro))
    token["green_sniper_reason"] = decision.reason
    token["live_profit_gate_failed_count"] = 0
    token["live_profit_gate_failures"] = ""
    token["live_profit_gate_profile"] = "research_rank_canary"
    token["sniper_gate_profile"] = "research_rank_canary"
    if record_audit:
        _record_context_application(token, shadow=False)
    return token


def apply_research_rank_canary_shadow_context(token: dict[str, Any], decision: ResearchRankCanaryDecision) -> dict[str, Any]:
    apply_research_rank_canary_context(token, decision, record_audit=False)
    token["green_sniper_reason"] = decision.reason
    token["research_rank_canary_shadow"] = 1
    token["research_rank_canary_not_executable"] = int(not decision.executable)
    _record_context_application(token, shadow=True)
    return token


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _pnl(row: dict[str, Any]) -> float:
    return fnum(_first(row, "realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct"), 0.0)


def _is_rank_canary_row(row: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(_first(row, key) or "")
        for key in (
            "entry_lane",
            "gate_profile",
            "sniper_gate_profile",
            "profit_lane_tier",
            "lane_policy_category",
            "reason",
            "green_sniper_reason",
        )
    ).lower()
    return "research_rank_canary" in haystack


def _price5m(row: dict[str, Any]) -> float:
    return fnum(_first(row, "buy_price_pct_5m", "price_pct_5m", "price5m"), 0.0)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_pnl(row) for row in rows]
    if not rows:
        return {
            "rows": 0,
            "win_rate_pct": 0.0,
            "avg_pnl_pct": 0.0,
            "median_pnl_pct": 0.0,
            "total_pnl_pct_points": 0.0,
            "severe_loss_count": 0,
        }
    return {
        "rows": len(rows),
        "win_rate_pct": round(100.0 * sum(1 for value in pnls if value > 0.0) / len(pnls), 3),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        "median_pnl_pct": round(statistics.median(pnls), 3),
        "total_pnl_pct_points": round(sum(pnls), 3),
        "severe_loss_count": sum(1 for row, pnl in zip(rows, pnls) if is_severe_exit(row) or pnl <= -25.0),
    }


def _runtime_audit_from_events(root: Path) -> dict[str, Any]:
    try:
        events = [
            row
            for row in load_runtime_events(root)
            if str(_first(row, "event_type") or "").strip().lower() == "research_rank_canary_eval"
        ]
    except Exception:
        events = []
    if not events:
        return {}
    reasons: dict[str, int] = {}
    blocked: dict[str, int] = {}
    allowed_count = 0
    for row in events:
        reason = str(_first(row, "reason") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
        allowed = _bool(_first(row, "allowed"))
        if allowed:
            allowed_count += 1
        else:
            blocked[reason] = blocked.get(reason, 0) + 1
    last = events[-1]
    return {
        "updated_at_utc": str(_first(last, "ts_utc", "timestamp") or datetime.now(timezone.utc).isoformat()),
        "total_evaluations": len(events),
        "evaluated": len(events),
        "allowed": allowed_count,
        "rejected": len(events) - allowed_count,
        "reasons": reasons,
        "blocked_by_reason": blocked,
        "last_decision": last,
        "source": "runtime_events",
    }


def build_research_rank_canary_audit_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    runtime_audit = _runtime_audit_from_events(root)
    if not runtime_audit and root == PROJECT_ROOT:
        runtime_audit = _read_audit()
    if isinstance(runtime_audit.get("runtime_audit"), dict):
        runtime_audit = runtime_audit["runtime_audit"]
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    rank_rows = [row for row in rows if _is_rank_canary_row(row)]
    band_25_40 = [row for row in rank_rows if 25.0 <= _price5m(row) < 40.0]
    band_40_50 = [row for row in rank_rows if 40.0 <= _price5m(row) < 50.0]
    band_50_100 = [row for row in rank_rows if 50.0 <= _price5m(row) <= 100.0]
    own_lane = [
        row
        for row in rank_rows
        if normalize_entry_lane(_first(row, "entry_lane")) == LANE_RESEARCH_RANK_CANARY
        and str(_first(row, "gate_profile", "sniper_gate_profile") or "").strip().lower() == "research_rank_canary"
    ]
    mixed_lane = [
        row
        for row in rank_rows
        if row not in own_lane
        or str(_first(row, "profit_lane_tier") or "").strip().lower() == "pump_early_pumpswap_prime"
        or str(_first(row, "gate_profile", "sniper_gate_profile") or "").strip().lower() == "pumpswap_profit_prime"
    ]
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "evaluated": int(runtime_audit.get("evaluated") or runtime_audit.get("total_evaluations") or 0),
        "allowed": int(runtime_audit.get("allowed") or 0),
        "rejected": int(runtime_audit.get("rejected") or 0),
        "bought_as_own_lane": int(runtime_audit.get("bought_as_own_lane") or 0),
        "shadow_as_own_lane": int(runtime_audit.get("shadow_as_own_lane") or 0),
        "mixed_lane_detected": int(runtime_audit.get("mixed_lane_detected") or 0) + len(mixed_lane),
        "blocked_by_reason": runtime_audit.get("blocked_by_reason") if isinstance(runtime_audit.get("blocked_by_reason"), dict) else {},
        "runtime_audit": runtime_audit,
        "config": {
            "enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_LIVE_ENABLED", False)),
            "min_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_PRICE5M", 40.0), 40.0),
            "max_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_PRICE5M", 100.0), 100.0),
            "low_band_min_rank_score": _float(getattr(CFG, "RESEARCH_RANK_CANARY_LOW_BAND_MIN_RANK_SCORE", 70.0), 70.0),
            "low_band_min_liquidity_usd": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_LOW_BAND_MIN_LIQUIDITY_USD", 20_000.0),
                20_000.0,
            ),
            "force_own_lane": bool(getattr(CFG, "RESEARCH_RANK_CANARY_FORCE_OWN_LANE", True)),
            "pullback_mode": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MODE", True)),
            "pullback_min_price5m": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MIN_PRICE5M", -10.0),
                -10.0,
            ),
            "pullback_max_price5m": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MAX_PRICE5M", 30.0),
                30.0,
            ),
            "stale_high_price5m_enabled": bool(
                getattr(CFG, "RESEARCH_RANK_CANARY_STALE_HIGH_PRICE5M_ENABLED", True)
            ),
        },
        "price5m_band_comparison": {
            "price5m_25_40_shadow_candidate": _summary(band_25_40),
            "price5m_40_50_conditional": _summary(band_40_50),
            "price5m_50_100_executable": _summary(band_50_100),
        },
        "lane_mix_audit": {
            "rank_canary_rows": len(rank_rows),
            "own_lane_rows": len(own_lane),
            "mixed_lane_rows": len(mixed_lane),
            "pumpswap_profit_prime_leak_rows": sum(
                1
                for row in rank_rows
                if str(_first(row, "profit_lane_tier") or "").strip().lower() == "pump_early_pumpswap_prime"
                or str(_first(row, "gate_profile", "sniper_gate_profile") or "").strip().lower() == "pumpswap_profit_prime"
            ),
        },
    }
    return report


def write_research_rank_canary_audit_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_research_rank_canary_audit_report(root)
    write_json(metrics_dir(root) / "research_rank_canary_audit.json", report)
    return report


def _is_priority_row(row: dict[str, Any]) -> bool:
    return _bool(row.get("research_rank_canary_priority")) or "research_rank_canary_priority" in str(
        _first(row, "reason", "green_sniper_reason", "entry_reason") or ""
    ).lower()


def _is_pullback_row(row: dict[str, Any]) -> bool:
    return _bool(row.get("research_rank_canary_pullback")) or "research_rank_canary_pullback" in str(
        _first(row, "reason", "green_sniper_reason", "entry_reason") or ""
    ).lower()


def _is_elite_row(row: dict[str, Any]) -> bool:
    return _bool(row.get("research_rank_canary_elite_consolidation")) or "research_rank_canary_elite_consolidation" in str(
        _first(row, "reason", "green_sniper_reason", "entry_reason") or ""
    ).lower()


def _is_pullback_tail_row(row: dict[str, Any]) -> bool:
    return _bool(row.get("research_rank_canary_pullback_tail_micro")) or "research_rank_canary_pullback_tail_micro" in str(
        _first(row, "reason", "green_sniper_reason", "entry_reason") or ""
    ).lower()


def build_research_rank_priority_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    rank_rows = [row for row in rows if _is_rank_canary_row(row)]
    priority_rows = [row for row in rank_rows if _is_priority_row(row)]
    elite_rows = [row for row in rank_rows if _is_elite_row(row)]
    pullback_tail_rows = [row for row in rank_rows if _is_pullback_tail_row(row)]
    pullback_rows = [row for row in rank_rows if _is_pullback_row(row)]
    normal_rows = [
        row
        for row in rank_rows
        if not _is_priority_row(row)
        and not _is_elite_row(row)
        and not _is_pullback_tail_row(row)
        and not _is_pullback_row(row)
    ]
    runtime_audit = _runtime_audit_from_events(root)
    if not runtime_audit and root == PROJECT_ROOT:
        runtime_audit = _read_audit()
    if isinstance(runtime_audit.get("runtime_audit"), dict):
        runtime_audit = runtime_audit["runtime_audit"]
    reasons = runtime_audit.get("reasons") if isinstance(runtime_audit.get("reasons"), dict) else {}
    blockers = runtime_audit.get("blocked_by_reason") if isinstance(runtime_audit.get("blocked_by_reason"), dict) else {}
    bought = [
        row
        for row in priority_rows
        if str(_first(row, "action", "decision_action", "event_type") or "").strip().lower() in {"buy", "bought", "paper_buy", "trade_close", ""}
        and normalize_entry_lane(_first(row, "entry_lane")) == LANE_RESEARCH_RANK_CANARY
    ]
    shadow = [
        row
        for row in priority_rows
        if "shadow" in str(_first(row, "action", "decision_action", "sample_type", "reason") or "").lower()
    ]
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "config": {
            "priority_mode": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MODE", True)),
            "min_rank_score": _float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE", 70.0), 70.0),
            "min_txns_5m": int(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MIN_TXNS_5M", 1000) or 1000),
            "min_liquidity_usd": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MIN_LIQUIDITY_USD", 15_000.0),
                15_000.0,
            ),
            "min_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MIN_PRICE5M", 50.0), 50.0),
            "max_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MAX_PRICE5M", 120.0), 120.0),
            "max_open": int(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MAX_OPEN", 2) or 2),
            "route_required": bool(getattr(CFG, "RESEARCH_RANK_CANARY_REQUIRE_ROUTE_PAPER", True)),
            "proxy_liquidity_allowed": False,
            "paper_normal_buy_enabled": bool(
                getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_BUY_ENABLED", True)
            ),
            "paper_normal_size_sol": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_SIZE_SOL", 0.005),
                0.005,
            ),
            "paper_normal_min_price5m": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_PRICE5M", 50.0),
                50.0,
            ),
            "paper_normal_max_price5m": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_MAX_PRICE5M", 140.0),
                140.0,
            ),
            "pullback_mode": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MODE", True)),
            "pullback_min_price5m": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MIN_PRICE5M", -10.0),
                -10.0,
            ),
            "pullback_max_price5m": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MAX_PRICE5M", 30.0),
                30.0,
            ),
            "pullback_min_rank_score": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_MIN_RANK_SCORE", 70.0),
                70.0,
            ),
            "pullback_alt_min_txns_5m": int(
                getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_ALT_MIN_TXNS_5M", 900) or 900
            ),
            "stale_high_price5m_enabled": bool(
                getattr(CFG, "RESEARCH_RANK_CANARY_STALE_HIGH_PRICE5M_ENABLED", True)
            ),
            "normal_buy_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED", False)),
            "elite_consolidation_mode": bool(
                getattr(CFG, "RESEARCH_RANK_CANARY_ELITE_CONSOLIDATION_MODE", True)
            ),
            "pullback_buy_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_BUY_ENABLED", False)),
            "pullback_tail_micro_mode": bool(
                getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_TAIL_MICRO_MODE", True)
            ),
            "size_sol": _float(getattr(CFG, "RESEARCH_RANK_CANARY_SIZE_SOL", 0.01), 0.01),
            "max_size_sol": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_SIZE_SOL", 0.02), 0.02),
            "pullback_tail_amount_sol": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_PULLBACK_TAIL_AMOUNT_SOL", 0.005),
                0.005,
            ),
        },
        "priority_seen": int(reasons.get("research_rank_canary_priority") or len(priority_rows)),
        "priority_bought": len(bought),
        "priority_shadow": len(shadow),
        "elite_seen": int(reasons.get("research_rank_canary_elite_consolidation") or len(elite_rows)),
        "pullback_tail_seen": int(reasons.get("research_rank_canary_pullback_tail_micro") or len(pullback_tail_rows)),
        "pullback_seen": int(reasons.get("research_rank_canary_pullback") or len(pullback_rows)),
        "blockers": blockers,
        "historical": {
            "priority": _summary(priority_rows),
            "elite_consolidation": _summary(elite_rows),
            "pullback_tail_micro": _summary(pullback_tail_rows),
            "pullback": _summary(pullback_rows),
            "normal": _summary(normal_rows),
        },
        "samples": [
            {
                "address": _first(row, "address", "mint", "token_address"),
                "pnl_pct": _pnl(row),
                "price5m": _price5m(row),
                "rank_score": _first(row, "research_rank_canary_rank_score", "rank_score", "research_rank_score"),
                "txns5m": _first(row, "buy_txns_last_5m", "txns_last_5m"),
                "liquidity_usd": _first(row, "buy_liquidity_usd", "liquidity_usd"),
            }
            for row in priority_rows[:50]
        ],
    }


def write_research_rank_priority_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_research_rank_priority_report(root)
    write_json(metrics_dir(root) / "research_rank_priority_report.json", report)
    return report


def build_research_rank_current_run_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    runtime_rows = load_runtime_events(root)
    identity = current_run_identity(root, runtime_rows)
    rows = filter_current_run_rows(
        runtime_rows + load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root),
        identity,
    )
    rank_rows = [row for row in rows if _is_rank_canary_row(row)]
    priority_rows = [row for row in rank_rows if _is_priority_row(row)]
    normal_shadow_rows = [
        row
        for row in rank_rows
        if not _is_priority_row(row)
        and (
            "shadow_rank_canary" in str(_first(row, "reason", "green_sniper_reason", "action", "decision_action") or "").lower()
            or "shadow" in str(_first(row, "sample_type", "action", "decision_action") or "").lower()
        )
    ]
    closed_trades = [
        row
        for row in rank_rows
        if _first(row, "total_pnl_pct", "realized_pnl_pct", "pnl_pct", "closed_at", "exit_reason") is not None
    ]
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "current_run": identity,
        "current_run_rank_trades": _summary(closed_trades),
        "priority_candidates": len(priority_rows),
        "normal_shadows": len(normal_shadow_rows),
        "avg_pnl": _summary(closed_trades)["avg_pnl_pct"],
        "severe_losses": _summary(closed_trades)["severe_loss_count"],
        "config": {
            "priority_only": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_ONLY", True)),
            "size_sol": _float(getattr(CFG, "RESEARCH_RANK_CANARY_SIZE_SOL", 0.02), 0.02),
            "priority_size_sol": _float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_SIZE_SOL", 0.02), 0.02),
            "max_size_sol": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_SIZE_SOL", 0.03), 0.03),
            "max_open": int(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_OPEN", 1) or 1),
            "paper_normal_buy_enabled": bool(
                getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_BUY_ENABLED", True)
            ),
            "paper_normal_size_sol": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_NORMAL_SIZE_SOL", 0.005),
                0.005,
            ),
            "priority_min_liquidity_usd": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_MIN_LIQUIDITY_USD", 20_000.0),
                20_000.0,
            ),
            "priority_mcap_band": [20_000.0, 120_000.0],
        },
        "samples": [
            {
                "address": _first(row, "address", "mint", "token_address"),
                "reason": _first(row, "reason", "green_sniper_reason", "entry_reason"),
                "pnl_pct": _pnl(row),
                "priority": _is_priority_row(row),
                "rank_score": _first(row, "research_rank_canary_rank_score", "rank_score", "research_rank_score"),
                "txns5m": _first(row, "buy_txns_last_5m", "txns_last_5m"),
                "liquidity_usd": _first(row, "buy_liquidity_usd", "liquidity_usd"),
                "mcap_usd": _first(row, "buy_market_cap_usd", "market_cap_usd"),
            }
            for row in rank_rows[:50]
        ],
    }


def write_research_rank_current_run_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_research_rank_current_run_report(root)
    write_json(metrics_dir(root) / "research_rank_current_run_report.json", report)
    return report


__all__ = [
    "AUDIT_PATH",
    "ResearchRankCanaryDecision",
    "apply_research_rank_canary_context",
    "apply_research_rank_canary_shadow_context",
    "build_research_rank_canary_audit_report",
    "build_research_rank_current_run_report",
    "build_research_rank_priority_report",
    "evaluate_research_rank_canary",
    "normalize_score",
    "write_research_rank_canary_audit_report",
    "write_research_rank_current_run_report",
    "write_research_rank_priority_report",
]
