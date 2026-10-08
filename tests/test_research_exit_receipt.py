from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json

import pytest

from execution.quote_receipt import capture_summary
from quote_fixtures import SOL, TOKEN, v2_quote
from research_loop import runner_forward as rf
from research_loop.paper_exit_receipt import make_intent
from test_runner_forward import T0, cfg, entry

FAMILIES = ("metis", "jupiterz", "dflow", "okx")
STAMP = T0 + dt.timedelta(minutes=2)


def prepared(family="metis"):
    original_quote = v2_quote(SOL, TOKEN, 100000000, 1000, now=T0, family=family)
    route = capture_summary(original_quote,
        input_mint=SOL, output_mint=TOKEN, amount=100000000,
        slippage=original_quote.other["slippageBps"],
        limit=8., now=T0)
    case = rf.prepare_partial_case(entry(token_address=TOKEN, entry_route_quote=route),
                                  cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    assert case is not None
    arm = next(iter(case["arms"].values()))
    arm["intent"] = make_intent(arm["subject"], quantity=800, reason="synthetic_close", now=STAMP)
    return case, arm


def reverse(family="metis", *, received=STAMP, quantity=800):
    return v2_quote(TOKEN, SOL, quantity, 200000000, now=received, family=family)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("fault", ("before_decision", "started_before_decision", "missing_start", "naive_start", "received_before_start"))
def test_actual_research_fill_rejects_temporally_unowned_quote_without_cash_mutation(family, fault):
    case, arm = prepared(family)
    before = copy.deepcopy(arm)
    received, started = STAMP, STAMP
    if fault == "before_decision": received = started = STAMP - dt.timedelta(seconds=1)
    if fault == "started_before_decision": started = STAMP - dt.timedelta(seconds=1)
    if fault == "missing_start": started = None
    if fault == "naive_start": started = STAMP.replace(tzinfo=None)
    if fault == "received_before_start": started = STAMP + dt.timedelta(seconds=1)
    assert not rf.apply_paper_exit_quote(case, arm, reverse(family, received=received), 100.,
        STAMP + dt.timedelta(seconds=2), quote_started_at=started)
    assert arm == before


@pytest.mark.parametrize("field,value", (
    ("realized_proceeds_usd", 4.1), ("realized_proceeds_sol", .041),
    ("estimated_fees_usd", .006), ("estimated_fees_sol", .00006),
    ("entry_notional_usd", 11.), ("execution_fill_count", 3),
    ("buy_price_usd", 2.), ("run_id", "ANOTHER_RUN"),
    ("opened_at", (T0 + dt.timedelta(seconds=1)).isoformat()),
    ("runner_trailing_policy", "changed"),
))
def test_pending_intent_cannot_fill_after_its_financial_generation_changed(field, value):
    case, arm = prepared()
    arm["subject"][field] = value
    before = copy.deepcopy(arm)
    assert not rf.apply_paper_exit_quote(case, arm, reverse(), 100., STAMP, quote_started_at=STAMP)
    assert arm == before


@pytest.mark.parametrize("fault", ("hash", "quantity", "reason", "clock", "basis", "legacy", "malformed_fills", "bad_partial_count"))
def test_unknown_or_forged_intent_is_not_repaired_into_a_fill(fault):
    case, arm = prepared()
    if fault == "hash": arm["intent"]["receipt"]["sha256"] = "0" * 64
    if fault == "quantity": arm["intent"]["quantity"] = 799
    if fault == "reason": arm["intent"]["reason"] = "changed"
    if fault == "clock": arm["intent"]["requested_at"] = T0.isoformat()
    if fault == "basis": arm["intent"]["receipt"]["financial_basis"]["realized_proceeds_usd"] = 999.
    if fault == "legacy": del arm["intent"]["receipt"]
    if fault == "malformed_fills": arm["fills"] = None
    if fault == "bad_partial_count":
        arm["subject"]["partial_count"] = "bad"
        arm["intent"] = make_intent(arm["subject"], quantity=100, reason="partial_tp", now=STAMP)
    before = copy.deepcopy(arm)
    quantity = arm["intent"]["quantity"]
    assert not rf.apply_paper_exit_quote(case, arm, reverse(quantity=quantity), 100., STAMP, quote_started_at=STAMP)
    assert arm == before


@pytest.mark.parametrize("family", FAMILIES)
def test_original_intent_and_request_clock_survive_close_and_are_required_by_terminal_consumer(family):
    case, arm = prepared(family)
    original = copy.deepcopy(arm["intent"])
    assert rf.apply_paper_exit_quote(case, arm, reverse(family), 100., STAMP, quote_started_at=STAMP)
    assert arm["fills"][0]["exit_intent"] == original
    assert arm["fills"][0]["quote_started_at"] == STAMP.isoformat()
    assert arm["net_pnl_sol"] == pytest.approx(.139925)
    assert rf.validate_paper_cash_terminal(case, arm, STAMP)
    for key in ("exit_intent", "quote_started_at", "route_quote"):
        damaged = copy.deepcopy(arm)
        del damaged["fills"][0][key]
        assert not rf.validate_paper_cash_terminal(case, damaged, STAMP)
    damaged = copy.deepcopy(arm)
    damaged["fills"][0]["quote_started_at"] = (STAMP - dt.timedelta(seconds=1)).isoformat()
    assert not rf.validate_paper_cash_terminal(case, damaged, STAMP)


def test_secondary_await_cannot_fill_replaced_financial_generation(tmp_path):
    case, _ = prepared()
    stamp = T0 + dt.timedelta(hours=25)
    path = rf._directory(tmp_path) / "active" / f"{case['case_id']}.json"
    for arm in case["arms"].values(): arm.pop("intent", None)
    rf._write(path, case)
    async def prices(_): return {}
    async def sol(): return 100.
    async def quoted(**kwargs):
        latest = rf._read(path)
        for arm in latest["arms"].values(): arm["subject"]["estimated_fees_usd"] += .1
        rf._write(path, latest)
        return reverse(received=stamp, quantity=kwargs["amount_lamports"])
    result = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=stamp,
        prices_func=prices, quote_func=quoted, sol_price_func=sol))
    assert result["quote_calls"] == 1
    latest = rf._read(path)
    assert all(not arm["fills"] and arm["subject"]["qty_lamports"] == 800 and arm.get("intent")
               for arm in latest["arms"].values())


