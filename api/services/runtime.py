from __future__ import annotations

import datetime as dt
from typing import Any

from api.repositories.filesystem import load_jsonl_rows, parse_timestamp
from api.repositories.runtime_state import load_bot_runtime_state
from api.schemas.common import Envelope, SourceStatus
from api.services.common import build_envelope, iso_or_none, make_source_status, utc_now
from api.services.sources import jsonl_status, sqlite_runtime_state_status
from api.settings import APISettings


DEFAULT_BOT_ID = "main"
RUNTIME_FRESH_S = 15
RUNTIME_STALE_S = 60
RUNTIME_ERROR_S = 180


def get_runtime_snapshot(settings: APISettings, *, bot_id: str = DEFAULT_BOT_ID) -> dict[str, Any] | None:
    return load_bot_runtime_state(settings.db_path, bot_id=bot_id)


def runtime_snapshot_age_seconds(snapshot: dict[str, Any] | None, *, now: dt.datetime | None = None) -> float | None:
    if not snapshot:
        return None
    updated_at = parse_timestamp(snapshot.get("updated_at"))
    if updated_at is None:
        return None
    current = now or utc_now()
    age_s = (current - updated_at).total_seconds()
    return max(0.0, float(age_s))


def runtime_snapshot_freshness(snapshot: dict[str, Any] | None, *, now: dt.datetime | None = None) -> str:
    age_s = runtime_snapshot_age_seconds(snapshot, now=now)
    if age_s is None:
        return "error"
    if age_s > RUNTIME_ERROR_S:
        return "error"
    if age_s > RUNTIME_STALE_S:
        return "degraded"
    if snapshot and snapshot.get("last_error"):
        return "degraded"
    if age_s > RUNTIME_FRESH_S:
        return "stale"
    return "fresh"


def runtime_snapshot_is_stale(snapshot: dict[str, Any] | None, *, now: dt.datetime | None = None) -> bool:
    age_s = runtime_snapshot_age_seconds(snapshot, now=now)
    return age_s is not None and RUNTIME_FRESH_S < age_s <= RUNTIME_ERROR_S


def get_runtime_source_status(
    settings: APISettings,
    snapshot: dict[str, Any] | None,
    *,
    bot_id: str = DEFAULT_BOT_ID,
) -> SourceStatus:
    base = sqlite_runtime_state_status(settings)
    if snapshot is None:
        detail = str(base.detail or "")
        if base.status == "ok":
            detail = f"{detail} bot_id={bot_id} missing".strip()
            return base.model_copy(update={"status": "empty", "detail": detail})
        return base

    age_s = runtime_snapshot_age_seconds(snapshot)
    freshness = runtime_snapshot_freshness(snapshot)
    status = "ok"
    if freshness == "error":
        status = "error"
    elif age_s is not None and age_s > RUNTIME_FRESH_S:
        status = "stale"

    detail = (
        f"bot_id={snapshot.get('bot_id')} "
        f"process_state={snapshot.get('process_state')} "
        f"age_s={int(age_s or 0)} "
        f"staleness={freshness}"
    )
    if snapshot.get("last_error"):
        detail += " last_error=present"

    return make_source_status(
        source_key="sqlite.bot_runtime_state",
        kind="sqlite",
        status=status,
        updated_at=snapshot.get("updated_at"),
        detail=detail,
        path=settings.db_path,
    )


def _runtime_state_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "bot_id": snapshot.get("bot_id"),
        "updated_at": snapshot.get("updated_at"),
        "heartbeat_at": snapshot.get("heartbeat_at"),
        "started_at": snapshot.get("started_at"),
        "process_state": snapshot.get("process_state"),
        "dry_run": snapshot.get("dry_run"),
        "discovery_paused": snapshot.get("discovery_paused"),
        "buys_paused": snapshot.get("buys_paused"),
        "wallet_sol": snapshot.get("wallet_sol"),
        "wallet_checked_at": snapshot.get("wallet_checked_at"),
        "open_positions_count": snapshot.get("open_positions_count"),
        "queue_pending": snapshot.get("queue_pending"),
        "queue_requeued": snapshot.get("queue_requeued"),
        "queue_cooldown": snapshot.get("queue_cooldown"),
        "queue_oldest_first_seen_at": snapshot.get("queue_oldest_first_seen_at"),
        "buy_limiter_in_window": snapshot.get("buy_limiter_in_window"),
        "buy_limiter_window_s": snapshot.get("buy_limiter_window_s"),
        "retrain_state": snapshot.get("retrain_state"),
        "reports_refresh_state": snapshot.get("reports_refresh_state"),
        "discovery_last_ok_at": snapshot.get("discovery_last_ok_at"),
        "monitor_last_ok_at": snapshot.get("monitor_last_ok_at"),
        "last_error": snapshot.get("last_error"),
        "last_error_at": snapshot.get("last_error_at"),
        "stats": snapshot.get("stats_json") or {},
        "ml_gate": snapshot.get("ml_gate_json") or {},
        "strategy_health": snapshot.get("strategy_health_json") or {},
        "research": snapshot.get("research_json") or {},
        "build_info": snapshot.get("build_info_json") or {},
    }


