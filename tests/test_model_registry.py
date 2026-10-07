from __future__ import annotations

import json

import numpy as np
import pytest
from sklearn.dummy import DummyClassifier
from types import SimpleNamespace

from ml.model_registry import promote_candidate, promote_family_candidate, write_candidate
from ml.financial_targets import TRAINING_VERSION, TRAINING_SCOPE, VERSION


def financial_meta():
    return {"version": TRAINING_VERSION, "return_basis": VERSION, "scope": TRAINING_SCOPE,
            "ready": True, "rows": 2, "unique_trades": 2, "population_sha256": "a" * 64,
            "conflicting_trade_ids": [], "positive_pnl_ratios": [0.]}


def test_model_registry_promotes_atomically(tmp_path, monkeypatch) -> None:
    import ml.model_registry as registry

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "model_registry.json")
    monkeypatch.setattr(registry, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=False))
    from primary_probability_fixtures import primary_probability_parts
    model, probability = primary_probability_parts()
    artifact = write_candidate(
        model=model,
        meta={**probability, "features": ["price_pct_5m"], "feature_set_hash": "abc", "activation_ready": True,
              "financial_training": financial_meta(), "validation_split": {"label_availability_purged": True}},
        model_id="m1",
    )
    active = tmp_path / "model.pkl"
    reg = promote_candidate(artifact, active_model_path=active)
    assert active.exists()
    assert active.with_suffix(".meta.json").exists()
    assert reg["active_model_id"] == "m1"


def test_model_registry_blocks_promotion_when_optimization_lock_active(tmp_path, monkeypatch) -> None:
    import ml.model_registry as registry

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "model_registry.json")
    monkeypatch.setattr(registry, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=False))
    model = DummyClassifier(strategy="constant", constant=1)
    model.fit(np.array([[0], [1]]), np.array([1, 1]))
    artifact = write_candidate(model=model, meta={"features": ["x"], "feature_set_hash": "abc"}, model_id="m1")

    monkeypatch.setattr(registry, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=True))
    with pytest.raises(RuntimeError, match="STRATEGY_OPTIMIZATION_LOCK=true blocks model promotion"):
        promote_candidate(artifact, active_model_path=tmp_path / "model.pkl")


def test_candidate_thresholds_publish_only_after_promotion(tmp_path, monkeypatch) -> None:
    import ml.model_registry as registry

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "model_registry.json")
    monkeypatch.setattr(registry, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(registry, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=False))

    metrics_dir = tmp_path / "data" / "metrics"
    metrics_dir.mkdir(parents=True)
    recommended_path = metrics_dir / "recommended_threshold.json"
    lanes_path = metrics_dir / "recommended_thresholds.by_lane.json"
    recommended_path.write_text(json.dumps({"picked": 0.41}), encoding="utf-8")
    lanes_path.write_text(json.dumps({"global": {"threshold": 0.41}}), encoding="utf-8")

    from primary_probability_fixtures import primary_probability_parts
    model, probability = primary_probability_parts()
    new_threshold = {"picked": 0.73, "activation_ready": True}
    new_lane_thresholds = {"global": {"threshold": 0.73}, "by_lane": {}}
    artifact = write_candidate(
        model=model,
        meta={
            **probability,
            "features": ["price_pct_5m"],
            "feature_set_hash": "abc",
            "activation_ready": True,
            "financial_training": financial_meta(),
            "validation_split": {"label_availability_purged": True},
            "threshold_result": new_threshold,
        },
        thresholds=new_lane_thresholds,
        model_id="m1",
    )

    assert json.loads(recommended_path.read_text(encoding="utf-8"))["picked"] == 0.41
    assert json.loads(lanes_path.read_text(encoding="utf-8"))["global"]["threshold"] == 0.41

    promote_candidate(artifact, active_model_path=tmp_path / "model.pkl")

    assert json.loads(recommended_path.read_text(encoding="utf-8")) == new_threshold
    assert json.loads(lanes_path.read_text(encoding="utf-8")) == new_lane_thresholds


@pytest.mark.parametrize("activation_ready", [False, None, "true", 1])
def test_model_registry_requires_literal_activation_ready_true(tmp_path, monkeypatch, activation_ready) -> None:
    import ml.model_registry as registry

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "model_registry.json")
    monkeypatch.setattr(registry, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(registry, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=False))

    model = DummyClassifier(strategy="constant", constant=1)
    model.fit(np.array([[0], [1]]), np.array([1, 1]))
    artifact = write_candidate(
        model=model,
        meta={"features": ["x"], "feature_set_hash": "abc", "activation_ready": activation_ready},
        model_id="not-ready",
    )
    active = tmp_path / "model.pkl"

    with pytest.raises(RuntimeError, match="activation_ready=true required for model promotion"):
        promote_candidate(artifact, active_model_path=active)

    assert not active.exists()
    assert not active.with_suffix(".meta.json").exists()
    assert not registry.REGISTRY_PATH.exists()


def test_family_model_promotion_also_requires_activation_ready(tmp_path, monkeypatch) -> None:
    import ml.model_registry as registry

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "model_registry.json")
    monkeypatch.setattr(registry, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=False))

    model = DummyClassifier(strategy="constant", constant=1)
    model.fit(np.array([[0], [1]]), np.array([1, 1]))
    artifact = write_candidate(
        model=model,
        meta={"features": ["x"], "feature_set_hash": "abc", "activation_ready": False},
        model_id="family-not-ready",
        family="risk",
    )

    with pytest.raises(RuntimeError, match="activation_ready=true required for model promotion"):
        promote_family_candidate(artifact, family="risk")

    assert not (tmp_path / "models" / "risk" / "active_model.pkl").exists()
