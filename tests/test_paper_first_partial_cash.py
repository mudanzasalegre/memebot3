"""Isolated original-cash reconstruction; no market, wallet or bot execution."""
import copy
import datetime as dt
from dataclasses import replace

import pytest

from analytics import runner_price_policy
from execution import paper_first_partial_cash as cash
from execution.quote_receipt import capture_summary
from quote_fixtures import SOL, TOKEN, v1_quote, v2_quote
from research_loop import runner_forward as rf
from research_loop.paper_exit_receipt import make_intent
from runtime import runner_enrollment as intake
from runtime.paper_archive import PaperArchiveError
from trader import papertrading as paper
from utils.atomic_json import read_json_strict, write_json_atomic
from test_runner_forward import T0, cfg, entry, closed_cohort, isolated_exit_policy
from test_paper_execution_fx import isolated, fx

CAPTURED = T0 + dt.timedelta(minutes=1)


def rehash(source):
    source["payload_sha256"] = rf._hash({"captured_at": source["captured_at"], "prefix": source["prefix"]})


@pytest.mark.parametrize("family", ["v1", "metis", "jupiterz", "dflow", "okx"])
def test_original_reverse_router_receipts_reconstruct_identical_cash(family):
    row = entry(token_address=TOKEN)
    response = row["exit_fill_events"][0]["response"]
    q = (v1_quote(TOKEN, SOL, 200, 40000000, now=CAPTURED) if family == "v1" else
         v2_quote(TOKEN, SOL, 200, 40000000, now=CAPTURED, family=family))
    response["exit_route_quote"] = capture_summary(q, input_mint=TOKEN, output_mint=SOL,
        amount=200, slippage=q.other["slippageBps"], limit=8., now=CAPTURED)
    assert cash.reconstruct(row, captured_at=CAPTURED) == pytest.approx({
        "realized_proceeds_sol": .04, "realized_proceeds_usd": 4.,
        "estimated_fees_sol": .00005, "estimated_fees_usd": .005})
    case = rf.prepare_partial_case(row, cfg=cfg(), now=CAPTURED)
    assert case is not None and case["prefix"]["exit_fill_events"] == row["exit_fill_events"]
    row["exit_fill_events"][0]["response"]["price_used_usd"] = 999.
    assert case["prefix"]["exit_fill_events"][0]["response"]["price_used_usd"] == 2.


FAULTS = ["proceeds_sol", "proceeds_usd", "fees_sol", "fees_usd", "entry_notional", "amount",
    "entry_fx_missing", "entry_clock", "buy_identity", "buy_signature", "exit_fx_missing", "exit_fx_rate",
    "exit_fx_future", "exit_fx_bool", "sell_scalar", "fill_clock", "first_clock", "last_clock",
    "price", "quantity", "qty_bool", "qty_left", "qty_before", "duplicate", "missing_events",
    "extra_fill", "exit_id", "signature", "venue", "partial", "not_ok", "missing_quote", "stale_quote",
    "quote_before_entry", "foreign_limit", "derived_cost", "derived_pnl", "derived_net"]


