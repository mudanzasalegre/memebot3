"""Typed T0 input tests with isolated synthetic models, not profit evidence."""
from __future__ import annotations

import datetime as dt
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from features.numeric_encoding import (RULES, PREFIX, VERSION, SCHEMA_SHA256,
    MISSINGNESS_FEATURES, augment_numeric_frame, available_numeric_features,
    binary_value, numeric_value, numeric_encoding_schema, checked_numeric_schema)
from features.context_encoding import independent_input_count, context_encoding_schema
from ml.feature_matrix import coerce_feature_frame


@pytest.mark.parametrize("first", ["utils.data_utils", "features.numeric_encoding", "trader.papertrading"])
def test_cold_import_orders_do_not_create_cycles_or_initialize_live_signer(tmp_path, first):
    root = Path(__file__).resolve().parents[1]
    env = {name: value for name, value in os.environ.items() if name not in {"SOL_PRIVATE_KEY", "SOL_PUBLIC_KEY"}}
    env.update(PYTHONPATH=str(root), DRY_RUN="1")
    code = (f"import importlib,sys; importlib.import_module('{first}'); "
        "from utils.numeric_types import binary_value; from features.numeric_encoding import numeric_value; "
        "assert binary_value('false') == 0; assert numeric_value(None, 'holders') is None; "
        "assert 'trader.sol_signer' not in sys.modules; print('cold_import_ok')")
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("cold_import_ok")


@pytest.mark.parametrize("source", list(RULES))
def test_every_declared_input_distinguishes_measured_zero_from_unknown(source):
    names = [source, PREFIX + source]
    X = coerce_feature_frame(pd.DataFrame({source: [0, None, float("nan"), float("inf")]}), names)
    assert X.to_numpy().tolist() == [[0., 0.], [0., 1.], [0., 1.], [0., 1.]]
    assert X.columns.tolist() == names and all(dtype == np.float32 for dtype in X.dtypes)
    assert independent_input_count(names) == 1


@pytest.mark.parametrize("value,expected", [(False, 0), (np.bool_(True), 1), ("false", 0),
    (" OFF ", 0), ("true", 1), ("yes", 1), ("0", 0), ("1", 1), (2, None),
    (-1, None), (.5, None), ("unknown", None), (pd.NA, None)])
def test_binary_contract_is_not_python_truthiness(value, expected):
    assert binary_value(value) == expected


@pytest.mark.parametrize("value", [None, pd.NA, "", "bad", float("nan"), float("inf"),
    -float("inf"), {}, [1], (1,), {1}, np.array([1]), complex(1, 0), np.complex64(1),
    dt.datetime(2026, 10, 1), pd.Timestamp("2026-10-01"), True, np.bool_(False), 10**1000])
def test_malformed_continuous_input_is_unknown_in_scalar_and_batch(value):
    source = "price_pct_5m"
    assert numeric_value(value, source) is None
    names = [source, PREFIX + source]
    frame = pd.DataFrame({source: pd.Series([value, value], dtype=object)})
    assert coerce_feature_frame(frame, names).to_numpy().tolist() == [[0., 1.], [0., 1.]]
    assert coerce_feature_frame(frame.iloc[:1], names).to_numpy().tolist() == [[0., 1.]]


@pytest.mark.parametrize("value,expected", [(0, 0), (12., 12), ("15", 15),
    (2**31-1, 2**31-1), (2**31, None), (-1, None), (1.5, None), (True, None)])
def test_counts_do_not_truncate_fractionals_or_accept_negative_overflow(value, expected):
    assert numeric_value(value, "holders") == expected


@pytest.mark.parametrize("value,expected", [(-1, -1), (0, 0), (1, 1), ("-1", -1),
    (2, None), (.3, None), (True, None)])
def test_numeric_trend_is_a_declared_signed_state(value, expected):
    assert numeric_value(value, "trend") == expected


