from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import joblib
import numpy as np
import pandas as pd
import pytest

from analytics import exit_model_runtime, model_runtime_common as runtime
from analytics.inference_scope import inference_scope, scoped_value
from ml.exit_diagnostics import exit_style_labels, checked_exit_metadata
from ml.family_training import train_exit_classifier


def source_frame(rows=240):
    index = np.arange(rows) % 4
    times = pd.date_range("2026-07-01", periods=rows, freq="2min", tz="UTC")
    return pd.DataFrame({"address": [f"diagnostic-{n}" for n in range(rows)],
        "timestamp": times, "ts": times + pd.Timedelta(seconds=10),
        "liquidity_usd": (index + 1) * 10000.0,
        "max_pnl_pct_seen": np.asarray([5, 5, 150, 20000])[index],
        "target_total_pnl_pct": np.asarray([1, -50, 30, 500])[index],
        "exit_profile": np.asarray(["balanced", "defensive", "runner", "moonbag"])[index]})


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    directory = tmp_path_factory.mktemp("exit-diagnostic")
    report = train_exit_classifier(frame=source_frame(), output_dir=directory)
    assert report["status"] == "ok"
    return directory, report


@pytest.fixture
def artifact(trained, tmp_path, monkeypatch):
    source, _ = trained
    destination = tmp_path / "ml" / "models" / "exit"
    destination.mkdir(parents=True)
    for path in source.iterdir():
        (destination / path.name).write_bytes(path.read_bytes())
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    return destination / "best_exit_profile.pkl"


def read_meta(path):
    return json.loads(path.with_suffix(".meta.json").read_text())


def write_meta(path, metadata):
    path.with_suffix(".meta.json").write_text(json.dumps(metadata))


def test_missing_outcomes_are_not_balanced_or_peak_proxies():
    frame = pd.DataFrame({"max_pnl_seen": [None, None, 20, 20, 150, 20000, float("inf")],
                          "target_total_pnl_pct": [None, 50000, None, -50, None, None, 10]})
    labels, source = exit_style_labels(frame)
    assert source == "observed_peak_and_realized_proxy"
    assert labels.isna().tolist() == [True, True, True, False, False, False, True]
    assert labels.dropna().tolist() == ["defensive", "runner", "moonbag"]


def test_explicit_labels_only_accept_known_styles_without_fallback():
    labels, source = exit_style_labels(pd.DataFrame({"best_exit_profile": [" Runner ", None, "wrong", 1, "bird_runner"]}))
    assert source == "provided_diagnostic_style"
    assert labels.dropna().tolist() == ["runner", "bird_runner"]
    assert labels.isna().sum() == 3


def test_boolean_outcome_values_are_not_measured_numeric_results():
    labels, _ = exit_style_labels(pd.DataFrame({"max_pnl_pct_seen": [True, 10, -101, 500],
                                               "target_total_pnl_pct": [1, False, 1, True]}))
    assert labels.isna().tolist() == [True, True, True, False]
    assert labels.iloc[-1] == "moonbag"


def test_empty_first_launch_skips_without_writing(tmp_path):
    report = train_exit_classifier(frame=pd.DataFrame(), output_dir=tmp_path)
    assert report["status"] == "skipped" and report["rows"] == 0
    assert list(tmp_path.iterdir()) == []


def test_unsupported_new_fit_preserves_existing_bytes(tmp_path):
    path = tmp_path / "best_exit_profile.pkl"
    path.write_bytes(b"original incumbent")
    meta = path.with_suffix(".meta.json")
    meta.write_bytes(b"original metadata")
    frame = source_frame().assign(liquidity_usd=10000)
    report = train_exit_classifier(frame=frame, output_dir=tmp_path)
    assert report["status"] == "skipped" and report["reason"] == "no_supported_oos_diagnostic"
    assert path.read_bytes() == b"original incumbent" and meta.read_bytes() == b"original metadata"


