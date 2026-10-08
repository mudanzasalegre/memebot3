"""Synthetic own-case cash, original FX and actual entry-research integration."""
import asyncio
import copy
import datetime as dt
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from analytics import exit_policy
from execution import paper_cash_mark as cash
from quote_fixtures import SOL, v2_quote
from research_loop import entry_gate_forward as bank, entry_gate_policy as evaluator, runner_forward as runner
from research_loop.paper_exit_receipt import make_intent
from test_entry_gate_forward import T0, config, token, capture, fill, case, quote, fx, isolated


@pytest.fixture(autouse=True)
def no_real_providers(monkeypatch, isolated):
    from fetcher import jupiter_price, jupiter_router
    from utils import sol_price
    fail = AsyncMock(side_effect=AssertionError("Synthetic entry cash test attempted a provider"))
    monkeypatch.setattr(jupiter_router, "get_routing_quote", fail)
    monkeypatch.setattr(jupiter_price, "get_many_usd_prices", fail)
    monkeypatch.setattr(sol_price, "get_sol_usd", fail)
    monkeypatch.setattr(sol_price, "get_sol_usd_observation", fail)
    runner._ACTIVE_INDEX.clear()


def prepared(root):
    cfg = config()
    identity = capture(root, cfg)
    assert identity and fill(root, cfg, identity)
    return cfg, identity


def plan(root, record):
    return bank.storage.read(bank.directory(root) / "plans" / f"{record['plan_id']}.json")


def persist(root, record):
    bank.storage.write(bank.directory(root) / "active" / f"{record['case_id']}.json", record)


def test_new_entry_retains_original_typed_fx_request_clock_and_own_identity(tmp_path):
    _, identity = prepared(tmp_path)
    record = case(tmp_path, identity)
    prefix = record["cash"]["prefix"]
    opened = T0 + dt.timedelta(seconds=1)
    assert prefix["entry_fx_observation"] == fx(opened).to_dict()
    assert prefix["entry_quote_started_at"] == opened.isoformat()
    row, arm_id = bank.cash_case(record)
    assert prefix["paper_cash_owner"] == "case:" + runner._hash([identity, arm_id])
    assert cash.basis(prefix, token=record["token"], owner=prefix["paper_cash_owner"])["amount_sol"] == .1
    assert record["financial_policy_version"] == plan(tmp_path, record)["financial_policy_version"] == cash.VERSION
    assert bank.has_quote_demand(tmp_path) and bank._valuation_quoteable(record)


def test_raw_spot_extreme_cannot_create_financial_exit_or_peak(tmp_path):
    cfg, identity = prepared(tmp_path)
    assert bank.observe_market(token()["address"], 1000000., root=tmp_path, cfg=cfg,
                               now=T0 + dt.timedelta(minutes=2)) == 1
    terminal = case(tmp_path, identity)["cash"]["terminal"]
    assert not terminal.get("intent") and not terminal.get("cash_last_mark")
    assert terminal["subject"]["highest_pnl_pct"] == 0.


@pytest.mark.parametrize("fault", ["scalar", "missing", "stale", "assumed", "bool", "disagree"])
def test_original_entry_fx_unknown_does_not_create_a_cash_prefix(tmp_path, fault):
    cfg = config()
    identity = capture(tmp_path, cfg)
    stamp = T0 + dt.timedelta(seconds=1)
    async def prices(mints): return {mint: 1. for mint in mints}
    async def scalar(): return 100.
    async def quoted(**kw): return quote(source=kw["input_mint"], target=kw["output_mint"], now=stamp)
    rate = {"scalar": 100., "missing": None, "stale": fx(stamp, received_at=stamp.timestamp() - 61),
            "assumed": fx(stamp, assumed=True), "bool": fx(stamp, price_usd=True), "disagree": fx(stamp, price_usd=101.)}[fault]
    async def original_fx(): return rate
    assert not asyncio.run(bank.fill_entry(identity, root=tmp_path, cfg=cfg, now=stamp,
        quote_func=quoted, prices_func=prices, sol_price_func=scalar, fx_func=original_fx))
    invalid = case(tmp_path, identity, "invalid")
    assert invalid["cash"] is None and not invalid["outcomes_complete"]


