"""Original current-generation cap learning, never retrospective profit proof."""
from copy import deepcopy
import datetime as dt
from hashlib import sha256
import json

import numpy as np
import pandas as pd
import pytest

from features.valuation_semantics import (PROOF_COLUMN, VERSION, checked_row,
    checked_population, population_proof, prepare_training_frame, validate_proof)

STAMP = dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc)


def vector(i=0, cap=12000.1234, **extra):
    from features.builder import build_feature_vector
    from utils.market_observation import stamp_market_observation
    from net_financial_fixtures import mint_for
    token = stamp_market_observation({"address": mint_for(f"valuation-{i}"), "market_cap_usd": cap,
        "fdv_usd": 99999, "liquidity_usd": 20000, "price_pct_5m": 25000,
        **extra}, "dexscreener", received_at=STAMP.timestamp() - 1)
    return build_feature_vector(token, now=STAMP)


def rows(n=40):
    from features.auxiliary_semantics import input_frame
    return pd.concat([input_frame(vector(i)) for i in range(n)], ignore_index=True)


def rehash(proof):
    proof["payload_sha256"] = sha256(json.dumps({k:v for k,v in proof.items() if k != "payload_sha256"},
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return proof


@pytest.mark.parametrize("cap", [None, 0., 12000., 123456789.])
def test_native_builder_binds_absence_zero_or_reported_cap_not_fdv(cap):
    from features.auxiliary_semantics import input_frame
    actual = vector(cap=cap, mcap_bucket="forged_bucket")
    assert PROOF_COLUMN in actual.attrs and actual.price_pct_5m == 25000
    proof = checked_row(input_frame(actual).iloc[0].to_dict())
    assert proof is not None and proof["values"]["market_cap_usd"] == cap
    assert actual.mcap_bucket == proof["values"]["mcap_bucket"]


@pytest.mark.parametrize("name", ["market_cap_usd", "mcap_bucket", "mcap_bucket_code"])
def test_legacy_cap_predictors_are_excluded_without_rewriting_history(name):
    source = pd.DataFrame({"address": [f"v-{i}" for i in range(40)], name:[12000]*40,
                           "liquidity_usd":[20000]*40, "price_pct_5m":[25000]*40})
    original = source.copy(deep=True)
    out, report = prepare_training_frame(source, min_current_rows=40)
    assert name not in out and out.liquidity_usd.to_list() == original.liquidity_usd.to_list()
    assert out.price_pct_5m.to_list() == original.price_pct_5m.to_list()
    assert report["mode"] == "unchanged_nonvaluation_inputs_only"
    pd.testing.assert_frame_equal(source, original)


def test_current_training_requires_distinct_original_tokens_not_repeated_rows():
    source = rows()
    out, report = prepare_training_frame(source, min_current_rows=40)
    assert "market_cap_usd" in out and report["current_rows"] == report["unique_tokens"] == 40
    repeated = pd.concat([source.iloc[:1]]*40, ignore_index=True)
    out, report = prepare_training_frame(repeated, min_current_rows=40)
    assert "market_cap_usd" not in out and report["unique_tokens"] == 1


@pytest.mark.parametrize("field,value", [("market_cap_usd", 99999.), ("mcap_bucket", ">=200k"),
    ("mcap_bucket_code", 5), ("address", "other"), ("timestamp", STAMP+dt.timedelta(seconds=1))])
def test_receipt_cannot_move_to_another_cap_bucket_identity_or_t0(field, value):
    row = rows(1).iloc[0].to_dict()
    assert checked_row(row) is not None
    row[field] = value
    assert checked_row(row) is None


@pytest.mark.parametrize("change", ["legacy_marker", "fdv_meaning", "future", "stale", "boolean_receipt", "missing_source"])
def test_rehashed_but_incompatible_receipt_is_not_generation_proof(change):
    row = rows(1).iloc[0].to_dict()
    proof = json.loads(row[PROOF_COLUMN])
    record = proof["market_observation"]["fields"]["market_cap_usd"]
    if change == "legacy_marker": proof["market_observation"].pop("valuation_semantics")
    elif change == "fdv_meaning": record["meaning"] = "FDV"
    elif change == "future": record["received_at"] = STAMP.timestamp()+.1
    elif change == "stale": record["received_at"] = STAMP.timestamp()-31
    elif change == "boolean_receipt": record["received_at"] = True
    else: record["source"] = ""
    row[PROOF_COLUMN] = json.dumps(rehash(proof))
    assert checked_row(row) is None


def test_only_exact_fixed_float32_projection_is_accepted():
    row = rows(1).iloc[0].to_dict()
    row["market_cap_usd"] = float(np.float32(row["market_cap_usd"]))
    assert checked_row(row) is not None
    row["market_cap_usd"] += .001
    assert checked_row(row) is None


def test_frozen_entry_keeps_proof_and_legacy_entry_cannot_be_upgraded_by_new_column():
    from runtime.trade_learning import freeze_entry_features, validate_entry_features
    from features.auxiliary_semantics import input_frame
    current = vector()
    entry = freeze_entry_features(current, address=current.address, captured_at=STAMP+dt.timedelta(seconds=1))
    assert entry["valuation"] == json.loads(current.attrs[PROOF_COLUMN])
    validate_entry_features(entry, address=current.address)
    row = input_frame(current).iloc[0].to_dict()
    row.pop(PROOF_COLUMN)
    row["t0_auxiliary_semantics_proof"] = json.dumps(entry)
    assert checked_row(row) == entry["valuation"]
    legacy = deepcopy(entry)
    legacy.pop("valuation")
    rehash(legacy)
    row["t0_auxiliary_semantics_proof"] = json.dumps(legacy)
    row[PROOF_COLUMN] = current.attrs[PROOF_COLUMN]
    assert checked_row(row) is None


def test_native_parquet_retains_original_proof_but_not_new_predictor(tmp_path, monkeypatch):
    from features import store
    from features.builder import COLUMNS
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    assert store.append(vector(), 1, strict=True)
    frame = pd.read_parquet(next(tmp_path.glob("*.parquet")))
    assert PROOF_COLUMN in frame and PROOF_COLUMN not in COLUMNS
    assert checked_row(frame.iloc[0].to_dict()) is not None


@pytest.mark.parametrize("encoded", [False, True])
def test_model_schema_and_inference_require_current_population_and_input(encoded):
    from features.numeric_encoding import PREFIX, checked_numeric_schema, numeric_encoding_schema
    from features.auxiliary_semantics import checked_model_frame
    names = ["market_cap_usd"] + ([PREFIX+"market_cap_usd"] if encoded else [])
    source = rows()
    metadata = {"numeric_encoding":numeric_encoding_schema(names), "rows":40}
    assert not checked_numeric_schema(metadata, names)
    metadata["valuation_training"] = population_proof(source)
    assert checked_numeric_schema(metadata, names)
    checked_model_frame(source.iloc[:1], names)
    with pytest.raises(ValueError, match="cap"):
        checked_model_frame(source.iloc[:1].drop(columns=[PROOF_COLUMN]), names)
    legacy = deepcopy(metadata)
    legacy["numeric_encoding"].pop("valuation_semantics")
    assert not checked_numeric_schema(legacy, names)


@pytest.mark.parametrize("bad", [True, 1, 29, 41, None])
def test_population_metadata_does_not_certify_wrong_denominator(bad):
    metadata = {"rows":40, "valuation_training":population_proof(rows())}
    metadata["valuation_training"]["current_rows"] = bad
    assert not checked_population(metadata, ["market_cap_usd"])


@pytest.mark.parametrize("encoded", [False, True])
def test_actual_multi_head_ranking_keeps_receipt_and_isolates_bad_peer(tmp_path, monkeypatch, encoded):
    import analytics.model_runtime_common as runtime
    from features.numeric_encoding import PREFIX, numeric_encoding_schema
    from test_multi_head_inference import write_head, publish
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    names = ["market_cap_usd"] + ([PREFIX+"market_cap_usd"] if encoded else [])
    entry = write_head(tmp_path, "runner_100", .8, version="valuation-current", metadata_changes={
        "features":names, "numeric_encoding":numeric_encoding_schema(names),
        "valuation_training":population_proof(rows())})
    publish(tmp_path, {"runner_100":entry})
    good, bad, unknown = vector(), vector(1), vector(2, cap=None)
    bad.attrs[PROOF_COLUMN] = "bad-json"
    expected = [runtime.predict_ranking_score("runner", "runner_100", item) for item in (good, bad, unknown)]
    actual = runtime.predict_ranking_scores("runner", "runner_100", [good, bad, unknown])
    assert actual == expected and actual[0] is not None and actual[1] is None and actual[2] is not None


def test_legacy_cap_model_is_rejected_before_deserialization(tmp_path, monkeypatch):
    import analytics.model_runtime_common as runtime
    from features.numeric_encoding import numeric_encoding_schema
    path = tmp_path / "runner_100.pkl"
    path.write_bytes(b"synthetic-not-a-model")
    path.with_suffix(".meta.json").write_text(json.dumps({"features":["market_cap_usd"],
        "model_sha256":sha256(path.read_bytes()).hexdigest(),
        "numeric_encoding":numeric_encoding_schema(["market_cap_usd"])}))
    called = []
    monkeypatch.setattr(runtime.joblib, "load", lambda *a, **k: called.append(True))
    runtime.invalidate_model_cache(path)
    assert runtime._load_unscoped(path, require_temporal_validation=False)[0] is None
    assert called == []


def test_stable_nonvaluation_numeric_model_needs_no_invented_new_population():
    from features.numeric_encoding import PREFIX, numeric_encoding_schema, checked_numeric_schema
    names = ["txns_last_5m", PREFIX+"txns_last_5m"]
    schema = numeric_encoding_schema(names)
    assert "valuation_semantics" not in schema
    assert checked_numeric_schema({"numeric_encoding":schema}, names)


@pytest.mark.parametrize("n", [29, 40])
def test_family_target_needs_its_own_original_population_not_parent(n):
    from ml.family_training import _supported_target_features
    from features.numeric_encoding import PREFIX
    frame = rows().iloc[:n]
    names = ["market_cap_usd", PREFIX+"market_cap_usd", "txns_last_5m"]
    out = _supported_target_features(names, frame)
    assert out == (names if n >= 30 else ["txns_last_5m"])


def test_costed_source_keeps_original_cap_and_cannot_be_upgraded_after_close():
    from net_financial_fixtures import net_frame
    from ml.financial_targets import checked_net_return
    frame = rows(1).assign(target_total_pnl_pct=100., max_pnl_pct_seen=25000.,
                           ts=STAMP+dt.timedelta(minutes=2))
    row = net_frame(frame).iloc[0].to_dict()
    assert checked_net_return(row) is not None and checked_row(row) is not None
    source = json.loads(row["outcome_execution_proof"])
    assert source["entry_features"]["valuation"]["values"]["market_cap_usd"] == pytest.approx(12000.1234)
    legacy = deepcopy(source)
    legacy["entry_features"].pop("valuation")
    rehash(legacy["entry_features"])
    rehash(legacy)
    row["outcome_execution_proof"] = json.dumps(legacy)
    row["outcome_source_sha256"] = legacy["payload_sha256"]
    row.pop("t0_auxiliary_semantics_proof", None)
    assert checked_net_return(row) is not None  # Original financial meaning stays usable.
    assert checked_row(row) is None  # A later standalone valuation marker cannot upgrade it.
