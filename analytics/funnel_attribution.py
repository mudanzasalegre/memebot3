from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analytics.report_utils import (
    address_of,
    load_candidate_outcomes,
    load_paper_positions,
    load_runtime_events,
    load_sqlite_positions,
    metrics_dir,
    write_json,
    write_markdown,
)
from config.config import PROJECT_ROOT


@dataclass(frozen=True)
class FunnelResult:
    address: str
    final_state: str
    final_blocking_reason: str
    primary_stage: str
    timeline: list[dict[str, Any]]


def _ts(row: dict[str, Any]) -> str:
    return str(row.get("ts_utc") or row.get("timestamp") or row.get("created_at") or row.get("closed_at") or "")


def _stage(row: dict[str, Any]) -> str:
    return str(row.get("stage") or row.get("event_type") or row.get("event") or row.get("action") or row.get("sample_type") or "unknown")


def _reason(row: dict[str, Any]) -> str:
    for key in ("final_blocking_reason", "reject_reason", "delay_reason", "shadow_reason", "reason", "exit_reason"):
        raw = str(row.get(key) or "").strip()
        if raw and raw.lower() != "late_funnel":
            return raw
    return ""


def _normalize_event(row: dict[str, Any], *, source: str) -> dict[str, Any]:
    return {
        "ts_utc": _ts(row),
        "source": source,
        "stage": _stage(row),
        "event_type": row.get("event_type") or row.get("event") or row.get("action"),
        "reason": _reason(row) or row.get("stage") or "",
        "entry_lane": row.get("entry_lane"),
        "gate_profile": row.get("gate_profile"),
    }


def _final_state(events: list[dict[str, Any]]) -> tuple[str, str, str]:
    bought = [event for event in events if str(event.get("event_type") or "").lower() in {"buy", "paper_buy", "bought", "buy_ok"}]
    position = [event for event in events if event.get("source") in {"paper_position", "sqlite_position"}]
    if bought or position:
        return "bought", "bought", "execution"

    execution_blocked = [
        event
        for event in events
        if "execution_blocked" in str(event.get("stage") or "").lower()
        or "no_route" in str(event.get("reason") or "").lower()
        or "zero_qty" in str(event.get("reason") or "").lower()
    ]
    if execution_blocked:
        event = execution_blocked[-1]
        return "execution_blocked", str(event.get("reason") or "execution_blocked"), str(event.get("stage") or "execution")

    shadow = [event for event in events if "shadow" in str(event.get("stage") or event.get("reason") or "").lower()]
    reject = [event for event in events if "reject" in str(event.get("stage") or event.get("reason") or "").lower()]
    delay = [event for event in events if "delay" in str(event.get("stage") or event.get("reason") or "").lower()]

    for state, bucket in (("rejected", reject), ("shadow", shadow), ("delayed", delay)):
        if bucket:
            event = bucket[-1]
            return state, str(event.get("reason") or state), str(event.get("stage") or state)

    meaningful = [event for event in events if str(event.get("stage") or "").lower() != "late_funnel"]
    if meaningful:
        event = meaningful[-1]
        return "expired", str(event.get("reason") or "expired"), str(event.get("stage") or "expired")
    return "expired", "expired", "expired"


def build_funnel_attribution(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or PROJECT_ROOT
    grouped: dict[str, list[dict[str, Any]]] = {}
    for source, rows in (
        ("runtime", load_runtime_events(root)),
        ("outcome", load_candidate_outcomes(root)),
        ("paper_position", load_paper_positions(root)),
        ("sqlite_position", load_sqlite_positions(root)),
    ):
        for row in rows:
            addr = address_of(row)
            if not addr:
                continue
            grouped.setdefault(addr, []).append(_normalize_event(row, source=source))

    out: list[dict[str, Any]] = []
    for addr, events in grouped.items():
        events.sort(key=lambda item: str(item.get("ts_utc") or ""))
        state, reason, stage = _final_state(events)
        if reason.lower() == "late_funnel":
            reason = "expired"
        out.append(
            {
                "address": addr,
                "final_state": state,
                "final_blocking_reason": reason,
                "primary_stage": "expired" if str(stage).lower() == "late_funnel" else stage,
                "timeline": events,
            }
        )
    return sorted(out, key=lambda item: str(item["address"]))


def write_funnel_attribution_report(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or PROJECT_ROOT
    rows = build_funnel_attribution(root)
    write_json(metrics_dir(root) / "funnel_attribution.json", rows)
    counts: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for row in rows:
        counts[row["final_state"]] = counts.get(row["final_state"], 0) + 1
        reasons[row["final_blocking_reason"]] = reasons.get(row["final_blocking_reason"], 0) + 1
    lines = ["# Funnel Attribution", "", "| Final state | Count |", "|---|---:|"]
    for state, count in sorted(counts.items(), key=lambda item: item[0]):
        lines.append(f"| {state} | {count} |")
    lines.extend(["", "| Blocking reason | Count |", "|---|---:|"])
    for reason, count in sorted(reasons.items(), key=lambda item: item[1], reverse=True)[:50]:
        lines.append(f"| {reason} | {count} |")
    write_markdown(root / "docs" / "FUNNEL_ATTRIBUTION.md", lines)
    return rows


__all__ = ["build_funnel_attribution", "write_funnel_attribution_report", "FunnelResult"]
