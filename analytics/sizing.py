from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from analytics.filters import effective_ai_threshold, effective_soft_score_min, effective_thresholds
from config.config import (
    AI_SIZING_ENABLED,
    BUY_SOFT_SCORE_MIN,
    CFG,
    DEX_MATURE_MAX_ACTIVE_POSITIONS,
    DEX_MATURE_MAX_SIZE_MULTIPLIER,
    DYNAMIC_SIZING_ENABLED,
    MAX_ACTIVE_POSITIONS_PER_REGIME,
    PUMP_EARLY_MAX_ACTIVE_POSITIONS,
    PUMP_EARLY_MAX_SIZE_MULTIPLIER,
    PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER,
    PUMP_EARLY_SNIPER_SIZE_HOT_MULTIPLIER,
    PUMP_EARLY_SNIPER_SIZE_MICRO_MULTIPLIER,
    REGIME_PUMP_EARLY_MAX_AGE_MIN,
    REVIVAL_MAX_ACTIVE_POSITIONS,
    REVIVAL_MAX_SIZE_MULTIPLIER,
    SIZE_MAX_MULTIPLIER,
    SIZE_MID_MULTIPLIER,
    SIZE_MIN_MULTIPLIER,
)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _normalize_discovery(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"pumpfun", "pump", "pump_fun"}:
        return "pumpfun"
    if raw in {"revival", "revive", "revived"}:
        return "revival"
    return "dex"


