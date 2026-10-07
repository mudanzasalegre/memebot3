from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from ml.label_builder import RUNNER_THRESHOLDS, build_labels
from ml.temporal_validation import purged_temporal_windows
from ml.family_training import train_classifier_family, train_regressor_family
from ml.outcome_targets import enrich_outcome_targets


def _frame(rows=80):
    times = pd.date_range("2026-10-01", periods=rows, freq="10min", tz="UTC")
    return pd.DataFrame({
        "address": [f"mint{i}" for i in range(rows)], "timestamp": times,
        "ts": times + pd.Timedelta(minutes=2),
        "entry_lane": ["a" if i % 2 else "b" for i in range(rows)],
        "price_pct_5m": [50 if i % 2 else -10 for i in range(rows)],
        "liquidity_usd": np.arange(rows) * 100 + 20000,
        "max_pnl_pct_seen": [1500 if i % 2 else 5 for i in range(rows)],
        "target_total_pnl_pct": [100 if i % 2 else -15 for i in range(rows)],
    })


def test_extreme_runner_labels_preserve_missing_and_coalesce_aliases():
    frame = pd.DataFrame({"max_pnl_seen": [None, 10001, np.inf], "max_pnl_pct_seen": [2200, None, None],
                          "target_total_pnl_pct": [50, 800, 1000]})
    labels = build_labels(frame)
    assert labels.loc[0, "runner_2000"] == 1
    assert labels.loc[0, "runner_5000"] == 0
    assert labels.loc[1, "runner_10000"] == 1
    assert pd.isna(labels.loc[2, "runner_100"])
    assert pd.isna(labels.loc[2, "ev_peak_adjusted"])
    assert pd.isna(build_labels(pd.DataFrame({"max_pnl_pct_seen": [None]})).loc[0, "is_winner"])


def test_windows_purge_unsettled_labels_and_repeated_tokens():
    frame = _frame(40)
    frame.loc[0, "address"] = frame.loc[12, "address"]
    frame.loc[1, "ts"] = frame.loc[20, "timestamp"]
    windows, metadata = purged_temporal_windows(frame, min_train_rows=5)
    assert len(windows) == 3
    assert any(fold["purged_shared_tokens"] for fold in metadata["folds"])
    assert any(fold["purged_unsettled_or_embargo"] for fold in metadata["folds"])
    for train, test in windows:
        assert set(frame.iloc[train].address).isdisjoint(frame.iloc[test].address)
        assert frame.iloc[train].ts.max() < frame.iloc[test].timestamp.min() - pd.Timedelta(seconds=60)


@pytest.mark.parametrize("column", ["timestamp", "ts", "address"])
def test_missing_timing_or_identity_is_not_invented(column):
    frame = _frame().drop(columns=column)
    windows, metadata = purged_temporal_windows(frame)
    assert not windows
    assert metadata["eligible_rows"] == 0


def test_equal_timestamps_never_cross_validation_boundary():
    frame = _frame(40)
    frame.loc[9:11, "timestamp"] = frame.loc[10, "timestamp"]
    windows, _ = purged_temporal_windows(frame, min_train_rows=5)
    for train, test in windows:
        assert set(frame.iloc[train].timestamp).isdisjoint(frame.iloc[test].timestamp)


def test_classifier_metrics_are_out_of_sample_with_model_metadata(tmp_path):
    report = train_classifier_family(family="runner", targets=["runner_1000"], feature_set_name="runner_features",
                                     frame=_frame(), output_dir=tmp_path, min_rows=10)
    target = report["targets"]["runner_1000"]
    assert target["validation"]["mode"] == "purged_token_walk_forward"
    assert target["validation"]["temporal"]["out_of_sample_rows"] == 60
    assert target["validation"]["ready_for_enforcement"] is False
    assert "in_sample_only" not in target["validation"]["warnings"]
    assert target["brier_score"] is not None
    metadata = json.loads((tmp_path / "runner_1000.meta.json").read_text())
    assert metadata["use"] == "advisory_only"
    assert len(metadata["model_sha256"]) == 64
    assert "max_pnl_pct_seen" not in metadata["features"]
    assert len(metadata["features"]) == len(set(metadata["features"]))


def test_unlabelled_runner_rows_do_not_become_negatives(tmp_path):
    frame = _frame()
    frame.loc[0:9, "max_pnl_pct_seen"] = np.nan
    report = train_classifier_family(family="runner", targets=["runner_100"], feature_set_name="runner_features",
                                     frame=frame, output_dir=tmp_path, min_rows=10)
    assert report["targets"]["runner_100"]["target_rows"] == 70
    assert report["targets"]["runner_100"]["unlabelled_rows"] == 10


