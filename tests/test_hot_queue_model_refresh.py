"""Native queue and checked temporary models; no operational bot or provider."""
from __future__ import annotations

import datetime as dt
from hashlib import sha256
import json
import os
from types import SimpleNamespace

import joblib
import numpy as np
import pytest

from analytics import model_runtime_common as models
from analytics.inference_scope import inference_scope
from runtime import hot_queue as queue_module
from runtime.hot_queue import HotQueue
from ml.model_validation_warnings import RANKING_METRIC_VERSION


class SwitchingRanker:
    hook = None
    calls = 0
    malformed = None

    def __init__(self, favor_low):
        self.favor_low = favor_low

    def rank_score(self, frame):
        type(self).calls += 1
        if type(self).hook is not None:
            hook, type(self).hook = type(self).hook, None
            hook()
        low = frame["price_pct_5m"].to_numpy() < 15
        if type(self).malformed == "short":
            return np.zeros(max(0, len(frame)-1))
        if type(self).malformed == "matrix":
            return np.zeros((len(frame), 1))
        if type(self).malformed == "nonfinite":
            return np.where(low, np.nan, .9)
        return np.where(low == self.favor_low, .9, .1)


def write_model(root, favor_low, *, preserve_stats=False, target="runner_100"):
    path = root / "ml/models/runner" / f"{target}.pkl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = path.with_suffix(".meta.json")
    stats = {p: p.stat() for p in (path, meta)} if preserve_stats else {}
    joblib.dump(SwitchingRanker(favor_low), path)
    metadata = {"family": "runner", "target": target, "features": ["price_pct_5m"],
        "model_sha256": sha256(path.read_bytes()).hexdigest(), "ranking_validation_ready": True,
        "ranking_metric_version": RANKING_METRIC_VERSION,
        "rank_reference_quantiles": [0, .25, .5, .75, 1],
        "validation": {"mode": "purged_token_walk_forward", "temporal": {"out_of_sample_rows": 30}}}
    meta.write_text(json.dumps(metadata))
    for original, stat in stats.items():
        assert original.stat().st_size == stat.st_size
        os.utime(original, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return path


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(queue_module, "CFG", SimpleNamespace(HOT_QUEUE_DYNAMIC_BATCH_ENABLED=False,
        HOT_QUEUE_RECHECK_INTERVAL_S=15, HOT_QUEUE_HIGH_PRIORITY_MIN_SCORE=75,
        HOT_QUEUE_HIGH_PRIORITY_MAX_AGE_MIN=20, HOT_QUEUE_LOW_PRIORITY_MAX_AGE_MIN=20))
    now = [dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc).timestamp()]
    queue = HotQueue(max_size=10, persist_events=False)
    monkeypatch.setattr(queue, "_now", lambda: now[0])
    SwitchingRanker.hook, SwitchingRanker.calls, SwitchingRanker.malformed = None, 0, None
    yield queue, now, tmp_path
    SwitchingRanker.hook = None
    SwitchingRanker.malformed = None


def candidate(address, momentum=12, **extra):
    return {"address": address, "age_minutes": 1, "price_pct_5m": momentum,
        "txns_last_5m": 100, "liquidity_usd": 20000, "market_cap_usd": 50000, **extra}


@pytest.mark.parametrize("preserve_stats", [False, True])
def test_pending_order_tracks_checked_replacement_without_a_new_feed_event(environment, preserve_stats):
    queue, _, root = environment
    write_model(root, False)
    assert queue.add(candidate("A"), source="dex")
    assert queue.add(candidate("B", 18), source="dex")
    write_model(root, True, preserve_stats=preserve_stats)
    assert queue.pop_batch(1)[0]["address"] == "A"


def test_invalidated_model_removes_original_pending_bonus(environment):
    queue, _, root = environment
    path = write_model(root, True)
    assert queue.add(candidate("A"), source="dex")
    path.write_bytes(b"not an approved ranker")
    token = queue.pop_batch(1)[0]
    assert token["learned_runner_priority"]["bonus"] == 0
    assert token["learned_runner_priority"]["buy_permission"] is False


def test_new_generation_reconsiders_unchanged_evaluated_snapshot_after_cooldown(environment):
    queue, now, root = environment
    write_model(root, False)
    token = candidate("A")
    assert queue.add(token) and queue.pop_batch(1)
    write_model(root, True)
    now[0] += 16
    assert queue.add(token)


