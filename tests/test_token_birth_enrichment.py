"""Native source/queue/T0 contracts; all creation/market requests are simulated."""
from copy import deepcopy
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from analytics.token_time import compute_age_minutes
from features.builder import build_feature_vector
from runtime import entry_observation as entry
from utils import price_service
from test_entry_observation import observed, preparation_namespace, run_function

MINT = "So11111111111111111111111111111111111111112"
OTHER = "11111111111111111111111111111111"


def creation(*, now=None, minutes=3):
    now = now or dt.datetime.now(dt.timezone.utc)
    born = (now - dt.timedelta(minutes=minutes)).replace(microsecond=0)
    return {"address": MINT, "created_at": born, "token_birth_observation": {
        "version": "birdeye_mint_creation_receipt_v1", "source": "birdeye", "chain": "solana",
        "address": MINT, "created_at": born.isoformat(), "slot": 123,
        "tx_hash": "1" * 64, "received_at": now.timestamp() - .01,
        "basis": "provider_reported_mint_creation_not_independent_chain_verification"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["pumpfun", "pumpportal", "dex", "revival"])
async def test_actual_common_entry_resolves_missing_birth_before_market_collection(source):
    receipt = creation()
    tick = observed(address=MINT, price_pct_5m=25000)
    ns, waits, market = preparation_namespace(tick)
    ns["_candidate_age_minutes"] = compute_age_minutes
    ns["warn_if_nulls"] = lambda *a, **k: None
    calls = []
    async def enrich(token):
        calls.append("birth")
        return {**token, **deepcopy(receipt)}
    async def collect(*args, **kwargs):
        calls.append("market")
        return tick
    market.side_effect = collect
    ns["enrich_token_birth"] = enrich
    queued = {"address": MINT, "discovered_via": source, "discovered_at": time.time() - 10}
    result = await ns["prepare"](queued, SimpleNamespace(scalar=AsyncMock(return_value=None)))
    assert result is not None, (waits, calls)
    candidate, frozen = result
    assert calls == ["birth", "market"] and not waits and frozen is not None
    assert candidate["created_at"] == receipt["created_at"]
    assert candidate["token_birth_observation"] == receipt["token_birth_observation"]
    assert candidate["discovered_at"] == queued["discovered_at"]
    assert candidate["price_pct_5m"] == 25000


def test_discovery_context_detaches_and_carries_original_birth_receipt():
    token = {**creation(), "price_pct_5m": 25000}
    candidate = entry.discovery_candidate(token)
    assert candidate["token_birth_observation"] == token["token_birth_observation"]
    candidate["token_birth_observation"]["slot"] = 999
    assert token["token_birth_observation"]["slot"] == 123
    assert "price_pct_5m" not in candidate


def test_current_market_snapshot_cannot_rejuvenate_known_birth():
    queued = creation(minutes=30)
    tick = observed(address=MINT, created_at=queued["created_at"] + dt.timedelta(minutes=29))
    candidate = entry.prepare_entry_candidate(queued, tick)
    assert candidate["created_at"] == queued["created_at"]
    assert candidate["token_birth_observation"] == queued["token_birth_observation"]


def test_fallback_merge_moves_creation_receipt_with_its_original_clock():
    secondary = creation()
    primary = observed(address=MINT)
    candidate = price_service._merge_market_fields(primary, secondary, "birdeye")
    assert candidate["created_at"] == secondary["created_at"]
    assert candidate["token_birth_observation"] == secondary["token_birth_observation"]


def test_native_t0_proof_retains_provider_context_outside_predictors():
    token = creation()
    vector = build_feature_vector(token)
    proof = json.loads(vector.attrs["t0_token_clock_proof"])
    assert proof["token_birth_observation"] == token["token_birth_observation"]
    assert "token_birth_observation" not in vector.index


@pytest.mark.parametrize("field,value", [("chain", "ethereum"), ("source", "dexscreener"),
    ("address", OTHER), ("slot", True), ("slot", -1), ("tx_hash", "x" * 64),
    ("received_at", True), ("received_at", float("inf")), ("received_at", -1), ("received_at", 10 ** 1000),
    ("created_at", "2026-10-11T00:00:00Z"), ("basis", "independent_chain_verification")])
def test_invalid_receipt_cannot_supply_a_birth(field, value):
    from analytics.token_birth import merge_birth_context
    raw = creation()
    raw["token_birth_observation"][field] = value
    merged = merge_birth_context({"address": MINT}, raw)
    assert merged == {"address": MINT}


@pytest.mark.parametrize("minutes", [0, 3, 60, 300000])
def test_birth_context_does_not_expire_like_market_prices(minutes):
    from analytics.token_birth import checked_token_birth
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=500000)
    raw = creation(now=now, minutes=minutes)
    assert checked_token_birth(raw["token_birth_observation"], MINT, now=now + dt.timedelta(hours=2)) is not None


