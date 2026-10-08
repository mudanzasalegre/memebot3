from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time

import pytest

from analytics import sizing
from features.builder import build_feature_vector
from runtime import entry_observation as entry
from utils import lista_pares, price_service, simple_cache
from utils.data_utils import sanitize_token_data, apply_default_values
from utils.market_observation import MARKET_ALIASES, retain_fresh_market_fields, stamp_market_observation

MINT = "A" * 44
OTHER = "B" * 44


def observed(**values):
    return stamp_market_observation({"address": MINT, "price_usd": 2,
        "liquidity_usd": 5000, "market_cap_usd": 50000, "volume_24h_usd": 2000,
        "txns_last_5m": 100, "txns_last_5m_buys": 60, "txns_last_5m_sells": 40,
        "price_pct_5m": 2000, "dexId": "pumpfun", **values}, "dexscreener")


def run_function(name):
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name)


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    simple_cache._CACHE.clear()
    monkeypatch.setattr(price_service, "_RETRY_ON_FAIL", 0)
    monkeypatch.setattr(price_service, "_USE_JUPITER_IMPACT", False)
    monkeypatch.setattr(price_service, "USE_GECKO_TERMINAL", False)
    yield
    simple_cache._CACHE.clear()


@pytest.mark.parametrize("alias", MARKET_ALIASES)
def test_stale_fields_cannot_be_resurrected_by_any_normalization_alias(alias):
    tick = observed()
    for record in tick["market_observation"]["fields"].values():
        record["received_at"] -= 121
    tick[alias] = {"usd": 999, "h24": 999} if alias in {"liquidity", "volume"} else 999
    filtered = sanitize_token_data(retain_fresh_market_fields(tick))
    assert alias not in filtered
    assert all(filtered.get(field) is None for field in entry.MARKET_FIELDS)
    assert alias in tick


def test_queue_is_identity_only_not_a_saved_market_or_model_decision():
    original = {"address": MINT, "symbol": "TEST", "discovered_via": "pumpfun",
        "paper_entry_policy": {"id": "checked-synthetic"}, "website": "https://example.invalid",
        "price_usd": 999, "liquidityUsd": 999999, "holders": 900,
        "price_pct_5m": 50000, "score_total": 100, "trend": 1, "social_ok": 1,
        "rug_score": 0, "cluster_bad": 0, "has_jupiter_route": 1, "price_impact_pct": 0,
        "entry_lane": "stale_lane", "liquidity_is_proxy": 1, "paper_bootstrap": 1,
        "runner_exit_profile": "old-policy", "label": 1, "pnl_pct": 10000}
    candidate = entry.prepare_entry_candidate(original, observed(price_pct_5m=-50, holders=None))
    assert candidate["price_usd"] == 2 and candidate["price_pct_5m"] == -50
    assert candidate["holders"] is None and candidate["liquidity_is_proxy"] == 0
    assert not any(key in candidate for key in ("score_total", "trend", "social_ok", "rug_score",
        "cluster_bad", "has_jupiter_route", "price_impact_pct", "entry_lane", "label", "pnl_pct",
        "paper_bootstrap", "runner_exit_profile", "liquidityUsd"))
    candidate["paper_entry_policy"]["id"] = "changed"
    candidate["market_observation"]["fields"]["price_usd"]["value"] = 99
    assert original["paper_entry_policy"]["id"] == "checked-synthetic"


@pytest.mark.parametrize("kind", ["missing", "wrong_mint", "stale_price", "unproved_price"])
def test_no_fallback_to_queued_price_after_failed_current_collection(kind):
    tick = observed()
    if kind == "missing": tick = None
    elif kind == "wrong_mint": tick["address"] = OTHER
    elif kind == "stale_price": tick["market_observation"]["fields"]["price_usd"]["received_at"] -= 121
    else: tick.pop("market_observation")
    assert entry.prepare_entry_candidate({"address": MINT, "price_usd": 100}, tick) is None


def test_unknown_optional_activity_stays_unknown_without_blanket_admission_gate():
    tick = stamp_market_observation({"address": MINT, "price_usd": 2, "liquidity_usd": 5000}, "birdeye")
    candidate = entry.prepare_entry_candidate({"address": MINT, "price_pct_5m": 999, "holders": 800}, tick)
    frozen = entry.freeze_entry_observation(candidate, paper=True)
    vector = build_feature_vector(candidate).to_dict()
    assert candidate["price_pct_5m"] is None and candidate["holders"] is None
    assert entry.entry_observation_problem(candidate, frozen, paper=True, vector=vector) is None


