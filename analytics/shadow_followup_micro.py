from __future__ import annotations

import collections
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analytics.lane_policy_categories import POLICY_SHADOW_FOLLOWUP_MICRO
from analytics.lane_sizing import LANE_SHADOW_FOLLOWUP_MICRO
from analytics.risk_guards import evaluate_pre_entry_risk
from analytics.report_utils import (
    address_of,
    boolish,
    fnum,
    load_candidate_outcomes,
    load_runtime_events,
    metrics_dir,
    write_json,
)
from config.config import CFG, PROJECT_ROOT


@dataclass(frozen=True)
class ShadowFollowupMicroDecision:
    allowed: bool
    reason: str
    failures: tuple[str, ...]
    amount_sol: float
    route_proxy: bool = False
    lane: str = LANE_SHADOW_FOLLOWUP_MICRO


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _cfg_value(cfg: Any, key: str, default: Any) -> Any:
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def _cfg_float(cfg: Any, key: str, default: float) -> float:
    return fnum(_cfg_value(cfg, key, default), default)


def _cfg_int(cfg: Any, key: str, default: int) -> int:
    value = _cfg_value(cfg, key, default)
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _cfg_bool(cfg: Any, key: str, default: bool) -> bool:
    return boolish(_cfg_value(cfg, key, default), default)


def _cap_reached(count: int, cap: int) -> bool:
    return cap > 0 and count >= cap


def _real_liquidity_breakout_trigger(row: dict[str, Any], *, cfg: Any = CFG) -> str | None:
    if not _cfg_bool(cfg, "SHADOW_FOLLOWUP_REAL_LIQUIDITY_BREAKOUT_ENABLED", True):
        return None
    route_ok = boolish(_first(row, "has_jupiter_route", "route_ok", "route_available"), False)
    if not route_ok:
        return None
    if boolish(_first(row, "liquidity_is_proxy", "liquidity_usd_is_proxy", "buy_liquidity_is_proxy"), False):
        return None

    liq = fnum(_first(row, "liquidity_usd", "buy_liquidity_usd"), 0.0)
    txns = fnum(_first(row, "txns_last_5m", "buy_txns_last_5m", "txns_5m"), 0.0)
    volume = fnum(_first(row, "volume_24h_usd", "volume_usd_24h", "buy_volume_24h_usd"), 0.0)
    mcap = fnum(_first(row, "market_cap_usd", "buy_market_cap_usd", "mcap"), 0.0)
    age = fnum(_first(row, "age_minutes", "age_min", "token_age_min", "queue_age_minutes"), 999.0)
    impact = fnum(_first(row, "price_impact_pct", "buy_price_impact_pct", "jupiter_price_impact_pct"), 0.0)
    price5m = fnum(_first(row, "price_pct_5m", "buy_price_pct_5m", "price5m"), 0.0)
    rank = fnum(_first(row, "rank_score", "research_rank_score", "research_rank_canary_rank_score"), 0.0)
    score_total = fnum(_first(row, "score_total", "total_score"), 0.0)

    min_rank = _cfg_float(cfg, "SHADOW_FOLLOWUP_REAL_LIQUIDITY_MIN_RANK_SCORE", 50.0)
    min_score = _cfg_float(cfg, "SHADOW_FOLLOWUP_REAL_LIQUIDITY_MIN_SCORE_TOTAL", 35.0)
    if rank < min_rank and score_total < min_score:
        return None
    if liq < _cfg_float(cfg, "SHADOW_FOLLOWUP_REAL_LIQUIDITY_MIN_USD", 10_000.0):
        return None
    if txns < _cfg_float(cfg, "SHADOW_FOLLOWUP_REAL_LIQUIDITY_MIN_TXNS_5M", 300.0):
        return None
    if volume < _cfg_float(cfg, "SHADOW_FOLLOWUP_REAL_LIQUIDITY_MIN_VOLUME_24H", 15_000.0):
        return None
    if mcap < _cfg_float(cfg, "SHADOW_FOLLOWUP_REAL_LIQUIDITY_MIN_MCAP_USD", 4_000.0):
        return None
    if mcap > _cfg_float(cfg, "SHADOW_FOLLOWUP_REAL_LIQUIDITY_MAX_MCAP_USD", 120_000.0):
        return None
    if age > _cfg_float(cfg, "SHADOW_FOLLOWUP_REAL_LIQUIDITY_MAX_AGE_MIN", 45.0):
        return None
    if impact > _cfg_float(cfg, "SHADOW_FOLLOWUP_REAL_LIQUIDITY_MAX_PRICE_IMPACT_PCT", 12.0):
        return None
    if price5m < _cfg_float(cfg, "SHADOW_FOLLOWUP_REAL_LIQUIDITY_MIN_PRICE5M", -30.0):
        return None
    if price5m > _cfg_float(cfg, "SHADOW_FOLLOWUP_REAL_LIQUIDITY_MAX_PRICE5M", 180.0):
        return None
    return "real_liquidity_breakout"


