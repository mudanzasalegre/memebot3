"""Synthetic per-arm cash policy integration, not market/profitability evidence."""
import asyncio
import copy
import datetime as dt
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from analytics import exit_policy, runner_ladder
from execution import paper_cash_mark as cash
from research_loop import runner_forward as rf, entry_gate_forward as bank, forward_budget
from research_loop.paper_exit_receipt import make_intent, valid_intent, valid_cash_valuation
from test_runner_forward import T0, MINT, cfg, entry, quote, read_active, closed_cohort, isolated_exit_policy
from utils.sol_price import SolUsdObservation


def fx(stamp, **changes):
    return SolUsdObservation(**{"status": "OK", "price_usd": 100.,
        "received_at": stamp.timestamp(), "market_updated_at": stamp.timestamp(), **changes})


def prepared():
    case = rf.prepare_partial_case(entry(), cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    return case, next(iter(case["arms"]))


def evidence(arm, stamp):
    return {"current": arm["cash_last_mark"], "remaining_peak": arm["cash_peak_mark"],
            "total_peak": arm["cash_total_peak_mark"], "quote_started_at": stamp.isoformat()}


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch, isolated_exit_policy):
    from fetcher import jupiter_price, jupiter_router
    from utils import sol_price
    from analytics.api_budget import reset_provider_circuits
    rf._ACTIVE_INDEX.clear()
    rf._VERIFIED_CACHE.clear()
    reset_provider_circuits()
    monkeypatch.setattr(exit_policy, "CFG", replace(exit_policy.CFG, BIRD_MOONBAG_FRACTION=.03))
    blocked = AsyncMock(side_effect=AssertionError("Synthetic test attempted a real provider"))
    monkeypatch.setattr(jupiter_router, "get_routing_quote", blocked)
    monkeypatch.setattr(jupiter_price, "get_many_usd_prices", blocked)
    monkeypatch.setattr(sol_price, "get_sol_usd_observation", blocked)
    monkeypatch.setattr(sol_price, "get_sol_usd", blocked)
    yield
    reset_provider_circuits()


def test_spot_moonshot_cannot_create_a_financial_intent_or_cash_peak():
    case, _ = prepared()
    before = copy.deepcopy(case["arms"])
    rf._observe_case(case, 1000000., T0 + dt.timedelta(minutes=2))
    assert case["arms"] == before


@pytest.mark.parametrize("liquidity", [0., 1.])
def test_independent_liquidity_crush_keeps_working_without_a_cash_or_spot_price(liquidity):
    case = rf.prepare_partial_case(entry(buy_liquidity_usd=10000.), cfg=cfg(),
                                   now=T0 + dt.timedelta(minutes=1))
    rf._observe_case(case, None, T0 + dt.timedelta(minutes=2), liq_now=liquidity)
    assert all(arm["intent"]["reason"] == "LIQUIDITY_CRUSH" and not arm.get("cash_last_mark")
               and not arm["fills"] for arm in case["arms"].values())


@pytest.mark.parametrize("liquidity", [None, True, "not_number", -1., float("nan"), float("inf"), 10000.])
def test_unknown_or_healthy_liquidity_does_not_create_a_cashless_exit(liquidity):
    case = rf.prepare_partial_case(entry(buy_liquidity_usd=10000.), cfg=cfg(),
                                   now=T0 + dt.timedelta(minutes=1))
    rf._observe_case(case, None, T0 + dt.timedelta(minutes=2), liq_now=liquidity)
    assert all(not arm.get("intent") and not arm.get("cash_last_mark") for arm in case["arms"].values())