def test_receipt_is_detached_and_contains_only_public_financial_inputs():
    case, arm = prepared()
    subject = arm["subject"]
    subject["private_key"] = "must_not_be_copied"
    intent = make_intent(subject, quantity=800, reason="synthetic_close", now=STAMP)
    assert "must_not_be_copied" not in json.dumps(intent)
    subject["execution_cost_model"]["fee_sol_per_fill"] = 9.
    assert intent["receipt"]["financial_basis"]["execution_cost_model"]["fee_sol_per_fill"] == .000025


def test_missing_financial_generation_leaves_timeout_unknown_without_crash():
    case, arm = prepared()
    arm.pop("intent")
    del arm["subject"]["estimated_fees_usd"]
    rf.paper_exit_request(arm, None, T0 + dt.timedelta(hours=25))
    assert not arm.get("intent")
    assert arm["intent_error"] == "unknown_original_financial_generation"


def test_partial_then_terminal_replays_each_original_financial_generation():
    case, arm = prepared()
    arm["intent"] = make_intent(arm["subject"], quantity=100, reason="partial_tp", now=STAMP)
    first_quote = v2_quote(TOKEN, SOL, 100, 20000000, now=STAMP)
    assert rf.apply_paper_exit_quote(case, arm, first_quote, 100., STAMP, quote_started_at=STAMP)
    assert arm["subject"]["qty_lamports"] == 700
    later = STAMP + dt.timedelta(minutes=1)
    arm["intent"] = make_intent(arm["subject"], quantity=700, reason="synthetic_close", now=later)
    last_quote = v2_quote(TOKEN, SOL, 700, 180000000, now=later)
    assert rf.apply_paper_exit_quote(case, arm, last_quote, 100., later, quote_started_at=later)
    assert arm["net_pnl_usd"] == pytest.approx(13.99)
    assert rf.validate_paper_cash_terminal(case, arm, later)
    damaged = copy.deepcopy(arm)
    damaged["fills"][1]["exit_intent"] = copy.deepcopy(damaged["fills"][0]["exit_intent"])
    assert not rf.validate_paper_cash_terminal(case, damaged, later)


def test_receiptless_profitable_cohort_is_not_eligible_for_automatic_selection(tmp_path):
    from test_runner_forward import closed_cohort
    cases = closed_cohort(tmp_path)
    stamp = T0 + dt.timedelta(hours=27)
    assert rf.compare_cohort(cases, now=stamp)["accepted"]
    for case in cases:
        for arm in case["arms"].values(): del arm["fills"][0]["exit_intent"]
        rf._write(rf._directory(tmp_path) / "closed" / f"{case['case_id']}.json", case)
    report = rf.compare_cohort(cases, now=stamp)
    assert not report["accepted"] and "unresolved_or_uncosted_arm" in report["reasons"]
    assert rf.evaluate_completed_cohorts(root=tmp_path, cfg=cfg(), now=stamp)["status"] != "selected"
    assert not (rf._directory(tmp_path) / "active_policy.json").exists()


@pytest.mark.parametrize("path", (("execution_cost_model", "slippage_bps"),
    ("execution_cost_model", "fee_sol_per_fill"), ("realized_proceeds_usd",),
    ("estimated_fees_sol",), ("execution_fill_count",)))
def test_rehashed_boolean_financial_basis_is_not_a_typed_money_receipt(path):
    from research_loop.paper_exit_receipt import _hash
    case, arm = prepared()
    # Equal numeric values in the actual subject do not justify bool receipts.
    subject = arm["subject"]
    if len(path) == 2:
        subject[path[0]][path[1]] = 0.
    elif path[0] == "execution_fill_count":
        subject[path[0]] = 1
    else:
        subject[path[0]] = 0.
    arm["intent"] = make_intent(subject, quantity=800, reason="synthetic_close", now=STAMP)
    receipt = arm["intent"]["receipt"]
    target = receipt["financial_basis"]
    if len(path) == 2: target = target[path[0]]
    target[path[-1]] = path[-1] == "execution_fill_count"
    receipt["sha256"] = _hash({key: value for key, value in receipt.items() if key != "sha256"})
    before = copy.deepcopy(arm)
    assert not rf.apply_paper_exit_quote(case, arm, reverse(), 100., STAMP, quote_started_at=STAMP)
    assert arm == before


@pytest.mark.parametrize("policy_present", (False, True))
def test_cash_only_terminal_replay_does_not_invent_absent_policy_field(policy_present):
    case, arm = prepared()
    case["prefix"].pop("runner_trailing_policy")
    arm["subject"].pop("runner_trailing_policy")
    if policy_present:
        case["prefix"]["runner_trailing_policy"] = None
        arm["subject"]["runner_trailing_policy"] = None
    arm["intent"] = make_intent(arm["subject"], quantity=800, reason="synthetic_close", now=STAMP)
    assert rf.apply_paper_exit_quote(case, arm, reverse(), 100., STAMP, quote_started_at=STAMP)
    assert rf.validate_paper_cash_terminal(case, arm, STAMP)