def test_fresh_venue_wins_over_queued_canonical_dex_alias():
    candidate = entry.prepare_entry_candidate({"address": MINT, "dex_id": "pumpfun"},
        observed(dexId="pumpswap"))
    assert candidate["dex_id"] == candidate["dexId"] == "pumpswap"
    assert build_feature_vector(candidate)["dex_id"] == "pumpswap"


def test_stale_optional_liquidity_can_be_explicit_paper_proxy_without_reusing_old_proof():
    tick = observed(liquidity_usd=90000)
    tick["market_observation"]["fields"]["liquidity_usd"]["received_at"] -= 121
    candidate = entry.prepare_entry_candidate({"address": MINT}, tick)
    assert candidate["liquidity_usd"] is None
    assert entry.apply_paper_liquidity_proxy(candidate, 1200, "green", paper=True)
    assert entry.freeze_entry_observation(candidate, paper=True) is not None
    assert "liquidity_usd" not in candidate["market_observation"]["fields"]
    assert tick["market_observation"]["fields"]["liquidity_usd"]["value"] == 90000


@pytest.mark.parametrize("paper", [True, False])
def test_fresh_provider_derived_liquidity_remains_a_proxy_in_market_and_model_inputs(paper):
    tick = observed(liquidity_usd_is_proxy=True, liquidity_is_proxy=True)
    candidate = entry.prepare_entry_candidate({"address": MINT, "liquidity_is_proxy": 0}, tick)
    frozen = entry.freeze_entry_observation(candidate, paper=paper)
    vector = build_feature_vector(candidate).to_dict()
    assert frozen is not None and frozen.provider_proxy and frozen.proxy is None
    assert candidate["liquidity_is_proxy"] == vector["liquidity_is_proxy"] == 1
    assert entry.entry_observation_problem(candidate, frozen, paper=paper, vector=vector) is None
    vector["liquidity_is_proxy"] = 0
    assert entry.entry_observation_problem(candidate, frozen, paper=paper, vector=vector) == "changed_model_liquidity_basis"


@pytest.mark.parametrize("first_liquidity", [None, 0, 5000])
def test_provider_proxy_metadata_moves_only_with_the_selected_liquidity(first_liquidity):
    primary = stamp_market_observation({"address": MINT, "price_usd": 2,
        "liquidity_usd": first_liquidity}, "birdeye")
    secondary = observed(liquidity_usd=90000, liquidity_usd_is_proxy=True, liquidity_is_proxy=True)
    merged = price_service._merge_market_fields(primary, secondary, "dexscreener")
    candidate = entry.prepare_entry_candidate({"address": MINT}, merged)
    frozen = entry.freeze_entry_observation(candidate, paper=True)
    assert candidate["liquidity_usd"] == (90000 if first_liquidity is None else first_liquidity)
    assert candidate["liquidity_is_proxy"] == int(first_liquidity is None)
    assert frozen is not None and frozen.provider_proxy is (first_liquidity is None)


def test_real_adapter_derived_liquidity_stays_distinct_from_direct_pool_liquidity():
    from fetcher.dexscreener import _norm_from_pair
    derived = stamp_market_observation(_norm_from_pair({"baseToken": {"address": MINT},
        "priceUsd": "2", "liquidityLocked": "500"}), "dexscreener")
    candidate = entry.prepare_entry_candidate({"address": MINT}, derived)
    assert candidate["liquidity_usd"] == 1000 and candidate["liquidity_is_proxy"] == 1
    assert entry.freeze_entry_observation(candidate, paper=True).provider_proxy
    direct = stamp_market_observation(_norm_from_pair({"baseToken": {"address": MINT},
        "priceUsd": "2", "liquidity": {"usd": 0}, "liquidityLocked": "500"}), "dexscreener")
    candidate = entry.prepare_entry_candidate({"address": MINT}, direct)
    assert candidate["liquidity_usd"] == 0 and candidate["liquidity_is_proxy"] == 0
    assert not entry.apply_paper_liquidity_proxy(candidate, 1200, "green", paper=True)


@pytest.mark.parametrize("momentum", [500, 2000, 10000, 100000, 1000000])
def test_current_extreme_momentum_has_no_new_fixed_upside_ceiling(momentum):
    candidate = entry.prepare_entry_candidate({"address": MINT}, observed(price_pct_5m=momentum))
    frozen = entry.freeze_entry_observation(candidate, paper=True)
    vector = build_feature_vector(candidate).to_dict()
    assert vector["price_pct_5m"] == momentum
    assert entry.entry_observation_problem(candidate, frozen, paper=True, vector=vector) is None