def _parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        out = value
    else:
        raw = str(value or "").replace("Z", "+00:00").strip()
        if not raw:
            return None
        try:
            out = dt.datetime.fromisoformat(raw)
        except Exception:
            return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=dt.timezone.utc)
    return out.astimezone(dt.timezone.utc)


def _age_since_seen_min(row: dict[str, Any]) -> float:
    explicit = _first(row, "minutes_since_first_seen", "shadow_age_min", "age_since_seen_min")
    if explicit is not None:
        return fnum(explicit, 999.0)
    first = _parse_time(_first(row, "first_seen_at", "opened_at"))
    now = _parse_time(_first(row, "ts_utc", "timestamp", "updated_at_utc")) or dt.datetime.now(dt.timezone.utc)
    if first is None:
        return 999.0
    return max(0.0, (now - first).total_seconds() / 60.0)


def _trigger(row: dict[str, Any], *, cfg: Any = CFG) -> str | None:
    shadow_pnl = fnum(_first(row, "shadow_pnl_pct", "pnl_pct", "target_total_pnl_pct"), 0.0)
    age_min = _age_since_seen_min(row)
    partial = fnum(_first(row, "candidate_partial_pnl_pct", "partial_pnl_pct"), 0.0)
    peak = fnum(_first(row, "observed_peak_after_seen", "max_pnl_pct_seen", "shadow_max_pnl_pct_seen", "peak_pnl_pct"), 0.0)
    age_at_seen = fnum(_first(row, "age_at_seen", "age_minutes", "age_min", "token_age_min"), 999.0)
    trigger_3m = _cfg_float(cfg, "SHADOW_FOLLOWUP_TRIGGER_PNL_3M", 25.0)
    trigger_6m = _cfg_float(cfg, "SHADOW_FOLLOWUP_TRIGGER_PNL_6M", 50.0)
    if shadow_pnl >= trigger_3m and age_min <= 3.0:
        return f"shadow_pnl_{trigger_3m:g}_within_3m"
    if shadow_pnl >= trigger_6m and age_min <= 6.0:
        return f"shadow_pnl_{trigger_6m:g}_within_6m"
    if partial >= 50.0:
        return "candidate_partial_50"
    if peak >= 50.0 and age_at_seen <= 10.0:
        return "observed_peak_after_seen_50"
    breakout = _real_liquidity_breakout_trigger(row, cfg=cfg)
    if breakout is not None:
        return breakout
    return None


