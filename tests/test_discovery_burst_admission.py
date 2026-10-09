"""Actual bounded ingestion and queue semantics; no operational/provider calls."""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from pathlib import Path
import random

import pytest

from analytics import model_runtime_common as models
from analytics.inference_scope import inference_scope
from runtime import hot_queue as queues
from runtime import loop_scheduler
from runtime.hot_queue import HotQueue
from runtime.runner_priority import RUNNER_PRIORITY_WEIGHTS
from test_hot_queue_model_refresh import SwitchingRanker, candidate, environment, write_model


def rows(count):
    return [candidate(f"Q{i}", 12 if i % 2 else 18,
        source=("pumpportal", "pumpfun", "dex")[i % 3]) for i in range(count)]


@pytest.mark.parametrize("size", [1, 3, 10])
@pytest.mark.parametrize("count", [3, 12, 75])
def test_bulk_matches_sequential_admission_capacity_order_and_receipts(environment, size, count):
    bulk, now, root = environment
    bulk.max_size = size
    serial = HotQueue(max_size=size, persist_events=False)
    serial._now = lambda: now[0]
    write_model(root, True)
    incoming = rows(count)
    expected = [serial.add(token, source=token["source"]) for token in incoming]
    assert bulk.add_many(incoming) == expected
    assert bulk._seen == serial._seen and bulk._pending == serial._pending
    actual, expected_rows = bulk.pop_batch(size), serial.pop_batch(size)
    assert actual == expected_rows
    assert bulk.snapshot()["drop_counts"] == serial.snapshot()["drop_counts"]


def test_nine_checked_heads_are_vectorized_once_per_distinct_burst(environment):
    queue, _, root = environment
    queue.max_size = 75
    for threshold in RUNNER_PRIORITY_WEIGHTS:
        write_model(root, False, target=f"runner_{threshold}")
    assert all(queue.add_many(rows(75)))
    assert SwitchingRanker.calls == 9
    assert all(token[2]["learned_runner_priority"]["buy_permission"] is False for token in queue._heap)


def test_scalar_and_bulk_queue_feature_clocks_use_the_same_owned_snapshot_epoch(environment, monkeypatch):
    queue, now, root = environment
    import features.builder as builder
    from datetime import datetime, timezone, timedelta
    original, clocks = builder.build_feature_vector, []
    def capture(token, **kwargs):
        clocks.append(kwargs.get("now"))
        return original(token, **kwargs)
    monkeypatch.setattr(builder, "build_feature_vector", capture)
    write_model(root, True)
    birth = datetime.fromtimestamp(now[0], timezone.utc) - timedelta(minutes=1)
    assert queue.add(candidate("SCALAR", created_at=birth), source="dex")
    assert queue.add_many([candidate("BULK", created_at=birth, source="dex")]) == [True]
    assert clocks and all(clock is not None and clock.timestamp() == now[0] for clock in clocks)


def test_partial_duplicate_snapshots_match_serial_pending_updates(environment):
    queue, now, root = environment
    serial = HotQueue(max_size=2, persist_events=False)
    serial._now = lambda: now[0]
    queue.max_size = 2
    write_model(root, True)
    incoming = [candidate("A", source="dex"), candidate("B", source="pumpfun"),
        {"address": "A", "holders": 900, "price_pct_5m": 19, "source": "dex"},
        candidate("C", rank_score=90, source="pumpportal"),
        {"address": "B", "txns_last_5m": 700, "source": "pumpfun"},
        {"address": "A", "price_pct_5m": 12, "source": "dex"}]
    expected = [serial.add(token, source=token["source"]) for token in incoming]
    assert queue.add_many(incoming) == expected
    assert queue.pop_batch(2) == serial.pop_batch(2)


@pytest.mark.parametrize("elapsed,expected", [(1, False), (14, False), (15, True), (16, True)])
def test_bulk_new_generation_retains_evaluation_cooldown(environment, elapsed, expected):
    queue, now, root = environment
    write_model(root, False)
    original = candidate("A")
    assert queue.add(original) and queue.pop_batch(1)
    write_model(root, True)
    now[0] += elapsed
    assert queue.add_many([original]) == [expected]