def test_extreme_momentum_is_not_clipped_or_treated_as_missing():
    values = [-100., 500., 1000., 10000., 100000., 1000000.]
    X = coerce_feature_frame(pd.DataFrame({"price_pct_5m": values}),
        ["price_pct_5m", PREFIX + "price_pct_5m"])
    assert X.price_pct_5m.tolist() == values
    assert X[PREFIX + "price_pct_5m"].tolist() == [0.] * len(values)


def test_encoding_recomputes_forged_bits_preserves_duplicate_index_and_input():
    names = ["holders", PREFIX + "holders", "price_pct_5m", PREFIX + "price_pct_5m"]
    raw = pd.DataFrame({"holders": [0, "invalid"], "price_pct_5m": [None, 1000000.],
        PREFIX + "holders": [1, 0], PREFIX + "price_pct_5m": [0, 1], "label": [1, 0]}, index=[8, 8])
    original = raw.copy(deep=True)
    X = coerce_feature_frame(raw, names)
    assert X.to_numpy().tolist() == [[0., 0., 0., 1.], [0., 1., 1000000., 0.]]
    assert X.index.tolist() == [8, 8]
    for i in range(2):
        pd.testing.assert_frame_equal(X.iloc[i:i+1], coerce_feature_frame(raw.iloc[i:i+1], names))
    pd.testing.assert_frame_equal(raw, original)
    pd.testing.assert_frame_equal(X, coerce_feature_frame(raw.assign(label=999), names))
    absent = coerce_feature_frame(pd.DataFrame(index=[3]), names)
    assert absent.to_numpy().tolist() == [[0., 1., 0., 1.]]
    assert coerce_feature_frame(raw.iloc[:0], names).shape == (0, 4)


@pytest.mark.parametrize("unrelated", [complex(1, 2), np.complex64(1)])
def test_one_row_does_not_coerce_valid_input_dtype_from_unrelated_column(unrelated):
    names = ["holders", PREFIX + "holders", "price_pct_5m", PREFIX + "price_pct_5m"]
    raw = pd.DataFrame({"holders": [0, 12], "price_pct_5m": [1000000., 0.]})
    enriched = raw.assign(unconsumed=unrelated)
    expected = coerce_feature_frame(raw, names)
    pd.testing.assert_frame_equal(expected, coerce_feature_frame(enriched, names))
    for i in range(2):
        pd.testing.assert_frame_equal(expected.iloc[i:i+1], coerce_feature_frame(enriched.iloc[i:i+1], names))


def test_training_augmentation_does_not_invent_missingness_after_float32_count_rounding():
    names = ["holders", PREFIX + "holders"]
    raw = pd.DataFrame({"holders": [2**31-1, 2**24+1, 0]})
    augmented = augment_numeric_frame(raw, names)
    assert augmented.holders.tolist() == raw.holders.tolist()
    expected = coerce_feature_frame(raw, names)
    assert expected[PREFIX + "holders"].tolist() == [0., 0., 0.]
    pd.testing.assert_frame_equal(expected, coerce_feature_frame(augmented, names))
    pd.testing.assert_frame_equal(expected, coerce_feature_frame(augment_numeric_frame(augmented, names), names))
    for i in range(3):
        pd.testing.assert_frame_equal(expected.iloc[i:i+1], coerce_feature_frame(augmented.iloc[i:i+1], names))


def test_raw_schema_and_legacy_matrix_do_not_change():
    from features.builder import COLUMNS, ALLOWED_FEATURES, build_feature_vector
    from features.store import _SCHEMA
    from runtime.trade_learning import freeze_entry_features, validate_entry_features
    assert set(MISSINGNESS_FEATURES) <= ALLOWED_FEATURES
    assert not set(MISSINGNESS_FEATURES) & set(COLUMNS)
    assert not set(MISSINGNESS_FEATURES) & set(_SCHEMA.names)
    now = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    vector = build_feature_vector({"address": "synthetic", "price_pct_5m": 1000000.}, now=now)
    receipt = freeze_entry_features(vector, address="synthetic", captured_at=now)
    validate_entry_features(receipt, address="synthetic")
    assert set(receipt["vector"]) == set(COLUMNS)
    assert receipt["vector"]["price_pct_5m"] == 1000000.
    assert numeric_encoding_schema(["holders"]) is None
    assert checked_numeric_schema({}, ["holders"])
    legacy = coerce_feature_frame(pd.DataFrame({"holders": [None, -2, 1.5]}), ["holders"])
    assert legacy.holders.tolist() == [0., -2., 1.5]
    assert available_numeric_features(pd.DataFrame({"timestamp": [1], "label": [0]})) == []


