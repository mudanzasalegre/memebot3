from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from ml.calibrated_ranker import fit_calibrated_ranker
from ml.family_training import train_classifier_family
from runtime.hot_queue import HotQueue


def _training_frame(n=120):
    times = pd.date_range("2026-09-01", periods=n, freq="10min", tz="UTC")
    return pd.DataFrame({"address": [f"token{i}" for i in range(n)], "timestamp": times,
                         "ts": times + pd.Timedelta(minutes=2), "entry_lane": ["a"] * n,
                         "price_pct_5m": [50 if i % 2 else -10 for i in range(n)],
                         "max_pnl_pct_seen": [1500 if i % 2 else 2 for i in range(n)],
                         "target_total_pnl_pct": [100 if i % 2 else -10 for i in range(n)]})


def test_calibration_does_not_fit_or_observe_future_window():
    frame = _training_frame()
    X = frame[["price_pct_5m"]]
    y = (frame.max_pnl_pct_seen >= 100).astype(int)
    model, calibration = fit_calibrated_ranker(make_pipeline(StandardScaler(), LogisticRegression(class_weight="balanced")), X, y, frame)
    assert calibration["calibrated"] is True
    assert calibration["fit_rows"] < len(frame)
    last = calibration["timing"]["folds"][-1]
    assert pd.Timestamp(last["train_label_latest"]) < pd.Timestamp(last["test_start"])
    assert np.allclose(model.predict_proba(X).sum(axis=1), 1)


def test_sparse_classes_remain_rank_only_not_fake_probability():
    frame = _training_frame()
    frame["max_pnl_pct_seen"] = 0
    frame.loc[[0, 20, 80, 110], "max_pnl_pct_seen"] = 1500
    y = (frame.max_pnl_pct_seen >= 1000).astype(int)
    model, calibration = fit_calibrated_ranker(make_pipeline(StandardScaler(), LogisticRegression(class_weight="balanced")), frame[["price_pct_5m"]], y, frame)
    assert not calibration["calibrated"]
    assert len(model.rank_score(frame[["price_pct_5m"]])) == len(frame)
    with pytest.raises(ValueError, match="Uncalibrated"):
        model.predict_proba(frame[["price_pct_5m"]])


def test_probability_runtime_requires_calibration_and_out_of_sample_skill(tmp_path, monkeypatch):
    import analytics.model_runtime_common as runtime
    path = tmp_path / "ml" / "models" / "runner"
    report = train_classifier_family(family="runner", targets=["runner_100"], feature_set_name="runner_features",
                                     frame=_training_frame(), output_dir=path, min_rows=10)
    assert report["targets"]["runner_100"]["probability_validation_ready"]
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    assert runtime.predict_model("runner", "runner_100", {"price_pct_5m": 50}) is not None
    meta_path = path / "runner_100.meta.json"
    meta = json.loads(meta_path.read_text())
    meta["probability_validation_ready"] = False
    meta_path.write_text(json.dumps(meta))
    assert runtime.predict_model("runner", "runner_100", {"price_pct_5m": 50}) is None
    # The same model can retain a validated ranking role without being an
    # authorized probability, position sizing rule, or trade permission.
    assert runtime.predict_ranking_score("runner", "runner_100", {"price_pct_5m": 50}) is not None


def test_rank_priority_is_bounded_and_never_bypasses_trade_gates(monkeypatch):
    import analytics.model_runtime_common as model_runtime
    import runtime.runner_priority as priority
    monkeypatch.setattr(priority, "CFG", SimpleNamespace(SNIPER_LEARNING_PRIORITY_ENABLED=True))
    monkeypatch.setattr(model_runtime, "predict_ranking_score", lambda *args: 100.0)
    result = priority.learned_runner_priority({"price_pct_5m": 20, "txns_last_5m": 200, "liquidity_usd": 20000, "market_cap_usd": 50000})
    assert 0 < result["bonus"] <= 20
    assert result["buy_permission"] is False
    assert result["mode"] == "ranking_only"
    assert priority.learned_runner_priority({"price_pct_5m": 20})["bonus"] == 0