@pytest.mark.parametrize("change", ["clock", "receipt", "model_age"])
def test_actual_prebuy_birth_guard_rejects_changed_original_inputs(change):
    candidate = entry.prepare_entry_candidate(creation(), observed(address=MINT))
    frozen = entry.freeze_entry_observation(candidate, paper=True)
    vector = build_feature_vector(candidate).to_dict()
    assert entry.entry_observation_problem(candidate, frozen, paper=True, vector=vector) is None
    if change == "clock": candidate["created_at"] += dt.timedelta(seconds=1)
    elif change == "receipt": candidate["token_birth_observation"]["slot"] += 1
    else: vector["age_minutes"] += 1
    assert entry.entry_observation_problem(candidate, frozen, paper=True, vector=vector) is not None


def test_native_trade_freeze_and_frame_keep_birth_context_without_a_new_predictor():
    from runtime.trade_learning import freeze_entry_features, validate_entry_features
    from features.auxiliary_semantics import input_frame
    from features.token_clock_semantics import checked_row_clock
    vector = build_feature_vector(creation())
    frozen = freeze_entry_features(vector, address=MINT, captured_at=dt.datetime.now(dt.timezone.utc))
    validate_entry_features(frozen, address=MINT)
    row = input_frame(vector).iloc[0].to_dict()
    assert checked_row_clock(row)["token_birth_observation"] == frozen["token_clock"]["token_birth_observation"]


def test_birth_receipt_received_after_frozen_t0_cannot_certify_a_model_input():
    from features.token_clock_semantics import bind_vector_clock
    token = creation()
    vector = build_feature_vector(token)
    token["token_birth_observation"]["received_at"] = vector.timestamp.timestamp() + .001
    assert "t0_token_clock_proof" not in bind_vector_clock(vector, token).attrs


def test_actual_requeue_context_retains_birth_without_resetting_residence():
    import ast
    from analytics.token_birth import merge_birth_context, FIELD
    from analytics.token_time import BIRTH_CLOCK_FIELDS
    meta = {"first_seen": time.time() - 120, "attempts": 7}
    original = deepcopy(meta)
    ns = {"lista_pares": SimpleNamespace(meta=lambda addr: meta), "merge_birth_context": merge_birth_context,
        "BIRTH_CLOCK_FIELDS": BIRTH_CLOCK_FIELDS, "BIRTH_FIELD": FIELD, "_norm_dex_id": lambda value: value}
    exec(compile(ast.Module(body=[run_function("_remember_queue_context")], type_ignores=[]), "run_bot.py", "exec"), ns)
    token = creation()
    ns["_remember_queue_context"](MINT, token)
    assert meta["first_seen"] == original["first_seen"] and meta["attempts"] == 7
    assert meta["token_birth_observation"] == token["token_birth_observation"]
    ns["_remember_queue_context"](MINT, creation(minutes=1))
    assert meta["created_at"] == token["created_at"]


