from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analytics.lane_policy_categories import POLICY_MOONSHOT_MICRO_LOTTERY
from analytics.report_utils import (
    address_of,
    boolish,
    fnum,
    load_candidate_outcomes,
    load_deduped_positions,
    load_runtime_events,
    metrics_dir,
    write_json,
)
from config.config import CFG, PROJECT_ROOT
from runtime.paper_entry_policy import entry_config
from ml.lane_taxonomy import LANE_MOONSHOT_MICRO_LOTTERY
from ml.labels import moonshot_execution_label


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


def _cfg_int(cfg: Any, key: str, default: int) -> int:
    value = getattr(cfg, key, default)
    if value in (None, ""):
        return int(default)
    try:
        return int(float(value))
    except Exception:
        return int(default)


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


def _mcap_known(row: dict[str, Any]) -> bool:
    value = _first(row, "market_cap_usd", "buy_market_cap_usd", "mcap")
    if value in (None, ""):
        return False
    return _field_float(row, "market_cap_usd", "buy_market_cap_usd", "mcap", default=0.0) > 0.0


def _relaxed_ultralow_moonshot(row: dict[str, Any], *, cfg: Any = CFG) -> bool:
    min_price5m = float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M", 300.0) or 300.0)
    min_txns = float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MIN_TXNS_5M", 80) or 80)
    max_age = float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MAX_AGE_MIN", 10.0) or 10.0)
    return (
        _field_float(row, "price_pct_5m", "buy_price_pct_5m", "price5m") > min_price5m
        and _field_float(row, "txns_last_5m", "buy_txns_last_5m", "txns_5m") >= min_txns
        and _field_float(row, "queue_age_minutes", "age_minutes", "age_min", "token_age_min", default=999.0) <= max_age
        and _mcap_known(row)
        and not _toxic(row)
    )


def _route_proxy_row(row: dict[str, Any]) -> bool:
    if boolish(_first(row, "moonshot_micro_lottery_route_proxy", "route_proxy"), False):
        return True
    route_value = _first(row, "has_jupiter_route", "route_ok", "route_available")
    return route_value is not None and not boolish(route_value, False)


def _liquidity_proxy_row(row: dict[str, Any]) -> bool:
    return boolish(_first(row, "liquidity_is_proxy", "liquidity_usd_is_proxy", "buy_liquidity_is_proxy"), False)


def _extreme_hot_queue(row: dict[str, Any], *, cfg: Any = CFG) -> bool:
    return (
        _field_float(row, "txns_last_5m", "buy_txns_last_5m", "txns_5m")
        >= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_EXTREME_MIN_TXNS_5M", 300) or 300)
        and _field_float(row, "queue_age_minutes", "age_minutes", "age_min", default=999.0)
        <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MAX_AGE_MIN", 10.0) or 10.0)
    )


