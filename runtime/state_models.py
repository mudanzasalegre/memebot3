from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuntimeStateSnapshot:
    bot_id: str
    updated_at: dt.datetime
    heartbeat_at: dt.datetime
    started_at: dt.datetime | None
    process_state: str
    dry_run: bool
    discovery_paused: bool
    buys_paused: bool
    retrain_state: str
    reports_refresh_state: str
    wallet_sol: float | None
    wallet_checked_at: dt.datetime | None
    open_positions_count: int
    queue_pending: int
    queue_requeued: int
    queue_cooldown: int
    queue_oldest_first_seen_at: dt.datetime | None
    buy_limiter_in_window: int
    buy_limiter_window_s: int
    discovery_last_ok_at: dt.datetime | None
    monitor_last_ok_at: dt.datetime | None
    last_error: str | None
    last_error_at: dt.datetime | None
    stats: dict[str, Any] = field(default_factory=dict)
    ml_gate: dict[str, Any] = field(default_factory=dict)
    strategy_health: dict[str, Any] = field(default_factory=dict)
    research: dict[str, Any] = field(default_factory=dict)
    queue_items: dict[str, Any] = field(default_factory=dict)
    build_info: dict[str, Any] = field(default_factory=dict)

