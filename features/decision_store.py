from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from config.config import PROJECT_ROOT

DECISION_LEDGER_PATH = PROJECT_ROOT / "data" / "metrics" / "decision_ledger.jsonl"
_LOCK = threading.Lock()


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    try:
        if value != value:
            return None
    except Exception:
        pass
    return value


def build_decision_id(*, address: str, timestamp: str, lane: str, action: str, reason: str) -> str:
    raw = json.dumps(
        {
            "address": address,
            "timestamp": timestamp,
            "lane": lane,
            "action": action,
            "reason": reason,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def normalize_decision_action(value: Any, row: Mapping[str, Any] | None = None) -> str:
    row = row or {}
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    reason = str(row.get("reason") or row.get("reject_reason") or row.get("delay_reason") or "").lower()
    stage = str(row.get("stage") or row.get("event_type") or row.get("sample_type") or "").lower()
    joined = "|".join([raw, reason, stage])
    if "zero_qty" in joined or "no_route" in joined or "execution_blocked" in joined:
        return "execution_blocked"
    if raw in {"buy", "bought", "buy_ok", "paper_buy", "live"}:
        return "buy"
    if "shadow" in joined:
        return "shadow"
    if raw in {"delay", "delayed", "wait", "waiting"} or "delay" in joined:
        return "delay"
    if raw in {"reject", "rejected", "policy_reject"} or "reject" in joined or "blocked" in joined:
        return "reject"
    return raw if raw in {"buy", "shadow", "reject", "delay", "execution_blocked"} else "reject"


def append_decision(row: Mapping[str, Any], *, path: Path | None = None) -> dict[str, Any]:
    target = path or DECISION_LEDGER_PATH
    timestamp = str(row.get("timestamp") or row.get("ts_utc") or datetime.now(timezone.utc).isoformat())
    address = str(row.get("address") or row.get("mint") or row.get("token_address") or "")
    lane = str(row.get("lane") or row.get("entry_lane") or "unknown")
    action = normalize_decision_action(row.get("action") or row.get("decision") or row.get("decision_action") or row.get("event_type"), row)
    reason = str(row.get("reason") or "")
    payload = {
        "decision_id": row.get("decision_id") or build_decision_id(address=address, timestamp=timestamp, lane=lane, action=action, reason=reason),
        "timestamp": timestamp,
        "address": address,
        "lane": lane,
        "gate_profile": row.get("gate_profile"),
        "entry_subtype": row.get("entry_subtype"),
        "source": row.get("source") or row.get("event_type") or "runtime",
        "features_snapshot": _json_safe(row.get("features_snapshot") or row.get("feature_snapshot") or {}),
        "green_score": row.get("green_score") or row.get("green_sniper_score"),
        "rank_score": row.get("rank_score"),
        "risk_score": row.get("risk_score") or row.get("risk_proba_30") or row.get("risk_proba"),
        "ev_score": row.get("ev_score") or row.get("ev_pred_pct"),
        "runner_score": row.get("runner_score") or row.get("runner100_proba"),
        "continuation_score": row.get("continuation_score"),
        "decision": action,
        "reason": reason,
        "amount_sol": row.get("amount_sol"),
        "exit_profile": row.get("exit_profile") or row.get("runner_exit_profile"),
        "policy_version": row.get("policy_version") or "legacy",
        "config_hash": row.get("config_hash"),
    }
    payload.update({k: _json_safe(v) for k, v in row.items() if k not in payload and k not in {"features_snapshot", "feature_snapshot"}})
    target.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_json_safe(payload), ensure_ascii=True, sort_keys=True) + "\n")
    return payload


def read_decisions(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or DECISION_LEDGER_PATH
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


__all__ = ["DECISION_LEDGER_PATH", "append_decision", "build_decision_id", "normalize_decision_action", "read_decisions"]