def test_native_hot_queue_market_update_cannot_rejuvenate_birth_or_residence(monkeypatch):
    from runtime import hot_queue as module
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    monkeypatch.setattr(module, "candidate_priority_score", lambda *a, **k: 50)
    queue = module.HotQueue(max_size=10, max_age_min=100, persist_events=False)
    monkeypatch.setattr(queue, "_now", lambda: now)
    original = creation(minutes=3)
    assert queue.add(original)
    now += 10
    assert queue.add({**creation(minutes=1), "price_pct_5m": 25000})
    result = queue.pop_batch(1)[0]
    assert result["created_at"] == original["created_at"]
    assert result["token_birth_observation"] == original["token_birth_observation"]
    assert result["_hot_queue_enqueued_at"] == now - 10


def resolver(lookup, **kwargs):
    from runtime.token_birth_enrichment import TokenBirthResolver
    return TokenBirthResolver(lookup=lookup, configured=lambda: True, degraded=lambda: False, **kwargs)


@pytest.mark.asyncio
async def test_real_resolver_queries_once_and_keeps_original_http_receipt_in_detached_cache():
    raw = creation()
    lookup = AsyncMock(return_value=raw)
    actual = resolver(lookup)
    first = await actual.enrich({"address": MINT, "discovered_at": 123})
    first["token_birth_observation"]["slot"] = 999
    second = await actual.enrich({"address": MINT, "discovered_at": 456})
    assert lookup.await_count == 1 and second["token_birth_observation"] == raw["token_birth_observation"]
    assert second["discovered_at"] == 456 and second["token_birth_enrichment"]["status"] == "cache_hit"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [creation(), {"address": "not-a-mint"},
    {"address": MINT, "created_at": "invalid"}, {"address": MINT, "created_at": True}])
async def test_existing_or_invalid_original_clock_never_triggers_a_replacement_request(token):
    lookup = AsyncMock(return_value=creation(minutes=1))
    result = await resolver(lookup).enrich(token)
    assert lookup.await_count == 0 and result.get("created_at") == token.get("created_at")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["not_configured", "degraded", "disabled"])
async def test_no_creation_request_without_configuration_or_during_circuit_degradation(monkeypatch, case):
    from runtime import token_birth_enrichment as module
    lookup = AsyncMock(return_value=creation())
    actual = resolver(lookup)
    if case == "not_configured": actual.configured = lambda: False
    elif case == "degraded": actual.degraded = lambda: True
    else:
        monkeypatch.setenv("BIRTH_ENRICHMENT_ENABLED", "false")
        monkeypatch.setattr(module, "GLOBAL_TOKEN_BIRTH_RESOLVER", actual)
    result = await (module.enrich_token_birth({"address": MINT}) if case == "disabled"
                    else actual.enrich({"address": MINT}))
    assert lookup.await_count == 0 and "created_at" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {"address": OTHER}, "malformed"])
async def test_missing_provider_context_stays_unknown_and_is_negative_cached(result):
    lookup = AsyncMock(return_value=result)
    actual = resolver(lookup)
    first = await actual.enrich({"address": MINT})
    second = await actual.enrich({"address": MINT})
    assert lookup.await_count == 1 and "created_at" not in first and "created_at" not in second
    assert second["token_birth_enrichment"]["status"] == "negative_cache"


@pytest.mark.asyncio
async def test_creation_budget_is_spaced_and_bounded_without_poisoning_price_provider_circuit():
    now = [0.]
    lookup = AsyncMock(return_value=None)
    actual = resolver(lookup, rpm=2, clock=lambda: now[0], negative_ttl_s=1)
    assert (await actual.enrich({"address": MINT}))["token_birth_enrichment"]["status"] == "unavailable"
    now[0] = 29.999
    assert (await actual.enrich({"address": OTHER}))["token_birth_enrichment"]["status"] == "creation_budget"
    now[0] = 30.
    await actual.enrich({"address": OTHER})
    now[0] = 59.999
    assert (await actual.enrich({"address": MINT}))["token_birth_enrichment"]["status"] == "creation_budget"
    now[0] = 60.
    await actual.enrich({"address": MINT})
    assert lookup.await_count == 3 and actual.snapshot()["max_creation_cu_per_minute"] == 70


