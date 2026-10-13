"""Native feature-loading T0 must never be invented from another event clock."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from ml import train
from ml.label_builder import attach_labels
from ml.temporal_validation import temporal_eligibility, purged_temporal_windows
from tools.audit_runner_population import profile_runner_population

T0 = "2026-08-01T12:00:00+00:00"
CLOSE = "2026-08-02T12:00:00+00:00"
AS_OF = "2026-10-11T08:00:00+00:00"


def row(**extra):
    return {"mint": "synthetic-t0-token", "address": "synthetic-t0-token",
        "label": 1, "sample_type": "shadow_close", "liquidity_usd": 50000.,
        "target_total_pnl_pct": 1000., "max_pnl_pct_seen": 20000.,
        "outcome_closed_at": CLOSE, **extra}


def write_frame(path, frame, kind):
    if kind == "parquet":
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, index=False)


@pytest.mark.parametrize("kind", ["parquet", "csv"])
@pytest.mark.parametrize("other", [
    pytest.param({}, id="missing"),
    pytest.param({"ts": CLOSE}, id="close"),
    pytest.param({"created_at": "2026-07-31T12:00:00+00:00"}, id="birth"),
    pytest.param({"listed_at": T0}, id="venue"),
    pytest.param({"ts_utc": T0}, id="other-event"),
])
def test_absent_original_t0_is_not_replaced(tmp_path, monkeypatch, kind, other):
    write_frame(tmp_path / f"features_202608.{kind}", pd.DataFrame([row(**other)]), kind)
    monkeypatch.setattr(train, "DATA_DIR", tmp_path)
    loaded = train._load_dataset()
    assert len(loaded) == 1
    assert loaded.timestamp.isna().all()
    valid, *_ = temporal_eligibility(loaded, as_of=AS_OF)
    assert not valid.any()
    labelled = attach_labels(loaded)
    assert labelled.label.tolist() == [1]
    assert labelled.runner_10000.tolist() == [1]
    assert labelled.liquidity_usd.tolist() == [50000.]
    profile = profile_runner_population(labelled, as_of=AS_OF)
    assert profile["settled_rows"] == 0
    assert profile["target_counts"]["runner_10000"]["positive_tokens"] == 0


@pytest.mark.parametrize("kind", ["parquet", "csv"])
@pytest.mark.parametrize("bad", [None, "", "invalid"])
def test_invalid_explicit_t0_does_not_use_valid_alias(tmp_path, monkeypatch, kind, bad):
    frame = pd.DataFrame([row(timestamp=bad, ts=CLOSE, ts_utc=T0, created_at=T0, listed_at=T0)])
    write_frame(tmp_path / f"features_202608.{kind}", frame, kind)
    monkeypatch.setattr(train, "DATA_DIR", tmp_path)
    loaded = train._load_dataset()
    assert loaded.timestamp.isna().all()
    assert not temporal_eligibility(loaded, as_of=AS_OF)[0].any()


@pytest.mark.parametrize("kind", ["parquet", "csv"])
def test_valid_original_t0_wins_over_all_other_clocks(tmp_path, monkeypatch, kind):
    frame = pd.DataFrame([row(timestamp="2026-08-01T14:00:00+02:00", ts=CLOSE,
        ts_utc=CLOSE, created_at="2026-07-01T00:00:00Z", listed_at="2026-08-01T11:00:00Z")])
    write_frame(tmp_path / f"features_202608.{kind}", frame, kind)
    monkeypatch.setattr(train, "DATA_DIR", tmp_path)
    loaded = train._load_dataset()
    assert loaded.timestamp.iloc[0] == pd.Timestamp(T0)
    assert temporal_eligibility(loaded, as_of=AS_OF)[0].tolist() == [True]
    assert loaded.ts.iloc[0] == CLOSE


@pytest.mark.parametrize("kind", ["parquet", "csv"])
def test_mixed_partitions_keep_unknown_t0_without_contaminating_valid_peer(tmp_path, monkeypatch, kind):
    write_frame(tmp_path / f"features_202608.{kind}", pd.DataFrame([row(timestamp=T0)]), kind)
    write_frame(tmp_path / f"features_202609.{kind}", pd.DataFrame([row(mint="unknown",
        address="unknown", ts=CLOSE, ts_utc=T0, created_at=T0)]), kind)
    monkeypatch.setattr(train, "DATA_DIR", tmp_path)
    loaded = train._load_dataset()
    assert loaded.timestamp.notna().tolist() == [True, False]
    labels = attach_labels(loaded)
    assert labels.runner_10000.tolist() == [1, 1]
    assert temporal_eligibility(labels, as_of=AS_OF)[0].tolist() == [True, False]
    from ml.family_training import _settled_training_frame
    assert _settled_training_frame(labels).mint.tolist() == ["synthetic-t0-token"]


@pytest.mark.parametrize("known", [True, False])
def test_actual_feature_store_persistence_clock_is_not_t0(tmp_path, monkeypatch, known):
    from features import store
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "_ROW_COUNT", 0)
    monkeypatch.setattr(train, "DATA_DIR", tmp_path)
    vector = {"address": "synthetic-t0-token", "liquidity_usd": 50000.}
    if known:
        vector["timestamp"] = dt.datetime.fromisoformat(T0)
    assert store.append(vector, 1, sample_type="shadow_close", target_total_pnl_pct=1000.,
        outcome_targets={"max_pnl_pct_seen": 20000., "outcome_closed_at": dt.datetime.fromisoformat(CLOSE)},
        strict=True, partition_at=dt.datetime.fromisoformat(T0))
    loaded = train._load_dataset()
    assert loaded.ts.notna().all()  # Actual native writer assigned persistence time.
    assert bool(loaded.timestamp.notna().iloc[0]) is known
    assert temporal_eligibility(loaded, as_of=AS_OF)[0].tolist() == [known]
    assert attach_labels(loaded).runner_10000.tolist() == [1]


def test_unknown_partition_cannot_enter_native_temporal_folds(tmp_path, monkeypatch):
    times = pd.date_range("2026-08-01", periods=12, freq="D", tz="UTC")
    good = pd.DataFrame([row(mint=f"good-{i}", address=f"good-{i}",
        timestamp=stamp.isoformat(), outcome_closed_at=(stamp + pd.Timedelta(minutes=1)).isoformat())
        for i, stamp in enumerate(times)])
    bad = pd.DataFrame([row(mint=f"unknown-{i}", address=f"unknown-{i}", created_at=T0,
        ts=CLOSE) for i in range(4)])
    good.to_parquet(tmp_path / "features_202608.parquet", index=False)
    bad.to_parquet(tmp_path / "features_202609.parquet", index=False)
    monkeypatch.setattr(train, "DATA_DIR", tmp_path)
    loaded = train._load_dataset()
    windows, metadata = purged_temporal_windows(loaded, min_train_rows=1, as_of=AS_OF)
    assert windows and metadata["eligible_rows"] == 12
    assert metadata["excluded_missing_or_invalid_timing_or_identity"] == 4
    for train_rows, test_rows in windows:
        assert all(loaded.iloc[index].mint.startswith("good-") for index in [*train_rows, *test_rows])


def test_empty_frame_has_empty_unknown_utc_t0():
    frame = train._coerce_timestamp(pd.DataFrame(index=pd.Index([], name="row")))
    assert frame.empty and str(frame.timestamp.dtype) == "datetime64[ns, UTC]"


@pytest.mark.parametrize("limit", [None, pd.NaT])
def test_nat_event_clock_remains_unknown(limit):
    from analytics.token_time import parse_event_clock
    assert parse_event_clock(pd.NaT, now=limit) is None
    if limit is not None:
        assert parse_event_clock(T0, now=limit) is None


def test_historical_unknown_t0_does_not_crash_or_rejuvenate_birth():
    from analytics.token_time import historical_age_snapshot
    original = {"timestamp": pd.NaT, "created_at": T0, "first_seen_at": pd.NaT}
    view = historical_age_snapshot(original)
    assert view["age_minutes"] is None and view["queue_age_minutes"] is None
    assert original["timestamp"] is pd.NaT and original["created_at"] == T0
