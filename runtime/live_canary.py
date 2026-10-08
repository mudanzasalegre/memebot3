from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from config.config import CFG


SEVERE_EXIT_REASONS = {"LIQUIDITY_CRUSH", "STOP_LOSS", "EARLY_DROP", "ADVERSE_TICK"}


@dataclass
class LiveCanaryState:
    daily_buys: dict[str, int] = field(default_factory=dict)
    daily_loss_sol: dict[str, float] = field(default_factory=dict)
    consecutive_losses: int = 0
    disabled_until: str | None = None
    last_disable_reason: str | None = None
    unvalued_closes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


STATE = LiveCanaryState()


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def _is_disabled() -> bool:
    if not STATE.disabled_until:
        return False
    try:
        until = dt.datetime.fromisoformat(STATE.disabled_until)
    except Exception:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc) < until


def evaluate_green_live_canary(token: dict[str, Any]) -> tuple[bool, str]:
    if bool(getattr(CFG, "STRATEGY_OPTIMIZATION_LOCK", True)):
        return False, "strategy_optimization_lock"
    if not bool(getattr(CFG, "GREEN_SNIPER_LIVE_ENABLED", False)):
        return False, "green_live_disabled"
    if STATE.unvalued_closes:
        return False, "pnl_valuation_unavailable"
    if _is_disabled():
        return False, STATE.last_disable_reason or "green_live_canary_disabled"
    day = _today()
    max_buys = int(getattr(CFG, "GREEN_SNIPER_LIVE_MAX_DAILY_BUYS", 0) or 0)
    if max_buys <= 0:
        return False, "daily_buy_cap_required"
    if STATE.daily_buys.get(day, 0) >= max_buys:
        return False, "daily_buy_cap"
    max_loss = float(getattr(CFG, "GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL", 0.05) or 0.05)
    if max_loss <= 0:
        return False, "daily_loss_cap_required"
    if abs(float(STATE.daily_loss_sol.get(day, 0.0))) >= max_loss:
        return False, "daily_loss_cap"
    max_losses = int(getattr(CFG, "GREEN_SNIPER_LIVE_MAX_CONSECUTIVE_LOSSES", 2) or 2)
    if STATE.consecutive_losses >= max_losses:
        return False, "loss_streak_cap"
    if bool(getattr(CFG, "GREEN_SNIPER_REQUIRE_ROUTE_LIVE", True)) and not bool(token.get("has_jupiter_route")):
        return False, "no_route"
    impact = float(token.get("price_impact_pct") or 0.0)
    max_impact = float(getattr(CFG, "GREEN_SNIPER_LIVE_MAX_PRICE_IMPACT_PCT", 12.0) or 12.0)
    if impact > max_impact:
        return False, "high_impact"
    return True, "ok"


def record_green_live_buy() -> None:
    day = _today()
    STATE.daily_buys[day] = STATE.daily_buys.get(day, 0) + 1


def record_green_live_close(*, pnl_sol: float | None = None, exit_reason: str | None = None) -> None:
    day = _today()
    if (isinstance(pnl_sol, bool) or not isinstance(pnl_sol, (int, float))
            or not math.isfinite(pnl_sol)):
        # Do not reset a loss streak or invent a dollar/SOL conversion. This
        # unresolved risk-budget observation needs reconciliation, not expiry.
        STATE.unvalued_closes += 1
        disable("pnl_valuation_unavailable")
    else:
        pnl = float(pnl_sol)
        if pnl < 0:
            STATE.daily_loss_sol[day] = STATE.daily_loss_sol.get(day, 0.0) + abs(pnl)
            STATE.consecutive_losses += 1
        else:
            STATE.consecutive_losses = 0
    if (
        bool(getattr(CFG, "GREEN_SNIPER_LIVE_DISABLE_ON_LIQ_CRUSH", True))
        and str(exit_reason or "").upper() == "LIQUIDITY_CRUSH"
    ):
        disable("liquidity_crush")


def disable(reason: str, *, minutes: int = 240) -> None:
    until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=max(1, int(minutes)))
    STATE.disabled_until = until.isoformat()
    STATE.last_disable_reason = str(reason)


def snapshot() -> dict[str, Any]:
    return STATE.to_dict() | {
        "enabled": bool(getattr(CFG, "GREEN_SNIPER_LIVE_ENABLED", False)),
        "disabled": _is_disabled() or bool(STATE.unvalued_closes),
        "max_daily_buys": int(getattr(CFG, "GREEN_SNIPER_LIVE_MAX_DAILY_BUYS", 0) or 0),
        "max_daily_loss_sol": float(getattr(CFG, "GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL", 0.05) or 0.05),
    }


__all__ = [
    "SEVERE_EXIT_REASONS",
    "STATE",
    "LiveCanaryState",
    "disable",
    "evaluate_green_live_canary",
    "record_green_live_buy",
    "record_green_live_close",
    "snapshot",
]
