"""Mixed-format partitions must reach native learning and its diagnostics."""
from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest

from ml import train, segment_report
from tools import preflight
from ml.feature_sources import feature_partition_files


def write_partition(path: Path, mint: str, timestamp: str | None) -> None:
    frame = pd.DataFrame([{"address": mint, "timestamp": timestamp,
        "label": 1, "sample_type": "shadow_close", "liquidity_usd": 50000.,
        "target_total_pnl_pct": 1000.}])
    if path.suffix == ".parquet":
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, index=False)


@pytest.mark.parametrize("consumer", ["files", "training", "snapshot", "segments", "preflight"])
def test_distinct_csv_partition_survives_existing_parquet(tmp_path, monkeypatch, consumer):
    old = tmp_path / "features_202608.parquet"
    fresh = tmp_path / "features_202609.csv"
    write_partition(old, "old", "2026-08-01T00:00:00Z")
    write_partition(fresh, "fresh", "2026-09-01T00:00:00Z")
    os.utime(old, (1, 1))
    monkeypatch.setattr(train, "DATA_DIR", tmp_path)
    monkeypatch.setenv("FEATURES_DIR", str(tmp_path))
    if consumer == "files":
        assert train._feature_files() == [old, fresh]
    elif consumer == "training":
        assert train._load_dataset().mint.tolist() == ["old", "fresh"]
    elif consumer == "snapshot":
        result = train.feature_dataset_snapshot(max_age_hours=1)
        assert result["file_count"] == 2 and result["latest_file"] == str(fresh)
        assert result["fresh"] and not result["stale"]
    elif consumer == "segments":
        assert segment_report.load_feature_history(tmp_path).address.tolist() == ["old", "fresh"]
    else:
        result = preflight._feature_dataset_status()
        assert result["file_count"] == 2 and result["latest_file"] == str(fresh)


def test_same_partition_csv_replica_does_not_duplicate_or_override_parquet(tmp_path, monkeypatch):
    authoritative = tmp_path / "features_202608.parquet"
    replica = authoritative.with_suffix(".csv")
    write_partition(authoritative, "parquet", "2026-08-01T00:00:00Z")
    write_partition(replica, "csv-replica", "2026-08-02T00:00:00Z")
    monkeypatch.setattr(train, "DATA_DIR", tmp_path)
    assert train._feature_files() == [authoritative]
    assert train._load_dataset().mint.tolist() == ["parquet"]
    assert segment_report.load_feature_history(tmp_path).address.tolist() == ["parquet"]


def test_mixed_format_unknown_t0_is_not_repaired_from_partition_name(tmp_path, monkeypatch):
    write_partition(tmp_path / "features_202608.parquet", "known", "2026-08-01T00:00:00Z")
    write_partition(tmp_path / "features_202609.csv", "unknown", None)
    monkeypatch.setattr(train, "DATA_DIR", tmp_path)
    loaded = train._load_dataset()
    assert loaded.mint.tolist() == ["known", "unknown"]
    assert loaded.timestamp.notna().tolist() == [True, False]


def test_selected_corrupt_parquet_cannot_silently_fall_back_to_replica(tmp_path, monkeypatch):
    parquet = tmp_path / "features_202608.parquet"
    parquet.write_bytes(b"not parquet")
    write_partition(parquet.with_suffix(".csv"), "unapproved-fallback", "2026-08-01T00:00:00Z")
    monkeypatch.setattr(train, "DATA_DIR", tmp_path)
    with pytest.raises(pa.ArrowInvalid):
        train._load_dataset()


@pytest.mark.parametrize("formats", [("csv",), ("parquet",), ("parquet", "csv"), ("csv", "parquet")])
def test_deterministic_partition_order_and_no_source_mutation(tmp_path, formats):
    for index, kind in enumerate(formats):
        write_partition(tmp_path / f"features_20260{8 + index}.{kind}", f"mint-{index}", None)
    ignored = tmp_path / "features_directory.parquet"
    ignored.mkdir()
    (tmp_path / "not-features.csv").write_text("not a feature source", encoding="utf-8")
    before = {p.name: sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir() if p.is_file()}
    selected = feature_partition_files(str(tmp_path))
    assert [p.name for p in selected] == [f"features_20260{8 + i}.{kind}" for i, kind in enumerate(formats)]
    assert before == {p.name: sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir() if p.is_file()}


def test_missing_and_empty_directories_have_no_selected_sources(tmp_path):
    assert feature_partition_files(tmp_path) == []
    assert feature_partition_files(tmp_path / "absent") == []


def test_runner_population_source_receipt_covers_selected_csv_not_replica(tmp_path):
    from tools.audit_runner_population import runner_population_sources
    directory = tmp_path / "data/features"
    directory.mkdir(parents=True)
    write_partition(directory / "features_202608.parquet", "old", None)
    write_partition(directory / "features_202608.csv", "replica", None)
    write_partition(directory / "features_202609.csv", "fresh", None)
    sources = runner_population_sources(tmp_path)
    assert [item["file"] for item in sources] == ["data/features/features_202608.parquet", "data/features/features_202609.csv"]
    for item in sources:
        assert item["sha256"] == sha256((tmp_path / item["file"]).read_bytes()).hexdigest()


def test_learning_audit_source_receipt_covers_csv_without_fitting(tmp_path, monkeypatch):
    from tools import audit_runner_learning as audit
    directory = tmp_path / "data/features"
    directory.mkdir(parents=True)
    write_partition(directory / "features_202608.parquet", "old", None)
    write_partition(directory / "features_202609.csv", "fresh", None)
    frame = pd.DataFrame({"timestamp": [pd.Timestamp("2026-08-01", tz="UTC")],
        **{f"runner_{t}": [0] for t in audit.RUNNER_THRESHOLDS}})
    monkeypatch.setattr(audit, "load_training_frame", lambda: frame)
    monkeypatch.setattr(audit, "train_classifier_family", lambda **kwargs: {"targets": {}})
    monkeypatch.setattr(audit, "train_regressor_family", lambda **kwargs: {"targets": {}})
    result = audit.audit_runner_learning(tmp_path, output_dir=tmp_path / "isolated-artifacts")
    assert [item["file"] for item in result["sources"]] == ["data/features/features_202608.parquet", "data/features/features_202609.csv"]
    assert not result["promotion_attempted"] and not result["bot_started"]
    assert not (tmp_path / "isolated-artifacts").exists()