@pytest.mark.parametrize("liquidity", [0, 10, False, "bad", float("nan")])
def test_paper_proxy_cannot_overwrite_any_reported_liquidity(liquidity):
    tick = observed(liquidity_usd=liquidity)
    assert not entry.apply_paper_liquidity_proxy(tick, 1200, "green", paper=True)
    assert entry.freeze_entry_observation(observed(liquidity_usd=0), paper=True) is not None


def test_explicit_paper_proxy_has_no_http_liquidity_proof_and_cannot_enter_live():
    candidate = entry.prepare_entry_candidate({"address": MINT}, observed(liquidity_usd=None))
    assert entry.apply_paper_liquidity_proxy(candidate, 1200, "green", paper=True)
    assert "liquidity_usd" not in candidate["market_observation"]["fields"]
    frozen = entry.freeze_entry_observation(candidate, paper=True)
    assert frozen is not None and entry.entry_observation_problem(candidate, frozen, paper=True) is None
    assert entry.freeze_entry_observation(candidate, paper=False) is None
    assert entry.entry_observation_problem(candidate, frozen, paper=False) is not None
    candidate["paper_liquidity_observation"]["kind"] = "changed"
    assert entry.entry_observation_problem(candidate, frozen, paper=True) == "changed_or_expired_paper_proxy"


@pytest.mark.parametrize("change", ["token", "vector", "receipt", "expiry", "proxy_flag", "mint"])
def test_frozen_decision_rejects_mutation_or_expiry_instead_of_only_refreshing_price(monkeypatch, change):
    candidate = entry.prepare_entry_candidate({"address": MINT}, observed())
    frozen = entry.freeze_entry_observation(candidate, paper=True)
    vector = build_feature_vector(candidate).to_dict()
    now = time.time()
    if change == "token": candidate["price_pct_5m"] = 10000
    elif change == "vector": vector["liquidity_usd"] = 90000
    elif change == "receipt": candidate["market_observation"]["fields"]["liquidity_usd"]["source"] = "jupiter"
    elif change == "expiry": monkeypatch.setattr(entry.time, "time", lambda: now + 31)
    elif change == "proxy_flag": candidate["liquidity_is_proxy"] = 1
    else: candidate["address"] = OTHER
    assert entry.entry_observation_problem(candidate, frozen, paper=True, vector=vector) is not None


@pytest.mark.asyncio
async def test_entry_collects_fast_activity_even_after_price_and_liquidity_are_complete(monkeypatch):
    calls = []
    async def jupiter(*args, **kwargs):
        calls.append("jupiter")
        return stamp_market_observation({"address": MINT, "price_usd": 2}, "jupiter")
    async def birdeye(address, *, force_refresh):
        assert force_refresh
        calls.append("birdeye")
        return stamp_market_observation({"address": MINT, "price_usd": 3, "liquidity_usd": 0,
            "market_cap_usd": 50000, "volume_24h_usd": 2000}, "birdeye")
    async def dex(address, *, force_refresh):
        assert force_refresh
        calls.append("dex")
        return observed()
    async def forbidden(*args, **kwargs): pytest.fail("entry collection quoted a different diagnostic size")
    monkeypatch.setattr(price_service, "get_jupiter_price_snapshot", jupiter)
    monkeypatch.setattr(price_service, "_USE_BIRDEYE", True)
    monkeypatch.setattr(price_service, "_USE_JUPITER_PRICE", True)
    monkeypatch.setattr(price_service, "_jup_get_usd_price", forbidden)
    monkeypatch.setattr(price_service, "_attach_jupiter_impact", forbidden)
    monkeypatch.setattr(price_service.birdeye, "get_token_info", birdeye)
    monkeypatch.setattr(price_service.dexscreener, "get_pair", dex)
    simple_cache.cache_set(f"price:{MINT}:0:0", False, ttl=999)
    tick = await price_service.get_entry_snapshot(MINT)
    assert calls == ["jupiter", "birdeye", "dex"]
    assert tick["price_usd"] == 2 and tick["liquidity_usd"] == 0
    assert tick["txns_last_5m"] == 100 and tick["price_pct_5m"] == 2000
    assert tick["market_observation"]["fields"]["liquidity_usd"]["source"] == "birdeye"


