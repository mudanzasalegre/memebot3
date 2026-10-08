"""Isolated original decision evidence; not a live or profitable strategy run."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from pathlib import Path

import pandas as pd
import numpy as np
import pytest

from analytics.inference_scope import inference_scope, MAX_PREDICTIONS
from analytics.decision_provenance import (input_identity, model_query_snapshot, record_model_query,
    model_source, digest)
from runtime import entry_decision as decision, trade_learning as learning
from runtime.buy_recovery import BuyRecoveryStore
from features.builder import build_feature_vector, COLUMNS
from test_financial_model_acceptance import setup_entry
from test_trade_learning import closed, dataset
from test_paper_archive import paper


def vector(i=0):
    return build_feature_vector({"address": f"A{i:031d}", "price_pct_5m": i + 1,
        "entry_lane": "pump_early_green_candle_sniper"}, now=pd.Timestamp("2026-10-01T00:00:00Z").to_pydatetime())


def record(vec, *, target="synthetic", value=.5):
    metadata = {"_artifact_runtime": {"model_sha256": "a" * 64, "metadata_sha256": "b" * 64}}
    record_model_query(vec, family="entry", target=target, operation="probability", value=value,
                       model=object(), features=["price_pct_5m"], metadata=metadata)


def draft(vec=None):
    return decision.capture_entry_decision(vec if vec is not None else vector(), {}, paper=True, amount_sol=.1)


def journal(draft, *, intent="a" * 32):
    from db.models import Position
    from runtime.buy_recovery import position_snapshot
    original = vector()
    position = Position(address=original.address, token_mint=original.address, dry_run=True,
        buy_amount_sol=.1, run_id="synthetic", qty=0, entry_qty=0, buy_price_usd=0.)
    created = "2026-10-01T00:00:01+00:00"
    row = {"version": 1, "intent_id": intent, "created_at": created, "state": "prepared",
        "address": original.address, "paper": True, "amount_sol": .1, "base_position": position_snapshot(position),
        "entry_features": learning.freeze_entry_features(original, address=original.address, captured_at=created)}
    row["entry_decision"] = decision.bind_entry_decision(draft, row)
    return row


def test_original_primary_query_uses_loaded_bytes_and_never_rereads_for_provenance(tmp_path, monkeypatch):
    runtime, _, artifact = setup_entry(tmp_path, monkeypatch)
    original_model = sha256(artifact.model_path.read_bytes()).hexdigest()
    original_meta = sha256(artifact.meta_path.read_bytes()).hexdigest()
    vec = vector()
    with inference_scope():
        assert runtime.should_buy(vec) == .5
        artifact.meta_path.write_text("{}"); artifact.model_path.write_bytes(b"later-corruption")
        captured = model_query_snapshot(vec)
        query = captured["observations"][0]
        assert query["value"] == .5 and query["source"]["mode"] == "checked_legacy_artifact"
        assert query["source"]["component_sha256"] == {"model": original_model, "meta": original_meta}
        receipt = draft(vec)
        decision.validate_draft(receipt)
        receipt["model_queries"]["observations"][0]["value"] = .99
        assert model_query_snapshot(vec)["observations"][0]["value"] == .5
    assert model_query_snapshot(vec)["status"] == "scope_missing"


def test_atomic_primary_components_are_translated_without_losing_original_hashes():
    hashes = {name: str(i) * 64 for i, name in enumerate(("model.pkl", "model.meta.json", "threshold.json",
        "thresholds.by_lane.json", "acceptance.json"), 1)}
    source = model_source(object(), ["x"], {"_primary_runtime": {"mode": "atomic_primary_bundle",
        "revision": 0, "component_sha256": hashes}}, primary_reader=True)
    assert source["status"] == "checked_artifact" and source["revision"] == 0
    assert source["component_sha256"] == dict(zip(("model", "meta", "thresholds", "lane_thresholds", "acceptance"), hashes.values()))


def test_only_final_vector_actual_calls_are_included_and_absent_heads_are_not_invented():
    old, current = vector(), vector(1)
    with inference_scope() as state:
        record(old, target="old_head")
        record(current, target="current_head")
        record(current, target="current_head", value=.6)
        receipt = draft(current)
        assert len(receipt["model_queries"]["observations"]) == 1
        assert receipt["model_queries"]["observations"][0]["target"] == "current_head"
        assert receipt["model_queries"]["observations"][0]["value"] == .6
        assert not receipt["coverage_complete"] and not receipt["buy_permission"]
        assert not receipt["full_strategy_profitability_established"]
    assert not state.observations


def test_nested_concurrent_and_closed_scopes_cannot_mix_decisions():
    async def task(i):
        vec = vector(i)
        with inference_scope():
            record(vec, target=f"head_{i}")
            await asyncio.sleep(0)
            return draft(vec)["model_queries"]["observations"][0]["target"]
    async def run(): return await asyncio.gather(*(task(i) for i in range(5)))
    assert asyncio.run(run()) == [f"head_{i}" for i in range(5)]
    with inference_scope():
        record(vector(), target="parent")
        with inference_scope():
            assert not model_query_snapshot(vector())["observations"]
        assert model_query_snapshot(vector())["observations"][0]["target"] == "parent"


def test_trace_is_bounded_and_reports_eviction_without_fabricating_complete_coverage():
    with inference_scope():
        for i in range(MAX_PREDICTIONS + 5): record(vector(), target=f"head_{i}")
        receipt = draft()
        assert len(receipt["model_queries"]["observations"]) == MAX_PREDICTIONS
        assert receipt["model_queries"]["dropped"] == 5 and not receipt["coverage_complete"]


def test_telemetry_invalid_partial_inputs_do_not_change_predictions(tmp_path, monkeypatch):
    runtime, _, _ = setup_entry(tmp_path, monkeypatch)
    with inference_scope():
        assert runtime.should_buy({"price_pct_5m": 1}) == .5
        assert not model_query_snapshot(vector())["observations"]
    assert draft()["model_queries"] == {"status": "scope_missing", "observations": [], "dropped": 0}


@pytest.mark.parametrize("mutation", ["hash", "intent", "run", "amount", "paper", "input", "input_receipt", "clock", "feature", "extra", "profit", "permission", "duplicate", "unknown_value"])
def test_strict_journal_identity_provenance_and_no_fake_acceptance(mutation):
    with inference_scope():
        record(vector())
        row = journal(draft())
    proof = row["entry_decision"]
    if mutation == "intent": proof["intent_id"] = "b" * 32
    elif mutation == "run": proof["run_id"] = "another-run"
    elif mutation == "amount": proof["entry"]["amount_sol"] = .2
    elif mutation == "paper": proof["entry"]["paper"] = False
    elif mutation == "input": proof["input_vector_sha256"] = "0" * 64
    elif mutation == "input_receipt": proof["input_receipt_sha256"] = "0" * 64
    elif mutation == "clock": proof["input_captured_at_utc"] = "2026-10-01T00:00:02Z"
    elif mutation == "feature": proof["entry_features_sha256"] = "0" * 64
    elif mutation == "extra": proof["wallet_private_key"] = "forbidden-not-a-real-key"
    elif mutation == "profit": proof["full_strategy_profitability_established"] = True
    elif mutation == "permission": proof["buy_permission"] = True
    elif mutation == "duplicate": proof["model_queries"]["observations"] *= 2
    elif mutation == "unknown_value":
        source = proof["model_queries"]["observations"][0]["source"]
        source.update(status="unknown", mode="unavailable", component_sha256={}, feature_schema_sha256=None)
        source["identity_sha256"] = digest({k: v for k, v in source.items() if k != "identity_sha256"})
    proof["payload_sha256"] = digest({k: v for k, v in proof.items() if k != "payload_sha256"})
    if mutation == "hash": proof["payload_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="decision proof"):
        BuyRecoveryStore._validate_row(row)


def test_model_artifact_identity_can_be_checked_without_fabricated_common_fit(tmp_path, monkeypatch):
    from test_multi_head_inference import write_head, publish
    from analytics import model_runtime_common as runtime
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    head = write_head(tmp_path, "runner_100", .8, version="one")
    unused = write_head(tmp_path, "runner_10000", .1, version="two")
    manifest = publish(tmp_path, {"runner_100": head, "runner_10000": unused})
    with inference_scope():
        assert runtime.predict_ranking_score("runner", "runner_100", vector()) == 80
        source = model_query_snapshot(vector())["observations"][0]["source"]
        assert source["selector_sha256"] == manifest and source["revision"] == "one"
        assert source["component_sha256"]["meta"] == head["metadata_sha256"]
        assert not source["same_training_cohort_asserted"]
        decision.validate_draft(draft())
        assert len(model_query_snapshot(vector())["observations"]) == 1


def test_flat_artifact_metadata_hash_is_original_without_a_fabricated_selector(tmp_path, monkeypatch):
    from test_multi_head_inference import write_head
    from analytics import model_runtime_common as runtime
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    write_head(tmp_path, "runner_100", .8)
    path = tmp_path / "ml" / "models" / "runner" / "runner_100.meta.json"
    original = sha256(path.read_bytes()).hexdigest()
    with inference_scope():
        assert runtime.predict_ranking_score("runner", "runner_100", vector()) == 80
        path.write_text("{}"); runtime.invalidate_model_cache(path.with_suffix(".pkl"))
        source = model_query_snapshot(vector())["observations"][0]["source"]
        assert source["mode"] == "legacy_flat" and source["component_sha256"]["meta"] == original
        assert source["selector_sha256"] is None and source["revision"] is None
        assert runtime.family_model_selection("runner")["heads"]["runner_100"]["metadata_sha256"] == original
        decision.validate_draft(draft())


def test_real_ml_policy_and_original_paper_binding_are_detached(tmp_path, monkeypatch):
    from analytics.ml_policy import MlPolicyDecision
    from runtime.paper_entry_policy import parameter_scope
    cfg = SimpleNamespace(DRY_RUN=True, SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M=40,
                          SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M=200)
    policy = MlPolicyDecision("advisory", "pump_early_green_candle_sniper", .7, .5, True, False,
                              1., False, "synthetic", False, "synthetic")
    with parameter_scope(cfg, {"SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M": 50}, revision="trial", evidence_sha256="c" * 64):
        captured = decision.capture_entry_decision(vector(), {}, paper=True, amount_sol=.1, ml_policy=policy)
    assert captured["ml_policy"] == policy.to_dict()
    assert captured["paper_entry_policy"]["parameters"] == {"SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M": 50}
    row = journal(captured)
    captured["paper_entry_policy"]["parameters"]["SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M"] = 999
    captured["ml_policy"]["allow_buy"] = False
    BuyRecoveryStore._validate_row(row)
    assert row["entry_decision"]["ml_policy"]["allow_buy"]


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [{"auxiliary": True, "strategy": "sniper_research_momentum_ignition", "provenance": True}], indirect=True)
async def test_corrupt_declared_decision_invalidates_financial_training_not_just_logging(closed):
    from ml.financial_targets import checked_net_return, checked_financial_frame
    learning.publish_close(closed.identity, root=closed.root)
    frame = dataset(closed.root)
    source = json.loads(frame.iloc[0]["outcome_execution_proof"])
    proof = source["entry_decision"]
    proof["run_id"] = "invented-run"
    proof["payload_sha256"] = digest({k: v for k, v in proof.items() if k != "payload_sha256"})
    source["payload_sha256"] = learning._hash({k: v for k, v in source.items() if k != "payload_sha256"})
    frame["outcome_execution_proof"] = json.dumps(source)
    frame["outcome_source_sha256"] = source["payload_sha256"]
    assert checked_net_return(frame.iloc[0].to_dict()) is None
    assert decision.checked_entry_decision(frame.iloc[0].to_dict()) is None
    restored, report = checked_financial_frame(frame)
    assert restored.empty and report["conflicting_trade_ids"] == [closed.identity]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_actual_pre_buy_and_restart_bind_real_query_to_one_intent_without_resubmission(tmp_path, monkeypatch, failure):
    from test_buy_recovery import (configure_paper, database, execution_tail_namespace, MINT)
    from test_auxiliary_semantics import current_vector
    from features.auxiliary_semantics import input_frame, checked_row_receipt
    from unittest.mock import AsyncMock
    from sqlalchemy import select
    from db.models import Token, Position
    runtime, _, _ = setup_entry(tmp_path, monkeypatch)
    vec = current_vector(monkeypatch, 1, subprofile="sniper_research_momentum_ignition", address=MINT)
    observations = deepcopy(checked_row_receipt(input_frame(vec).iloc[0].to_dict())["auxiliary_observations"])
    paper_runtime = configure_paper(monkeypatch, tmp_path)
    store = BuyRecoveryStore(tmp_path / "journal")
    namespace = execution_tail_namespace(tmp_path, store, paper_runtime)
    namespace["vec"] = vec
    namespace["entry_observation"] = SimpleNamespace(social=json.dumps(observations.pop("social")), auxiliary=json.dumps(observations))
    engine, sessions = await database(tmp_path)
    try:
        async with sessions() as seed:
            seed.add(Token(address=MINT)); await seed.commit()
        token = {"address": MINT, "entry_subprofile": "sniper_research_momentum_ignition"}
        with inference_scope(), store.scope():
            assert runtime.should_buy(vec) == .5
            async with sessions() as session:
                if failure:
                    monkeypatch.setattr(session, "commit", AsyncMock(side_effect=RuntimeError("isolated SQL failure")))
                    with pytest.raises(RuntimeError): await namespace["execution_tail"](token, session)
                else: await namespace["execution_tail"](token, session)
        restarted = BuyRecoveryStore(store.directory)
        async with sessions() as session:
            result = await restarted.recover(session, paper_portfolio=paper_runtime.load_portfolio())
            assert not result["failed"] and len(result["resolved"]) == int(failure)
            position = (await session.execute(select(Position))).scalar_one()
            assert position.buy_amount_sol == .1
        receipt = json.loads(next((store.directory / "resolved").glob("*.json")).read_text())["entry_decision"]
        assert receipt["model_queries"]["observations"][0]["value"] == .5
        assert receipt["intent_id"] == position.source_position_key.removeprefix("buy:")
        assert receipt["entry"]["entry_subprofile"] == "sniper_research_momentum_ignition"
        assert len(paper_runtime._PORTFOLIO) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [{"auxiliary": True, "strategy": "sniper_research_momentum_ignition", "provenance": True}], indirect=True)
async def test_original_decision_survives_close_parquet_csv_and_checked_net_restore(closed):
    from features import store
    from ml.financial_targets import checked_financial_frame
    assert learning.publish_close(closed.identity, root=closed.root)["status"] == "written"
    original = learning.prepare_close(closed.identity, root=closed.root)
    assert original["entry_decision"]["intent_id"] == closed.identity
    frame = dataset(closed.root)
    proof = decision.checked_entry_decision(frame.iloc[0].to_dict())
    assert proof == original["entry_decision"]
    assert proof["model_queries"]["observations"][0]["value"] == .5
    assert proof["input_receipt_sha256"] is not None
    frame["entry_decision"] = "post-close-invented-model"
    restored, _ = checked_financial_frame(frame)
    assert decision.checked_entry_decision(restored.iloc[0].to_dict()) == proof
    store.export_csv()
    path = next((closed.root / "data" / "features").glob("features_*.parquet"))
    assert decision.checked_entry_decision(pd.read_csv(path.with_suffix(".csv")).iloc[0].to_dict()) == proof
    proof["run_id"] = "mutable-return-copy"
    assert decision.checked_entry_decision(frame.iloc[0].to_dict())["run_id"] == original["entry_decision"]["run_id"]


def test_legacy_net_receipt_does_not_get_retrospective_model_provenance():
    from net_financial_fixtures import net_frame
    from test_financial_model_acceptance import frame
    row = net_frame(frame(4)).iloc[0].to_dict()
    assert decision.checked_entry_decision({**row, "entry_decision": "post-close-invented-model"}) is None


def test_decision_metadata_is_never_selected_as_a_numeric_predictor():
    from ml.train import _select_feature_columns
    frame = pd.DataFrame({"entry_decision": [1., 2.], "model_queries": [3., 4.], "price_pct_5m": [1., 2.]})
    _, features, excluded = _select_feature_columns(frame)
    assert not {"entry_decision", "model_queries"} & set(features)
    assert {"entry_decision", "model_queries"} <= set(excluded)
    assert len(COLUMNS) == 77


@pytest.mark.parametrize("value", [np.float32(.5), np.float64(.5), np.int64(1)])
def test_numpy_model_measurements_are_json_native_without_changing_value(value):
    with inference_scope():
        record(vector(), value=value)
        receipt = draft()
    observed = receipt["model_queries"]["observations"][0]["value"]
    assert type(observed) is float and observed == float(value)
    assert json.loads(json.dumps(receipt, allow_nan=False)) == receipt


@pytest.mark.parametrize("value", [True, np.bool_(True), "0.5", np.array([.5]),
                                 np.float64("nan"), np.float64("inf")])
def test_nonmeasurement_telemetry_is_not_silently_coerced_into_evidence(value):
    with inference_scope():
        record(vector(), value=value)
        assert not draft()["model_queries"]["observations"]


def test_equal_raw_values_do_not_mix_original_strategy_input_generations(monkeypatch):
    from test_auxiliary_semantics import current_vector
    from features.auxiliary_semantics import PROOF_COLUMN
    first = current_vector(monkeypatch, subprofile="sniper_research_momentum_ignition")
    second = first.copy(deep=True)
    proof = json.loads(first.attrs[PROOF_COLUMN])
    proof["strategy_context"]["entry_subprofile"] = "sniper_research_deep_reversal"
    proof["payload_sha256"] = digest({k: v for k, v in proof.items() if k != "payload_sha256"})
    second.attrs[PROOF_COLUMN] = json.dumps(proof)
    assert input_identity(first)["input_vector_sha256"] == input_identity(second)["input_vector_sha256"]
    assert input_identity(first)["input_receipt_sha256"] != input_identity(second)["input_receipt_sha256"]
    with inference_scope():
        record(first, target="original_profile")
        assert not model_query_snapshot(second)["observations"]
        record(second, target="final_profile")
        assert model_query_snapshot(second)["observations"][0]["target"] == "final_profile"
        second.attrs.pop(PROOF_COLUMN)
        assert not model_query_snapshot(second)["observations"]


def test_declared_invalid_original_receipt_cannot_be_downgraded_to_plain_vector(monkeypatch):
    from test_auxiliary_semantics import current_vector
    from features.auxiliary_semantics import PROOF_COLUMN
    vec = current_vector(monkeypatch)
    proof = json.loads(vec.attrs[PROOF_COLUMN])
    proof["payload_sha256"] = "0" * 64
    vec.attrs[PROOF_COLUMN] = json.dumps(proof)
    with inference_scope():
        record(vec)
        assert not model_query_snapshot(vector())["observations"]
        with pytest.raises(ValueError, match="input receipt"):
            decision.capture_entry_decision(vec, {}, paper=True, amount_sol=.1)


def test_scope_closing_between_fast_check_and_locked_snapshot_has_no_late_trace():
    from analytics.inference_scope import observation_snapshot
    with inference_scope() as state:
        state.observations_dropped = 3
        lock = state.lock
        class CloseWhenLocked:
            def __enter__(self):
                lock.acquire()
                state.closed = True
                state.observations.clear()
            def __exit__(self, *args): lock.release()
        state.lock = CloseWhenLocked()
        assert observation_snapshot("a" * 64, None) == {"status": "scope_missing", "observations": [], "dropped": 0}


@pytest.mark.parametrize("fault", ["extra_component", "schema", "selector", "revision", "mode",
                                  "common_fit", "numeric_string", "probability", "operation", "input_receipt"])
def test_invalid_query_source_or_measurement_cannot_become_original_evidence(fault):
    with inference_scope():
        record(vector())
        captured = draft()
    query = captured["model_queries"]["observations"][0]
    source = query["source"]
    if fault == "extra_component": source["component_sha256"]["invented"] = "c" * 64
    elif fault == "schema": source["feature_schema_sha256"] = "wrong"
    elif fault == "selector": source["selector_sha256"] = "c" * 64
    elif fault == "revision": source["revision"] = 1
    elif fault == "mode": source["mode"] = "unavailable"
    elif fault == "common_fit": source["same_training_cohort_asserted"] = True
    elif fault == "numeric_string": query["value"] = "0.5"
    elif fault == "probability": query["value"] = 2.
    elif fault == "operation": query["operation"] = "guaranteed_profit"
    elif fault == "input_receipt": query["input_receipt_sha256"] = "c" * 64
    source["identity_sha256"] = digest({k: v for k, v in source.items() if k != "identity_sha256"})
    with pytest.raises(ValueError): decision.validate_draft(captured)


@pytest.mark.parametrize("fault", ["bool_probability", "nonfinite_ev", "negative_size", "extra_secret", "numeric_flag"])
def test_invalid_ml_policy_cannot_become_original_admission_evidence(fault):
    from analytics.ml_policy import MlPolicyDecision
    captured = decision.capture_entry_decision(vector(), {}, paper=True, amount_sol=.1,
        ml_policy=MlPolicyDecision("advisory", "runner", .7, .5, True, False, 1., False, "synthetic", False, "synthetic"))
    policy = captured["ml_policy"]
    if fault == "bool_probability": policy["proba"] = True
    elif fault == "nonfinite_ev": policy["ev_pred_pct"] = float("nan")
    elif fault == "negative_size": policy["sizing_multiplier"] = -1.
    elif fault == "extra_secret": policy["private_key"] = "not-a-real-secret"
    elif fault == "numeric_flag": policy["enforce"] = 1
    with pytest.raises(ValueError): decision.validate_draft(captured)


@pytest.mark.parametrize("primary_reader", [False, True])
def test_the_actual_reader_not_persisted_foreign_fields_owns_model_identity(primary_reader):
    filenames = ("model.pkl", "model.meta.json", "threshold.json", "thresholds.by_lane.json", "acceptance.json")
    metadata = {"_primary_runtime": {"mode": "atomic_primary_bundle", "revision": 0,
                    "component_sha256": {name: "c" * 64 for name in filenames}},
                "_artifact_runtime": {"mode": "legacy_flat", "model_sha256": "a" * 64, "metadata_sha256": "b" * 64}}
    source = model_source(object(), ["x"], metadata, primary_reader=primary_reader)
    assert source["mode"] == ("atomic_primary_bundle" if primary_reader else "legacy_flat")
    assert source["component_sha256"]["model"] == ("c" * 64 if primary_reader else "a" * 64)
    assert source["revision"] == (0 if primary_reader else None)