@pytest.mark.parametrize("fault", ["pre_request", "entry_generation", "plan_generation"])
def test_entry_quote_must_follow_original_request_and_unchanged_registration(tmp_path, fault):
    cfg = config()
    identity = capture(tmp_path, cfg)
    stamp = T0 + dt.timedelta(seconds=1)
    async def prices(mints): return {mint: 1. for mint in mints}
    async def scalar(): return 100.
    async def original_fx(): return fx(stamp)
    async def quoted(**kw):
        if fault == "entry_generation":
            record = case(tmp_path, identity)
            record["features"]["rank_score"] += 1
            persist(tmp_path, record)
        if fault == "plan_generation":
            record = case(tmp_path, identity)
            original = plan(tmp_path, record)
            original["financial_policy_version"] = "changed"
            bank.storage.write(bank.directory(tmp_path) / "plans" / f"{record['plan_id']}.json", original)
        received = stamp - dt.timedelta(seconds=1) if fault == "pre_request" else stamp
        return quote(source=kw["input_mint"], target=kw["output_mint"], now=received)
    assert not asyncio.run(bank.fill_entry(identity, root=tmp_path, cfg=cfg, now=stamp,
        quote_func=quoted, prices_func=prices, sol_price_func=scalar, fx_func=original_fx))
    assert case(tmp_path, identity, "invalid")["cash"] is None


def test_entry_tick_values_then_requests_a_separate_partial_quote(tmp_path, monkeypatch):
    monkeypatch.setattr(exit_policy, "CFG", replace(exit_policy.CFG, TP_PARTIAL_ENABLED=True,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=True, BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED=True))
    cfg, identity = prepared(tmp_path)
    stamp, calls = T0 + dt.timedelta(minutes=2), []
    async def quoted(**kw):
        calls.append(kw)
        return quote(quantity=kw["amount_lamports"], output=kw["amount_lamports"] * 1000000,
                     source=kw["input_mint"], now=stamp)
    async def original_fx(): return fx(stamp)
    async def scalar(): return 100.
    result = asyncio.run(bank.tick(root=tmp_path, cfg=cfg, now=stamp,
        quote_func=quoted, fx_func=original_fx, sol_price_func=scalar))
    terminal = case(tmp_path, identity)["cash"]["terminal"]
    assert result["quote_calls"] == len(calls) == 1
    assert terminal["cash_observation_count"] == 1 and not terminal["fills"]
    quantity = terminal["intent"]["quantity"]
    assert 0 < quantity < 1000 and terminal["intent"]["reason"] == "partial_tp"
    original_mark = copy.deepcopy(terminal["intent"]["cash_valuation"]["current"])
    stamp += dt.timedelta(seconds=60)
    result = asyncio.run(bank.tick(root=tmp_path, cfg=cfg, now=stamp,
        quote_func=quoted, fx_func=original_fx, sol_price_func=scalar))
    terminal = case(tmp_path, identity)["cash"]["terminal"]
    assert result["quote_calls"] == 1 and len(calls) == 2
    assert terminal["subject"]["qty_lamports"] == 1000 - quantity
    assert terminal["fills"][0]["fx_observation"] == fx(stamp).to_dict()
    assert terminal["fills"][0]["exit_intent"]["cash_valuation"]["current"] == original_mark
    assert calls[0]["amount_lamports"] == 1000 and calls[1]["amount_lamports"] == quantity