@pytest.mark.parametrize("mutation", ["missing", "hash", "version", "order", "extra", "unknown"])
def test_unproved_numeric_schema_is_unavailable(mutation):
    names = ["holders", "price_pct_5m", PREFIX + "holders", PREFIX + "price_pct_5m"]
    schema = numeric_encoding_schema(names)
    assert schema["version"] == VERSION and schema["schema_sha256"] == SCHEMA_SHA256
    assert checked_numeric_schema({"numeric_encoding": schema}, names)
    if mutation == "missing": schema = None
    elif mutation == "hash": schema["schema_sha256"] = "0" * 64
    elif mutation == "version": schema["version"] = "unverified"
    elif mutation == "order": schema["encoded_features"].reverse()
    elif mutation == "extra": schema["extra"] = True
    else: schema["encoded_features"][0] = PREFIX + "future_outcome"
    assert not checked_numeric_schema({"numeric_encoding": schema}, names)


@pytest.mark.parametrize("names", [[PREFIX + "holders"], ["holders", PREFIX + "future_outcome"],
    ["holders", "price_pct_5m", PREFIX + "holders"]])
def test_partial_or_unknown_pairs_cannot_be_declared(names):
    with pytest.raises(ValueError, match="paired raw inputs"):
        numeric_encoding_schema(names)
    assert not checked_numeric_schema({}, names)
    with pytest.raises(TypeError): RULES["holders"] = "changed"


@pytest.mark.parametrize("impact", [None, "invalid", pd.NA, float("inf"), True, [0]])
def test_producer_missing_impact_never_becomes_observed_zero_or_raises(impact):
    from features.builder import build_feature_vector
    vector = build_feature_vector({"address": "synthetic", "price_impact_pct": impact,
        "liquidity_is_proxy": False, "liquidity_usd_is_proxy": True, "route_proxy": "false"})
    assert pd.isna(vector.impact_zero_flag)
    assert vector.liquidity_is_proxy == 0 and vector.route_proxy == 0


def test_producer_strict_boolean_count_and_unknown_trend_contracts():
    from utils.data_utils import sanitize_token_data
    from features.builder import build_feature_vector
    clean = sanitize_token_data({"address": "synthetic", "cluster_bad": "false", "mint_auth_renounced": "false",
        "holders": True, "rug_score": 1.5, "txns_last_5m": -1, "discord_members": 2**31,
        "twitter_followers": np.bool_(True), "trend": "unknown", "age_minutes": 0})
    assert clean["cluster_bad"] == clean["mint_auth_renounced"] == 0
    assert all(clean[name] is None for name in ("holders", "rug_score", "txns_last_5m", "discord_members", "twitter_followers", "trend"))
    assert clean["age_minutes"] == 0
    vector = build_feature_vector(clean)
    assert vector.cluster_bad == vector.mint_auth_renounced == 0 and pd.isna(vector.trend)
    assert build_feature_vector({"address": "synthetic", "price_impact_pct": 0}).impact_zero_flag == 1
    assert build_feature_vector({"address": "synthetic", "price_impact_pct": 1}).impact_zero_flag == 0
    assert sanitize_token_data({"address": "synthetic", "trend": 10**1000})["trend"] is None