def damage(row, fault):
    response = row["exit_fill_events"][0]["response"]
    if fault in {"proceeds_sol", "proceeds_usd", "fees_sol", "fees_usd"}:
        field = {"proceeds_sol": "realized_proceeds_sol", "proceeds_usd": "realized_proceeds_usd",
            "fees_sol": "estimated_fees_sol", "fees_usd": "estimated_fees_usd"}[fault]
        row[field] += .01
    elif fault == "entry_notional": row["entry_notional_usd"] += 1.
    elif fault == "amount": row["amount_sol"] = .01
    elif fault == "entry_fx_missing": row.pop("entry_fx_observation")
    elif fault == "entry_clock": row["entry_valued_at"] = (T0-dt.timedelta(seconds=1)).isoformat()
    elif fault == "buy_identity": row["source_position_key"] = "buy:" + "f"*32
    elif fault == "buy_signature": row["buy_signature"] = "LIVE"
    elif fault == "exit_fx_missing": response.pop("fill_fx_observation")
    elif fault == "exit_fx_rate": response["fill_fx_observation"]["price_usd"] = 200.
    elif fault == "exit_fx_future": response["fill_fx_observation"]["received_at"] += 1.
    elif fault == "exit_fx_bool": response["fill_fx_observation"]["price_usd"] = True
    elif fault == "sell_scalar": response["quote_sol_usd"] = "100"
    elif fault == "fill_clock": response["filled_at"] = T0.isoformat()
    elif fault == "first_clock": row["first_partial_at"] = T0.isoformat()
    elif fault == "last_clock": row["last_partial_at"] = T0.isoformat()
    elif fault == "price": response["price_used_usd"] += .01
    elif fault == "quantity": response["qty_sold"] += 1
    elif fault == "qty_bool": response["qty_sold"] = True
    elif fault == "qty_left": response["qty_left"] += 1
    elif fault == "qty_before": row["exit_fill_events"][0]["qty_before"] -= 1
    elif fault == "duplicate": row["exit_fill_events"].append(copy.deepcopy(row["exit_fill_events"][0]))
    elif fault == "missing_events": row.pop("exit_fill_events")
    elif fault == "extra_fill": row["execution_fill_count"] += 1
    elif fault == "exit_id": response["exit_intent_id"] = "f"*32
    elif fault == "signature": response["signature"] = "LIVE"
    elif fault == "venue": response["venue"] = "live"
    elif fault == "partial": response["partial"] = False
    elif fault == "not_ok": response["ok"] = False
    elif fault == "missing_quote": response.pop("exit_route_quote")
    elif fault in {"stale_quote", "quote_before_entry"}:
        stamp = CAPTURED-dt.timedelta(seconds=11) if fault == "stale_quote" else T0-dt.timedelta(seconds=1)
        q = v1_quote(TOKEN, SOL, 200, 40000000, now=stamp)
        response["exit_route_quote"] = capture_summary(q, input_mint=TOKEN, output_mint=SOL,
            amount=200, slippage=q.other["slippageBps"], limit=8., now=stamp)
    elif fault == "foreign_limit": response["exit_route_quote"]["max_impact_pct"] = 7.
    elif fault == "derived_cost": row["realized_cost_usd"] = 3.
    elif fault == "derived_pnl": row["realized_pnl_usd"] = 3.
    elif fault == "derived_net": row["net_realized_pnl_usd"] = 3.
    else: raise AssertionError(fault)


@pytest.mark.parametrize("fault", FAULTS)
def test_rehashed_original_intake_does_not_certify_altered_cash(tmp_path, fault):
    source = intake.capture_source(entry(token_address=TOKEN), captured_at=CAPTURED)
    damage(source["prefix"], fault)
    rehash(source)
    assert rf.prepare_partial_case(source["prefix"], cfg=cfg(), now=CAPTURED) is None
    with pytest.raises((ValueError, TypeError, KeyError, OverflowError, PaperArchiveError)):
        cash.reconstruct(source["prefix"], captured_at=CAPTURED)
    with pytest.raises(intake.RunnerEnrollmentError):
        intake.register_source(source, root=tmp_path, cfg=cfg(), now=CAPTURED)
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
async def test_actual_primary_partial_enrolls_replays_cash_and_terminal_with_distinct_original_fx(isolated, monkeypatch):
    clock = isolated
    monkeypatch.setattr(paper, "CFG", replace(paper.CFG, PAPER_RUNNER_RESEARCH_ENABLED=True,
        PAPER_RUNNER_RESEARCH_AUTO_APPLY=False, RUNNER_PRICE_TRAILING_PAPER_ENABLED=True))
    monkeypatch.setattr(paper, "_research_root", lambda: paper._DATA_PATH.parent.parent)
    monkeypatch.setattr(paper, "runtime_context_payload", lambda: {
        "run_id": "synthetic-original-cash", "run_started_at": clock.now.isoformat()})
    monkeypatch.setattr(rf, "_now", lambda: clock.now)
    monkeypatch.setenv("PAPER_FILL_SLIPPAGE_BPS", "100")
    await paper.buy(TOKEN, .1, entry_intent_id="a"*32, entry_regime="pump_early", entry_lane="normal")
    assert paper._PORTFOLIO[TOKEN]["entry_qty"] == 990
    clock.now += dt.timedelta(minutes=2)
    clock.price, clock.output = 200., 40000000
    filled = await paper.sell(TOKEN, 250, exit_intent_id="b"*32)
    assert filled["ok"] and filled["qty_left"] == 740
    original = paper._PORTFOLIO[TOKEN]["runner_research_source"]
    expected = {"realized_proceeds_sol": .0396, "realized_proceeds_usd": 7.92,
        "estimated_fees_sol": .00005, "estimated_fees_usd": .0075}
    assert cash.reconstruct(original["prefix"], captured_at=clock.now) == pytest.approx(expected)
    base = rf._directory(paper._research_root())
    case = read_json_strict(next((base / "active").glob("*.json")))
    assert case["prefix"]["exit_fill_events"] == original["prefix"]["exit_fill_events"]
    assert intake.population_matches(paper._research_root(), case["cohort_id"], [case])
    clock.now += dt.timedelta(minutes=2)
    for arm in case["arms"].values():
        arm["intent"] = make_intent(arm["subject"], quantity=740, reason="TIMEOUT_RUNNER", now=clock.now)
        q = v1_quote(TOKEN, SOL, 740, 300000000, now=clock.now)
        assert rf.apply_paper_exit_quote(case, arm, q, 50., clock.now,
            quote_started_at=clock.now, fx_observation=fx(clock.now, 50.))
        assert rf.validate_paper_cash_terminal(case, arm, clock.now)
        assert arm["net_pnl_sol"] == pytest.approx(.0396+.297-.1-.000075)
        assert arm["net_pnl_usd"] == pytest.approx(7.92+14.85-10-.00875)
        damaged = copy.deepcopy(case)
        damaged["prefix"]["estimated_fees_usd"] = 0.
        assert not rf.validate_paper_cash_terminal(damaged, arm, clock.now)
    assert paper._PORTFOLIO[TOKEN]["qty_lamports"] == 740  # Counterfactual arms cannot sell the real PAPER runner.


