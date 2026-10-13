"""Actual queue clock/reconsideration contracts; no providers or trading."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from analytics.token_time import BIRTH_CLOCK_FIELDS
from runtime import hot_queue as module
from runtime.hot_queue import HotQueue


@pytest.fixture
def environment(monkeypatch):
    now = [dt.datetime(2026, 10, 13, tzinfo=dt.timezone.utc).timestamp()]
    monkeypatch.setattr(module, "CFG", SimpleNamespace(HOT_QUEUE_DYNAMIC_BATCH_ENABLED=False,
        HOT_QUEUE_RECHECK_INTERVAL_S=15, HOT_QUEUE_HIGH_PRIORITY_MIN_SCORE=75,
        HOT_QUEUE_HIGH_PRIORITY_MAX_AGE_MIN=20, HOT_QUEUE_LOW_PRIORITY_MAX_AGE_MIN=20))
    monkeypatch.setattr(module, "candidate_priority_score", lambda token, **kwargs: 100.)
    monkeypatch.setattr(module, "runner_priority_generation", lambda: "isolated-scheduling-only")
    monkeypatch.setattr(module, "learned_runner_priorities", lambda rows, **kwargs: [{"bonus": 0.} for _ in rows])
    queue = HotQueue(max_size=2, max_age_min=20, dedup_ttl_s=1800)
    monkeypatch.setattr(queue, "_now", lambda: now[0])
    return queue, now


def candidate(**fields):
    return {"address": "synthetic-mint", "price_pct_5m": 50, "price_usd": .01,
            "txns_last_5m": 100, "liquidity_usd": 20000, "market_cap_usd": 50000, **fields}


@pytest.mark.parametrize("field", BIRTH_CLOCK_FIELDS)
@pytest.mark.parametrize("encoding", ["iso", "seconds", "milliseconds", "microseconds",
                                      "nanoseconds", "datetime", "naive", "digit_string"])
def test_every_original_birth_alias_expires_at_same_original_age(environment, field, encoding):
    queue, now = environment
    born = dt.datetime.fromtimestamp(now[0] - 19*60, dt.timezone.utc)
    values = {"iso": born.isoformat(), "seconds": born.timestamp(),
              "milliseconds": born.timestamp()*1000, "microseconds": born.timestamp()*1e6,
              "nanoseconds": born.timestamp()*1e9, "datetime": born,
              "naive": born.replace(tzinfo=None), "digit_string": str(int(born.timestamp()*1000))}
    assert queue.add(candidate(**{field: values[encoding]}))
    now[0] += 2*60
    assert queue.pop_batch(1) == []
    assert queue.snapshot()["drop_counts"]["reason:max_age"] == 1


@pytest.mark.parametrize("field", BIRTH_CLOCK_FIELDS)
def test_new_original_birth_reconsiders_evaluated_unknown_age(environment, field):
    queue, now = environment
    assert queue.add(candidate())
    assert queue.pop_batch(1)
    now[0] += 16
    born = dt.datetime.fromtimestamp(now[0] - 60, dt.timezone.utc)
    assert queue.add(candidate(**{field: born.isoformat()})) is True
    assert queue.pop_batch(1)[0][field] == born.isoformat()


def test_measured_token_age_alias_advances_from_original_observation(environment):
    queue, now = environment
    assert queue.add(candidate(token_age_min=19))
    now[0] += 2*60
    assert queue.pop_batch(1) == []


@pytest.mark.parametrize("field", ["age_minutes", "age_min", "token_age_min"])
def test_new_measured_age_availability_reconsiders_unknown_age(environment, field):
    queue, now = environment
    assert queue.add(candidate()) and queue.pop_batch(1)
    now[0] += 16
    assert queue.add(candidate(**{field: 1})) is True
    assert queue.pop_batch(1)[0][field] == 1


def test_token_age_alias_update_resets_only_its_measurement_observation(environment):
    queue, now = environment
    assert queue.add(candidate(token_age_min=18))
    now[0] += 60
    assert queue.add(candidate(token_age_min=19))
    observed = now[0]
    now[0] += 30
    pending = queue._heap[0][2]
    assert pending["_hot_queue_observed_at"] == observed
    view = queue._priority_view(pending, dt.datetime.fromtimestamp(now[0], dt.timezone.utc))
    assert view["age_minutes"] == 19.5
    now[0] += 60
    assert queue.pop_batch(1) == []


@pytest.mark.parametrize("field", ["age_minutes", "age_min", "token_age_min"])
@pytest.mark.parametrize("invalid", [None, False, True, -1, float("nan"), float("inf"), "invalid"])
def test_invalid_measured_age_is_not_new_usable_availability(environment, field, invalid):
    queue, now = environment
    assert queue.add(candidate()) and queue.pop_batch(1)
    now[0] += 16
    assert queue.add(candidate(**{field: invalid})) is False


@pytest.mark.parametrize("field", ["age_minutes", "age_min", "token_age_min"])
def test_advancing_known_measured_age_alone_does_not_bypass_market_dedup(environment, field):
    queue, now = environment
    assert queue.add(candidate(**{field: 1})) and queue.pop_batch(1)
    now[0] += 16
    assert queue.add(candidate(**{field: 2})) is False


def test_new_birth_still_obeys_original_recheck_interval(environment):
    queue, now = environment
    assert queue.add(candidate()) and queue.pop_batch(1)
    now[0] += 1
    born = dt.datetime.fromtimestamp(now[0]-60, dt.timezone.utc).isoformat()
    updated = candidate(created_at=born)
    assert queue.add(updated) is False
    now[0] += 15
    assert queue.add(updated)


@pytest.mark.parametrize("old_field", BIRTH_CLOCK_FIELDS)
@pytest.mark.parametrize("new_field", BIRTH_CLOCK_FIELDS)
def test_known_original_birth_cannot_be_rejuvenated_by_another_alias(environment, old_field, new_field):
    queue, now = environment
    old = dt.datetime.fromtimestamp(now[0]-19*60, dt.timezone.utc).isoformat()
    assert queue.add(candidate(**{old_field: old}))
    now[0] += 2*60
    younger = dt.datetime.fromtimestamp(now[0]-60, dt.timezone.utc).isoformat()
    assert queue.add(candidate(price_pct_5m=100, **{new_field: younger})) is False
    assert queue.pop_batch(1) == []


def test_checked_provider_birth_receipt_is_a_reconsidered_input_not_buy_permission(environment):
    from analytics.token_birth import BASIS, FIELD, VERSION
    queue, now = environment
    address = "1"*32
    born = dt.datetime.fromtimestamp(now[0]-60, dt.timezone.utc).isoformat()
    token = candidate(address=address, created_at=born)
    assert queue.add(token) and queue.pop_batch(1)
    now[0] += 16
    receipt = {"version": VERSION, "source": "birdeye", "chain": "solana", "address": address,
               "created_at": born, "slot": 1, "tx_hash": "1"*64, "received_at": now[0], "basis": BASIS}
    assert queue.add({**token, FIELD: receipt})
    queued = queue.pop_batch(1)[0]
    assert queued[FIELD] == receipt
    assert "buy_permission" not in queued


def test_birth_receipt_reconsideration_preserves_unevaluated_peer_order(environment):
    queue, now = environment
    assert queue.add(candidate(address="first"))
    assert queue.add(candidate(address="second"))
    original_turn = queue._pending["first"]
    now[0] += 1
    born = dt.datetime.fromtimestamp(now[0]-60, dt.timezone.utc).isoformat()
    assert queue.add(candidate(address="first", created_at=born))
    assert queue._pending["first"] == original_turn
    assert [row["address"] for row in queue.pop_batch(2)] == ["first", "second"]


@pytest.mark.parametrize("field", BIRTH_CLOCK_FIELDS)
def test_native_bulk_admission_reconsiders_new_original_birth(environment, field):
    queue, now = environment
    assert queue.add_many([candidate()]) == [True]
    assert queue.pop_batch(1)
    now[0] += 16
    born = dt.datetime.fromtimestamp(now[0]-60, dt.timezone.utc).isoformat()
    assert queue.add_many([candidate(**{field: born})]) == [True]
    assert queue.pop_batch(1)[0][field] == born


@pytest.mark.parametrize("field", ["pair_created_at", "listed_at", "launch_date", "first_seen_at"])
def test_venue_and_discovery_clock_is_not_new_birth_availability(environment, field):
    queue, now = environment
    assert queue.add(candidate()) and queue.pop_batch(1)
    now[0] += 16
    clock = dt.datetime.fromtimestamp(now[0]-60, dt.timezone.utc).isoformat()
    assert queue.add(candidate(**{field: clock})) is False


def test_new_creation_reference_reconsiders_lookup_without_becoming_a_birth(environment):
    from analytics.mint_creation_reference import BASIS, FIELD, VERSION
    from analytics.token_time import compute_age_minutes
    queue, now = environment
    address = "1"*32
    token = candidate(address=address)
    assert queue.add(token) and queue.pop_batch(1)
    now[0] += 16
    receipt = {"version": VERSION, "source": "pumpportal", "chain": "solana", "address": address,
               "tx_hash": "1"*64, "slot": 1, "received_at": now[0], "basis": BASIS}
    assert queue.add({**token, FIELD: receipt})
    queued = queue.pop_batch(1)[0]
    assert queued[FIELD] == receipt
    assert compute_age_minutes(queued, now=dt.datetime.fromtimestamp(now[0], dt.timezone.utc)) is None
    assert "buy_permission" not in queued


@pytest.mark.parametrize("renewed_receipt", [False, True])
def test_repeated_creation_reference_does_not_rejuvenate_or_force_reconsideration(environment, renewed_receipt):
    from analytics.mint_creation_reference import BASIS, FIELD, VERSION
    queue, now = environment
    address = "1"*32
    receipt = {"version": VERSION, "source": "pumpportal", "chain": "solana", "address": address,
               "tx_hash": "1"*64, "slot": 1, "received_at": now[0], "basis": BASIS}
    token = candidate(address=address, **{FIELD: receipt})
    assert queue.add(token) and queue.pop_batch(1)
    now[0] += 16
    # Reconnect/replay receipt time is not a different original transaction.
    if renewed_receipt:
        token = {**token, FIELD: {**receipt, "received_at": now[0]}}
    assert queue.add(token) is False