@pytest.mark.asyncio
async def test_entry_missing_optional_fields_uses_one_collection_pass_not_full_chain_retry(monkeypatch):
    monkeypatch.setattr(price_service, "_RETRY_ON_FAIL", 5)
    monkeypatch.setattr(price_service, "_USE_BIRDEYE", False)
    monkeypatch.setattr(price_service, "_USE_JUPITER_PRICE", False)
    requests = []
    async def dex(address, *, force_refresh):
        requests.append((address, force_refresh))
        return stamp_market_observation({"address": MINT, "price_usd": 2}, "dexscreener")
    monkeypatch.setattr(price_service.dexscreener, "get_pair", dex)
    tick = await price_service.get_entry_snapshot(MINT)
    assert requests == [(MINT, True)] and tick["holders"] is None and tick["price_pct_5m"] is None
    assert entry.prepare_entry_candidate({"address": MINT, "holders": 99}, tick)["holders"] is None


def preparation_namespace(snapshot):
    node = deepcopy(run_function("_evaluate_and_buy"))
    last = next(index for index, item in enumerate(node.body) if isinstance(item, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "entry_observation" for target in item.targets))
    node.name = "prepare"
    node.body = node.body[:last+2] + [ast.Return(value=ast.Tuple(elts=[ast.Name(id="token", ctx=ast.Load()),
                ast.Name(id="entry_observation", ctx=ast.Load())], ctx=ast.Load()))]
    node.body = [item for item in node.body if not isinstance(item, ast.Global)]
    waits = []
    source = AsyncMock(return_value=snapshot)
    namespace = {"SessionLocal": object, "sanitize_token_data": sanitize_token_data,
        "discovery_candidate": entry.discovery_candidate, "prepare_entry_candidate": entry.prepare_entry_candidate,
        "freeze_entry_observation": entry.freeze_entry_observation, "time": time,
        "_stats": {"raw_discovered": 0, "filtered_out": 0, "incomplete": 0},
        "_stream_candidate_is_cooled": lambda *a: False, "_in_trading_window": lambda: True,
        "filters": SimpleNamespace(effective_require_jupiter_for_buy=lambda *a: False), "_REQUIRE_JUP_FOR_BUY": False,
        "lista_pares": SimpleNamespace(meta=lambda *a: {"first_seen": time.time(), "attempts": 0}),
        "_norm_dex_id": lambda v: str(v or ""), "_remember_queue_context": lambda *a: None,
        "entry_sizing": sizing, "CFG": SimpleNamespace(), "MIN_AGE_MIN": 1,
        "_candidate_age_minutes": lambda *a: 0, "_log_token": lambda *a: None, "BANNED_CREATORS": set(),
        "select": lambda *a: SimpleNamespace(where=lambda *a: None),
        "Position": SimpleNamespace(address="column", closed=SimpleNamespace(is_=lambda *a: None)),
        "_pf_can_try_now": lambda *a: True, "_PUMPFUN_PRICE_USE_GECKO": False,
        "_GECKO_MIN_QUEUE_ATTEMPTS": 2, "_GECKO_MIN_QUEUE_AGE_S": 90,
        "price_service": SimpleNamespace(get_entry_snapshot=source), "DRY_RUN": True,
        "_maybe_apply_green_sniper_liquidity_proxy": AsyncMock(return_value=False),
        "_maybe_apply_paper_sniper_liquidity_proxy": AsyncMock(return_value=False),
        "_defer_entry_observation": lambda token, **kwargs: waits.append(kwargs),
        "_entry_observation_is_current": lambda token, observation, **kwargs:
            entry.entry_observation_problem(token, observation, paper=True) is None,
        "log": SimpleNamespace(debug=lambda *a: None)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "run_bot.py", "exec"), namespace)
    return namespace, waits, source


@pytest.mark.asyncio
async def test_real_entry_prefix_uses_current_signals_and_original_receipts_for_any_source():
    tick = observed(price_pct_5m=-50)
    namespace, waits, source = preparation_namespace(tick)
    queued = {"address": MINT, "discovered_via": "pumpfun", "score_total": 100,
        "price_usd": 999, "price_pct_5m": 9000, "liquidity_usd": 90000,
        "market_cap": 999999, "has_jupiter_route": 1, "rug_score": 0}
    candidate, frozen = await namespace["prepare"](queued, SimpleNamespace(scalar=AsyncMock(return_value=None)))
    vector = build_feature_vector(candidate).to_dict()
    assert candidate["price_usd"] == 2 and vector["price_pct_5m"] == -50 and vector["liquidity_usd"] == 5000
    assert frozen is not None and not waits and source.await_count == 1
    assert candidate["entry_regime"] == "pump_early" and queued["price_usd"] == 999
    assert candidate["market_observation"] == tick["market_observation"]
    assert "score_total" not in candidate and "has_jupiter_route" not in candidate


