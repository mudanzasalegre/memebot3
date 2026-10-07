"""Actual isolated calibration/OOS evidence; never runtime or profit evidence."""
from copy import deepcopy
from functools import lru_cache

import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier


@lru_cache(maxsize=1)
def _parts():
    from ml import train
    from ml.entry_probability import fit_primary_probability, probability_metadata
    from net_financial_fixtures import net_frame
    labels = np.r_[np.ones(4), np.zeros(76), np.tile([0, 1], 40)].astype(int)
    times = pd.date_range("2026-09-01", periods=160, freq="10min", tz="UTC")
    data = net_frame(pd.DataFrame({"address": [f"ProbabilityMint{i}" for i in range(160)],
        "timestamp": times, "ts": times + pd.Timedelta(minutes=2), "price_pct_5m": 5.,
        "target_total_pnl_pct": np.where(labels, 100., -30.)}))
    data["mint"] = data.address
    def fit(frame, features):
        return fit_primary_probability(DummyClassifier(strategy="prior"), frame[features], frame.label, frame)
    candidate = train._evaluate_candidate(name="isolated_probability", model_family="synthetic_dummy",
        builder=fit, x_cols=["price_pct_5m"], use_forward=True, tr_df=data.iloc[:120], te_df=data.iloc[120:])
    model = fit(data.iloc[:120], ["price_pct_5m"])
    # Natural later calibration prevalence is exactly 1/2. Its OOS Brier loss
    # beats the older mature train prevalence under this deliberate drift.
    return model, probability_metadata(model, candidate.probability_evaluation)


def primary_probability_parts():
    return deepcopy(_parts())