@pytest.mark.parametrize("return_pct", [300., 1000., 10000., 1000000.])
def test_exact_cash_return_is_uncapped_and_preserves_a_runner_tail(return_pct):
    case, arm_id = prepared()
    stamp = T0 + dt.timedelta(minutes=2)
    output = round(80000000 * (1 + return_pct / 100))
    q = quote(output=output, now=stamp)
    assert rf._observe_cash(case, arm_id, q, fx(stamp), stamp, quote_started_at=stamp)
    arm = case["arms"][arm_id]
    assert arm["cash_last_mark"]["values"]["gross_remaining_return_pct"] == pytest.approx(return_pct)
    assert arm["subject"]["highest_pnl_pct"] == pytest.approx(return_pct)
    intent = arm["intent"]
    assert intent["reason"] == "partial_tp" and valid_intent(intent, arm["subject"])
    assert 0 < intent["quantity"] < 800 and not arm["fills"]
    if return_pct >= 10000:
        assert intent["quantity"] == 770  # 3% of original entry remains; no upper return cap.
    assert not rf.apply_paper_exit_quote(case, arm, q, 100., stamp, quote_started_at=stamp)
    # A separate, later exact-size quote executes the original intent.
    later = stamp + dt.timedelta(seconds=1)
    fill_quote = quote(quantity=intent["quantity"], output=round(output * intent["quantity"] / 800), now=later)
    assert rf.apply_paper_exit_quote(case, arm, fill_quote, 100., later, quote_started_at=later)
    assert arm["subject"]["qty_lamports"] == 800 - intent["quantity"] > 0
    assert arm["fills"][0]["exit_intent"]["cash_valuation"]["current"]["valued_at"] == stamp.isoformat()


@pytest.mark.parametrize("change", [{"price_usd": None}, {"price_usd": True},
    {"received_at": T0.timestamp()}, {"market_updated_at": T0.timestamp() - 300},
    {"assumed": True}, {"status": "ERR"}, {"source": "configured_override"}])
def test_bad_original_fx_never_becomes_cash_or_a_zero_return(change):
    case, arm_id = prepared()
    stamp = T0 + dt.timedelta(minutes=2)
    before = copy.deepcopy(case)
    assert not rf._observe_cash(case, arm_id, quote(now=stamp), fx(stamp, **change), stamp, quote_started_at=stamp)
    assert case == before


@pytest.mark.parametrize("quantity,started_offset", [(799, 0), (800, None), (800, 1)])
def test_wrong_size_missing_start_or_pre_request_receipt_is_not_an_observation(quantity, started_offset):
    case, arm_id = prepared()
    stamp = T0 + dt.timedelta(minutes=2)
    started = None if started_offset is None else stamp + dt.timedelta(seconds=started_offset)
    before = copy.deepcopy(case)
    assert not rf._observe_cash(case, arm_id, quote(quantity=quantity, now=stamp), fx(stamp), stamp,
                                quote_started_at=started)
    assert case == before


def test_cash_protection_is_owned_by_one_arm_and_cannot_match_a_sql_position():
    case, arm_id = prepared()
    stamp = T0 + dt.timedelta(minutes=2)
    assert rf._observe_cash(case, arm_id, quote(output=80000000, now=stamp), fx(stamp), stamp,
                            request_decision=False, quote_started_at=stamp)
    arm, other = case["arms"][arm_id], next(arm for key, arm in case["arms"].items() if key != arm_id)
    row = arm["subject"]
    context = cash.protection_context(row, arm["cash_last_mark"], arm["cash_total_peak_mark"],
        token=MINT, owner=row["paper_cash_owner"], now=stamp)
    assert cash.protection_returns(context, row, now=stamp, price=1.) == pytest.approx((19.925, 19.925))
    assert cash.protection_returns(context, other["subject"], now=stamp, price=1.) is None
    assert cash.protection_returns(context, object(), now=stamp, price=1.) is None
    assert cash.protection_returns(context, row, now=stamp + dt.timedelta(seconds=11), price=1.) is None


