from __future__ import annotations

import pandas as pd

from ml.risk_model import severe_loss_labels


def test_severe_loss_labels() -> None:
    labels = severe_loss_labels(pd.DataFrame({"target_total_pnl_pct": [-31, -30, -29, 10]}), severe_loss_pct=-30)
    assert labels.tolist() == [1, 1, 0, 0]