@pytest.mark.parametrize("return_pct", [300., 1000., 10000., 1000000.])
def test_actual_entry_cash_tick_preserves_uncapped_extreme_runner_tail(tmp_path, monkeypatch, return_pct):
    # Explicit synthetic 3% tail, not an edit to the operator environment.
    monkeypatch.setattr(exit_policy, "CFG", replace(exit_policy.CFG, TP_PARTIAL_ENABLED=True,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=True, BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED=True,
        BIRD_MOONBAG_FRACTION=.03))
    cfg, identity = prepared(tmp_path)
    stamp = T0 + dt.timedelta(minutes=2)
    full_output = round(100000000 * (1 + return_pct / 100))
    async def quoted(**kw):
        return quote(quantity=kw["amount_lamports"], output=full_output * kw["amount_lamports"] // 1000,
                     source=kw["input_mint"], now=stamp)
    async def original_fx(): return fx(stamp)
    async def scalar(): return 100.
    assert asyncio.run(bank.tick(root=tmp_path, cfg=cfg, now=stamp,
        quote_func=quoted, fx_func=original_fx))["quote_calls"] == 1
    terminal = case(tmp_path, identity)["cash"]["terminal"]
    assert terminal["cash_last_mark"]["values"]["gross_remaining_return_pct"] == pytest.approx(return_pct)
    intent = copy.deepcopy(terminal["intent"])
    assert intent["reason"] == "partial_tp" and 0 < intent["quantity"] < 1000
    assert not terminal["fills"]
    stamp += dt.timedelta(seconds=60)
    assert asyncio.run(bank.tick(root=tmp_path, cfg=cfg, now=stamp,
        quote_func=quoted, fx_func=original_fx, sol_price_func=scalar))["quote_calls"] == 1
    terminal = case(tmp_path, identity)["cash"]["terminal"]
    assert terminal["subject"]["qty_lamports"] == 1000 - intent["quantity"] > 0
    assert len(terminal["fills"]) == 1 and not terminal["closed"]
    if return_pct >= 10000:
        assert terminal["subject"]["qty_lamports"] == 30


@pytest.mark.parametrize("family", ["metis", "jupiterz", "dflow", "okx"])
def test_actual_valuation_consumer_checks_all_quote_families(tmp_path, family):
    cfg, identity = prepared(tmp_path)
    stamp = T0 + dt.timedelta(minutes=2)
    mint = case(tmp_path, identity)["token"]
    async def quoted(**kw): return v2_quote(mint, SOL, 1000, 100000000, now=stamp, family=family)
    async def original_fx(): return fx(stamp)
    result = asyncio.run(bank.tick(root=tmp_path, cfg=cfg, now=stamp, quote_func=quoted, fx_func=original_fx))
    terminal = case(tmp_path, identity)["cash"]["terminal"]
    assert result["quote_calls"] == 1 and terminal["cash_observation_count"] == 1
    assert terminal["cash_last_mark"]["values"]["gross_remaining_return_pct"] == pytest.approx(0.)
    assert not terminal["fills"]


@pytest.mark.parametrize("fault", ["size", "fx", "provider", "pre_request", "generation", "prefix", "registration", "plan"])
def test_failed_or_changed_valuation_leaves_financial_state_unknown(tmp_path, fault):
    cfg, identity = prepared(tmp_path)
    stamp = T0 + dt.timedelta(minutes=2)
    async def original_fx(): return 100. if fault == "fx" else fx(stamp)
    async def quoted(**kw):
        if fault == "provider": raise RuntimeError("synthetic provider outage")
        if fault == "generation":
            record = case(tmp_path, identity)
            record["cash"]["terminal"]["subject"]["estimated_fees_usd"] += .1
            persist(tmp_path, record)
        if fault in {"prefix", "registration", "plan"}:
            record = case(tmp_path, identity)
            if fault == "prefix":
                record["cash"]["prefix"]["entry_fx_observation"]["price_usd"] += 1
            elif fault == "registration":
                record["features"]["rank_score"] += 1
            else:
                planned = plan(tmp_path, record)
                planned["runner_exit_policy"] = "changed"
                bank.storage.write(bank.directory(tmp_path) / "plans" / f"{record['plan_id']}.json", planned)
            persist(tmp_path, record)
        return quote(quantity=999 if fault == "size" else 1000, output=100000000,
                     now=stamp - dt.timedelta(seconds=1) if fault == "pre_request" else stamp)
    asyncio.run(bank.tick(root=tmp_path, cfg=cfg, now=stamp, quote_func=quoted, fx_func=original_fx))
    terminal = case(tmp_path, identity)["cash"]["terminal"]
    assert not terminal.get("cash_last_mark") and not terminal["fills"] and not terminal.get("intent")


