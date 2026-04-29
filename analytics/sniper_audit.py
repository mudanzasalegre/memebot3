from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from config.config import PROJECT_ROOT


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _bucket(value: Any, cuts: tuple[float, ...]) -> str:
    try:
        v = float(value)
    except Exception:
        return "missing"
    prev = None
    for cut in cuts:
        if v < cut:
            return f"<{cut:g}" if prev is None else f"{prev:g}_{cut:g}"
        prev = cut
    return f">={cuts[-1]:g}"


def _sqlite_closed_positions(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT address, entry_lane, gate_profile, buy_dex_id, buy_price_pct_5m, "
                "total_pnl_pct, exit_reason, opened_at, closed_at FROM positions WHERE closed=1"
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def build_sniper_audit(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    metrics = root / "data" / "metrics"
    runtime_events = _read_jsonl(metrics / "runtime_events.jsonl")
    candidate_outcomes = _read_jsonl(metrics / "candidate_outcomes.jsonl")
    closed = _sqlite_closed_positions(root / "data" / "memebotdatabase.db")
    logs_text = ""
    for path in (root / "logs").glob("*.txt"):
        try:
            logs_text += "\n" + path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

    rejected = Counter()
    delayed = Counter()
    shadowed = Counter()
    bought_by_lane = Counter()
    price_buckets = Counter()
    age_buckets = Counter()
    source_counts = Counter()
    hot_seen = 0
    pumpfun_seen = 0

    for row in runtime_events + candidate_outcomes:
        event = str(row.get("event") or row.get("event_type") or row.get("action") or "").lower()
        reason = str(row.get("reason") or row.get("reject_reason") or row.get("stage") or "unknown")
        lane = str(row.get("entry_lane") or row.get("lane") or "unknown")
        source = str(row.get("source") or row.get("discovered_via") or "unknown").lower()
        source_counts[source] += 1
        if source in {"pumpfun", "pumpportal"}:
            pumpfun_seen += 1
            hot_seen += 1
        if "reject" in event or str(row.get("action") or "").lower() == "rejected":
            rejected[reason] += 1
        if "wait" in event or "delay" in event or str(row.get("action") or "").lower() == "wait":
            delayed[reason] += 1
        if "shadow" in event or str(row.get("action") or "").lower() == "shadow":
            shadowed[reason] += 1
        if "buy" in event or str(row.get("action") or "").lower() == "bought":
            bought_by_lane[lane] += 1
        price_buckets[_bucket(row.get("price_pct_5m") or row.get("buy_price_pct_5m"), (25, 50, 100, 180, 300))] += 1
        age_buckets[_bucket(row.get("age_minutes") or row.get("age_min"), (0.5, 1, 3, 8, 30))] += 1

    for row in closed:
        lane = str(row.get("entry_lane") or "unknown")
        bought_by_lane[lane] += 1

    missed_gt100 = []
    missed_gt300 = []
    bought_addresses = {str(row.get("address")) for row in closed}
    for row in candidate_outcomes:
        addr = str(row.get("address") or row.get("mint") or "")
        pnl = row.get("max_pnl_pct") or row.get("max_pnl_pct_seen") or row.get("target_total_pnl_pct")
        try:
            pnl_f = float(pnl)
        except Exception:
            pnl_f = 0.0
        if addr and addr not in bought_addresses and pnl_f >= 100:
            payload = {"address": addr, "later_max_pnl_pct": pnl_f, "reason": row.get("reason")}
            missed_gt100.append(payload)
            if pnl_f >= 300:
                missed_gt300.append(payload)

    log_reasons = Counter(re.findall(r"reason=([a-zA-Z0-9_:\\-]+)", logs_text))
    for reason, count in log_reasons.items():
        if reason not in rejected:
            rejected[reason] += count

    return {
        "total_candidates_seen": len(runtime_events) + len(candidate_outcomes),
        "pumpfun_candidates_seen": pumpfun_seen,
        "hot_candidates_seen": hot_seen,
        "candidates_with_price_pct_5m_gt_25": sum(1 for row in runtime_events + candidate_outcomes if _as_float(row.get("price_pct_5m")) > 25),
        "candidates_with_price_pct_5m_gt_50": sum(1 for row in runtime_events + candidate_outcomes if _as_float(row.get("price_pct_5m")) > 50),
        "candidates_with_price_pct_5m_gt_100": sum(1 for row in runtime_events + candidate_outcomes if _as_float(row.get("price_pct_5m")) > 100),
        "rejected_by_reason": dict(rejected.most_common()),
        "delayed_by_reason": dict(delayed.most_common()),
        "shadowed_by_reason": dict(shadowed.most_common()),
        "bought_by_lane": dict(bought_by_lane.most_common()),
        "by_price_pct_5m_bucket": dict(price_buckets),
        "by_age_bucket": dict(age_buckets),
        "by_source": dict(source_counts),
        "missed_pumps_gt_100": missed_gt100[:50],
        "missed_pumps_gt_300": missed_gt300[:50],
        "avg_time_created_to_seen_s": None,
        "avg_time_seen_to_eval_s": None,
        "avg_time_seen_to_buy_s": None,
    }


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def write_sniper_audit(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_sniper_audit(root)
    metrics_dir = root / "data" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "sniper_audit.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Sniper Audit",
        "",
        f"- Total candidates seen: {report['total_candidates_seen']}",
        f"- Pumpfun candidates seen: {report['pumpfun_candidates_seen']}",
        f"- Hot candidates seen: {report['hot_candidates_seen']}",
        f"- Missed pumps >=100%: {len(report['missed_pumps_gt_100'])}",
        f"- Missed pumps >=300%: {len(report['missed_pumps_gt_300'])}",
        "",
        "## Top Reject Reasons",
    ]
    for reason, count in list(report["rejected_by_reason"].items())[:20]:
        lines.append(f"- {reason}: {count}")
    (docs / "SNIPER_AUDIT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


__all__ = ["build_sniper_audit", "write_sniper_audit"]
