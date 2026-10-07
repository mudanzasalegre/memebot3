"""All evidence in this module is synthetic; none establishes trading profit."""
from __future__ import annotations

import dataclasses
import json
from hashlib import sha256

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.dummy import DummyClassifier, DummyRegressor

from ml.family_training import train_classifier_family, train_regressor_family
from ml.label_builder import build_labels
from ml.prediction_validation import paired_token_loss_check, regression_error_check
from ml.risk_model import severe_loss_labels


def _frame(n=160):
    times = pd.date_range("2026-09-01", periods=n, freq="10min", tz="UTC")
    return pd.DataFrame({"address": [f"Token{i}" for i in range(n)], "timestamp": times,
                         "ts": times + pd.Timedelta(minutes=2), "entry_lane": ["a", "b"] * (n // 2),
                         "price_pct_5m": [5.0, 80.0] * (n // 2),
                         "target_total_pnl_pct": [-60.0, 200.0] * (n // 2),
                         "max_pnl_pct_seen": [2.0, 25000.0] * (n // 2),
                         "max_pnl_after_seen_1m": [-5.0, 12000.0] * (n // 2),
                         "max_pnl_after_seen_3m": [-10.0, 20000.0] * (n // 2)})


def _configured_paths(module, root, name, monkeypatch):
    monkeypatch.setattr(module, "MODEL_PATH", root / "ml" / f"{name}_model.pkl")
    monkeypatch.setattr(module, "META_PATH", root / "ml" / f"{name}_model.meta.json")
    if hasattr(module, "VAL_PREDS"):
        monkeypatch.setattr(module, "VAL_PREDS", root / "data" / "metrics" / f"{name}_val_preds.csv")
    if hasattr(module, "THRESHOLDS_JSON"):
        monkeypatch.setattr(module, "THRESHOLDS_JSON", root / "data" / "metrics" / f"{name}_thresholds.json")


def test_missing_risk_returns_never_become_healthy_labels():
    labels = severe_loss_labels(pd.DataFrame({"target_total_pnl_pct": [None, np.nan, np.inf, -np.inf, -31, 10]}))
    assert labels.iloc[:4].isna().all()
    assert labels.iloc[4:].tolist() == [1, 0]
    assert severe_loss_labels(pd.DataFrame(index=[0, 1])).isna().all()
    all_labels = build_labels(pd.DataFrame({"target_total_pnl_pct": [None, -50, -50],
                                         "exit_reason": ["LIQUIDITY_CRUSH", None, "LIQUIDITY_CRUSH"]}))
    assert all_labels.loc[:1, "liquidity_crush_loss"].isna().all()
    assert all_labels.loc[:1, "toxic_exit_loss"].isna().all()
    assert all_labels.loc[2, "liquidity_crush_loss"] == 1


def test_repeated_token_rows_cannot_fabricate_independent_skill():
    result = paired_token_loss_check(["A"] * 1000, [0.0] * 1000, [1.0] * 1000)
    assert result["rows"] == 1000 and result["unique_tokens"] == 1
    assert not result["validation_ready"]
    assert result["lower_loss_improvement"] is None


def test_token_cluster_skill_is_case_sensitive_and_deterministic():
    tokens = [f"Mint{i}" for i in range(15)] + [f"mint{i}" for i in range(15)]
    one = paired_token_loss_check(tokens, np.zeros(30), np.ones(30))
    assert one == paired_token_loss_check(tokens, np.zeros(30), np.ones(30))
    assert one["unique_tokens"] == 30 and one["validation_ready"]
    assert one["lower_loss_improvement"] == 1


@pytest.mark.parametrize("tokens,actual,baseline", [
    ([None] * 30, [0.0] * 30, [1.0] * 30),
    ([""] * 30, [0.0] * 30, [1.0] * 30),
    (["a"] * 30, [np.nan] * 30, [1.0] * 30),
    (["a"] * 30, [0.0] * 30, [np.inf] * 30),
    (["a"] * 30, [-1.0] * 30, [1.0] * 30),
    (["a"] * 30, [0.0] * 29, [1.0] * 30),
])
def test_invalid_loss_evidence_is_unknown(tokens, actual, baseline):
    assert not paired_token_loss_check(tokens, actual, baseline)["validation_ready"]


def test_nonpositive_or_uncertain_cluster_delta_is_not_skill():
    tokens = [f"t{i}" for i in range(30)]
    assert not paired_token_loss_check(tokens, np.ones(30), np.zeros(30))["validation_ready"]
    assert not paired_token_loss_check(tokens, np.ones(30), np.ones(30))["validation_ready"]
    # A positive row-average can be caused by repeating the same lucky token.
    tokens += ["t0"] * 300
    baseline = [-0.0] * 30 + [100.0] * 300
    actual = [10.0] * 30 + [0.0] * 300
    assert not paired_token_loss_check(tokens, actual, baseline)["validation_ready"]


def test_regression_error_envelope_uses_token_max_and_is_not_coverage_promise():
    tokens = [f"t{i}" for i in range(30)] * 2
    result = regression_error_check(tokens, [100.0] * 60, [99.0] * 30 + [90.0] * 30, [0.0] * 60)
    assert result["validation_ready"] and result["absolute_error_radius_pct_points"] == 10
    assert "not conditional or guaranteed" in result["interval_caveat"]


def test_extreme_continuation_is_not_capped_at_200_or_10000(tmp_path, monkeypatch):
    from analytics import model_runtime_common as runtime
    from analytics.continuation_model_runtime import predict_continuation
    path = tmp_path / "ml" / "models" / "continuation"
    report = train_regressor_family(family="continuation", targets=["continuation_peak_after_seen_3m"],
                                    feature_set_name="continuation_features", frame=_frame(), output_dir=path)
    target = report["targets"]["continuation_peak_after_seen_3m"]
    assert target["regression_validation_ready"]
    assert target["regression_evaluation"]["cluster_skill"]["unique_tokens"] == 120
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    result = predict_continuation({"price_pct_5m": 80.0})
    assert result["continuation_score"] > 10000
    assert result["continuation_3m_estimate"]["future_coverage_guaranteed"] is False
    assert result["continuation_1m"] is None


def test_regression_without_skill_returns_unknown_not_confident_zero(tmp_path, monkeypatch):
    from analytics import model_runtime_common as runtime
    frame = _frame()
    frame["max_pnl_after_seen_3m"] = 100.0
    path = tmp_path / "ml" / "models" / "continuation"
    report = train_regressor_family(family="continuation", targets=["continuation_peak_after_seen_3m"],
                                    feature_set_name="continuation_features", frame=frame, output_dir=path)
    assert not report["targets"]["continuation_peak_after_seen_3m"]["regression_validation_ready"]
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    assert runtime.predict_model("continuation", "continuation_peak_after_seen_3m", {}) is None
    assert runtime.predict_regression_estimate("continuation", "continuation_peak_after_seen_3m", {})["status"] == "unknown"


@pytest.mark.parametrize("module_name,name,is_classifier", [
    ("analytics.risk_predict", "risk", True), ("analytics.ev_predict", "ev", False),
])
def test_old_in_sample_and_corrupt_compatibility_models_do_not_bypass_checks(tmp_path, monkeypatch, module_name, name, is_classifier):
    import importlib
    from analytics import model_runtime_common as runtime
    module = importlib.import_module(module_name)
    _configured_paths(module, tmp_path, name, monkeypatch)
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    module.MODEL_PATH.parent.mkdir(parents=True)
    model = (DummyClassifier(strategy="prior") if is_classifier else DummyRegressor()).fit([[0], [1]], [0, 1])
    joblib.dump(model, module.MODEL_PATH)
    module.META_PATH.write_text(json.dumps({"features": ["price_pct_5m"], "rows": 10000,
                                           "model_sha256": sha256(module.MODEL_PATH.read_bytes()).hexdigest()}))
    predict = module.predict_risk if is_classifier else module.predict_ev
    assert predict({"price_pct_5m": 1}) is None
    module.MODEL_PATH.write_bytes(b"corrupt")
    module.META_PATH.write_text("[]")
    assert predict({}) is None


def test_compatibility_risk_training_exports_oos_rank_not_fit_probabilities(tmp_path, monkeypatch):
    from ml import train_risk as trainer
    from analytics import risk_predict, model_runtime_common as runtime
    _configured_paths(trainer, tmp_path, "risk", monkeypatch)
    _configured_paths(risk_predict, tmp_path, "risk", monkeypatch)
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    frame = _frame()
    frame.loc[0, "target_total_pnl_pct"] = np.nan
    report = trainer.train_risk_model(frame=frame)
    assert report["compatibility_published"]
    target = report["targets"]["severe_loss_configured"]
    assert target["unlabelled_rows"] == 1
    exported = pd.read_csv(trainer.VAL_PREDS)
    assert 0 < len(exported) < len(frame)
    assert "rank_score" in exported and "y_prob" not in exported
    assert 0 <= risk_predict.predict_risk({"price_pct_5m": 5}) <= 1
    old_hash = sha256(trainer.MODEL_PATH.read_bytes()).hexdigest()
    report = trainer.train_risk_model(frame=frame.drop(columns="ts"))
    assert not report["compatibility_published"]
    assert sha256(trainer.MODEL_PATH.read_bytes()).hexdigest() == old_hash
    monkeypatch.setattr(risk_predict, "CFG", dataclasses.replace(risk_predict.CFG, ML_SEVERE_LOSS_PCT=-50))
    assert risk_predict.predict_risk({"price_pct_5m": 5}) is None


def test_compatibility_ev_training_is_oos_and_preserves_configured_clip(tmp_path, monkeypatch):
    from ml import train_ev as trainer
    from analytics import ev_predict
    _configured_paths(trainer, tmp_path, "ev", monkeypatch)
    _configured_paths(ev_predict, tmp_path, "ev", monkeypatch)
    frame = _frame()
    frame.loc[0, "target_total_pnl_pct"] = np.inf
    report = trainer.train_ev_model(frame=frame)
    assert report["compatibility_published"]
    assert report["targets"]["ev_configured_clipped"]["unlabelled_rows"] == 1
    exported = pd.read_csv(trainer.VAL_PREDS)
    assert 0 < len(exported) < len(frame)
    assert np.isfinite(exported.target_ev).all()
    assert ev_predict.predict_ev({"price_pct_5m": 80}) == pytest.approx(200)
    monkeypatch.setattr(ev_predict, "CFG", dataclasses.replace(ev_predict.CFG, ML_EV_CLIP_MAX=1000))
    assert ev_predict.predict_ev({"price_pct_5m": 80}) is None


def test_ev_prediction_magnitude_is_never_reported_as_confidence(monkeypatch):
    from analytics import ev_model_runtime as runtime
    monkeypatch.setattr(runtime, "predict_regression_estimate", lambda *args: {"value": None, "status": "unknown"})
    monkeypatch.setattr(runtime, "predict_ev", lambda *args: 25000.0)
    monkeypatch.setattr(runtime, "predict_model", lambda *args: None)
    scores = runtime.predict_ev_scores({})
    assert scores["ev_pred_pct"] == 25000 and scores["ev_confidence"] is None
    assert scores["ev_estimate"]["status"] == "compatibility_estimate_without_interval"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1.0])
def test_invalid_error_envelope_cannot_return_ev_value(tmp_path, monkeypatch, invalid):
    from ml import train_ev as trainer
    from analytics import ev_predict
    _configured_paths(trainer, tmp_path, "ev", monkeypatch)
    _configured_paths(ev_predict, tmp_path, "ev", monkeypatch)
    assert trainer.train_ev_model(frame=_frame())["compatibility_published"]
    meta = json.loads(trainer.META_PATH.read_text())
    meta["regression_evaluation"]["absolute_error_radius_pct_points"] = invalid
    trainer.META_PATH.write_text(json.dumps(meta))
    assert ev_predict.predict_ev({"price_pct_5m": 80}) is None


def test_oos_export_refuses_multi_target_destination_before_writes(tmp_path):
    with pytest.raises(ValueError, match="requires_one_target"):
        train_classifier_family(family="runner", targets=["runner_100", "runner_500"],
                                feature_set_name="runner_features", frame=_frame(), output_dir=tmp_path,
                                validation_predictions_path=tmp_path / "predictions.csv")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("field,value", [
    ("lower_loss_improvement", float("nan")), ("lower_loss_improvement", 0.0),
    ("unique_tokens", 1), ("mean_loss_improvement", float("inf")),
])
def test_probability_ready_flag_cannot_override_invalid_cluster_evidence(tmp_path, monkeypatch, field, value):
    from ml import train_risk as trainer
    from analytics import risk_predict, model_runtime_common as runtime
    _configured_paths(trainer, tmp_path, "risk", monkeypatch)
    _configured_paths(risk_predict, tmp_path, "risk", monkeypatch)
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    assert trainer.train_risk_model(frame=_frame())["compatibility_published"]
    metadata = json.loads(trainer.META_PATH.read_text())
    metadata["validation"]["temporal"]["probability_evaluation"]["cluster_skill"][field] = value
    trainer.META_PATH.write_text(json.dumps(metadata))
    assert risk_predict.predict_risk({"price_pct_5m": 5}) is None


def test_risk30_api_never_substitutes_configured_risk50(monkeypatch):
    from analytics import risk_model_runtime as runtime
    calls = []
    monkeypatch.setattr(runtime, "predict_model", lambda *args: None)
    monkeypatch.setattr(runtime, "predict_risk", lambda vec, **kwargs: calls.append(kwargs) or None)
    assert runtime.predict_severe_loss_risk({})["risk_proba_30"] is None
    assert calls == [{"severe_loss_pct": -30}]


def test_calibration_cannot_count_one_repeated_positive_token_as_three():
    from ml.calibrated_ranker import fit_calibrated_ranker
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    frame = _frame(120)
    frame.loc[[i for i in range(80, 120) if i % 2], "address"] = "OneRepeatedWinner"
    X = frame[["price_pct_5m"]]
    y = frame.max_pnl_pct_seen.ge(10000).astype(int)
    _, details = fit_calibrated_ranker(make_pipeline(StandardScaler(), LogisticRegression(class_weight="balanced")), X, y, frame)
    assert details["calibration_tokens_by_class"][1] == 1
    assert not details["calibrated"]


def test_learning_pipeline_revision_invalidates_unchanged_dataset_cache(tmp_path, monkeypatch):
    from ml import runner_advisory_learning as learning
    first = learning.train_runner_advisory(root=tmp_path, frame=_frame())
    assert first["updated"]
    assert learning.train_runner_advisory(root=tmp_path, frame=_frame())["status"] == "unchanged"
    monkeypatch.setattr(learning, "PIPELINE_VERSION", learning.PIPELINE_VERSION + 1)
    upgraded = learning.train_runner_advisory(root=tmp_path, frame=_frame())
    assert upgraded["status"] != "unchanged"
    assert upgraded["dataset_sha256"] != first["dataset_sha256"]