def _strategy_health_event_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "effective_execution_state": row.get("effective_execution_state"),
        "requested_mode": row.get("requested_mode"),
        "health_state": row.get("health_state"),
        "trade_count": row.get("trade_count"),
        "avg_pnl_pct": row.get("avg_pnl_pct"),
        "short_avg_pnl_pct": row.get("short_avg_pnl_pct"),
        "win_rate": row.get("win_rate"),
        "exec_rate": row.get("exec_rate"),
        "price_rate": row.get("price_rate"),
        "consecutive_losses": row.get("consecutive_losses"),
        "cooldown_until": iso_or_none(parse_timestamp(row.get("cooldown_until")))
        or row.get("cooldown_until"),
        "disable_reason": row.get("disable_reason"),
        "last_disable_reason": row.get("last_disable_reason") or row.get("disable_reason"),
        "size_cap_multiplier": row.get("size_cap_multiplier"),
        "severe_exit_count": row.get("severe_exit_count"),
        "liq_crush_count": row.get("liq_crush_count"),
        "recovery_trade_count": row.get("recovery_trade_count"),
        "recovery_avg_pnl_pct": row.get("recovery_avg_pnl_pct"),
        "recovery_ready": row.get("recovery_ready"),
        "last_auto_demote_at": iso_or_none(parse_timestamp(row.get("last_auto_demote_at")))
        or row.get("last_auto_demote_at"),
        "last_auto_recover_at": iso_or_none(parse_timestamp(row.get("last_auto_recover_at")))
        or row.get("last_auto_recover_at"),
    }


def _strategy_health_from_events(settings: APISettings) -> tuple[dict[str, Any], dt.datetime | None]:
    rows = load_jsonl_rows(settings.runtime_events_path)
    latest_by_regime: dict[str, tuple[dt.datetime | None, dict[str, Any]]] = {}

    for row in rows:
        if str(row.get("event_type") or "") != "regime_health":
            continue
        regime = str(row.get("regime") or "").strip()
        if not regime:
            continue
        row_ts = parse_timestamp(row.get("ts_utc"))
        current = latest_by_regime.get(regime)
        if current is None:
            latest_by_regime[regime] = (row_ts, _strategy_health_event_payload(row))
            continue
        current_ts, _ = current
        if current_ts is None or (row_ts is not None and row_ts >= current_ts):
            latest_by_regime[regime] = (row_ts, _strategy_health_event_payload(row))

    last_ts = max((item[0] for item in latest_by_regime.values() if item[0] is not None), default=None)
    return {regime: payload for regime, (_, payload) in latest_by_regime.items()}, last_ts


def _strategy_health_events_status(
    settings: APISettings,
    *,
    last_ts: dt.datetime | None,
    regimes_count: int,
) -> SourceStatus:
    base = jsonl_status(source_key="metrics.runtime_events", path=settings.runtime_events_path)
    if base.status != "ok":
        return base
    if last_ts is None:
        return base.model_copy(update={"status": "empty", "detail": "regime_health_rows=0"})

    age_s = max(0.0, (utc_now() - last_ts).total_seconds())
    status = "ok"
    if age_s > RUNTIME_ERROR_S:
        status = "error"
    elif age_s > RUNTIME_FRESH_S:
        status = "stale"
    return base.model_copy(
        update={
            "status": status,
            "updated_at": iso_or_none(last_ts),
            "detail": f"regime_health_rows={regimes_count} age_s={int(age_s)}",
        }
    )


def _sniper_runtime_rollups(settings: APISettings) -> dict[str, Any]:
    rows = load_jsonl_rows(settings.research_events_path)
    lane_counts: dict[str, int] = {}
    reject_reasons: dict[str, int] = {}
    productive_pnls: list[float] = []
    productive_wins = 0
    severe_reasons = {"LIQUIDITY_CRUSH", "STOP_LOSS", "EARLY_DROP", "ADVERSE_TICK"}
    productive_severe = 0
    for row in rows[-1000:]:
        lane = str(row.get("entry_lane") or "").strip() or str(row.get("sniper_gate_profile") or "").strip()
        if lane:
            lane_counts[lane] = lane_counts.get(lane, 0) + 1
        reason = str(row.get("reason") or "").strip()
        if reason.startswith("live_profit_gate:"):
            reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
        if str(row.get("event_type") or "") == "candidate_outcome" and lane == "pump_early_pumpswap_profit":
            try:
                pnl = float(row.get("pnl_pct"))
            except Exception:
                continue
            productive_pnls.append(pnl)
            if pnl > 0:
                productive_wins += 1
            if str(row.get("exit_reason") or "").strip().upper() in severe_reasons or pnl <= -25.0:
                productive_severe += 1
    productive_count = len(productive_pnls)
    return {
        "entry_lane_counts": lane_counts,
        "sniper_reject_reasons": dict(sorted(reject_reasons.items(), key=lambda item: item[1], reverse=True)[:20]),
        "productive_trade_count": productive_count,
        "productive_avg_pnl_pct": (sum(productive_pnls) / productive_count) if productive_count else None,
        "productive_win_rate": (productive_wins / productive_count) if productive_count else None,
        "severe_exits_rolling": productive_severe,
    }