@pytest.mark.asyncio
async def test_same_mint_concurrent_waiters_share_one_owned_provider_lookup():
    import asyncio
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def lookup(address):
        calls.append(address)
        started.set()
        await release.wait()
        return creation()
    actual = resolver(lookup)
    owner = asyncio.create_task(actual.enrich({"address": MINT}))
    await started.wait()
    peer = asyncio.create_task(actual.enrich({"address": MINT}))
    await asyncio.sleep(0)
    release.set()
    a, b = await asyncio.gather(owner, peer)
    assert calls == [MINT] and a["token_birth_observation"] == b["token_birth_observation"]
    assert b["token_birth_enrichment"]["status"] == "coalesced"
    assert actual.snapshot()["inflight"] == 0


@pytest.mark.asyncio
async def test_cancellation_drains_owned_lookup_without_negative_cache_or_detached_work():
    import asyncio
    started, ended = asyncio.Event(), asyncio.Event()
    async def lookup(address):
        started.set()
        try: await asyncio.Event().wait()
        finally: ended.set()
    actual = resolver(lookup)
    owner = asyncio.create_task(actual.enrich({"address": MINT}))
    await started.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError): await owner
    assert ended.is_set() and actual.snapshot()["inflight"] == 0 and actual.snapshot()["cache_size"] == 0


@pytest.mark.asyncio
async def test_timeout_drains_lookup_and_does_not_fabricate_birth():
    import asyncio
    ended = asyncio.Event()
    async def lookup(address):
        try: await asyncio.Event().wait()
        finally: ended.set()
    actual = resolver(lookup, timeout_s=.05)
    result = await actual.enrich({"address": MINT})
    assert ended.is_set() and "created_at" not in result
    assert result["token_birth_enrichment"]["status"] == "timeout" and actual.snapshot()["inflight"] == 0


@pytest.mark.asyncio
async def test_cache_capacity_is_bounded_and_expired_negatives_are_not_rejuvenated():
    now = [0.]
    lookup = AsyncMock(return_value=None)
    actual = resolver(lookup, rpm=60, cache_size=1, negative_ttl_s=1, clock=lambda: now[0])
    await actual.enrich({"address": MINT})
    now[0] = 1.
    await actual.enrich({"address": OTHER})
    assert actual.snapshot()["cache_size"] == 1 and MINT not in actual._cache
    now[0] = 2.
    await actual.enrich({"address": MINT})
    assert lookup.await_count == 3 and actual.snapshot()["cache_size"] == 1


@pytest.mark.asyncio
async def test_real_creation_adapter_is_consumed_without_any_market_or_order_request(monkeypatch):
    from fetcher import birdeye
    raw = creation()
    proof = raw["token_birth_observation"]
    fetch = AsyncMock(return_value={"tokenAddress": MINT, "blockUnixTime": int(raw["created_at"].timestamp()),
        "slot": proof["slot"], "txHash": proof["tx_hash"], "_market_received_at": proof["received_at"]})
    monkeypatch.setattr(birdeye, "_fetch", fetch)
    actual = resolver(birdeye.get_token_creation_info)
    result = await actual.enrich({"address": MINT})
    assert result["token_birth_observation"] == proof
    assert fetch.await_args.args[0] == "/defi/token_creation_info" and fetch.await_count == 1
    assert fetch.await_args.kwargs["force_refresh"] is False
    assert fetch.await_args.kwargs["identity_field"] == "tokenAddress"


@pytest.mark.asyncio
async def test_real_provider_circuit_prevents_creation_lookup_without_clearing_shared_degradation():
    from analytics.api_budget import record_provider_event, provider_status, reset_provider_circuits
    from runtime.token_birth_enrichment import TokenBirthResolver
    lookup = AsyncMock(return_value=creation())
    reset_provider_circuits()
    try:
        record_provider_event("birdeye", "rate_limit")
        actual = TokenBirthResolver(lookup=lookup, configured=lambda: True)
        result = await actual.enrich({"address": MINT})
        assert result["token_birth_enrichment"]["status"] == "provider_degraded" and lookup.await_count == 0
        assert provider_status("birdeye")["degraded"] is True
    finally:
        reset_provider_circuits()


