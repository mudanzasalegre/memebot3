from __future__ import annotations

from pathlib import Path
from typing import Any

from analytics.lane_policy_categories import classify_policy_category
from analytics.report_utils import load_candidate_outcomes, load_runtime_events, metrics_dir, write_json, write_markdown
from config.config import PROJECT_ROOT
from features.decision_store import append_decision, normalize_decision_action, read_decisions


def rebuild_decision_ledger(root: Path | None = None, *, output_path: Path | None = None) -> list[dict[str, Any]]:
    root = root or PROJECT_ROOT
    target = output_path or metrics_dir(root) / "decision_ledger.jsonl"
    if target.exists():
        target.unlink()
    rows: list[dict[str, Any]] = []
    latest_by_address_lane: dict[tuple[str, str], str] = {}
    for source, events in (("runtime", load_runtime_events(root)), ("candidate_outcomes", load_candidate_outcomes(root))):
        for event in events:
            action = event.get("decision_action") or event.get("action")
            event_type = str(event.get("event_type") or "")
            reason = str(event.get("reason") or event.get("reject_reason") or event.get("delay_reason") or "")
            normalized_action = normalize_decision_action(action or event_type or reason, event)
            if not action and event_type not in {"candidate_decision", "candidate_outcome", "buy", "paper_buy", "bought", "buy_ok", "execution_blocked", "reject", "delay"}:
                continue
            address = str(event.get("address") or event.get("mint") or event.get("token_address") or "")
            lane = str(event.get("entry_lane") or event.get("lane") or "unknown")
            linked_id = event.get("decision_id") or latest_by_address_lane.get((address, lane)) or latest_by_address_lane.get((address, "unknown"))
            row = append_decision(
                {
                    **event,
                    "source": source,
                    "action": normalized_action,
                    "timestamp": event.get("ts_utc") or event.get("timestamp"),
                    "lane": lane,
                    "lane_policy_category": classify_policy_category({**event, "lane": lane, "action": normalized_action}),
                    "linked_decision_id": linked_id,
                    "features_snapshot": {k: v for k, v in event.items() if k not in {"event_type", "ts_utc", "address"}},
                },
                path=target,
            )
            latest_by_address_lane[(str(row.get("address") or address), str(row.get("lane") or lane))] = str(row.get("decision_id"))
            rows.append(row)
    return rows


def summarize_decision_ledger(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = read_decisions(metrics_dir(root) / "decision_ledger.jsonl")
    by_action: dict[str, int] = {}
    by_lane: dict[str, int] = {}
    by_policy_category: dict[str, int] = {}
    for row in rows:
        by_action[str(row.get("decision") or "unknown")] = by_action.get(str(row.get("decision") or "unknown"), 0) + 1
        by_lane[str(row.get("lane") or "unknown")] = by_lane.get(str(row.get("lane") or "unknown"), 0) + 1
        category = str(row.get("lane_policy_category") or classify_policy_category(row))
        by_policy_category[category] = by_policy_category.get(category, 0) + 1
    return {"rows": len(rows), "by_action": by_action, "by_lane": by_lane, "by_policy_category": by_policy_category}


def write_decision_ledger_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rebuild_decision_ledger(root)
    summary = summarize_decision_ledger(root)
    write_json(metrics_dir(root) / "decision_ledger_summary.json", summary)
    lines = ["# Decision Ledger", "", f"- Rows: `{summary['rows']}`", "", "## By Action", ""]
    for action, count in sorted(summary["by_action"].items()):
        lines.append(f"- `{action}`: `{count}`")
    lines.extend(["", "## By Lane", ""])
    for lane, count in sorted(summary["by_lane"].items()):
        lines.append(f"- `{lane}`: `{count}`")
    lines.extend(["", "## By Policy Category", ""])
    for category, count in sorted(summary["by_policy_category"].items()):
        lines.append(f"- `{category}`: `{count}`")
    write_markdown(root / "docs" / "DECISION_LEDGER.md", lines)
    return summary


__all__ = ["rebuild_decision_ledger", "summarize_decision_ledger", "write_decision_ledger_report"]
