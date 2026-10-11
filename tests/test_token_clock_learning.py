from __future__ import annotations

from copy import deepcopy
import datetime as dt
from hashlib import sha256
import json
from types import SimpleNamespace

import pandas as pd
import pytest

STAMP = dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc)
PROOF = "t0_token_clock_proof"


def _vector(index=0, *, unknown=False, zero=False):
    from features.builder import build_feature_vector
    token = {"address": f"clock-mint-{index}", "liquidity_usd": 20000,
             "price_pct_5m": 80 if index % 2 else -5,
             "txns_last_5m": 200, "market_cap_usd": 50000}
    if not unknown:
        token.update(created_at=STAMP - dt.timedelta(minutes=0 if zero else 100),
                     first_seen_at=STAMP - dt.timedelta(minutes=0 if zero else 3))
    return build_feature_vector(token, now=STAMP)


def _rows(n=40):
    from features.auxiliary_semantics import input_frame
    return pd.concat([input_frame(_vector(i)) for i in range(n)], ignore_index=True)


@pytest.mark.parametrize("name", ["age_minutes", "queue_age_minutes"])
def test_legacy_training_does_not_relabel_unproved_ages(name):
    from features.auxiliary_semantics import prepare_training_frame
    source = pd.DataFrame({"address": [f"mint{i}" for i in range(40)],
                           name: [3.] * 40, "liquidity_usd": [20000.] * 40})
    original = source.copy(deep=True)
    prepared, report = prepare_training_frame(source, min_current_rows=40)
    assert name not in prepared
    assert prepared.liquidity_usd.to_list() == original.liquidity_usd.to_list()
    pd.testing.assert_frame_equal(source, original)
    assert report["token_clock_filtering"]["mode"] == "unchanged_nonclock_inputs_only"


@pytest.mark.parametrize("unknown,zero", [(False, False), (True, False), (False, True)])
def test_native_builder_captures_clock_absence_zero_and_original_input(unknown, zero):
    vector = _vector(unknown=unknown, zero=zero)
    assert PROOF in vector.attrs
    from features.token_clock_semantics import validate_clock_proof
    proof = json.loads(vector.attrs[PROOF])
    validate_clock_proof(proof, vector.to_dict())
    assert proof["captured_at"] == STAMP.isoformat()
    expected = None if unknown else 0. if zero else 100.
    assert proof["ages"]["age_minutes"] == expected
    assert "pairCreatedAt" not in proof["inputs"]


def test_current_training_age_population_requires_distinct_original_tokens():
    from features.auxiliary_semantics import prepare_training_frame
    source = _rows()
    current, report = prepare_training_frame(source, min_current_rows=40)
    assert "age_minutes" in current and "queue_age_minutes" in current
    assert report["token_clock_filtering"]["current_rows"] == 40
    repeated = pd.concat([source.iloc[:1]] * 40, ignore_index=True)
    prepared, report = prepare_training_frame(repeated, min_current_rows=40)
    assert "age_minutes" not in prepared
    assert report["token_clock_filtering"]["unique_tokens"] == 1


@pytest.mark.parametrize("field,value", [("age_minutes", 101.), ("queue_age_minutes", 4.),
                                         ("address", "other"), ("timestamp", STAMP + dt.timedelta(seconds=1))])
def test_receipt_cannot_be_moved_to_another_age_identity_or_t0(field, value):
    from features.token_clock_semantics import checked_row_clock
    row = _rows(1).iloc[0].to_dict()
    assert checked_row_clock(row) is not None
    row[field] = value
    assert checked_row_clock(row) is None


def test_fixed_parquet_projection_is_accepted_but_not_generic_epsilon():
    import numpy as np
    from features.auxiliary_semantics import input_frame
    from features.token_clock_semantics import checked_row_clock
    from features.builder import build_feature_vector
    vector = build_feature_vector({"address": "fractional", "created_at": STAMP - dt.timedelta(seconds=1)}, now=STAMP)
    row = input_frame(vector).iloc[0].to_dict()
    row["age_minutes"] = float(np.float32(row["age_minutes"]))
    assert checked_row_clock(row) is not None
    row["age_minutes"] += .00001
    assert checked_row_clock(row) is None


