from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.green_sniper_score import score_green_sniper
from analytics.report_utils import fnum, load_candidate_outcomes, metrics_dir, write_json, write_markdown


def main() -> None:
    rows = []
    for row in load_candidate_outcomes(ROOT):
        score = score_green_sniper(
            row,
            has_route=bool(row.get("has_jupiter_route")),
            proxy_liquidity=bool(row.get("liquidity_is_proxy") or row.get("liquidity_usd_is_proxy")),
            live=False,
        ).score
        rows.append({"score": score, "pnl": fnum(row.get("pnl_pct") or row.get("target_total_pnl_pct"), 0.0)})
    rows.sort(key=lambda item: item["score"])
    deciles = {}
    if rows:
        step = max(1, len(rows) // 10)
        for idx in range(0, len(rows), step):
            bucket = rows[idx : idx + step]
            name = f"d{min(10, idx // step + 1)}"
            deciles[name] = {
                "rows": len(bucket),
                "min_score": round(bucket[0]["score"], 3),
                "max_score": round(bucket[-1]["score"], 3),
                "avg_pnl": round(sum(item["pnl"] for item in bucket) / len(bucket), 3),
            }
    report = {"deciles": deciles}
    write_json(metrics_dir(ROOT) / "green_sniper_score_report.json", report)
    lines = ["# Green Sniper Score Report", "", "| Decile | Rows | Min score | Max score | Avg PnL |", "|---|---:|---:|---:|---:|"]
    for key, stats in deciles.items():
        lines.append(f"| {key} | {stats['rows']} | {stats['min_score']} | {stats['max_score']} | {stats['avg_pnl']}% |")
    write_markdown(ROOT / "docs" / "GREEN_SNIPER_SCORE_REPORT.md", lines)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
