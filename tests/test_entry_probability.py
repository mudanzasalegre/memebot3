"""Real synthetic temporal calibration; not live fills or profit evidence."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.dummy import DummyClassifier

from ml import entry_probability as probability
from primary_probability_fixtures import primary_probability_parts


def data(n=180):
    times = pd.date_range("2026-09-01", periods=n, freq="10min", tz="UTC")
    return pd.DataFrame({"address": [f"Mint{i}" for i in range(n)], "mint": [f"Mint{i}" for i in range(n)],
        "timestamp": times, "ts": times + pd.Timedelta(minutes=2), "label": np.arange(n) % 2,
        "price_pct_5m": np.where(np.arange(n) % 2, 80., 5.), "row_identity": np.arange(n),
        "target_total_pnl_pct": np.where(np.arange(n) % 2, 200., -60.)})


class RecordingClassifier(ClassifierMixin, BaseEstimator):
    def fit(self, X, y):
        self.classes_ = np.asarray([0, 1])
        self.seen_rows_ = X.row_identity.tolist()
        self.n_features_in_ = len(X.columns)
        return self

    def predict_proba(self, X):
        value = np.where(X.row_identity.to_numpy() % 2, .9, .1)
        return np.c_[1 - value, value]

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= .5).astype(int)


def test_actual_calibration_excludes_reentries_and_not_yet_closed_labels_and_does_not_refit_base():
    frame = data()
    frame.loc[0, "ts"] = frame.loc[140, "timestamp"]
    frame.loc[130, ["address", "mint"]] = "Mint1"
    model = probability.fit_primary_probability(RecordingClassifier(), frame[["row_identity"]], frame.label, frame)
    assert model.calibrated_model is not None
    assert not {0, 1} & set(model.base_model.seen_rows_)
    assert max(model.base_model.seen_rows_) < 120
    assert len(model.base_model.seen_rows_) == model.calibration_["fit_rows"]
    assert model.calibration_["calibration_rows"] == 60
    assert model.calibration_["timing"]["folds"][-1]["purged_shared_tokens"] >= 1
    assert model.predict_proba(frame[["row_identity"]]).shape == (180, 2)
    assert max(model.base_model.seen_rows_) < 120  # Prediction/calibration did not refit on later rows.


def test_calibration_uses_event_time_not_input_row_order():
    frame = data().sample(frac=1, random_state=831)
    model = probability.fit_primary_probability(RecordingClassifier(), frame[["row_identity"]], frame.label, frame)
    assert max(model.base_model.seen_rows_) < 120 and model.calibration_["calibration_rows"] == 60


@pytest.mark.parametrize("column,value", [("timestamp", None), ("ts", None), ("address", None),
    ("timestamp", "2100-01-01T00:00:00Z"), ("ts", "2020-01-01T00:00:00Z"), ("label", 2)])
def test_missing_future_backwards_or_nonbinary_inputs_cannot_calibrate(column, value):
    frame = data()
    if column == "address":
        frame.loc[0, "mint"] = None
    frame.loc[0, column] = value
    with pytest.raises(ValueError, match="Primary calibration"):
        probability.fit_primary_probability(RecordingClassifier(), frame[["row_identity"]], frame.label, frame)


def test_insufficient_independent_calibration_classes_keeps_rank_unknown_as_probability():
    frame = data()
    frame.loc[frame.label == 1, ["mint", "address"]] = "OnePositiveMint"
    model = probability.fit_primary_probability(RecordingClassifier(), frame[["row_identity"]], frame.label, frame)
    assert model.calibrated_model is None
    assert model.rank_score(frame[["row_identity"]]).max() == .9
    with pytest.raises(ValueError, match="Uncalibrated rank"):
        model.predict_proba(frame[["row_identity"]])
    metadata = probability.probability_metadata(model, primary_probability_parts()[1]["probability_evaluation"])
    assert not probability.supported_entry_probability(metadata)


@pytest.mark.parametrize("builder_name", ["_fit_logreg_calibrated", "_fit_lightgbm_small"])
def test_both_actual_primary_builders_have_later_calibration_and_independent_oos_probability_skill(builder_name):
    from ml import train
    frame = data(240)
    builder = getattr(train, builder_name)
    candidate = train._evaluate_candidate(name=builder_name, model_family="isolated", builder=builder,
        x_cols=["price_pct_5m"], use_forward=True, tr_df=frame.iloc[:180], te_df=frame.iloc[180:])
    model = builder(frame, ["price_pct_5m"])
    metadata = probability.probability_metadata(model, candidate.probability_evaluation)
    assert probability.supported_entry_probability(metadata) and probability.supported_entry_model(model, metadata)
    assert model.calibration_["fit_rows"] == 160 and model.calibration_["calibration_rows"] == 80
    assert candidate.val_preds.baseline_probability.unique().tolist() == [.5]
    assert candidate.probability_evaluation["positive_tokens"] == 30
    assert candidate.probability_evaluation["brier_skill_score"] > .8
    assert train._extract_feature_signal(model, ["price_pct_5m"])
    if builder_name == "_fit_logreg_calibrated":
        assert model.base_model.named_steps["scaler"].n_samples_seen_ == 160
    else:
        assert model.base_model.n_jobs == 1 and hasattr(model.base_model, "predict_proba")


@pytest.mark.parametrize("problem", ["shared_token", "late_close", "future_train_entry", "missing_test_time"])
def test_outer_evaluation_cannot_accept_forged_unpurged_boundaries(problem):
    from ml import train
    frame = data()
    if problem == "shared_token":
        frame.loc[140, ["mint", "address"]] = "Mint1"
    elif problem == "late_close":
        frame.loc[0, "ts"] = frame.loc[120, "timestamp"]
    elif problem == "future_train_entry":
        frame.loc[0, "timestamp"] = frame.loc[125, "timestamp"]
        frame.loc[0, "ts"] = frame.loc[125, "ts"]
    else:
        frame.loc[140, "timestamp"] = None
    with pytest.raises(ValueError, match="Outer probability evaluation"):
        train._evaluate_candidate(name="invalid", model_family="isolated", builder=train._fit_logreg_calibrated,
            x_cols=["price_pct_5m"], use_forward=True, tr_df=frame.iloc[:120], te_df=frame.iloc[120:])


def test_walk_forward_probability_evidence_contains_separate_inner_and_outer_windows():
    from ml import train
    frame = data(240)
    splits = [(np.arange(120), np.arange(120, 180)), (np.arange(180), np.arange(180, 240))]
    candidate = train._evaluate_candidate(name="walk", model_family="isolated", builder=train._fit_logreg_calibrated,
        x_cols=["price_pct_5m"], use_forward=False, full_df=frame, cv_splits=splits)
    evidence = candidate.probability_evaluation
    assert evidence["rows"] == 120 and evidence["validation_ready"]
    assert len(evidence["folds"]) == 2 and set(candidate.val_preds.fold) == {1, 2}
    for fold in evidence["folds"]:
        assert probability._calibration_supported(fold["calibration"])
        assert probability._boundary_supported(fold["outer_boundary"])
        assert fold["calibration"]["timing"]["folds"][-1]["test_end"] < fold["outer_boundary"]["test_start"]


def test_unsupported_early_calibration_does_not_discard_supported_later_windows():
    from ml import train
    frame = data()
    frame.loc[:39, "label"] = [1] * 4 + [0] * 36
    frame["price_pct_5m"] = np.where(frame.label, 80., 5.)
    splits = [(np.arange(40), np.arange(40, 80)), (np.arange(120), np.arange(120, 180))]
    candidate = train._evaluate_candidate(name="later", model_family="isolated", builder=train._fit_logreg_calibrated,
        x_cols=["price_pct_5m"], use_forward=False, full_df=frame, cv_splits=splits)
    evidence = candidate.probability_evaluation
    assert evidence["rows"] == 60 and evidence["validation_ready"]
    assert len(evidence["skipped_folds"]) == 1 and evidence["skipped_folds"][0]["fold"] == 1
    assert set(candidate.val_preds.fold) == {2} and len(evidence["folds"]) == 1


def test_holdout_acceptance_counts_only_actual_supported_probability_rows(monkeypatch):
    from ml import train
    from ml.financial_targets import checked_financial_frame
    from net_financial_fixtures import net_frame
    quality = SimpleNamespace(passed=True, holdout_rows=100, holdout_positives=50)
    tune = {"activation_ready": True, "objective_applied": "expected_pnl_precision_floor", "precision_at_picked": 1.,
            "avg_realized_pnl_pct_at_picked": 100., "realized_selected_rows_at_picked": 100}
    monkeypatch.setattr(train, "CFG", SimpleNamespace(ML_MIN_HOLDOUT_ROWS=41, ML_MIN_HOLDOUT_POSITIVES=8))
    result = train._enforcement_gates(quality, tune, checked_financial_frame(net_frame(data(4)))[1],
        {"label_availability_purged": True}, primary_probability_parts()[1])
    assert not result["activation_ready"] and "holdout_rows" in result["blockers"]
    assert result["checks"]["holdout_rows"] == 40 and result["checks"]["dataset_split_holdout_rows"] == 100


def test_baseline_is_older_mature_train_prevalence_not_future_validation_prevalence():
    model, metadata = primary_probability_parts()
    evaluation = metadata["probability_evaluation"]
    assert evaluation["positives"] / evaluation["rows"] == .5
    assert evaluation["baseline_brier_score"] == pytest.approx(.34)
    assert evaluation["brier_score"] == pytest.approx(.25)
    assert probability.supported_entry_model(model, metadata)


def test_repeated_test_decisions_cannot_inflate_independent_token_support():
    from ml import train
    frame = data()
    frame.loc[120:, "mint"] = [f"Repeated{i % 8}" for i in range(60)]
    frame.loc[120:, "address"] = frame.loc[120:, "mint"]
    candidate = train._evaluate_candidate(name="repeated", model_family="isolated", builder=train._fit_logreg_calibrated,
        x_cols=["price_pct_5m"], use_forward=True, tr_df=frame.iloc[:120], te_df=frame.iloc[120:])
    assert candidate.probability_evaluation["rows"] == 60
    assert candidate.probability_evaluation["unique_tokens"] == 8
    assert not candidate.probability_evaluation["validation_ready"]


@pytest.mark.parametrize("path,value", [
    ("probability_contract_version", "old"), ("prediction_kind", "rank_score"),
    ("probabilities_calibrated", 1), ("probability_validation_ready", "true"),
    ("calibration_sha256", "0" * 64), ("calibration.calibrated", False),
    ("calibration.fit_rows", 19), ("calibration.calibration_rows", 11),
    ("calibration.fit_positives", 1), ("calibration.calibration_positives", 1),
    ("calibration.calibration_tokens_by_class.1", 2),
    ("calibration.timing.embargo_seconds", 0),
    ("probability_evaluation.mode", "in_sample_only"), ("probability_evaluation.validation_ready", 1),
    ("probability_evaluation.rows", True), ("probability_evaluation.unique_tokens", 20),
    ("probability_evaluation.positives", None), ("probability_evaluation.positives", 40),
    ("probability_evaluation.positives", True),
    ("probability_evaluation.positive_tokens", 4), ("probability_evaluation.negative_tokens", 4),
    ("probability_evaluation.brier_score", float("nan")),
    ("probability_evaluation.baseline_brier_score", 0.), ("probability_evaluation.brier_skill_score", 1.),
    ("probability_evaluation.cluster_skill.validation_ready", False),
    ("probability_evaluation.cluster_skill.lower_loss_improvement", 0.),
    ("probability_evaluation.cluster_skill.unique_tokens", 50),
    ("probability_evaluation.folds", []),
])
def test_flags_cannot_override_broken_calibration_or_oos_support(path, value):
    metadata = primary_probability_parts()[1]
    cell = metadata
    keys = path.split(".")
    for key in keys[:-1]:
        cell = cell[int(key) if int_key(cell, key) else key]
    key = int(keys[-1]) if int_key(cell, keys[-1]) else keys[-1]
    cell[key] = value
    if path.startswith("calibration."):
        metadata["calibration_sha256"] = probability._digest(metadata["calibration"])
    assert not probability.supported_entry_probability(metadata)


def int_key(cell, key):
    return key.isdigit() and int(key) in cell


@pytest.mark.parametrize("corrupt", ["version", "different_calibration", "raw_classifier"])
def test_model_bytes_must_match_the_actual_primary_calibration_contract(corrupt):
    model, metadata = primary_probability_parts()
    if corrupt == "version":
        model.entry_probability_version_ = "old"
    elif corrupt == "different_calibration":
        model.calibration_["fit_positives"] += 1
    else:
        model = DummyClassifier(strategy="prior").fit([[0], [1]], [0, 1])
    assert not probability.supported_entry_model(model, metadata)


@pytest.mark.parametrize("problem", ["base_classes", "calibration_classes", "different_base", "multiple_estimators"])
def test_calibrator_must_stay_bound_to_the_same_binary_base_estimator(problem):
    model, metadata = primary_probability_parts()
    if problem == "base_classes":
        model.base_model.classes_ = np.asarray([1, 0])
    elif problem == "calibration_classes":
        model.calibrated_model.classes_ = np.asarray([1, 0])
    elif problem == "different_base":
        model.base_model = deepcopy(model.base_model)
    else:
        model.calibrated_model.calibrated_classifiers_.append(model.calibrated_model.calibrated_classifiers_[0])
    assert not probability.supported_entry_model(model, metadata)


@pytest.mark.parametrize("problem", ["unpurged", "shared", "late_inner", "small_fit",
    "inconsistent_rows", "impossible_class_tokens", "missing_calibration"])
def test_outer_fold_and_calibration_population_consistency_is_rechecked(problem):
    metadata = primary_probability_parts()[1]
    fold = metadata["probability_evaluation"]["folds"][0]
    if problem == "unpurged":
        fold["outer_boundary"]["label_availability_purged"] = False
    elif problem == "shared":
        fold["outer_boundary"]["token_disjoint"] = False
    elif problem == "late_inner":
        fold["calibration"]["timing"]["folds"][-1]["test_end"] = fold["outer_boundary"]["test_start"]
    elif problem == "small_fit":
        fold["outer_boundary"]["train_rows"] = 20
    elif problem == "inconsistent_rows":
        fold["outer_boundary"]["test_rows"] += 1
    elif problem == "impossible_class_tokens":
        metadata["calibration"]["fit_tokens_by_class"][1] = 99999
        metadata["calibration_sha256"] = probability._digest(metadata["calibration"])
    else:
        fold["calibration"] = None
    assert not probability.supported_entry_probability(metadata)


def test_probability_metadata_is_detached_from_model_and_evaluation():
    model, original = primary_probability_parts()
    output = probability.probability_metadata(model, original["probability_evaluation"])
    output["calibration"]["fit_positives"] = 99999
    output["probability_evaluation"]["rows"] = 99999
    assert model.calibration_["fit_positives"] != 99999
    assert original["probability_evaluation"]["rows"] == 40


def test_candidate_selection_prefers_supported_probabilities_over_an_unsupported_high_ev():
    from ml.train import CandidateResult, _candidate_rank
    def candidate(ready, score):
        return CandidateResult(name="isolated", model_family="test",
            tune_result={"activation_ready": True, "selection_metric": "avg_realized_pnl_pct_at_picked",
                         "selection_score": score}, auc_mean=.8, ap_mean=.8, precision_at_k=.8,
            val_preds=pd.DataFrame(), feature_signal=[], probability_evaluation={"validation_ready": ready})
    assert _candidate_rank(candidate(True, 5.)) > _candidate_rank(candidate(False, 99999.))


def test_bad_probability_metadata_is_rejected_before_deserialization(tmp_path, monkeypatch):
    from test_financial_model_acceptance import setup_entry
    runtime, _, artifact = setup_entry(tmp_path, monkeypatch)
    metadata = json.loads(artifact.meta_path.read_text())
    metadata["probability_validation_ready"] = False
    artifact.meta_path.write_text(json.dumps(metadata))
    monkeypatch.setattr(runtime.joblib, "load", lambda *_: (_ for _ in ()).throw(AssertionError("must not deserialize")))
    assert runtime.should_buy({"price_pct_5m": 5}) is None
    assert not runtime.model_runtime_status()["probability_validation_ready"]
    assert runtime.entry_prediction_state() == {"activation_ready": False, "metadata": {}}


def test_matching_checksum_and_valid_metadata_cannot_turn_raw_classifier_into_probability(tmp_path, monkeypatch):
    from test_financial_model_acceptance import setup_entry
    runtime, registry, original = setup_entry(tmp_path, monkeypatch)
    raw = DummyClassifier(strategy="prior").fit([[0], [1]], [0, 1])
    artifact = registry.write_candidate(model=raw, meta=json.loads(original.meta_path.read_text()), model_id="raw")
    monkeypatch.setattr(runtime, "_MODEL_PATH", artifact.model_path)
    monkeypatch.setattr(runtime, "_META_PATH", artifact.meta_path)
    assert runtime.should_buy({}) is None
    active = tmp_path / "active.pkl"
    active.write_bytes(b"preserved")
    with pytest.raises(RuntimeError, match="model/calibration"):
        registry.promote_candidate(artifact, active_model_path=active)
    assert active.read_bytes() == b"preserved" and not registry.REGISTRY_PATH.exists()


def test_no_probability_contract_is_neutral_not_an_entry_veto(tmp_path, monkeypatch):
    from test_financial_model_acceptance import setup_entry
    from analytics.ml_policy import decide_ml_action
    runtime, _, artifact = setup_entry(tmp_path, monkeypatch)
    metadata = json.loads(artifact.meta_path.read_text())
    metadata.pop("probability_contract_version")
    artifact.meta_path.write_text(json.dumps(metadata))
    prediction = runtime.should_buy({})
    action = decide_ml_action(token={"entry_lane": "pump_early_pumpswap_profit"}, feature_row={}, proba=prediction,
        base_rules_passed=True, dry_run=True, live=False, entry_model_activation_ready=False)
    assert prediction is None and action.allow_buy and not action.enforce and action.sizing_multiplier == 1


def test_actual_train_save_and_checked_promotion_persist_probability_evidence(tmp_path, monkeypatch):
    from ml import train, model_registry as registry
    from net_financial_fixtures import net_frame
    from test_ml_pipeline_reliability_pr10 import _patch_train_paths
    _patch_train_paths(monkeypatch, tmp_path)
    source = net_frame(data(240))
    monkeypatch.setattr(train, "_load_dataset", lambda: source.copy())
    monkeypatch.setattr(train, "TRAIN_WINDOW_DAYS", None)
    monkeypatch.setattr(train, "HOLDOUT_DAYS", None)
    monkeypatch.setattr(train, "HOLDOUT_PCT", .25)
    monkeypatch.setattr(train, "_productive_regime_mask", lambda frame, **kw: pd.Series(True, index=frame.index))
    monkeypatch.setattr(train, "_productive_dex_mask", lambda frame, **kw: (pd.Series(True, index=frame.index), {}))
    monkeypatch.setattr(train, "_productive_lane_mask", lambda frame, **kw: (pd.Series(True, index=frame.index), {}))
    cfg = SimpleNamespace(**vars(train.CFG))
    for name, value in {"ML_MIN_DATASET_ROWS": 80, "ML_MIN_POSITIVES": 20, "ML_MIN_UNIQUE_TOKENS": 80,
        "ML_MIN_REALIZED_RETURN_ROWS": 50, "ML_MIN_NON_CONSTANT_FEATURES": 1,
        "ML_MIN_HOLDOUT_ROWS": 40, "ML_MIN_HOLDOUT_POSITIVES": 10,
        "ML_TUNE_OBJECTIVE": "expected_pnl_precision_floor", "ML_TUNE_MIN_SELECTED": 10,
        "ML_TUNE_MIN_REALIZED_SELECTED": 5, "ML_TUNE_PRECISION_FLOOR": .6,
        "STRATEGY_OPTIMIZATION_LOCK": False}.items():
        setattr(cfg, name, value)
    monkeypatch.setattr(train, "CFG", cfg)
    monkeypatch.setattr(registry, "CFG", cfg)
    monkeypatch.setattr(registry, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path / "ml" / "models")
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(train, "SEGMENT_JSON", tmp_path / "segment.json")
    monkeypatch.setattr(train, "write_segment_outputs", lambda *args, **kw: None)
    monkeypatch.setattr(train, "_candidate_builders", lambda: [("logreg_calibrated", "sklearn_logreg", train._fit_logreg_calibrated)])
    result = train.train_and_save()
    assert result.trained and result.active_promoted
    metadata = json.loads(Path(result.meta_path).read_text())
    assert probability.supported_entry_probability(metadata)
    assert probability.supported_entry_model(joblib.load(result.model_path), metadata)
    assert metadata["enforcement_gates"]["checks"]["temporal_probability_ready"]
    assert "baseline_probability" in pd.read_csv(result.val_preds_path)


@pytest.mark.parametrize("promoted,new_valid,old_valid,expected", [(False, True, False, False),
    (True, True, False, True), (True, False, True, False)])
def test_retrain_reports_actual_promotion_and_cannot_restore_legacy_from_an_incomparable_score(
        tmp_path, monkeypatch, promoted, new_valid, old_valid, expected):
    from ml import retrain
    model_path, meta_path, threshold_path = tmp_path / "model.pkl", tmp_path / "model.meta.json", tmp_path / "threshold.json"
    good = primary_probability_parts()[1]
    old = {**(deepcopy(good) if old_valid else {}), "model_selection_metric": "avg_realized_pnl_pct_at_picked", "model_selection_score": 99999.}
    model_path.write_bytes(b"old")
    meta_path.write_text(json.dumps(old))
    threshold_path.write_text(json.dumps({"picked": .4}))
    monkeypatch.setattr(retrain, "MODEL_PATH", model_path)
    monkeypatch.setattr(retrain, "META_PATH", meta_path)
    monkeypatch.setattr(retrain, "RECOMMENDED_JSON", threshold_path)
    def attempt():
        if promoted:
            model_path.write_bytes(b"new")
            meta_path.write_text(json.dumps({**(good if new_valid else {}), "model_selection_metric": "avg_realized_pnl_pct_at_picked", "model_selection_score": 100.}))
        return SimpleNamespace(trained=True, active_promoted=promoted, status="trained")
    monkeypatch.setattr(retrain, "train_and_save", attempt)
    assert retrain.retrain_if_better() is expected
    assert model_path.read_bytes() == (b"new" if expected else b"old")
    assert not list(tmp_path.glob("*.bkup.*"))


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), None])
def test_retrain_selection_ignores_nonfinite_or_boolean_metrics(value):
    from ml.retrain import _selection
    assert _selection({"model_selection_metric": "metric", "model_selection_score": value,
                       "auc_pr_forward_or_cv_mean": value}) == (None, None)