@pytest.mark.parametrize("fault", ["scalar", "stale", "missing", "conflicting"])
def test_new_pending_exit_does_not_consume_scalar_or_unknown_fx(tmp_path, fault):
    cfg, identity = prepared(tmp_path)
    record = case(tmp_path, identity)
    stamp = T0 + dt.timedelta(minutes=2)
    terminal = record["cash"]["terminal"]
    terminal["intent"] = make_intent(terminal["subject"], quantity=1000, reason="synthetic_stop", now=stamp)
    persist(tmp_path, record)
    rate = {"scalar": 100., "stale": fx(stamp, received_at=stamp.timestamp() - 61),
            "missing": None, "conflicting": fx(stamp, price_usd=99.)}[fault]
    assert bank.observe_quote(record["token"], quote(quantity=1000, output=200000000, now=stamp), 100.,
        root=tmp_path, cfg=cfg, now=stamp, quote_started_at=stamp, fx_observation=rate) == 0
    assert case(tmp_path, identity)["cash"]["terminal"] == terminal


def closed_fixture(root):
    cfg, identity = prepared(root)
    stamp = T0 + dt.timedelta(minutes=2)
    bank.observe_market(token()["address"], 1., root=root, cfg=cfg, now=stamp)
    record = case(root, identity)
    row, arm_id = bank.cash_case(record)
    assert runner._observe_cash(row, arm_id, quote(quantity=1000, output=200000000, now=stamp), fx(stamp),
                                stamp, request_decision=False, quote_started_at=stamp)
    arm = record["cash"]["terminal"]
    proof = {"current": arm["cash_last_mark"], "remaining_peak": arm["cash_peak_mark"],
             "total_peak": arm["cash_total_peak_mark"], "quote_started_at": stamp.isoformat()}
    arm["intent"] = make_intent(arm["subject"], quantity=1000, reason="synthetic_common_exit", now=stamp,
                                cash_valuation=proof)
    persist(root, record)
    later = stamp + dt.timedelta(seconds=1)
    assert bank.observe_quote(record["token"], quote(quantity=1000, output=200000000, now=later), 100.,
        root=root, cfg=cfg, now=later, quote_started_at=later, fx_observation=fx(later)) == 1
    record = case(root, identity, "closed")
    return record, plan(root, record), later


@pytest.mark.parametrize("fault", ["none", "entry_fx", "entry_start", "fill_fx", "cash_mark", "gap",
                                    "foreign_owner", "version", "parameters", "decision_mark"])
def test_financial_consumer_requires_original_fx_owned_cash_and_decision_proof(tmp_path, fault):
    record, planned, stamp = closed_fixture(tmp_path)
    if fault == "none":
        assert evaluator._entry_cash(record, planned, stamp) == pytest.approx((.09995, 9.995))
        return
    prefix, terminal = record["cash"]["prefix"], record["cash"]["terminal"]
    if fault == "entry_fx": prefix["entry_fx_observation"]["assumed"] = True
    elif fault == "entry_start": prefix["entry_quote_started_at"] = (T0 - dt.timedelta(seconds=1)).isoformat()
    elif fault == "fill_fx": terminal["fills"][0].pop("fx_observation")
    elif fault == "cash_mark": terminal["cash_last_mark"]["values"]["quoted_proceeds_usd"] += 1
    elif fault == "gap": terminal["cash_observation_gap_limit_exceeded"] = True
    elif fault == "foreign_owner": terminal["subject"]["paper_cash_owner"] = "case:" + "f" * 64
    elif fault == "version": record.pop("financial_policy_version")
    elif fault == "parameters": terminal["parameters"]["max_price_drawdown_pct"] += 1
    else: terminal["fills"][0]["exit_intent"].pop("cash_valuation")
    with pytest.raises((ValueError, KeyError, TypeError)):
        evaluator._entry_cash(record, planned, stamp)


def test_incompatible_original_plan_is_not_relabelled_or_appended_to(tmp_path):
    cfg, identity = prepared(tmp_path)
    record = case(tmp_path, identity)
    original = plan(tmp_path, record)
    altered = {key: value for key, value in original.items() if key != "financial_policy_version"}
    new_id = bank._plan_id(altered)
    bank.storage.write(bank.directory(tmp_path) / "plans" / f"{new_id}.json", altered)
    bank.storage.write(bank.directory(tmp_path) / "open_plan.json", {"plan_id": new_id})
    assert capture(tmp_path, cfg, token(2), now=T0 + dt.timedelta(minutes=16)) is None
    assert bank.storage.read(bank.directory(tmp_path) / "plans" / f"{new_id}.json") == altered
    assert not evaluator.compare_cohort(altered, [], cfg, now=T0 + dt.timedelta(days=2))["accepted"]
