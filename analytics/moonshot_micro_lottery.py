from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analytics.lane_policy_categories import POLICY_MOONSHOT_MICRO_LOTTERY
from analytics.report_utils import (
    address_of,
    boolish,
    fnum,
    load_candidate_outcomes,
    load_paper_positions,
    load_runtime_events,
    load_sqlite_positions,
    metrics_dir,
    write_json,
)
from config.config import CFG, PROJECT_ROOT
from ml.lane_taxonomy import LANE_MOONSHOT_MICRO_LOTTERY


REPORT_JSON = "moonshot_micro_lottery_report.json"


@dataclass(frozen=True)
class MoonshotMicroLotteryDecision:
    allowed: bool
    reason: str
    failures: tuple[str, ...]
    amount_sol: float
    route_proxy: bool = False
    lane: str = LANE_MOONSHOT_MICRO_LOTTERY


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _field_float(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    return fnum(_first(row, *keys), default)


def _source(row: dict[str, Any]) -> str:
    for key in ("source", "discovered_via", "entry_source"):
        value = _norm(row.get(key))
        if value in {"candidate_decision", "candidate_stage", "candidate_outcome", "candidate_partial", "research_shadow", "live_trade"}:
            continue
        if value:
            return value
    return ""


def _address_looks_pumpfun(row: dict[str, Any]) -> bool:
    return address_of(row).strip().lower().endswith("pump")


def _source_ok(row: dict[str, Any]) -> bool:
    src = _source(row)
    gate = _norm(_first(row, "gate_profile", "sniper_gate_profile", "entry_subtype"))
    reason = _norm(_first(row, "reason", "green_sniper_reason", "sniper_gate_failures"))
    return (
        src in {"pumpfun", "green_sniper_birth_probe"}
        or "green_sniper_birth_probe" in gate
        or "green_sniper_birth_probe" in reason
        or _address_looks_pumpfun(row)
    )


def _toxic(row: dict[str, Any]) -> bool:
    if boolish(_first(row, "toxic_initial_sell_pressure", "initial_sell_pressure_toxic"), False):
        return True
    reason = _norm(_first(row, "reason", "green_sniper_reason", "reject_reason"))
    return "toxic_initial_sell_pressure" in reason


def _cluster_bad(row: dict[str, Any]) -> bool:
    value = _first(row, "cluster_bad", "helius_cluster_bad")
    if value is not None and boolish(value, False):
        return True
    reason = _norm(
        _first(
            row,
            "reason",
            "green_sniper_reason",
            "sniper_gate_failures",
            "sniper_research_subprofile_failures",
        )
    )
    return "cluster_bad" in reason


def _extreme_hot_queue(row: dict[str, Any], *, cfg: Any = CFG) -> bool:
    return (
        _field_float(row, "txns_last_5m", "buy_txns_last_5m", "txns_5m")
        >= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_EXTREME_MIN_TXNS_5M", 300) or 300)
        and _field_float(row, "queue_age_minutes", "age_minutes", "age_min", default=999.0)
        <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MAX_AGE_MIN", 6.0) or 6.0)
    )


def _birth_velocity_probe(row: dict[str, Any], *, cfg: Any = CFG) -> bool:
    if not bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_ENABLED", True)):
        return False
    reason = _norm(_first(row, "reason", "green_sniper_reason", "sniper_gate_failures"))
    price5m = _field_float(row, "price_pct_5m", "buy_price_pct_5m", "price5m")
    txns = _field_float(row, "txns_last_5m", "buy_txns_last_5m", "txns_5m")
    mcap = _field_float(row, "market_cap_usd", "buy_market_cap_usd", "mcap", default=999_999_999.0)
    age = _field_float(row, "age_minutes", "age_min", "token_age_min", "queue_age_minutes", default=999.0)
    volume = _field_float(row, "volume_24h_usd", "volume_usd_24h", "buy_volume_24h_usd", default=0.0)
    return (
        "paper_birth_probe" in reason
        and "weak_buy_sell_ratio" not in reason
        and float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MIN_PRICE5M", 25.0) or 25.0)
        <= price5m
        <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MAX_PRICE5M", 120.0) or 120.0)
        and float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MIN_TXNS_5M", 15) or 15)
        <= txns
        <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MAX_TXNS_5M", 50) or 50)
        and mcap <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MAX_MCAP_USD", 10_000.0) or 10_000.0)
        and age <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MAX_AGE_MIN", 2.0) or 2.0)
        and float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MIN_VOLUME_24H", 800.0) or 800.0)
        <= volume
        <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MAX_VOLUME_24H", 1500.0) or 1500.0)
    )


def _late_proxy_momentum_probe(row: dict[str, Any], *, cfg: Any = CFG) -> bool:
    if not bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_LATE_PROXY_ENABLED", True)):
        return False
    price5m = _field_float(row, "price_pct_5m", "buy_price_pct_5m", "price5m")
    txns = _field_float(row, "txns_last_5m", "buy_txns_last_5m", "txns_5m")
    mcap = _field_float(row, "market_cap_usd", "buy_market_cap_usd", "mcap", default=0.0)
    age = _field_float(row, "age_minutes", "age_min", "token_age_min", "queue_age_minutes", default=999.0)
    reason = _norm(_first(row, "reason", "green_sniper_reason", "sniper_gate_failures"))
    return (
        "weak_buy_sell_ratio" not in reason
        and float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_LATE_PROXY_MIN_PRICE5M", 300.0) or 300.0)
        <= price5m
        <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_LATE_PROXY_MAX_PRICE5M", 800.0) or 800.0)
        and float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_LATE_PROXY_MIN_TXNS_5M", 15) or 15)
        <= txns
        <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_LATE_PROXY_MAX_TXNS_5M", 40) or 40)
        and float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_LATE_PROXY_MIN_MCAP_USD", 15_000.0) or 15_000.0)
        <= mcap
        <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_LATE_PROXY_MAX_MCAP_USD", 25_000.0) or 25_000.0)
        and age <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_LATE_PROXY_MAX_AGE_MIN", 12.0) or 12.0)
    )


def _cluster_tail_probe(row: dict[str, Any], *, cfg: Any = CFG) -> bool:
    if not bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_ENABLED", True)):
        return False
    if not _cluster_bad(row):
        return False
    if _toxic(row):
        return False
    age = _field_float(row, "age_minutes", "age_min", "token_age_min", "queue_age_minutes", default=999.0)
    liq = _field_float(row, "liquidity_usd", "buy_liquidity_usd", default=0.0)
    mcap = _field_float(row, "market_cap_usd", "buy_market_cap_usd", "mcap", default=0.0)
    volume = _field_float(row, "volume_24h_usd", "volume_usd_24h", "buy_volume_24h_usd", default=0.0)
    return (
        age <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_MAX_AGE_MIN", 5.0) or 5.0)
        and liq >= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_MIN_LIQUIDITY_USD", 10_000.0) or 10_000.0)
        and float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_MIN_MCAP_USD", 20_000.0) or 20_000.0)
        <= mcap
        <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_MAX_MCAP_USD", 150_000.0) or 150_000.0)
        and volume >= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_MIN_VOLUME_24H", 20_000.0) or 20_000.0)
    )


def evaluate_moonshot_micro_lottery(
    row: dict[str, Any],
    *,
    dry_run: bool,
    live: bool,
    cfg: Any = CFG,
) -> MoonshotMicroLotteryDecision:
    amount = min(float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL", 0.002) or 0.002), 0.005)
    cluster_tail_amount = min(
        float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_AMOUNT_SOL", 0.001) or 0.001),
        0.005,
    )

    def decision(
        allowed: bool,
        reason: str,
        failures: list[str] | tuple[str, ...],
        *,
        route_proxy: bool = False,
        amount_override: float | None = None,
    ) -> MoonshotMicroLotteryDecision:
        return MoonshotMicroLotteryDecision(
            bool(allowed),
            str(reason),
            tuple(failures),
            amount if amount_override is None else min(max(float(amount_override), 0.0), 0.005),
            route_proxy=bool(route_proxy),
        )

    if not bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_ENABLED", True)):
        return decision(False, "moonshot_disabled", ["disabled"])
    if live or not dry_run or not bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_PAPER_ENABLED", True)):
        return decision(False, "moonshot_paper_only", ["paper_only"])
    if bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED", False)):
        return decision(False, "moonshot_live_flag_blocked", ["live_flag_enabled"])
    if amount > 0.005:
        return decision(False, "moonshot_amount_cap", ["amount>0.005"])

    failures: list[str] = []
    age = _field_float(row, "queue_age_minutes", "age_minutes", "age_min", "token_age_min", default=999.0)
    price5m = _field_float(row, "price_pct_5m", "buy_price_pct_5m", "price5m")
    txns = _field_float(row, "txns_last_5m", "buy_txns_last_5m", "txns_5m")
    mcap_raw = _first(row, "market_cap_usd", "buy_market_cap_usd", "mcap")
    mcap = _field_float(row, "market_cap_usd", "buy_market_cap_usd", "mcap", default=0.0)
    has_route = boolish(_first(row, "has_jupiter_route", "route_ok", "route_available"), False)
    route_proxy = not has_route
    birth_velocity = _birth_velocity_probe(row, cfg=cfg)
    late_proxy_momentum = _late_proxy_momentum_probe(row, cfg=cfg)
    cluster_tail = _cluster_tail_probe(row, cfg=cfg)
    special_probe = birth_velocity or late_proxy_momentum or cluster_tail

    if not _source_ok(row):
        failures.append("source_not_allowed")
    if not special_probe and age > float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MAX_AGE_MIN", 6.0) or 6.0):
        failures.append("age_gt_6m")
    if not special_probe and txns < float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MIN_TXNS_5M", 80) or 80):
        failures.append("txns5m<80")
    if mcap_raw not in (None, "") and mcap > float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MAX_MCAP_USD", 150_000.0) or 150_000.0):
        failures.append("mcap>150000")
    if (
        not special_probe
        and price5m < float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M", 500.0) or 500.0)
        and not _extreme_hot_queue(row, cfg=cfg)
    ):
        failures.append("not_extreme_momentum")
    if _toxic(row):
        failures.append("toxic_initial_sell_pressure")
    if _cluster_bad(row) and not cluster_tail:
        failures.append("cluster_bad")
    if failures:
        return decision(False, "moonshot_micro_lottery_shadow:" + ",".join(failures[:8]), failures, route_proxy=route_proxy)
    if cluster_tail:
        return decision(
            True,
            "moonshot_cluster_tail_probe",
            [],
            route_proxy=route_proxy,
            amount_override=cluster_tail_amount,
        )
    if birth_velocity:
        return decision(True, "moonshot_birth_velocity_probe", [], route_proxy=route_proxy)
    if late_proxy_momentum:
        return decision(True, "moonshot_late_proxy_momentum", [], route_proxy=route_proxy)
    return decision(True, "moonshot_micro_lottery", [], route_proxy=route_proxy)


def apply_moonshot_micro_lottery_context(
    row: dict[str, Any],
    decision: MoonshotMicroLotteryDecision,
) -> dict[str, Any]:
    row["entry_lane"] = decision.lane
    row["gate_profile"] = "moonshot_micro_lottery"
    row["profit_lane_tier"] = decision.lane
    row["lane_policy_category"] = POLICY_MOONSHOT_MICRO_LOTTERY
    row["green_sniper_reason"] = decision.reason
    row["moonshot_micro_lottery"] = int(bool(decision.allowed))
    row["moonshot_micro_lottery_amount_sol"] = float(decision.amount_sol)
    row["moonshot_micro_lottery_route_proxy"] = int(bool(decision.route_proxy))
    row["route_proxy"] = int(bool(decision.route_proxy))
    row["live_profit_gate_failed_count"] = 0
    row["live_profit_gate_failures"] = ""
    row["live_profit_gate_profile"] = "moonshot_micro_lottery"
    row["sniper_gate_profile"] = "moonshot_micro_lottery"
    row["runner_exit_profile"] = "moonshot_micro_lottery"
    return row


def _pnl(row: dict[str, Any]) -> float:
    return fnum(_first(row, "realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct"), 0.0)


def _peak(row: dict[str, Any]) -> float:
    return max(
        fnum(_first(row, "highest_pnl_pct", "max_pnl_pct_seen", "peak_pnl_pct", "observed_peak_after_seen"), 0.0),
        _pnl(row),
    )


def _is_moonshot_row(row: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(_first(row, key) or "")
        for key in ("entry_lane", "gate_profile", "profit_lane_tier", "reason", "green_sniper_reason")
    ).lower()
    return "moonshot_micro_lottery" in haystack


def build_moonshot_micro_lottery_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_runtime_events(root) + load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    candidates = [
        row
        for row in rows
        if _is_moonshot_row(row)
        or (
            _source_ok(row)
            and (
                _field_float(row, "price_pct_5m", "buy_price_pct_5m") >= 500.0
                or _extreme_hot_queue(row)
                or _birth_velocity_probe(row)
                or _late_proxy_momentum_probe(row)
                or _cluster_tail_probe(row)
            )
        )
    ]
    moonshot_rows = [row for row in rows if _is_moonshot_row(row)]
    buys = [
        row
        for row in moonshot_rows
        if str(_first(row, "event_type", "action", "decision_action") or "").strip().lower() in {"buy", "bought", "paper_buy", "trade_close", ""}
    ]
    shadows = [row for row in moonshot_rows if "shadow" in _norm(_first(row, "reason", "action", "decision_action"))]
    closed_pnls = [_pnl(row) for row in moonshot_rows if _first(row, "realized_pnl_pct", "total_pnl_pct", "pnl_pct") is not None]
    peak100 = [row for row in moonshot_rows if _peak(row) >= 100.0]
    peak500 = [row for row in moonshot_rows if _peak(row) >= 500.0]
    peak1000 = [row for row in moonshot_rows if _peak(row) >= 1000.0]
    missed_tail_candidates = [row for row in candidates if _peak(row) >= 100.0]
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "enabled": bool(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED", False)),
            "amount_sol": min(float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL", 0.002) or 0.002), 0.005),
            "max_open": int(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_MAX_OPEN", 1) or 1),
            "max_daily_buys": int(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS", 3) or 3),
            "birth_velocity_enabled": bool(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_ENABLED", True)),
            "birth_velocity_price5m": [
                float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MIN_PRICE5M", 25.0) or 25.0),
                float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MAX_PRICE5M", 120.0) or 120.0),
            ],
            "birth_velocity_txns5m": [
                int(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MIN_TXNS_5M", 15) or 15),
                int(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MAX_TXNS_5M", 50) or 50),
            ],
            "birth_velocity_volume24h": [
                float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MIN_VOLUME_24H", 800.0) or 800.0),
                float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_BIRTH_VELOCITY_MAX_VOLUME_24H", 1500.0) or 1500.0),
            ],
            "late_proxy_enabled": bool(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_LATE_PROXY_ENABLED", True)),
            "cluster_tail_enabled": bool(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_ENABLED", True)),
            "cluster_tail_amount_sol": min(
                float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_AMOUNT_SOL", 0.001) or 0.001),
                0.005,
            ),
            "cluster_tail_min_liquidity_usd": float(
                getattr(CFG, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_MIN_LIQUIDITY_USD", 10_000.0)
                or 10_000.0
            ),
            "cluster_tail_min_mcap_usd": float(
                getattr(CFG, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_MIN_MCAP_USD", 20_000.0)
                or 20_000.0
            ),
            "cluster_tail_min_volume_24h": float(
                getattr(CFG, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_MIN_VOLUME_24H", 20_000.0)
                or 20_000.0
            ),
        },
        "candidates_seen": len(candidates),
        "buys": len(buys),
        "shadows": len(shadows),
        "birth_velocity_candidates": sum(1 for row in candidates if _birth_velocity_probe(row)),
        "late_proxy_candidates": sum(1 for row in candidates if _late_proxy_momentum_probe(row)),
        "cluster_tail_candidates": sum(1 for row in candidates if _cluster_tail_probe(row)),
        "peak100_captured": len(peak100),
        "peak500_captured": len(peak500),
        "peak1000_captured": len(peak1000),
        "loss_count": sum(1 for value in closed_pnls if value < 0.0),
        "avg_pnl": round(sum(closed_pnls) / len(closed_pnls), 3) if closed_pnls else 0.0,
        "max_loss": round(min(closed_pnls), 3) if closed_pnls else 0.0,
        "tail_capture_ratio": round(len(peak100) / len(missed_tail_candidates), 4) if missed_tail_candidates else 0.0,
        "median_pnl": round(statistics.median(closed_pnls), 3) if closed_pnls else 0.0,
        "samples": [
            {
                "address": address_of(row),
                "peak_pct": _peak(row),
                "pnl_pct": _pnl(row),
                "reason": _first(row, "reason", "green_sniper_reason"),
            }
            for row in moonshot_rows[:50]
        ],
    }


def write_moonshot_micro_lottery_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_moonshot_micro_lottery_report(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    return report


__all__ = [
    "MoonshotMicroLotteryDecision",
    "apply_moonshot_micro_lottery_context",
    "build_moonshot_micro_lottery_report",
    "evaluate_moonshot_micro_lottery",
    "write_moonshot_micro_lottery_report",
]
