"""Actual route consumers with isolated HTTP/clock doubles, never live markets."""
from __future__ import annotations

import ast
import asyncio
import copy
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from execution.quote_observation import NO_ROUTE_CODES, observe_quote, rejection_metadata
from fetcher import jupiter_router as router
from utils.raw_units import sol_to_lamports
from utils.market_observation import fresh_market_value, stamp_market_observation
from test_entry_observation import run_function
from test_jupiter_quote_contract import AMOUNT, SOL, TOKEN, Response, checked, network, payload
from test_live_quote_admission import guarded


def observe(quote, **changes):
    args = {"input_mint": SOL, "output_mint": TOKEN, "amount": AMOUNT, "slippage": 100}
    args.update(changes)
    return observe_quote(quote, **args)


def test_positive_receipt_keeps_original_time_and_independent_evidence_limits():
    quote = checked(payload())
    before = copy.deepcopy(quote.other)
    result = observe(quote)
    assert result.has_route is True and result.in_amount == AMOUNT and result.out_amount == 2_000_000
    assert result.price_impact_bps == 1 and result.route_count == 1
    assert quote.other == before and quote.other["market_asof_verified"] is False and quote.other["fill_verified"] is False


@pytest.mark.parametrize("age", [-1, 10.01, 60, 900])
def test_raw_revalidation_does_not_refresh_an_expired_or_future_receipt(age):
    quote = checked(payload())
    now = dt.datetime.now(dt.timezone.utc)
    quote.other["received_at_utc"] = (now - dt.timedelta(seconds=age)).isoformat()
    before = quote.other["received_at_utc"]
    assert observe(quote, now=now).has_route is None
    assert quote.other["received_at_utc"] == before


@pytest.mark.parametrize("field,value", [("received_at_utc", None), ("received_at_utc", "2026-10-08T00:00:00"),
    ("requested_in_amount", True), ("requested_in_amount", 100_000_001), ("inputMint", TOKEN),
    ("outputMint", "OtherMint"), ("slippageBps", True), ("slippageBps", 101),
    ("onlyDirectRoutes", 0), ("quote_contract_version", True), ("routePlan_len", True),
    ("routePlan_len", 0), ("contextSlot", -1), ("market_asof_verified", True), ("fill_verified", True)])
def test_positive_receipt_metadata_cannot_override_raw_request(field, value):
    quote = checked(payload())
    quote.other[field] = value
    assert observe(quote).has_route is None


@pytest.mark.parametrize("field,value", [("inputMint", TOKEN), ("outputMint", SOL), ("inAmount", "1"),
    ("outAmount", "1"), ("slippageBps", 99), ("priceImpactPct", None), ("routePlan", []),
    ("errorCode", "NO_ROUTES_FOUND")])
def test_mutated_or_mixed_success_payload_is_unknown_not_a_negative(field, value):
    quote = checked(payload())
    quote.raw[field] = value
    assert observe(quote).has_route is None


class ErrorResponse(Response):
    def __init__(self, body, status=400, *, wire=None):
        super().__init__(body, status)
        self.wire = json.dumps(body).encode() if wire is None else wire
        self.offset, self.reads = 0, []
        self.content = self

    async def read(self, count):
        self.reads.append(count)
        chunk = self.wire[self.offset:self.offset + count]
        self.offset += len(chunk)
        return chunk


