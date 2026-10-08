"""Actual isolated synthetic training/cohort approval, never market evidence."""
from copy import deepcopy
from functools import lru_cache

import numpy as np
import pandas as pd

from net_financial_fixtures import net_frame


def population(n=240, *, start="2026-09-01", prefix="ChampionFit", positive_feature=80.):
    times = pd.date_range(start, periods=n, freq="10min", tz="UTC")
    raw = pd.DataFrame({"address": [f"{prefix}{i}" for i in range(n)], "timestamp": times,
        "ts": times + pd.Timedelta(minutes=2), "entry_regime": "pump_early",
        "entry_lane": "pump_early_pumpswap_profit", "dex_id": "pumpswap",
        "price_pct_5m": np.tile([5., positive_feature], n // 2),
        "target_total_pnl_pct": np.tile([-30., 200.], n // 2)})
    return net_frame(raw)


@lru_cache(maxsize=1)
def _strong_parts():
    from ml import train
    from ml.entry_probability import probability_metadata
    from ml.financial_targets import checked_financial_frame
    from ml.primary_champion import training_provenance
    data, proof = checked_financial_frame(population())
    result = train._evaluate_candidate(name="synthetic_champion", model_family="isolated_logreg",
        builder=train._fit_logreg_calibrated, x_cols=["price_pct_5m"], use_forward=True,
        tr_df=data.iloc[:180], te_df=data.iloc[180:])
    model = train._fit_logreg_calibrated(data, ["price_pct_5m"])
    meta = {**probability_metadata(model, result.probability_evaluation), "features": ["price_pct_5m"],
        "activation_ready": True, "financial_training": proof, "training_provenance": training_provenance(data),
        "validation_split": {"label_availability_purged": True}, "feature_set_hash": "synthetic"}
    return model, meta


def champion_artifact(root, monkeypatch, *, name="champion", threshold=.73, start="2026-09-10", prefix="Later",
                      positive_feature=80.):
    from ml import model_registry as registry
    from ml.primary_champion import current_incumbent, authorize_candidate
    from types import SimpleNamespace
    monkeypatch.setattr(registry, "MODELS_DIR", root / "ml" / "models")
    monkeypatch.setattr(registry, "REGISTRY_PATH", root / "ml" / "registry.json")
    monkeypatch.setattr(registry, "PROJECT_ROOT", root)
    monkeypatch.setattr(registry, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=False))
    model, meta = deepcopy(_strong_parts())
    meta["threshold_result"] = {"picked": threshold, "activation_ready": True}
    lanes = {"global": {"threshold": threshold, "activation_ready": True}, "by_lane": {}}
    artifact = registry.write_candidate(model=model, meta=meta, thresholds=lanes, model_id=name)
    alias = root / "ml" / "model.pkl"
    incumbent = current_incumbent(registry_path=registry.REGISTRY_PATH, models_dir=registry.MODELS_DIR, model_alias=alias)
    cohort = population(60, start=start, prefix=prefix, positive_feature=positive_feature)
    approval = authorize_candidate(artifact, cohort, incumbent=incumbent)
    return registry, artifact, approval, cohort, alias
