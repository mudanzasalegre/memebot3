from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, median
from typing import Any, Iterable


@dataclass(frozen=True)
class ExitProfile:
    name: str
    partial_trigger_pct: float
    partial_fraction: float
    lock_floor_pct: float
    max_giveback_pct: float


PROFILES = (
    ExitProfile("defensive_4pct_80", 4.0, 0.80, 20.0, 5.0),
    ExitProfile("balanced_12pct_50", 12.0, 0.50, 30.0, 12.0),
    ExitProfile("runner_18pct_35", 18.0, 0.35, 50.0, 25.0),
    ExitProfile("moonbag_25pct_25", 25.0, 0.25, 80.0, 35.0),
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def simulate_profile(row: dict[str, Any], profile: ExitProfile) -> float:
    peak = max(_f(row.get("max_pnl_pct_seen")), _f(row.get("highest_pnl_pct")), _f(row.get("total_pnl_pct")))
    final = _f(row.get("total_pnl_pct"))
    if peak < profile.partial_trigger_pct:
        return final
    partial_pnl = profile.partial_trigger_pct * profile.partial_fraction
    runner_fraction = 1.0 - profile.partial_fraction
    runner_exit = final
    if peak >= profile.lock_floor_pct:
        runner_exit = max(profile.lock_floor_pct, peak - profile.max_giveback_pct)
    return partial_pnl + runner_exit * runner_fraction


def compare_exit_profiles(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    data = list(rows)
    out: dict[str, dict[str, Any]] = {}
    for profile in PROFILES:
        pnls = [simulate_profile(row, profile) for row in data]
        out[profile.name] = {
            "avg_realized_pnl": mean(pnls) if pnls else 0.0,
            "median_realized_pnl": median(pnls) if pnls else 0.0,
            "total_realized_pnl": sum(pnls),
            "max_drawdown_proxy": min(pnls) if pnls else 0.0,
            "runner_capture_rate": None,
            "trades_over_100": sum(1 for v in pnls if v >= 100.0),
            "trades_over_300": sum(1 for v in pnls if v >= 300.0),
            "profit_given_back": sum(max(0.0, _f(row.get("max_pnl_pct_seen")) - pnl) for row, pnl in zip(data, pnls)),
        }
    return out


__all__ = ["ExitProfile", "PROFILES", "compare_exit_profiles", "simulate_profile"]