def test_pending_tie_update_retains_original_waiting_turn(environment):
    queue, _, _ = environment
    assert queue.add(candidate("A"), source="dex")
    assert queue.add(candidate("B"), source="dex")
    assert queue.add(candidate("A", rank_score=90), source="dex")
    assert queue.add(candidate("B", rank_score=90), source="dex")
    assert queue.add(candidate("A", rank_score=90, has_jupiter_route=True), source="dex")
    assert [row["address"] for row in queue.pop_batch(2)] == ["A", "B"]


def test_pending_non_bucket_measurement_is_not_discarded(environment):
    queue, _, _ = environment
    assert queue.add(candidate("A", holders=100), source="dex")
    assert queue.add(candidate("A", holders=900), source="dex")
    assert queue.pop_batch(1)[0]["holders"] == 900


def test_original_measured_age_is_not_frozen_in_pending_base_priority(environment):
    queue, now, _ = environment
    assert queue.add(candidate("A"), source="dex")
    now[0] += 240
    assert queue.add(candidate("B", 11), source="dex")
    assert queue.pop_batch(1)[0]["address"] == "B"


def test_queue_refresh_does_not_borrow_ambient_entry_generation(environment):
    queue, _, root = environment
    write_model(root, False)
    assert queue.add(candidate("A"), source="dex")
    assert queue.add(candidate("B", 18), source="dex")
    with inference_scope():
        old = models.family_model_selection("runner")
        write_model(root, True)
        assert queue.pop_batch(1)[0]["address"] == "A"
        assert models.family_model_selection("runner") == old


@pytest.mark.parametrize("seconds,allowed", [(1, False), (14, False), (15, True), (16, True)])
def test_generation_change_never_bypasses_evaluation_cooldown(environment, seconds, allowed):
    queue, now, root = environment
    write_model(root, False)
    token = candidate("A")
    assert queue.add(token) and queue.pop_batch(1)
    write_model(root, True)
    now[0] += seconds
    assert queue.add(token) is allowed


def test_same_generation_keeps_unchanged_evaluation_ttl(environment):
    queue, now, root = environment
    write_model(root, False)
    token = candidate("A")
    assert queue.add(token) and queue.pop_batch(1)
    now[0] += 16
    assert not queue.add(token)


@pytest.mark.parametrize("original_age", [None, 19])
def test_generation_refresh_never_rejuvenates_expired_candidate(environment, original_age):
    queue, now, root = environment
    write_model(root, False)
    assert queue.add(candidate("A", age_minutes=original_age))
    now[0] += 1260 if original_age is None else 120
    write_model(root, True)
    assert queue.pop_batch(1) == []
    assert queue.snapshot()["size"] == 0
    assert not queue._seen and not queue._signals and not queue._evaluated_generations


def test_refresh_preserves_pending_original_clock_and_seen_history(environment):
    queue, now, root = environment
    write_model(root, False)
    assert queue.add(candidate("A"), source="dex")
    original_seen = queue._seen["A"]
    original_clock = queue._heap[0][2]["_hot_queue_enqueued_at"]
    now[0] += 60
    write_model(root, True)
    assert queue.add(candidate("B", 18), source="dex")
    pending_a = next(row[2] for row in queue._heap if row[2]["address"] == "A")
    assert pending_a["_hot_queue_enqueued_at"] == original_clock
    assert pending_a["_hot_queue_observed_at"] == original_clock
    assert queue._seen["A"] == original_seen and not queue._evaluated_at


def test_replacement_is_applied_before_actual_capacity_eviction(environment):
    queue, _, root = environment
    queue.max_size = 2
    write_model(root, False)
    assert queue.add(candidate("A"), source="dex")
    assert queue.add(candidate("B", 18), source="dex")
    write_model(root, True)
    assert not queue.add(candidate("C", 18), source="dex")
    assert [row["address"] for row in queue.pop_batch(2)] == ["A", "B"]


def test_same_generation_refresh_reuses_checked_snapshot_predictions(environment):
    queue, _, root = environment
    write_model(root, False)
    assert queue.add(candidate("A"), source="dex")
    assert queue.add(candidate("B", 18), source="dex")
    calls = SwitchingRanker.calls
    assert queue.pop_batch(1)
    assert SwitchingRanker.calls == calls
    assert queue.pop_batch(1)
    assert SwitchingRanker.calls == calls


def test_pending_snapshot_change_does_recompute_model_prediction(environment):
    queue, _, root = environment
    write_model(root, False)
    assert queue.add(candidate("A"), source="dex")
    calls = SwitchingRanker.calls
    assert queue.add(candidate("A", 18), source="dex")
    assert SwitchingRanker.calls == calls + 1
    assert queue.pop_batch(1)[0]["learned_runner_priority"]["bonus"] == 3.6