def test_native_freeze_and_input_transport_retain_original_clock_receipt():
    from runtime.trade_learning import freeze_entry_features, validate_entry_features
    from features.auxiliary_semantics import input_frame
    from features.token_clock_semantics import checked_row_clock
    vector = _vector()
    proof = freeze_entry_features(vector, address=vector.address, captured_at=STAMP + dt.timedelta(seconds=1))
    validate_entry_features(proof, address=vector.address)
    assert proof["token_clock"] == json.loads(vector.attrs[PROOF])
    stored = input_frame(vector).iloc[0].to_dict()
    stored.pop(PROOF)
    stored["t0_auxiliary_semantics_proof"] = json.dumps(proof)
    assert checked_row_clock(stored) == proof["token_clock"]


def test_store_keeps_clock_receipt_outside_predictors(tmp_path, monkeypatch):
    from features import store
    from features.builder import COLUMNS
    from features.token_clock_semantics import checked_row_clock
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    vector = _vector()
    assert store.append(vector, 1, strict=True)
    result = pd.read_parquet(next(tmp_path.glob("*.parquet")))
    assert PROOF in result and PROOF not in COLUMNS
    assert checked_row_clock(result.iloc[0].to_dict()) is not None


@pytest.mark.parametrize("encoded", [False, True])
def test_age_schema_alone_does_not_certify_original_training_population(encoded):
    from features.numeric_encoding import PREFIX, numeric_encoding_schema, checked_numeric_schema
    names = ["age_minutes"] + ([PREFIX + "age_minutes"] if encoded else [])
    metadata = {"features": names, "numeric_encoding": numeric_encoding_schema(names)}
    assert not checked_numeric_schema(metadata, names)


def test_original_age_population_metadata_and_inference_are_both_checked():
    from features.token_clock_semantics import population_proof
    from features.numeric_encoding import numeric_encoding_schema, checked_numeric_schema
    from features.auxiliary_semantics import checked_model_frame
    source = _rows()
    names = ["age_minutes", "queue_age_minutes"]
    metadata = {"features": names, "rows": len(source), "numeric_encoding": numeric_encoding_schema(names),
                "token_clock_training": population_proof(source)}
    assert checked_numeric_schema(metadata, names)
    checked_model_frame(source.iloc[:1], names)
    with pytest.raises(ValueError, match="clock"):
        checked_model_frame(source.iloc[:1].drop(columns=[PROOF]), names)


@pytest.mark.parametrize("encoded", [False, True])
def test_native_batch_ranking_keeps_series_clock_receipt_and_isolates_bad_peer(tmp_path, monkeypatch, encoded):
    import analytics.model_runtime_common as runtime
    from features.token_clock_semantics import population_proof
    from features.numeric_encoding import PREFIX, numeric_encoding_schema
    from test_multi_head_inference import write_head, publish
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    names = ["age_minutes"] + ([PREFIX + "age_minutes"] if encoded else [])
    entry = write_head(tmp_path, "runner_100", .8, version="current", metadata_changes={
        "features":names, "numeric_encoding":numeric_encoding_schema(names),
        "token_clock_training":population_proof(_rows())})
    publish(tmp_path, {"runner_100":entry})
    valid, invalid, unknown = _vector(), _vector(1), _vector(2, unknown=True)
    assert "t0_auxiliary_semantics_proof" not in valid.attrs  # Only the actual clock transport can supply it.
    invalid.attrs[PROOF] = "bad-json"
    expected = [runtime.predict_ranking_score("runner", "runner_100", vector) for vector in (valid, invalid, unknown)]
    assert expected == [80., None, 80.]
    assert runtime.predict_ranking_scores("runner", "runner_100", [valid, invalid, unknown]) == expected