def test_actual_fit_has_checked_metadata_and_no_profile_predictors(trained):
    directory, report = trained
    metadata = read_meta(directory / "best_exit_profile.pkl")
    assert checked_exit_metadata(metadata)
    assert metadata["classification_evaluation"]["accuracy"] > metadata["classification_evaluation"]["baseline_accuracy"]
    assert metadata["classes"] == ["balanced", "defensive", "moonbag", "runner"]
    assert "exit_profile" not in metadata["features"]
    from features.context_encoding import FEATURE_SOURCES
    assert not any(FEATURE_SOURCES.get(name) == "exit_profile" for name in metadata["features"])
    assert metadata["model_sha256"] == sha256((directory / "best_exit_profile.pkl").read_bytes()).hexdigest()
    for fold in metadata["validation"]["temporal"]["folds"]:
        if fold["used"]:
            assert pd.Timestamp(fold["train_label_latest"]) < pd.Timestamp(fold["test_start"])
    assert report["activation_role"] == "diagnostic_only"


@pytest.mark.parametrize("liquidity,label", [(10000, "balanced"), (20000, "defensive"), (30000, "runner"), (40000, "moonbag")])
def test_actual_label_reader_is_not_probability_or_exit_permission(artifact, liquidity, label):
    result = exit_model_runtime.predict_exit_profile({"liquidity_usd": liquidity})
    assert result["exit_profile"] == label
    assert result["status"] == "validated_historical_diagnostic"
    assert not result["buy_permission"] and not result["exit_policy_permission"]
    assert result["model_selection"]["heads"]["best_exit_profile"]["model_sha256"] == read_meta(artifact)["model_sha256"]
    assert runtime.predict_model("exit", "best_exit_profile", {"liquidity_usd": liquidity}) is None
    assert scoped_value("outside", lambda: "no active scope") == "no active scope"


@pytest.mark.parametrize("field,value", [("activation_role", "paper_exit_only"), ("label_contract", "invented"),
    ("automatic_live_activation", True), ("classification_validation_ready", False),
    ("label_source", "optimal_net_exit"), ("features", ["exit_profile"]), ("classes", ["balanced", "wrong"])])
def test_invalid_diagnostic_contract_is_rejected_before_deserialization(artifact, monkeypatch, field, value):
    metadata = read_meta(artifact)
    metadata[field] = value
    write_meta(artifact, metadata)
    calls = []
    def unsafe_load(*args, **kwargs):
        calls.append(True)
        raise AssertionError("unsafe load")
    monkeypatch.setattr(runtime.joblib, "load", unsafe_load)
    assert exit_model_runtime.predict_exit_profile({"liquidity_usd": 40000})["exit_profile"] is None
    assert not calls


def test_checksum_failure_returns_unknown_not_balanced(artifact):
    artifact.write_bytes(b"broken")
    assert exit_model_runtime.predict_exit_profile({})["exit_profile"] is None


class MalformedClassifier:
    classes_ = ["balanced", "defensive", "moonbag", "runner"]
    def __init__(self, output):
        self.output = output
    def predict(self, matrix):
        return self.output


@pytest.mark.parametrize("output", [["wrong"], [0.9], [True], [["moonbag"]], [], ["moonbag", "runner"]])
def test_malformed_class_prediction_is_unknown_not_balanced(artifact, output):
    metadata = read_meta(artifact)
    joblib.dump(MalformedClassifier(output), artifact)
    metadata["model_sha256"] = sha256(artifact.read_bytes()).hexdigest()
    write_meta(artifact, metadata)
    assert exit_model_runtime.predict_exit_profile({"liquidity_usd": 40000})["exit_profile"] is None


def test_snapshot_is_pinned_and_detached_for_parent_entry(artifact):
    with inference_scope():
        result = exit_model_runtime.predict_exit_profile({"liquidity_usd": 40000})
        expected = deepcopy(result)
        artifact.write_bytes(b"replacement")
        result["model_selection"]["heads"].clear()
        assert exit_model_runtime.predict_exit_profile({"liquidity_usd": 40000}) == expected
    assert exit_model_runtime.predict_exit_profile({"liquidity_usd": 40000})["exit_profile"] is None