@pytest.mark.asyncio
@pytest.mark.parametrize("code", sorted(NO_ROUTE_CODES))
async def test_actual_gateway_rejection_is_scoped_to_current_exact_request(network, code):
    calls, replies = network
    replies.append(ErrorResponse({"errorCode": code, "error": "synthetic private text omitted"}, 400))
    quote = await router.get_quote(input_mint=SOL, output_mint=TOKEN, amount_sol=.1)
    observation = observe(quote)
    assert observation.has_route is False and observation.reason == code.lower()
    assert quote.raw == {"errorCode": code} and len(calls) == 1
    assert "synthetic private text" not in json.dumps(quote.other)
    assert observe(quote, amount=AMOUNT + 1).has_route is None
    assert observe(quote, output_mint=SOL).has_route is None
    stamp = dt.datetime.fromisoformat(quote.other["received_at_utc"])
    assert observe(quote, now=stamp + dt.timedelta(seconds=11)).has_route is None


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [({}, 400), ({"error": "NO_ROUTES_FOUND"}, 400),
    ({"errorCode": "UNKNOWN_ERROR"}, 400), ({"errorCode": True}, 400),
    ({"errorCode": "NO_ROUTES_FOUND", "outAmount": "1"}, 400),
    ({"errorCode": "NO_ROUTES_FOUND", "error": None}, 400),
    ({"errorCode": "NO_ROUTES_FOUND"}, 200),
    ({"errorCode": "NO_ROUTES_FOUND"}, 401), ({"errorCode": "NO_ROUTES_FOUND"}, 403),
    ({"errorCode": "NO_ROUTES_FOUND"}, 429), ({"errorCode": "NO_ROUTES_FOUND"}, 500)])
async def test_http_errors_are_not_automatically_negative_routes(network, body, status):
    calls, replies = network
    response = ErrorResponse(body, status)
    replies.append(response)
    quote = await router.get_quote(input_mint=SOL, output_mint=TOKEN, amount_sol=.1)
    assert observe(quote).has_route is None and len(calls) == 1
    if status != 400:
        assert not response.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [b'{"errorCode":"NO_ROUTES_FOUND","errorCode":"TOKEN_NOT_TRADABLE"}',
    b'{"errorCode":"NO_ROUTES_FOUND",', b'[' + b' ' * 65536 + b']', b'\xff'],
    ids=["duplicate", "truncated", "oversized", "invalid-utf8"])
async def test_negative_envelope_is_bounded_duplicate_free_and_not_partial_evidence(network, wire):
    _, replies = network
    response = ErrorResponse({}, wire=wire)
    replies.append(response)
    quote = await router.get_quote(input_mint=SOL, output_mint=TOKEN, amount_sol=.1)
    assert observe(quote).has_route is None and response.offset <= 65537


def test_custom_source_or_unbound_rejection_flags_are_unknown():
    args = dict(status=400, input_mint=SOL, output_mint=TOKEN, amount=AMOUNT, slippage=100, direct=False)
    assert rejection_metadata({"errorCode": "NO_ROUTES_FOUND"}, url="https://custom.invalid/quote", **args) is None
    quote = router.QuoteResult(False, None, None, None, {"errorCode": "NO_ROUTES_FOUND"}, {"errorCode": "NO_ROUTES_FOUND"})
    assert observe(quote).has_route is None


def test_metadata_boolean_cannot_impersonate_raw_slot_one_and_invalid_clock_is_unknown():
    body = payload()
    body["contextSlot"] = 1
    quote = checked(body)
    assert observe(quote).has_route is True
    quote.other["contextSlot"] = True
    assert observe(quote).has_route is None
    assert observe(checked(payload()), now="not-a-clock").has_route is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "no_route", "budget", "network", "malformed", "stale", "cancel"])
async def test_actual_run_probe_uses_checked_exact_quote_and_no_extra_price_request(network, fault):
    calls, replies = network
    if fault == "no_route": response = ErrorResponse({"errorCode": "NO_ROUTES_FOUND"})
    elif fault == "network": response = Response(TimeoutError())
    elif fault == "malformed": response = Response({})
    elif fault == "cancel": response = Response(asyncio.CancelledError())
    else: response = Response(payload())
    replies.append(response)
    source = router
    if fault in {"budget", "stale"}:
        quote = checked(payload()) if fault == "stale" else router.QuoteResult(False, None, None, None,
            {"quote_contract_error": "provider_budget_unavailable"}, {})
        if fault == "stale": quote.other["received_at_utc"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)).isoformat()
        source = SimpleNamespace(routing_quote_slippage_bps=lambda: 100, get_routing_quote=AsyncMock(return_value=quote))
    price = AsyncMock(side_effect=AssertionError("unused price request"))
    namespace = {"Dict": dict, "Any": object, "Optional": __import__("typing").Optional,
        "_JUP_ROUTER_AVAILABLE": True, "jupiter": source,
        "jupiter_price": SimpleNamespace(get_price=price), "observe_quote": observe_quote,
        "sol_to_lamports": sol_to_lamports}
    exec(compile(ast.Module(body=[run_function("_probe_jupiter_route")], type_ignores=[]), "run_bot.py", "exec"), namespace)
    if fault == "cancel":
        with pytest.raises(asyncio.CancelledError): await namespace["_probe_jupiter_route"](TOKEN, .1)
    else:
        result = await namespace["_probe_jupiter_route"](TOKEN, .1)
        assert result["has_route"] is (True if fault == "none" else False if fault == "no_route" else None)
        assert result["price_impact_pct"] == (.01 if fault == "none" else None)
    price.assert_not_awaited()
    if calls: assert len(calls) == 1 and calls[0][1]["amount"] == str(AMOUNT)