def test_native_model_reader_rejects_unproved_age_before_deserialization(tmp_path, monkeypatch):
    import analytics.model_runtime_common as runtime
    from features.numeric_encoding import numeric_encoding_schema
    path = tmp_path / "runner_100.pkl"
    path.write_bytes(b"isolated-clock-model")
    path.with_suffix(".meta.json").write_text(json.dumps({"features": ["age_minutes"],
        "model_sha256": sha256(path.read_bytes()).hexdigest(),
        "numeric_encoding": numeric_encoding_schema(["age_minutes"])}))
    called = []
    monkeypatch.setattr(runtime.joblib, "load", lambda *a, **k: called.append(True))
    runtime.invalidate_model_cache(path)
    assert runtime._load_unscoped(path, require_temporal_validation=False)[0] is None
    assert called == []


@pytest.mark.parametrize("invalid", [True, False, [], {}, "bad-json", 3, float("inf")])
def test_malformed_declared_clock_receipt_is_not_replaced_by_legacy_fallback(invalid):
    from features.token_clock_semantics import checked_row_clock
    row = _rows(1).iloc[0].to_dict()
    row[PROOF] = invalid
    assert checked_row_clock(row) is None


@pytest.mark.parametrize("field", ["captured_at", "address", "version", "basis", "payload_sha256"])
def test_changed_original_clock_receipt_is_not_accepted(field):
    from features.token_clock_semantics import checked_row_clock
    row = _rows(1).iloc[0].to_dict()
    proof = json.loads(row[PROOF])
    proof[field] = "changed"
    row[PROOF] = json.dumps(proof)
    assert checked_row_clock(row) is None


@pytest.mark.parametrize("field,value", [("age_minutes", True), ("age_minutes", -1.),
                                        ("queue_age_minutes", 2.), ("queue_age_minutes", None)])
