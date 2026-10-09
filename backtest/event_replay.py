from __future__ import annotations

import datetime as dt
import json
import os
import statistics
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from analytics.moonshot_micro_lottery import evaluate_moonshot_micro_lottery
from analytics.lane_sizing import resolve_lane_buy_amount
from analytics.paper_bootstrap import should_allow_paper_bootstrap
from analytics.report_utils import (
    address_of,
    boolish,
    first_nonempty,
    fnum,
    is_severe_exit,
    load_candidate_outcomes,
    load_runtime_events,
    load_sqlite_closed_trades,
    load_sqlite_tokens,
    metrics_dir,
    parse_event_timestamp,
    row_event_timestamp,
    write_json,
)
from analytics.shadow_followup_micro import evaluate_shadow_followup_micro
from config.config import CFG, PROJECT_ROOT
from research_loop.api_budget import build_api_budget_report, metrics_from_api_budget


REPORT_JSON = "event_replay.json"
REPLAY_ASSUMPTION_KEYS = {
    "EVENT_REPLAY_LATENCY_SECONDS",
    "EVENT_REPLAY_SLIPPAGE_BPS",
    "EVENT_REPLAY_ALLOW_ROUTE_PROXY",
}
REPLAY_ASSUMPTION_DEFAULTS = {
    "EVENT_REPLAY_LATENCY_SECONDS": 30.0,
    "EVENT_REPLAY_SLIPPAGE_BPS": 50.0,
    # Acceptance replay must only count routes that were actually observed as
    # executable. A trusted caller can opt into proxy fills for diagnostics,
    # but candidates cannot relax this evaluator assumption.
    "EVENT_REPLAY_ALLOW_ROUTE_PROXY": False,
}
# At identical timestamps, terminal evidence must not be used to open and close
# a new position in the same tick. Preserve the conservative causal order.
ENTRY_EVENT_ORDER = {"outcome": 0, "partial": 1, "candidate": 2}
BUY_ACTIONS = {"buy", "bought", "buy_ok", "paper_buy", "actual_paper_buy", "confirmed_moonshot_buy"}
OUTCOME_EVENT_TYPES = {"candidate_outcome", "shadow_close", "trade_close", "close", "sell", "exit"}
PARTIAL_EVENT_TYPES = {"partial_fill", "candidate_partial", "partial_sell"}
FUTURE_ENTRY_FIELDS = {
    "closed_at",
    "exit_reason",
    "highest_pnl_pct",
    "max_pnl_after_seen_1m",
    "max_pnl_after_seen_3m",
    "max_pnl_pct",
    "max_pnl_pct_seen",
    "observed_peak_after_seen",
    "peak_at",
    "peak_pnl_pct",
    "realized_pnl_pct",
    "shadow_max_pnl_pct_seen",
    "shadow_outcome_pnl_pct",
    "target_total_pnl_pct",
    "total_pnl_pct",
    "trade_outcome_pnl_pct",
}
ENTRY_VISIBLE_FIELDS = {
    "action",
    "address",
    "age_at_seen",
    "age_since_seen_min",
    "age_min",
    "age_minutes",
    "buy_liquidity_is_proxy",
    "buy_liquidity_usd",
    "buy_market_cap_usd",
    "buy_price_impact_pct",
    "buy_price_pct_5m",
    "buy_txns_last_5m",
    "candidate_partial_pnl_pct",
    "cluster_bad",
    "created_at",
    "createdAt",
    "created",
    "createdAtUtc",
    "pairCreatedAt",
    "pair_created_at",
    "pairCreatedAtMs",
    "decision_action",
    "discovered_via",
    "entry_lane",
    "entry_notional_usd",
    "entry_source",
    "entry_subtype",
    "event_type",
    "first_seen_at",
    "first_seen_epoch_s",
    "gate_profile",
    "green_sniper_reason",
    "has_jupiter_route",
    "helius_cluster_bad",
    "initial_sell_pressure_toxic",
    "jupiter_price_impact_pct",
    "liquidity_is_proxy",
    "liquidity_usd",
    "liquidity_usd_is_proxy",
    "market_cap_usd",
    "actual_buy_amount_sol",
    "buy_amount_sol",
    "mcap",
    "mint",
    "minutes_since_first_seen",
    "mode",
    "price5m",
    "price_impact_pct",
    "price_pct_5m",
    "profit_lane_tier",
    "queue_age_minutes",
    "rank_score",
    "reason",
    "reject_reason",
    "route_available",
    "route_ok",
    "rug_score",
    "sample_type",
    "score_total",
    "shadow_age_min",
    "shadow_followup_mode",
    "shadow_kind",
    "shadow_pnl_pct",
    "shadow_reason",
    "sniper_gate_failures",
    "sniper_gate_profile",
    "sniper_research_subprofile_failures",
    "source",
    "stage",
    "symbol",
    "timestamp",
    "token_address",
    "token_age_min",
    "toxic_initial_sell_pressure",
    "ts_utc",
    "txns_5m",
    "txns_last_5m",
    "updated_at_utc",
    "volume_24h_usd",
    "volume_usd_24h",
}


@dataclass(frozen=True)
class ReplayEvent:
    kind: str
    ts: dt.datetime
    order: int
    row: dict[str, Any]