def test_typed_invalid_raw_values_stay_null_after_actual_parquet_write(tmp_path, monkeypatch):
    from features import store
    from features.builder import build_feature_vector
    import pyarrow.parquet as pq
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    vector = build_feature_vector({"address": "synthetic", "queue_attempts": 1.5,
        "score_total": 70.5, "price_impact_pct": [0], "green_sniper_score": True,
        "price_pct_5m": 1000000., "holders": 0, "route_proxy": "false"})
    invalid = ["queue_attempts", "score_total", "price_impact_pct", "green_sniper_score"]
    assert all(pd.isna(vector[name]) for name in invalid)
    assert store.append(vector, label=None, strict=True)
    stored = pq.read_table(next(tmp_path.glob("*.parquet"))).to_pandas()
    assert all(pd.isna(stored.loc[0, name]) for name in invalid)
    names = [*invalid, "holders", "route_proxy", "price_pct_5m"]
    features = [*names, *(PREFIX + name for name in names)]
    X = coerce_feature_frame(stored, features)
    assert all(X.loc[0, PREFIX + name] == 1 for name in invalid)
    assert all(X.loc[0, PREFIX + name] == 0 for name in names if name not in invalid)
    assert X.loc[0, "price_pct_5m"] == 1000000.


def missingness_frame():
    from test_financial_model_acceptance import frame
    data = frame()
    data["price_pct_5m"] = [0., None] * 80
    return data


def test_primary_selection_keeps_constant_raw_partner_without_quality_inflation(monkeypatch):
    from ml import train as trainer
    frame = pd.DataFrame({"price_pct_5m": [0., None] * 10, "label": [0, 1] * 10,
        "mint": [f"Mint{i}" for i in range(20)]})
    selected, features, excluded = trainer._select_feature_columns(frame)
    assert features == ["price_pct_5m", PREFIX + "price_pct_5m"]
    assert not set(features) & set(excluded) and independent_input_count(features) == 1
    assert checked_numeric_schema({"numeric_encoding": numeric_encoding_schema(features)}, features)
    monkeypatch.setattr(trainer, "CFG", SimpleNamespace(ML_MIN_DATASET_ROWS=1, ML_MIN_POSITIVES=1,
        ML_MIN_UNIQUE_TOKENS=1, ML_MIN_REALIZED_RETURN_ROWS=0, ML_MIN_NON_CONSTANT_FEATURES=2,
        ML_MIN_HOLDOUT_ROWS=1, ML_MIN_HOLDOUT_POSITIVES=1))
    quality = trainer._initial_quality(selected, selected, features, {})
    assert not quality.passed and quality.non_constant_input_sources == 1
    assert quality.numeric_feature_candidates == 2 and quality.non_constant_numeric_features == 1
    assert "non_constant_input_sources<2" in quality.reasons


def test_primary_actual_calibration_reader_and_memoization_distinguish_zero_from_absence(tmp_path, monkeypatch):
    from test_financial_model_acceptance import setup_entry
    from ml import train as trainer
    from ml.entry_probability import probability_metadata
    runtime, registry, artifact = setup_entry(tmp_path, monkeypatch)
    original = json.loads(artifact.meta_path.read_text())
    data = missingness_frame().assign(label=[0, 1] * 80)
    data["mint"] = data.address
    data, features, _ = trainer._select_feature_columns(data)
    from features.builder import ALLOWED_FEATURES
    features = [name for name in features if name in ALLOWED_FEATURES]
    model = trainer._fit_logreg_calibrated(data, features)
    candidate = trainer._evaluate_candidate(name="isolated_missingness", model_family="sklearn_logreg",
        builder=trainer._fit_logreg_calibrated, x_cols=features, use_forward=True,
        tr_df=data.iloc[:120], te_df=data.iloc[120:])
    artifact = registry.write_candidate(model=model, meta={**original,
        **probability_metadata(model, candidate.probability_evaluation), "features": features,
        "context_encoding": context_encoding_schema(features), "numeric_encoding": numeric_encoding_schema(features)},
        model_id="missingness")
    monkeypatch.setattr(runtime, "_MODEL_PATH", artifact.model_path)
    monkeypatch.setattr(runtime, "_META_PATH", artifact.meta_path)
    assert runtime.should_buy({"price_pct_5m": 0}) < .1 and runtime.should_buy({}) > .9
    loaded, _, _ = runtime._load_model()
    original_predict, calls = loaded.predict_proba, []
    monkeypatch.setattr(loaded, "predict_proba", lambda X: calls.append(X.copy()) or original_predict(X))
    from analytics.inference_scope import inference_scope
    with inference_scope():
        low, high = runtime.should_buy({"price_pct_5m": 0}), runtime.should_buy({})
        assert high > low and len(calls) == 2
        assert runtime.should_buy({"price_pct_5m": "invalid"}) == high and len(calls) == 2
    metadata = json.loads(artifact.meta_path.read_text())
    metadata.pop("numeric_encoding")
    artifact.meta_path.write_text(json.dumps(metadata))
    monkeypatch.setattr(runtime.joblib, "load", lambda *args, **kwargs: pytest.fail("unverified model deserialized"))
    assert runtime.should_buy({}) is None
    with pytest.raises(RuntimeError, match="encoding"):
        registry.promote_candidate(artifact, active_model_path=tmp_path / "active.pkl")
    assert not (tmp_path / "active.pkl").exists()