@pytest.mark.parametrize("proof_field", cash.PROOF_FIELDS)
def test_terminal_does_not_accept_changed_original_proof_in_one_arm(tmp_path, proof_field):
    case = closed_cohort(tmp_path, n=1)[0]
    arm = next(iter(case["arms"].values()))
    assert rf.validate_paper_cash_terminal(case, arm, T0+dt.timedelta(hours=27))
    arm["subject"].pop(proof_field)
    assert not rf.validate_paper_cash_terminal(case, arm, T0+dt.timedelta(hours=27))


def test_rehashed_original_receipts_invalidate_cohort_and_cached_policy(tmp_path):
    cases = closed_cohort(tmp_path)
    stamp = T0 + dt.timedelta(hours=27)
    assert rf.evaluate_completed_cohorts(root=tmp_path, cfg=cfg(), now=stamp)["status"] == "selected"
    assert runner_price_policy.parse_policy(rf.entry_policy(cfg(), root=tmp_path, now=stamp))["max_price_drawdown_pct"] == 15
    case, base = cases[0], rf._directory(tmp_path)
    path = base / "enrollment_sources" / (case["case_id"]+".json")
    source = read_json_strict(path)
    source["prefix"]["exit_fill_events"][0]["response"]["price_used_usd"] += .01
    rehash(source)
    write_json_atomic(path, source)
    receipt_path = base / "enrollment_receipts" / path.name
    receipt = read_json_strict(receipt_path)
    receipt["source_sha256"] = source["payload_sha256"]
    write_json_atomic(receipt_path, receipt)
    for row in [case["prefix"]] + [arm["subject"] for arm in case["arms"].values()]:
        row["exit_fill_events"][0]["response"]["price_used_usd"] += .01
    case["enrollment_source_sha256"] = source["payload_sha256"]
    write_json_atomic(base / "closed" / path.name, case)
    assert not rf.compare_cohort(cases, now=stamp)["accepted"]
    assert not intake.population_matches(tmp_path, case["cohort_id"], cases)
    assert runner_price_policy.parse_policy(rf.entry_policy(cfg(), root=tmp_path, now=stamp))["max_price_drawdown_pct"] == 20


@pytest.mark.parametrize("field,value", [("realized_proceeds_sol", True), ("realized_proceeds_usd", "4"),
    ("estimated_fees_sol", float("nan")), ("estimated_fees_usd", float("inf")),
    ("entry_notional_usd", 10**1000)])
def test_untyped_nonfinite_or_overflowing_original_money_is_not_prepared(field, value):
    row = entry(token_address=TOKEN)
    row[field] = value
    assert rf.prepare_partial_case(row, cfg=cfg(), now=CAPTURED) is None


def test_runner_version_cannot_disguise_original_partial_as_zero_cash(tmp_path):
    case = closed_cohort(tmp_path, n=1)[0]
    arm = next(iter(case["arms"].values()))
    case["prefix"].update(partial_taken=False, realized_qty=0, realized_proceeds_sol=0., realized_proceeds_usd=0.)
    assert not rf.validate_paper_cash_terminal(case, arm, T0+dt.timedelta(hours=27))


def test_public_prepared_proof_drops_arbitrary_nested_payload():
    row = entry(token_address=TOKEN)
    row["exit_fill_events"][0]["response"]["private_header"] = "synthetic-private-value"
    case = rf.prepare_partial_case(row, cfg=cfg(), now=CAPTURED)
    assert case is not None
    assert "private_header" not in case["prefix"]["exit_fill_events"][0]["response"]