@pytest.mark.asyncio
@pytest.mark.parametrize("required,bootstrap,route", [(True, False, None), (False, True, None),
    (False, False, None), (True, False, True), (True, False, False)])
async def test_actual_early_route_guard_defers_before_bootstrap_or_negative_features(required, bootstrap, route):
    function = run_function("_evaluate_and_buy")
    start = next(i for i, node in enumerate(function.body) if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "route_probe" for t in node.targets))
    stop = next(i for i in range(start + 1, len(function.body)) if isinstance(function.body[i], ast.If)
        and isinstance(function.body[i].test, ast.Name) and function.body[i].test.id == "moonshot_fast_path")
    deferred, passed, token = [], [], {"address": TOKEN}
    wrapper = ast.AsyncFunctionDef(name="guard", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
        kw_defaults=[], defaults=[]), body=function.body[start:stop] + [ast.Expr(value=ast.Call(
        func=ast.Name(id="passed", ctx=ast.Load()), args=[], keywords=[]))], decorator_list=[])
    namespace = {"token": token, "addr": TOKEN, "require_jup_for_buy": required,
        "paper_bootstrap_fast_path": bootstrap, "CFG": SimpleNamespace(PAPER_BOOTSTRAP_REQUIRE_ROUTE=True),
        "_entry_probe_amount_sol": lambda: .1, "_probe_jupiter_route": AsyncMock(return_value={"has_route": route}),
        "_defer_entry_observation": lambda *a, **kw: deferred.append(kw), "passed": lambda: passed.append(True)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "run_bot.py", "exec"), namespace)
    await namespace["guard"]()
    if (required or bootstrap) and route is None:
        assert deferred == [{"reason": "route_unverified", "stage": "route_probe"}] and not passed
    else: assert not deferred and passed
    assert token["has_jupiter_route"] is None if route is None else token["has_jupiter_route"] == int(route)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [None, False, True])
