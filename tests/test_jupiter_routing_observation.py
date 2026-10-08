"""All-router observations with fake HTTP and isolated portfolios only."""
import ast
import asyncio
import copy
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from execution.quote_observation import observe_quote
from execution.quote_receipt import capture_summary, valid_summary, public_summary
from execution.jupiter_quote_v2 import QuoteRequest
from fetcher import jupiter_router as router
from quote_fixtures import SOL, TOKEN, v2_body, v2_quote
from test_entry_observation import run_function
from test_paper_archive import paper
from trader.papertrading import _has_jupiter_route as actual_paper_route

T0 = dt.datetime(2026, 10, 8, 12, tzinfo=dt.timezone.utc)


def observed(q, **changes):
    return observe_quote(q, **{"input_mint": SOL, "output_mint": TOKEN, "amount": 100000000,
        "slippage": router.routing_quote_slippage_bps(), "now": T0, **changes})


@pytest.mark.parametrize("family", ["metis", "jupiterz", "dflow", "okx"])
def test_four_families_have_honest_step_counts_no_transaction_and_original_receipts(family):
    q = v2_quote(now=T0, family=family)
    original = copy.deepcopy(q.other)
    result = observed(q)
    assert result.has_route is True and result.protocol == "swap_v2" and result.router == family
    assert result.route_count == (1 if family == "metis" else 0)
    assert q.other == original and q.other["transaction_available"] is False
    assert observed(q, now=T0 + dt.timedelta(seconds=11)).has_route is None
    receipt = capture_summary(q, input_mint=SOL, output_mint=TOKEN, amount=100000000,
        slippage=router.routing_quote_slippage_bps(), limit=3, now=T0)
    assert valid_summary(receipt, input_mint=SOL, output_mint=TOKEN, amount=100000000, not_after=T0)
    assert not valid_summary(receipt, input_mint=SOL, output_mint=TOKEN, amount=20000000)
    assert not valid_summary(receipt, not_after=T0 - dt.timedelta(seconds=1))


@pytest.mark.parametrize("field,value", [("transaction", "signable"), ("transaction", ""), ("taker", TOKEN),
    ("receiver", TOKEN), ("referralAccount", TOKEN), ("router", "unknown"), ("router", []),
    ("mode", []), ("inAmount", True), ("outAmount", "0"), ("slippageBps", True), ("transactionVersion", 1),
    ("transactionVersion", True), ("priceImpact", float("nan")), ("priceImpact", True),
    ("otherAmountThreshold", "1001"), ("otherAmountThreshold", "1"), ("routePlan", [None]),
    ("expireAt", T0.isoformat()), ("expireAt", "2026-10-08T13:00:00"), ("errorCode", 1)])
def test_bad_observation_never_admits_a_route(field, value):
    body = v2_body()
    body[field] = value
    q = router._checked_v2_quote(body, input_mint=SOL, output_mint=TOKEN,
        amount=100000000, slippage=router.routing_quote_slippage_bps(), now=T0)
    assert not q.ok and observed(q).has_route is None


def test_receipt_whitelist_checksum_expiry_and_signed_improvement_are_not_profit():
    q = v2_quote(now=T0, impact=-25)
    q.raw.update(private_key="NEVER_STORE", priceImpactPct="NEVER_STORE", requestId="NEVER_STORE")
    q.raw["expireAt"] = (T0 + dt.timedelta(seconds=2)).isoformat()
    summary = capture_summary(q, input_mint=SOL, output_mint=TOKEN, amount=100000000,
        slippage=router.routing_quote_slippage_bps(), limit=3, now=T0)
    assert summary["impact_bps"] == -2500 and "NEVER_STORE" not in json.dumps(summary)
    assert observed(q, now=T0 + dt.timedelta(seconds=3)).has_route is None
    assert valid_summary(summary)  # Original historical observation, NOT current routability.
    bad = copy.deepcopy(summary)
    bad["observation_receipt"]["raw"]["outAmount"] = "2000"
    assert not valid_summary(bad)
    with pytest.raises(ValueError): public_summary(bad)
    assert not valid_summary({"in_amount": 100000000, "out_amount": 1000, "route_count": 0,
        "protocol": "swap_v2", "router": "jupiterz"}, allow_legacy=True)


class Response:
    def __init__(self, body, status=200):
        self.status, self.headers, self.offset = status, {}, 0
        self.body = body if isinstance(body, (bytes, BaseException)) else json.dumps(body).encode()
        self.content = self

    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def read(self, size):
        if isinstance(self.body, BaseException): raise self.body
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


