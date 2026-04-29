from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from config.config import CFG
from analytics.social_signal import SOCIAL_STATUS_PRESENT, SOCIAL_STATUS_SUSPICIOUS, social_signal_from_token


@dataclass(frozen=True)
class GreenSniperSizingDecision:
    size_hint: str
    amount_sol: float
    mode: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        out = float(value)
        if out != out:
            return float(default)
        return out
    except Exception:
        return float(default)


def _tier_amount(size_hint: str) -> float:
    hint = str(size_hint or "micro").strip().lower()
    if hint == "hot":
        return _to_float(getattr(CFG, "GREEN_SNIPER_SIZE_HOT_SOL", 0.10), 0.10)
    if hint == "core":
        return _to_float(getattr(CFG, "GREEN_SNIPER_SIZE_CORE_SOL", 0.06), 0.06)
    return _to_float(getattr(CFG, "GREEN_SNIPER_SIZE_MICRO_SOL", 0.03), 0.03)


def _tier_index(size_hint: str) -> int:
    order = {"micro": 0, "core": 1, "hot": 2}
    return order.get(str(size_hint or "micro").strip().lower(), 0)


def _tier_name(index: int) -> str:
    return ("micro", "core", "hot")[max(0, min(2, int(index)))]


def compute_green_sniper_sizing(
    token: dict[str, Any],
    *,
    dry_run: bool,
    live: bool,
    size_hint: str | None = None,
    risk_proba: float | None = None,
    ev_pred_pct: float | None = None,
) -> GreenSniperSizingDecision:
    hint = str(size_hint or token.get("green_sniper_size_hint") or "micro").strip().lower()
    if hint not in {"micro", "core", "hot"}:
        hint = "micro"

    if live:
        amount = _to_float(getattr(CFG, "GREEN_SNIPER_LIVE_SIZE_SOL", 0.01), 0.01)
        mode = str(getattr(CFG, "GREEN_SNIPER_LIVE_SIZE_MODE", "canary_fixed") or "canary_fixed")
        social = social_signal_from_token(token)
        if (
            social.status == SOCIAL_STATUS_SUSPICIOUS
            and bool(getattr(CFG, "GREEN_SNIPER_SOCIALS_CAN_DECREASE_SIZE", True))
            and bool(getattr(CFG, "GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_REDUCE_SIZE", True))
        ):
            hint = _tier_name(_tier_index(hint) - 1)
        if bool(getattr(CFG, "GREEN_SNIPER_LIVE_ADVANCED_ENABLED", False)) and hint == "hot":
            amount = _to_float(getattr(CFG, "GREEN_SNIPER_LIVE_ADVANCED_SIZE_SOL", 0.03), 0.03)
            return GreenSniperSizingDecision(hint, amount, mode, "live_advanced_hot")
        return GreenSniperSizingDecision(hint, amount, mode, "live_canary_fixed")

    social = social_signal_from_token(token)
    if (
        social.status == SOCIAL_STATUS_PRESENT
        and bool(getattr(CFG, "GREEN_SNIPER_SOCIALS_CAN_INCREASE_SIZE_PAPER", True))
    ):
        max_bonus = max(0, int(getattr(CFG, "GREEN_SNIPER_SOCIALS_MAX_SIZE_BONUS_TIER", 1) or 1))
        promoted = _tier_name(_tier_index(hint) + min(max_bonus, 1))
        if promoted != hint:
            hint = promoted
    elif (
        social.status == SOCIAL_STATUS_SUSPICIOUS
        and bool(getattr(CFG, "GREEN_SNIPER_SOCIALS_CAN_DECREASE_SIZE", True))
        and bool(getattr(CFG, "GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_REDUCE_SIZE", True))
    ):
        hint = _tier_name(_tier_index(hint) - 1)

    amount = _tier_amount(hint)
    reason = f"paper_{hint}"
    if social.status == SOCIAL_STATUS_PRESENT:
        reason = f"{reason}_social_confidence"
    elif social.status == SOCIAL_STATUS_SUSPICIOUS:
        reason = f"{reason}_social_risk"
    if risk_proba is not None and bool(getattr(CFG, "GREEN_SNIPER_ML_RISK_REDUCE_SIZE", True)) and float(risk_proba) >= 0.70:
        amount = min(amount, _tier_amount("micro"))
        reason = "paper_risk_reduced"
    if (
        dry_run
        and ev_pred_pct is not None
        and bool(getattr(CFG, "GREEN_SNIPER_ML_EV_SIZE_UP_PAPER", True))
        and float(ev_pred_pct) >= float(getattr(CFG, "ML_EV_MIN_FOR_SIZE_UP", 20.0) or 20.0)
    ):
        amount = max(amount, _tier_amount("hot" if hint == "core" else "core"))
        reason = "paper_ev_size_up"
    return GreenSniperSizingDecision(hint, amount, str(getattr(CFG, "GREEN_SNIPER_SIZE_MODE", "fixed_tiers")), reason)


def describe_green_sniper_sizing() -> dict[str, Any]:
    return {
        "paper_mode": str(getattr(CFG, "GREEN_SNIPER_SIZE_MODE", "fixed_tiers")),
        "paper_tiers_sol": {
            "micro": _tier_amount("micro"),
            "core": _tier_amount("core"),
            "hot": _tier_amount("hot"),
        },
        "live_mode": str(getattr(CFG, "GREEN_SNIPER_LIVE_SIZE_MODE", "canary_fixed")),
        "live_size_sol": _to_float(getattr(CFG, "GREEN_SNIPER_LIVE_SIZE_SOL", 0.01), 0.01),
        "live_advanced_enabled": bool(getattr(CFG, "GREEN_SNIPER_LIVE_ADVANCED_ENABLED", False)),
        "socials": {
            "paper_can_increase": bool(getattr(CFG, "GREEN_SNIPER_SOCIALS_CAN_INCREASE_SIZE_PAPER", True)),
            "live_can_increase": bool(getattr(CFG, "GREEN_SNIPER_SOCIALS_CAN_INCREASE_SIZE_LIVE", False)),
            "can_decrease": bool(getattr(CFG, "GREEN_SNIPER_SOCIALS_CAN_DECREASE_SIZE", True)),
            "max_bonus_tier": int(getattr(CFG, "GREEN_SNIPER_SOCIALS_MAX_SIZE_BONUS_TIER", 1) or 1),
        },
    }


__all__ = ["GreenSniperSizingDecision", "compute_green_sniper_sizing", "describe_green_sniper_sizing"]
