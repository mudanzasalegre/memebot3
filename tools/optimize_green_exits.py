from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from backtest.exit_simulator import compare_exit_profiles
from config.config import PROJECT_ROOT


def _load_rows() -> list[dict[str, object]]:
    db_path = PROJECT_ROOT / "data" / "memebotdatabase.db"
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT entry_lane, gate_profile, total_pnl_pct, highest_pnl_pct, max_pnl_pct_seen "
                "FROM positions WHERE closed=1"
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def main() -> None:
    report = compare_exit_profiles(_load_rows())
    metrics = PROJECT_ROOT / "data" / "metrics"
    metrics.mkdir(parents=True, exist_ok=True)
    (metrics / "green_exit_optimization.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    docs = PROJECT_ROOT / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    lines = ["# Green Exit Optimization", "", "| Profile | Avg | Median | Total | >100 | >300 |", "|---|---:|---:|---:|---:|---:|"]
    for name, m in report.items():
        lines.append(
            f"| {name} | {m['avg_realized_pnl']:.2f}% | {m['median_realized_pnl']:.2f}% | "
            f"{m['total_realized_pnl']:.2f} | {m['trades_over_100']} | {m['trades_over_300']} |"
        )
    (docs / "GREEN_EXIT_OPTIMIZATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
