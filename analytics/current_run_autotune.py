from __future__ import annotations

import collections
import datetime as dt
import json
from pathlib import Path
from typing import Any

from analytics.current_run import current_run_identity, filter_current_run_rows, parse_time, row_time
from analytics.report_utils import (
    address_of,
    boolish,
    load_deduped_positions,
    fnum,
    is_severe_exit,
    load_candidate_outcomes,
    load_runtime_events,
    metrics_dir,
    write_json,
)
from config.config import CFG, PROJECT_ROOT
from ml.lane_taxonomy import (
    LANE_MOONSHOT_MICRO_LOTTERY,
    LANE_PAPER_BOOTSTRAP_MICRO,
    LANE_PAPER_EXPLORATION_MICRO,
    LANE_RESEARCH_RANK_CANARY,
    LANE_SHADOW_FOLLOWUP_MICRO,
    LANE_SNIPER_RESEARCH_MICRO_FALLBACK,
    normalize_entry_lane,
)
from runtime.policy_overlay import build_policy_overlay_state


REPORT_JSON = "current_run_autotune_state.json"
TOXIC_EXIT_REASONS = {"LIQUIDITY_CRUSH", "NO_PUMP_EXIT"}
MICRO_LANES = {
    "paper_bootstrap": LANE_PAPER_BOOTSTRAP_MICRO,
    "shadow_followup_micro": LANE_SHADOW_FOLLOWUP_MICRO,
    "moonshot_micro_lottery": LANE_MOONSHOT_MICRO_LOTTERY,
    "paper_exploration": LANE_PAPER_EXPLORATION_MICRO,
    "research_rank_canary": LANE_RESEARCH_RANK_CANARY,
    "sniper_research_micro_fallback": LANE_SNIPER_RESEARCH_MICRO_FALLBACK,
}
LIVE_KEYS = (
    "LIVE_CANARY_ENABLED",
    "GREEN_SNIPER_LIVE_ENABLED",
    "RESEARCH_RANK_CANARY_LIVE_ENABLED",
    "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED",
    "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED",
    "SNIPER_RESEARCH_MICRO_FALLBACK_LIVE_ENABLED",
    "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED",
    "LATE_MOMENTUM_WATCH_LIVE_ENABLED",
    "AUTO_PROMOTE_LIVE",
    "MODEL_AUTO_PROMOTE",
    "ML_AUTO_PROMOTE_LANES",
    "LLM_TRADING_ENABLED",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _event(row: dict[str, Any]) -> str:
    return str(_first(row, "event_type", "event", "action", "decision_action") or "").strip().lower()


def _reason(row: dict[str, Any]) -> str:
    return str(
        _first(row, "exit_reason", "reason", "green_sniper_reason", "entry_reason", "reject_reason") or ""
    ).strip()


def _reason_upper(row: dict[str, Any]) -> str:
    return _reason(row).strip().upper()


def _lane(row: dict[str, Any]) -> str:
    return normalize_entry_lane(_first(row, "entry_lane", "lane", "profit_lane_tier", "size_bucket"))


def _pnl(row: dict[str, Any]) -> float:
    return fnum(_first(row, "total_pnl_pct", "realized_pnl_pct", "pnl_pct", "target_total_pnl_pct"), 0.0)


def _pnl_usd(row: dict[str, Any]) -> float:
    return fnum(_first(row, "total_pnl_usd", "realized_pnl_usd", "pnl_usd"), 0.0)


def _closed(row: dict[str, Any]) -> bool:
    if boolish(row.get("closed"), False):
        return True
    if _first(row, "closed_at", "exit_reason", "total_pnl_pct", "realized_pnl_pct", "pnl_pct") is not None:
        return True
    return _event(row) in {"trade_close", "close", "closed"}


def _bought(row: dict[str, Any]) -> bool:
    return (
        _event(row) in {"buy", "bought", "paper_buy", "buy_ok"}
        or boolish(_first(row, "bought", "paper_buy"), False)
        or _first(row, "opened_at", "buy_price_usd", "closed_at") is not None
    )


def _buy_key(row: dict[str, Any]) -> str:
    if not _bought(row):
        return ""
    address = address_of(row)
    if address:
        return address.lower()
    return str(
        _first(
            row,
            "buy_tx_sig",
            "position_id",
            "id",
            "opened_at",
            "ts_utc",
            "timestamp",
            "closed_at",
        )
        or id(row)
    )


def _count_buys(rows: list[dict[str, Any]]) -> int:
    return len({key for row in rows for key in [_buy_key(row)] if key})


def _sort_key(row: dict[str, Any]) -> str:
    parsed = row_time(row)
    if parsed is not None:
        return parsed.isoformat()
    return str(_first(row, "ts_utc", "timestamp", "opened_at", "closed_at", "created_at") or "")


def _hours(identity: dict[str, Any], rows: list[dict[str, Any]]) -> float:
    started = parse_time(identity.get("run_started_at") or identity.get("selected_at"))
    latest = max((row_time(row) for row in rows if row_time(row) is not None), default=None)
    if started is None or latest is None:
        return 0.0
    return max((latest - started).total_seconds() / 3600.0, 0.0)


def _load_previous(root: Path) -> dict[str, Any]:
    path = metrics_dir(root) / REPORT_JSON
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _lane_stats(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        lane = _lane(row)
        if lane:
            grouped[lane].append(row)

    out: dict[str, dict[str, Any]] = {}
    for name, lane in MICRO_LANES.items():
        lane_rows = sorted(grouped.get(lane, []), key=_sort_key)
        closed_rows = [row for row in lane_rows if _closed(row)]
        pnls = [_pnl(row) for row in closed_rows]
        total_pnl_usd = sum(_pnl_usd(row) for row in closed_rows)
        total_notional_usd = sum(
            max(0.0, fnum(_first(row, "entry_notional_usd", "buy_notional_usd"), 0.0))
            for row in closed_rows
        )
        consecutive_losses = 0
        for value in reversed(pnls):
            if value < 0.0:
                consecutive_losses += 1
            else:
                break
        consecutive_toxic_exits = 0
        for row in reversed(closed_rows):
            if _reason_upper(row) in TOXIC_EXIT_REASONS:
                consecutive_toxic_exits += 1
            else:
                break
        out[name] = {
            "lane": lane,
            "rows": len(lane_rows),
            "buys": _count_buys(lane_rows),
            "closed_trades": len(closed_rows),
            "wins": sum(1 for value in pnls if value > 0.0),
            "losses": sum(1 for value in pnls if value < 0.0),
            "win_rate_pct": round(100.0 * sum(1 for value in pnls if value > 0.0) / len(pnls), 6) if pnls else 0.0,
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 6) if pnls else 0.0,
            "consecutive_losses": consecutive_losses,
            "consecutive_toxic_exits": consecutive_toxic_exits,
            "severe_loss_count": sum(1 for row in closed_rows if is_severe_exit(row)),
            "toxic_exit_count": sum(1 for row in closed_rows if _reason_upper(row) in TOXIC_EXIT_REASONS),
            "total_pnl_pct_points": round(sum(pnls), 6) if pnls else 0.0,
            "total_pnl_usd": round(total_pnl_usd, 6) if closed_rows else 0.0,
            "entry_notional_usd": round(total_notional_usd, 6) if closed_rows else 0.0,
            "return_on_notional_pct": (
                round(100.0 * total_pnl_usd / total_notional_usd, 6)
                if total_notional_usd > 0.0
                else round(sum(pnls) / len(pnls), 6) if pnls else 0.0
            ),
            "last_result": "win" if pnls and pnls[-1] > 0 else "loss" if pnls and pnls[-1] < 0 else "none",
            "last_reason": _reason(closed_rows[-1]) if closed_rows else "",
        }
    return out


def _merge_change(target: dict[str, Any], key: str, value: Any) -> None:
    if key in LIVE_KEYS:
        return
    target[key] = value


def _micro_relax_changes() -> dict[str, Any]:
    changes: dict[str, Any] = {}
    _merge_change(changes, "SHADOW_FOLLOWUP_TRIGGER_PNL_3M", min(float(getattr(CFG, "SHADOW_FOLLOWUP_TRIGGER_PNL_3M", 25.0) or 25.0), 20.0))
    _merge_change(changes, "SHADOW_FOLLOWUP_TRIGGER_PNL_6M", min(float(getattr(CFG, "SHADOW_FOLLOWUP_TRIGGER_PNL_6M", 50.0) or 50.0), 40.0))
    _merge_change(changes, "PAPER_EXPLORATION_QUOTA_ENABLED", True)
    _merge_change(changes, "PAPER_IDLE_MICRO_EXPLORATION_ENABLED", True)
    _merge_change(changes, "PAPER_IDLE_AFTER_HOURS", 0)
    _merge_change(changes, "RESEARCH_RANK_CANARY_PRIORITY_ONLY", False)
    _merge_change(changes, "RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED", True)
    _merge_change(changes, "RESEARCH_RANK_CANARY_PAPER_NORMAL_BUY_ENABLED", True)
    _merge_change(changes, "SNIPER_RESEARCH_MICRO_FALLBACK_ENABLED", True)
    _merge_change(changes, "MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M", min(float(getattr(CFG, "MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M", 300.0) or 300.0), 300.0))
    return changes


def _current_rows(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    runtime_rows = load_runtime_events(root)
    identity = current_run_identity(root, runtime_rows)
    outcome_rows = load_candidate_outcomes(root)
    position_rows = load_deduped_positions(root)
    return (
        identity,
        filter_current_run_rows(runtime_rows, identity),
        filter_current_run_rows(outcome_rows, identity),
        filter_current_run_rows(position_rows, identity),
    )


def build_current_run_autotune_state(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    identity, runtime_rows, outcome_rows, position_rows = _current_rows(root)
    previous = _load_previous(root)
    all_rows = runtime_rows + outcome_rows + position_rows
    lane_states = _lane_stats(position_rows if position_rows else all_rows)
    hours = _hours(identity, all_rows)
    buys = _count_buys(position_rows) if position_rows else _count_buys(runtime_rows)
    buys_per_hour = round(buys / hours, 6) if hours > 0 else 0.0
    closed_positions = [row for row in position_rows if _closed(row)]
    total_pnl_usd = round(sum(_pnl_usd(row) for row in closed_positions), 6)
    severe_loss_count = sum(int(state.get("severe_loss_count") or 0) for state in lane_states.values())
    previous_run_id = str(previous.get("current_run_id") or "").strip()
    current_run_id = str(identity.get("run_id") or "legacy").strip()
    same_previous_run = not previous_run_id or previous_run_id == current_run_id
    previous_lane_states = previous.get("lane_states") if same_previous_run and isinstance(previous.get("lane_states"), dict) else {}
    previous_severe = (
        int((previous.get("snapshot") or {}).get("severe_loss_count") or 0)
        if same_previous_run
        else 0
    )

    zero_buy_hours = float(getattr(CFG, "CURRENT_RUN_AUTOTUNE_ZERO_BUY_HOURS", 3.0) or 3.0)
    loss_threshold = float(getattr(CFG, "CURRENT_RUN_AUTOTUNE_LOSS_USD_THRESHOLD", 5.0) or 5.0)
    loss_streak = int(getattr(CFG, "CURRENT_RUN_AUTOTUNE_LOSS_STREAK", 3) or 3)
    cooldown_min = float(getattr(CFG, "CURRENT_RUN_AUTOTUNE_COOLDOWN_MIN", 60.0) or 60.0)
    overlay_enabled = bool(getattr(CFG, "CURRENT_RUN_AUTOTUNE_RUNTIME_OVERLAY_ENABLED", True))
    overtrade_buys_per_hour = float(getattr(CFG, "CURRENT_RUN_AUTOTUNE_OVERTRADE_BUYS_PER_HOUR", 120.0) or 120.0)
    bootstrap_min_closed = int(getattr(CFG, "CURRENT_RUN_AUTOTUNE_BOOTSTRAP_MIN_CLOSED", 25) or 25)
    bootstrap_min_win_rate = float(getattr(CFG, "CURRENT_RUN_AUTOTUNE_BOOTSTRAP_MIN_WIN_RATE_PCT", 8.0) or 8.0)
    negative_lane_min_closed = int(getattr(CFG, "CURRENT_RUN_AUTOTUNE_NEGATIVE_LANE_MIN_CLOSED", 10) or 10)
    negative_lane_max_avg = float(getattr(CFG, "CURRENT_RUN_AUTOTUNE_NEGATIVE_LANE_MAX_AVG_PNL_PCT", -5.0))
    negative_lane_max_win_rate = float(
        getattr(CFG, "CURRENT_RUN_AUTOTUNE_NEGATIVE_LANE_MAX_WIN_RATE_PCT", 25.0)
    )
    negative_lane_min_severe_rate = float(
        getattr(CFG, "CURRENT_RUN_AUTOTUNE_NEGATIVE_LANE_MIN_SEVERE_RATE_PCT", 20.0)
    )
    apply_enabled = bool(getattr(CFG, "CURRENT_RUN_AUTOTUNE_APPLY_ENABLED", False))
    enabled = bool(getattr(CFG, "CURRENT_RUN_AUTOTUNE_ENABLED", True))

    actions: list[dict[str, Any]] = []
    recommended_changes: dict[str, Any] = {}

    if enabled and buys_per_hour == 0.0 and hours >= zero_buy_hours:
        changes = _micro_relax_changes()
        recommended_changes.update(changes)
        actions.append(
            {
                "action": "relax_micro_lanes",
                "reason": "zero_buys_for_3h",
                "hours": round(hours, 3),
                "scope": "micro_lanes_only",
                "changes": changes,
            }
        )

    if enabled and total_pnl_usd < -abs(loss_threshold):
        _merge_change(recommended_changes, "PAPER_EXPLORATION_QUOTA_ENABLED", False)
        _merge_change(recommended_changes, "PAPER_IDLE_MICRO_EXPLORATION_ENABLED", False)
        actions.append(
            {
                "action": "disable_paper_exploration",
                "reason": "current_run_total_pnl_below_threshold",
                "total_pnl_usd": total_pnl_usd,
                "threshold_usd": -abs(loss_threshold),
            }
        )

    for severe_lane, severe_state in lane_states.items():
        current_lane_severe = int(severe_state.get("severe_loss_count") or 0)
        previous_lane = previous_lane_states.get(severe_lane) if isinstance(previous_lane_states, dict) else {}
        previous_lane_severe = int((previous_lane or {}).get("severe_loss_count") or 0)
        if not enabled or current_lane_severe <= previous_lane_severe:
            continue
        disable_key = {
            "paper_bootstrap": "PAPER_BOOTSTRAP_ENABLED",
            "paper_exploration": "PAPER_EXPLORATION_QUOTA_ENABLED",
            "shadow_followup_micro": "SHADOW_FOLLOWUP_MICRO_ENABLED",
            "research_rank_canary": "RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED",
            "moonshot_micro_lottery": "MOONSHOT_MICRO_LOTTERY_ENABLED",
            "sniper_research_micro_fallback": "SNIPER_RESEARCH_MICRO_FALLBACK_ENABLED",
        }.get(severe_lane)
        if disable_key:
            _merge_change(recommended_changes, disable_key, False)
        actions.append(
            {
                "action": "disable_severe_loss_lane",
                "reason": "lane_severe_loss_count_increased",
                "lane": severe_lane,
                "previous_severe_loss_count": previous_lane_severe,
                "current_severe_loss_count": current_lane_severe,
            }
        )

    toxic_disable_keys = {
        "paper_bootstrap": "PAPER_BOOTSTRAP_ENABLED",
        "paper_exploration": "PAPER_EXPLORATION_QUOTA_ENABLED",
        "shadow_followup_micro": "SHADOW_FOLLOWUP_MICRO_ENABLED",
        "research_rank_canary": "RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED",
        "moonshot_micro_lottery": "MOONSHOT_MICRO_LOTTERY_ENABLED",
        "sniper_research_micro_fallback": "SNIPER_RESEARCH_MICRO_FALLBACK_ENABLED",
    }
    negative_expectancy_lanes: set[str] = set()
    for lane_name, state in lane_states.items():
        closed = int(state.get("closed_trades") or 0)
        if closed <= 0:
            continue
        severe_rate = 100.0 * int(state.get("severe_loss_count") or 0) / closed
        persistently_negative = (
            enabled
            and closed >= negative_lane_min_closed
            and float(state.get("total_pnl_usd") or 0.0) < 0.0
            and (
                float(state.get("avg_pnl_pct") or 0.0) <= negative_lane_max_avg
                or float(state.get("return_on_notional_pct") or 0.0) <= negative_lane_max_avg
            )
            and (
                float(state.get("win_rate_pct") or 0.0) <= negative_lane_max_win_rate
                or severe_rate >= negative_lane_min_severe_rate
            )
        )
        if not persistently_negative:
            continue
        negative_expectancy_lanes.add(lane_name)
        disable_key = toxic_disable_keys.get(lane_name)
        if disable_key:
            _merge_change(recommended_changes, disable_key, False)
        actions.append(
            {
                "action": "disable_negative_expectancy_lane",
                "reason": "persistent_negative_expectancy",
                "lane": lane_name,
                "closed_trades": closed,
                "avg_pnl_pct": float(state.get("avg_pnl_pct") or 0.0),
                "return_on_notional_pct": float(state.get("return_on_notional_pct") or 0.0),
                "win_rate_pct": float(state.get("win_rate_pct") or 0.0),
                "severe_loss_rate_pct": round(severe_rate, 6),
                "total_pnl_usd": float(state.get("total_pnl_usd") or 0.0),
                "cooldown_min": cooldown_min,
            }
        )

    for lane_name, state in lane_states.items():
        toxic_streak = int(state.get("consecutive_toxic_exits") or 0)
        if not enabled or toxic_streak < loss_streak:
            continue
        disable_key = toxic_disable_keys.get(lane_name)
        if disable_key:
            _merge_change(recommended_changes, disable_key, False)
        actions.append(
            {
                "action": "cooldown_toxic_exit_lane",
                "reason": "liquidity_crush_or_no_pump_loss_streak",
                "lane": lane_name,
                "consecutive_toxic_exits": toxic_streak,
                "loss_streak_threshold": loss_streak,
                "cooldown_min": cooldown_min,
            }
        )

    bootstrap_state = lane_states["paper_bootstrap"]
    bootstrap_closed = int(bootstrap_state.get("closed_trades") or 0)
    bootstrap_buys = int(bootstrap_state.get("buys") or 0)
    bootstrap_win_rate = float(bootstrap_state.get("win_rate_pct") or 0.0)
    bootstrap_avg = float(bootstrap_state.get("avg_pnl_pct") or 0.0)
    if (
        enabled
        and bootstrap_closed >= bootstrap_min_closed
        and bootstrap_buys > 0
        and (
            buys_per_hour >= overtrade_buys_per_hour
            or bootstrap_win_rate < bootstrap_min_win_rate
            or bootstrap_avg < 0.0
        )
    ):
        _merge_change(recommended_changes, "PAPER_BOOTSTRAP_QUALITY_GATES_ENABLED", True)
        _merge_change(recommended_changes, "PAPER_BOOTSTRAP_REQUIRE_ROUTE", True)
        _merge_change(recommended_changes, "PAPER_BOOTSTRAP_REQUIRE_REAL_LIQUIDITY", True)
        _merge_change(recommended_changes, "PAPER_BOOTSTRAP_BLOCK_CLUSTER_BAD", True)
        paper_trade_cap = max(0.0, float(getattr(CFG, "PAPER_MAX_TRADE_AMOUNT_SOL", 0.1) or 0.1))
        if paper_trade_cap > 0.0:
            _merge_change(
                recommended_changes,
                "PAPER_BOOTSTRAP_AMOUNT_SOL",
                min(float(getattr(CFG, "PAPER_BOOTSTRAP_AMOUNT_SOL", paper_trade_cap) or paper_trade_cap), paper_trade_cap),
            )
            _merge_change(
                recommended_changes,
                "PAPER_BOOTSTRAP_MAX_AMOUNT_SOL",
                min(float(getattr(CFG, "PAPER_BOOTSTRAP_MAX_AMOUNT_SOL", paper_trade_cap) or paper_trade_cap), paper_trade_cap),
            )
        actions.append(
            {
                "action": "bootstrap_quality_guard",
                "reason": "paper_bootstrap_overtrading_or_low_quality",
                "buys_per_hour": buys_per_hour,
                "bootstrap_buys": bootstrap_buys,
                "bootstrap_closed_trades": bootstrap_closed,
                "bootstrap_win_rate_pct": bootstrap_win_rate,
                "bootstrap_avg_pnl_pct": bootstrap_avg,
                "scope": "quality_and_size_only",
            }
        )

    rank_state = lane_states["research_rank_canary"]
    if enabled and int(rank_state.get("consecutive_losses") or 0) >= loss_streak:
        _merge_change(recommended_changes, "RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED", False)
        _merge_change(recommended_changes, "RESEARCH_RANK_CANARY_PAPER_NORMAL_BUY_ENABLED", False)
        actions.append(
            {
                "action": "rank_canary_shadow_only",
                "reason": "rank_canary_lost_3_consecutive",
                "consecutive_losses": int(rank_state.get("consecutive_losses") or 0),
            }
        )

    shadow_state = lane_states["shadow_followup_micro"]
    if (
        enabled
        and "shadow_followup_micro" not in negative_expectancy_lanes
        and int(shadow_state.get("wins") or 0) > 0
        and float(shadow_state.get("avg_pnl_pct") or 0.0) > 0.0
        and float(shadow_state.get("total_pnl_usd") or 0.0) > 0.0
        and shadow_state.get("last_result") == "win"
    ):
        actions.append(
            {
                "action": "keep_shadow_followup_micro",
                "reason": "shadow_followup_micro_profitable",
                "wins": int(shadow_state.get("wins") or 0),
            }
        )

    moonshot_state = lane_states["moonshot_micro_lottery"]
    if enabled and int(moonshot_state.get("consecutive_losses") or 0) >= loss_streak:
        actions.append(
            {
                "action": "review_moonshot_loss_streak",
                "reason": "moonshot_micro_lost_3_consecutive",
                "consecutive_losses": int(moonshot_state.get("consecutive_losses") or 0),
                "scope": "quality_filters_only",
            }
        )

    if enabled and same_previous_run:
        active_lanes = {str(action.get("lane") or "") for action in actions if isinstance(action, dict)}
        previous_overlay = previous.get("runtime_overlay") if isinstance(previous.get("runtime_overlay"), dict) else {}
        now_dt = dt.datetime.now(dt.timezone.utc)
        for block in previous_overlay.get("blocked_lanes") or []:
            if not isinstance(block, dict):
                continue
            lane = str(block.get("lane") or "").strip()
            until = parse_time(block.get("cooldown_until"))
            if not lane or lane in active_lanes or until is None or until <= now_dt:
                continue
            actions.append(
                {
                    "action": "carry_forward_lane_cooldown",
                    "reason": str(block.get("reason") or "prior_cooldown_active"),
                    "lane": lane,
                    "cooldown_until": until.isoformat(),
                    "original_action": str(block.get("action") or "lane_block"),
                }
            )

    applied_changes = dict(recommended_changes) if apply_enabled and enabled else {}
    report = {
        "generated_at_utc": _utc_now(),
        "current_run_id": str(identity.get("run_id") or "legacy"),
        "current_run": identity,
        "enabled": enabled,
        "apply_enabled": apply_enabled,
        "mode": "paper_only_report" if not apply_enabled else "paper_only_runtime_overlay",
        "actions": actions,
        "lane_states": lane_states,
        "recommended_changes": recommended_changes,
        "applied_changes": applied_changes,
        "snapshot": {
            "hours": round(hours, 6),
            "buys": buys,
            "buys_per_hour": buys_per_hour,
            "current_run_total_pnl_usd": total_pnl_usd,
            "severe_loss_count": severe_loss_count,
            "previous_severe_loss_count": previous_severe,
        },
        "safety": {
            "live_changes_allowed": False,
            "live_keys_guarded": list(LIVE_KEYS),
            "touches_wallet": False,
            "touches_api_budget": False,
        },
    }
    report["runtime_overlay"] = build_policy_overlay_state(
        report,
        cooldown_min=cooldown_min,
        enabled=bool(enabled and overlay_enabled),
    )
    return report


def write_current_run_autotune_state(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_current_run_autotune_state(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    return report


__all__ = [
    "REPORT_JSON",
    "build_current_run_autotune_state",
    "write_current_run_autotune_state",
]
