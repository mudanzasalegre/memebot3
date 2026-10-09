"""Actual scanner queue behavior with synthetic candidates, no bot/provider calls."""
import datetime as dt
from types import SimpleNamespace

import pytest

import runtime.hot_queue as queue_module
from runtime.hot_queue import HotQueue


@pytest.fixture
def clock_queue(monkeypatch):
    now = [dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc).timestamp()]
    monkeypatch.setattr(queue_module, "CFG", SimpleNamespace(HOT_QUEUE_DYNAMIC_BATCH_ENABLED=False,
        HOT_QUEUE_RECHECK_INTERVAL_S=15, HOT_QUEUE_HIGH_PRIORITY_MIN_SCORE=75,
        HOT_QUEUE_HIGH_PRIORITY_MAX_AGE_MIN=20, HOT_QUEUE_LOW_PRIORITY_MAX_AGE_MIN=20))
    monkeypatch.setattr(queue_module, "candidate_priority_score", lambda token, **kwargs: token["score"])
    queue = HotQueue(max_size=1, dedup_ttl_s=1800)
    monkeypatch.setattr(queue, "_now", lambda: now[0])
    return queue, now


def token(address, score, momentum=10, **extra):
    return dict(address=address, score=score, price_pct_5m=momentum, **extra)


def test_add_reports_false_when_incoming_candidate_is_immediately_evicted(clock_queue):
    queue, _ = clock_queue
    assert queue.add(token("high", 100))
    assert queue.add(token("low", 1)) is False
    assert [row["address"] for row in queue.pop_batch(1)] == ["high"]


@pytest.mark.parametrize("previously_evaluated", [False, True])
def test_unevaluated_signal_reenters_when_capacity_frees_without_waiting_thirty_minutes(clock_queue, previously_evaluated):
    queue, now = clock_queue
    if previously_evaluated:
        assert queue.add(token("candidate", 1, momentum=10))
        assert queue.pop_batch(1)[0]["price_pct_5m"] == 10
        now[0] += 16
    candidate = token("candidate", 1, momentum=80)
    assert queue.add(candidate)
    assert queue.add(token("higher", 100))
    assert queue.pop_batch(1)[0]["address"] == "higher"
    now[0] += 16
    assert queue.add(candidate) is True
    assert queue.pop_batch(1)[0]["address"] == "candidate"


def test_equal_priority_overflow_preserves_oldest_waiting_candidates(clock_queue):
    queue, _ = clock_queue
    queue.max_size = 2
    assert queue.add(token("first", 100))
    assert queue.add(token("second", 100))
    assert queue.add(token("newest", 100)) is False
    assert [row["address"] for row in queue.pop_batch(2)] == ["first", "second"]


def test_pending_explicit_rank_update_is_not_discarded_as_unchanged_market(clock_queue):
    queue, _ = clock_queue
    assert queue.add(token("candidate", 1, rank_score=50))
    assert queue.add(token("candidate", 1, rank_score=90))
    assert queue.pop_batch(1)[0]["rank_score"] == 90


@pytest.mark.parametrize("age", [None, False, True, -1, float("nan"), float("inf"), "invalid"])
def test_invalid_age_update_does_not_rejuvenate_original_age(clock_queue, age):
    queue, now = clock_queue
    assert queue.add(token("candidate", 10, age_min=19))
    now[0] += 120
    assert queue.add(token("candidate", 10, momentum=100, age_minutes=age)) is False
    assert queue.pop_batch(1) == []


def test_expired_high_priority_candidate_does_not_evict_fresh_lower_priority(clock_queue):
    queue, now = clock_queue
    assert queue.add(token("expired", 100, age_minutes=19))
    now[0] += 120
    assert queue.add(token("fresh", 1, age_minutes=0))
    assert [row["address"] for row in queue.pop_batch(1)] == ["fresh"]


def test_evaluated_unchanged_candidate_still_requires_ttl(clock_queue):
    queue, now = clock_queue
    candidate = token("candidate", 1)
    assert queue.add(candidate)
    assert queue.pop_batch(1)
    now[0] += 16
    assert not queue.add(candidate)
    now[0] += 1800
    assert queue.add(candidate)


def test_evaluated_changed_candidate_still_obeys_recheck_interval(clock_queue):
    queue, now = clock_queue
    assert queue.add(token("candidate", 1, momentum=10))
    assert queue.pop_batch(1)
    now[0] += 1
    assert not queue.add(token("candidate", 1, momentum=100))
    now[0] += 15
    assert queue.add(token("candidate", 1, momentum=100))