def test_peak_original_clocks_survive_partial_and_total_peak_is_not_remaining_peak():
    case, arm_id = prepared()
    stamp = T0 + dt.timedelta(minutes=2)
    arm = case["arms"][arm_id]
    assert rf._observe_cash(case, arm_id, quote(output=400000000, now=stamp), fx(stamp), stamp,
                            request_decision=False, quote_started_at=stamp)
    first = copy.deepcopy(arm["cash_total_peak_mark"])
    arm["intent"] = make_intent(arm["subject"], quantity=700, reason="synthetic_partial", now=stamp,
                                cash_valuation=evidence(arm, stamp))
    fill_at = stamp + dt.timedelta(seconds=1)
    assert rf.apply_paper_exit_quote(case, arm, quote(quantity=700, output=300000000, now=fill_at), 100.,
                                     fill_at, quote_started_at=fill_at)
    later = stamp + dt.timedelta(seconds=60)
    assert rf._observe_cash(case, arm_id, quote(quantity=100, output=60000000, now=later), fx(later), later,
                            request_decision=False, quote_started_at=later)
    assert arm["cash_peak_mark"]["values"]["gross_remaining_return_pct"] == pytest.approx(500.)
    assert arm["cash_peak_mark"]["valued_at"] == later.isoformat()
    # $4 + $30 realized and $6 residual is less whole cash than the first $4+$40
    # valuation, although the remaining leg has risen from +400% to +500%.
    assert arm["cash_total_peak_mark"]["valued_at"] == stamp.isoformat()
    assert first["valued_at"] == stamp.isoformat()  # The original first observation is never renewed.
    assert cash.public_historical_mark(first, arm["subject"], token=MINT,
        owner=arm["subject"]["paper_cash_owner"]) == first
    context = cash.protection_context(arm["subject"], arm["cash_last_mark"], arm["cash_total_peak_mark"],
        token=MINT, owner=arm["subject"]["paper_cash_owner"], now=later)
    assert cash.protection_returns(context, arm["subject"], now=later, price=6.) == pytest.approx((299.9, 339.925))
    assert exit_policy.total_pnl_protection_reason(arm["subject"], peak=500., close_price_usd=6.,
                                                  cash_context=context, now=later) is None


def test_same_instant_duplicate_is_idempotent_conflict_and_rewind_are_rejected():
    case, arm_id = prepared()
    stamp = T0 + dt.timedelta(minutes=2)
    q = quote(output=80000000, now=stamp)
    assert rf._observe_cash(case, arm_id, q, fx(stamp), stamp, request_decision=False, quote_started_at=stamp)
    before = copy.deepcopy(case)
    assert rf._observe_cash(case, arm_id, q, fx(stamp), stamp, request_decision=False, quote_started_at=stamp)
    assert case == before
    assert not rf._observe_cash(case, arm_id, quote(output=90000000, now=stamp), fx(stamp), stamp,
                                request_decision=False, quote_started_at=stamp)
    earlier = stamp - dt.timedelta(seconds=1)
    assert not rf._observe_cash(case, arm_id, quote(output=90000000, now=earlier), fx(earlier), earlier,
                                request_decision=False, quote_started_at=earlier)
    assert case == before


@pytest.mark.parametrize("part", ["current", "remaining_peak", "total_peak", "quote_started_at"])
def test_modified_original_decision_proof_cannot_fill(part):
    case, arm_id = prepared()
    stamp = T0 + dt.timedelta(minutes=2)
    assert rf._observe_cash(case, arm_id, quote(output=800000000, now=stamp), fx(stamp), stamp,
                            quote_started_at=stamp)
    arm = case["arms"][arm_id]
    corrupted = copy.deepcopy(arm["intent"]["cash_valuation"])
    corrupted[part] = {} if part != "quote_started_at" else (stamp + dt.timedelta(seconds=1)).isoformat()
    assert not valid_cash_valuation(corrupted, arm["subject"], now=stamp)


