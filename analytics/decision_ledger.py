from __future__ import annotations

from pathlib import Path
from typing import Any

from analytics.report_utils import load_candidate_outcomes, load_runtime_events, metrics_dir, write_json, write_markdown
from config.config import PROJECT_ROOT
from features.decision_store import append_decision, read_decisions


def rebuild_decision_ledger(root: Path | None = None, *, output_path: Path | None = None) -> list[dict[str, Any]]:
    root = root or PROJECT_ROOT
    target = output_path or metrics_dir(root) / "decision_ledger.jsonl"
    if target.exists():
        target.unlink()
    rows: list[dict[str, Any]] = []
    for source, events in (("runtime", load_runtime_events(root)), ("candidate_outcomes", load_candidate_outcomes(root))):
        for event in events:
            action = event.get("decision_action") or event.get("action")
            event_type = str(event.get("event_type") or "")
            if not action and event_type not in {"candidate_decision", "buy", "paper_buy", "bought", "buy_ok"}:
                continue
            row = append_decision(
                {
                    **event,
                    "source": source,
                    "action": action or event_type,
                    "timestamp": event.get("ts_utc") or event.get("timestamp"),
                    "lane": event.get("entry_lane"),
                    "features_snapshot": {k: v for k, v in event.items() if k not in {"event_type", "ts_utc", "address"}},
                },
                path=target,
            )
            rows.append(row)
    return rows


def summarize_decision_ledger(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = read_decisions(metrics_dir(root) / "decision_ledger.jsonl")
    by_action: dict[str, int] = {}
    by_lane: dict[str, int] = {}
    for row in rows:
        by_action[str(row.get("decision") or "unknown")] = by_action.get(str(row.get("decision") or "unknown"), 0) + 1
        by_lane[str(row.get("lane") or "unknown")] = by_lane.get(str(row.get("lane") or "unknown"), 0) + 1
    return {"rows": len(rows), "by_action": by_action, "by_lane": by_lane}


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
    write_markdown(root / "docs" / "DECISION_LEDGER.md", lines)
    return summary


__all__ = ["rebuild_decision_ledger", "summarize_decision_ledger", "write_decision_ledger_report"]