@pytest.mark.parametrize("family,target,set_name", [("runner", "runner_10000", "runner_features"),
    ("risk", "severe_loss_30", "risk_features"), ("ev", "ev_realized", "ev_features")])
def test_actual_family_oos_and_reader_share_typed_missingness(tmp_path, monkeypatch, family, target, set_name):
    from ml.family_training import train_classifier_family, train_regressor_family
    from analytics import model_runtime_common as runtime
    from net_financial_fixtures import net_frame
    data = missingness_frame()
    if family != "runner": data = net_frame(data)
    training = train_regressor_family if family == "ev" else train_classifier_family
    report = training(family=family, targets=[target], feature_set_name=set_name, frame=data,
        output_dir=tmp_path / "ml" / "models" / family)
    result = report["targets"][target]
    assert result["regression_validation_ready" if family == "ev" else "probability_validation_ready"]
    path = Path(result["model_path"])
    metadata = json.loads(path.with_suffix(".meta.json").read_text())
    assert checked_numeric_schema(metadata, metadata["features"])
    assert PREFIX + "price_pct_5m" in metadata["features"]
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    low = runtime.predict_model(family, target, {"price_pct_5m": 0})
    high = runtime.predict_model(family, target, {})
    assert low is not None and high is not None and (low > high if family == "risk" else high > low)
    metadata["numeric_encoding"]["version"] = "unsupported"
    path.with_suffix(".meta.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(runtime.joblib, "load", lambda *args, **kwargs: pytest.fail("unverified family deserialized"))
    assert runtime.predict_model(family, target, {}) is None


def test_advisory_comparison_and_rollback_require_same_numeric_contract(tmp_path):
    from ml.runner_advisory_learning import _evaluate, rollback_runner_advisory
    names = ["price_pct_5m", PREFIX + "price_pct_5m"]
    data = missingness_frame().assign(runner_10000=[0, 1] * 80)
    model = SimpleNamespace(rank_score=lambda X: X[names[1]].to_numpy())
    with pytest.raises(ValueError, match="encoding"):
        _evaluate(model, names, data, "runner_10000", metadata={})
    metadata = {"numeric_encoding": numeric_encoding_schema(names), "features": names}
    result = _evaluate(model, names, data, "runner_10000", metadata=metadata)
    assert result["precision_lift_at_k"] == 2 and result["metric"] == "observed_peak_ranking_not_costed_profit"
    directory = tmp_path / "ml" / "models" / "runner"
    path = directory / "versions" / "one" / "runner_10000.pkl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic checksum fixture; never deserialized")
    checksum = sha256(path.read_bytes()).hexdigest()
    metadata.update(activation_role="scanner_ranking_only", model_sha256=checksum,
                    family="runner", target="runner_10000")
    metadata["numeric_encoding"]["schema_sha256"] = "invalid"
    path.with_suffix(".meta.json").write_text(json.dumps(metadata))
    manifest = directory / "advisory_manifest.json"
    manifest.write_text(json.dumps({"role": "scanner_ranking_only", "heads": {}, "previous_heads": {
        "runner_10000": {"path": "versions/one/runner_10000.pkl", "model_sha256": checksum,
            "version": "one", "metadata_sha256": sha256(path.with_suffix(".meta.json").read_bytes()).hexdigest()}}}))
    before = manifest.read_bytes()
    assert not rollback_runner_advisory(root=tmp_path) and manifest.read_bytes() == before
    metadata["numeric_encoding"] = numeric_encoding_schema(names)
    path.with_suffix(".meta.json").write_text(json.dumps(metadata))
    selected = json.loads(manifest.read_text())
    selected["previous_heads"]["runner_10000"]["metadata_sha256"] = sha256(path.with_suffix(".meta.json").read_bytes()).hexdigest()
    manifest.write_text(json.dumps(selected))
    assert rollback_runner_advisory(root=tmp_path)