def _productive_health_payload(strategy_health: dict[str, Any], sniper_rollups: dict[str, Any]) -> dict[str, Any]:
    pump = dict((strategy_health or {}).get("pump_early") or {})
    return {
        **pump,
        "current_gate_rebased": bool(pump.get("current_gate_rebased")),
        "recovery_basis": pump.get("recovery_basis") or {},
        "productive_trade_count": sniper_rollups.get("productive_trade_count"),
        "productive_avg_pnl_pct": sniper_rollups.get("productive_avg_pnl_pct"),
        "productive_win_rate": sniper_rollups.get("productive_win_rate"),
        "severe_exits_rolling": sniper_rollups.get("severe_exits_rolling"),
    }


def _research_health_payload(snapshot: dict[str, Any] | None, sniper_rollups: dict[str, Any]) -> dict[str, Any]:
    research = ((snapshot or {}).get("research_json") or {}) if snapshot else {}
    return {
        "lane_enabled": bool(research.get("lane_enabled", True)),
        "shadow_enabled": bool(research.get("shadow_enabled", True)),
        "open_shadow_count": research.get("open_shadow_count"),
        "open_shadow_by_regime": research.get("open_shadow_by_regime") or {},
        "last_event_at": research.get("last_event_at"),
        "entry_lane_counts": sniper_rollups.get("entry_lane_counts") or {},
        "sniper_reject_reasons": sniper_rollups.get("sniper_reject_reasons") or {},
    }


def get_runtime_state_envelope(settings: APISettings, *, bot_id: str = DEFAULT_BOT_ID) -> Envelope:
    snapshot = get_runtime_snapshot(settings, bot_id=bot_id)
    statuses = [get_runtime_source_status(settings, snapshot, bot_id=bot_id)]
    freshness = runtime_snapshot_freshness(snapshot)
    stale = runtime_snapshot_is_stale(snapshot)
    degraded = freshness in {"degraded", "error"}
    empty = snapshot is None
    data = _runtime_state_payload(snapshot) if snapshot else {}
    return build_envelope(
        data,
        source_status=statuses,
        empty=empty,
        degraded=degraded or any(item.status in {"missing", "error"} for item in statuses),
        stale=stale or any(item.status == "stale" for item in statuses),
    )


def get_runtime_strategy_health_envelope(settings: APISettings, *, bot_id: str = DEFAULT_BOT_ID) -> Envelope:
    snapshot = get_runtime_snapshot(settings, bot_id=bot_id)
    runtime_status = get_runtime_source_status(settings, snapshot, bot_id=bot_id)
    freshness = runtime_snapshot_freshness(snapshot)
    stale = runtime_snapshot_is_stale(snapshot)
    strategy_health = (snapshot or {}).get("strategy_health_json") or {}
    sniper_rollups = _sniper_runtime_rollups(settings)
    if strategy_health:
        productive_health = _productive_health_payload(strategy_health, sniper_rollups)
        research_health = _research_health_payload(snapshot, sniper_rollups)
        blocked_buckets = dict((strategy_health or {}).get("blocked_buckets") or {})
        return build_envelope(
            {
                "bot_id": snapshot.get("bot_id"),
                "updated_at": snapshot.get("updated_at"),
                "strategy_health": strategy_health,
                "productive_health": productive_health,
                "research_health": research_health,
                "current_gate_rebased": bool(productive_health.get("current_gate_rebased")),
                "recovery_basis": productive_health.get("recovery_basis") or {},
                "blocked_buckets": blocked_buckets,
                **sniper_rollups,
            },
            source_status=[runtime_status],
            empty=False,
            degraded=freshness in {"degraded", "error"},
            stale=stale or runtime_status.status == "stale",
        )

    event_health, last_ts = _strategy_health_from_events(settings)
    event_status = _strategy_health_events_status(
        settings,
        last_ts=last_ts,
        regimes_count=len(event_health),
    )
    statuses = [runtime_status, event_status]
    degraded = True
    stale_flag = stale or event_status.status == "stale"
    if freshness in {"degraded", "error"}:
        degraded = True
    payload: dict[str, Any] = {
        "bot_id": snapshot.get("bot_id") if snapshot else bot_id,
        "updated_at": snapshot.get("updated_at") if snapshot else last_ts,
        "strategy_health": event_health,
        "productive_health": _productive_health_payload(event_health, sniper_rollups),
        "research_health": _research_health_payload(snapshot, sniper_rollups),
        "current_gate_rebased": bool((_productive_health_payload(event_health, sniper_rollups)).get("current_gate_rebased")),
        "recovery_basis": (_productive_health_payload(event_health, sniper_rollups)).get("recovery_basis") or {},
        "blocked_buckets": dict((event_health or {}).get("blocked_buckets") or {}),
        **sniper_rollups,
    }
    return build_envelope(
        payload,
        source_status=statuses,
        empty=not bool(event_health),
        degraded=degraded or any(item.status in {"missing", "error"} for item in statuses),
        stale=stale_flag,
    )