@pytest.mark.parametrize("bad", [None, [], {"address": ""}, {"address": "BAD", 1: 5}])
def test_malformed_record_does_not_discard_valid_peers(environment, bad):
    queue, _, root = environment
    write_model(root, False)
    assert queue.add_many([candidate("A"), bad, candidate("B", 18)]) == [True, False, True]
    assert {token["address"] for token in queue.pop_batch(2)} == {"A", "B"}


def test_bulk_input_detaches_and_rejects_forged_queue_cache(environment):
    queue, now, _ = environment
    incoming = candidate("A", auxiliary_observations={"social": {"value": 1}},
        learned_runner_priority={"bonus": 999, "buy_permission": True},
        _hot_queue_observed_at=now[0] + 999, _hot_queue_priority_generation="forged")
    original = deepcopy(incoming)
    assert queue.add_many([incoming]) == [True]
    incoming["auxiliary_observations"]["social"]["value"] = 999
    saved = queue.pop_batch(1)[0]
    assert saved["auxiliary_observations"]["social"]["value"] == 1
    assert saved["learned_runner_priority"]["bonus"] == 0
    assert saved["learned_runner_priority"]["buy_permission"] is False
    assert saved["_hot_queue_observed_at"] == now[0]
    assert original["learned_runner_priority"]["bonus"] == 999


def test_bulk_has_fresh_generation_isolated_from_ambient_entry(environment):
    queue, _, root = environment
    write_model(root, False)
    with inference_scope():
        old = models.family_model_selection("runner")
        write_model(root, True)
        assert all(queue.add_many([candidate("A"), candidate("B", 18)]))
        assert queue.pop_batch(1)[0]["address"] == "A"
        assert models.family_model_selection("runner") == old


def test_mid_prediction_replacement_does_not_mix_burst_generation(environment):
    queue, _, root = environment
    for threshold in (100, 500):
        write_model(root, False, target=f"runner_{threshold}")
    SwitchingRanker.hook = lambda: write_model(root, True, target="runner_500")
    assert all(queue.add_many(rows(3)))
    selections = [token[2]["learned_runner_priority"]["model_selection"] for token in queue._heap]
    assert all(selection == selections[0] for selection in selections)
    old = queue.snapshot()["priority_generation"]
    queue.pop_batch(1)
    assert queue.snapshot()["priority_generation"] != old


@pytest.mark.parametrize("bad", [None, (), [candidate("X")] * 1001])
def test_invalid_or_unbounded_batch_rejected_before_any_admission(environment, bad):
    queue, _, _ = environment
    with pytest.raises(ValueError):
        queue.add_many(bad)
    assert queue.snapshot()["size"] == 0 and not queue.events()


def test_pending_expiry_before_partial_update_invalidates_precomputed_merge(environment):
    queue, now, root = environment
    queue.max_age_min = 1
    write_model(root, False)
    assert queue.add(candidate("OLD", age_minutes=.5))
    now[0] += 61
    # First admission expires OLD; later partial notification must not borrow
    # its former measured market snapshot or learned receipt.
    assert queue.add_many([candidate("NEW", age_minutes=.1), {"address": "OLD"}]) == [True, True]
    saved = {token["address"]: token for token in queue.pop_batch(2)}
    assert "market_cap_usd" not in saved["OLD"]
    assert saved["OLD"]["learned_runner_priority"]["reason"] == "incomplete_market_snapshot"


@pytest.mark.parametrize("size", [1, 5, 25])
@pytest.mark.parametrize("seed", [7, 19, 37])
def test_duplicate_partial_capacity_sequences_keep_native_semantics(environment, size, seed):
    queue, now, root = environment
    queue.max_size = size
    serial = HotQueue(max_size=size, persist_events=False)
    serial._now = lambda: now[0]
    write_model(root, True)
    rng = random.Random(seed)
    incoming = []
    for _ in range(75):
        address = f"A{rng.randrange(10)}"
        token = candidate(address, rng.choice([12, 18, 50]), source=rng.choice(["dex", "pumpfun", "pumpportal"]))
        if rng.random() < .5:
            token = {"address": address, "source": token["source"], "holders": rng.randrange(1000)}
        incoming.append(token)
    expected = [serial.add(token, source=token["source"]) for token in incoming]
    assert queue.add_many(incoming) == expected
    assert queue.pop_batch(100) == serial.pop_batch(100)
    assert queue.snapshot()["drop_counts"] == serial.snapshot()["drop_counts"]