@pytest.mark.asyncio
async def test_real_entry_prefix_defers_unavailable_fresh_collection_without_old_input_fallback():
    namespace, waits, source = preparation_namespace(None)
    assert await namespace["prepare"]({"address": MINT, "price_usd": 900, "liquidity_usd": 90000},
        SimpleNamespace(scalar=AsyncMock(return_value=None))) is None
    assert waits == [{"reason": "snapshot_unavailable", "stage": "entry_snapshot"}]
    assert source.await_count == 1 and namespace["_stats"]["filtered_out"] == 0


@pytest.mark.asyncio
async def test_real_entry_prefix_unknown_liquidity_defers_without_negative_policy_label():
    namespace, waits, source = preparation_namespace(observed(liquidity_usd=None))
    assert await namespace["prepare"]({"address": MINT}, SimpleNamespace(scalar=AsyncMock(return_value=None))) is None
    assert waits == [{"reason": "liquidity_unverified", "stage": "entry_snapshot"}]
    assert source.await_count == 1 and namespace["_stats"]["filtered_out"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expiry", "vector", "none"])
async def test_actual_pre_buy_guard_precedes_buy_limiter_and_intent(monkeypatch, change):
    candidate = entry.prepare_entry_candidate({"address": MINT}, observed())
    frozen = entry.freeze_entry_observation(candidate, paper=True)
    vector = build_feature_vector(candidate)
    if change == "expiry":
        now = time.time()
        monkeypatch.setattr(entry.time, "time", lambda: now + 31)
    elif change == "vector": vector["price_pct_5m"] = 9000
    function = run_function("_evaluate_and_buy")
    begin = next(index for index, node in enumerate(function.body) if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "final_vec_payload" for t in node.targets))
    calls, waits = [], []
    namespace = {"vec": vector, "token": candidate, "entry_observation": frozen,
        "_entry_observation_is_current": lambda token, observation, **kwargs:
            entry.entry_observation_problem(token, observation, paper=True, vector=kwargs.get("vector")) is None}
    def limiter(): calls.append("buy_limiter")
    def intent(): calls.append("buy_intent")
    namespace.update(limiter=limiter, intent=intent)
    wrapper = ast.AsyncFunctionDef(name="check", args=ast.arguments(posonlyargs=[], args=[],
        kwonlyargs=[], kw_defaults=[], defaults=[]), body=function.body[begin:begin+2] + [
        ast.Expr(value=ast.Call(func=ast.Name(id="limiter", ctx=ast.Load()), args=[], keywords=[])),
        ast.Expr(value=ast.Call(func=ast.Name(id="intent", ctx=ast.Load()), args=[], keywords=[]))], decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "run_bot.py", "exec"), namespace)
    await namespace["check"]()
    assert calls == (["buy_limiter", "buy_intent"] if change == "none" else [])
    source = ast.unparse(function)
    assert source.index("stage='pre_buy'") < source.index("_BUY_LIMITER.allow()") < source.index("_BUY_RECOVERY.begin(")


def test_real_defer_clears_vector_waits_and_requeues_without_creating_negative_target():
    events, queue = [], []
    namespace = {"_pending_ai_vectors": {MINT: {"stale": True}},
        "_research_decision": lambda *a, **kwargs: events.append(kwargs),
        "_ensure_requeue_with_stats": lambda *a, **kwargs: queue.append(kwargs)}
    exec(compile(ast.Module(body=[run_function("_defer_entry_observation")], type_ignores=[]), "run_bot.py", "exec"), namespace)
    namespace["_defer_entry_observation"]({"address": MINT}, reason="expired_inputs", stage="pre_buy")
    assert not namespace["_pending_ai_vectors"]
    assert events[0]["action"] == "wait" and queue[0]["backoff"] == 5
    assert events[0]["reason"] == "entry_observation:expired_inputs"


def test_freshness_wait_preserves_retry_budget_even_if_env_customizes_other_reasons(monkeypatch):
    monkeypatch.setattr(lista_pares, "_pair_watch", {MINT: {"retries": 1, "attempts": 0,
        "first_seen": time.time(), "next_try": time.time()}})
    monkeypatch.setattr(lista_pares, "NON_DECREMENT_REASON_PREFIXES", ())
    monkeypatch.setattr(lista_pares, "log_queue_requeue", lambda *a, **k: None)
    for _ in range(8): assert lista_pares.requeue(MINT, reason="entry_observation:expired_inputs", backoff=5)
    assert lista_pares.retries_left(MINT) == 1 and lista_pares.meta(MINT)["attempts"] == 8


