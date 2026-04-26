from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.config import PROJECT_ROOT

SEGMENT_REPORT = PROJECT_ROOT / "data" / "metrics" / "segment_report.json"
OUT = PROJECT_ROOT / "data" / "metrics" / "lane_promotion_status.json"


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def recommend_lane_state(row: dict[str, Any]) -> str:
    rows = int(row.get("rows") or 0)
    positives = int(row.get("positives") or 0)
    selected_total = float(row.get("selected_total_pnl") or 0.0)
    baseline_total = float(row.get("total_pnl_pct_points") or 0.0)
    jackpot_capture = row.get("jackpot_capture_rate")
    jackpot_ok = jackpot_capture is None or float(jackpot_capture) >= 0.8
    if rows >= 190 and positives >= 40 and selected_total >= baseline_total and jackpot_ok:
        return "enforce"
    if rows >= 50 and selected_total >= baseline_total and jackpot_ok:
        return "sizing_only"
    if rows > 0:
        return "shadow"
    return "disabled"


def build_promotion_status(report: dict[str, Any] | None = None) -> dict[str, Any]:
    report = report or _read(SEGMENT_REPORT)
    lanes = (((report.get("segments") or {}).get("entry_lane") or {}) if isinstance(report, dict) else {})
    out = {"generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "by_lane": {}}
    for lane, row in lanes.items():
        if isinstance(row, dict):
            out["by_lane"][lane] = {"recommended_state": recommend_lane_state(row), "metrics": row}
    return out


def main() -> int:
    status = build_promotion_status()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