@dataclass
class PendingOrder:
    address: str
    signal_ts: dt.datetime
    entry_ts: dt.datetime
    row: dict[str, Any]
    amount_sol: float
    lane: str
    reason: str
    route_proxy: bool
    entry_notional_usd: float | None = None


@dataclass
class SimPosition:
    address: str
    opened_at: dt.datetime
    signal_ts: dt.datetime
    amount_sol: float
    lane: str
    reason: str
    route_proxy: bool
    entry_notional_usd: float | None = None
    remaining_fraction: float = 1.0
    realized_pnl_pct: float = 0.0
    partial_fills: int = 0


def _event_type(row: dict[str, Any]) -> str:
    return str(first_nonempty(row, "event_type", "event", "action", "decision_action", "sample_type") or "").strip().lower()


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _parse_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    raw = value.strip()
    lowered = raw.lower()
    if lowered in {"true", "yes", "y", "on"}:
        return True
    if lowered in {"false", "no", "n", "off"}:
        return False
    try:
        if "." in raw:
            return float(raw.replace("_", ""))
        return int(raw.replace("_", ""))
    except ValueError:
        return raw


def _read_env_values(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    env_path = Path(path)
    if not env_path.exists() or not env_path.is_file():
        return {}
    values: dict[str, Any] = {}
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip().upper()
        if key:
            values[key] = _parse_scalar(raw_value.strip().strip('"').strip("'"))
    return values


def _read_policy_changes(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    policy_path = Path(path)
    if not policy_path.exists():
        return {}
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("changes"), dict):
        return {}
    return {str(key).strip().upper(): _parse_scalar(value) for key, value in payload["changes"].items() if str(key).strip()}


def _auto_candidate_env_path() -> Path | None:
    raw = str(os.getenv("CONFIG_PROFILE_PATH") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.name.lower() == "candidate.env" and path.exists() else None


def _cfg_with_changes(
    *,
    candidate_config: dict[str, Any] | None = None,
    candidate_policy_path: str | Path | None = None,
    candidate_env_path: str | Path | None = None,
) -> tuple[Any, dict[str, Any]]:
    values = {name: getattr(CFG, name) for name in dir(CFG) if name.isupper()}
    resolved_env = Path(candidate_env_path) if candidate_env_path is not None else _auto_candidate_env_path()
    changes: dict[str, Any] = {}
    env_values = _read_env_values(resolved_env)
    changes.update(env_values)
    if candidate_policy_path is None and resolved_env is not None:
        local_policy = resolved_env.parent / "candidate_policy.json"
        if local_policy.exists():
            candidate_policy_path = local_policy
    changes.update(_read_policy_changes(candidate_policy_path))
    if candidate_config:
        changes.update({str(key).strip().upper(): _parse_scalar(value) for key, value in candidate_config.items() if str(key).strip()})
    values.update(changes)
    return SimpleNamespace(**values), changes


def _cfg_float(cfg: Any, key: str, default: float) -> float:
    return fnum(getattr(cfg, key, default), default)


def _cfg_bool(cfg: Any, key: str, default: bool) -> bool:
    return boolish(getattr(cfg, key, default), default)


def _entry_ts(row: dict[str, Any], fallback: dt.datetime) -> dt.datetime:
    # Use when the signal was emitted. ``first_seen_at`` is discovery context,
    # not the time later enrichment fields became observable.
    return row_event_timestamp(row, "ts_utc", "timestamp", "created_at", "opened_at", "seen_at", "first_seen_at") or fallback


def _outcome_ts(row: dict[str, Any], fallback: dt.datetime) -> dt.datetime:
    explicit = row_event_timestamp(row, "closed_at", "outcome_at", "exit_at", "ts_utc", "timestamp", "created_at")
    if explicit is not None:
        return explicit
    base = _entry_ts(row, fallback)
    hold = first_nonempty(row, "hold_seconds", "time_to_close_sec", "time_to_peak_sec")
    if hold is not None:
        return base + dt.timedelta(seconds=max(0.0, fnum(hold, 0.0)))
    hold_min = first_nonempty(row, "hold_minutes", "time_to_close_min", "time_to_peak_min")
    if hold_min is not None:
        return base + dt.timedelta(minutes=max(0.0, fnum(hold_min, 0.0)))
    return base


def _has_outcome_payload(row: dict[str, Any]) -> bool:
    event = _event_type(row)
    if event in OUTCOME_EVENT_TYPES:
        return True
    if first_nonempty(row, "closed_at", "exit_reason", "realized_pnl_pct", "total_pnl_pct", "trade_outcome_pnl_pct") is not None:
        return True
    closed_flag = boolish(row.get("closed"), False)
    terminal_sample = _norm(row.get("sample_type")) in {
        "candidate_outcome",
        "shadow_close",
        "trade_close",
        "close",
        "closed",
    }
    if first_nonempty(row, "pnl_pct", "shadow_outcome_pnl_pct", "target_total_pnl_pct") is not None and (
        "close" in event or closed_flag or terminal_sample
    ):
        return True
    return False


def _has_partial_payload(row: dict[str, Any]) -> bool:
    if _event_type(row) in PARTIAL_EVENT_TYPES:
        return True
    return first_nonempty(row, "partial_fill_fraction", "partial_fill_pct", "sell_fraction", "candidate_partial_pnl_pct") is not None


def _has_entry_payload(row: dict[str, Any]) -> bool:
    if not address_of(row):
        return False
    event = _event_type(row)
    if event in PARTIAL_EVENT_TYPES:
        return False
    visible_signal = first_nonempty(row, "price_pct_5m", "txns_last_5m", "market_cap_usd", "shadow_pnl_pct") is not None
    if event in OUTCOME_EVENT_TYPES:
        return bool(first_nonempty(row, "first_seen_at", "seen_at", "opened_at") is not None and visible_signal)
    if event == "candidate_stage":
        return False
    if event in {"candidate_decision", "candidate", "shadow", "research_shadow"}:
        return True
    if event in BUY_ACTIONS:
        return True
    if not event and visible_signal:
        return True
    return False


def _iter_events(rows: Iterable[dict[str, Any]]) -> list[ReplayEvent]:
    fallback_base = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    events: list[ReplayEvent] = []
    for order, row in enumerate(rows):
        fallback = fallback_base + dt.timedelta(milliseconds=order)
        if _has_entry_payload(row):
            events.append(ReplayEvent("candidate", _entry_ts(row, fallback), order, row))
        if _has_partial_payload(row):
            ts = row_event_timestamp(row, "ts_utc", "timestamp", "created_at", "partial_at", "updated_at_utc") or _entry_ts(row, fallback)
            events.append(ReplayEvent("partial", ts, order, row))
        if _has_outcome_payload(row):
            events.append(ReplayEvent("outcome", _outcome_ts(row, fallback), order, row))
    events.sort(key=lambda event: (event.ts, ENTRY_EVENT_ORDER.get(event.kind, 9), event.order))
    return events


def _entry_visible_row(row: dict[str, Any]) -> tuple[dict[str, Any], int]:
    visible = {key: value for key, value in row.items() if key in ENTRY_VISIBLE_FIELDS}
    ignored = sum(1 for key in row if key in FUTURE_ENTRY_FIELDS)
    return visible, ignored


def _route_value(row: dict[str, Any]) -> Any:
    return first_nonempty(row, "has_jupiter_route", "route_ok", "route_available")


def _entry_decision(
    row: dict[str, Any],
    *,
    cfg: Any,
    open_count: int,
    daily_buys: int,
    bootstrap_open_count: int = 0,
    bootstrap_daily_buys: int = 0,
    bootstrap_hourly_buys: int = 0,
    bootstrap_seconds_since_last_buy: float = float("inf"),
    closed_trades: int = 0,
    now: dt.datetime | None = None,
) -> tuple[bool, str, float, str, bool]:
    # Normalized decisions always carry ``event_type=candidate_decision``.
    # Prefer the actual decision so explicit buys are not silently ignored.
    action_text = _norm(first_nonempty(row, "action", "decision_action", "green_sniper_action", "event_type", "reason"))
    if action_text in BUY_ACTIONS or "confirmed_moonshot_buy" in action_text:
        explicit_lane = _norm(first_nonempty(row, "entry_lane", "gate_profile"))
        if "shadow_followup_micro" in explicit_lane:
            shadow = evaluate_shadow_followup_micro(
                row,
                open_count=open_count,
                daily_buys=daily_buys,
                dry_run=True,
                live=False,
                cfg=cfg,
                now=now,
            )
            if not shadow.allowed:
                return False, shadow.reason, 0.0, shadow.lane, bool(shadow.route_proxy)
            return True, shadow.reason, float(shadow.amount_sol), shadow.lane, bool(shadow.route_proxy)
        if "moonshot_micro_lottery" in explicit_lane:
            moonshot = evaluate_moonshot_micro_lottery(row, dry_run=True, live=False, cfg=cfg, now=now)
            if not moonshot.allowed:
                return False, moonshot.reason, 0.0, moonshot.lane, bool(moonshot.route_proxy)
            return True, moonshot.reason, float(moonshot.amount_sol), moonshot.lane, bool(moonshot.route_proxy)
        if "paper_bootstrap" in explicit_lane:
            bootstrap = should_allow_paper_bootstrap(
                row,
                dry_run=True,
                live=False,
                open_count=bootstrap_open_count,
                daily_buys=bootstrap_daily_buys,
                hourly_buys=bootstrap_hourly_buys,
                seconds_since_last_buy=bootstrap_seconds_since_last_buy,
                closed_trades=closed_trades,
                model_loaded=False,
                model_rows=0,
                trigger_stage="event_replay",
                trigger_reason="explicit_buy_event",
                require_observed_route=True,
                cfg=cfg,
                now=now,
            )
            if not bootstrap.allowed:
                failures = ",".join(bootstrap.hard_failures)
                reason = f"{bootstrap.reason}:{failures}" if failures else bootstrap.reason
                return False, reason, 0.0, bootstrap.lane, False
            route_ok = boolish(_route_value(row), False)
            return (
                True,
                bootstrap.reason,
                float(bootstrap.amount_sol),
                bootstrap.lane,
                not route_ok,
            )
        amount = _cfg_float(cfg, "PAPER_MAX_TRADE_AMOUNT_SOL", 0.01)
        return True, "explicit_buy_event", max(amount, 0.0), str(first_nonempty(row, "entry_lane", "gate_profile") or "explicit_buy"), not boolish(_route_value(row), False)

    shadow = evaluate_shadow_followup_micro(row, open_count=open_count, daily_buys=daily_buys, dry_run=True, live=False, cfg=cfg, now=now)
    if shadow.allowed:
        return True, shadow.reason, float(shadow.amount_sol), shadow.lane, bool(shadow.route_proxy)

    moonshot = evaluate_moonshot_micro_lottery(row, dry_run=True, live=False, cfg=cfg, now=now)
    if moonshot.allowed:
        return True, moonshot.reason, float(moonshot.amount_sol), moonshot.lane, bool(moonshot.route_proxy)

    return False, shadow.reason if shadow.reason else moonshot.reason, 0.0, "", False


def _sqlite_outcome_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a committed position into one authoritative close event."""
    item = dict(row)
    closed_at = first_nonempty(item, "closed_at", "exit_at", "updated_at_utc")
    item.update(
        {
            "event_type": "trade_close",
            "sample_type": "trade_close",
            "source": "sqlite_closed_trade",
            "ts_utc": closed_at,
            "pnl_pct": first_nonempty(item, "total_pnl_pct", "realized_pnl_pct", "pnl_pct"),
            "pnl_usd": first_nonempty(item, "total_pnl_usd", "realized_pnl_usd", "pnl_usd"),
            "closed": True,
        }
    )
    return item


def _replay_source_rows(root: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Avoid closing executed buys with unrelated research-shadow outcomes."""
    candidate_rows = load_candidate_outcomes(root)
    runtime_rows = load_runtime_events(root)
    sqlite_rows = [
        row
        for row in load_sqlite_closed_trades(root)
        if address_of(row) and first_nonempty(row, "closed_at") is not None
    ]
    token_rows = load_sqlite_tokens(root)
    tokens_by_address = {address_of(row).strip().lower(): row for row in token_rows if address_of(row)}
    positions_by_address = {address_of(row).strip().lower(): row for row in sqlite_rows}
    authoritative_addresses = {address_of(row).strip().lower() for row in sqlite_rows}
    suppressed = 0
    enriched_entries = 0
    retained: list[dict[str, Any]] = []
    for row in candidate_rows + runtime_rows:
        address = address_of(row).strip().lower()
        if address in authoritative_addresses and _has_outcome_payload(row):
            position_opened = row_event_timestamp(positions_by_address.get(address) or {}, "opened_at")
            outcome_at = _outcome_ts(row, dt.datetime.min.replace(tzinfo=dt.timezone.utc))
            if position_opened is None or outcome_at >= position_opened:
                suppressed += 1
                continue
        item = dict(row)
        if address in authoritative_addresses and _has_entry_payload(item):
            position = positions_by_address.get(address) or {}
            token = tokens_by_address.get(address) or {}
            for key, value in {
                "actual_buy_amount_sol": position.get("buy_amount_sol"),
                "entry_notional_usd": position.get("entry_notional_usd"),
                "cluster_bad": token.get("cluster_bad"),
                "rug_score": token.get("rug_score"),
            }.items():
                if item.get(key) is None and value is not None:
                    item[key] = value
            enriched_entries += 1
        retained.append(item)
    retained.extend(_sqlite_outcome_row(row) for row in sqlite_rows)
    return retained, {
        "candidate_outcomes": len(candidate_rows),
        "runtime_events": len(runtime_rows),
        "sqlite_closed_trades": len(sqlite_rows),
        "sqlite_tokens": len(token_rows),
        "executed_entries_enriched": enriched_entries,
        "conflicting_outcomes_suppressed": suppressed,
    }


def _pnl_pct(row: dict[str, Any], *, partial: bool = False) -> float | None:
    if partial:
        value = first_nonempty(row, "partial_pnl_pct", "candidate_partial_pnl_pct", "fill_pnl_pct", "pnl_pct")
    else:
        value = first_nonempty(
            row,
            "realized_pnl_pct",
            "total_pnl_pct",
            "trade_outcome_pnl_pct",
            "shadow_outcome_pnl_pct",
            "pnl_pct",
            "target_total_pnl_pct",
        )
    if value is None:
        return None
    return fnum(value, 0.0)


def _peak_pct(row: dict[str, Any], pnl_pct: float) -> float:
    return max(
        pnl_pct,
        fnum(first_nonempty(row, "highest_pnl_pct", "max_pnl_pct_seen", "max_pnl_pct", "peak_pnl_pct", "observed_peak_after_seen"), pnl_pct),
    )


def _partial_fraction(row: dict[str, Any]) -> float:
    value = first_nonempty(row, "partial_fill_fraction", "sell_fraction", "fill_fraction", "fraction")
    if value is None:
        value = first_nonempty(row, "partial_fill_pct", "sell_pct", "fill_pct", "qty_pct")
        fraction = fnum(value, 0.0) / 100.0
    else:
        fraction = fnum(value, 0.0)
        if fraction > 1.0:
            fraction = fraction / 100.0
    return max(0.0, min(1.0, fraction))


def _slippage_cost_pct(slippage_bps: float, *, route_proxy: bool = False) -> float:
    multiplier = 2.0 if route_proxy else 1.0
    return max(0.0, float(slippage_bps)) / 100.0 * multiplier


def _activate_pending(
    *,
    now: dt.datetime,
    pending: dict[str, PendingOrder],
    open_positions: dict[str, SimPosition],
    stats: dict[str, int],
) -> None:
    ready = [address for address, order in pending.items() if order.entry_ts <= now]
    for address in ready:
        order = pending.pop(address)
        open_positions[address] = SimPosition(
            address=address,
            opened_at=order.entry_ts,
            signal_ts=order.signal_ts,
            amount_sol=order.amount_sol,
            lane=order.lane,
            reason=order.reason,
            route_proxy=order.route_proxy,
            entry_notional_usd=order.entry_notional_usd,
        )
        stats["simulated_buys"] += 1
        if order.route_proxy:
            stats["route_proxy_fills"] += 1


def _close_position(
    position: SimPosition,
    row: dict[str, Any],
    *,
    slippage_bps: float,
) -> dict[str, Any] | None:
    pnl = _pnl_pct(row)
    if pnl is None:
        return None
    if position.partial_fills == 0 and boolish(first_nonempty(row, "partial_taken", "has_partial_fill"), False):
        fraction = _partial_fraction(row)
        partial_pnl = _pnl_pct(row, partial=True)
        if fraction > 0.0 and partial_pnl is not None:
            fill_fraction = min(position.remaining_fraction, fraction)
            position.realized_pnl_pct += fill_fraction * (
                partial_pnl - _slippage_cost_pct(slippage_bps, route_proxy=position.route_proxy)
            )
            position.remaining_fraction -= fill_fraction
            position.partial_fills += 1
    cost = _slippage_cost_pct(slippage_bps, route_proxy=position.route_proxy)
    realized_pct = position.realized_pnl_pct + max(0.0, position.remaining_fraction) * (pnl - cost)
    peak = _peak_pct(row, pnl)
    notional_usd = position.entry_notional_usd
    if notional_usd is None:
        historical_notional = fnum(row.get("entry_notional_usd"), 0.0)
        historical_amount = fnum(first_nonempty(row, "buy_amount_sol", "actual_buy_amount_sol"), 0.0)
        if historical_notional > 0.0 and historical_amount > 0.0:
            notional_usd = historical_notional * position.amount_sol / historical_amount
    if notional_usd is not None and notional_usd > 0.0:
        pnl_usd = realized_pct / 100.0 * notional_usd
        pnl_usd_basis = "simulated_entry_notional_usd"
    elif _norm(row.get("source")) == "sqlite_closed_trade":
        # Compatibility fallback for old ledgers that lack entry notional.
        pnl_usd = fnum(first_nonempty(row, "total_pnl_usd", "realized_pnl_usd", "pnl_usd"), 0.0)
        pnl_usd_basis = "committed_total_pnl_usd_unscaled"
    else:
        # Never label a SOL amount as USD. Percentage metrics remain usable.
        pnl_usd = 0.0
        pnl_usd_basis = "unavailable"
    return {
        "address": position.address,
        "opened_at": position.opened_at.isoformat(),
        "closed_at": _outcome_ts(row, position.opened_at).isoformat(),
        "signal_latency_seconds": round((position.opened_at - position.signal_ts).total_seconds(), 3),
        "lane": position.lane,
        "entry_reason": position.reason,
        "exit_reason": first_nonempty(row, "exit_reason", "reason") or "event_replay_close",
        "pnl_pct": round(realized_pct, 6),
        "pnl_usd": round(fnum(pnl_usd, 0.0), 6),
        "pnl_sol": round(realized_pct / 100.0 * position.amount_sol, 9),
        "pnl_usd_basis": pnl_usd_basis,
        "entry_notional_usd": round(float(notional_usd), 6) if notional_usd is not None else None,
        "peak_pct": round(peak, 6),
        "amount_sol": position.amount_sol,
        "route_proxy": position.route_proxy,
        "partial_fills": position.partial_fills,
    }


def _apply_partial(position: SimPosition, row: dict[str, Any], *, slippage_bps: float) -> bool:
    pnl = _pnl_pct(row, partial=True)
    if pnl is None:
        return False
    fraction = min(position.remaining_fraction, _partial_fraction(row))
    if fraction <= 0.0:
        return False
    position.realized_pnl_pct += fraction * (pnl - _slippage_cost_pct(slippage_bps, route_proxy=position.route_proxy))
    position.remaining_fraction -= fraction
    position.partial_fills += 1
    return True


def _summarize_trades(trades: list[dict[str, Any]], missed: list[dict[str, Any]], stats: dict[str, int], api_budget: dict[str, Any]) -> dict[str, Any]:
    pnls = [fnum(row.get("pnl_pct"), 0.0) for row in trades]
    pnl_usd = [fnum(row.get("pnl_usd"), 0.0) for row in trades]
    pnl_sol = [fnum(row.get("pnl_sol"), 0.0) for row in trades]
    cumulative = 0.0
    max_drawdown = 0.0
    for value in pnl_usd:
        cumulative += value
        max_drawdown = min(max_drawdown, cumulative)
    closed = len(trades)
    peak_ratios = [
        max(0.0, fnum(row.get("pnl_pct"), 0.0)) / max(fnum(row.get("peak_pct"), 0.0), 1.0)
        for row in trades
        if fnum(row.get("peak_pct"), 0.0) > 0.0
    ]
    severe = sum(1 for row in trades if fnum(row.get("pnl_pct"), 0.0) <= -25.0 or is_severe_exit(row))
    liquidity = sum(1 for row in trades if str(row.get("exit_reason") or "").upper() == "LIQUIDITY_CRUSH")
    adverse = sum(1 for row in trades if str(row.get("exit_reason") or "").upper() == "ADVERSE_TICK")
    stop_loss = sum(1 for row in trades if str(row.get("exit_reason") or "").upper() == "STOP_LOSS")
    no_pump = sum(1 for row in trades if str(row.get("exit_reason") or "").upper() == "NO_PUMP_EXIT")
    api_metrics = metrics_from_api_budget(api_budget)
    metrics = {
        "total_pnl_usd": round(sum(pnl_usd), 6),
        "total_pnl_sol": round(sum(pnl_sol), 9),
        "pnl_usd_unavailable_count": sum(1 for row in trades if row.get("pnl_usd_basis") == "unavailable"),
        "avg_pnl_pct": round(sum(pnls) / closed, 6) if closed else 0.0,
        "median_pnl_pct": round(statistics.median(pnls), 6) if closed else 0.0,
        "win_rate_pct": round(100.0 * sum(1 for value in pnls if value > 0.0) / closed, 6) if closed else 0.0,
        "closed_trades": closed,
        "runner_capture_ratio": round(sum(peak_ratios) / len(peak_ratios), 6) if peak_ratios else 0.0,
        "moonshot_peak100_capture": sum(1 for row in trades if fnum(row.get("peak_pct"), 0.0) >= 100.0),
        "moonshot_peak500_capture": sum(1 for row in trades if fnum(row.get("peak_pct"), 0.0) >= 500.0),
        "moonshot_peak1000_capture": sum(1 for row in trades if fnum(row.get("peak_pct"), 0.0) >= 1000.0),
        "severe_loss_count": severe,
        "liquidity_crush_count": liquidity,
        "adverse_tick_count": adverse,
        "stop_loss_count": stop_loss,
        "no_pump_exit_count": no_pump,
        "max_drawdown_proxy": round(max_drawdown, 6),
        "objective_score": round(sum(pnl_usd) + (sum(pnls) / closed if closed else 0.0) * 2.0 - severe * 40.0 - liquidity * 35.0 - adverse * 20.0, 6),
        "allowed_shadow_followup": stats.get("allowed_shadow_followup", 0),
        "simulated_buys": stats.get("simulated_buys", 0),
        "shadow_followup_risk_blocked": stats.get("shadow_followup_risk_blocked", 0),
        "shadow_followup_route_proxy": stats.get("route_proxy_fills", 0),
        "giveback_pct": 0.0,
        "missed_peak100_count": sum(1 for row in missed if fnum(row.get("peak_pct"), 0.0) >= 100.0),
        "missed_peak500_count": sum(1 for row in missed if fnum(row.get("peak_pct"), 0.0) >= 500.0),
        "missed_peak1000_count": sum(1 for row in missed if fnum(row.get("peak_pct"), 0.0) >= 1000.0),
        "overtrading_count": 0,
        "idle_no_buy_hours": 0.0,
        "event_replay_candidate_events": stats.get("candidate_events", 0),
        "event_replay_duplicate_mints": stats.get("duplicate_mints", 0),
        "event_replay_latency_expired": stats.get("latency_expired", 0),
        "event_replay_partial_fills": stats.get("partial_fills", 0),
        "event_replay_route_blocked": stats.get("route_blocked", 0),
        "event_replay_route_proxy_fills": stats.get("route_proxy_fills", 0),
        "event_replay_lookahead_fields_ignored": stats.get("lookahead_fields_ignored", 0),
        "event_replay_used_for_acceptance": False,
    }
    metrics.update(api_metrics)
    return metrics


def build_event_replay(
    root: Path | None = None,
    *,
    candidate_config: dict[str, Any] | None = None,
    candidate_policy_path: str | Path | None = None,
    candidate_env_path: str | Path | None = None,
    api_budget: dict[str, Any] | None = None,
    replay_assumptions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    cfg, candidate_changes = _cfg_with_changes(
        candidate_config=candidate_config,
        candidate_policy_path=candidate_policy_path,
        candidate_env_path=candidate_env_path,
    )
    ignored_assumption_changes = sorted(key for key in candidate_changes if key in REPLAY_ASSUMPTION_KEYS)
    for key in REPLAY_ASSUMPTION_KEYS:
        setattr(cfg, key, REPLAY_ASSUMPTION_DEFAULTS[key])
    for raw_key, value in (replay_assumptions or {}).items():
        key = str(raw_key).strip().upper()
        if key in REPLAY_ASSUMPTION_KEYS:
            setattr(cfg, key, _parse_scalar(value))
    latency_seconds = max(0.0, _cfg_float(cfg, "EVENT_REPLAY_LATENCY_SECONDS", 30.0))
    slippage_bps = max(0.0, _cfg_float(cfg, "EVENT_REPLAY_SLIPPAGE_BPS", 50.0))
    allow_route_proxy = _cfg_bool(cfg, "EVENT_REPLAY_ALLOW_ROUTE_PROXY", True)
    rows, source_counts = _replay_source_rows(root)
    events = _iter_events(rows)
    pending: dict[str, PendingOrder] = {}
    open_positions: dict[str, SimPosition] = {}
    trades: list[dict[str, Any]] = []
    missed_outcomes: list[dict[str, Any]] = []
    stats = {
        "allowed_shadow_followup": 0,
        "candidate_events": 0,
        "duplicate_mints": 0,
        "latency_expired": 0,
        "lookahead_fields_ignored": 0,
        "partial_fills": 0,
        "route_blocked": 0,
        "route_proxy_fills": 0,
        "shadow_followup_risk_blocked": 0,
        "simulated_buys": 0,
    }

    for event in events:
        _activate_pending(now=event.ts, pending=pending, open_positions=open_positions, stats=stats)
        address = address_of(event.row).strip().lower()
        if not address:
            continue
        if event.kind == "candidate":
            stats["candidate_events"] += 1
            if address in open_positions or address in pending:
                stats["duplicate_mints"] += 1
                continue
            visible, ignored = _entry_visible_row(event.row)
            stats["lookahead_fields_ignored"] += ignored
            route_value = _route_value(visible)
            route_known = route_value is not None
            route_ok = boolish(route_value, False)
            if not route_ok and not allow_route_proxy:
                stats["route_blocked"] += 1
                continue
            shadow_open_count = sum(
                1
                for position in open_positions.values()
                if "shadow_followup_micro" in _norm(position.lane)
            ) + sum(
                1
                for order in pending.values()
                if "shadow_followup_micro" in _norm(order.lane)
            )
            event_day = event.ts.astimezone(dt.timezone.utc).date()
            shadow_daily_buys = sum(
                1
                for trade in trades
                if "shadow_followup_micro" in _norm(trade.get("lane"))
                and (parse_event_timestamp(trade.get("opened_at")) or event.ts).date() == event_day
            ) + sum(
                1
                for position in open_positions.values()
                if "shadow_followup_micro" in _norm(position.lane)
                and position.opened_at.astimezone(dt.timezone.utc).date() == event_day
            ) + sum(
                1
                for order in pending.values()
                if "shadow_followup_micro" in _norm(order.lane)
                and order.entry_ts.astimezone(dt.timezone.utc).date() == event_day
            )
            bootstrap_positions = [
                position
                for position in open_positions.values()
                if "paper_bootstrap" in _norm(position.lane)
            ]
            bootstrap_orders = [
                order
                for order in pending.values()
                if "paper_bootstrap" in _norm(order.lane)
            ]
            bootstrap_trades = [
                trade
                for trade in trades
                if "paper_bootstrap" in _norm(trade.get("lane"))
            ]
            bootstrap_daily_buys = sum(
                1
                for trade in bootstrap_trades
                if (parse_event_timestamp(trade.get("opened_at")) or event.ts).date() == event_day
            ) + sum(
                1
                for position in bootstrap_positions
                if position.opened_at.astimezone(dt.timezone.utc).date() == event_day
            ) + sum(
                1
                for order in bootstrap_orders
                if order.entry_ts.astimezone(dt.timezone.utc).date() == event_day
            )
            hour_start = event.ts.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
            bootstrap_hourly_buys = sum(
                1
                for trade in bootstrap_trades
                if hour_start
                <= (parse_event_timestamp(trade.get("opened_at")) or event.ts).astimezone(dt.timezone.utc)
                <= event.ts
            ) + sum(
                1
                for position in bootstrap_positions
                if hour_start <= position.opened_at.astimezone(dt.timezone.utc) <= event.ts
            ) + sum(
                1
                for order in bootstrap_orders
                if hour_start <= order.entry_ts.astimezone(dt.timezone.utc) <= event.ts
            )
            bootstrap_buy_times = [
                timestamp
                for timestamp in (
                    *(
                        parse_event_timestamp(trade.get("opened_at"))
                        for trade in bootstrap_trades
                    ),
                    *(position.opened_at for position in bootstrap_positions),
                    *(order.entry_ts for order in bootstrap_orders),
                )
                if timestamp is not None and timestamp <= event.ts
            ]
            bootstrap_seconds_since_last_buy = (
                max(0.0, (event.ts - max(bootstrap_buy_times)).total_seconds())
                if bootstrap_buy_times
                else float("inf")
            )
            allowed, reason, amount, lane, route_proxy = _entry_decision(
                visible,
                cfg=cfg,
                open_count=shadow_open_count,
                daily_buys=shadow_daily_buys,
                bootstrap_open_count=len(bootstrap_positions) + len(bootstrap_orders),
                bootstrap_daily_buys=bootstrap_daily_buys,
                bootstrap_hourly_buys=bootstrap_hourly_buys,
                bootstrap_seconds_since_last_buy=bootstrap_seconds_since_last_buy,
                closed_trades=len(trades),
                now=event.ts,
            )
            if not allowed:
                if "risk" in reason or "cluster_bad" in reason:
                    stats["shadow_followup_risk_blocked"] += 1
                continue
            if "shadow_followup_micro" in lane or "shadow_followup_micro" in reason:
                stats["allowed_shadow_followup"] += 1
            sizing_row = dict(visible)
            sizing_row["entry_lane"] = lane
            sizing = resolve_lane_buy_amount(
                sizing_row,
                computed_amount_sol=amount,
                dry_run=True,
                live=False,
                cfg=cfg,
            )
            amount = max(0.0, float(sizing.amount_sol))
            if amount <= 0.0:
                continue
            pending[address] = PendingOrder(
                address=address,
                signal_ts=event.ts,
                entry_ts=event.ts + dt.timedelta(seconds=latency_seconds),
                row=visible,
                amount_sol=max(0.0, amount),
                lane=lane,
                reason=reason,
                route_proxy=bool(route_proxy or (route_known and not route_ok)),
                entry_notional_usd=(
                    fnum(visible.get("entry_notional_usd"), 0.0)
                    * max(0.0, amount)
                    / fnum(visible.get("actual_buy_amount_sol"), 0.0)
                    if fnum(visible.get("entry_notional_usd"), 0.0) > 0.0
                    and fnum(visible.get("actual_buy_amount_sol"), 0.0) > 0.0
                    else None
                ),
            )
            continue

        if event.kind == "partial":
            if address in pending and event.ts < pending[address].entry_ts:
                continue
            _activate_pending(now=event.ts, pending=pending, open_positions=open_positions, stats=stats)
            position = open_positions.get(address)
            if position is not None and _apply_partial(position, event.row, slippage_bps=slippage_bps):
                stats["partial_fills"] += 1
            continue

        if event.kind == "outcome":
            if address in pending and event.ts < pending[address].entry_ts:
                pending.pop(address, None)
                stats["latency_expired"] += 1
                missed_outcomes.append({"address": address, "peak_pct": _peak_pct(event.row, _pnl_pct(event.row) or 0.0)})
                continue
            _activate_pending(now=event.ts, pending=pending, open_positions=open_positions, stats=stats)
            position = open_positions.pop(address, None)
            if position is None:
                missed_outcomes.append({"address": address, "peak_pct": _peak_pct(event.row, _pnl_pct(event.row) or 0.0)})
                continue
            closed = _close_position(position, event.row, slippage_bps=slippage_bps)
            if closed is not None:
                trades.append(closed)

    if events:
        _activate_pending(now=events[-1].ts + dt.timedelta(seconds=latency_seconds), pending=pending, open_positions=open_positions, stats=stats)

    budget = api_budget if isinstance(api_budget, dict) else build_api_budget_report(root, write=False)
    metrics = _summarize_trades(trades, missed_outcomes, stats, budget)
    warnings: list[str] = []
    timestamp_violations = sum(
        1
        for row in trades
        if (parse_event_timestamp(row.get("closed_at")) or dt.datetime.max.replace(tzinfo=dt.timezone.utc))
        < (parse_event_timestamp(row.get("opened_at")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    )
    if timestamp_violations:
        warnings.append(f"timestamp_order_violations:{timestamp_violations}")
    if open_positions:
        warnings.append(f"unclosed_positions:{len(open_positions)}")
    if pending:
        warnings.append(f"pending_orders:{len(pending)}")
    if int(metrics.get("pnl_usd_unavailable_count") or 0) > 0:
        warnings.append(f"pnl_usd_unavailable:{int(metrics['pnl_usd_unavailable_count'])}")
    if int(metrics.get("event_replay_route_proxy_fills") or 0) > 0:
        warnings.append(f"non_executable_route_proxy_fills:{int(metrics['event_replay_route_proxy_fills'])}")
    if int(metrics.get("event_replay_lookahead_fields_ignored") or 0) > 0:
        warnings.append(f"lookahead_fields_removed:{int(metrics['event_replay_lookahead_fields_ignored'])}")
    critical_warnings = [
        warning
        for warning in warnings
        if warning.startswith(("timestamp_order_violations", "unclosed_positions", "pending_orders", "pnl_usd_unavailable", "non_executable_route_proxy_fills"))
    ]
    acceptance_ready = bool(trades) and not critical_warnings
    metrics["event_replay_used_for_acceptance"] = acceptance_ready
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "schema_version": 2,
        "causal": timestamp_violations == 0,
        "acceptance_ready": acceptance_ready,
        "acceptance_metrics": "event_replay" if acceptance_ready else "event_replay_diagnostic",
        "config": {
            "latency_seconds": latency_seconds,
            "slippage_bps": slippage_bps,
            "allow_route_proxy": allow_route_proxy,
            "candidate_changes": sorted(candidate_changes),
            "ignored_candidate_replay_assumptions": ignored_assumption_changes,
        },
        "inputs": {
            **source_counts,
            "events": len(events),
        },
        "metrics": metrics,
        "trades": trades[:100],
        "open_positions": len(open_positions),
        "pending_orders": len(pending),
        "missed_outcomes": missed_outcomes[:100],
        "warnings": warnings,
    }


def write_event_replay(
    root: Path | None = None,
    *,
    candidate_config: dict[str, Any] | None = None,
    candidate_policy_path: str | Path | None = None,
    candidate_env_path: str | Path | None = None,
    api_budget: dict[str, Any] | None = None,
    replay_assumptions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_event_replay(
        root,
        candidate_config=candidate_config,
        candidate_policy_path=candidate_policy_path,
        candidate_env_path=candidate_env_path,
        api_budget=api_budget,
        replay_assumptions=replay_assumptions,
    )
    write_json(metrics_dir(root) / REPORT_JSON, report)
    return report


__all__ = [
    "REPORT_JSON",
    "build_event_replay",
    "write_event_replay",
]
