from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.config import PROJECT_ROOT


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check rollout readiness without changing config.")
    parser.add_argument("--phase", default="1")
    args = parser.parse_args()
    segment = _read(PROJECT_ROOT / "data" / "metrics" / "segment_report.json")
    promotion = _read(PROJECT_ROOT / "data" / "metrics" / "lane_promotion_status.json")
    global_row = segment.get("global") or {}
    ready = False
    reason = "missing_metrics"
    if args.phase == "1":
        ready = True
        reason = "observation_allowed"
    elif args.phase == "2":
        ready = bool(global_row.get("model_improves_total_pnl") and (global_row.get("jackpot_capture_rate") is None or float(global_row.get("jackpot_capture_rate")) >= 0.8))
        reason = "lane_aware_paper_ready" if ready else "segment_report_not_ready"
    elif args.phase in {"3", "4", "5"}:
        states = [row.get("recommended_state") for row in (promotion.get("by_lane") or {}).values() if isinstance(row, dict)]
        ready = any(state in {"sizing_only", "risk_veto_only", "enforce"} for state in states)
        reason = "promotion_status_ready" if ready else "promotion_status_not_ready"
    print(json.dumps({"phase": args.phase, "ready": ready, "reason": reason}, indent=2))
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
