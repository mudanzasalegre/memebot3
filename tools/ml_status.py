from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.ai_predict import model_runtime_status, threshold_runtime_metadata
from config.config import CFG, PROJECT_ROOT


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Print ML runtime status.")
    parser.add_argument("--no-fail-if-missing-model", action="store_true")
    args = parser.parse_args()
    status = model_runtime_status()
    thresholds = threshold_runtime_metadata()
    train_status = _read(PROJECT_ROOT / "data" / "metrics" / "train_status.json")
    promotion = _read(PROJECT_ROOT / "data" / "metrics" / "lane_promotion_status.json")
    if not status.get("model_exists") and not args.no_fail_if_missing_model:
        return 1
    lines = [
        f"Model path: {status.get('model_path')}",
        f"Active model exists: {status.get('active_model_exists')}",
        f"Candidate fallback used: {status.get('candidate_fallback_used')}",
        f"Global threshold: {(thresholds.get('global') or {}).get('threshold')}",
        f"ML_GATE_MODE: {getattr(CFG, 'ML_GATE_MODE', None)}",
        f"Last train: {status.get('last_train_attempt_at') or train_status.get('last_train_attempt_at')}",
        f"Training scope: {status.get('training_scope')}",
        "",
    ]
    by_lane = thresholds.get("by_lane") or {}
    if by_lane:
        for lane, row in by_lane.items():
            promo = ((promotion.get("by_lane") or {}).get(lane) or {}).get("recommended_state")
            lines.extend(
                [
                    f"Lane {lane}:",
                    f"  mode: {row.get('mode_recommended')}",
                    f"  activation_ready: {row.get('activation_ready')}",
                    f"  threshold: {row.get('threshold')}",
                    f"  reason: {row.get('reason')}",
                    f"  promotion: {promo}",
                    "",
                ]
            )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
