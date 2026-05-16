from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.post_hotfix_strategy_preview import write_post_hotfix_strategy_preview


def main() -> None:
    report = write_post_hotfix_strategy_preview(ROOT)
    combined = report["combined_hotfix_v1"]
    print(
        "post_hotfix_strategy_preview "
        f"baseline={report['baseline_current']['count']} "
        f"delta={combined['expected_total_pnl_delta_pct_points']} "
        f"severe_delta={combined['expected_severe_loss_delta']} "
        f"runner_capture_delta={combined['expected_runner_capture_delta']}"
    )


if __name__ == "__main__":
    main()