def _normalize_dex_id(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return raw.replace("_", "").replace("-", "").replace(" ", "")


def classify_entry_regime(token: dict[str, Any], queue_attempts: int = 0) -> str:
    """Clasificador operativo simple y conservador para sizing/exposición."""
    explicit_regime = str(token.get("entry_regime") or "").strip().lower()
    if explicit_regime in {"pump_early", "revival", "dex_mature"}:
        return explicit_regime
    entry_lane = str(token.get("entry_lane") or "").strip().lower()
    if entry_lane.startswith("pump_early_"):
        return "pump_early"
    discovered_via = _normalize_discovery(token.get("discovered_via"))
    dex_id = _normalize_dex_id(token.get("dex_id") or token.get("dexId"))
    age_min = _to_float(token.get("age_minutes") or token.get("age_min"))
    if discovered_via == "revival":
        return "revival"
    if discovered_via == "pumpfun":
        return "pump_early"
    if dex_id == "pumpfun" and (age_min <= 0.0 or age_min <= float(REGIME_PUMP_EARLY_MAX_AGE_MIN)):
        return "pump_early"

    # DeX recién listado se trata como setup temprano para limitar tamaño.
    if age_min > 0.0 and age_min <= float(REGIME_PUMP_EARLY_MAX_AGE_MIN):
        return "pump_early"

    # Requeues repetidos sin discovery explícito no bastan para marcar revival.
    # Mantenemos un sesgo conservador hacia "dex_mature".
    _ = queue_attempts
    return "dex_mature"


def regime_size_cap(regime: str) -> float:
    if regime == "pump_early":
        return float(PUMP_EARLY_MAX_SIZE_MULTIPLIER)
    if regime == "revival":
        return float(REVIVAL_MAX_SIZE_MULTIPLIER)
    return float(DEX_MATURE_MAX_SIZE_MULTIPLIER)


def regime_position_cap(regime: str, global_cap: int) -> int:
    per_regime = int(MAX_ACTIVE_POSITIONS_PER_REGIME or 0)
    if regime == "pump_early":
        override = PUMP_EARLY_MAX_ACTIVE_POSITIONS
    elif regime == "revival":
        override = REVIVAL_MAX_ACTIVE_POSITIONS
    else:
        override = DEX_MATURE_MAX_ACTIVE_POSITIONS

    caps = [int(global_cap)]
    if per_regime > 0:
        caps.append(per_regime)
    if override is not None and int(override) > 0:
        caps.append(int(override))
    return max(1, min(caps))


@dataclass(frozen=True)
class EntrySizingDecision:
    regime: str
    quality_points: int
    bucket: str
    multiplier: float
    amount_sol: float
    notes: tuple[str, ...]


def _quality_points(token: dict[str, Any], ai_proba: float) -> tuple[int, list[str]]:
    thresholds = effective_thresholds(token)
    soft_floor = int(effective_soft_score_min(token, BUY_SOFT_SCORE_MIN))
    ai_floor = float(effective_ai_threshold(token, 0.0)) if AI_SIZING_ENABLED else 0.0

    score_total = _to_int(token.get("score_total"))
    liq = _to_float(token.get("liquidity_usd"))
    vol = _to_float(token.get("volume_24h_usd"))
    mcap = _to_float(token.get("market_cap_usd"))
    price_impact = _to_float(token.get("price_impact_pct"))
    ai_edge = float(ai_proba) - ai_floor

    points = 0
    notes: list[str] = []

    if score_total >= soft_floor + 15:
        points += 2
        notes.append("score_strong")
    elif score_total >= soft_floor + 5:
        points += 1
        notes.append("score_ok")

    if AI_SIZING_ENABLED:
        if ai_edge >= 0.10:
            points += 2
            notes.append("ai_edge_strong")
        elif ai_edge >= 0.03:
            points += 1
            notes.append("ai_edge_ok")

    if liq >= float(thresholds.min_liquidity_usd) * 2.0:
        points += 1
        notes.append("liq_2x")

    if vol >= float(thresholds.min_vol_usd_24h) * 3.0:
        points += 1
        notes.append("vol_3x")

    if mcap > 0.0 and thresholds.min_market_cap_usd <= mcap <= float(thresholds.max_market_cap_usd) * 0.60:
        points += 1
        notes.append("mcap_compact")

    if price_impact > 0.0 and price_impact <= 10.0:
        points += 1
        notes.append("impact_ok")

    return points, notes


def compute_entry_sizing(
    *,
    token: dict[str, Any],
    ai_proba: float,
    base_amount_sol: float,
    queue_attempts: int = 0,
    ai_threshold: float | None = None,
) -> EntrySizingDecision:
    regime = classify_entry_regime(token, queue_attempts=queue_attempts)
    points, notes = _quality_points(token, ai_proba)
    _ = ai_threshold

    if not DYNAMIC_SIZING_ENABLED:
        multiplier = float(SIZE_MID_MULTIPLIER)
        bucket = "standard"
        notes = [*notes, "dynamic_sizing_disabled"]
    else:
        multiplier = float(SIZE_MID_MULTIPLIER)
        bucket = "standard"

    sniper_profile = str(token.get("gate_profile") or token.get("sniper_gate_profile") or "").strip().lower()
    entry_lane = str(token.get("entry_lane") or "").strip().lower()
    if regime == "pump_early" and (
        entry_lane in {"pump_early_pumpswap_profit", "pump_early_pumpswap_breakout_probe"}
        or sniper_profile.startswith("pumpswap_profit")
        or sniper_profile.startswith("pumpswap_meteor")
        or sniper_profile.startswith("pumpswap_breakout")
    ):
        if entry_lane == "pump_early_pumpswap_breakout_probe" or sniper_profile.startswith("pumpswap_breakout"):
            bucket = "pumpswap_breakout"
        elif sniper_profile == "pumpswap_meteor_prime":
            bucket = "pumpswap_meteor"
        else:
            bucket = "pumpswap_prime" if sniper_profile == "pumpswap_profit_prime" else "pumpswap_profit"
        multiplier = float(getattr(CFG, "PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER", PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER) or 0.20)
        notes = [*notes, f"lane_{bucket}"]

    if regime == "pump_early" and (
        entry_lane == "pump_early_green_candle_sniper" or sniper_profile.startswith("green_sniper")
    ):
        hint = str(token.get("green_sniper_size_hint") or "micro").strip().lower()
        bucket = f"green_sniper_{hint if hint in {'micro', 'core', 'hot'} else 'micro'}"
        if hint == "hot":
            multiplier = float(PUMP_EARLY_SNIPER_SIZE_HOT_MULTIPLIER)
        elif hint == "core":
            multiplier = float(PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER)
        else:
            multiplier = float(PUMP_EARLY_SNIPER_SIZE_MICRO_MULTIPLIER)
        notes = [*notes, f"lane_{bucket}"]

    if regime == "pump_early" and sniper_profile in {"sniper_micro", "sniper_core", "sniper_hot"}:
        bucket = sniper_profile
        if sniper_profile == "sniper_hot":
            multiplier = float(PUMP_EARLY_SNIPER_SIZE_HOT_MULTIPLIER)
        elif sniper_profile == "sniper_core":
            multiplier = float(PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER)
        else:
            multiplier = float(PUMP_EARLY_SNIPER_SIZE_MICRO_MULTIPLIER)
        notes = [*notes, f"lane_{sniper_profile}"]

    cap = regime_size_cap(regime)
    if multiplier > cap:
        multiplier = cap
        notes.append(f"cap_{regime}")
        if multiplier <= float(SIZE_MIN_MULTIPLIER):
            bucket = "recovery"

    multiplier = max(0.0, min(float(multiplier), float(SIZE_MAX_MULTIPLIER)))
    # TRADE_AMOUNT_SOL is the effective order size. Keep multipliers as policy
    # metadata only; the runtime uses the fixed amount for paper and live buys.
    amount_sol = max(0.0, float(base_amount_sol)) if multiplier > 0.0 else 0.0

    return EntrySizingDecision(
        regime=regime,
        quality_points=int(points),
        bucket=bucket,
        multiplier=float(multiplier),
        amount_sol=float(amount_sol),
        notes=tuple(notes),
    )


def count_open_by_regime(open_regimes: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for regime in open_regimes:
        key = str(regime or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def describe_sizing_policy() -> dict[str, Any]:
    return {
        "dynamic_sizing_enabled": bool(DYNAMIC_SIZING_ENABLED),
        "ai_sizing_enabled": bool(AI_SIZING_ENABLED),
        "trade_amount_mode": "fixed",
        "default_trade_amount_sol": float(getattr(CFG, "TRADE_AMOUNT_SOL", 0.1) or 0.1),
        "min_buy_sol": float(getattr(CFG, "MIN_BUY_SOL", 0.1) or 0.1),
        "multipliers_affect_trade_amount": False,
        "pump_early_max_age_min": float(REGIME_PUMP_EARLY_MAX_AGE_MIN),
        "size_multipliers": {
            "standard": float(SIZE_MID_MULTIPLIER),
            "recovery": float(SIZE_MIN_MULTIPLIER),
            "sniper_micro": float(PUMP_EARLY_SNIPER_SIZE_MICRO_MULTIPLIER),
            "sniper_core": float(PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER),
            "sniper_hot": float(PUMP_EARLY_SNIPER_SIZE_HOT_MULTIPLIER),
            "pumpswap_profit": float(getattr(CFG, "PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER", 0.20) or 0.20),
            "pumpswap_prime": float(getattr(CFG, "PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER", 0.20) or 0.20),
            "pumpswap_meteor": float(getattr(CFG, "PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER", 0.20) or 0.20),
            "pumpswap_breakout": float(getattr(CFG, "PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER", 0.20) or 0.20),
            "green_sniper_micro": float(PUMP_EARLY_SNIPER_SIZE_MICRO_MULTIPLIER),
            "green_sniper_core": float(PUMP_EARLY_SNIPER_SIZE_CORE_MULTIPLIER),
            "green_sniper_hot": float(PUMP_EARLY_SNIPER_SIZE_HOT_MULTIPLIER),
        },
        "green_sniper_amounts_sol": {
            "paper_micro": float(getattr(CFG, "GREEN_SNIPER_SIZE_MICRO_SOL", 0.03) or 0.03),
            "paper_core": float(getattr(CFG, "GREEN_SNIPER_SIZE_CORE_SOL", 0.06) or 0.06),
            "paper_hot": float(getattr(CFG, "GREEN_SNIPER_SIZE_HOT_SOL", 0.10) or 0.10),
            "live_canary": float(getattr(CFG, "GREEN_SNIPER_LIVE_SIZE_SOL", 0.01) or 0.01),
        },
        "regime_size_caps": {
            "pump_early": float(PUMP_EARLY_MAX_SIZE_MULTIPLIER),
            "dex_mature": float(DEX_MATURE_MAX_SIZE_MULTIPLIER),
            "revival": float(REVIVAL_MAX_SIZE_MULTIPLIER),
        },
        "regime_position_caps": {
            "global_per_regime": int(MAX_ACTIVE_POSITIONS_PER_REGIME),
            "pump_early": PUMP_EARLY_MAX_ACTIVE_POSITIONS,
            "dex_mature": DEX_MATURE_MAX_ACTIVE_POSITIONS,
            "revival": REVIVAL_MAX_ACTIVE_POSITIONS,
        },
    }


__all__ = [
    "EntrySizingDecision",
    "classify_entry_regime",
    "compute_entry_sizing",
    "count_open_by_regime",
    "describe_sizing_policy",
    "regime_position_cap",
    "regime_size_cap",
]