@pytest.mark.parametrize("drop", ["ready_expiry", "requeue_expiry", "capacity"])
def test_receipt_wait_is_bounded_but_expiry_does_not_permanently_blacklist(monkeypatch, drop):
    now = time.time()
    monkeypatch.setattr(lista_pares, "_pair_watch", {MINT: {"retries": 1, "attempts": 0,
        "first_seen": now - 601, "next_try": now, "reason": "entry_observation:expired_inputs"}})
    monkeypatch.setattr(lista_pares, "_processed", set())
    monkeypatch.setattr(lista_pares, "MAX_INCOMPLETE_SEC", 600)
    monkeypatch.setattr(lista_pares, "MAX_QUEUE_SIZE", 1)
    monkeypatch.setattr(lista_pares, "_persist", lambda *a: pytest.fail("temporary observation wait became permanent"))
    for name in ("log_queue_add", "log_queue_requeue", "log_queue_drop"):
        monkeypatch.setattr(lista_pares, name, lambda *a, **k: None)
    if drop == "ready_expiry": assert lista_pares.obtener_pares() == []
    elif drop == "requeue_expiry": assert lista_pares.requeue(MINT, reason="entry_observation:expired_inputs", backoff=5)
    else: assert lista_pares.agregar_si_nuevo(OTHER)
    assert MINT not in lista_pares._pair_watch and MINT not in lista_pares._processed


@pytest.mark.asyncio
async def test_actual_queue_consumer_dispatches_identity_without_exploratory_nil_gate():
    nested = next(node for node in ast.walk(run_function("main_loop"))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "validate_address")
    calls = []
    async def guarded(token, session, *, source): calls.append((token, session, source))
    namespace = {"_evaluate_and_buy_guarded": guarded, "log": SimpleNamespace(error=lambda *a: None)}
    exec(compile(ast.Module(body=[nested], type_ignores=[]), "run_bot.py", "exec"), namespace)
    await namespace["validate_address"](MINT)
    assert calls == [({"address": MINT}, None, "queue")]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [None, False, True])
async def test_actual_sniper_route_proxy_never_turns_unknown_route_into_true(route):
    requests = []
    async def probe(address, amount):
        requests.append(amount)
        return {"has_route": route, "price_impact_pct": None}
    namespace = {"DRY_RUN": True, "_PUMP_EARLY_SNIPER_ENABLED": True,
        "_PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_ENABLED": True,
        "_PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_MIN_AGE_MIN": 0,
        "_PUMP_EARLY_SNIPER_PAPER_ROUTE_PROXY_LIQUIDITY_USD": 1200,
        "_PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD": 1000,
        "_PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT": 10, "_candidate_age_minutes": lambda *a: 1,
        "_probe_jupiter_route": probe, "_entry_probe_amount_sol": lambda: .1,
        "apply_paper_liquidity_proxy": entry.apply_paper_liquidity_proxy,
        "log": SimpleNamespace(info=lambda *a: None)}
    exec(compile(ast.Module(body=[run_function("_maybe_apply_paper_sniper_liquidity_proxy")], type_ignores=[]), "run_bot.py", "exec"), namespace)
    token = entry.prepare_entry_candidate({"address": MINT}, observed(liquidity_usd=None))
    token["entry_regime"] = "pump_early"
    allowed = await namespace["_maybe_apply_paper_sniper_liquidity_proxy"](token, MINT)
    assert allowed is (route is True) and requests == [.1]
    if route is not True: assert token.get("has_jupiter_route") is None and token["liquidity_usd"] is None


@pytest.mark.parametrize("paper", [True, False])
def test_actual_probe_amount_matches_configured_exact_size(paper):
    namespace = {"DRY_RUN": paper, "CFG": SimpleNamespace(PAPER_EXACT_TRADE_SIZE_ENABLED=True,
        PAPER_EXACT_TRADE_SIZE_SOL=.1), "TRADE_AMOUNT_SOL_CFG": .1, "MIN_BUY_SOL": .01}
    exec(compile(ast.Module(body=[run_function("_entry_probe_amount_sol")], type_ignores=[]), "run_bot.py", "exec"), namespace)
    assert namespace["_entry_probe_amount_sol"]() == .1