def test_dedup_ttl_starts_at_evaluation_not_at_original_enqueue(monkeypatch, clock_queue):
    queue, now = clock_queue
    queue.max_age_min = 100
    monkeypatch.setattr(queue_module, "CFG", SimpleNamespace(HOT_QUEUE_DYNAMIC_BATCH_ENABLED=False,
        HOT_QUEUE_LOW_PRIORITY_MAX_AGE_MIN=100, HOT_QUEUE_HIGH_PRIORITY_MAX_AGE_MIN=100))
    candidate = token("candidate", 1)
    assert queue.add(candidate)
    now[0] += 2400
    assert queue.pop_batch(1)
    assert not queue.add(candidate)


@pytest.mark.parametrize("rank_field", ["rank_score", "research_rank_score"])
def test_changed_rank_after_evaluation_can_be_reconsidered(clock_queue, rank_field):
    queue, now = clock_queue
    assert queue.add(token("candidate", 1, **{rank_field: 50}))
    assert queue.pop_batch(1)
    now[0] += 16
    assert queue.add(token("candidate", 1, **{rank_field: 90}))


def test_expired_and_evicted_history_does_not_grow_without_evaluations(clock_queue):
    queue, _ = clock_queue
    assert queue.add(token("high", 100))
    for i in range(200):
        assert not queue.add(token(f"low{i}", 1))
    assert len(queue._pending) == len(queue._seen) == len(queue._signals) == len(queue._heap) == 1
    assert not queue._evaluated_at and not queue._evaluated_signals


def test_expired_evaluation_history_is_pruned_as_one_population(clock_queue):
    queue, now = clock_queue
    assert queue.add(token("old", 1)) and queue.pop_batch(1)
    now[0] += 1801
    assert queue.add(token("new", 100))
    assert "old" not in queue._seen and "old" not in queue._signals
    assert "old" not in queue._evaluated_at and "old" not in queue._evaluated_signals


def test_market_rank_updates_change_actual_queue_priority(monkeypatch, clock_queue):
    import runtime.candidate_priority as priority
    monkeypatch.setattr(priority, "learned_runner_priority", lambda token: {"bonus": 0.})
    monkeypatch.setattr(queue_module, "candidate_priority_score", priority.candidate_priority_score)
    queue, _ = clock_queue
    queue.max_size = 2
    assert queue.add(token("candidate", 1, rank_score=50, age_minutes=1), source="dex")
    assert queue.add(token("competitor", 1, rank_score=61, age_minutes=1), source="dex")
    assert queue.add(token("candidate", 1, rank_score=90, age_minutes=1), source="dex")
    assert queue.pop_batch(1)[0]["address"] == "candidate"


@pytest.mark.parametrize("invalid", [True, False, float("nan"), float("inf"), "invalid"])
def test_invalid_rank_is_unknown_not_high_priority(monkeypatch, invalid):
    import runtime.candidate_priority as priority
    monkeypatch.setattr(priority, "learned_runner_priority", lambda token: {"bonus": 0.})
    common = dict(age_minutes=20, price_pct_5m=0, txns_last_5m=0, liquidity_usd=0)
    assert priority.candidate_priority_score({**common, "rank_score": invalid}, source="dex") == 0


def test_measured_zero_rank_does_not_fall_through_to_historical_alias(monkeypatch):
    import runtime.candidate_priority as priority
    monkeypatch.setattr(priority, "learned_runner_priority", lambda token: {"bonus": 0.})
    common = dict(age_minutes=20, price_pct_5m=0, txns_last_5m=0, liquidity_usd=0, research_rank_score=90)
    assert priority.candidate_priority_score({**common, "rank_score": 0}, source="dex") == 0
    assert priority.candidate_priority_score({**common, "rank_score": None}, source="dex") == 35


@pytest.mark.parametrize("field", ["price_pct_5m", "txns_last_5m", "liquidity_usd", "market_cap_usd"])
@pytest.mark.parametrize("boolean", [False, True])
def test_json_boolean_is_not_a_complete_snapshot_for_learned_priority(monkeypatch, field, boolean):
    import analytics.model_runtime_common as models
    from runtime.runner_priority import learned_runner_priority
    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid measurements must not query a ranker")
    monkeypatch.setattr(models, "predict_ranking_score", unexpected)
    monkeypatch.setattr(models, "family_model_selection", unexpected)
    payload = dict(price_pct_5m=0., txns_last_5m=100, liquidity_usd=20000, market_cap_usd=50000)
    payload[field] = boolean
    result = learned_runner_priority(payload)
    assert result["reason"] == "incomplete_market_snapshot" and result["bonus"] == 0.
    assert result["buy_permission"] is False
