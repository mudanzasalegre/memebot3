"""Synthetic T0 encoding checks, not forward opportunity or profit evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from features.context_encoding import (CONTEXT_FEATURES, DOMAINS, FEATURE_SOURCES,
    MISSING, OTHER, SCHEMA_SHA256, VERSION, augment_context_frame,
    available_context_features, checked_context_schema, context_encoding_schema,
    encode_context_row, independent_input_count)
from ml.feature_matrix import coerce_feature_frame
from features.numeric_encoding import numeric_encoding_schema


@pytest.mark.parametrize("source,value", [(s, v) for s, values in DOMAINS.items() for v in values])
def test_fixed_categories_are_distinct_exact_onehot_without_ordinal_values(source, value):
    row = {source: value}
    encoded = encode_context_row(row)
    assert encoded[f"t0ctx_{source}__{value}"] == 1
    assert sum(encoded[name] for name, parent in FEATURE_SOURCES.items() if parent == source) == 1
    assert set(encoded.values()) <= {0, 1} and row == {source: value}


@pytest.mark.parametrize("value", [None, "", "nan", pd.NA, float("nan")])
def test_unobserved_is_not_observed_missing_social_or_unknown_risk(value):
    missing = encode_context_row({"social_status": value})
    observed = encode_context_row({"social_status": "missing"})
    assert missing[f"t0ctx_social_status__{MISSING}"] == 1
    assert observed["t0ctx_social_status__missing"] == 1
    assert missing != observed
    risk = encode_context_row({"liquidity_risk_level": "unknown"})
    assert risk["t0ctx_liquidity_risk_level__unknown"] == 1


def test_unrecognized_category_does_not_become_known_lane_or_unobserved():
    encoded = encode_context_row({"entry_lane": "future_lane_not_in_software"})
    assert encoded[f"t0ctx_entry_lane__{OTHER}"] == 1
    assert encoded[f"t0ctx_entry_lane__{MISSING}"] == 0
    assert encoded == encode_context_row({"entry_lane": "another_new_lane"})


def test_lane_aliases_use_existing_canonical_taxonomy():
    assert encode_context_row({"entry_lane": "Green Candle"}) == encode_context_row(
        {"entry_lane": "pump_early_green_candle_sniper"})


def test_matrix_derives_from_raw_t0_and_ignores_precomputed_or_post_outcome_indicators():
    features = ["price_pct_5m", "t0ctx_entry_lane__pump_early_green_candle_sniper",
                "t0ctx_entry_lane__pump_early_paper_bootstrap_micro"]
    original = pd.DataFrame({"entry_lane": ["pump_early_green_candle_sniper", "pump_early_paper_bootstrap_micro"],
        "price_pct_5m": [25000., None], features[1]: [0, 1], features[2]: [1, 0],
        "best_exit_profile": ["moonbag", "defensive"], "target_total_pnl_pct": [-100, 50000]}, index=[7, 3])
    untouched = original.copy(deep=True)
    X = coerce_feature_frame(original, features)
    assert X.index.tolist() == [7, 3] and X.columns.tolist() == features
    assert X.to_numpy().tolist() == [[25000., 1., 0.], [0., 0., 1.]]
    pd.testing.assert_frame_equal(original, untouched)
    mutable = original.assign(target_total_pnl_pct=0, best_exit_profile="changed")
    pd.testing.assert_frame_equal(X, coerce_feature_frame(mutable, features))
    assert all(dtype == np.float32 for dtype in X.dtypes)


def test_legacy_numeric_and_text_artifact_inputs_keep_their_original_interpretation():
    frame = pd.DataFrame({"price_pct_5m": [float("inf"), 0, "25000"],
        "entry_lane": ["pump_early_green_candle_sniper"] * 3})
    X = coerce_feature_frame(frame, ["entry_lane", "price_pct_5m", "missing_legacy_feature"])
    assert X.to_numpy().tolist() == [[0., 0., 0.], [0., 0., 0.], [0., 25000., 0.]]
    assert checked_context_schema({"features": list(X)}, list(X))


def test_absent_training_descriptors_do_not_invent_a_categorical_population():
    frame = pd.DataFrame({"timestamp": [1, 2], "label": [0, 1]})
    assert available_context_features(frame) == []
    pd.testing.assert_frame_equal(frame, augment_context_frame(frame, available_context_features(frame)))


def test_schema_is_fixed_ordered_checksummed_and_detached_from_rows():
    features = ["score_total", "t0ctx_exit_profile__jackpot_runner", "t0ctx_social_status__suspicious"]
    schema = context_encoding_schema(features)
    assert schema == {"version": VERSION, "schema_sha256": SCHEMA_SHA256, "encoded_features": features[1:]}
    assert checked_context_schema({"context_encoding": schema}, features)
    assert context_encoding_schema(["score_total"]) is None
    assert not set(CONTEXT_FEATURES) & {"timestamp", "address", "label", "target_total_pnl_pct", "max_pnl_pct_seen"}


def test_global_vocabulary_cannot_be_mutated_after_schema_acceptance():
    with pytest.raises(TypeError):
        DOMAINS["entry_lane"] = ("replaced",)
    with pytest.raises(TypeError):
        FEATURE_SOURCES["t0ctx_entry_lane__unobserved"] = "target_total_pnl_pct"


@pytest.mark.parametrize("mutation", ["missing", "hash", "version", "order", "extra", "wrong_source"])
def test_mixed_or_unproved_schema_is_not_accepted(mutation):
    features = ["t0ctx_entry_lane__pump_early_green_candle_sniper", "t0ctx_exit_profile__jackpot_runner"]
    schema = context_encoding_schema(features)
    if mutation == "missing":
        schema = None
    elif mutation == "hash":
        schema["schema_sha256"] = "0" * 64
    elif mutation == "version":
        schema["version"] = "future_unverified_encoding"
    elif mutation == "order":
        schema["encoded_features"].reverse()
    elif mutation == "extra":
        schema["extra"] = True
    else:
        schema["encoded_features"][0] = "t0ctx_entry_lane__future_result"
    assert not checked_context_schema({"context_encoding": schema}, features)


def test_derived_inputs_do_not_change_parquet_or_frozen_causal_receipt_schema():
    from features.builder import COLUMNS, build_feature_vector
    from features.store import _SCHEMA
    from runtime import trade_learning as learning
    now = pd.Timestamp("2026-10-01T00:00:00Z").to_pydatetime()
    vec = build_feature_vector({"address": "synthetic", "entry_lane": "pump_early_green_candle_sniper",
        "exit_profile": "jackpot_runner", "green_sniper_risk_level": "medium"}, now=now)
    proof = learning.freeze_entry_features(vec, address="synthetic", captured_at=now)
    learning.validate_entry_features(proof, address="synthetic")
    assert proof["version"] == "frozen_entry_features_v1"
    assert set(proof["vector"]) == set(COLUMNS)
    assert not set(CONTEXT_FEATURES) & set(COLUMNS)
    assert not set(CONTEXT_FEATURES) & set(_SCHEMA.names)
    X = coerce_feature_frame(pd.DataFrame([proof["vector"]]), CONTEXT_FEATURES)
    assert X.loc[0, "t0ctx_exit_profile__jackpot_runner"] == 1
    assert X.loc[0, "t0ctx_green_sniper_risk_level__medium"] == 1


def test_primary_selection_retains_categorical_signal_and_counts_sources_not_dummy_dimensions(monkeypatch):
    from ml import train as trainer
    frame = pd.DataFrame({"entry_lane": list(DOMAINS["entry_lane"]) * 2,
        "label": [0, 1] * len(DOMAINS["entry_lane"]), "target_total_pnl_pct": 5.,
        "mint": [f"Mint{i}" for i in range(2 * len(DOMAINS["entry_lane"]))]})
    selected, features, excluded = trainer._select_feature_columns(frame)
    assert len(features) > 12 and independent_input_count(features) == 1
    assert set(features) <= set(CONTEXT_FEATURES)
    assert "target_total_pnl_pct" not in features and "entry_lane" in excluded
    monkeypatch.setattr(trainer, "CFG", SimpleNamespace(ML_MIN_DATASET_ROWS=1, ML_MIN_POSITIVES=1,
        ML_MIN_UNIQUE_TOKENS=1, ML_MIN_REALIZED_RETURN_ROWS=1, ML_MIN_NON_CONSTANT_FEATURES=12,
        ML_MIN_HOLDOUT_ROWS=1, ML_MIN_HOLDOUT_POSITIVES=1))
    quality = trainer._initial_quality(selected, selected, features, {})
    assert not quality.passed and quality.non_constant_input_sources == 1
    assert quality.non_constant_numeric_features == len(features)
    assert "non_constant_input_sources<12" in quality.reasons
    final = trainer._finalize_quality(quality, selected)
    assert final.non_constant_input_sources == 1


def context_frame():
    from test_financial_model_acceptance import frame
    out = frame()
    out["price_pct_5m"] = 5.
    out["entry_lane"] = ["pump_early_paper_bootstrap_micro", "pump_early_green_candle_sniper"] * 80
    out["gate_profile"] = ["paper_bootstrap", "green_sniper"] * 80
    return out


def test_primary_fit_and_reader_can_distinguish_lanes_with_identical_numeric_observations(tmp_path, monkeypatch):
    from test_financial_model_acceptance import setup_entry
    from ml import train as trainer
    runtime, registry, artifact = setup_entry(tmp_path, monkeypatch)
    original = json.loads(artifact.meta_path.read_text())
    data = context_frame().assign(label=[0, 1] * 80)
    data["mint"] = data.address
    data, features, _ = trainer._select_feature_columns(data)
    from features.builder import ALLOWED_FEATURES
    features = [name for name in features if name in ALLOWED_FEATURES]
    model = trainer._fit_logreg_calibrated(data, features)
    from ml.entry_probability import probability_metadata
    candidate = trainer._evaluate_candidate(name="isolated_context", model_family="sklearn_logreg",
        builder=trainer._fit_logreg_calibrated, x_cols=features, use_forward=True,
        tr_df=data.iloc[:120], te_df=data.iloc[120:])
    artifact = registry.write_candidate(model=model,
        meta={**original, **probability_metadata(model, candidate.probability_evaluation),
              "features": features, "context_encoding": context_encoding_schema(features),
              "numeric_encoding": numeric_encoding_schema(features)}, model_id="categorical")
    monkeypatch.setattr(runtime, "_MODEL_PATH", artifact.model_path)
    monkeypatch.setattr(runtime, "_META_PATH", artifact.meta_path)
    low = {"entry_lane": "pump_early_paper_bootstrap_micro", "gate_profile": "paper_bootstrap", "price_pct_5m": 5}
    high = {"entry_lane": "pump_early_green_candle_sniper", "gate_profile": "green_sniper", "price_pct_5m": 5}
    assert runtime.should_buy(high) > .9 and runtime.should_buy(low) < .1
    from analytics.inference_scope import inference_scope
    loaded, _, _ = runtime._load_model()
    calls = []
    original_predict = loaded.predict_proba
    monkeypatch.setattr(loaded, "predict_proba", lambda X: calls.append(X.copy()) or original_predict(X))
    with inference_scope():
        p_low = runtime.should_buy(low)
        p_high = runtime.should_buy(high)
        assert p_high > p_low and len(calls) == 2
        assert runtime.should_buy({**high, "entry_lane": "green_candle", "irrelevant": "changed"}) == p_high
        assert len(calls) == 2
    metadata = json.loads(artifact.meta_path.read_text())
    metadata["context_encoding"]["version"] = "unsupported"
    artifact.meta_path.write_text(json.dumps(metadata))
    assert runtime.should_buy(high) is None
    with pytest.raises(RuntimeError, match="context encoding"):
        registry.promote_candidate(artifact, active_model_path=tmp_path / "active.pkl")
    assert not (tmp_path / "active.pkl").exists()


def test_actual_runner_family_oos_and_runtime_learn_context_not_post_outcome_prices(tmp_path, monkeypatch):
    from ml.family_training import train_classifier_family
    from analytics import model_runtime_common as runtime
    data = context_frame()
    report = train_classifier_family(family="runner", targets=["runner_10000"],
        feature_set_name="runner_features", frame=data,
        output_dir=tmp_path / "ml" / "models" / "runner")
    target = report["targets"]["runner_10000"]
    assert target["ranking_validation_ready"] and target["probability_validation_ready"]
    path = Path(target["model_path"])
    metadata = json.loads(path.with_suffix(".meta.json").read_text())
    assert checked_context_schema(metadata, metadata["features"])
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    low = {"entry_lane": "pump_early_paper_bootstrap_micro", "gate_profile": "paper_bootstrap", "price_pct_5m": 5}
    high = {"entry_lane": "pump_early_green_candle_sniper", "gate_profile": "green_sniper", "price_pct_5m": 5}
    assert runtime.predict_model("runner", "runner_10000", high) > runtime.predict_model("runner", "runner_10000", low)
    assert runtime.predict_ranking_score("runner", "runner_10000", high) > runtime.predict_ranking_score("runner", "runner_10000", low)


def test_actual_net_ev_family_restores_frozen_context_and_uses_same_encoded_reader(tmp_path, monkeypatch):
    from ml.family_training import train_regressor_family
    from analytics import model_runtime_common as runtime
    from net_financial_fixtures import net_frame
    original = net_frame(context_frame())
    tampered = original.copy()
    tampered["entry_lane"] = "unrecognized_post_close_lane"
    tampered["gate_profile"] = "unrecognized_post_close_gate"
    tampered["t0ctx_entry_lane__pump_early_green_candle_sniper"] = 99
    report = train_regressor_family(family="ev", targets=["ev_realized"], feature_set_name="ev_features",
        frame=tampered, output_dir=tmp_path / "ml" / "models" / "ev")
    target = report["targets"]["ev_realized"]
    assert target["regression_validation_ready"]
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    low = {"entry_lane": "pump_early_paper_bootstrap_micro", "gate_profile": "paper_bootstrap", "price_pct_5m": 5}
    high = {"entry_lane": "pump_early_green_candle_sniper", "gate_profile": "green_sniper", "price_pct_5m": 5}
    assert runtime.predict_model("ev", "ev_realized", low) == pytest.approx(-60)
    assert runtime.predict_model("ev", "ev_realized", high) == pytest.approx(200)
    metadata = json.loads(Path(target["model_path"]).with_suffix(".meta.json").read_text())
    metadata.pop("context_encoding")
    Path(target["model_path"]).with_suffix(".meta.json").write_text(json.dumps(metadata))
    assert runtime.predict_model("ev", "ev_realized", high) is None


@pytest.mark.parametrize("feature", ["label", "target_total_pnl_pct", "max_pnl_pct_seen", "t0ctx_outcome__future"])
def test_specialized_reader_rejects_outcome_or_undeclared_predictors(tmp_path, monkeypatch, feature):
    from analytics import model_runtime_common as runtime
    from ml.family_training import train_regressor_family
    from net_financial_fixtures import net_frame
    report = train_regressor_family(family="ev", targets=["ev_realized"], feature_set_name="ev_features",
        frame=net_frame(context_frame()), output_dir=tmp_path / "ml" / "models" / "ev")
    path = Path(report["targets"]["ev_realized"]["model_path"])
    metadata = json.loads(path.with_suffix(".meta.json").read_text())
    metadata["features"] = [feature]
    metadata.pop("context_encoding", None)
    path.with_suffix(".meta.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    assert runtime.predict_model("ev", "ev_realized", {feature: 999999}) is None


def test_advisory_comparison_requires_same_accepted_encoding():
    from ml.runner_advisory_learning import _evaluate
    features = ["t0ctx_entry_lane__pump_early_green_candle_sniper"]
    cohort = context_frame().assign(runner_10000=[0, 1] * 80)
    model = SimpleNamespace(rank_score=lambda X: X[features[0]].to_numpy())
    with pytest.raises(ValueError, match="context encoding"):
        _evaluate(model, features, cohort, "runner_10000", metadata={})
    result = _evaluate(model, features, cohort, "runner_10000",
        metadata={"context_encoding": context_encoding_schema(features)})
    assert result["precision_lift_at_k"] == 2.
    assert result["metric"] == "observed_peak_ranking_not_costed_profit"


def test_advisory_rollback_cannot_activate_a_different_context_schema(tmp_path):
    from hashlib import sha256
    from ml.runner_advisory_learning import rollback_runner_advisory
    directory = tmp_path / "ml" / "models" / "runner"
    path = directory / "versions" / "one" / "runner_10000.pkl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic checksum fixture; never deserialized")
    checksum = sha256(path.read_bytes()).hexdigest()
    features = ["t0ctx_entry_lane__pump_early_green_candle_sniper"]
    metadata = {"activation_role": "scanner_ranking_only", "model_sha256": checksum,
        "family": "runner", "target": "runner_10000",
        "features": features, "context_encoding": context_encoding_schema(features)}
    metadata["context_encoding"]["schema_sha256"] = "invalid"
    path.with_suffix(".meta.json").write_text(json.dumps(metadata))
    manifest = directory / "advisory_manifest.json"
    manifest.write_text(json.dumps({"role": "scanner_ranking_only", "heads": {}, "previous_heads": {
        "runner_10000": {"path": "versions/one/runner_10000.pkl", "model_sha256": checksum,
            "version": "one", "metadata_sha256": sha256(path.with_suffix(".meta.json").read_bytes()).hexdigest()}}}))
    before = manifest.read_bytes()
    assert not rollback_runner_advisory(root=tmp_path)
    assert manifest.read_bytes() == before
    metadata["context_encoding"] = context_encoding_schema(features)
    path.with_suffix(".meta.json").write_text(json.dumps(metadata))
    # The repaired fixture is a new complete approval, not metadata-only repair.
    selected = json.loads(manifest.read_text())
    selected["previous_heads"]["runner_10000"]["metadata_sha256"] = sha256(path.with_suffix(".meta.json").read_bytes()).hexdigest()
    manifest.write_text(json.dumps(selected))
    assert rollback_runner_advisory(root=tmp_path)
