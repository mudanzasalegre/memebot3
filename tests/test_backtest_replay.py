from __future__ import annotations

import pandas as pd

from backtest.policies import selected_mask
from backtest.report import summarize_replay


def test_replay_summary_counts_rejected_winners() -> None:
    frame = pd.DataFrame({"y_prob": [0.1, 0.9], "target_total_pnl_pct": [100.0, -10.0]})
    mask = selected_mask(frame, "global_enforce", 0.5)
    report = summarize_replay(frame, mask)
    assert report["rejected_winners"] == 1
    assert report["accepted_losers"] == 1
