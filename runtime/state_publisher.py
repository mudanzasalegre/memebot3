from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import func, select

from db.models import BotRuntimeState, Position
from runtime.state_models import RuntimeStateSnapshot

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore


UTC = dt.timezone.utc


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        ts = value
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.isoformat()
    if isinstance(value, dt.datetime):
        ts = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return ts.astimezone(UTC).isoformat()
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min, tzinfo=UTC).isoformat()
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            return None
        return value
    if np is not None and isinstance(value, np.generic):
        return _json_safe(value.item())
    return value


def _json_dump(value: dict[str, Any]) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=True, sort_keys=True)


async def count_open_positions(session_factory: Any) -> int:
    async with session_factory() as session:
        stmt = select(func.count(Position.id)).where(Position.closed.is_(False))
        result = await session.execute(stmt)
        count = result.scalar_one_or_none()
        return int(count or 0)


async def publish_runtime_state(session_factory: Any, snapshot: RuntimeStateSnapshot) -> None:
    async with session_factory() as session:
        row = await session.get(BotRuntimeState, snapshot.bot_id)
        if row is None:
            row = BotRuntimeState(bot_id=snapshot.bot_id)

        row.updated_at = snapshot.updated_at
        row.heartbeat_at = snapshot.heartbeat_at
        row.started_at = snapshot.started_at
        row.process_state = str(snapshot.process_state)
        row.dry_run = bool(snapshot.dry_run)
        row.discovery_paused = bool(snapshot.discovery_paused)
        row.buys_paused = bool(snapshot.buys_paused)
        row.retrain_state = str(snapshot.retrain_state)
        row.reports_refresh_state = str(snapshot.reports_refresh_state)
        row.wallet_sol = None if snapshot.wallet_sol is None else float(snapshot.wallet_sol)
        row.wallet_checked_at = snapshot.wallet_checked_at
        row.open_positions_count = int(snapshot.open_positions_count)
        row.queue_pending = int(snapshot.queue_pending)
        row.queue_requeued = int(snapshot.queue_requeued)
        row.queue_cooldown = int(snapshot.queue_cooldown)
        row.queue_oldest_first_seen_at = snapshot.queue_oldest_first_seen_at
        row.buy_limiter_in_window = int(snapshot.buy_limiter_in_window)
        row.buy_limiter_window_s = int(snapshot.buy_limiter_window_s)
        row.discovery_last_ok_at = snapshot.discovery_last_ok_at
        row.monitor_last_ok_at = snapshot.monitor_last_ok_at
        row.last_error = snapshot.last_error
        row.last_error_at = snapshot.last_error_at
        row.stats_json = _json_dump(snapshot.stats)
        row.ml_gate_json = _json_dump(snapshot.ml_gate)
        row.strategy_health_json = _json_dump(snapshot.strategy_health)
        row.research_json = _json_dump(snapshot.research)
        row.queue_items_json = _json_dump(snapshot.queue_items)
        row.build_info_json = _json_dump(snapshot.build_info)

        session.add(row)
        await session.commit()