@pytest.mark.parametrize("failure", ["short", "matrix", "nonfinite"])
def test_invalid_head_output_does_not_grant_a_buy_or_lose_a_peer(environment, failure):
    queue, _, root = environment
    write_model(root, False)
    SwitchingRanker.malformed = failure
    assert all(queue.add_many([candidate("LOW"), candidate("HIGH", 18)]))
    saved = {token["address"]: token for token in queue.pop_batch(2)}
    assert all(token["learned_runner_priority"]["buy_permission"] is False for token in saved.values())
    assert saved["LOW"]["learned_runner_priority"]["bonus"] == 0
    if failure != "nonfinite":
        assert saved["HIGH"]["learned_runner_priority"]["bonus"] == 0


def test_discovery_chunk_yield_keeps_live_pending_updates_and_evaluation_history(environment):
    queue, now, root = environment
    queue.max_size = 300
    write_model(root, False)
    incoming = rows(129)
    async def cooperate(delay):
        assert delay == 0
        first = queue.pop_batch(1)[0]
        now[0] += 16
        assert queue.add({"address": "Q127", "price_pct_5m": 500, "source": "pumpfun"})
        assert first["address"] in queue._evaluated_at
    assert asyncio.run(loop_scheduler.admit_hot_candidates(queue, incoming, sleep=cooperate)) == 129
    assert queue.snapshot()["size"] == 128
    saved = {token["address"]: token for token in queue.pop_batch(300)}
    assert saved["Q127"]["price_pct_5m"] == 500
    assert saved["Q128"]["_hot_queue_enqueued_at"] == now[0]


@pytest.mark.parametrize("count", [0, 1, 75, 129, 257])
def test_actual_discovery_scheduler_yields_between_bounded_chunks_without_loss(count):
    calls, yields = [], []
    class Queue:
        def add_many(self, tokens, *, source):
            calls.append((deepcopy(tokens), source))
            return [True] * len(tokens)
    async def cooperate(delay):
        assert delay == 0
        yields.append(len(calls))
    admitted = asyncio.run(loop_scheduler.admit_hot_candidates(Queue(), rows(count), sleep=cooperate))
    assert admitted == count
    assert [row["address"] for batch, _ in calls for row in batch] == [f"Q{i}" for i in range(count)]
    assert all(len(batch) <= 128 and source == "pumpfun" for batch, source in calls)
    assert len(yields) == max(0, len(calls) - 1)


def test_run_bot_native_stream_routes_bulk_queue_and_retains_guarded_disabled_path():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "run_bot.py").read_text(encoding="utf-8"))
    # Extract only the actual discovery conditional, never import/start run_bot.
    statement = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp) and isinstance(node.test.operand, ast.Name)
        and node.test.operand.id == "_runtime_discovery_paused"
        and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == "get_latest_pumpfun" for call in ast.walk(node)))
    wrapper = ast.AsyncFunctionDef(name="stream", args=ast.arguments(posonlyargs=[], args=[],
        kwonlyargs=[], kw_defaults=[], defaults=[]), body=[statement], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    from types import SimpleNamespace
    incoming, evaluated, errors = rows(3), [], []
    async def fetch():
        return incoming
    async def evaluate(token, session, *, source):
        evaluated.append((token["address"], session, source))
    queue = HotQueue(max_size=10, persist_events=False)
    queue._now = lambda: 1791504000.
    namespace = {"_runtime_discovery_paused": False, "pumpfun": SimpleNamespace(get_latest_pumpfun=fetch),
        "CFG": SimpleNamespace(HOT_QUEUE_ENABLED=True), "GLOBAL_HOT_QUEUE": queue,
        "_evaluate_and_buy_guarded": evaluate, "_note_runtime_error": lambda *args: errors.append(args),
        "log": SimpleNamespace(error=lambda *args: None)}
    exec(compile(module, "run_bot.py", "exec"), namespace)
    asyncio.run(namespace["stream"]())
    assert not errors and not evaluated and queue.snapshot()["size"] == 3
    assert [token["source"] for _, _, token in sorted(queue._heap, key=lambda entry: entry[1])] == [row["source"] for row in incoming]
    namespace["CFG"].HOT_QUEUE_ENABLED = False
    asyncio.run(namespace["stream"]())
    assert evaluated == [(row["address"], None, "pumpfun") for row in incoming]
    assert not errors
