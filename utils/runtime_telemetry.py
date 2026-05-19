from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from config.config import PROJECT_ROOT
from utils.runtime_context import runtime_context_payload
from utils.time import utc_now


METRICS_DIR = PROJECT_ROOT / "data" / "metrics"
RUNTIME_EVENTS_PATH = METRICS_DIR / "runtime_events.jsonl"

_LOCK = threading.Lock()


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        ts = value if value.tzinfo is not None else value.replace(tzinfo=utc_now().tzinfo)
        return ts.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def record_runtime_event(event_type: str, address: str, **payload: Any) -> None:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "ts_utc": utc_now().isoformat(),
        "event_type": str(event_type),
        "address": str(address),
    }
    row.update(runtime_context_payload())
    row.update({str(k): _json_safe(v) for k, v in payload.items()})

    line = json.dumps(row, ensure_ascii=True)
    with _LOCK:
        with RUNTIME_EVENTS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def log_queue_add(
    address: str,
    *,
    first_seen_epoch_s: float,
    retries: int,
) -> None:
    record_runtime_event(
        "queue_add",
        address,
        first_seen_epoch_s=float(first_seen_epoch_s),
        retries_left=int(retries),
    )


def log_queue_requeue(
    address: str,
    *,
    reason: str,
    attempts: int,
    retries_left: int,
    backoff_s: int,
    first_seen_epoch_s: float,
) -> None:
    record_runtime_event(
        "requeue",
        address,
        reason=str(reason or ""),
        attempts=int(attempts),
        retries_left=int(retries_left),
        backoff_s=int(backoff_s),
        first_seen_epoch_s=float(first_seen_epoch_s),
    )


def log_queue_drop(
    address: str,
    *,
    reason: str,
    attempts: int,
    retries_left: int,
    first_seen_epoch_s: float | None = None,
) -> None:
    payload: dict[str, Any] = {
        "reason": str(reason or ""),
        "attempts": int(attempts),
        "retries_left": int(retries_left),
    }
    if first_seen_epoch_s is not None:
        payload["first_seen_epoch_s"] = float(first_seen_epoch_s)
    record_runtime_event("queue_drop", address, **payload)


def log_buy_event(
    address: str,
    *,
    attempts: int | None = None,
    first_seen_epoch_s: float | None = None,
    discovered_via: str | None = None,
    entry_regime: str | None = None,
    entry_lane: str | None = None,
    dex_id: str | None = None,
    price_source_at_buy: str | None = None,
    buy_amount_sol: float | None = None,
    size_multiplier: float | None = None,
    size_bucket: str | None = None,
) -> None:
    payload: dict[str, Any] = {}
    if attempts is not None:
        payload["attempts"] = int(attempts)
    if first_seen_epoch_s is not None:
        payload["first_seen_epoch_s"] = float(first_seen_epoch_s)
    if discovered_via:
        payload["discovered_via"] = str(discovered_via)
    if entry_regime:
        payload["entry_regime"] = str(entry_regime)
    if entry_lane:
        payload["entry_lane"] = str(entry_lane)
    if dex_id:
        payload["dex_id"] = str(dex_id)
    if price_source_at_buy:
        payload["price_source_at_buy"] = str(price_source_at_buy)
    if buy_amount_sol is not None:
        payload["buy_amount_sol"] = float(buy_amount_sol)
    if size_multiplier is not None:
        payload["size_multiplier"] = float(size_multiplier)
    if size_bucket:
        payload["size_bucket"] = str(size_bucket)
    record_runtime_event("buy", address, **payload)


def log_ml_decision_event(
    address: str,
    *,
    proba: float,
    threshold: float,
    passed: bool,
    enforced: bool,
    gate_mode: str,
    activation_ready: bool | None = None,
    discovered_via: str | None = None,
    entry_regime: str | None = None,
    score_total: int | None = None,
) -> None:
    payload: dict[str, Any] = {
        "proba": float(proba),
        "threshold": float(threshold),
        "passed": bool(passed),
        "enforced": bool(enforced),
        "gate_mode": str(gate_mode),
    }
    if activation_ready is not None:
        payload["activation_ready"] = bool(activation_ready)
    if discovered_via:
        payload["discovered_via"] = str(discovered_via)
    if entry_regime:
        payload["entry_regime"] = str(entry_regime)
    if score_total is not None:
        payload["score_total"] = int(score_total)
    record_runtime_event("ml_decision", address, **payload)


def log_ml_policy_decision_event(address: str, decision: Any, **payload: Any) -> None:
    if hasattr(decision, "to_dict"):
        data = decision.to_dict()
    elif isinstance(decision, dict):
        data = dict(decision)
    else:
        data = {"decision": str(decision)}
    data.update(payload)
    record_runtime_event("ml_policy_decision", address, **data)


def log_strategy_decision_event(
    address: str,
    *,
    regime: str,
    requested_mode: str,
    effective_mode: str,
    effective_execution_state: str | None = None,
    action: str,
    reason: str,
    confirmations: int,
    confirmations_required: int,
    health_state: str,
    size_cap_multiplier: float | None = None,
) -> None:
    payload: dict[str, Any] = {
        "regime": str(regime),
        "requested_mode": str(requested_mode),
        "effective_mode": str(effective_mode),
        "action": str(action),
        "reason": str(reason),
        "confirmations": int(confirmations),
        "confirmations_required": int(confirmations_required),
        "health_state": str(health_state),
    }
    if effective_execution_state:
        payload["effective_execution_state"] = str(effective_execution_state)
    if size_cap_multiplier is not None:
        payload["size_cap_multiplier"] = float(size_cap_multiplier)
    record_runtime_event("strategy_decision", address, **payload)


def log_regime_health_event(regime: str, **payload: Any) -> None:
    record_runtime_event("regime_health", f"regime:{regime}", regime=str(regime), **payload)


def log_execution_event(
    address: str,
    *,
    regime: str,
    side: str,
    ok: bool,
    venue: str | None = None,
    signature: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "regime": str(regime),
        "side": str(side),
        "ok": bool(ok),
    }
    if venue:
        payload["venue"] = str(venue)
    if signature:
        payload["signature"] = str(signature)
    record_runtime_event("execution", address, **payload)


__all__ = [
    "RUNTIME_EVENTS_PATH",
    "record_runtime_event",
    "log_queue_add",
    "log_queue_requeue",
    "log_queue_drop",
    "log_buy_event",
    "log_ml_decision_event",
    "log_ml_policy_decision_event",
    "log_strategy_decision_event",
    "log_regime_health_event",
    "log_execution_event",
]
