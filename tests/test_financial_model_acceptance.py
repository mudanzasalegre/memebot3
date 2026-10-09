"""Isolated synthetic model evidence, explicitly not profitable forward trading."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.dummy import DummyClassifier

from ml.family_training import train_classifier_family, train_regressor_family
from ml.financial_targets import checked_financial_frame, supported_financial_training
from net_financial_fixtures import net_frame, mint_for


def frame(n=160):
    times = pd.date_range("2026-09-01", periods=n, freq="10min", tz="UTC")
    return pd.DataFrame({"address": [f"Mint{i}" for i in range(n)], "timestamp": times,
                         "ts": times + pd.Timedelta(minutes=2), "entry_regime": "pump_early",
                         "entry_lane": "pump_early_pumpswap_profit", "dex_id": "pumpswap",
                         "price_pct_5m": np.tile([5., 80.], n // 2),
                         "score_total": 70, "target_total_pnl_pct": np.tile([-60., 200.], n // 2),
                         "max_pnl_pct_seen": np.tile([2., 25000.], n // 2)})


def test_gross_rows_cannot_become_financial_validation_but_can_teach_runners(tmp_path):
    risk = train_classifier_family(family="risk", targets=["severe_loss_30"], feature_set_name="risk_features",
                                   frame=frame(), output_dir=tmp_path / "risk")
    assert risk["status"] == "skipped" and risk["financial_training"]["rows"] == 0
    ev = train_regressor_family(family="ev", targets=["ev_realized", "ev_peak_adjusted"],
                               feature_set_name="ev_features", frame=frame(), output_dir=tmp_path / "ev")
    assert ev["targets"]["ev_realized"]["status"] == "skipped"
    assert ev["targets"]["ev_peak_adjusted"]["status"] == "trained"
    runner = train_classifier_family(family="runner", targets=["runner_10000"], feature_set_name="runner_features",
                                     frame=frame(), output_dir=tmp_path / "runner")
    assert runner["targets"]["runner_10000"]["ranking_validation_ready"]
    assert not (tmp_path / "risk").exists()


def test_mixed_net_training_restores_original_inputs_and_derives_targets(tmp_path):
    original = net_frame(frame())
    mutable = original.copy()
    mutable["price_pct_5m"] = -99999
    mutable["severe_loss_configured"] = 0
    mutable["ev_configured_clipped"] = 99999
    mixed = pd.concat([mutable, frame(20)], ignore_index=True)
    restored, proof = checked_financial_frame(mixed)
    assert len(restored) == 160 and proof["unchecked_rows"] == 20
    assert restored.price_pct_5m.tolist() == original.price_pct_5m.tolist()
    assert restored.ts.equals(restored.outcome_closed_at)
    risk = train_classifier_family(family="risk", targets=["severe_loss_configured"], feature_set_name="risk_features",
                                   frame=mixed, output_dir=tmp_path / "risk",
                                   financial_target_parameters={"severe_loss_pct": -30})
    result = risk["targets"]["severe_loss_configured"]
    assert result["positives"] == 80 and result["probability_validation_ready"]
    assert supported_financial_training(result)
    ev = train_regressor_family(family="ev", targets=["ev_configured_clipped"], feature_set_name="ev_features",
                               frame=mixed, output_dir=tmp_path / "ev",
                               financial_target_parameters={"clip_min": -100, "clip_max": 300})
    result = ev["targets"]["ev_configured_clipped"]
    assert result["regression_validation_ready"] and result["mae"] < 1
    assert result["target_rows"] == 160 and supported_financial_training(result)


def test_identical_trade_copies_deduplicate_and_bad_copy_invalidates_identity():
    original = net_frame(frame(4))
    duplicated = pd.concat([original, original.iloc[:1]], ignore_index=True)
    restored, proof = checked_financial_frame(duplicated)
    assert len(restored) == 4 and proof["duplicates_removed"] == 1
    assert proof["population_sha256"] == checked_financial_frame(original)[1]["population_sha256"]
    duplicated.loc[4, "target_total_pnl_pct"] = 999
    restored, proof = checked_financial_frame(duplicated)
    assert len(restored) == 3 and proof["conflicting_trade_ids"] == [original.iloc[0].outcome_trade_id]
    assert not proof["ready"]


@pytest.mark.parametrize("field,value", [("version", "old"), ("return_basis", "gross"), ("scope", "live_profit"),
    ("rows", True), ("unique_trades", 0), ("ready", 1), ("population_sha256", "fake"),
    ("positive_pnl_ratios", [float("nan")]), ("conflicting_trade_ids", ["bad"])])
def test_financial_metadata_flags_cannot_override_broken_basis(field, value):
    proof = checked_financial_frame(net_frame(frame(4)))[1]
    assert supported_financial_training({"financial_training": proof}, entry=True)
    proof[field] = value
    assert not supported_financial_training({"financial_training": proof}, entry=True)


def test_primary_enforcement_needs_net_population_and_uniform_positive_threshold():
    from ml.train import _enforcement_gates
    from primary_probability_fixtures import primary_probability_parts
    quality = SimpleNamespace(passed=True, holdout_rows=100, holdout_positives=50)
    tune = {"activation_ready": True, "objective_applied": "expected_pnl_precision_floor", "precision_at_picked": 1.,
            "avg_realized_pnl_pct_at_picked": 100., "realized_selected_rows_at_picked": 100}
    assert not _enforcement_gates(quality, tune)["activation_ready"]
    proof = checked_financial_frame(net_frame(frame(4)))[1]
    split = {"label_availability_purged": True}
    probability = primary_probability_parts()[1]
    assert _enforcement_gates(quality, tune, proof, split, probability)["activation_ready"]
    proof["positive_pnl_ratios"] = [0., .1]
    assert not _enforcement_gates(quality, tune, proof, split, probability)["activation_ready"]


def setup_entry(root, monkeypatch, *, proof=True):
    from analytics import ai_predict as runtime
    from ml import model_registry as registry
    monkeypatch.setattr(registry, "MODELS_DIR", root / "models")
    monkeypatch.setattr(registry, "REGISTRY_PATH", root / "registry.json")
    monkeypatch.setattr(registry, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=False))
    from primary_probability_fixtures import primary_probability_parts
    model, probability = primary_probability_parts()
    metadata = {**probability, "features": ["price_pct_5m"], "activation_ready": True,
                "validation_split": {"label_availability_purged": True}}
    if proof:
        metadata["financial_training"] = checked_financial_frame(net_frame(frame(4)))[1]
    artifact = registry.write_candidate(model=model, meta=metadata, model_id="isolated")
    monkeypatch.setattr(runtime, "_MODEL_PATH", artifact.model_path)
    monkeypatch.setattr(runtime, "_META_PATH", artifact.meta_path)
    monkeypatch.setattr(runtime, "_TRAIN_STATUS_PATH", root / "absent.json")
    monkeypatch.setattr(runtime, "_REGISTRY_PATH", registry.REGISTRY_PATH)
    monkeypatch.setattr(runtime, "_MODELS_DIR", registry.MODELS_DIR)
    monkeypatch.setattr(runtime, "_model_signature", None)
    return runtime, registry, artifact


def test_legacy_entry_model_is_unknown_not_zero_and_cannot_activate(tmp_path, monkeypatch):
    runtime, registry, artifact = setup_entry(tmp_path, monkeypatch, proof=False)
    assert runtime.should_buy({"price_pct_5m": 1}) is None
    status = runtime.model_runtime_status()
    assert not status["model_loaded"] and not status["activation_ready"]
    with pytest.raises(RuntimeError, match="checked net"):
        registry.promote_candidate(artifact, active_model_path=tmp_path / "active.pkl")
    assert not (tmp_path / "active.pkl").exists() and not registry.REGISTRY_PATH.exists()


def test_entry_model_meta_only_change_invalidates_loaded_snapshot(tmp_path, monkeypatch):
    runtime, registry, artifact = setup_entry(tmp_path, monkeypatch)
    assert runtime.should_buy({"price_pct_5m": 1}) == .5
    assert runtime.model_runtime_status()["activation_ready"]
    metadata = json.loads(artifact.meta_path.read_text())
    metadata["financial_training"]["return_basis"] = "gross"
    artifact.meta_path.write_text(json.dumps(metadata))
    assert runtime.should_buy({"price_pct_5m": 1}) is None
    assert not runtime.model_runtime_status()["activation_ready"]


def test_model_checksum_mismatch_cannot_promote_or_predict(tmp_path, monkeypatch):
    runtime, registry, artifact = setup_entry(tmp_path, monkeypatch)
    assert runtime.should_buy({}) == .5
    artifact.model_path.write_bytes(b"corrupted")
    assert runtime.should_buy({}) is None
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        registry.promote_candidate(artifact, active_model_path=tmp_path / "active.pkl")
    assert not (tmp_path / "active.pkl").exists()


def test_financial_runtime_rejects_legacy_even_with_otherwise_valid_oos(tmp_path, monkeypatch):
    from analytics import model_runtime_common as runtime
    report = train_regressor_family(family="ev", targets=["ev_realized"], feature_set_name="ev_features",
                                   frame=net_frame(frame()), output_dir=tmp_path / "ml" / "models" / "ev")
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    assert runtime.predict_model("ev", "ev_realized", {"price_pct_5m": 80}) == pytest.approx(200)
    path = report["targets"]["ev_realized"]["model_path"]
    from pathlib import Path
    meta_path = Path(path).with_suffix(".meta.json")
    metadata = json.loads(meta_path.read_text())
    metadata.pop("financial_training")
    meta_path.write_text(json.dumps(metadata))
    assert runtime.predict_model("ev", "ev_realized", {"price_pct_5m": 80}) is None
    assert runtime.predict_artifact(Path(path), {}, require_temporal_validation=False) is None


def test_configured_financial_targets_require_explicit_definition(tmp_path):
    data = net_frame(frame(4))
    data["severe_loss_configured"] = 0
    with pytest.raises(ValueError, match="explicit_net_target"):
        train_classifier_family(family="risk", targets=["severe_loss_configured"], feature_set_name="risk_features",
                                frame=data, output_dir=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("proba,ready", [(None, True), (.1, False), (float("nan"), True)])
def test_stale_threshold_cannot_turn_unknown_net_model_into_blanket_rejection(tmp_path, monkeypatch, proba, ready):
    from analytics import ml_policy as policy
    path = tmp_path / "threshold.json"
    path.write_text(json.dumps({"picked": .5, "activation_ready": True}))
    monkeypatch.setattr(policy, "THRESHOLDS_BY_LANE_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(policy, "LEGACY_THRESHOLD_PATH", path)
    monkeypatch.setattr(policy, "CFG", SimpleNamespace(ML_GATE_MODE="enforce", ML_SIZING_ENABLED=True))
    result = policy.decide_ml_action(token={"entry_lane": "pump_early_pumpswap_profit"}, feature_row={},
                                    proba=proba, base_rules_passed=True, dry_run=True, live=False,
                                    entry_model_activation_ready=ready)
    assert result.allow_buy and not result.enforce and not result.activation_ready
    if proba is None:
        assert result.sizing_multiplier == 1


def productive_context(monkeypatch, source):
    from ml import train as trainer
    monkeypatch.setattr(trainer, "_productive_regime_mask", lambda data: pd.Series(True, index=data.index))
    monkeypatch.setattr(trainer, "_productive_lane_mask", lambda data, **kw: (pd.Series(True, index=data.index), {}))
    monkeypatch.setattr(trainer, "_productive_dex_mask", lambda data, **kw: (pd.Series(True, index=data.index), {}))
    monkeypatch.setattr(trainer, "HOLDOUT_DAYS", None)
    monkeypatch.setattr(trainer, "HOLDOUT_PCT", .25)
    return trainer._build_training_context(source, training_scope="isolated_test")


def test_actual_primary_training_context_is_net_only_and_uses_frozen_t0(monkeypatch):
    from features.builder import ALLOWED_FEATURES
    original = net_frame(frame(20))
    mutable = original.copy()
    mutable["price_pct_5m"] = 999999
    mutable["is_winner"] = 1  # Must not become a predictor through numeric selection.
    source = pd.concat([mutable, frame(20)], ignore_index=True)
    context = productive_context(monkeypatch, source)
    trained = context["df_trainable"]
    assert len(trained) == 20 and trained.price_pct_5m.tolist() == original.price_pct_5m.tolist()
    assert set(context["x_cols"]) <= ALLOWED_FEATURES and "is_winner" not in context["x_cols"]
    assert context["filtering_meta"]["financial_filtering"]["unchecked_rows"] == 20


def test_invalid_declared_primary_population_never_backfills_gross_and_skips_cleanly(monkeypatch):
    original = net_frame(frame(4))
    broken = original.copy()
    broken["target_total_pnl_pct"] = 999999
    # Valid and contradictory copies plus legacy rows cannot resurrect a model.
    source = pd.concat([original, broken, frame(4)], ignore_index=True)
    context = productive_context(monkeypatch, source)
    assert context["df_trainable"].empty and not context["quality"].passed
    assert context["split_meta"]["mode"] == "empty_trainable_population"
    assert len(context["filtering_meta"]["financial_filtering"]["conflicting_trade_ids"]) == 4


def test_primary_forward_holdout_purges_unclosed_training_labels(monkeypatch):
    from ml import train as trainer
    raw = frame(20)
    raw.loc[0, "ts"] = raw.loc[19, "timestamp"]
    data = net_frame(raw)
    monkeypatch.setattr(trainer, "HOLDOUT_DAYS", None)
    monkeypatch.setattr(trainer, "HOLDOUT_PCT", .25)
    train, test, metadata = trainer._forward_holdout_split(data.assign(mint=data.address))
    assert metadata["label_availability_purged"] and mint_for("Mint0") not in train.address.tolist()
    assert train.outcome_closed_at.max() < test.timestamp.min() - pd.Timedelta(seconds=60)
    assert not set(train.address) & set(test.address)


def test_primary_walk_forward_purges_future_reentries_and_unclosed_labels():
    from ml import train as trainer
    raw = frame(20)
    raw.loc[0, "ts"] = raw.loc[19, "timestamp"]
    raw.loc[16, "address"] = raw.loc[1, "address"]
    data = net_frame(raw)
    windows, metadata = trainer._build_walk_forward_scheme(data.assign(mint=data.address))
    assert windows and metadata["label_availability_purged"]
    for train, test in windows:
        assert 0 not in train
        assert data.iloc[train].outcome_closed_at.max() < data.iloc[test].timestamp.min() - pd.Timedelta(seconds=60)
        assert not set(data.iloc[train].address) & set(data.iloc[test].address)


def test_entry_artifact_without_temporal_purging_cannot_predict_or_promote(tmp_path, monkeypatch):
    runtime, registry, artifact = setup_entry(tmp_path, monkeypatch)
    metadata = json.loads(artifact.meta_path.read_text())
    metadata.pop("validation_split")
    artifact.meta_path.write_text(json.dumps(metadata))
    assert runtime.should_buy({}) is None
    with pytest.raises(RuntimeError, match="purged financial label availability"):
        registry.promote_candidate(artifact, active_model_path=tmp_path / "active.pkl")
    assert not (tmp_path / "active.pkl").exists()


def test_runtime_status_cannot_mutate_cached_financial_proof(tmp_path, monkeypatch):
    runtime, registry, artifact = setup_entry(tmp_path, monkeypatch)
    status = runtime.model_runtime_status()
    status["financial_training"]["return_basis"] = "gross"
    assert runtime.should_buy({}) == .5
    assert runtime.model_runtime_status()["financial_training_ready"]