async def test_actual_final_route_guard_does_not_bypass_or_consume_retry_budget_for_unknown(route):
    function = run_function("_evaluate_and_buy")
    guard = next(node for node in ast.walk(function) if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name) and node.test.id == "require_jup_for_buy"
        and "route_unverified" in ast.unparse(node))
    waits, requeues, namespace = [], [], {"has_route": route, "addr": TOKEN, "token": {"address": TOKEN},
        "require_jup_for_buy": True, "proba": .5, "ai_threshold_eff": .5, "rank_info": {},
        "log": SimpleNamespace(info=lambda *a: None), "_pending_ai_vectors": {}, "_research_decision": lambda *a, **k: None,
        "_defer_entry_observation": lambda *a, **k: waits.append(k), "_requeue_with_stats": lambda *a, **k: requeues.append(k)}
    wrapper = ast.AsyncFunctionDef(name="guard", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
        kw_defaults=[], defaults=[]), body=[copy.deepcopy(guard)], decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "run_bot.py", "exec"), namespace)
    await namespace["guard"]()
    assert bool(waits) is (route is None) and bool(requeues) is (route is False)
    if waits: assert waits[0] == {"reason": "route_unverified", "stage": "execution_guard"}


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "unbound", "stale", "wrong_source", "none"])
async def test_actual_required_price_guard_defers_unknown_without_shadow_or_permanent_cooldown(fault):
    function = run_function("_evaluate_and_buy")
    guard = next(node for node in ast.walk(function) if isinstance(node, ast.If)
        and ast.unparse(node.test).startswith("fresh_market_value(jtok,")
        and "jupiter_price_missing" in ast.unparse(node))
    snapshot = stamp_market_observation({"address": TOKEN, "price_usd": 2}, "jupiter")
    if fault == "missing": snapshot = None
    elif fault == "unbound": snapshot.pop("market_observation")
    elif fault == "stale": snapshot["market_observation"]["fields"]["price_usd"]["received_at"] -= 900
    elif fault == "wrong_source": snapshot["market_observation"]["fields"]["price_usd"]["source"] = "dexscreener"
    waits, namespace = [], {"jtok": snapshot, "addr": TOKEN, "token": {"address": TOKEN},
        "fresh_market_value": fresh_market_value, "log": SimpleNamespace(info=lambda *a: None),
        "_defer_entry_observation": lambda *a, **k: waits.append(k)}
    wrapper = ast.AsyncFunctionDef(name="guard", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
        kw_defaults=[], defaults=[]), body=[copy.deepcopy(guard)], decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "run_bot.py", "exec"), namespace)
    await namespace["guard"]()
    assert bool(waits) is (fault != "none")
    if waits: assert waits == [{"reason": "jupiter_price_missing", "stage": "execution_guard"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,signature", [("HIGH_QUOTE_IMPACT", "HIGH_IMPACT"), ("IMPACT_LIMIT_UNKNOWN", "INVALID_IMPACT_LIMIT")])
async def test_actual_paper_quote_policy_rejection_is_not_misreported_as_temporary_no_route(monkeypatch, tmp_path, status, signature):
    from trader import papertrading as paper
    from test_buy_recovery import configure_paper, MINT
    configure_paper(monkeypatch, tmp_path)
    monkeypatch.setattr(paper, "_has_jupiter_route", AsyncMock(return_value=(False, status)))
    result = await paper.buy(MINT, .1, require_jupiter_for_buy=True)
    assert result["qty_lamports"] == 0 and result["signature"] == signature and not paper._PORTFOLIO


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429])
async def test_actual_live_code_quote_to_durable_no_send_to_full_decision_deferral(network, guarded, tmp_path, status):
    """Live-code integration only: fake HTTP/funds/price, no signer/order/RPC."""
    from runtime.buy_recovery import BuyRecoveryStore
    from trader import buyer
    from test_buy_recovery import execution_tail_namespace, MINT
    calls, replies = network
    replies.append(ErrorResponse({"errorCode": "NO_ROUTES_FOUND"}, status))
    store = BuyRecoveryStore(tmp_path / "journal")
    namespace = execution_tail_namespace(tmp_path, store, buyer)
    waits, queues = [], []
    namespace.update(DRY_RUN=False, _research_decision=lambda *a, **k: waits.append(k),
        _ensure_requeue_with_stats=lambda *a, **k: queues.append(k),
        _remove_from_queue_if_present=lambda *a: pytest.fail("proved live-code no-send removed candidate"),
        strategy_runtime=SimpleNamespace(record_execution=lambda *a: pytest.fail("preparation entered financial feedback")))
    exec(compile(ast.Module(body=[run_function("_defer_entry_observation")], type_ignores=[]), "run_bot.py", "exec"), namespace)
    with store.scope(): await namespace["execution_tail"]({"address": MINT}, SimpleNamespace())
    assert len(calls) == 1 and calls[0][1]["amount"] == str(AMOUNT) and calls[0][1]["outputMint"] == MINT
    assert not guarded[0].called and not guarded[1].called and not store.pending_addresses
    assert waits[0]["action"] == "wait" and waits[0]["reason"] == "entry_observation:buy_preparation:no_jup_route"
    assert queues[0]["backoff"] == 5
    durable = json.loads(next((store.directory / "resolved").glob("*.json")).read_text())
    assert durable["state"] == "no_fill" and durable["rejection"] == "NO_JUP_ROUTE" and durable["paper"] is False
    assert "execution" not in durable and durable["amount_sol"] == .1
