from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from analytics.report_utils import boolish, first_nonempty, fnum
from config.config import CFG


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _parse_ts(value: Any) -> dt.datetime | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except Exception:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)


def moonshot_peak_pct(row: Mapping[str, Any]) -> float:
    values = [
        fnum(first_nonempty(dict(row), "highest_pnl_pct", "max_pnl_pct_seen", "max_pnl_pct", "peak_pnl_pct"), 0.0),
        fnum(first_nonempty(dict(row), "shadow_max_pnl_pct_seen", "observed_peak_after_seen", "observed_shadow_move_pct"), 0.0),
        fnum(first_nonempty(dict(row), "target_total_pnl_pct", "shadow_outcome_pnl_pct", "total_pnl_pct", "pnl_pct"), 0.0),
    ]
    return max(values)


def moonshot_time_to_peak_min(row: Mapping[str, Any]) -> tuple[float | None, str]:
    item = dict(row)
    seconds = first_nonempty(
        item,
        "time_to_peak_sec",
        "time_to_peak_seconds",
        "shadow_time_to_peak_sec",
        "seconds_to_peak",
    )
    if seconds is not None:
        value = fnum(seconds, -1.0)
        if value >= 0.0:
            return round(value / 60.0, 4), "seconds"

    minutes = first_nonempty(
        item,
        "time_to_peak_min",
        "time_to_peak_minutes",
        "minutes_to_peak",
        "hold_minutes_to_peak",
    )
    if minutes is not None:
        value = fnum(minutes, -1.0)
        if value >= 0.0:
            return round(value, 4), "minutes"

    seen_at = _parse_ts(first_nonempty(item, "first_seen_at", "seen_at", "timestamp", "ts_utc", "created_at", "opened_at"))
    peak_at = _parse_ts(first_nonempty(item, "peak_at", "max_pnl_seen_at", "highest_pnl_at", "peak_ts_utc"))
    if seen_at is not None and peak_at is not None:
        return round(max(0.0, (peak_at - seen_at).total_seconds()) / 60.0, 4), "timestamps"

    hold_minutes = first_nonempty(item, "hold_minutes", "age_to_peak_min")
    if hold_minutes is not None and moonshot_peak_pct(item) > 0.0:
        value = fnum(hold_minutes, -1.0)
        if value >= 0.0:
            return round(value, 4), "hold_minutes"

    return None, "missing"


def _has_moonshot_marker(row: Mapping[str, Any]) -> bool:
    haystack = " ".join(
        str(first_nonempty(dict(row), key) or "")
        for key in ("entry_lane", "gate_profile", "profit_lane_tier", "reason", "green_sniper_reason")
    ).lower()
    return "moonshot_micro_lottery" in haystack or boolish(row.get("moonshot_micro_lottery"), False)


def _source_looks_moonshot(row: Mapping[str, Any]) -> bool:
    source = _norm(first_nonempty(dict(row), "source", "discovered_via", "entry_source"))
    address = _norm(first_nonempty(dict(row), "address", "mint", "token_address"))
    return source in {"pumpfun", "green_sniper_birth_probe", "candidate_decision", "candidate_stage", "candidate_outcome"} or address.endswith("pump")