def test_legacy_entry_keeps_original_intake_but_has_no_financial_valuation_demand(tmp_path):
    original = entry(entry_route_quote={"in_amount": 100000000, "out_amount": 1000,
                                       "max_impact_pct": 8., "route_count": 1})
    assert rf.register_partial(original, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    case = read_active(tmp_path)
    assert case["prefix"]["entry_route_quote"] == original["entry_route_quote"]
    assert not rf.has_quote_demand(tmp_path)
    assert not any(_arm.get("cash_last_mark") for _arm in case["arms"].values())


def test_tick_values_identical_arms_once_then_fills_on_a_separate_budget_slot(tmp_path):
    assert rf.register_partial(entry(), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    stamp, calls = T0 + dt.timedelta(minutes=2), []
    async def quoted(**kwargs):
        calls.append(kwargs)
        return quote(quantity=kwargs["amount_lamports"], output=kwargs["amount_lamports"] * 1000000, now=stamp)
    async def rate(): return fx(stamp)
    async def scalar(): return 100.
    async def spot(_): return {MINT: 1000000.}
    first = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=stamp, quote_func=quoted,
                                prices_func=spot, fx_func=rate, sol_price_func=scalar))
    case = read_active(tmp_path)
    assert first["quote_calls"] == len(calls) == 1
    assert all(arm["cash_observation_count"] == 1 and arm.get("intent") and not arm["fills"]
               for arm in case["arms"].values())
    quantity = next(iter(case["arms"].values()))["intent"]["quantity"]
    assert quantity < 800
    assert asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=stamp + dt.timedelta(seconds=1),
                               quote_func=quoted, fx_func=rate))["status"] == "throttled"
    stamp += dt.timedelta(seconds=60)
    second = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=stamp, quote_func=quoted,
                                 fx_func=rate, sol_price_func=scalar))
    case = read_active(tmp_path)
    assert second["quote_calls"] == 1 and len(calls) == 2
    assert all(len(arm["fills"]) == 1 and arm["subject"]["qty_lamports"] == 800 - quantity
               for arm in case["arms"].values())
    assert calls[0]["amount_lamports"] == 800 and calls[1]["amount_lamports"] == quantity


@pytest.mark.parametrize("failure", ["provider", "scalar_fx", "pre_request", "generation"])
def test_tick_failure_or_changed_generation_does_not_mutate_financial_evidence(tmp_path, failure):
    rf.register_partial(entry(), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    stamp = T0 + dt.timedelta(minutes=2)
    path = next((rf._directory(tmp_path) / "active").glob("*.json"))
    async def quoted(**kwargs):
        if failure == "provider": raise RuntimeError("synthetic outage")
        if failure == "generation":
            latest = rf._read(path)
            for arm in latest["arms"].values(): arm["subject"]["estimated_fees_usd"] += 1.
            rf._write(path, latest)
        return quote(output=800000000, now=stamp - dt.timedelta(seconds=1) if failure == "pre_request" else stamp)
    async def rate(): return 100. if failure == "scalar_fx" else fx(stamp)
    result = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=stamp, quote_func=quoted, fx_func=rate))
    assert result["quote_calls"] == 1
    assert all(not arm["fills"] and not arm.get("intent") and not arm.get("cash_last_mark")
               and arm["subject"]["qty_lamports"] == 800 for arm in read_active(tmp_path)["arms"].values())


def test_actual_pending_exit_has_priority_over_new_valuations(tmp_path):
    rf.register_partial(entry(), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    case = read_active(tmp_path)
    arm = next(iter(case["arms"].values()))
    arm["intent"] = make_intent(arm["subject"], quantity=100, reason="synthetic_partial",
                                now=T0 + dt.timedelta(minutes=2))
    rf._write(rf._directory(tmp_path) / "active" / f"{case['case_id']}.json", case)
    stamp, calls = T0 + dt.timedelta(minutes=3), []
    async def quoted(**kwargs):
        calls.append(kwargs)
        return quote(quantity=kwargs["amount_lamports"], now=stamp)
    async def scalar(): return 100.
    result = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=stamp, quote_func=quoted, sol_price_func=scalar))
    assert result["quote_calls"] == 1 and calls[0]["amount_lamports"] == 100
    case = read_active(tmp_path)
    assert sum(len(a["fills"]) for a in case["arms"].values()) == 1
    assert not any(a.get("cash_last_mark") for a in case["arms"].values())


def test_shared_budget_still_alternates_when_entry_gate_has_pending_work(tmp_path):
    rf.register_partial(entry(), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    stamp = T0 + dt.timedelta(minutes=2)
    assert forward_budget.claim(tmp_path, "runner_exit", now=stamp - dt.timedelta(seconds=60))
    forward_budget.write(bank.directory(tmp_path) / "active" / ("a" * 64 + ".json"),
                         {"token": MINT, "cash": {"terminal": {"intent": {"quantity": 1}}}})
    async def forbidden(**_): pytest.fail("Alternation should retain the slot for entry research")
    result = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=stamp, quote_func=forbidden))
    assert result["quote_calls"] == 0
    assert rf.has_quote_demand(tmp_path)


