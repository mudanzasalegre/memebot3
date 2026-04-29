from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from config.config import CFG


@dataclass(frozen=True)
class ProfitPnlGuardDecision:
    allowed: bool
    failures: tuple[str, ...] = ()
    blocked_bucket: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        out = float(value)
        if out != out or out == float("inf") or out == float("-inf"):
            return float(default)
        return out
    except Exception:
        return float(default)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _mcap_bucket(mcap: float) -> str:
    if mcap <= 0:
        return "missing"
    if mcap < 25_000:
        return "<25k"
    if mcap < 50_000:
        return "25k_50k"
    if mcap < 100_000:
        return "50k_100k"
    if mcap < 200_000:
        return "100k_200k"
    return ">=200k"


def _is_broad_profit_candidate(token: dict[str, Any], gate_profile: str | None = None) -> bool:
    profile = _norm(gate_profile or token.get("gate_profile") or token.get("sniper_gate_profile"))
    lane = _norm(token.get("entry_lane"))
    return (
        profile in {"", "pumpswap_profit_broad", "pumpswap_profit_research"}
        and lane not in {
            "pump_early_green_candle_sniper",
            "pump_early_pumpswap_breakout_probe",
        }
    )


def evaluate_profit_pnl_guard(
    token: dict[str, Any],
    *,
    gate_profile: str | None = None,
    prime: bool = False,
    meteor_prime: bool = False,
    breakout_probe: bool = False,
) -> ProfitPnlGuardDecision:
    if not bool(getattr(CFG, "PUMP_EARLY_PROFIT_PNL_GUARD_ENABLED", True)):
        return ProfitPnlGuardDecision(allowed=True)
    if prime or meteor_prime or breakout_probe:
        return ProfitPnlGuardDecision(allowed=True)
    if not _is_broad_profit_candidate(token, gate_profile):
        return ProfitPnlGuardDecision(allowed=True)

    price5m = _to_float(token.get("price_pct_5m"), 0.0)
    mcap = _to_float(token.get("market_cap_usd"), 0.0)
    txns5m = _to_float(token.get("txns_last_5m"), 0.0)
    failures: list[str] = []

    jackpot_min = _to_float(getattr(CFG, "PUMP_EARLY_PROFIT_PNL_GUARD_JACKPOT_PRICE5M_MIN", 180.0), 180.0)
    if jackpot_min > 0 and price5m >= jackpot_min:
        return ProfitPnlGuardDecision(allowed=True)

    mcap_bucket = _mcap_bucket(mcap)
    weak_price_max = _to_float(
        getattr(CFG, "PUMP_EARLY_PROFIT_PNL_GUARD_50K_100K_WEAK_PRICE5M_MAX", 25.0),
        25.0,
    )
    weak_min_txns = _to_float(
        getattr(CFG, "PUMP_EARLY_PROFIT_PNL_GUARD_50K_100K_WEAK_MIN_TXNS_5M", 700.0),
        700.0,
    )
    if mcap_bucket == "50k_100k" and price5m < weak_price_max and txns5m >= weak_min_txns:
        failures.append("pnl_guard_50k_100k_weak_high_txns")

    local_top_min_mcap = _to_float(
        getattr(CFG, "PUMP_EARLY_PROFIT_PNL_GUARD_LOCAL_TOP_MIN_MCAP_USD", 25_000.0),
        25_000.0,
    )
    if mcap >= local_top_min_mcap and 50.0 <= price5m < 100.0:
        failures.append("pnl_guard_local_top_50_100")

    mid_min_mcap = _to_float(
        getattr(CFG, "PUMP_EARLY_PROFIT_PNL_GUARD_MID_MOMENTUM_MIN_MCAP_USD", 50_000.0),
        50_000.0,
    )
    if mcap >= mid_min_mcap and 25.0 <= price5m < 50.0:
        failures.append("pnl_guard_mid_momentum_25_50")

    if not failures:
        return ProfitPnlGuardDecision(allowed=True)
    return ProfitPnlGuardDecision(
        allowed=False,
        failures=tuple(failures),
        blocked_bucket=failures[0],
    )


__all__ = ["ProfitPnlGuardDecision", "evaluate_profit_pnl_guard"]