def _theoretical_moonshot(row: Mapping[str, Any], *, cfg: Any = CFG) -> bool:
    if _has_moonshot_marker(row):
        return True
    peak = moonshot_peak_pct(row)
    price5m = fnum(first_nonempty(dict(row), "price_pct_5m", "buy_price_pct_5m", "price5m"), 0.0)
    min_price5m = float(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M", 300.0) or 300.0)
    return _source_looks_moonshot(row) and (peak >= 100.0 or price5m >= min_price5m)


def _row_blocker(row: Mapping[str, Any]) -> str | None:
    item = dict(row)
    for key in (
        "final_blocking_reason",
        "rule_that_blocked",
        "reject_reason",
        "delay_reason",
        "shadow_reason",
        "reason",
        "stage",
    ):
        raw = str(item.get(key) or "").strip()
        if raw and raw.lower() not in {"late_funnel", "confirmed_moonshot_buy"}:
            return raw
    return None


def moonshot_execution_label(row: Mapping[str, Any], *, cfg: Any = CFG) -> dict[str, Any]:
    item = dict(row)
    from analytics.moonshot_micro_lottery import evaluate_moonshot_micro_lottery
    from analytics.token_time import historical_age_snapshot

    decision = evaluate_moonshot_micro_lottery(historical_age_snapshot(item), dry_run=True, live=False, cfg=cfg)
    theoretical = _theoretical_moonshot(item, cfg=cfg)
    route_value = first_nonempty(item, "has_jupiter_route", "route_ok", "route_available")
    route_known = route_value is not None
    route_ok = boolish(route_value, False)
    liquidity_proxy = boolish(first_nonempty(item, "liquidity_is_proxy", "liquidity_usd_is_proxy", "buy_liquidity_is_proxy"), False)
    cluster_bad = boolish(first_nonempty(item, "cluster_bad", "helius_cluster_bad"), False) or "cluster_bad" in _norm(
        first_nonempty(item, "reason", "green_sniper_reason", "sniper_gate_failures")
    )
    time_to_peak, time_to_peak_source = moonshot_time_to_peak_min(item)

    if route_ok:
        route_viability = "route_ok"
    elif route_known and decision.route_proxy:
        route_viability = "route_proxy_paper_only"
    elif route_known:
        route_viability = "no_route"
    else:
        route_viability = "route_unknown"

    if not cluster_bad:
        cluster_viability = "cluster_ok"
    elif decision.allowed and float(decision.amount_sol) <= 0.0005:
        cluster_viability = "risky_cluster_ultra_micro_only"
    else:
        cluster_viability = "cluster_bad_blocked"

    row_blocker = _row_blocker(item)
    if decision.allowed:
        blocker = row_blocker or "executable_no_policy_blocker"
    else:
        blocker = ",".join(decision.failures) if decision.failures else decision.reason

    if decision.allowed:
        viability = "executable"
    elif theoretical:
        viability = "theoretical_only"
    else:
        viability = "not_moonshot"

    return {
        "theoretical_moonshot": bool(theoretical),
        "executable_moonshot": bool(decision.allowed),
        "moonshot_viability": viability,
        "moonshot_blocker": blocker,
        "moonshot_decision_reason": decision.reason,
        "moonshot_failures": list(decision.failures),
        "moonshot_amount_sol": float(decision.amount_sol),
        "moonshot_route_proxy": bool(decision.route_proxy),
        "moonshot_route_viability": route_viability,
        "moonshot_liquidity_viability": "proxy_liquidity" if liquidity_proxy else "real_or_unknown_liquidity",
        "moonshot_cluster_viability": cluster_viability,
        "moonshot_time_to_peak_min": time_to_peak,
        "moonshot_time_to_peak_source": time_to_peak_source,
        "moonshot_peak_pct": moonshot_peak_pct(item),
        "moonshot_paper_only": bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_PAPER_ENABLED", True))
        and not bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED", False)),
        "moonshot_live_enabled": bool(getattr(cfg, "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED", False)),
    }


def build_moonshot_labels(frame: Any, *, cfg: Any = CFG) -> Any:
    import pandas as pd

    if frame.empty:
        return pd.DataFrame(index=frame.index)
    labels = [moonshot_execution_label(row, cfg=cfg) for row in frame.to_dict(orient="records")]
    return pd.DataFrame(labels, index=frame.index)


def attach_moonshot_labels(frame: Any, *, cfg: Any = CFG) -> Any:
    labels = build_moonshot_labels(frame, cfg=cfg)
    out = frame.copy()
    for column in labels.columns:
        out[column] = labels[column]
    return out


__all__ = [
    "attach_moonshot_labels",
    "build_moonshot_labels",
    "moonshot_execution_label",
    "moonshot_peak_pct",
    "moonshot_time_to_peak_min",
]