@pytest.mark.parametrize("problem", ["numeric", "context", "future"])
def test_primary_champion_rejects_unproved_inputs_before_deserialization(tmp_path, monkeypatch, problem):
    from primary_champion_fixtures import champion_artifact
    from ml import primary_champion
    registry, artifact, _, cohort, alias = champion_artifact(tmp_path, monkeypatch)
    metadata = json.loads(artifact.meta_path.read_text())
    if problem == "numeric": metadata["numeric_encoding"] = {"version": "unsupported"}
    elif problem == "context": metadata["context_encoding"] = {"version": "unsupported"}
    else: metadata["features"] = ["target_total_pnl_pct"]
    artifact.meta_path.write_text(json.dumps(metadata))
    monkeypatch.setattr(primary_champion.joblib, "load", lambda *args, **kwargs: pytest.fail("unproved candidate deserialized"))
    with pytest.raises(ValueError, match="input encoding"):
        primary_champion.authorize_candidate(artifact, cohort, incumbent={"epoch": "unused"})
    assert not alias.exists() and not registry.REGISTRY_PATH.exists()


def test_actual_paired_primary_model_survives_same_cohort_approval_and_atomic_bundle(tmp_path, monkeypatch):
    from primary_champion_fixtures import champion_artifact, population
    from ml import train as trainer
    from ml.entry_probability import probability_metadata
    from ml.financial_targets import checked_financial_frame
    from ml.primary_champion import authorize_candidate, current_incumbent
    from ml.primary_activation import read_bundle
    from test_primary_champion_lifecycle import bind_runtime
    registry, initial, _, cohort, alias = champion_artifact(tmp_path, monkeypatch)
    data, _ = checked_financial_frame(population())
    names = ["price_pct_5m", PREFIX + "price_pct_5m"]
    candidate = trainer._evaluate_candidate(name="paired", model_family="isolated_logreg",
        builder=trainer._fit_logreg_calibrated, x_cols=names, use_forward=True,
        tr_df=data.iloc[:180], te_df=data.iloc[180:])
    model = trainer._fit_logreg_calibrated(data, names)
    metadata = json.loads(initial.meta_path.read_text())
    metadata.update(probability_metadata(model, candidate.probability_evaluation))
    metadata.update(features=names, numeric_encoding=numeric_encoding_schema(names))
    artifact = registry.write_candidate(model=model, meta=metadata, model_id="paired")
    incumbent = current_incumbent(registry_path=registry.REGISTRY_PATH, models_dir=registry.MODELS_DIR, model_alias=alias)
    approval = authorize_candidate(artifact, cohort, incumbent=incumbent)
    assert approval["accepted"]
    selection = registry.promote_candidate(artifact, active_model_path=alias, approval=approval)
    _, accepted, _, _ = read_bundle(selection["primary_activation"]["active"], registry.REGISTRY_PATH, registry.MODELS_DIR)
    assert accepted["features"] == names and checked_numeric_schema(accepted, names)
    runtime = bind_runtime(tmp_path, monkeypatch, registry, alias)
    assert runtime.should_buy({"price_pct_5m": 80}) > .9
    assert runtime.should_buy({"price_pct_5m": 5}) < .1