def _extreme_cluster_bad_override(row: dict[str, Any], *, cfg: Any = CFG) -> bool:
    if not bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_BUY_ENABLED", False)):
        return False
    if not _cluster_bad(row) or _toxic(row) or not _mcap_known(row):
        return False
    if bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_REQUIRE_REAL_LIQUIDITY", True)):
        if _liquidity_proxy_row(row):
            return False
        if not boolish(_first(row, "has_jupiter_route", "route_ok", "route_available"), False):
            return False
        if _field_float(row, "liquidity_usd", "buy_liquidity_usd") < float(
            getattr(cfg, "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MIN_REAL_LIQUIDITY_USD", 10_000.0)
            or 10_000.0
        ):
            return False
        if _field_float(row, "price_impact_pct", "buy_price_impact_pct", "jupiter_price_impact_pct") > float(
            getattr(cfg, "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MAX_PRICE_IMPACT_PCT", 12.0)
            or 12.0
        ):
            return False
    max_age = float(
        getattr(
            cfg,
            "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MAX_AGE_MIN",
            getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MAX_AGE_MIN", 10.0),
        )
        or 10.0
    )
    return (
        _field_float(row, "price_pct_5m", "buy_price_pct_5m", "price5m")
        >= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MIN_PRICE5M", 500.0) or 500.0)
        and _field_float(row, "txns_last_5m", "buy_txns_last_5m", "txns_5m")
        >= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MIN_TXNS_5M", 80) or 80)
        and _field_float(row, "queue_age_minutes", "age_minutes", "age_min", "token_age_min", default=999.0)
        <= max_age
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


def _observed_shadow_move(row: dict[str, Any]) -> float:
    return max(
        _field_float(row, "observed_shadow_move_pct", "observed_peak_after_seen"),
        _field_float(row, "shadow_max_pnl_pct_seen", "max_pnl_pct_seen", "peak_pnl_pct"),
        _field_float(row, "shadow_pnl_pct", "pnl_pct", "target_total_pnl_pct"),
    )


def _candidate_partial_move(row: dict[str, Any]) -> float:
    return _field_float(row, "candidate_partial_pnl_pct", "partial_pnl_pct", "shadow_partial_pnl_pct")


def _confirmation_reason(row: dict[str, Any], *, cfg: Any = CFG) -> str | None:
    confirmation_pnl = float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL", 75.0) or 75.0)
    if _observed_shadow_move(row) >= confirmation_pnl:
        return f"observed_shadow_move_{confirmation_pnl:g}"
    if _candidate_partial_move(row) >= confirmation_pnl:
        return f"candidate_partial_{confirmation_pnl:g}"
    if _relaxed_ultralow_moonshot(row, cfg=cfg):
        return "relaxed_ultralow_moonshot"
    price5m = _field_float(row, "price_pct_5m", "buy_price_pct_5m", "price5m")
    txns = _field_float(row, "txns_last_5m", "buy_txns_last_5m", "txns_5m")
    mcap_raw = _first(row, "market_cap_usd", "buy_market_cap_usd", "mcap")
    if price5m >= 500.0 and txns >= 300.0 and mcap_raw not in (None, ""):
        return "extreme_price5m_txns_mcap_known"
    if _norm(_first(row, "shadow_followup_signal", "followup_signal")) == "moonshot":
        return "shadow_followup_signal_moonshot"
    return None


def evaluate_moonshot_micro_lottery(
    row: dict[str, Any],
    *,
    dry_run: bool,
    live: bool,
    cfg: Any = CFG,
) -> MoonshotMicroLotteryDecision:
    cfg = entry_config(cfg, dry_run=dry_run, live=live)
    raw_amount = float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL", 0.001) or 0.001)
    amount = max(raw_amount, 0.0)
    raw_cluster_tail_amount = float(
        getattr(cfg, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_AMOUNT_SOL", raw_amount) or raw_amount
    )
    cluster_tail_amount = max(raw_cluster_tail_amount, 0.0)
    risky_cluster_amount = float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_AMOUNT_SOL", 0.0005) or 0.0005)

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
            amount if amount_override is None else max(float(amount_override), 0.0),
            route_proxy=bool(route_proxy),
        )

    if not bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_ENABLED", True)):
        return decision(False, "moonshot_disabled", ["disabled"])
    if live or not dry_run:
        if not bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED", False)):
            return decision(False, "moonshot_live_disabled", ["live_disabled"])
    elif not bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_PAPER_ENABLED", True)):
        return decision(False, "moonshot_paper_disabled", ["paper_disabled"])

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
    confirmation = _confirmation_reason(row, cfg=cfg)
    extreme_cluster_allowed = _extreme_cluster_bad_override(row, cfg=cfg)
    if extreme_cluster_allowed and confirmation is None:
        confirmation = "extreme_cluster_bad_override"

    if not _source_ok(row):
        failures.append("source_not_allowed")
    max_age = float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MAX_AGE_MIN", 10.0) or 10.0)
    if not special_probe and age > max_age:
        failures.append(f"age_gt_{max_age:g}m")
    if not special_probe and txns < float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MIN_TXNS_5M", 80) or 80):
        failures.append("txns5m<80")
    if not special_probe and not _mcap_known(row):
        failures.append("mcap_missing")
    if mcap_raw not in (None, "") and mcap > float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MAX_MCAP_USD", 150_000.0) or 150_000.0):
        failures.append("mcap>150000")
    if (
        not special_probe
        and price5m <= float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M", 300.0) or 300.0)
        and not _extreme_hot_queue(row, cfg=cfg)
    ):
        failures.append("not_extreme_momentum")
    if _toxic(row):
        failures.append("toxic_initial_sell_pressure")
    risky_cluster_allowed = (
        bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_MODE_ENABLED", False))
        and 0.0 < risky_cluster_amount <= 0.0005
        and confirmation is not None
    )
    cluster_is_bad = _cluster_bad(row)
    if cluster_is_bad and not (risky_cluster_allowed or extreme_cluster_allowed):
        failures.append("cluster_bad")
    if failures:
        return decision(False, "moonshot_micro_lottery_shadow:" + ",".join(failures[:8]), failures, route_proxy=route_proxy)
    if bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_CONFIRMATION_REQUIRED", True)) and confirmation is None:
        shadow_reason = "moonshot_needs_confirmation"
        if cluster_tail:
            shadow_reason = "moonshot_needs_confirmation:cluster_tail_shadow"
        elif birth_velocity:
            shadow_reason = "moonshot_needs_confirmation:birth_velocity_shadow"
        elif late_proxy_momentum:
            shadow_reason = "moonshot_needs_confirmation:late_proxy_shadow"
        return decision(False, shadow_reason, ["confirmation_required"], route_proxy=route_proxy)
    if cluster_tail:
        if not bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_BUY_ENABLED", False)):
            return decision(False, "moonshot_needs_confirmation:cluster_tail_shadow", ["cluster_tail_buy_disabled"], route_proxy=route_proxy)
        return decision(
            True,
            "confirmed_moonshot_buy:extreme_cluster_bad" if extreme_cluster_allowed else "confirmed_moonshot_buy",
            [],
            route_proxy=route_proxy,
            amount_override=risky_cluster_amount if cluster_is_bad and risky_cluster_allowed else cluster_tail_amount,
        )
    risky_amount_override = risky_cluster_amount if cluster_is_bad and risky_cluster_allowed else None
    reason = "confirmed_moonshot_buy:extreme_cluster_bad" if extreme_cluster_allowed else "confirmed_moonshot_buy"
    if birth_velocity:
        return decision(True, reason, [], route_proxy=route_proxy, amount_override=risky_amount_override)
    if late_proxy_momentum:
        return decision(True, reason, [], route_proxy=route_proxy, amount_override=risky_amount_override)
    return decision(True, reason, [], route_proxy=route_proxy, amount_override=risky_amount_override)


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
    return fnum(_first(row, "total_pnl_pct", "realized_pnl_pct", "pnl_pct", "target_total_pnl_pct"), 0.0)


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


def _event(row: dict[str, Any]) -> str:
    return str(_first(row, "event_type", "event", "action", "decision_action") or "").strip().lower()


def _position_opened(row: dict[str, Any]) -> bool:
    return _first(row, "opened_at", "entry_price_usd", "buy_price_usd", "buy_amount_sol") is not None


def _unique_count(rows: list[dict[str, Any]]) -> int:
    addresses = {address_of(row) for row in rows if address_of(row)}
    return len(addresses) if addresses else len(rows)


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = address_of(row)
        if not key:
            out.append(row)
            continue
        normalized = key.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(row)
    return out


def _dedupe_rows_by_peak(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []
    for row in rows:
        key = address_of(row).strip().lower()
        if not key:
            passthrough.append(row)
            continue
        grouped.setdefault(key, []).append(row)

    out: list[dict[str, Any]] = []
    for group in grouped.values():
        winner = max(
            group,
            key=lambda item: (
                _peak(item),
                _field_float(item, "price_pct_5m", "buy_price_pct_5m", "price5m"),
                1 if boolish(item.get("moonshot_micro_lottery"), False) else 0,
            ),
        )
        merged = dict(winner)
        for row in group:
            for key, value in row.items():
                if merged.get(key) is None or (isinstance(merged.get(key), str) and not str(merged.get(key)).strip()):
                    merged[key] = value
        out.append(merged)
    return out + passthrough


def _bought_address_set(runtime_rows: list[dict[str, Any]], position_rows: list[dict[str, Any]]) -> set[str]:
    bought: set[str] = set()
    buy_events = {"actual_paper_buy", "buy", "bought", "buy_ok", "paper_buy"}
    for row in runtime_rows:
        if _event(row) in buy_events:
            addr = address_of(row)
            if addr:
                bought.add(addr.lower())
    for row in position_rows:
        addr = address_of(row)
        if addr and _position_opened(row):
            bought.add(addr.lower())
    return bought


def _count_label_field(labels: list[dict[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(str(label.get(field) or "unknown") for label in labels).items()))


def build_moonshot_micro_lottery_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    runtime_rows = load_runtime_events(root)
    outcome_rows = load_candidate_outcomes(root)
    position_rows = load_deduped_positions(root)
    rows = runtime_rows + outcome_rows + position_rows
    candidates = [
        row
        for row in rows
        if _is_moonshot_row(row)
        or (
            _source_ok(row)
            and (
                _field_float(row, "price_pct_5m", "buy_price_pct_5m") > 300.0
                or _extreme_hot_queue(row)
                or _birth_velocity_probe(row)
                or _late_proxy_momentum_probe(row)
                or _cluster_tail_probe(row)
            )
        )
    ]
    deduped_candidates = _dedupe_rows_by_peak(candidates)
    bought = _bought_address_set(runtime_rows, position_rows)
    labeled_candidates = [(row, moonshot_execution_label(row)) for row in deduped_candidates]
    theoretical_labels = [label for _, label in labeled_candidates if label.get("theoretical_moonshot")]
    executable_labels = [label for _, label in labeled_candidates if label.get("executable_moonshot")]
    missed_moonshot_pairs = [
        (row, label)
        for row, label in labeled_candidates
        if label.get("theoretical_moonshot") and address_of(row).lower() not in bought
    ]
    missed_moonshot_labels = [label for _, label in missed_moonshot_pairs]
    missed_peak100 = [label for label in missed_moonshot_labels if fnum(label.get("moonshot_peak_pct"), 0.0) >= 100.0]
    missed_peak500 = [label for label in missed_moonshot_labels if fnum(label.get("moonshot_peak_pct"), 0.0) >= 500.0]
    missed_peak1000 = [label for label in missed_moonshot_labels if fnum(label.get("moonshot_peak_pct"), 0.0) >= 1000.0]
    moonshot_rows = [row for row in rows if _is_moonshot_row(row)]
    moonshot_position_rows = [row for row in position_rows if _is_moonshot_row(row) and _position_opened(row)]
    moonshot_actual_buy_events = [row for row in runtime_rows if _is_moonshot_row(row) and _event(row) == "actual_paper_buy"]
    moonshot_legacy_buy_events = [
        row for row in runtime_rows if _is_moonshot_row(row) and _event(row) in {"buy", "bought", "paper_buy", "buy_ok"}
    ]
    buy_count = max(
        _unique_count(moonshot_position_rows),
        _unique_count(moonshot_actual_buy_events),
        _unique_count(moonshot_legacy_buy_events),
    )
    shadows = [row for row in moonshot_rows if "shadow" in _norm(_first(row, "reason", "action", "decision_action"))]
    result_rows = _dedupe_rows([row for row in position_rows + outcome_rows if _is_moonshot_row(row)])
    closed_pnls = [_pnl(row) for row in result_rows if _first(row, "total_pnl_pct", "realized_pnl_pct", "pnl_pct") is not None]
    peak100 = [row for row in result_rows if _peak(row) >= 100.0]
    peak500 = [row for row in result_rows if _peak(row) >= 500.0]
    peak1000 = [row for row in result_rows if _peak(row) >= 1000.0]
    missed_tail_candidates = [row for row in candidates if _peak(row) >= 100.0]
    cluster_tail_shadow = [
        row
        for row in candidates
        if _cluster_tail_probe(row)
        and not ("confirmed_moonshot_buy" in _norm(_first(row, "reason", "green_sniper_reason", "entry_reason")))
    ]
    birth_velocity_shadow = [
        row
        for row in candidates
        if _birth_velocity_probe(row)
        and not ("confirmed_moonshot_buy" in _norm(_first(row, "reason", "green_sniper_reason", "entry_reason")))
    ]
    late_proxy_shadow = [
        row
        for row in candidates
        if _late_proxy_momentum_probe(row)
        and not ("confirmed_moonshot_buy" in _norm(_first(row, "reason", "green_sniper_reason", "entry_reason")))
    ]
    confirmed_buy = [
        row
        for row in moonshot_rows
        if "confirmed_moonshot_buy" in _norm(_first(row, "reason", "green_sniper_reason", "entry_reason"))
        or boolish(row.get("moonshot_micro_lottery"), False)
    ]
    risky_cluster_shadow = [
        row
        for row in candidates
        if _cluster_bad(row)
        and not ("confirmed_moonshot_buy" in _norm(_first(row, "reason", "green_sniper_reason", "entry_reason")))
        and not boolish(row.get("moonshot_micro_lottery"), False)
    ]
    extreme_cluster_candidates = [row for row in candidates if _extreme_cluster_bad_override(row)]
    confirmed_buy_count = max(_unique_count(confirmed_buy), buy_count)
    route_proxy_buys = [row for row in confirmed_buy if _route_proxy_row(row)]
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "enabled": bool(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED", False)),
            "amount_sol": max(float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL", 0.001) or 0.001), 0.0),
            "max_open": _cfg_int(CFG, "MOONSHOT_MICRO_LOTTERY_MAX_OPEN", 0),
            "max_daily_buys": _cfg_int(CFG, "MOONSHOT_MICRO_LOTTERY_MAX_DAILY_BUYS", 0),
            "confirmation_required": bool(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_CONFIRMATION_REQUIRED", True)),
            "confirmation_pnl": float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL", 75.0) or 75.0),
            "max_age_min": float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_MAX_AGE_MIN", 10.0) or 10.0),
            "min_txns_5m": int(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_MIN_TXNS_5M", 80) or 80),
            "max_mcap_usd": float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_MAX_MCAP_USD", 150_000.0) or 150_000.0),
            "min_price5m": float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M", 300.0) or 300.0),
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
            "cluster_tail_buy_enabled": bool(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_BUY_ENABLED", False)),
            "extreme_cluster_buy_enabled": bool(
                getattr(CFG, "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_BUY_ENABLED", False)
            ),
            "extreme_cluster_min_price5m": float(
                getattr(CFG, "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MIN_PRICE5M", 500.0)
                or 500.0
            ),
            "extreme_cluster_min_txns_5m": int(
                getattr(CFG, "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MIN_TXNS_5M", 80)
                or 80
            ),
            "extreme_cluster_max_age_min": float(
                getattr(CFG, "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MAX_AGE_MIN", 10.0)
                or 10.0
            ),
            "extreme_cluster_require_real_liquidity": bool(
                getattr(CFG, "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_REQUIRE_REAL_LIQUIDITY", True)
            ),
            "extreme_cluster_min_real_liquidity_usd": float(
                getattr(CFG, "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MIN_REAL_LIQUIDITY_USD", 10_000.0)
                or 10_000.0
            ),
            "extreme_cluster_max_price_impact_pct": float(
                getattr(CFG, "MOONSHOT_MICRO_LOTTERY_EXTREME_CLUSTER_MAX_PRICE_IMPACT_PCT", 12.0)
                or 12.0
            ),
            "risky_cluster_mode_enabled": bool(
                getattr(CFG, "MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_MODE_ENABLED", False)
            ),
            "risky_cluster_amount_sol": max(
                float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_AMOUNT_SOL", 0.0005) or 0.0005),
                0.0,
            ),
            "cluster_tail_amount_sol": max(
                float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_AMOUNT_SOL", 0.001) or 0.001),
                0.0,
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
            "paper_only": bool(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_PAPER_ENABLED", True))
            and not bool(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED", False)),
        },
        "candidates_seen": len(candidates),
        "deduped_candidates_seen": len(deduped_candidates),
        "theoretical_moonshot_candidates": len(theoretical_labels),
        "executable_moonshot_candidates": len(executable_labels),
        "missed_moonshot_count": len(missed_moonshot_pairs),
        "missed_peak100": len(missed_peak100),
        "missed_peak500": len(missed_peak500),
        "missed_peak1000": len(missed_peak1000),
        "moonshot_blockers": _count_label_field(missed_moonshot_labels, "moonshot_blocker"),
        "moonshot_viability": _count_label_field(missed_moonshot_labels, "moonshot_viability"),
        "moonshot_route_viability": _count_label_field(missed_moonshot_labels, "moonshot_route_viability"),
        "moonshot_cluster_viability": _count_label_field(missed_moonshot_labels, "moonshot_cluster_viability"),
        "buys": buy_count,
        "shadows": len(shadows),
        "birth_velocity_candidates": sum(1 for row in candidates if _birth_velocity_probe(row)),
        "late_proxy_candidates": sum(1 for row in candidates if _late_proxy_momentum_probe(row)),
        "cluster_tail_candidates": sum(1 for row in candidates if _cluster_tail_probe(row)),
        "cluster_tail_shadow": len(cluster_tail_shadow),
        "confirmed_moonshot_buy": confirmed_buy_count,
        "late_proxy_shadow": len(late_proxy_shadow),
        "birth_velocity_shadow": len(birth_velocity_shadow),
        "risky_cluster_shadow": len(risky_cluster_shadow),
        "extreme_cluster_candidates": _unique_count(extreme_cluster_candidates),
        "route_proxy_buys": _unique_count(route_proxy_buys),
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
        "missed_moonshots": [
            {
                "address": address_of(row),
                "peak_pct": fnum(label.get("moonshot_peak_pct"), 0.0),
                "executable_moonshot": bool(label.get("executable_moonshot")),
                "viability": label.get("moonshot_viability"),
                "blocker": label.get("moonshot_blocker") or "unknown",
                "decision_reason": label.get("moonshot_decision_reason"),
                "route_viability": label.get("moonshot_route_viability"),
                "liquidity_viability": label.get("moonshot_liquidity_viability"),
                "cluster_viability": label.get("moonshot_cluster_viability"),
                "time_to_peak_min": label.get("moonshot_time_to_peak_min"),
                "time_to_peak_source": label.get("moonshot_time_to_peak_source"),
                "amount_sol": label.get("moonshot_amount_sol"),
                "paper_only": bool(label.get("moonshot_paper_only")),
            }
            for row, label in sorted(
                missed_moonshot_pairs,
                key=lambda item: fnum(item[1].get("moonshot_peak_pct"), 0.0),
                reverse=True,
            )[:100]
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