def test_rehashed_receipt_must_reconstruct_its_original_ages(field, value):
    from features.token_clock_semantics import checked_row_clock
    row = _rows(1).iloc[0].to_dict()
    proof = json.loads(row[PROOF])
    proof["ages"][field] = value
    proof["payload_sha256"] = sha256(json.dumps({k: v for k, v in proof.items() if k != "payload_sha256"},
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    row[PROOF] = json.dumps(proof)
    assert checked_row_clock(row) is None


def test_standalone_receipt_cannot_upgrade_original_legacy_entry():
    from runtime.trade_learning import freeze_entry_features
    from features.token_clock_semantics import checked_row_clock
    vector = _vector()
    row = _rows(1).iloc[0].to_dict()
    # Preserve the original older receipt, not a newly rehashed clock extension.
    vector.attrs.clear()
    entry = freeze_entry_features(vector, address=vector.address, captured_at=STAMP)
    assert "token_clock" not in entry
    row["t0_auxiliary_semantics_proof"] = json.dumps(entry)
    assert checked_row_clock(row) is None


def test_tiny_raw_age_change_is_not_the_exact_parquet_projection():
    from features.token_clock_semantics import checked_row_clock
    row = _rows(1).iloc[0].to_dict()
    row["age_minutes"] += 1e-10
    assert checked_row_clock(row) is None


@pytest.mark.parametrize("column", ["t0_auxiliary_semantics_proof", "outcome_execution_proof"])
@pytest.mark.parametrize("invalid", [True, [], 3, "", "bad-json", "null", "[]"])
def test_declared_broken_original_entry_or_close_cannot_be_ignored(column, invalid):
    from features.token_clock_semantics import checked_row_clock
    row = _rows(1).iloc[0].to_dict()
    row[column] = invalid
    assert checked_row_clock(row) is None


def test_standalone_current_clock_cannot_contradict_original_entry_inputs():
    from features.builder import build_feature_vector
    from runtime.trade_learning import freeze_entry_features
    from features.token_clock_semantics import checked_row_clock
    row = _rows(1).iloc[0].to_dict()
    # Equal ages from a different original source alias are not the same receipt.
    other = build_feature_vector({"address": "clock-mint-0", "createdAt": STAMP - dt.timedelta(minutes=100),
                                  "first_seen_at": STAMP - dt.timedelta(minutes=3)}, now=STAMP)
    entry = freeze_entry_features(other, address=other.address, captured_at=STAMP)
    row["t0_auxiliary_semantics_proof"] = json.dumps(entry)
    assert checked_row_clock(row) is None


@pytest.mark.parametrize("legacy", [False, True])
def test_native_financial_close_uses_its_original_clock_not_later_marker(legacy):
    from features.builder import build_feature_vector
    from features.auxiliary_semantics import input_frame
    from features.token_clock_semantics import checked_row_clock
    from runtime.trade_learning import freeze_entry_features
    from net_financial_fixtures import net_frame, mint_for
    from ml.financial_targets import checked_net_return
    vector = build_feature_vector({"address": mint_for("clock-financial"),
        "created_at": STAMP - dt.timedelta(minutes=100), "first_seen_at": STAMP - dt.timedelta(minutes=3)}, now=STAMP)
    frame = input_frame(vector)
    if legacy:
        vector.attrs.clear()
    entry = freeze_entry_features(vector, address=vector.address, captured_at=STAMP)
    frame["t0_auxiliary_semantics_proof"] = json.dumps(entry)
    frame["target_total_pnl_pct"] = -3.
    closed = net_frame(frame).iloc[0].to_dict()
    assert checked_net_return(closed) == pytest.approx(-3.)
    assert (checked_row_clock(closed) is None) is legacy
    if not legacy:
        closed.pop(PROOF)
        assert checked_row_clock(closed) == entry["token_clock"]


@pytest.mark.parametrize("rows,tokens", [(29, 29), (40, 1), (40, 40)])
def test_target_cannot_borrow_parent_clock_population_support(rows, tokens):
    from ml.family_training import _supported_target_features
    source = _rows(40)
    target = (source.iloc[:rows].copy() if tokens > 1 else pd.concat([source.iloc[:1]] * rows, ignore_index=True))
    names = ["liquidity_usd", "age_minutes", "t0num_missing__age_minutes"]
    supported = _supported_target_features(names, target)
    assert "liquidity_usd" in supported
    assert ("age_minutes" in supported) is (rows >= 30 and tokens >= 30)
    assert ("t0num_missing__age_minutes" in supported) is (rows >= 30 and tokens >= 30)


@pytest.mark.parametrize("field", ["rows", "current_rows", "unique_tokens", "population_sha256", "version", "mode"])
def test_changed_clock_training_population_is_not_supported(field):
    from features.token_clock_semantics import population_proof
    from features.numeric_encoding import numeric_encoding_schema, checked_numeric_schema
    source = _rows()
    metadata = {"numeric_encoding": numeric_encoding_schema(["age_minutes"]),
                "token_clock_training": population_proof(source)}
    assert checked_numeric_schema(metadata, ["age_minutes"])
    metadata["token_clock_training"][field] = True if field in {"rows", "current_rows", "unique_tokens"} else "changed"
    assert not checked_numeric_schema(metadata, ["age_minutes"])


@pytest.mark.parametrize("alias", ["age_min", "token_age_min", "minutes_since_first_seen", "createdAt",
                                   "first_seen_epoch_s", "age_at_seen", "shadow_age_min"])
def test_legacy_clock_alias_cannot_escape_through_generic_primary_predictors(alias):
    from features.auxiliary_semantics import prepare_training_frame
    from features.numeric_encoding import checked_numeric_schema
    frame = _rows()
    frame[alias] = 3.
    prepared, report = prepare_training_frame(frame, min_current_rows=40)
    assert alias not in prepared
    assert alias in report["token_clock_filtering"]["excluded_clock_aliases"]
    assert not checked_numeric_schema({}, [alias])


@pytest.mark.parametrize("field,value", [("created_at", None), ("created_at", True), ("created_at", float("nan")),
                                        ("created_at", STAMP + dt.timedelta(minutes=1)),
                                        ("first_seen_at", STAMP + dt.timedelta(minutes=1))])
def test_unknown_invalid_or_future_original_clocks_remain_unknown(field, value):
    from features.builder import build_feature_vector
    from features.token_clock_semantics import checked_row_clock
    from features.auxiliary_semantics import input_frame
    token = {"address": "original-unknown", field: value}
    vector = build_feature_vector(token, now=STAMP)
    row = input_frame(vector).iloc[0].to_dict()
    proof = checked_row_clock(row)
    assert proof is not None
    assert proof["ages"]["age_minutes"] is None and proof["ages"]["queue_age_minutes"] is None


@pytest.mark.parametrize("obsolete", ["numeric", "clock_population"])
def test_native_obsolete_age_incumbent_gets_validated_successor_not_daemon_failure(tmp_path, monkeypatch, obsolete):
    from ml import runner_advisory_learning as learning
    monkeypatch.setattr(learning, "CFG", SimpleNamespace(ML_RUNNER_ADVISORY_ENABLED=True,
        ML_RUNNER_ADVISORY_MIN_ROWS=40, ML_RUNNER_ADVISORY_MIN_LIFT_DELTA=.05))
    # Original native time-disjoint training; only isolated temporary artifacts.
    frame = _rows(160)
    times = pd.date_range("2026-09-01", periods=len(frame), freq="10min", tz="UTC")
    # Capture new original vectors at each actual T0 rather than move receipts.
    from features.builder import build_feature_vector
    from features.auxiliary_semantics import input_frame
    frame = pd.concat([input_frame(build_feature_vector({"address": f"native-{i}",
        "created_at": times[i].to_pydatetime() - dt.timedelta(minutes=100),
        "first_seen_at": times[i].to_pydatetime() - dt.timedelta(minutes=3),
        "price_pct_5m": 80 if i % 2 else -5, "liquidity_usd": 20000,
        "txns_last_5m": 200, "market_cap_usd": 50000}, now=times[i].to_pydatetime()))
        for i in range(len(frame))], ignore_index=True)
    frame["ts"] = times + pd.Timedelta(minutes=2)
    frame["max_pnl_pct_seen"] = [1600 if i % 2 else 3 for i in range(len(frame))]
    frame["target_total_pnl_pct"] = [100 if i % 2 else -10 for i in range(len(frame))]
    first = learning.train_runner_advisory(root=tmp_path, frame=frame)
    assert first["updated"]
    manifest_path = tmp_path / "ml/models/runner/advisory_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    old_entry = manifest["heads"]["runner_100"]
    old_path = manifest_path.parent / old_entry["path"]
    metadata_path = old_path.with_suffix(".meta.json")
    metadata = json.loads(metadata_path.read_text())
    assert "age_minutes" in metadata["features"]
    if obsolete == "numeric":
        metadata["numeric_encoding"].pop("token_clock_semantics")
    else:
        metadata.pop("token_clock_training", None)
    metadata_path.write_text(json.dumps(metadata))
    old_entry["metadata_sha256"] = sha256(metadata_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    original_model, original_metadata = old_path.read_bytes(), metadata_path.read_bytes()
    second = learning.train_runner_advisory(root=tmp_path, frame=frame, force=True)
    assert second["status"] == "completed" and second["updated"]
    assert second["decisions"]["runner_100"]["obsolete_incumbent_input_generation"]
    assert old_path.read_bytes() == original_model and metadata_path.read_bytes() == original_metadata
    new_manifest = json.loads(manifest_path.read_text())
    assert new_manifest["previous_heads"]["runner_100"] == old_entry
    assert not learning.rollback_runner_advisory(root=tmp_path)
