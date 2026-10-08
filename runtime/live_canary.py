from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Any
from pathlib import Path

from config.config import CFG
from runtime.green_canary_risk import GreenCanaryRiskStore, GreenCanaryRiskError
from utils.raw_units import sol_to_lamports


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
STORE: GreenCanaryRiskStore | None = None


def initialize(root: Path, positions) -> None:
    global STORE
    STORE = GreenCanaryRiskStore(root)
    STORE.reconcile(positions)


def reconcile(positions) -> None:
    if STORE is None:
        raise GreenCanaryRiskError("Green LIVE risk store is not initialized")
    STORE.reconcile(positions)


def invalidate() -> None:
    if STORE is not None:
        STORE.ready, STORE.error = False, "source_query_unavailable"


def _risk_snapshot() -> dict:
    if STORE is None:
        return STATE.to_dict() | {"ready": False, "ledger_error": None, "pending_buys": 0,
            "unproved_positions": 0, "open_positions": 0, "daily_loss_lamports": {}}
    try:
        return STORE.snapshot()
    except GreenCanaryRiskError:
        return {"ready": False, "ledger_error": STORE.error}


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


def evaluate_green_live_canary(token: dict[str, Any], *, risk: dict | None = None) -> tuple[bool, str]:
    if bool(getattr(CFG, "STRATEGY_OPTIMIZATION_LOCK", True)):
        return False, "strategy_optimization_lock"
    if not bool(getattr(CFG, "GREEN_SNIPER_LIVE_ENABLED", False)):
        return False, "green_live_disabled"
    risk = _risk_snapshot() if risk is None else risk
    if risk.get("unvalued_closes"):
        return False, "pnl_valuation_unavailable"
    if risk.get("ledger_error"):
        return False, "risk_ledger_unavailable"
    if not risk.get("ready"):
        return False, "risk_ledger_not_initialized"
    if risk.get("pending_buys") or risk.get("unproved_positions"):
        return False, "original_execution_unresolved"
    if risk.get("disabled"):
        return False, risk.get("last_disable_reason") or "green_live_canary_disabled"
    day = _today()
    max_buys = getattr(CFG, "GREEN_SNIPER_LIVE_MAX_DAILY_BUYS", 0)
    if type(max_buys) is not int or max_buys <= 0:
        return False, "daily_buy_cap_required"
    if risk["daily_buys"].get(day, 0) >= max_buys:
        return False, "daily_buy_cap"
    max_loss = sol_to_lamports(getattr(CFG, "GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL", 0.05))
    if max_loss is None:
        return False, "daily_loss_cap_required"
    if risk["daily_loss_lamports"].get(day, 0) >= max_loss:
        return False, "daily_loss_cap"
    if risk.get("reserved_lamports", 0) + risk["daily_loss_lamports"].get(day, 0) > max_loss:
        return False, "remaining_loss_budget"
    max_open = getattr(CFG, "GREEN_SNIPER_LIVE_MAX_OPEN", 1)
    if type(max_open) is not int or max_open <= 0:
        return False, "open_cap_required"
    if risk["open_positions"] >= max_open:
        return False, "open_cap"
    max_losses = getattr(CFG, "GREEN_SNIPER_LIVE_MAX_CONSECUTIVE_LOSSES", 2)
    if type(max_losses) is not int or max_losses <= 0:
        return False, "loss_streak_cap_required"
    if risk["consecutive_losses"] >= max_losses:
        return False, "loss_streak_cap"
    route = token.get("has_jupiter_route")
    if bool(getattr(CFG, "GREEN_SNIPER_REQUIRE_ROUTE_LIVE", True)) and not (type(route) in {bool, int} and route == 1):
        return False, "no_route"
    impact = token.get("price_impact_pct")
    max_impact = getattr(CFG, "GREEN_SNIPER_LIVE_MAX_PRICE_IMPACT_PCT", 12.0)
    if (isinstance(max_impact, bool) or not isinstance(max_impact, (int, float))
            or not math.isfinite(max_impact) or max_impact <= 0):
        return False, "price_impact_cap_required"
    if (isinstance(impact, bool) or not isinstance(impact, (int, float))
            or not math.isfinite(impact) or impact < 0):
        return False, "price_impact_unavailable"
    if impact > max_impact:
        return False, "high_impact"
    return True, "ok"


def reserve_green_live_buy(row: dict, token: dict) -> tuple[bool, str]:
    if STORE is None:
        return False, "risk_ledger_not_initialized"
    amount = sol_to_lamports(row.get("amount_sol"))
    size_cap = sol_to_lamports(getattr(CFG, "GREEN_SNIPER_LIVE_SIZE_SOL", .01))
    if size_cap is None:
        return False, "size_cap_required"
    if amount is None or amount > size_cap:
        return False, "live_size_cap"
    def evaluate(token, *, risk):
        return evaluate_green_live_canary(token, risk={**risk, "reserved_lamports": amount})
    return STORE.reserve(row, token, evaluate)


def record_green_live_buy() -> None:
    # Legacy diagnostic-only API. It cannot authorize risk without a store, and
    # cannot mutate the production store without an original intent identity.
    if STORE is not None:
        raise GreenCanaryRiskError("Unowned buy counters cannot update durable risk")
    day = _today()
    STATE.daily_buys[day] = STATE.daily_buys.get(day, 0) + 1


def record_green_live_close(*, pnl_sol: float | None = None, exit_reason: str | None = None) -> None:
    if STORE is not None:
        raise GreenCanaryRiskError("Scalar/post-hoc FX cannot update original risk")
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
    if STORE is not None:
        STORE.disable(reason, minutes=minutes)
        return
    until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=max(1, int(minutes)))
    STATE.disabled_until = until.isoformat()
    STATE.last_disable_reason = str(reason)


def snapshot() -> dict[str, Any]:
    risk = _risk_snapshot()
    return risk | {
        "enabled": bool(getattr(CFG, "GREEN_SNIPER_LIVE_ENABLED", False)),
        "disabled": bool(risk.get("disabled") or risk.get("unvalued_closes") or not risk.get("ready")),
        "max_daily_buys": getattr(CFG, "GREEN_SNIPER_LIVE_MAX_DAILY_BUYS", 0),
        "max_daily_loss_sol": getattr(CFG, "GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL", 0.05),
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
    "initialize",
    "reconcile",
    "invalidate",
    "reserve_green_live_buy",
]