@pytest.fixture
def network(monkeypatch):
    from jupiter_access_fixtures import isolate_budget
    isolate_budget(monkeypatch)
    monkeypatch.setattr(router, "JUP_MANAGED_ENABLED", True)
    monkeypatch.setattr(router, "JUP_API_KEY", "")
    monkeypatch.setattr(router, "JUP_ORDER_URL", router._ORDER_URL)
    calls, replies = [], []
    class Session:
        def __init__(self, **kwargs): assert "x-api-key" not in kwargs["headers"]
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def get(self, url, params, **kwargs):
            assert kwargs == {"allow_redirects": False}
            assert "taker" not in params and "payer" not in params
            calls.append((url, params))
            return replies.pop(0)
        def post(self, *args, **kwargs): raise AssertionError("Quote must never POST")
    monkeypatch.setattr(router.aiohttp, "ClientSession", Session)
    return calls, replies


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["metis", "jupiterz", "dflow", "okx"])
async def test_actual_anonymous_get_and_scanner_use_all_routers_without_wallet_or_price(network, family):
    calls, replies = network
    replies.append(Response(v2_body(family=family)))
    ns = {"Dict": dict, "Any": object, "jupiter": router, "_JUP_ROUTER_AVAILABLE": True,
        "sol_to_lamports": __import__("utils.raw_units", fromlist=["sol_to_lamports"]).sol_to_lamports,
        "observe_quote": observe_quote}
    exec(compile(ast.Module(body=[run_function("_probe_jupiter_route")], type_ignores=[]), "run_bot.py", "exec"), ns)
    result = await ns["_probe_jupiter_route"](TOKEN, .1)
    assert result["has_route"] is True and result["route_router"] == family
    assert len(calls) == 1 and calls[0][1]["amount"] == "100000000"
    assert calls[0][1]["maxSupportedTransactionVersion"] == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [({}, 200), ({"error": "no route"}, 400), ({}, 401), ({}, 429),
    (b'{"outAmount":"1","outAmount":"2"}', 200), (b"x" * (2 * 1024 * 1024 + 1), 200),
    (TimeoutError(), 200)], ids=["malformed", "ambiguous_no_route", "auth", "quota", "duplicate", "bound", "network"])
async def test_unknown_http_body_never_falls_back_or_fabricates_no_route(network, body, status):
    calls, replies = network
    replies.append(Response(body, status))
    q = await router.get_routing_quote(input_mint=SOL, output_mint=TOKEN, amount_sol=.1)
    assert q.ok is False and observed(q).has_route is None and len(calls) == 1


