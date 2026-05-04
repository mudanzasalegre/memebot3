from __future__ import annotations

import numpy as np
import pytest
from sklearn.dummy import DummyClassifier
from types import SimpleNamespace

from ml.model_registry import promote_candidate, write_candidate


def test_model_registry_promotes_atomically(tmp_path, monkeypatch) -> None:
    import ml.model_registry as registry

    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "model_registry.json")
    monkeypatch.setattr(registry, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=False))
    model = DummyClassifier(strategy="constant", constant=1)
    model.fit(np.array([[0], [1]]), np.array([1, 1]))
    artifact = write_candidate(model=model, meta={"features": ["x"], "feature_set_hash": "abc"}, model_id="m1")
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
