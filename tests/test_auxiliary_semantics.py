"""Isolated T0-generation transport; no provider, active model or bot run."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

import pandas as pd
import pyarrow.parquet as pq
import pytest

from features import auxiliary_semantics as sem
from features.builder import build_feature_vector, COLUMNS, ALLOWED_FEATURES
from analytics.social_signal import unknown_social_signal, apply_social_signal_to_token
from runtime.auxiliary_enrichment import prepare_cheap_auxiliary
from utils import auxiliary_observation as aux
from utils.market_observation import stamp_market_observation


def current_vector(monkeypatch, i=0):
    stamp = pd.Timestamp("2026-09-01T00:00:00Z") + pd.Timedelta(minutes=10 * i)
    clock = stamp.timestamp()
    with monkeypatch.context() as scope:
        scope.setattr(aux.time, "time", lambda: clock)
        scope.setattr(aux, "observation_clock", lambda: clock)
        token = stamp_market_observation({"address": f"M{i:031d}", "price_usd": 1.,
            "price_pct_5m": 80.12345 if i % 2 else -5.12345,
            "txns_last_5m_buys": 10, "txns_last_5m_sells": 2, "txns_last_5m": 12,
            "liquidity_usd": 20000.12345, "liquidity_is_proxy": 0, "liquidity_usd_is_proxy": 0,
            "entry_lane": "pump_early_green_sniper", "entry_regime": "pump_early",
            "created_at": (stamp - pd.Timedelta(minutes=2)).isoformat(),
            "score_total": 50 if i % 2 else 20}, "dexscreener", received_at=clock - 1)
        apply_social_signal_to_token(token, unknown_social_signal(source="synthetic_unavailable"))
        prepare_cheap_auxiliary(token)
        return build_feature_vector(token, now=stamp.to_pydatetime())


def current_frame(monkeypatch, n=160):
    rows = []
    for i in range(n):
        vector = current_vector(monkeypatch, i)
        row = sem.input_frame(vector).iloc[0].to_dict()
        row.update(mint=row["address"], ts=row["timestamp"] + pd.Timedelta(minutes=2),
            max_pnl_pct_seen=1600 if i % 2 else 3, target_total_pnl_pct=100 if i % 2 else -10,
            sample_type="shadow", label=i % 2)
        rows.append(row)
    return pd.DataFrame(rows)


def test_builder_preserves_raw_shape_and_binds_an_original_nonpredictor_receipt(monkeypatch):
    vector = current_vector(monkeypatch)
    assert list(vector.index) == COLUMNS and len(COLUMNS) == 77
    assert sem.PROOF_COLUMN not in ALLOWED_FEATURES and sem.PROOF_COLUMN not in COLUMNS
    row = sem.input_frame(vector).iloc[0].to_dict()
    proof = sem.checked_row_receipt(row)
    assert proof is not None and proof["vector"]["trend"] == 0
    assert proof["vector"]["rug_score"] is None and proof["vector"]["cluster_bad"] is None
    assert proof["vector"]["timestamp"] == vector["timestamp"].isoformat()
    assert sem.checked_row_receipt(vector.to_dict()) is None


@pytest.mark.parametrize("field,value", [("address", "other"), ("timestamp", "2026-09-01T00:00:01Z"),
    ("trend", 1), ("rug_score", 20), ("cluster_bad", False), ("score_total", 99),
    ("liquidity_usd", 99), ("price_pct_5m", -6), ("coverage_core_fields", 7),
    ("missing_trend", 1)])
def test_receipt_cannot_be_rebound_to_another_or_modified_t0_row(monkeypatch, field, value):
    row = sem.input_frame(current_vector(monkeypatch)).iloc[0].to_dict()
    row[field] = value
    assert sem.checked_row_receipt(row) is None
    with pytest.raises(ValueError, match="generation"):
        sem.checked_model_frame(pd.DataFrame([row]), ["trend"])


@pytest.mark.parametrize("change", ["hash", "version", "extra_key", "future_clock", "wrong_basis"])
def test_invalid_source_generations_are_not_restamped(monkeypatch, change):
    row = sem.input_frame(current_vector(monkeypatch)).iloc[0].to_dict()
    proof = json.loads(row[sem.PROOF_COLUMN])
    if change == "hash": proof["payload_sha256"] = "0" * 64
    elif change == "version": proof["version"] = "frozen_entry_features_v1"
    elif change == "extra_key": proof["auxiliary_observations"]["invented"] = {}
    else:
        record = proof["auxiliary_observations"]["trend"]
        if change == "future_clock": record["evaluated_at"] += 1
        else: record["basis"] = "old_chart_ema"
        record["payload_sha256"] = aux._hash({k: v for k, v in record.items() if k != "payload_sha256"})
        proof["payload_sha256"] = aux._hash({k: v for k, v in proof.items() if k != "payload_sha256"})
    row[sem.PROOF_COLUMN] = json.dumps(proof)
    assert sem.checked_row_receipt(row) is None


def test_legacy_and_small_current_population_use_stable_inputs_without_dropping_rows(monkeypatch):
    frame = current_frame(monkeypatch, 24)
    legacy = frame.iloc[:3].copy()
    legacy[sem.PROOF_COLUMN] = None
    legacy["rug_score"] = 1000
    mixed = pd.concat([frame, legacy], ignore_index=True)
    original = mixed.copy(deep=True)
    prepared, report = sem.prepare_training_frame(mixed)
    assert len(prepared) == len(mixed) and report["mode"] == "unchanged_historical_inputs_only"
    assert report["current_rows"] == 24
    assert not sem.semantic_sources(prepared.columns)
    assert "price_pct_5m" in prepared and "liquidity_usd" in prepared
    pd.testing.assert_frame_equal(mixed, original)


def test_enough_original_current_tokens_select_one_generation_not_mixed_meanings(monkeypatch):
    frame = current_frame(monkeypatch, 40)
    legacy = frame.iloc[:3].copy()
    legacy[sem.PROOF_COLUMN] = None
    mixed = pd.concat([legacy, frame], ignore_index=True)
    mixed.attrs["outcome_target_join"] = {"source": "preserved-original-join"}
    prepared, report = sem.prepare_training_frame(mixed)
    assert len(prepared) == 40 and report["mode"] == "current_receipts_only"
    assert report["current_rows"] == 40 and report["unique_tokens"] == 40
    assert "trend" in prepared and "rug_score" in prepared
    assert sem.population_proof(prepared) == report
    assert prepared.attrs["outcome_target_join"] == mixed.attrs["outcome_target_join"]
    # A higher family/primary training requirement must keep historical stable
    # inputs usable until the current population reaches that requirement.
    fallback, pending = sem.prepare_training_frame(mixed, min_current_rows=50)
    assert len(fallback) == len(mixed) and pending["mode"] == "unchanged_historical_inputs_only"
    assert not sem.semantic_sources(fallback.columns)


def test_duplicate_snapshots_do_not_manufacture_token_support(monkeypatch):
    frame = current_frame(monkeypatch, 2)
    repeated = pd.concat([frame] * 20, ignore_index=True)
    out, report = sem.prepare_training_frame(repeated)
    assert report["mode"] == "unchanged_historical_inputs_only"
    assert len(out) == 40 and not sem.semantic_sources(out.columns)


def test_store_upgrade_and_cold_reload_keep_old_rows_unknown_and_current_proof_original(tmp_path, monkeypatch):
    from features import store
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    legacy = current_vector(monkeypatch).to_dict()
    assert store.append(legacy, None, strict=True)
    path = next(tmp_path.glob("features_*.parquet"))
    pq.write_table(pq.read_table(path).drop([sem.PROOF_COLUMN]), path)
    vector = current_vector(monkeypatch, 1)
    assert store.append(vector, 1, strict=True)
    loaded = pq.read_table(path).to_pandas()
    assert loaded[sem.PROOF_COLUMN].iat[0] is None
    assert loaded[sem.PROOF_COLUMN].iat[1] == vector.attrs[sem.PROOF_COLUMN]
    assert sem.checked_row_receipt(loaded.iloc[1].to_dict()) is not None
    restored = sem.checked_model_frame(loaded.iloc[1:2], ["trend"])
    assert restored["price_pct_5m"].iat[0] == vector["price_pct_5m"]  # Original precision.
    store.export_csv()
    csv_row = pd.read_csv(path.with_suffix(".csv")).iloc[1].to_dict()
    assert sem.checked_row_receipt(csv_row) is not None, [name for name, value in json.loads(vector.attrs[sem.PROOF_COLUMN])["vector"].items()
        if not sem._same_value(name, csv_row.get(name), value)]


@pytest.mark.parametrize("features", [["trend"], ["t0num_missing__trend"], ["rug_score"], ["score_total"]])
def test_model_contract_rejects_old_or_unproved_auxiliary_meanings_before_deserialization(tmp_path, monkeypatch, features):
    import analytics.model_runtime_common as runtime
    path = tmp_path / "runner_100.pkl"
    path.write_bytes(b"not-a-model")
    path.with_suffix(".meta.json").write_text(json.dumps({"features": features,
        "model_sha256": sha256(path.read_bytes()).hexdigest()}))
    monkeypatch.setattr(runtime.joblib, "load", lambda *a, **kw: pytest.fail("old generation deserialized"))
    runtime.invalidate_model_cache(path)
    assert runtime._load_unscoped(path, require_temporal_validation=False)[0] is None


def test_semantic_schema_and_population_are_both_required(monkeypatch):
    frame = current_frame(monkeypatch, 40)
    features = ["trend", "rug_score"]
    meta = {"features": features, "auxiliary_semantics": sem.semantics_schema(features),
            "auxiliary_semantics_training": sem.population_proof(frame), "target_rows": 40}
    assert sem.checked_semantics_schema(meta, features)
    for key in ("auxiliary_semantics", "auxiliary_semantics_training"):
        broken = deepcopy(meta)
        broken.pop(key)
        assert not sem.checked_semantics_schema(broken, features)
    broken = deepcopy(meta)
    broken["auxiliary_semantics_training"]["unique_tokens"] = 1
    assert not sem.checked_semantics_schema(broken, features)
    assert sem.checked_semantics_schema({}, ["price_pct_5m"])


def test_real_current_generation_training_load_and_advisory_prediction(tmp_path, monkeypatch):
    from ml.family_training import train_classifier_family
    import analytics.model_runtime_common as runtime
    frame = current_frame(monkeypatch, 160)
    directory = tmp_path / "ml" / "models" / "runner"
    report = train_classifier_family(family="runner", targets=["runner_100"],
        feature_set_name="runner_features", frame=frame, output_dir=directory, min_rows=30)
    item = report["targets"]["runner_100"]
    assert item["status"] == "trained" and item["ranking_validation_ready"]
    path = directory / "runner_100.pkl"
    meta = json.loads(path.with_suffix(".meta.json").read_text())
    assert "trend" in meta["features"] and sem.checked_semantics_schema(meta, meta["features"])
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    assert runtime.predict_ranking_score("runner", "runner_100", current_vector(monkeypatch, 1)) is not None
    assert runtime.predict_ranking_score("runner", "runner_100", {"trend": 1}) is None


def test_primary_promotion_and_comparison_require_the_same_semantic_contract(tmp_path):
    from ml import primary_champion, model_registry
    metadata = {"features": ["trend"]}
    with pytest.raises(ValueError, match="encoding"):
        primary_champion._ensure_input_encoding(metadata)
    with pytest.raises(RuntimeError, match="encoding"):
        model_registry._ensure_financial_artifact(metadata, tmp_path / "not-deserialized.pkl")


def test_unusable_primary_generation_preserves_the_actual_cas_epoch(tmp_path, monkeypatch):
    from ml import primary_champion as champion, primary_activation as activation
    monkeypatch.setattr(activation, "read_registry", lambda path: {"unchanged": True})
    monkeypatch.setattr(activation, "active_epoch", lambda registry, path: "actual-active-epoch")
    monkeypatch.setattr(activation, "selected_reference", lambda *a: {"reference": {"model_id": "old"}})
    def obsolete(*a): raise sem.AuxiliarySemanticsError("old source generation")
    monkeypatch.setattr(activation, "read_bundle", obsolete)
    state = champion.current_incumbent(registry_path=tmp_path / "registry.json",
        models_dir=tmp_path, model_alias=tmp_path / "model.pkl")
    assert state["epoch"] == "actual-active-epoch" and state["model"] is None
    assert state["unavailable_reason"] == "auxiliary_semantics_changed"
    def corrupt(*a): raise ValueError("checksum mismatch")
    monkeypatch.setattr(activation, "read_bundle", corrupt)
    with pytest.raises(ValueError, match="checksum"):
        champion.current_incumbent(registry_path=tmp_path / "registry.json",
            models_dir=tmp_path, model_alias=tmp_path / "model.pkl")


def test_advisory_outer_cohort_uses_current_generation_and_checked_rollback(tmp_path, monkeypatch):
    from ml import runner_advisory_learning as advisory
    import analytics.model_runtime_common as runtime
    from types import SimpleNamespace
    monkeypatch.setattr(advisory, "CFG", SimpleNamespace(ML_RUNNER_ADVISORY_ENABLED=True,
        ML_RUNNER_ADVISORY_MIN_ROWS=40, ML_RUNNER_ADVISORY_MIN_LIFT_DELTA=.05))
    result = advisory.train_runner_advisory(root=tmp_path, frame=current_frame(monkeypatch, 160))
    assert result["updated"] and result["comparison_rows"] >= 30
    assert result["auxiliary_semantics_filtering"]["mode"] == "current_receipts_only"
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    assert runtime.predict_ranking_score("runner", "runner_100", current_vector(monkeypatch, 1)) is not None
    assert runtime.predict_model("runner", "runner_100", current_vector(monkeypatch, 1)) is None
    assert advisory.rollback_runner_advisory(root=tmp_path)
    assert runtime.predict_ranking_score("runner", "runner_100", current_vector(monkeypatch, 1)) is None
    assert advisory.rollback_runner_advisory(root=tmp_path)
    assert runtime.predict_ranking_score("runner", "runner_100", current_vector(monkeypatch, 1)) is not None
    # Simulate an intact, retained head whose semantic generation is obsolete.
    manifest_path = tmp_path / "ml" / "models" / "runner" / "advisory_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for head in manifest["heads"].values():
        meta_path = manifest_path.parent / head["path"]
        meta_path = meta_path.with_suffix(".meta.json")
        meta = json.loads(meta_path.read_text())
        meta.pop("auxiliary_semantics")
        meta_path.write_text(json.dumps(meta))
        # Construct an intact historical approval, not a later metadata mutation.
        head["metadata_sha256"] = sha256(meta_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    assert runtime.predict_ranking_score("runner", "runner_100", current_vector(monkeypatch, 1)) is None
    replacement = advisory.train_runner_advisory(root=tmp_path, frame=current_frame(monkeypatch, 160), force=True)
    assert replacement["updated"] and replacement["decisions"]["runner_100"]["obsolete_incumbent_generation"]
    assert runtime.predict_ranking_score("runner", "runner_100", current_vector(monkeypatch, 1)) is not None
    before = manifest_path.read_bytes()
    assert not advisory.rollback_runner_advisory(root=tmp_path)  # Old meaning cannot be reactivated.
    assert manifest_path.read_bytes() == before
