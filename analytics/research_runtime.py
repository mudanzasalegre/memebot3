from __future__ import annotations

from datetime import date, datetime, timezone
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from analytics.audit import normalize_candidate_outcomes_frame, write_normalized_candidate_outcomes
from config.config import CFG, PROJECT_ROOT
from utils.time import utc_now


log = logging.getLogger("research_runtime")

METRICS_DIR = PROJECT_ROOT / "data" / "metrics"
RESEARCH_EVENTS_PATH = METRICS_DIR / "candidate_outcomes.jsonl"
RESEARCH_EVENTS_NORMALIZED_PATH = METRICS_DIR / "candidate_outcomes.normalized.jsonl"
RESEARCH_SCORECARD_JSON = METRICS_DIR / "research_scorecard.json"
RESEARCH_SCORECARD_MD = METRICS_DIR / "research_scorecard.md"
RESEARCH_THRESHOLDS_JSON = METRICS_DIR / "research_thresholds.json"
RESEARCH_PORTFOLIO_PATH = PROJECT_ROOT / "data" / "research_portfolio.json"

_LOCK = threading.Lock()
_SEEN: dict[str, float] = {}
_OPEN_SHADOWS: dict[str, dict[str, Any]] = {}
_LIVE_CONTEXT: dict[str, dict[str, Any]] = {}
_LAST_SCORECARD_BUILD_MONO = 0.0


def _parse_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.floating, float)):
        val = float(value)
        if not np.isfinite(val):
            return None
        return val
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, pd.Timestamp):
        if value.tzinfo is None:
            value = value.tz_localize("UTC")
        return value.isoformat()
    return value


def _normalize_regime(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"pump_early", "pumpfun", "pump", "pump_fun"}:
        return "pump_early"
    if raw in {"revival", "revive", "revived"}:
        return "revival"
    return "dex_mature"


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        val = float(value)
        if not np.isfinite(val):
            return default
        return val
    except Exception:
        return default


def _to_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _event_dedup(key: str, ttl_s: int) -> bool:
    if ttl_s <= 0:
        return False
    now = time.monotonic()
    last = _SEEN.get(key, 0.0)
    if (now - last) < ttl_s:
        return True
    _SEEN[key] = now
    return False