def test_admission_does_not_trust_forged_internal_generation_bonus_or_clocks(environment):
    queue, now, _ = environment
    token = candidate("A", age_minutes=None, _hot_queue_enqueued_at=now[0]+10000,
        _hot_queue_observed_at=now[0]+10000, _hot_queue_priority_generation="forged",
        learned_runner_priority={"bonus": 10000, "buy_permission": True})
    assert queue.add(token)
    result = queue.pop_batch(1)[0]
    assert result["learned_runner_priority"]["bonus"] == 0
    assert result["learned_runner_priority"]["buy_permission"] is False
    assert result["_hot_queue_enqueued_at"] == now[0]
    assert result["age_minutes"] is None


def test_changed_generation_during_prediction_is_pinned_until_next_queue_operation(environment):
    queue, _, root = environment
    write_model(root, False)
    assert queue.add(candidate("A"), source="dex")
    assert queue.add(candidate("B", 18), source="dex")
    write_model(root, True)
    SwitchingRanker.hook = lambda: write_model(root, False)
    # Both pending ranks use the same captured generation, not a mixed family.
    first = queue.pop_batch(1)[0]
    assert first["address"] == "A" and first["learned_runner_priority"]["bonus"] == 3.6
    second = queue.pop_batch(1)[0]
    assert second["address"] == "B" and second["learned_runner_priority"]["bonus"] == 3.6
    assert first["_hot_queue_priority_generation"] != second["_hot_queue_priority_generation"]


def test_actual_scheduler_refreshes_generation_between_awaited_evaluations(environment):
    import asyncio
    from runtime.loop_scheduler import evaluate_hot_queue
    queue, _, root = environment
    write_model(root, False)
    for address, momentum in [("A", 12), ("B", 18), ("C", 19)]:
        assert queue.add(candidate(address, momentum), source="dex")
    calls = []
    async def evaluate(token):
        calls.append(token["address"])
        if len(calls) == 1:
            write_model(root, True)
    assert asyncio.run(evaluate_hot_queue(queue, evaluate, max_items=2, budget_s=3)) == 2
    assert calls == ["C", "A"]
    assert queue.snapshot()["size"] == 1


@pytest.mark.parametrize("disabled", [True, False])
def test_disabled_or_missing_ranker_removes_cached_bonus(environment, monkeypatch, disabled):
    import runtime.runner_priority as priority
    queue, _, root = environment
    path = write_model(root, True)
    assert queue.add(candidate("A"), source="dex")
    if disabled:
        monkeypatch.setattr(priority, "CFG", SimpleNamespace(SNIPER_LEARNING_PRIORITY_ENABLED=False))
    else:
        path.unlink()
    token = queue.pop_batch(1)[0]
    assert token["learned_runner_priority"]["bonus"] == 0
    assert token["learned_runner_priority"]["buy_permission"] is False


def test_generation_history_is_pruned_with_other_evaluated_history(environment):
    queue, now, _ = environment
    assert queue.add(candidate("A")) and queue.pop_batch(1)
    assert "A" in queue._evaluated_generations
    now[0] += 1801
    assert queue.add(candidate("B"))
    assert "A" not in queue._evaluated_generations


@pytest.mark.parametrize("seconds,allowed", [(1, False), (16, True)])
def test_changed_learned_input_after_evaluation_uses_bounded_reconsideration(environment, seconds, allowed):
    queue, now, _ = environment
    assert queue.add(candidate("A", holders=100)) and queue.pop_batch(1)
    now[0] += seconds
    assert queue.add(candidate("A", holders=900)) is allowed


def test_admission_detaches_nested_market_snapshot_from_feed_object(environment):
    queue, _, _ = environment
    payload = candidate("A", social_signal={"status": "observed", "links": ["original"]})
    assert queue.add(payload)
    payload["social_signal"]["links"].append("later mutation")
    assert queue.pop_batch(1)[0]["social_signal"]["links"] == ["original"]


@pytest.mark.parametrize("count", [0, 1, 20, 1000])
def test_batched_ranks_match_native_scalar_percentiles(environment, count):
    _, _, root = environment
    write_model(root, True)
    vectors = [{"price_pct_5m": 12 if i % 2 == 0 else 18} for i in range(count)]
    with inference_scope():
        values = models.predict_ranking_scores("runner", "runner_100", vectors)
        assert values == [80 if i % 2 == 0 else 20 for i in range(count)]
        if values:
            assert values[0] == models.predict_ranking_score("runner", "runner_100", vectors[0])


