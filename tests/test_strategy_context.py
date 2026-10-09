"""Isolated causal selection tests; neither a provider run nor profit evidence."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from features import auxiliary_semantics as sem, context_encoding as enc
from features import strategy_context as ctx
from features.builder import COLUMNS
from ml.feature_matrix import coerce_feature_frame
from runtime import trade_learning as learning
from test_auxiliary_semantics import current_vector
from test_trade_learning import closed, dataset
from test_paper_archive import paper

FEATURES = [name for name, source in enc.FEATURE_SOURCES.items() if source == ctx.SOURCE]


def selected_frame(monkeypatch, n=160):
    rows = []
    for i in range(n):
        # Identical market/auxiliary observations; only the original selection
        # distinguishes the alternating labels. Addresses and clocks are not inputs.
        profile = ctx.VALUES[i % 2]
        vector = current_vector(monkeypatch, 2 * i + 1, subprofile=profile)
        row = sem.input_frame(vector).iloc[0].to_dict()
        row.update(mint=row["address"], ts=row["timestamp"] + pd.Timedelta(minutes=2),
            sample_type="shadow", max_pnl_pct_seen=1600 if i % 2 == 0 else 3,
            target_total_pnl_pct=100 if i % 2 == 0 else -10, label=int(i % 2 == 0))
        rows.append(row)
    return pd.DataFrame(rows)


@pytest.mark.parametrize("value", [*ctx.VALUES, None, "future_original_subprofile"])
def test_actual_producer_builder_encodes_the_original_selected_subprofile(monkeypatch, value):
    vector = current_vector(monkeypatch, 1, subprofile=value)
    frame = sem.input_frame(vector)
    proof = sem.checked_row_receipt(frame.iloc[0].to_dict())
    assert proof["version"] == ctx.ENTRY_VERSION
    assert proof["strategy_context"] == {"version": ctx.VERSION, ctx.SOURCE: value}
    assert list(vector.index) == COLUMNS and len(COLUMNS) == 77
    assert ctx.SOURCE not in proof["vector"]
    encoded = coerce_feature_frame(frame, FEATURES)
    category = value if value in ctx.VALUES else enc.MISSING if value is None else enc.OTHER
    assert encoded[f"t0ctx_entry_subprofile__{category}"].iat[0] == 1
    assert encoded.sum(axis=1).iat[0] == 1


@pytest.mark.parametrize("value", [True, 1, {}, [], "x" * 129, "line\nfeed"])
def test_invalid_producer_aliases_are_not_coerced(value):
    with pytest.raises(ValueError):
        ctx.capture_strategy_context({ctx.SOURCE: value})


def test_conflicting_aliases_and_changed_post_vector_selection_are_rejected(monkeypatch):
    with pytest.raises(ValueError, match="Conflicting"):
        ctx.capture_strategy_context({ctx.SOURCE: ctx.VALUES[0], "sniper_research_subprofile": ctx.VALUES[1]})
    vector = current_vector(monkeypatch, 1, subprofile=ctx.VALUES[0])
    token = {ctx.SOURCE: ctx.VALUES[0]}
    original = ctx.entry_strategy_context(vector, token)
    token[ctx.SOURCE] = ctx.VALUES[1]
    assert original[ctx.SOURCE] == ctx.VALUES[0]
    with pytest.raises(ValueError, match="changed"):
        ctx.entry_strategy_context(vector, token)
    assert ctx.entry_strategy_context(vector.to_dict(), token) is None


def test_reasons_gates_outcomes_and_mutable_raw_columns_cannot_invent_selection(monkeypatch):
    vector = current_vector(monkeypatch, 1, subprofile=ctx.VALUES[0])
    frame = sem.input_frame(vector)
    original = coerce_feature_frame(frame, FEATURES)
    changed = frame.assign(entry_subprofile=ctx.VALUES[1],
        **{name: 99 for name in FEATURES}, target_total_pnl_pct=-100, best_exit_profile="defensive")
    pd.testing.assert_frame_equal(original, coerce_feature_frame(changed, FEATURES))
    assert ctx.capture_strategy_context({"green_sniper_reason": ctx.VALUES[0],
        "gate_profile": ctx.VALUES[0], "best_exit_profile": ctx.VALUES[0]})[ctx.SOURCE] is None
    unproved = changed.drop(columns=[sem.PROOF_COLUMN])
    assert coerce_feature_frame(unproved, FEATURES)[f"t0ctx_entry_subprofile__{enc.MISSING}"].iat[0] == 1
    assert not set(FEATURES) & set(enc.available_context_features(unproved))
    with pytest.raises(ValueError, match="generation"):
        sem.checked_model_frame(unproved, FEATURES)


@pytest.mark.parametrize("mutation", ["hash", "context_version", "bool", "extra", "missing_aux", "clock"])
def test_v4_strict_keys_hash_scalar_and_original_clock(monkeypatch, mutation):
    row = sem.input_frame(current_vector(monkeypatch, 1, subprofile=ctx.VALUES[0])).iloc[0].to_dict()
    proof = json.loads(row[sem.PROOF_COLUMN])
    if mutation == "context_version": proof["strategy_context"]["version"] = "invented"
    elif mutation == "bool": proof["strategy_context"][ctx.SOURCE] = True
    elif mutation == "extra": proof["strategy_context"]["label"] = 1
    elif mutation == "missing_aux": proof["auxiliary_observations"].pop("rug")
    elif mutation == "clock": proof["captured_at"] = "2026-01-01T00:00:00Z"
    proof["payload_sha256"] = learning._hash({k: v for k, v in proof.items() if k != "payload_sha256"})
    if mutation == "hash": proof["payload_sha256"] = "0" * 64
    row[sem.PROOF_COLUMN] = json.dumps(proof)
    assert sem.checked_row_receipt(row) is None


def legacy_v3(frame):
    out = frame.copy()
    for index, row in out.iterrows():
        proof = json.loads(row[sem.PROOF_COLUMN])
        proof.pop("strategy_context")
        proof["version"] = sem.ENTRY_VERSION
        proof["payload_sha256"] = learning._hash({k: v for k, v in proof.items() if k != "payload_sha256"})
        out.at[index, sem.PROOF_COLUMN] = json.dumps(proof)
    return out


def test_v1_schema_and_v3_receipts_remain_compatible_without_retroactive_subprofiles(monkeypatch):
    old_domains = {source: values for source, values in enc.DOMAINS.items() if source != ctx.SOURCE}
    old_hash = sha256(json.dumps(old_domains, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    old_features = ["t0ctx_entry_lane__unobserved"]
    assert enc.SCHEMA_SHA256 == old_hash
    assert enc.checked_context_schema({"context_encoding": enc.context_encoding_schema(old_features)}, old_features)
    frame = legacy_v3(selected_frame(monkeypatch, 40)).assign(entry_subprofile=ctx.VALUES[0])
    before = frame.copy(deep=True)
    prepared, report = sem.prepare_training_frame(frame)
    assert report["current_rows"] == 40 and ctx.SOURCE not in prepared
    assert "trend" in prepared and ctx.population_proof(prepared)["current_rows"] == 0
    assert not set(FEATURES) & set(enc.available_context_features(prepared))
    pd.testing.assert_frame_equal(frame, before)
    with pytest.raises(ValueError, match="generation"):
        sem.checked_model_frame(frame, FEATURES)


def test_current_cohort_support_is_independent_and_target_specific(monkeypatch):
    original = selected_frame(monkeypatch, 40)
    mixed = pd.concat([legacy_v3(original), original.iloc[:20]], ignore_index=True)
    mixed[ctx.SOURCE] = ctx.VALUES[0]
    out, report = sem.prepare_training_frame(mixed)
    assert len(out) == 60 and report["current_rows"] == 60 and ctx.SOURCE not in out
    out, report = sem.prepare_training_frame(pd.concat([legacy_v3(original), original], ignore_index=True))
    assert len(out) == 40 and ctx.SOURCE in out
    proof = ctx.population_proof(out)
    metadata = {"context_encoding": enc.context_encoding_schema(FEATURES), "strategy_context_training": proof,
                "target_rows": len(out)}
    assert enc.checked_context_schema(metadata, FEATURES)
    metadata["strategy_context_training"] = ctx.population_proof(out.iloc[:20])
    assert not enc.checked_context_schema(metadata, FEATURES)
    repeated = pd.concat([original.iloc[:2]] * 20, ignore_index=True)
    fallback, _ = sem.prepare_training_frame(repeated)
    assert len(fallback) == 40 and ctx.SOURCE not in fallback
    assert enc.independent_input_count(FEATURES) == 1


def test_target_specific_missing_labels_do_not_borrow_parent_population(monkeypatch, tmp_path):
    from ml.family_training import train_classifier_family
    frame = selected_frame(monkeypatch)
    frame.loc[24:, "max_pnl_pct_seen"] = None
    report = train_classifier_family(family="runner", targets=["runner_1000"],
        feature_set_name="runner_features", frame=frame, output_dir=tmp_path / "runner", min_rows=20)
    item = report["targets"]["runner_1000"]
    assert item["target_rows"] == 24 and item["status"] == "trained"
    assert not set(FEATURES) & set(item["features"])
    assert not sem.semantic_sources(item["features"])
    metadata = json.loads(Path(item["model_path"]).with_suffix(".meta.json").read_text())
    assert enc.checked_context_schema(metadata, metadata["features"])
    assert sem.checked_semantics_schema(metadata, metadata["features"])


@pytest.mark.parametrize("mutation", ["missing", "rows", "tokens", "hash", "version", "receipt", "extra"])
def test_unproved_training_population_rejected_before_any_deserialization(tmp_path, monkeypatch, mutation):
    import analytics.model_runtime_common as runtime
    frame = selected_frame(monkeypatch, 40)
    metadata = {"features": FEATURES, "context_encoding": enc.context_encoding_schema(FEATURES),
                "strategy_context_training": ctx.population_proof(frame), "target_rows": 40}
    proof = metadata["strategy_context_training"]
    if mutation == "missing": metadata.pop("strategy_context_training")
    elif mutation == "rows": proof["rows"] = 41
    elif mutation == "tokens": proof["unique_tokens"] = 1
    elif mutation == "hash": proof["population_sha256"] = "not-a-hash"
    elif mutation == "version": proof["version"] = "unsupported"
    elif mutation == "receipt": proof["entry_receipt_version"] = sem.ENTRY_VERSION
    else: proof["post_close_selection"] = True
    assert not enc.checked_context_schema(metadata, FEATURES)
    path = tmp_path / "runner_100.pkl"
    path.write_bytes(b"not-a-model")
    metadata["model_sha256"] = sha256(path.read_bytes()).hexdigest()
    path.with_suffix(".meta.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(runtime.joblib, "load", lambda *a, **kw: pytest.fail("Unproved input deserialized"))
    runtime.invalidate_model_cache(path)
    assert runtime._load_unscoped(path, require_temporal_validation=False)[0] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [{"auxiliary": True, "strategy": ctx.VALUES[0]}], indirect=True)
async def test_durable_buy_close_restart_parquet_csv_and_net_target_replay(closed):
    from features import store
    from ml.financial_targets import checked_financial_frame
    from runtime.buy_recovery import BuyRecoveryStore
    journal = closed.root / "data" / "metrics" / "buy_recovery" / "resolved" / (closed.identity + ".json")
    before = journal.read_bytes()
    closed.pos.entry_subprofile = ctx.VALUES[1]  # SQL is deliberately not the chosen source.
    assert learning.publish_close(closed.identity, root=closed.root)
    source = learning.prepare_close(closed.identity, root=closed.root)
    assert source["entry_features"]["strategy_context"][ctx.SOURCE] == ctx.VALUES[0]
    BuyRecoveryStore._validate_row(json.loads(before))
    fresh = BuyRecoveryStore(journal.parent.parent)
    assert not fresh.pending_addresses
    frame = dataset(closed.root)
    restored, _ = checked_financial_frame(frame.assign(entry_subprofile=ctx.VALUES[1]))
    assert ctx.context_values(restored)[0] == [ctx.VALUES[0]]
    X = coerce_feature_frame(sem.checked_model_frame(restored, FEATURES), FEATURES)
    assert X[f"t0ctx_entry_subprofile__{ctx.VALUES[0]}"].iat[0] == 1
    store.export_csv()
    path = next((closed.root / "data" / "features").glob("features_*.parquet"))
    assert ctx.context_values(pd.read_csv(path.with_suffix(".csv")))[0] == [ctx.VALUES[0]]
    # The original close already stores the receipt; replay a pre-column export
    # without backfilling or modifying its historical parquet bytes.
    pq.write_table(pq.read_table(path).drop([sem.PROOF_COLUMN]), path)
    old_bytes = path.read_bytes()
    assert ctx.context_values(dataset(closed.root))[0] == [ctx.VALUES[0]]
    assert learning.publish_close(closed.identity, root=closed.root)["status"] == "already_written"
    assert path.read_bytes() == old_bytes and journal.read_bytes() == before


def test_actual_family_learning_reader_and_advisory_comparison_use_selected_context(tmp_path, monkeypatch):
    from ml.family_training import train_classifier_family
    from ml.runner_advisory_learning import _evaluate
    from analytics import model_runtime_common as runtime
    import joblib
    frame = selected_frame(monkeypatch)
    report = train_classifier_family(family="runner", targets=["runner_1000"],
        feature_set_name="runner_features", frame=frame,
        output_dir=tmp_path / "ml" / "models" / "runner", min_rows=30)
    item = report["targets"]["runner_1000"]
    assert item["status"] == "trained" and item["ranking_validation_ready"]
    path = Path(item["model_path"])
    metadata = json.loads(path.with_suffix(".meta.json").read_text())
    assert set(FEATURES) <= set(metadata["features"])
    assert enc.checked_context_schema(metadata, metadata["features"])
    assert metadata["strategy_context_training"] == ctx.population_proof(frame)
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    high = current_vector(monkeypatch, 1, subprofile=ctx.VALUES[0])
    low = current_vector(monkeypatch, 1, subprofile=ctx.VALUES[1])
    assert runtime.predict_ranking_score("runner", "runner_1000", high) > runtime.predict_ranking_score("runner", "runner_1000", low)
    assert runtime.predict_ranking_score("runner", "runner_1000", low.to_dict()) is None
    evaluated = _evaluate(joblib.load(path), metadata["features"], frame.assign(runner_1000=frame.label),
                          "runner_1000", metadata=metadata)
    assert evaluated["precision_lift_at_k"] == 2
    broken = deepcopy(metadata)
    broken.pop("strategy_context_training")
    with pytest.raises(ValueError, match="context encoding"):
        _evaluate(joblib.load(path), metadata["features"], frame, "runner_1000", metadata=broken)


def test_primary_fit_and_reader_use_the_same_original_context_contract(tmp_path, monkeypatch):
    from test_financial_model_acceptance import setup_entry
    from net_financial_fixtures import net_frame
    from ml import train as trainer
    from ml.entry_probability import probability_metadata
    from ml.financial_targets import checked_financial_frame
    runtime, registry, original = setup_entry(tmp_path, monkeypatch)
    data, financial = checked_financial_frame(net_frame(selected_frame(monkeypatch)))
    data, _ = sem.prepare_training_frame(data)
    data["mint"] = data.address
    data, features, excluded = trainer._select_feature_columns(data)
    features = [name for name in features if name in FEATURES]
    assert len(features) == 2 and ctx.SOURCE in excluded
    assert enc.independent_input_count(features) == 1
    model = trainer._fit_logreg_calibrated(data, features)
    candidate = trainer._evaluate_candidate(name="isolated_subprofile", model_family="sklearn_logreg",
        builder=trainer._fit_logreg_calibrated, x_cols=features, use_forward=True,
        tr_df=data.iloc[:120], te_df=data.iloc[120:])
    artifact = registry.write_candidate(model=model, meta={
        **json.loads(original.meta_path.read_text()), **probability_metadata(model, candidate.probability_evaluation),
        "features": features, "rows": len(data), "financial_training": financial,
        "context_encoding": enc.context_encoding_schema(features),
        "strategy_context_training": ctx.population_proof(data),
        "numeric_encoding": None, "auxiliary_semantics": None}, model_id="original-subprofile")
    monkeypatch.setattr(runtime, "_MODEL_PATH", artifact.model_path)
    monkeypatch.setattr(runtime, "_META_PATH", artifact.meta_path)
    low = current_vector(monkeypatch, 1, subprofile=ctx.VALUES[1])
    high = current_vector(monkeypatch, 1, subprofile=ctx.VALUES[0])
    assert runtime.should_buy(high) > runtime.should_buy(low)
    assert runtime.should_buy(high.to_dict()) is None
    metadata = json.loads(artifact.meta_path.read_text())
    metadata.pop("strategy_context_training")
    artifact.meta_path.write_text(json.dumps(metadata))
    assert runtime.should_buy(high) is None
    with pytest.raises(RuntimeError, match="context encoding"):
        registry.promote_candidate(artifact, active_model_path=tmp_path / "not-promoted.pkl")
    assert not (tmp_path / "not-promoted.pkl").exists()


def test_costed_family_targets_restore_context_and_bind_their_own_population(tmp_path, monkeypatch):
    from net_financial_fixtures import net_frame
    from ml.family_training import train_classifier_family, train_regressor_family
    from analytics import model_runtime_common as runtime
    data = net_frame(selected_frame(monkeypatch))
    data[ctx.SOURCE] = "untrusted_post_close_subprofile"
    risk = train_classifier_family(family="risk", targets=["severe_loss_configured"],
        feature_set_name="risk_features", frame=data, output_dir=tmp_path / "ml" / "models" / "risk",
        financial_target_parameters={"severe_loss_pct": -5})
    ev = train_regressor_family(family="ev", targets=["ev_realized"], feature_set_name="ev_features",
        frame=data, output_dir=tmp_path / "ml" / "models" / "ev")
    for report, target in ((risk, "severe_loss_configured"), (ev, "ev_realized")):
        item = report["targets"][target]
        assert item["status"] == "trained"
        metadata = json.loads(Path(item["model_path"]).with_suffix(".meta.json").read_text())
        assert enc.checked_context_schema(metadata, metadata["features"])
        assert metadata["strategy_context_training"]["rows"] == item["target_rows"] == 160
        assert metadata["strategy_context_training"]["current_rows"] == 160
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    high = current_vector(monkeypatch, 1, subprofile=ctx.VALUES[0])
    low = current_vector(monkeypatch, 1, subprofile=ctx.VALUES[1])
    assert runtime.predict_model("ev", "ev_realized", high) == pytest.approx(100)
    assert runtime.predict_model("ev", "ev_realized", low) == pytest.approx(-10)


def test_advisory_pipeline_fingerprint_and_rollback_preserve_original_context_proof(tmp_path, monkeypatch):
    from ml import runner_advisory_learning as advisory
    from types import SimpleNamespace
    monkeypatch.setattr(advisory, "CFG", SimpleNamespace(ML_RUNNER_ADVISORY_ENABLED=True,
        ML_RUNNER_ADVISORY_MIN_ROWS=40, ML_RUNNER_ADVISORY_MIN_LIFT_DELTA=.05))
    data = selected_frame(monkeypatch)
    result = advisory.train_runner_advisory(root=tmp_path, frame=data)
    assert result["updated"] and advisory.PIPELINE_VERSION == 8
    original_fingerprint = result["dataset_sha256"]
    manifest_path = tmp_path / "ml" / "models" / "runner" / "advisory_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["heads"]
    for head in manifest["heads"].values():
        path = manifest_path.parent / head["path"]
        metadata = json.loads(path.with_suffix(".meta.json").read_text())
        assert enc.checked_context_schema(metadata, metadata["features"])
        assert metadata["strategy_context_training"]["rows"] == result["training_rows"]
    # A software meaning change invalidates an otherwise identical cohort.
    monkeypatch.setattr(advisory, "STRATEGY_SCHEMA_SHA256", "0" * 64)
    repeated = advisory.train_runner_advisory(root=tmp_path, frame=data)
    assert repeated["dataset_sha256"] != original_fingerprint
    assert advisory.rollback_runner_advisory(root=tmp_path)
    assert advisory.rollback_runner_advisory(root=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_actual_pre_buy_v4_source_is_original_and_survives_sql_failure_restart(tmp_path, monkeypatch, failure):
    from test_buy_recovery import (configure_paper, database, execution_tail_namespace, MINT)
    from runtime.buy_recovery import BuyRecoveryStore
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from sqlalchemy import select
    from db.models import Token, Position
    paper_runtime = configure_paper(monkeypatch, tmp_path)
    store = BuyRecoveryStore(tmp_path / "journal")
    engine, sessions = await database(tmp_path)
    namespace = execution_tail_namespace(tmp_path, store, paper_runtime)
    vector = current_vector(monkeypatch, 1, subprofile=ctx.VALUES[0], address=MINT)
    proof = sem.checked_row_receipt(sem.input_frame(vector).iloc[0].to_dict())
    observations = deepcopy(proof["auxiliary_observations"])
    namespace["vec"] = vector
    namespace["entry_observation"] = SimpleNamespace(social=json.dumps(observations.pop("social")),
                                                    auxiliary=json.dumps(observations))
    token = {"address": MINT, "symbol": "ORIGINAL", ctx.SOURCE: ctx.VALUES[0],
             "sniper_research_subprofile": ctx.VALUES[0]}
    try:
        async with sessions() as seed:
            seed.add(Token(address=MINT))
            await seed.commit()
        with store.scope():
            async with sessions() as session:
                if failure:
                    monkeypatch.setattr(session, "commit", AsyncMock(side_effect=RuntimeError("isolated SQL failure")))
                    with pytest.raises(RuntimeError, match="SQL failure"):
                        await namespace["execution_tail"](token, session)
                else:
                    await namespace["execution_tail"](token, session)
        restarted = BuyRecoveryStore(store.directory)
        async with sessions() as session:
            result = await restarted.recover(session, paper_portfolio=paper_runtime.load_portfolio())
            assert not result["failed"] and len(result["resolved"]) == int(failure)
            position = (await session.execute(select(Position))).scalar_one()
            assert position.buy_amount_sol == .1 and position.entry_subprofile == ctx.VALUES[0]
        journal = json.loads(next((store.directory / "resolved").glob("*.json")).read_text())
        assert journal["entry_features"]["version"] == ctx.ENTRY_VERSION
        assert journal["entry_features"]["vector"] == proof["vector"]
        assert journal["entry_features"]["strategy_context"] == proof["strategy_context"]
        assert journal["entry_features"]["captured_at"] == journal["created_at"]
        assert len(paper_runtime._PORTFOLIO) == 1
    finally:
        await engine.dispose()