def test_different_quantities_are_serviced_oldest_first_without_cross_size_cash(tmp_path, monkeypatch):
    original_observer = rf._observe_cash
    def valuation_only(*args, **kwargs):
        return original_observer(*args, **{**kwargs, "request_decision": False})
    monkeypatch.setattr(rf, "_observe_cash", valuation_only)
    rf.register_partial(entry(), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    case = read_active(tmp_path)
    stamp = T0 + dt.timedelta(minutes=2)
    for index, arm in enumerate(case["arms"].values()):
        if index:
            arm["intent"] = make_intent(arm["subject"], quantity=index * 100, reason="synthetic_partial", now=stamp)
            assert rf.apply_paper_exit_quote(case, arm, quote(quantity=index * 100, output=index * 10000000,
                now=stamp), 100., stamp, quote_started_at=stamp)
    path = rf._directory(tmp_path) / "active" / f"{case['case_id']}.json"
    rf._write(path, case)
    quantities = []
    async def quoted(**kwargs):
        quantity = kwargs["amount_lamports"]
        quantities.append(quantity)
        return quote(quantity=quantity, output=quantity * 100000, now=stamp)
    async def rate(): return fx(stamp)
    for _ in range(3):
        result = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=stamp, quote_func=quoted, fx_func=rate))
        assert result["quote_calls"] == 1, (result, read_active(tmp_path))
        stamp += dt.timedelta(seconds=60)
    assert set(quantities) == {600, 700, 800}
    final = read_active(tmp_path)
    for arm in final["arms"].values():
        assert arm["cash_observation_count"] == 1
        assert arm["cash_last_mark"]["basis"]["remaining_qty"] == arm["subject"]["qty_lamports"]


@pytest.mark.parametrize("mutation", ["version", "arm_gap", "arm_zero", "old_last", "missing_mark", "foreign_owner",
                                    "modified_last_mark", "missing_total_peak", "modified_peak_clock"])
def test_costed_terminal_cash_alone_cannot_certify_financial_policy_coverage(tmp_path, mutation):
    cases = closed_cohort(tmp_path)
    case = cases[0]
    arm = next(iter(case["arms"].values()))
    if mutation == "version": case.pop("financial_policy_version")
    elif mutation == "arm_gap": arm["cash_observation_gap_limit_exceeded"] = True
    elif mutation == "arm_zero": arm["cash_observation_count"] = 0
    elif mutation == "old_last": arm["cash_last_observed_at"] = T0.isoformat()
    elif mutation == "missing_mark": arm.pop("cash_last_mark")
    elif mutation == "foreign_owner": arm["subject"]["paper_cash_owner"] = "case:" + "f" * 64
    elif mutation == "modified_last_mark": arm["cash_last_mark"]["values"]["quoted_proceeds_usd"] += 1.
    elif mutation == "missing_total_peak": arm.pop("cash_total_peak_mark")
    else: arm["cash_peak_mark"]["valued_at"] = T0.isoformat()
    assert not rf.compare_cohort(cases, now=T0 + dt.timedelta(hours=27))["accepted"]


def test_gap_is_recorded_per_arm_not_hidden_by_another_arms_market_coverage():
    case, arm_id = prepared()
    stamp = T0 + dt.timedelta(minutes=2)
    assert rf._observe_cash(case, arm_id, quote(output=80000000, now=stamp), fx(stamp), stamp,
                            request_decision=False, quote_started_at=stamp)
    stamp += dt.timedelta(seconds=301)
    assert rf._observe_cash(case, arm_id, quote(output=80000000, now=stamp), fx(stamp), stamp,
                            request_decision=False, quote_started_at=stamp)
    assert case["arms"][arm_id]["cash_observation_gap_limit_exceeded"] is True
    assert all(a["cash_observation_count"] == 0 for key, a in case["arms"].items() if key != arm_id)
