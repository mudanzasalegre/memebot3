from __future__ import annotations

import os
from types import SimpleNamespace

import analytics.ai_predict as ai_predict


def _candidate(root, name: str) -> tuple[object, object]:
    path = root / "ml" / "models" / name
    path.mkdir(parents=True, exist_ok=True)
    model = path / "model.pkl"
    meta = path / "model.meta.json"
    model.write_bytes(b"model")
    meta.write_text('{"features": ["x"]}', encoding="utf-8")
    return model.resolve(), meta.resolve()


def test_candidate_fallback_uses_latest_non_family_candidate(monkeypatch, tmp_path) -> None:
    old_model, old_meta = _candidate(tmp_path, "20260513_100000_logreg_calibrated")
    new_model, new_meta = _candidate(tmp_path, "20260513_110000_logreg_calibrated")
    family = tmp_path / "ml" / "models" / "risk"
    family.mkdir(parents=True)
    (family / "model.pkl").write_bytes(b"ignored")
    (family / "model.meta.json").write_text("{}", encoding="utf-8")
    older_time = 1_700_000_000
    newer_time = older_time + 10
    old_model.touch()
    new_model.touch()
    os.utime(old_model, (older_time, older_time))
    os.utime(old_meta, (older_time, older_time))
    os.utime(new_model, (newer_time, newer_time))
    os.utime(new_meta, (newer_time, newer_time))

    monkeypatch.setattr(ai_predict, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(ai_predict, "_MODEL_PATH", (tmp_path / "ml" / "model.pkl").resolve())
    monkeypatch.setattr(ai_predict, "_META_PATH", (tmp_path / "ml" / "model.meta.json").resolve())
    monkeypatch.setattr(
        ai_predict,
        "CFG",
        SimpleNamespace(ML_SHADOW_CANDIDATE_MODEL_FALLBACK_ENABLED=True, ML_GATE_MODE="shadow"),
    )

    model_path, meta_path, fallback = ai_predict._effective_model_paths()

    assert fallback is True
    assert model_path == new_model
    assert meta_path == new_meta


def test_candidate_fallback_disabled_for_enforced_gate(monkeypatch, tmp_path) -> None:
    _candidate(tmp_path, "20260513_110000_logreg_calibrated")
    active_model = (tmp_path / "ml" / "model.pkl").resolve()
    active_meta = (tmp_path / "ml" / "model.meta.json").resolve()
    monkeypatch.setattr(ai_predict, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(ai_predict, "_MODEL_PATH", active_model)
    monkeypatch.setattr(ai_predict, "_META_PATH", active_meta)
    monkeypatch.setattr(
        ai_predict,
        "CFG",
        SimpleNamespace(ML_SHADOW_CANDIDATE_MODEL_FALLBACK_ENABLED=True, ML_GATE_MODE="enforce"),
    )

    model_path, meta_path, fallback = ai_predict._effective_model_paths()

    assert fallback is False
    assert model_path == active_model
    assert meta_path == active_meta