def _write_event(event_type: str, address: str, **payload: Any) -> None:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "ts_utc": utc_now().isoformat(),
        "event_type": str(event_type),
        "address": str(address),
    }
    row.update({str(k): _json_safe(v) for k, v in payload.items()})
    line = json.dumps(row, ensure_ascii=True)
    with _LOCK:
        with RESEARCH_EVENTS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _load_portfolio() -> dict[str, Any]:
    try:
        return json.loads(RESEARCH_PORTFOLIO_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_portfolio(payload: dict[str, Any]) -> None:
    RESEARCH_PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESEARCH_PORTFOLIO_PATH.write_text(
        json.dumps(_json_safe(payload), indent=2),
        encoding="utf-8",
    )


def _shadow_counts_by_regime() -> dict[str, int]:
    counts = {"pump_early": 0, "dex_mature": 0, "revival": 0}
    for data in _OPEN_SHADOWS.values():
        regime = _normalize_regime(data.get("regime"))
        counts[regime] = counts.get(regime, 0) + 1
    return counts


def _common_payload(
    token: dict[str, Any],
    *,
    proba: float | None = None,
    threshold: float | None = None,
    rank_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    regime = _normalize_regime(token.get("entry_regime") or token.get("discovered_via"))
    price_pct_5m = _to_float(token.get("price_pct_5m"))
    mcap = _to_float(token.get("market_cap_usd"))

    def mcap_bucket(value: float | None) -> str:
        if value is None or value <= 0:
            return "missing"
        if value < 25_000:
            return "<25k"
        if value < 50_000:
            return "25k_50k"
        if value < 100_000:
            return "50k_100k"
        if value < 200_000:
            return "100k_200k"
        return ">=200k"

    def price5m_bucket(value: float | None) -> str:
        if value is None:
            return "missing"
        if value < 0:
            return "<0"
        if value < 25:
            return "0_25"
        if value < 50:
            return "25_50"
        if value < 100:
            return "50_100"
        if value < 180:
            return "100_180"
        return ">=180"

    out = {
        "regime": regime,
        "discovered_via": str(token.get("discovered_via") or "dex"),
        "symbol": str(token.get("symbol") or ""),
        "dex_id": token.get("dex_id") or token.get("dexId"),
        "price_source": token.get("price_source"),
        "ml_proba": _to_float(proba),
        "threshold": _to_float(threshold),
        "score_total": _to_int(token.get("score_total")),
        "age_minutes": _to_float(token.get("age_minutes") or token.get("age_min")),
        "queue_attempts": _to_int(token.get("queue_attempts")),
        "queue_age_minutes": _to_float(token.get("queue_age_minutes")),
        "snapshot_missing_fields": _to_int(token.get("snapshot_missing_fields")),
        "coverage_core_fields": _to_int(token.get("coverage_core_fields")),
        "liquidity_usd": _to_float(token.get("liquidity_usd")),
        "volume_24h_usd": _to_float(token.get("volume_24h_usd")),
        "market_cap_usd": mcap,
        "holders": _to_int(token.get("holders")),
        "has_jupiter_route": _to_int(token.get("has_jupiter_route")),
        "price_impact_pct": _to_float(token.get("price_impact_pct")),
        "price_pct_5m": price_pct_5m,
        "txns_last_5m": _to_int(token.get("txns_last_5m")),
        "require_jupiter_for_buy": _to_int(token.get("require_jupiter_for_buy")),
        "entry_lane": token.get("entry_lane"),
        "gate_profile": token.get("gate_profile") or token.get("sniper_gate_profile"),
        "profit_lane_tier": token.get("profit_lane_tier") or token.get("size_bucket"),
        "sniper_gate_profile": token.get("sniper_gate_profile"),
        "sniper_gate_failures": token.get("sniper_gate_failures") or token.get("live_profit_gate_failures"),
        "live_profit_gate_profile": token.get("live_profit_gate_profile"),
        "liquidity_is_proxy": _to_int(token.get("liquidity_is_proxy") or token.get("liquidity_usd_is_proxy")),
        "mcap_bucket": token.get("mcap_bucket") or mcap_bucket(mcap),
        "price5m_bucket": token.get("price5m_bucket") or price5m_bucket(price_pct_5m),
        "impact_zero_flag": _to_int(token.get("impact_zero_flag")),
        "venue_is_pumpswap": _to_int(token.get("venue_is_pumpswap")),
        "profit_gate_reject_reasons": token.get("profit_gate_reject_reasons"),
        "runner_exit_profile": token.get("runner_exit_profile"),
        "blocked_bucket": token.get("blocked_bucket"),
        "paper_cold_start_shadow_probe": bool(token.get("paper_cold_start_shadow_probe")),
        "paper_cold_start_shadow_probe_reason": token.get("paper_cold_start_shadow_probe_reason"),
    }
    if rank_info:
        out["rank_score"] = _to_float(rank_info.get("rank_score"))
        for key, value in (rank_info.get("components") or {}).items():
            # Keep rank_score as the total score; the "score" component used to
            # overwrite it via rank_score and corrupt downstream thresholding.
            component_key = f"rank_{key}"
            if component_key == "rank_score":
                component_key = "rank_score_component"
            out[component_key] = _to_float(value)
    return out


def score_candidate(
    token: dict[str, Any],
    *,
    proba: float | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    proba_f = max(0.0, min(1.0, float(_to_float(proba, 0.0) or 0.0)))
    threshold_f = _to_float(threshold)
    score_total = max(0.0, min(100.0, float(_to_float(token.get("score_total"), 0.0) or 0.0)))
    age = max(0.0, float(_to_float(token.get("age_minutes") or token.get("age_min"), 0.0) or 0.0))
    liq = max(0.0, float(_to_float(token.get("liquidity_usd"), 0.0) or 0.0))
    vol = max(0.0, float(_to_float(token.get("volume_24h_usd"), 0.0) or 0.0))
    mcap = max(0.0, float(_to_float(token.get("market_cap_usd"), 0.0) or 0.0))
    holders = max(0.0, float(_to_float(token.get("holders"), 0.0) or 0.0))
    coverage = max(0.0, min(7.0, float(_to_float(token.get("coverage_core_fields"), 0.0) or 0.0)))
    missing_fields = max(0.0, float(_to_int(token.get("snapshot_missing_fields"), 0) or 0))
    has_route = bool(_to_int(token.get("has_jupiter_route"), 0) or 0)
    txns_5m = max(0.0, float(_to_int(token.get("txns_last_5m"), 0) or 0))
    price_pct_5m = _to_float(token.get("price_pct_5m"))

    if threshold_f is None:
        proba_edge = proba_f - 0.50
    else:
        proba_edge = proba_f - float(threshold_f)

    ml_component = max(0.0, min(1.0, (proba_edge + 0.25) / 0.50)) * 15.0
    score_component = (score_total / 100.0) * 20.0
    liq_component = min(liq / 20_000.0, 1.0) * 10.0
    vol_component = min(vol / 100_000.0, 1.0) * 10.0
    age_component = min(age / 20.0, 1.0) * 5.0
    holders_component = 0.0
    route_component = 10.0 if has_route else 0.0
    coverage_component = (coverage / 7.0) * 5.0
    missing_component = max(0.0, 1.0 - min(missing_fields, 7.0) / 7.0) * 5.0
    txns_component = min(txns_5m / 100.0, 1.0) * 10.0
    if price_pct_5m is None:
        momentum_component = 0.0
    elif price_pct_5m < -12.0:
        momentum_component = 0.0
    elif price_pct_5m <= 0.0:
        momentum_component = 2.0
    elif price_pct_5m <= 120.0:
        momentum_component = min(price_pct_5m / 120.0, 1.0) * 10.0
    else:
        momentum_component = max(0.0, 10.0 - min((price_pct_5m - 120.0) / 60.0, 1.0) * 5.0)

    rank_score = max(
        0.0,
        min(
            100.0,
            ml_component
            + score_component
            + liq_component
            + vol_component
            + age_component
            + holders_component
            + route_component
            + coverage_component
            + missing_component
            + txns_component
            + momentum_component,
        ),
    )

    return {
        "rank_score": float(rank_score),
        "components": {
            "ml": float(ml_component),
            "score": float(score_component),
            "liq": float(liq_component),
            "vol": float(vol_component),
            "age": float(age_component),
            "holders": float(holders_component),
            "route": float(route_component),
            "coverage": float(coverage_component),
            "missing": float(missing_component),
            "txns": float(txns_component),
            "momentum": float(momentum_component),
        },
    }


def load_live_rank_gate(regime: str, *, now: datetime | None = None) -> dict[str, Any]:
    resolved_regime = _normalize_regime(regime)
    now = now or utc_now()
    fallback = float(getattr(CFG, "LIVE_RANK_SCORE_FALLBACK_MIN", 12.5) or 12.5)
    min_selected_rows = max(1, int(getattr(CFG, "LIVE_RANK_SCORE_MIN_SELECTED_ROWS", 20) or 20))
    min_avg_pnl_pct = float(getattr(CFG, "LIVE_RANK_SCORE_MIN_AVG_PNL_PCT", 3.0) or 3.0)
    max_age_min = max(0.0, float(getattr(CFG, "STRATEGY_SCORECARD_MAX_AGE_MIN", 240.0) or 240.0))
    payload = _read_json_file(RESEARCH_THRESHOLDS_JSON)
    generated_at = _parse_utc((payload or {}).get("generated_at_utc"))
    stale = generated_at is None
    if generated_at is not None and max_age_min > 0:
        stale = ((now - generated_at).total_seconds() / 60.0) > max_age_min

    regimes = (payload or {}).get("regimes") if isinstance(payload, dict) else None
    regime_payload = regimes.get(resolved_regime) if isinstance(regimes, dict) else None
    rank_payload = regime_payload.get("rank_score") if isinstance(regime_payload, dict) else None
    activation_ready = bool(rank_payload.get("activation_ready")) if isinstance(rank_payload, dict) else False
    threshold = _to_float((rank_payload or {}).get("picked_rank_score")) if isinstance(rank_payload, dict) else None
    if threshold is None and isinstance(rank_payload, dict):
        picked = _to_float(rank_payload.get("picked"))
        if picked is not None:
            threshold = float(picked) * 100.0

    selected_rows = _to_int(rank_payload.get("selected_rows_at_picked")) if isinstance(rank_payload, dict) else None
    avg_realized = (
        _to_float(rank_payload.get("avg_realized_pnl_pct_at_picked")) if isinstance(rank_payload, dict) else None
    )
    if not stale and activation_ready and threshold is not None:
        if selected_rows is None or selected_rows >= min_selected_rows:
            return {
                "regime": resolved_regime,
                "enabled": True,
                "source": "research_thresholds",
                "threshold": float(threshold),
                "fallback_threshold": float(fallback),
                "activation_ready": True,
                "generated_at_utc": generated_at.isoformat() if generated_at else None,
                "stale": False,
                "selected_rows_at_picked": selected_rows,
                "avg_realized_pnl_pct_at_picked": avg_realized,
                "min_selected_rows": int(min_selected_rows),
                "min_avg_pnl_pct": float(min_avg_pnl_pct),
            }

        alternatives = rank_payload.get("alternatives") if isinstance(rank_payload, dict) else None
        best_alt_name: str | None = None
        best_alt: dict[str, Any] | None = None
        best_alt_score: float | None = None
        if isinstance(alternatives, dict):
            for alt_name in ("youden", "max_expected_pnl", "precision_floor_best", "max_f1"):
                alt = alternatives.get(alt_name)
                if not isinstance(alt, dict):
                    continue
                alt_threshold = _to_float(alt.get("threshold"))
                alt_selected = _to_int(alt.get("selected_rows"))
                alt_avg = _to_float(alt.get("avg_realized_pnl_pct"))
                if alt_threshold is None or alt_selected is None or alt_avg is None:
                    continue
                if alt_selected < min_selected_rows or alt_avg < min_avg_pnl_pct:
                    continue
                if best_alt_score is None or float(alt_avg) > best_alt_score:
                    best_alt_name = str(alt_name)
                    best_alt = alt
                    best_alt_score = float(alt_avg)

        if best_alt is not None:
            alt_threshold = float(_to_float(best_alt.get("threshold")) or 0.0) * 100.0
            alt_selected = _to_int(best_alt.get("selected_rows"))
            alt_avg = _to_float(best_alt.get("avg_realized_pnl_pct"))
            return {
                "regime": resolved_regime,
                "enabled": True,
                "source": f"research_thresholds_alternative:{best_alt_name}",
                "threshold": float(alt_threshold),
                "fallback_threshold": float(fallback),
                "activation_ready": True,
                "generated_at_utc": generated_at.isoformat() if generated_at else None,
                "stale": False,
                "selected_rows_at_picked": alt_selected,
                "avg_realized_pnl_pct_at_picked": alt_avg,
                "min_selected_rows": int(min_selected_rows),
                "min_avg_pnl_pct": float(min_avg_pnl_pct),
                "picked_selected_rows_at_picked": selected_rows,
                "picked_avg_realized_pnl_pct_at_picked": avg_realized,
                "picked_threshold": float(threshold),
            }

        return {
            "regime": resolved_regime,
            "enabled": True,
            "source": "fallback_sparse_research_threshold",
            "threshold": float(fallback),
            "fallback_threshold": float(fallback),
            "activation_ready": False,
            "generated_at_utc": generated_at.isoformat() if generated_at else None,
            "stale": False,
            "selected_rows_at_picked": selected_rows,
            "avg_realized_pnl_pct_at_picked": avg_realized,
            "min_selected_rows": int(min_selected_rows),
            "min_avg_pnl_pct": float(min_avg_pnl_pct),
            "picked_threshold": float(threshold),
        }

    return {
        "regime": resolved_regime,
        "enabled": True,
        "source": "fallback",
        "threshold": float(fallback),
        "fallback_threshold": float(fallback),
        "activation_ready": bool(activation_ready),
        "generated_at_utc": generated_at.isoformat() if generated_at else None,
        "stale": bool(stale),
        "selected_rows_at_picked": selected_rows,
        "avg_realized_pnl_pct_at_picked": avg_realized,
        "min_selected_rows": int(min_selected_rows),
        "min_avg_pnl_pct": float(min_avg_pnl_pct),
    }


def record_candidate_stage(
    token: dict[str, Any],
    *,
    stage: str,
    proba: float | None = None,
    threshold: float | None = None,
    rank_info: dict[str, Any] | None = None,
) -> None:
    if not bool(getattr(CFG, "RESEARCH_LANE_ENABLED", True)):
        return
    address = str(token.get("address") or "").strip()
    if not address:
        return
    ttl_s = int(getattr(CFG, "RESEARCH_DECISION_DEDUP_TTL_S", 600) or 600)
    dedup_key = f"stage:{address}:{stage}"
    if _event_dedup(dedup_key, ttl_s):
        return
    _write_event(
        "candidate_stage",
        address,
        stage=str(stage),
        **_common_payload(token, proba=proba, threshold=threshold, rank_info=rank_info),
    )


def record_candidate_decision(
    token: dict[str, Any],
    *,
    action: str,
    reason: str,
    stage: str,
    proba: float | None = None,
    threshold: float | None = None,
    rank_info: dict[str, Any] | None = None,
    shadow_kind: str | None = None,
    dedup_ttl_s: int | None = None,
) -> None:
    if not bool(getattr(CFG, "RESEARCH_LANE_ENABLED", True)):
        return
    address = str(token.get("address") or "").strip()
    if not address:
        return
    ttl_s = int(
        dedup_ttl_s
        if dedup_ttl_s is not None
        else int(getattr(CFG, "RESEARCH_DECISION_DEDUP_TTL_S", 600) or 600)
    )
    dedup_key = f"decision:{address}:{action}:{reason}:{stage}"
    if _event_dedup(dedup_key, ttl_s):
        return

    payload = _common_payload(token, proba=proba, threshold=threshold, rank_info=rank_info)
    payload.update(
        {
            "decision_action": str(action),
            "reason": str(reason),
            "stage": str(stage),
        }
    )
    if shadow_kind:
        payload["shadow_kind"] = str(shadow_kind)
    _write_event("candidate_decision", address, **payload)

    if action == "bought":
        _LIVE_CONTEXT[address] = payload


def should_open_shadow(
    token: dict[str, Any],
    *,
    action: str,
    reason: str,
    proba: float | None,
    threshold: float | None,
    rank_info: dict[str, Any] | None,
    soft_score_min: int = 0,
) -> tuple[bool, str]:
    if not bool(getattr(CFG, "RESEARCH_LANE_ENABLED", True)):
        return False, "research_disabled"
    if not bool(getattr(CFG, "RESEARCH_SHADOW_ENABLED", True)):
        return False, "shadow_disabled"
    if action not in {"rejected", "shadow"}:
        return False, "action_not_shadowable"

    address = str(token.get("address") or "").strip()
    if not address:
        return False, "missing_address"
    if address in _OPEN_SHADOWS:
        return False, "already_shadowed"

    regime = _normalize_regime(token.get("entry_regime") or token.get("discovered_via"))
    if regime != "pump_early":
        return False, "regime_not_enabled"
    profit_research_reject = (
        str(reason).startswith("live_profit_gate:")
        and str(token.get("entry_lane") or "").strip().lower() == "pump_early_sniper_research"
        and bool(getattr(CFG, "PUMP_EARLY_RESEARCH_ALLOW_PROXY", True))
    )
    counts = _shadow_counts_by_regime()
    global_cap = max(1, int(getattr(CFG, "RESEARCH_SHADOW_MAX_OPEN", 8) or 8))
    regime_cap = max(1, int(getattr(CFG, "RESEARCH_SHADOW_MAX_OPEN_PER_REGIME", 4) or 4))
    if len(_OPEN_SHADOWS) >= global_cap:
        return False, "shadow_cap_global"
    if int(counts.get(regime, 0)) >= regime_cap:
        return False, "shadow_cap_regime"

    rank_score = float(_to_float((rank_info or {}).get("rank_score"), 0.0) or 0.0)
    min_rank = float(getattr(CFG, "RESEARCH_SHADOW_MIN_RANK_SCORE", 55.0) or 55.0)
    if rank_score < min_rank and not profit_research_reject:
        return False, "rank_too_low"

    age_min = float(_to_float(token.get("age_minutes") or token.get("age_min"), 0.0) or 0.0)
    liq = float(_to_float(token.get("liquidity_usd"), 0.0) or 0.0)
    min_age = float(getattr(CFG, "RESEARCH_SHADOW_MIN_AGE_MIN", 2.0) or 2.0)
    min_liq = float(getattr(CFG, "RESEARCH_SHADOW_MIN_LIQUIDITY_USD", 1500.0) or 1500.0)
    if age_min < min_age:
        return False, "age_too_low"
    if liq < min_liq:
        return False, "liq_too_low"

    if reason in {"banned_creator", "no_liq", "basic_filter", "strategy_off", "dex_whitelist"}:
        return False, "reason_blocked"
    if str(reason).startswith("snapshot:"):
        return False, "snapshot_reject"
    if str(reason).startswith("strategy:"):
        return False, "still_waiting"
    if (
        str(reason).startswith("live_profit_gate:")
        and int(_to_int(token.get("live_profit_gate_failed_count"), 99) or 99) > 1
        and not profit_research_reject
    ):
        margin = float(_to_float(token.get("live_rank_gate_margin"), 99.0) or 99.0)
        if margin > 5.0:
            return False, "not_near_live_gate"

    score_total = int(_to_int(token.get("score_total"), 0) or 0)
    score_margin = max(0, int(soft_score_min or 0) - score_total)
    proba_f = _to_float(proba)
    thr_f = _to_float(threshold)
    proba_margin = (float(thr_f) - float(proba_f)) if proba_f is not None and thr_f is not None else None

    near_score = score_margin <= int(getattr(CFG, "RESEARCH_NEAR_MISS_SCORE_MARGIN", 8) or 8)
    near_proba = proba_margin is not None and 0.0 <= float(proba_margin) <= float(
        getattr(CFG, "RESEARCH_NEAR_MISS_PROBA_MARGIN", 0.12) or 0.12
    )

    if reason in {"ml_gate", "soft_score"} and not (near_score or near_proba):
        return False, "not_near_miss"

    return True, "open_shadow"


def record_shadow_open(
    address: str,
    *,
    payload: dict[str, Any],
    shadow_kind: str,
) -> None:
    address = str(address or "").strip()
    if not address:
        return
    data = dict(payload)
    data["shadow_kind"] = str(shadow_kind)
    _OPEN_SHADOWS[address] = data

    portfolio = _load_portfolio()
    portfolio[address] = _json_safe({**portfolio.get(address, {}), **data, "closed": False})
    _save_portfolio(portfolio)

    _write_event(
        "candidate_decision",
        address,
        decision_action="research_shadow_open",
        reason=str(data.get("reason") or "shadow"),
        stage=str(data.get("stage") or "decision"),
        shadow_kind=str(shadow_kind),
        **{k: v for k, v in data.items() if k not in {"decision_action", "stage", "reason", "shadow_kind"}},
    )


def record_shadow_partial(
    address: str,
    *,
    pnl_pct: float,
    fraction_sold: float,
) -> None:
    address = str(address or "").strip()
    if not address:
        return
    if address in _OPEN_SHADOWS:
        _OPEN_SHADOWS[address]["partial_taken"] = True

    _write_event(
        "candidate_partial",
        address,
        pnl_pct=float(pnl_pct),
        fraction_sold=float(fraction_sold),
    )


def record_shadow_close(
    address: str,
    *,
    regime: str,
    pnl_pct: float | None,
    exit_reason: str,
    label: int,
    close_price_usd: float | None = None,
    shadow_kind: str = "research",
    extra: dict[str, Any] | None = None,
) -> None:
    address = str(address or "").strip()
    if not address:
        return
    context = dict(_OPEN_SHADOWS.pop(address, {}))
    payload = {
        "source": "research_shadow",
        "shadow_kind": str(shadow_kind),
        "regime": _normalize_regime(regime),
        "pnl_pct": _to_float(pnl_pct),
        "exit_reason": str(exit_reason or ""),
        "label": int(label),
        "close_price_usd": _to_float(close_price_usd),
    }
    payload.update(context)
    if extra:
        payload.update(extra)
    _write_event("candidate_outcome", address, **payload)

    portfolio = _load_portfolio()
    row = dict(portfolio.get(address, {}))
    row.update(_json_safe(payload))
    row["closed"] = True
    row["closed_at"] = utc_now().isoformat()
    portfolio[address] = row
    _save_portfolio(portfolio)


def record_live_trade_close(
    address: str,
    *,
    regime: str,
    pnl_pct: float | None,
    exit_reason: str,
    extra: dict[str, Any] | None = None,
) -> None:
    address = str(address or "").strip()
    if not address:
        return
    context = dict(_LIVE_CONTEXT.pop(address, {}))
    label = 1 if (
        pnl_pct is not None
        and float(pnl_pct) >= float(getattr(CFG, "ML_POSITIVE_PNL_PCT", 5.0) or 5.0)
    ) else 0
    payload = {
        "source": "live_trade",
        "regime": _normalize_regime(regime),
        "pnl_pct": _to_float(pnl_pct),
        "exit_reason": str(exit_reason or ""),
        "label": int(label),
    }
    payload.update(context)
    if extra:
        payload.update(extra)
    _write_event("candidate_outcome", address, **payload)


def _load_events_frame() -> pd.DataFrame:
    frame, _stats = normalize_candidate_outcomes_frame(events_path=RESEARCH_EVENTS_PATH)
    return frame


def _group_stats(df: pd.DataFrame, group_col: str) -> list[dict[str, Any]]:
    if df.empty or group_col not in df.columns:
        return []
    out: list[dict[str, Any]] = []
    for group, grp in df.groupby(df[group_col].fillna("unknown").astype("string")):
        raw_pnl = grp["pnl_pct"] if "pnl_pct" in grp.columns else pd.Series(np.nan, index=grp.index, dtype="float64")
        grp_pnl = pd.to_numeric(raw_pnl, errors="coerce").dropna()
        out.append(
            {
                "group": str(group),
                "count": int(len(grp)),
                "win_rate_pct": _json_safe((grp_pnl.gt(0).mean() * 100.0) if not grp_pnl.empty else None),
                "avg_pnl_pct": _json_safe(float(grp_pnl.mean())) if not grp_pnl.empty else None,
                "median_pnl_pct": _json_safe(float(grp_pnl.median())) if not grp_pnl.empty else None,
            }
        )
    out.sort(key=lambda row: (-int(row["count"]), str(row["group"])))
    return out


def _calibrate_thresholds(outcomes: pd.DataFrame) -> dict[str, Any]:
    from ml.tune_threshold import tune_from_frame

    min_outcomes = int(getattr(CFG, "RESEARCH_THRESHOLD_MIN_OUTCOMES", 20) or 20)
    min_positives = int(getattr(CFG, "RESEARCH_THRESHOLD_MIN_POSITIVES", 4) or 4)
    min_selected = int(getattr(CFG, "RESEARCH_THRESHOLD_MIN_SELECTED", 6) or 6)
    min_realized = int(getattr(CFG, "RESEARCH_THRESHOLD_MIN_REALIZED_SELECTED", 4) or 4)
    precision_floor = float(getattr(CFG, "RESEARCH_THRESHOLD_PRECISION_FLOOR", 0.55) or 0.55)

    regimes: dict[str, Any] = {}
    if outcomes.empty:
        return {"generated_at_utc": utc_now().isoformat(), "regimes": regimes}

    for regime, grp in outcomes.groupby(outcomes["regime"].fillna("dex_mature").astype("string")):
        grp = grp.copy()
        grp["label"] = pd.to_numeric(grp.get("label"), errors="coerce").fillna(0).astype(int)
        grp["pnl_pct"] = pd.to_numeric(grp.get("pnl_pct"), errors="coerce")
        grp = grp[grp["pnl_pct"].notna()].copy()
        if len(grp) < min_outcomes or int(grp["label"].sum()) < min_positives:
            continue

        regime_out: dict[str, Any] = {
            "outcomes": int(len(grp)),
            "positives": int(grp["label"].sum()),
        }

        if "rank_score" in grp.columns:
            rank_series = pd.to_numeric(grp["rank_score"], errors="coerce").clip(0.0, 100.0)
            rank_frame = pd.DataFrame(
                {
                    "y_true": grp["label"].astype(int),
                    "y_prob": rank_series / 100.0,
                    "target_total_pnl_pct": grp["pnl_pct"],
                }
            ).dropna(subset=["y_prob"])
            if len(rank_frame) >= min_outcomes and int(rank_frame["y_true"].sum()) >= min_positives:
                rank_result = tune_from_frame(
                    rank_frame,
                    objective="expected_pnl_precision_floor",
                    precision_floor=precision_floor,
                    min_selected=min_selected,
                    min_realized_selected=min_realized,
                    source_csv=str(RESEARCH_EVENTS_NORMALIZED_PATH),
                )
                rank_result = dict(rank_result)
                rank_result["picked_rank_score"] = _json_safe(float(rank_result.get("picked", 0.0)) * 100.0)
                regime_out["rank_score"] = _json_safe(rank_result)

        if "ml_proba" in grp.columns:
            proba_series = pd.to_numeric(grp["ml_proba"], errors="coerce").clip(0.0, 1.0)
            proba_frame = pd.DataFrame(
                {
                    "y_true": grp["label"].astype(int),
                    "y_prob": proba_series,
                    "target_total_pnl_pct": grp["pnl_pct"],
                }
            ).dropna(subset=["y_prob"])
            if len(proba_frame) >= min_outcomes and int(proba_frame["y_true"].sum()) >= min_positives:
                regime_out["ml_proba"] = _json_safe(
                    tune_from_frame(
                        proba_frame,
                        objective="expected_pnl_precision_floor",
                        precision_floor=precision_floor,
                        min_selected=min_selected,
                        min_realized_selected=min_realized,
                        source_csv=str(RESEARCH_EVENTS_NORMALIZED_PATH),
                    )
                )

        if len(regime_out) > 2:
            regimes[str(regime)] = regime_out

    return {"generated_at_utc": utc_now().isoformat(), "regimes": _json_safe(regimes)}


def _render_scorecard_md(snapshot: dict[str, Any]) -> str:
    lines = [
        "# Research Scorecard",
        "",
        f"- Generated at UTC: `{snapshot.get('generated_at_utc')}`",
        f"- Events path: `{snapshot.get('events_path')}`",
        f"- Decision rows: `{snapshot.get('decision_rows')}`",
        f"- Outcome rows: `{snapshot.get('outcome_rows')}`",
        f"- Live closed: `{snapshot.get('live_closed')}`",
        f"- Research closed: `{snapshot.get('research_closed')}`",
        f"- Live avg pnl (%): `{snapshot.get('live_avg_pnl_pct')}`",
        f"- Research avg pnl (%): `{snapshot.get('research_avg_pnl_pct')}`",
        f"- Profitable research shadows: `{snapshot.get('profitable_research_shadows')}`",
        "",
        "## Decisions",
        "",
    ]

    for row in snapshot.get("decision_actions", []):
        lines.append(f"- `{row['group']}`: count=`{row['count']}`")
    if not snapshot.get("decision_actions"):
        lines.append("- Sin datos")
    lines.append("")
    lines.append("## Reject Reasons")
    lines.append("")
    for row in snapshot.get("reject_reasons", []):
        lines.append(f"- `{row['group']}`: count=`{row['count']}`")
    if not snapshot.get("reject_reasons"):
        lines.append("- Sin datos")
    lines.append("")
    lines.append("## Outcomes By Source")
    lines.append("")
    for row in snapshot.get("outcomes_by_source", []):
        lines.append(
            f"- `{row['group']}`: count=`{row['count']}`, win_rate=`{row['win_rate_pct']}`, avg_pnl=`{row['avg_pnl_pct']}`, median_pnl=`{row['median_pnl_pct']}`"
        )
    if not snapshot.get("outcomes_by_source"):
        lines.append("- Sin datos")
    lines.append("")
    lines.append("## Research Thresholds")
    lines.append("")
    thresholds = snapshot.get("thresholds", {}).get("regimes", {})
    if thresholds:
        for regime, payload in thresholds.items():
            rank = payload.get("rank_score") or {}
            proba = payload.get("ml_proba") or {}
            lines.append(
                f"- `{regime}`: rank=`{rank.get('picked_rank_score')}`, ml_proba=`{proba.get('picked')}`, outcomes=`{payload.get('outcomes')}`, positives=`{payload.get('positives')}`"
            )
    else:
        lines.append("- Sin datos suficientes")
    lines.append("")
    return "\n".join(lines)


def refresh_scorecard(*, force: bool = False) -> dict[str, Any] | None:
    if not bool(getattr(CFG, "RESEARCH_LANE_ENABLED", True)):
        return None
    global _LAST_SCORECARD_BUILD_MONO
    interval_s = max(60, int(getattr(CFG, "RESEARCH_SCORECARD_INTERVAL_MIN", 60) or 60) * 60)
    now_mono = time.monotonic()
    if not force and (now_mono - _LAST_SCORECARD_BUILD_MONO) < interval_s:
        return None

    events = _load_events_frame()
    if events.empty:
        return None
    normalized_write = write_normalized_candidate_outcomes(
        events_path=RESEARCH_EVENTS_PATH,
        output_path=RESEARCH_EVENTS_NORMALIZED_PATH,
    )

    decisions = events[events["event_type"].astype("string") == "candidate_decision"].copy()
    outcomes = events[events["event_type"].astype("string") == "candidate_outcome"].copy()
    if not outcomes.empty and {"address", "source"}.issubset(outcomes.columns):
        outcomes = (
            outcomes.sort_values("ts_utc", na_position="last")
            .drop_duplicates(subset=["address", "source"], keep="last")
            .copy()
        )
    live_outcomes = outcomes[outcomes.get("source", pd.Series("", index=outcomes.index)).astype("string") == "live_trade"].copy()
    research_outcomes = outcomes[
        outcomes.get("source", pd.Series("", index=outcomes.index)).astype("string") == "research_shadow"
    ].copy()

    rejects = decisions[
        decisions.get("decision_action", pd.Series("", index=decisions.index)).astype("string") == "rejected"
    ].copy()

    def _safe_mean(frame: pd.DataFrame) -> float | None:
        if frame.empty or "pnl_pct" not in frame.columns:
            return None
        series = pd.to_numeric(frame["pnl_pct"], errors="coerce").dropna()
        if series.empty:
            return None
        return float(series.mean())

    thresholds = _calibrate_thresholds(outcomes)
    snapshot = {
        "generated_at_utc": utc_now().isoformat(),
        "events_path": str(RESEARCH_EVENTS_NORMALIZED_PATH),
        "raw_events_path": str(RESEARCH_EVENTS_PATH),
        "decision_rows": int(len(decisions)),
        "outcome_rows": int(len(outcomes)),
        "live_closed": int(len(live_outcomes)),
        "research_closed": int(len(research_outcomes)),
        "live_avg_pnl_pct": _json_safe(_safe_mean(live_outcomes)),
        "research_avg_pnl_pct": _json_safe(_safe_mean(research_outcomes)),
        "profitable_research_shadows": int(
            pd.to_numeric(research_outcomes.get("pnl_pct"), errors="coerce").gt(0).sum()
        ) if not research_outcomes.empty else 0,
        "decision_actions": _group_stats(decisions, "decision_action"),
        "reject_reasons": _group_stats(rejects, "reason"),
        "outcomes_by_source": _group_stats(outcomes, "source"),
        "outcomes_by_regime": _group_stats(outcomes, "regime"),
        "research_shadow_origins": _group_stats(research_outcomes, "reason"),
        "normalization": normalized_write,
        "thresholds": thresholds,
    }

    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    RESEARCH_SCORECARD_JSON.write_text(
        json.dumps(_json_safe(snapshot), indent=2),
        encoding="utf-8",
    )
    RESEARCH_THRESHOLDS_JSON.write_text(
        json.dumps(_json_safe(thresholds), indent=2),
        encoding="utf-8",
    )
    RESEARCH_SCORECARD_MD.write_text(
        _render_scorecard_md(snapshot),
        encoding="utf-8",
    )
    _LAST_SCORECARD_BUILD_MONO = now_mono
    log.info(
        "Research scorecard actualizado: decisions=%d outcomes=%d live=%d research=%d",
        int(snapshot["decision_rows"]),
        int(snapshot["outcome_rows"]),
        int(snapshot["live_closed"]),
        int(snapshot["research_closed"]),
    )
    return snapshot


__all__ = [
    "load_live_rank_gate",
    "RESEARCH_EVENTS_PATH",
    "RESEARCH_EVENTS_NORMALIZED_PATH",
    "RESEARCH_PORTFOLIO_PATH",
    "RESEARCH_SCORECARD_JSON",
    "RESEARCH_SCORECARD_MD",
    "RESEARCH_THRESHOLDS_JSON",
    "record_candidate_decision",
    "record_candidate_stage",
    "record_live_trade_close",
    "record_shadow_close",
    "record_shadow_open",
    "record_shadow_partial",
    "refresh_scorecard",
    "score_candidate",
    "should_open_shadow",
]