def test_hot_queue_updates_pending_candidate_without_duplicate_pop(monkeypatch):
    import runtime.hot_queue as queue_module
    monkeypatch.setattr(queue_module, "CFG", SimpleNamespace(HOT_QUEUE_DYNAMIC_BATCH_ENABLED=False, HOT_QUEUE_RECHECK_INTERVAL_S=15))
    queue = HotQueue(max_size=5)
    assert queue.add({"address": " A ", "price_pct_5m": 10, "txns_last_5m": 25})
    assert queue.add({"address": "A", "price_pct_5m": 60, "txns_last_5m": 200})
    assert queue.snapshot()["size"] == 1
    popped = queue.pop_batch(5)
    assert len(popped) == 1 and popped[0]["address"] == "A"
    assert popped[0]["price_pct_5m"] == 60
    assert not queue.pop_batch(5)
    assert "hot_queue_update" in [event["event"] for event in queue.events()]


def test_changed_opportunity_can_reenter_before_thirty_minute_ttl(monkeypatch):
    import runtime.hot_queue as queue_module
    monkeypatch.setattr(queue_module, "CFG", SimpleNamespace(HOT_QUEUE_DYNAMIC_BATCH_ENABLED=False, HOT_QUEUE_RECHECK_INTERVAL_S=15))
    queue = HotQueue(dedup_ttl_s=1800)
    now = [dt.datetime.now(dt.timezone.utc).timestamp()]
    monkeypatch.setattr(queue, "_now", lambda: now[0])
    initial = {"address": "A", "price_pct_5m": 10, "txns_last_5m": 25, "has_jupiter_route": False}
    assert queue.add(initial)
    assert len(queue.pop_batch(1)) == 1
    update = {**initial, "price_pct_5m": 80, "has_jupiter_route": True}
    assert not queue.add(update)
    now[0] += 16
    assert queue.add(update)
    assert len(queue.pop_batch(1)) == 1
    now[0] += 16
    assert not queue.add(update)  # no blind periodic re-buy/re-evaluation


def test_partial_update_does_not_reset_age_clock(monkeypatch):
    import runtime.hot_queue as queue_module
    monkeypatch.setattr(queue_module, "CFG", SimpleNamespace(HOT_QUEUE_HIGH_PRIORITY_MAX_AGE_MIN=20,
        HOT_QUEUE_LOW_PRIORITY_MAX_AGE_MIN=20, HOT_QUEUE_DYNAMIC_BATCH_ENABLED=False))
    queue = HotQueue(max_age_min=20)
    now = [dt.datetime.now(dt.timezone.utc).timestamp()]
    monkeypatch.setattr(queue, "_now", lambda: now[0])
    assert queue.add({"address": "A", "age_minutes": 19, "price_pct_5m": 10})
    now[0] += 120
    assert queue.add({"address": "A", "price_pct_5m": 100}) is False
    assert queue.pop_batch(1) == []


def test_queue_update_heap_and_seen_history_are_bounded(monkeypatch):
    queue = HotQueue(max_size=2, dedup_ttl_s=30)
    now = [dt.datetime.now(dt.timezone.utc).timestamp()]
    monkeypatch.setattr(queue, "_now", lambda: now[0])
    for value in range(20):
        queue.add({"address": "A", "price_pct_5m": value * 10})
    assert queue.snapshot()["size"] == 1
    assert len(queue._heap) <= 4
    queue.pop_batch(1)
    now[0] += 61
    queue.add({"address": "B"})
    assert "A" not in queue._seen


def test_unknown_future_and_infinite_age_cannot_receive_newborn_bonus():
    from runtime.candidate_priority import candidate_priority_score
    now = dt.datetime.now(dt.timezone.utc)
    baseline = candidate_priority_score({}, source="dex", now=now)
    assert baseline == 0
    assert candidate_priority_score({"age_minutes": float("inf")}, source="dex", now=now) == baseline
    assert candidate_priority_score({"age_minutes": -5}, source="dex", now=now) == baseline
    assert candidate_priority_score({"created_at": now + dt.timedelta(hours=1)}, source="dex", now=now) == baseline
    assert candidate_priority_score({"age_minutes": 0}, source="dex", now=now) > baseline