@pytest.mark.asyncio
async def test_cancelling_coalesced_peer_cannot_cancel_the_owned_provider_lookup():
    import asyncio
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def lookup(address):
        calls.append(address)
        started.set()
        await release.wait()
        return creation()
    actual = resolver(lookup)
    owner = asyncio.create_task(actual.enrich({"address": MINT}))
    await started.wait()
    peer = asyncio.create_task(actual.enrich({"address": MINT}))
    await asyncio.sleep(0)
    peer.cancel()
    with pytest.raises(asyncio.CancelledError): await peer
    assert not owner.done()
    release.set()
    result = await owner
    assert calls == [MINT] and result["token_birth_enrichment"]["status"] == "resolved"
    assert actual.snapshot()["inflight"] == 0


@pytest.mark.asyncio
async def test_parallel_identity_capacity_returns_immediately_without_launching_extra_lookup():
    import asyncio
    now = [0.]
    started, release = asyncio.Event(), asyncio.Event()
    async def lookup(address):
        started.set()
        await release.wait()
        return creation()
    actual = resolver(lookup, max_inflight=1, clock=lambda: now[0], rpm=60)
    owner = asyncio.create_task(actual.enrich({"address": MINT}))
    await started.wait()
    now[0] = 1.
    result = await actual.enrich({"address": OTHER})
    assert result["token_birth_enrichment"]["status"] == "concurrency_budget"
    assert actual.snapshot()["inflight"] == 1
    release.set()
    await owner
    assert actual.snapshot()["inflight"] == 0


def test_malformed_new_birth_settings_are_clamped_without_first_import_failure():
    actual = resolver(AsyncMock(), rpm="invalid", timeout_s=float("inf"), cache_size=-1,
                      max_inflight=100, negative_ttl_s=True)
    assert actual.rpm == 12 and actual.timeout_s == 3 and actual.cache_size == 1
    assert actual.max_inflight == 8 and actual.negative_ttl_s == 30


@pytest.mark.asyncio
async def test_creation_wait_does_not_block_independently_owned_discovery_or_position_ticks():
    import asyncio
    started, release = asyncio.Event(), asyncio.Event()
    async def lookup(address):
        started.set()
        await release.wait()
        return creation()
    owner = asyncio.create_task(resolver(lookup).enrich({"address": MINT}))
    await started.wait()
    ticks = []
    async def independent(name):
        await asyncio.sleep(0)
        ticks.append(name)
    await asyncio.gather(independent("pump-discovery"), independent("position-monitor"))
    assert ticks == ["pump-discovery", "position-monitor"] and not owner.done()
    release.set()
    await owner


@pytest.mark.asyncio
@pytest.mark.parametrize("first_stop", ["cancel", "timeout"])
async def test_repeated_cancellation_cannot_release_ownership_before_provider_cleanup(first_stop):
    import asyncio
    started, cleaning, release, ended = [asyncio.Event() for _ in range(4)]
    async def lookup(address):
        started.set()
        try: await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            ended.set()
    actual = resolver(lookup, timeout_s=.05 if first_stop == "timeout" else 3.)
    owner = asyncio.create_task(actual.enrich({"address": MINT}))
    await started.wait()
    if first_stop == "cancel": owner.cancel()
    await cleaning.wait()
    owner.cancel()
    owner.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not owner.done() and not ended.is_set() and actual.snapshot()["inflight"] == 1
    release.set()
    if first_stop == "cancel":
        with pytest.raises(asyncio.CancelledError): await owner
        assert actual.snapshot()["cache_size"] == 0
    else:
        result = await owner
        assert result["token_birth_enrichment"]["status"] == "timeout"
    assert ended.is_set() and actual.snapshot()["inflight"] == 0
