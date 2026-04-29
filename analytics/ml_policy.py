from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from config.config import CFG, PROJECT_ROOT
from ml.data_contract import reconstruct_entry_lane
from ml.lane_taxonomy import (
    LANE_PUMP_EARLY_GREEN_SNIPER,
    LIVE_PROFIT_LANES,
    RESEARCH_LANES,
    LANE_UNKNOWN,
    normalize_entry_lane,
)


THRESHOLDS_BY_LANE_PATH = PROJECT_ROOT / "data" / "metrics" / "recommended_thresholds.by_lane.json"
LEGACY_THRESHOLD_PATH = PROJECT_ROOT / "data" / "metrics" / "recommended_threshold.json"


@dataclass(frozen=True)
class MlPolicyDecision:
    mode: str
    lane: str
    proba: float | None
    threshold: float | None
    allow_buy: bool
    enforce: bool
    sizing_multiplier: float
    risk_veto: bool
    reason: str
    activation_ready: bool
    source: str
    risk_proba: float | None = None
    ev_pred_pct: float | None = None
    edge_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _bool_cfg(name: str, default: bool = False) -> bool:
    raw = getattr(CFG, name, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _float_cfg(name: str, default: float) -> float:
    try:
        return float(getattr(CFG, name, default))
    except Exception:
        return float(default)


def _mode_cfg(name: str, default: str) -> str:
    return str(getattr(CFG, name, default) or default).strip().lower()


def _threshold_payload(lane: str) -> tuple[float | None, bool, str, str]:
    by_lane = _read_json(THRESHOLDS_BY_LANE_PATH)
    lane_payload = ((by_lane.get("by_lane") or {}).get(lane) or {}) if isinstance(by_lane, dict) else {}
    if lane_payload:
        threshold = lane_payload.get("threshold")
        if threshold is None:
            threshold = lane_payload.get("picked")
        try:
            threshold_f = float(threshold)
        except Exception:
            threshold_f = None
        return threshold_f, bool(lane_payload.get("activation_ready")), str(lane_payload.get("reason") or "by_lane"), "by_lane"

    legacy = _read_json(LEGACY_THRESHOLD_PATH)
    threshold = legacy.get("picked", getattr(CFG, "AI_THRESHOLD", None))
    try:
        threshold_f = float(threshold)
    except Exception:
        threshold_f = None
    return threshold_f, bool(legacy.get("activation_ready", False)), str(legacy.get("activation_reason") or "legacy_threshold"), "legacy"


def _resolve_lane(token: dict[str, Any], feature_row: Any) -> str:
    row: dict[str, Any] = {}
    if hasattr(feature_row, "to_dict"):
        row.update(feature_row.to_dict())
    elif isinstance(feature_row, dict):
        row.update(feature_row)
    row.update(token or {})
    return normalize_entry_lane(reconstruct_entry_lane(row))


def _risk_veto(risk_proba: float | None) -> bool:
    if risk_proba is None:
        return False
    if not _bool_cfg("ML_RISK_VETO_ENABLED", False):
        return False
    if _bool_cfg("ML_RISK_SHADOW_ONLY", True):
        return False
    return float(risk_proba) >= _float_cfg("ML_RISK_VETO_THRESHOLD", 0.70)


def _edge_score(ev_pred_pct: float | None, risk_proba: float | None) -> float | None:
    if ev_pred_pct is None:
        return None
    severe = abs(_float_cfg("ML_SEVERE_LOSS_PCT", -30.0))
    penalty = _float_cfg("ML_RISK_PENALTY_MULT", 1.0)
    return float(ev_pred_pct) - penalty * float(risk_proba or 0.0) * severe


def _sizing_multiplier(lane: str, proba: float | None, ev_pred_pct: float | None, risk_proba: float | None) -> float:
    if lane == LANE_PUMP_EARLY_GREEN_SNIPER and not _bool_cfg("GREEN_SNIPER_ML_RISK_REDUCE_SIZE", True):
        return 1.0
    if not _bool_cfg("ML_SIZING_ENABLED", False):
        return 1.0
    min_mult = _float_cfg("ML_SIZE_MIN_MULT", 0.25)
    mid_mult = _float_cfg("ML_SIZE_MID_MULT", 0.50)
    max_mult = _float_cfg("ML_SIZE_MAX_MULT", 1.00)
    if risk_proba is not None and risk_proba >= _float_cfg("ML_RISK_VETO_THRESHOLD", 0.70):
        return min_mult
    if lane in LIVE_PROFIT_LANES:
        proba_f = float(proba or 0.0)
        ev_f = float(ev_pred_pct or 0.0)
        if ev_f >= _float_cfg("ML_LIVE_PROFIT_EV_SIZE_UP", 50.0) and proba_f >= _float_cfg("ML_LIVE_PROFIT_PROBA_SIZE_UP", 0.30):
            return max_mult
        if ev_f >= _float_cfg("ML_LIVE_PROFIT_EV_MIN", 0.0):
            return mid_mult
        return min_mult
    if lane in RESEARCH_LANES:
        return min_mult
    return 0.0


def decide_ml_action(
    *,
    token: dict[str, Any],
    feature_row: Any,
    proba: float | None,
    base_rules_passed: bool,
    dry_run: bool,
    live: bool,
    risk_proba: float | None = None,
    ev_pred_pct: float | None = None,
) -> MlPolicyDecision:
    lane = _resolve_lane(token, feature_row)
    global_mode = _mode_cfg("ML_GATE_MODE", "shadow")
    if global_mode not in {"off", "shadow", "legacy", "enforce", "lane_aware", "sizing_only", "risk_veto_only"}:
        global_mode = "legacy"
    threshold, activation_ready, threshold_reason, source = _threshold_payload(lane)
    proba_pass = bool(proba is not None and threshold is not None and float(proba) >= float(threshold))
    risk_veto = _risk_veto(risk_proba)
    edge = _edge_score(ev_pred_pct, risk_proba)
    sizing_mult = _sizing_multiplier(lane, proba, ev_pred_pct, risk_proba)

    mode = global_mode
    enforce = False
    allow = bool(base_rules_passed)
    reason = "rules_passed"

    if lane == LANE_PUMP_EARLY_GREEN_SNIPER:
        mode = _mode_cfg("GREEN_SNIPER_ML_MODE", "sizing_only")
        enforce = bool(_bool_cfg("GREEN_SNIPER_ML_BLOCK_ENABLED", False) and activation_ready)
        allow = bool(base_rules_passed and (not enforce or proba_pass))
        reason = "green_sniper_ml_copilot" if allow else "green_sniper_ml_block"
        green_risk_veto = bool(
            live
            and _bool_cfg("GREEN_SNIPER_RISK_CAN_VETO_LIVE", False)
            and risk_proba is not None
            and float(risk_proba) >= _float_cfg("ML_RISK_VETO_THRESHOLD", 0.70)
        )
        if green_risk_veto:
            allow = False
            reason = "green_sniper_risk_veto"
            risk_veto = True
        return MlPolicyDecision(
            mode=mode,
            lane=lane or LANE_UNKNOWN,
            proba=None if proba is None else float(proba),
            threshold=threshold,
            allow_buy=bool(allow),
            enforce=bool(enforce),
            sizing_multiplier=float(sizing_mult),
            risk_veto=bool(risk_veto),
            reason=reason,
            activation_ready=bool(activation_ready),
            source=source,
            risk_proba=None if risk_proba is None else float(risk_proba),
            ev_pred_pct=None if ev_pred_pct is None else float(ev_pred_pct),
            edge_score=edge,
        )

    if global_mode == "off":
        reason = "ml_off"
    elif global_mode == "shadow":
        reason = "ml_shadow"
    elif global_mode in {"legacy", "enforce"}:
        enforce = global_mode == "legacy" or bool(activation_ready)
        allow = bool(base_rules_passed and (not enforce or proba_pass))
        reason = "global_threshold_pass" if allow else "global_threshold_reject"
    elif global_mode == "sizing_only":
        mode = "sizing_only"
        reason = "sizing_only"
    elif global_mode == "risk_veto_only":
        mode = "risk_veto_only"
        reason = "risk_veto_only"
    elif global_mode == "lane_aware":
        if lane in LIVE_PROFIT_LANES:
            mode = _mode_cfg("ML_LIVE_PROFIT_MODE", "sizing_only")
            enforce = mode == "enforce" and bool(activation_ready)
            allow = bool(base_rules_passed and (not enforce or proba_pass))
            reason = "live_profit_lane_sizing_only" if not enforce else ("live_profit_lane_enforce_pass" if allow else "live_profit_lane_enforce_reject")
        elif lane in RESEARCH_LANES:
            mode = _mode_cfg("ML_RESEARCH_MODE", "enforce")
            enforce = mode == "enforce" and bool(activation_ready)
            allow = bool(base_rules_passed and (not enforce or proba_pass))
            if live and not _bool_cfg("ML_ALLOW_RESEARCH_LIVE", False):
                allow = False
                reason = "research_live_disabled"
            else:
                reason = "research_enforce_pass" if allow else "research_enforce_reject"
        else:
            mode = _mode_cfg("ML_UNKNOWN_LANE_MODE", "shadow")
            allow = bool(base_rules_passed and dry_run and mode == "shadow")
            if live and not _bool_cfg("ML_ALLOW_UNKNOWN_LIVE", False):
                allow = False
            reason = "unknown_lane_shadow" if allow else "unknown_lane_no_live"

    if risk_veto:
        allow = False
        reason = "risk_veto"

    return MlPolicyDecision(
        mode=mode,
        lane=lane or LANE_UNKNOWN,
        proba=None if proba is None else float(proba),
        threshold=threshold,
        allow_buy=bool(allow),
        enforce=bool(enforce),
        sizing_multiplier=float(sizing_mult),
        risk_veto=bool(risk_veto),
        reason=f"{reason}:{threshold_reason}" if threshold_reason and reason not in {"ml_off", "ml_shadow"} else reason,
        activation_ready=bool(activation_ready),
        source=source,
        risk_proba=None if risk_proba is None else float(risk_proba),
        ev_pred_pct=None if ev_pred_pct is None else float(ev_pred_pct),
        edge_score=edge,
    )


__all__ = ["MlPolicyDecision", "decide_ml_action"]