def test_unsettled_backwards_missing_identity_and_unknown_labels_are_excluded(tmp_path):
    frame = source_frame()
    frame.loc[0, "ts"] = pd.Timestamp("2099-01-01", tz="UTC")
    frame.loc[1, "ts"] = pd.Timestamp("2020-01-01", tz="UTC")
    frame.loc[2, "address"] = None
    frame.loc[3, ["max_pnl_pct_seen", "target_total_pnl_pct"]] = np.nan
    report = train_exit_classifier(frame=frame, output_dir=tmp_path)
    assert report["excluded_invalid_timing_or_identity_rows"] == 3
    assert report["unknown_or_invalid_label_rows"] == 1
    assert report["rows"] == len(frame) - 4


def test_report_bundle_contains_exit_without_policy_activation():
    from tools.train_model_reports import TRAINING_JOBS
    assert any(name == "exit" and filename == "exit_model_report.json" for name, _, filename in TRAINING_JOBS)


def test_actual_entry_diagnostic_uses_same_vector_without_changing_exit(monkeypatch):
    from test_entry_inference_coherence import load_helpers
    namespace, _ = load_helpers()
    seen = []
    monkeypatch.setattr(exit_model_runtime, "predict_exit_profile", lambda vector: seen.append(vector) or {
        "exit_profile": "moonbag", "status": "validated_historical_diagnostic",
        "activation_role": "diagnostic_only", "exit_policy_permission": False, "buy_permission": False})
    namespace.update(_runner_profile_for_subject=lambda token: "balanced", _config_hash=lambda: "config",
        build_feature_vector=lambda token, now: {"liquidity_usd": token["liquidity_usd"]},
        CFG=SimpleNamespace(ML_GATE_MODE="off"), DRY_RUN=True,
        predict_risk=lambda vector: None, predict_ev=lambda vector: None,
        entry_prediction_state=lambda: {"activation_ready": False, "metadata": {}},
        decide_ml_action=lambda **kwargs: SimpleNamespace(threshold=.5),
        research_runtime=SimpleNamespace(score_candidate=lambda *a, **k: {}))
    token = {"liquidity_usd": 40000}
    captured = datetime(2026, 7, 1, tzinfo=timezone.utc)
    namespace["_score_entry_inputs"](token, captured_at=captured)
    assert not seen and token["exit_model_diagnostic"]["status"] == "disabled"
    namespace.update(CFG=SimpleNamespace(ML_GATE_MODE="advisory"), should_buy=lambda vector: None)
    result = namespace["_score_entry_inputs"](token, captured_at=captured)
    assert seen == [result[0]] and token["exit_model_diagnostic"]["exit_profile"] == "moonbag"
    assert token["exit_profile"] == token["runner_exit_profile"] == "balanced"
    assert "exit_model_diagnostic" not in result[1]
    assert token["exit_model_diagnostic"]["input_captured_at_utc"] == captured.isoformat()


def test_diagnostic_payload_is_detached_in_decision_logs():
    from analytics.research_runtime import _common_payload
    token = {"address": "synthetic", "exit_model_diagnostic": {"exit_profile": "moonbag", "exit_policy_permission": False}}
    payload = _common_payload(token)
    token["exit_model_diagnostic"]["exit_profile"] = "defensive"
    assert payload["exit_model_diagnostic"]["exit_profile"] == "moonbag"


def test_diagnostic_is_nonpredictor_metadata_even_after_repeated_builds():
    from features.builder import build_feature_vector, ALLOWED_FEATURES, COLUMNS
    from ml.train import _META_COLS
    token = {"exit_model_diagnostic": {"exit_profile": "moonbag"}, "liquidity_usd": 40000}
    captured = datetime(2026, 7, 1, tzinfo=timezone.utc)
    first = build_feature_vector(token, now=captured)
    token["exit_model_diagnostic"] = {"exit_profile": "defensive"}
    pd.testing.assert_series_equal(first, build_feature_vector(token, now=captured))
    assert "exit_model_diagnostic" not in ALLOWED_FEATURES and "exit_model_diagnostic" not in COLUMNS
    assert "exit_model_diagnostic" in _META_COLS
