from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from config.config import PROJECT_ROOT
from ml.data_contract import normalize_candidate_event_row, normalize_decision

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
    return normalize_decision(value, row)


def append_decision(row: Mapping[str, Any], *, path: Path | None = None) -> dict[str, Any]:
    target = path or DECISION_LEDGER_PATH
    normalized = normalize_candidate_event_row(row)
    timestamp = str(normalized.get("timestamp") or normalized.get("ts_utc") or datetime.now(timezone.utc).isoformat())
    address = str(normalized.get("address") or normalized.get("mint") or normalized.get("token_address") or "")
    lane = str(normalized.get("lane") or normalized.get("entry_lane") or "unknown")
    action = normalize_decision_action(
        normalized.get("action") or normalized.get("decision") or normalized.get("decision_action") or normalized.get("event_type"),
        normalized,
    )
    reason = str(normalized.get("reason") or "")
    payload = {
        "decision_id": normalized.get("decision_id") or build_decision_id(address=address, timestamp=timestamp, lane=lane, action=action, reason=reason),
        "timestamp": timestamp,
        "address": address,
        "lane": lane,
        "candidate_stage": normalized.get("candidate_stage"),
        "outcome": normalized.get("outcome"),
        "blockers": _json_safe(normalized.get("blockers") or []),
        "blocker": normalized.get("blocker") or "",
        "run_id": normalized.get("run_id") or "",
        "gate_profile": normalized.get("gate_profile"),
        "entry_subtype": normalized.get("entry_subtype"),
        "entry_subprofile": normalized.get("entry_subprofile") or normalized.get("sniper_research_subprofile"),
        "sniper_research_subprofile_reason": normalized.get("sniper_research_subprofile_reason"),
        "source": normalized.get("source") or normalized.get("event_type") or "runtime",
        "feature_snapshot": _json_safe(normalized.get("feature_snapshot") or {}),
        "features_snapshot": _json_safe(normalized.get("features_snapshot") or normalized.get("feature_snapshot") or {}),
        "green_score": normalized.get("green_score") or normalized.get("green_sniper_score"),
        "rank_score": normalized.get("rank_score"),
        "risk_score": normalized.get("risk_score") or normalized.get("risk_proba_30") or normalized.get("risk_proba"),
        "ev_score": normalized.get("ev_score") or normalized.get("ev_pred_pct"),
        "runner_score": normalized.get("runner_score") or normalized.get("runner100_proba"),
        "continuation_score": normalized.get("continuation_score"),
        "decision": action,
        "reason": reason,
        "amount_sol": normalized.get("amount_sol"),
        "exit_profile": normalized.get("exit_profile") or normalized.get("runner_exit_profile"),
        "policy_version": normalized.get("policy_version") or "legacy",
        "config_hash": normalized.get("config_hash"),
    }
    payload.update({k: _json_safe(v) for k, v in normalized.items() if k not in payload and k not in {"features_snapshot", "feature_snapshot"}})
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