@pytest.mark.asyncio
async def test_explicit_disable_uses_legacy_and_cancellation_propagates(network, monkeypatch):
    calls, replies = network
    replies.append(Response(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await router.get_routing_quote(input_mint=SOL, output_mint=TOKEN, amount_sol=.1)
    monkeypatch.setattr(router, "JUP_MANAGED_ENABLED", False)
    legacy = AsyncMock(return_value="legacy-only")
    monkeypatch.setattr(router, "get_quote", legacy)
    assert await router.get_routing_quote(input_mint=SOL, output_mint=TOKEN, amount_sol=.1) == "legacy-only"
    assert legacy.await_count == 1 and len(calls) == 1


def test_request_and_clock_are_strict():
    assert not set(QuoteRequest(SOL, TOKEN, 100000000, 100).params()) & {"taker", "payer", "receiver", "referralAccount"}
    for amount in (True, 1., 0, 2 ** 64):
        with pytest.raises(ValueError): QuoteRequest(SOL, TOKEN, amount, 100)
    assert not router._checked_v2_quote(v2_body(), input_mint=SOL, output_mint=TOKEN,
        amount=100000000, slippage=router.routing_quote_slippage_bps(), now="bad").ok


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["metis", "jupiterz", "dflow", "okx"])
async def test_actual_exact_paper_entry_exit_archive_accept_all_router_receipts(paper, network, family, monkeypatch):
    # Use the actual routing adapter/HTTP checker, not the fixture's shortcut.
    monkeypatch.setattr(paper, "_has_jupiter_route", actual_paper_route)
    calls, replies = network
    monkeypatch.setattr(router, "get_routing_quote", actual_routing_quote)
    replies.append(Response(v2_body(family=family, impact=-12)))
    bought = await paper.buy(TOKEN, .1)
    assert bought["qty_lamports"] == 1000
    entry = paper._PORTFOLIO[TOKEN]
    assert entry["amount_sol"] == .1 and valid_summary(entry["entry_route_quote"])
    replies.append(Response(v2_body(TOKEN, SOL, 1000, 200000000, family=family)))
    sold = await paper.sell(TOKEN, 1000, exit_intent_id="a" * 32)
    assert sold["ok"] and valid_summary(sold["exit_route_quote"])
    assert len(calls) == 2 and calls[1][1]["amount"] == "1000"
    from runtime.paper_archive import read_closed_evidence
    rows, issues = read_closed_evidence(paper._DATA_PATH.parent)
    assert not issues and len(rows) == 1 and valid_summary(rows[0]["entry_route_quote"])
    assert valid_summary(rows[0]["exit_fill_events"][0]["response"]["exit_route_quote"])
    from analytics.forward_evidence import _costed_close
    assert _costed_close(rows[0])[4] is True


actual_routing_quote = router.get_routing_quote


@pytest.mark.asyncio
async def test_actual_reverse_exit_rechecks_fx_after_quote_before_any_money_write(paper, network, monkeypatch):
    monkeypatch.setattr(paper, "_has_jupiter_route", actual_paper_route)
    monkeypatch.setattr(router, "get_routing_quote", actual_routing_quote)
    calls, replies = network
    replies.append(Response(v2_body()))
    assert (await paper.buy(TOKEN, .1))["qty_lamports"] == 1000
    before, saved = copy.deepcopy(paper._PORTFOLIO[TOKEN]), paper._DATA_PATH.read_bytes()
    fx = AsyncMock(side_effect=[100., None])
    monkeypatch.setattr(paper, "get_sol_usd", fx)
    replies.append(Response(v2_body(TOKEN, SOL, 1000, 200000000)))
    result = await paper.sell(TOKEN, 1000, exit_intent_id="b" * 32)
    assert result["ok"] is False and result["qty_sold"] == 0
    assert fx.await_count == 2 and len(calls) == 2
    assert paper._PORTFOLIO[TOKEN] == before and paper._DATA_PATH.read_bytes() == saved


@pytest.mark.parametrize("family", ["metis", "jupiterz", "dflow", "okx"])
def test_runner_research_accepts_checked_opaque_quotes_and_rejects_stripped_proof(tmp_path, family):
    from research_loop import runner_forward as rf
    from test_runner_forward import cfg, entry
    q = v2_quote(now=T0, family=family, impact=-12)
    route = capture_summary(q, input_mint=SOL, output_mint=TOKEN, amount=100000000,
        slippage=router.routing_quote_slippage_bps(), limit=8, now=T0)
    original = entry(token_address=TOKEN, opened_at=T0.isoformat(), run_started_at=T0.isoformat(), entry_route_quote=route)
    case = rf.prepare_partial_case(original, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    assert case is not None
    arm = next(iter(case["arms"].values()))
    stamp = T0 + dt.timedelta(minutes=2)
    arm["intent"] = rf.make_intent(arm["subject"], quantity=800, reason="synthetic_close", now=stamp)
    assert rf.apply_paper_exit_quote(case, arm, v2_quote(TOKEN, SOL, 800, 200000000,
        now=stamp, family=family, impact=-12), 100., stamp, quote_started_at=stamp)
    assert rf.validate_paper_cash_terminal(case, arm, stamp)
    damaged = copy.deepcopy(arm)
    damaged["fills"][0]["route_quote"]["observation_receipt"]["sha256"] = "0" * 64
    assert not rf.validate_paper_cash_terminal(case, damaged, stamp)
    del damaged["fills"][0]["route_quote"]
    assert not rf.validate_paper_cash_terminal(case, damaged, stamp)


@pytest.mark.parametrize("family", ["metis", "jupiterz", "dflow", "okx"])
def test_entry_gate_research_costs_complete_v2_quotes_without_claiming_live_profit(tmp_path, family):
    from research_loop import entry_gate_forward as bank, entry_gate_policy as evaluator, forward_budget as storage
    from test_entry_gate_forward import capture, config, case as read_case, token
    cfg = config()
    mint = token()["address"]
    identity = capture(tmp_path, cfg, token(), now=T0, start=T0)
    entered = T0 + dt.timedelta(seconds=1)
    async def quoted(**kwargs): return v2_quote(SOL, mint, now=entered, family=family, impact=-12)
    async def prices(_): return {mint: 1.}
    async def sol(): return 100.
    assert asyncio.run(bank.fill_entry(identity, root=tmp_path, cfg=cfg, now=entered,
        quote_func=quoted, prices_func=prices, sol_price_func=sol))
    record = read_case(tmp_path, identity)
    quantity = record["cash"]["prefix"]["entry_qty"]
    stamp = T0 + dt.timedelta(minutes=2)
    from research_loop.paper_exit_receipt import make_intent
    record["cash"]["terminal"]["intent"] = make_intent(record["cash"]["terminal"]["subject"],
        quantity=quantity, reason="synthetic_close", now=stamp)
    record["observation_count"] = 2
    storage.write(bank.directory(tmp_path) / "active" / f"{identity}.json", record)
    assert bank.observe_quote(mint, v2_quote(mint, SOL, quantity, 200000000, now=stamp, family=family),
        100., root=tmp_path, cfg=cfg, now=stamp, quote_started_at=stamp) == 1
    closed = read_case(tmp_path, identity, "closed")
    plan = storage.read(bank.directory(tmp_path) / "plans" / f"{record['plan_id']}.json")
    sol_pnl, usd_pnl = evaluator._entry_cash(closed, plan, stamp)
    assert sol_pnl > 0 and usd_pnl > 0  # Synthetic fixture, not an operator outcome.
