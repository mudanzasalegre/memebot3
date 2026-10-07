from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from config.config import resolve_project_path

try:
    import ml.train as ml_train
    import ml.training_daemon as training_daemon
except BaseException as exc:  # pragma: no cover - environment-specific dependency gate
    pytest.skip(f"ml stack unavailable: {exc}", allow_module_level=True)


def _patch_train_paths(monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Path, Path]:
    features = root / "data" / "features"
    metrics = root / "data" / "metrics"
    model = root / "ml" / "model.pkl"
    features.mkdir(parents=True, exist_ok=True)
    metrics.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ml_train, "DATA_DIR", features)
    monkeypatch.setattr(ml_train, "METRICS_DIR", metrics)
    monkeypatch.setattr(ml_train, "MODEL_PATH", model)
    monkeypatch.setattr(ml_train, "META_PATH", model.with_suffix(".meta.json"))
    monkeypatch.setattr(ml_train, "VAL_PREDS_CSV", metrics / "val_preds.csv")
    monkeypatch.setattr(ml_train, "RECOMMENDED_JSON", metrics / "recommended_threshold.json")
    monkeypatch.setattr(ml_train, "DATASET_QUALITY_JSON", metrics / "dataset_quality.json")
    monkeypatch.setattr(ml_train, "TRAIN_STATUS_JSON", metrics / "train_status.json")
    return features, metrics


def test_config_resolves_relative_paths_against_project_root(tmp_path: Path) -> None:
    base = tmp_path / "repo"
    absolute = tmp_path / "external" / "features"

    assert resolve_project_path("data/features", base=base) == (base / "data" / "features").resolve()
    assert resolve_project_path(absolute, base=base) == absolute.resolve()


def test_missing_dataset_writes_actionable_train_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    features, metrics = _patch_train_paths(monkeypatch, tmp_path)

    result = ml_train.train_and_save()

    status_path = metrics / "train_status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert result.trained is False
    assert result.status == "missing_dataset"
    assert payload["status"] == "missing_dataset"
    assert payload["feature_dataset"]["features_dir"] == str(features.resolve())
    assert payload["feature_dataset"]["exists"] is True
    assert payload["feature_dataset"]["file_count"] == 0
    assert "Generate features_" in payload["action"]
    assert "FEATURES_DIR" in payload["error"]


def test_feature_dataset_snapshot_reports_stale_files(tmp_path: Path) -> None:
    features = tmp_path / "data" / "features"
    features.mkdir(parents=True)
    feature_file = features / "features_fixture.csv"
    feature_file.write_text("label,x\n1,2\n", encoding="utf-8")
    old = time.time() - 3 * 3600
    os.utime(feature_file, (old, old))

    snapshot = ml_train.feature_dataset_snapshot(features, max_age_hours=1)

    assert snapshot["usable"] is True
    assert snapshot["file_count"] == 1
    assert snapshot["stale"] is True
    assert snapshot["fresh"] is False


def test_training_daemon_lock_status_keeps_feature_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    status_path = tmp_path / "data" / "metrics" / "train_status.json"
    monkeypatch.setattr(training_daemon, "STATUS_PATH", status_path)
    monkeypatch.setattr(training_daemon, "acquire_lock", lambda ttl_s: False)
    monkeypatch.setattr(training_daemon, "feature_dataset_snapshot", lambda: {"usable": True, "fresh": True})

    updated = training_daemon.train_once()

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert updated is False
    assert payload["status"] == "locked"
    assert payload["daemon_status"] == "locked"
    assert payload["feature_dataset"]["fresh"] is True


def test_training_daemon_preserves_train_status_when_not_promoted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "data" / "metrics" / "train_status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps({"status": "missing_dataset", "last_train_status": "missing_dataset"}),
        encoding="utf-8",
    )
    released: list[bool] = []
    monkeypatch.setattr(training_daemon, "STATUS_PATH", status_path)
    monkeypatch.setattr(training_daemon, "acquire_lock", lambda ttl_s: True)
    monkeypatch.setattr(training_daemon, "release_lock", lambda: released.append(True))
    monkeypatch.setattr(training_daemon, "retrain_if_better", lambda: False)
    monkeypatch.setattr(training_daemon, "feature_dataset_snapshot", lambda: {"usable": False, "fresh": False})

    updated = training_daemon.train_once()

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert updated is False
    assert released == [True]
    assert payload["status"] == "missing_dataset"
    assert payload["last_train_status"] == "missing_dataset"
    assert payload["daemon_status"] == "not_promoted"
    assert payload["updated"] is False