@pytest.mark.parametrize("malformed", ["short", "matrix"])
def test_batch_output_shape_is_not_silently_truncated_or_broadcast(environment, malformed):
    _, _, root = environment
    write_model(root, True)
    SwitchingRanker.malformed = malformed
    with inference_scope():
        assert models.predict_ranking_scores("runner", "runner_100", [{"price_pct_5m": 12}] * 3) == [None] * 3


def test_nonfinite_batch_output_is_independently_unknown(environment):
    _, _, root = environment
    write_model(root, True)
    SwitchingRanker.malformed = "nonfinite"
    with inference_scope():
        assert models.predict_ranking_scores("runner", "runner_100", [{"price_pct_5m": 12}, {"price_pct_5m": 18}]) == [None, 80]


def test_bad_input_row_does_not_disable_valid_batch_peers(environment):
    import pandas as pd
    _, _, root = environment
    write_model(root, True)
    with inference_scope():
        assert models.predict_ranking_scores("runner", "runner_100", [
            {"price_pct_5m": 12}, pd.DataFrame({"price_pct_5m": [12, 18]}), {"price_pct_5m": 18}]) == [80, None, 20]


@pytest.mark.parametrize("vectors", [None, (), [{"price_pct_5m": 12}] * 1001])
def test_batch_has_strict_type_and_work_bound(environment, vectors):
    with pytest.raises(ValueError, match="at most 1000"):
        models.predict_ranking_scores("runner", "runner_100", vectors)


def test_batch_preserves_original_query_evidence_in_an_entry_scope(environment):
    from features.builder import build_feature_vector
    from analytics.decision_provenance import model_query_snapshot
    _, _, root = environment
    write_model(root, True)
    vector = build_feature_vector(candidate("A"))
    with inference_scope():
        assert models.predict_ranking_scores("runner", "runner_100", [vector]) == [80]
        observations = model_query_snapshot(vector)["observations"]
        assert len(observations) == 1
        assert observations[0]["value"] == 80 and observations[0]["source"]["status"] == "checked_artifact"


def test_scheduling_scope_does_not_mute_ambient_entry_evidence(environment):
    from analytics.inference_scope import observations_enabled
    queue, _, root = environment
    write_model(root, True)
    with inference_scope():
        assert observations_enabled()
        assert queue.add(candidate("A")) and queue.pop_batch(1)
        assert observations_enabled()
        with inference_scope(record_observations=False):
            assert not observations_enabled()
        assert observations_enabled()


def test_all_nine_extreme_heads_remain_bounded_ranking_only(environment):
    from runtime.runner_priority import RUNNER_PRIORITY_WEIGHTS, learned_runner_priorities
    _, now, root = environment
    for threshold in RUNNER_PRIORITY_WEIGHTS:
        path = write_model(root, True, target=f"runner_{threshold}")
        meta = path.with_suffix(".meta.json")
        payload = json.loads(meta.read_text())
        payload["rank_reference_quantiles"] = [0, .1, .2, .3, .4]
        meta.write_text(json.dumps(payload))
    with inference_scope():
        result = learned_runner_priorities([candidate("A"), candidate("B", 18)],
            now=dt.datetime.fromtimestamp(now[0], dt.timezone.utc))
    assert result[0]["bonus"] == 20 and result[0]["buy_permission"] is False
    assert set(result[0]["rank_percentiles"]) == {f"runner_{t}" for t in RUNNER_PRIORITY_WEIGHTS}
    assert result[0]["rank_percentiles"]["runner_10000"] == 100
    result[0]["model_selection"]["heads"].clear()
    assert len(result[1]["model_selection"]["heads"]) == 9


@pytest.mark.parametrize("field", ["price_pct_5m", "txns_last_5m", "liquidity_usd", "market_cap_usd"])
@pytest.mark.parametrize("invalid", [True, None, float("nan")])
def test_bad_batch_measurement_is_neutral_without_disabling_valid_peers(environment, field, invalid):
    from runtime.runner_priority import learned_runner_priorities
    _, _, root = environment
    write_model(root, True)
    with inference_scope():
        result = learned_runner_priorities([candidate("A", **{field: invalid}), candidate("B")])
    assert result[0]["bonus"] == 0 and result[0]["rank_percentiles"] == {}
    assert result[1]["bonus"] == 3.6 and result[1]["buy_permission"] is False