def evaluate_shadow_followup_micro(
    row: dict[str, Any],
    *,
    open_count: int = 0,
    daily_buys: int = 0,
    dry_run: bool = True,
    live: bool = False,
    cfg: Any = CFG,
) -> ShadowFollowupMicroDecision:
    amount = max(0.0, _cfg_float(cfg, "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL", 0.003))

    def out(allowed: bool, reason: str, failures: list[str] | tuple[str, ...], *, route_proxy: bool = False) -> ShadowFollowupMicroDecision:
        return ShadowFollowupMicroDecision(bool(allowed), reason, tuple(failures), amount, route_proxy=route_proxy)

    if not _cfg_bool(cfg, "SHADOW_FOLLOWUP_MICRO_ENABLED", True):
        return out(False, "shadow_followup_disabled", ["disabled"])
    if live or not dry_run:
        if not _cfg_bool(cfg, "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED", False):
            return out(False, "shadow_followup_live_disabled", ["live_disabled"])
    elif not _cfg_bool(cfg, "SHADOW_FOLLOWUP_MICRO_PAPER_ENABLED", True):
        return out(False, "shadow_followup_paper_disabled", ["paper_disabled"])
    if _cap_reached(open_count, _cfg_int(cfg, "SHADOW_FOLLOWUP_MICRO_MAX_OPEN", 0)):
        return out(False, "shadow_followup_open_cap", ["open_cap"])
    if _cap_reached(daily_buys, _cfg_int(cfg, "SHADOW_FOLLOWUP_MICRO_MAX_DAILY_BUYS", 0)):
        return out(False, "shadow_followup_daily_cap", ["daily_cap"])

    failures: list[str] = []
    trigger = _trigger(row, cfg=cfg)
    if trigger is None:
        failures.append("no_followup_trigger")
    reason_text = " ".join(str(_first(row, key) or "") for key in ("reason", "green_sniper_reason", "reject_reason")).lower()
    if boolish(_first(row, "toxic_initial_sell_pressure", "initial_sell_pressure_toxic"), False) or "toxic_initial_sell_pressure" in reason_text:
        failures.append("toxic_initial_sell_pressure")
    mcap_raw = _first(row, "market_cap_usd", "buy_market_cap_usd", "mcap")
    mcap = fnum(mcap_raw, 0.0)
    if mcap_raw in (None, "") and int(fnum(_first(row, "mcap_missing_ticks", "missing_mcap_ticks"), 0.0)) > 2:
        failures.append("mcap_missing_gt_2_ticks")
    if mcap > 150_000.0:
        failures.append("mcap_gt_150k")
    cluster_bad = boolish(_first(row, "cluster_bad", "helius_cluster_bad"), False) or "cluster_bad" in reason_text
    mode = str(_first(row, "mode", "shadow_followup_mode", "gate_profile") or "").strip().lower()
    cluster_escape = trigger == "real_liquidity_breakout" and _cfg_bool(
        cfg,
        "SHADOW_FOLLOWUP_ALLOW_CLUSTER_BAD_REAL_LIQUIDITY_BREAKOUT",
        False,
    )
    moonshot_escape = (
        amount <= 0.001
        and mode == "moonshot"
        and _cfg_bool(cfg, "SHADOW_FOLLOWUP_ALLOW_CLUSTER_BAD_MOONSHOT_MICRO", False)
    )
    if cluster_bad and not (cluster_escape or moonshot_escape):
        failures.append("cluster_bad")
    route_ok = boolish(_first(row, "has_jupiter_route", "route_ok", "route_available"), False)
    route_proxy = not route_ok
    if (live or not dry_run) and not route_ok:
        failures.append("no_executable_jupiter_route")
    pre_entry_risk = evaluate_pre_entry_risk(
        row,
        amount_sol=amount,
        dry_run=dry_run,
        live=live,
        cfg=cfg,
    )
    if not pre_entry_risk.allowed:
        failures.extend(pre_entry_risk.failures or pre_entry_risk.risk_flags)
        return out(
            False,
            "shadow_followup_pre_entry_risk:" + pre_entry_risk.reason,
            failures,
            route_proxy=route_proxy,
        )
    if pre_entry_risk.action == "downsize":
        amount = float(pre_entry_risk.amount_sol)
    if failures:
        return out(False, "shadow_followup_blocked:" + ",".join(failures[:6]), failures, route_proxy=route_proxy)
    return out(True, f"shadow_followup_micro:{trigger}", [], route_proxy=route_proxy)