def test_regression_reports_only_temporal_mae(tmp_path):
    report = train_regressor_family(family="ev", targets=["ev_realized"], feature_set_name="ev_features",
                                    frame=_frame(), output_dir=tmp_path, min_rows=10)
    target = report["targets"]["ev_realized"]
    assert target["validation"]["mode"] == "purged_token_walk_forward"
    assert target["mae"] is not None
    report = train_regressor_family(family="ev", targets=["ev_realized"], feature_set_name="ev_features",
                                    frame=_frame().drop(columns="ts"), output_dir=tmp_path, min_rows=10)
    assert report["targets"]["ev_realized"]["mae"] is None


def test_peak_join_enriches_targets_only_and_rejects_future_feature(tmp_path):
    times = pd.Timestamp("2026-10-01T00:00:00Z")
    frame = pd.DataFrame({"address": ["CaseMint", "casemint", "CaseMint"],
                          "timestamp": [times, times, times + pd.Timedelta(minutes=1)],
                          "ts": [times + pd.Timedelta(minutes=5)] * 3,
                          "target_total_pnl_pct": [20.0] * 3, "liquidity_usd": [1000.0] * 3})
    row = {"event_type": "candidate_outcome", "address": "CaseMint", "opened_at": times.isoformat(),
           "ts_utc": (times + pd.Timedelta(minutes=5)).isoformat(), "pnl_pct": 20,
           "max_pnl_pct_seen": 3500, "liquidity_usd": 99999,
           "features_snapshot": {"liquidity_usd": 99999}}
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(json.dumps(row) + "\n")
    result = enrich_outcome_targets(frame, tmp_path)
    assert result.loc[0, "max_pnl_pct_seen"] == 3500
    assert pd.isna(result.loc[1, "max_pnl_pct_seen"])
    assert pd.isna(result.loc[2, "max_pnl_pct_seen"])
    assert result.liquidity_usd.tolist() == [1000.0] * 3


def test_model_runtime_caches_and_rejects_corruption(tmp_path, monkeypatch):
    import analytics.model_runtime_common as runtime
    path = tmp_path / "ml" / "models" / "runner"
    train_classifier_family(family="runner", targets=["runner_100"], feature_set_name="runner_features",
                            frame=_frame(), output_dir=path, min_rows=10)
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    actual_load = runtime.joblib.load
    calls = []
    monkeypatch.setattr(runtime.joblib, "load", lambda p: (calls.append(p), actual_load(p))[1])
    assert runtime.predict_model("runner", "runner_100", {"price_pct_5m": 50}) is not None
    assert runtime.predict_model("runner", "runner_100", {"price_pct_5m": 50}) is not None
    assert len(calls) == 1
    (path / "runner_100.pkl").write_bytes(b"broken")
    assert runtime.predict_model("runner", "runner_100", {}) is None
    assert runtime.predict_model("runner", "missing", {}) is None


def test_insample_family_model_cannot_be_used_by_runtime(tmp_path, monkeypatch):
    import analytics.model_runtime_common as runtime
    train_classifier_family(family="runner", targets=["runner_100"], feature_set_name="runner_features",
                            frame=_frame().drop(columns="ts"), output_dir=tmp_path / "ml" / "models" / "runner", min_rows=10)
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    assert runtime.predict_model("runner", "runner_100", {"price_pct_5m": 50}) is None


def test_nested_runner_probabilities_cannot_increase(monkeypatch):
    import analytics.runner_model_runtime as runtime
    monkeypatch.setattr(runtime, "predict_model", lambda family, target, vec: .5 if target == "runner_50" else .9)
    probabilities = runtime.predict_runner_probabilities({})
    assert all(probabilities[f"runner{threshold}_proba"] == .5 for threshold in RUNNER_THRESHOLDS)


def test_feature_store_keeps_outcomes_separate_and_preserves_legacy(tmp_path, monkeypatch):
    from features import store
    from features.builder import ALLOWED_FEATURES
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    now = dt.datetime.now(dt.timezone.utc)
    store.append({"address": "a", "timestamp": now}, 1, target_total_pnl_pct=20,
                 sample_type="shadow_close", outcome_targets={"max_pnl_pct_seen": 2400, "outcome_closed_at": now})
    store.append({"address": "b", "timestamp": now}, 0, target_total_pnl_pct=-5, sample_type="shadow_close")
    data = pd.read_parquet(next(tmp_path.glob("features_*.parquet")))
    assert data.loc[0, "max_pnl_pct_seen"] == 2400
    assert pd.isna(data.loc[1, "max_pnl_pct_seen"])
    assert "max_pnl_pct_seen" not in ALLOWED_FEATURES
    assert "outcome_closed_at" not in ALLOWED_FEATURES
    with pytest.raises(ValueError):
        store.append({}, 0, outcome_targets={"future_price": 10})


def test_walk_forward_report_runs_with_current_pandas():
    from ml.walk_forward import walk_forward_report
    report = walk_forward_report(_frame())
    assert report["windows"] == 3


def test_feature_matrix_never_sends_infinity_to_models():
    from ml.feature_matrix import coerce_feature_frame
    frame = coerce_feature_frame(pd.DataFrame({"value": [np.inf, -np.inf, np.nan, 5]}), ["value"])
    assert frame.value.tolist() == [0, 0, 0, 5]