def apply_shadow_followup_micro_context(row: dict[str, Any], decision: ShadowFollowupMicroDecision) -> dict[str, Any]:
    row["entry_lane"] = decision.lane
    row["gate_profile"] = "shadow_followup_micro"
    row["sniper_gate_profile"] = "shadow_followup_micro"
    row["live_profit_gate_profile"] = "shadow_followup_micro"
    row["profit_lane_tier"] = decision.lane
    row["lane_policy_category"] = POLICY_SHADOW_FOLLOWUP_MICRO
    row["green_sniper_reason"] = decision.reason
    row["shadow_followup_micro"] = int(bool(decision.allowed))
    row["shadow_followup_micro_amount_sol"] = float(decision.amount_sol)
    row["shadow_followup_micro_route_proxy"] = int(bool(decision.route_proxy))
    row["route_proxy"] = int(bool(decision.route_proxy))
    row["runner_exit_profile"] = "shadow_followup_micro"
    row["live_profit_gate_failed_count"] = 0
    row["live_profit_gate_failures"] = ""
    return row


def build_shadow_followup_micro_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_runtime_events(root) + load_candidate_outcomes(root)
    shadow_rows = [
        row
        for row in rows
        if "shadow" in " ".join(str(_first(row, key) or "") for key in ("sample_type", "reason", "action", "shadow_kind")).lower()
        or _trigger(row, cfg=CFG) is not None
    ]
    decisions = [evaluate_shadow_followup_micro(row) for row in shadow_rows]
    allowed = [decision for decision in decisions if decision.allowed]
    real_liquidity_breakouts = sum(1 for decision in allowed if "real_liquidity_breakout" in decision.reason)
    blocked = collections.Counter(decision.reason for decision in decisions if not decision.allowed)
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "enabled": bool(getattr(CFG, "SHADOW_FOLLOWUP_MICRO_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "SHADOW_FOLLOWUP_MICRO_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED", False)),
            "amount_sol": max(0.0, _cfg_float(CFG, "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL", 0.003)),
            "trigger_pnl_3m": _cfg_float(CFG, "SHADOW_FOLLOWUP_TRIGGER_PNL_3M", 25.0),
            "trigger_pnl_6m": _cfg_float(CFG, "SHADOW_FOLLOWUP_TRIGGER_PNL_6M", 50.0),
            "max_open": _cfg_int(CFG, "SHADOW_FOLLOWUP_MICRO_MAX_OPEN", 0),
            "max_daily_buys": _cfg_int(CFG, "SHADOW_FOLLOWUP_MICRO_MAX_DAILY_BUYS", 0),
            "real_liquidity_breakout_enabled": _cfg_bool(
                CFG,
                "SHADOW_FOLLOWUP_REAL_LIQUIDITY_BREAKOUT_ENABLED",
                True,
            ),
            "real_liquidity_min_usd": _cfg_float(CFG, "SHADOW_FOLLOWUP_REAL_LIQUIDITY_MIN_USD", 10_000.0),
            "real_liquidity_min_txns_5m": _cfg_float(
                CFG,
                "SHADOW_FOLLOWUP_REAL_LIQUIDITY_MIN_TXNS_5M",
                300.0,
            ),
        },
        "candidates_seen": len(shadow_rows),
        "micro_triggers": len(allowed),
        "real_liquidity_breakouts": real_liquidity_breakouts,
        "route_proxy": sum(1 for decision in allowed if decision.route_proxy),
        "blocked_by_reason": dict(blocked.most_common()),
        "samples": [
            {
                "address": address_of(row),
                "trigger": _trigger(row, cfg=CFG),
                "decision": decision.reason,
                "allowed": decision.allowed,
                "route_proxy": decision.route_proxy,
            }
            for row, decision in zip(shadow_rows[:50], decisions[:50])
        ],
    }


def write_shadow_followup_micro_report(root: Path | None = None) -> dict[str, Any]:
    report = build_shadow_followup_micro_report(root)
    write_json(metrics_dir(root) / "shadow_followup_micro_report.json", report)
    return report


__all__ = [
    "ShadowFollowupMicroDecision",
    "apply_shadow_followup_micro_context",
    "build_shadow_followup_micro_report",
    "evaluate_shadow_followup_micro",
    "write_shadow_followup_micro_report",
]
