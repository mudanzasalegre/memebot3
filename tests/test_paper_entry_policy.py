"""All cash/cohorts here are synthetic fixtures, never profitability evidence."""
from __future__ import annotations

import ast
import asyncio
import copy
import datetime as dt
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import base58
import pytest

from config.config import CFG
from research_loop import entry_gate_policy as transport
from runtime import paper_entry_policy as policy


def config(**changes):
    return replace(CFG, DRY_RUN=True, RESEARCH_RANK_CANARY_MIN_SCORE=65,
        RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_RANK_SCORE=60,
        RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_PRICE5M=40,
        RESEARCH_RANK_CANARY_PAPER_NORMAL_MAX_PRICE5M=120,
        RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_TXNS_5M=300,
        RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_LIQUIDITY_USD=15000, **changes)


PARAMETERS = {"RESEARCH_RANK_CANARY_MIN_SCORE": 60}


def cohort(cfg, *, start=None):
    start = start or dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(hours=27)
    plan = {"version": transport.VERSION, "role": transport.ROLE, "gate": "rank_canary",
        "parameters": PARAMETERS, "configured_hash": policy.configured_hash(cfg, "rank_canary"),
        "planned_at": start.isoformat(), "cohort_started_at": start.isoformat(),
        "cohort_ends_at": (start + dt.timedelta(hours=24)).isoformat(),
        "run_id": "SYNTHETIC_UNIT_FIXTURE_NOT_A_PRODUCTION_RUN", "run_started_at": start.isoformat(),
        "exit_rule_id": policy.digest({"rule": "synthetic fixed common paper outcome"})}
    plan_id = policy.digest(plan)
    cases = []
    for i in range(50):
        mint = base58.b58encode((i + 1).to_bytes(32, "big")).decode()
        decision = start + dt.timedelta(minutes=i)
        close = start + dt.timedelta(hours=26, seconds=i)
        features = {"address": mint, "entry_lane": "pump_early_sniper_research", "rank_score": 62 if i < 30 else 72,
            "price_pct_5m": 70, "txns_last_5m": 600, "liquidity_usd": 20000,
            "market_cap_usd": 50000, "has_jupiter_route": True, "liquidity_is_proxy": False}
        prefix = {"dry_run": True, "run_id": plan["run_id"], "run_started_at": plan["run_started_at"],
            "opened_at": decision.isoformat(), "amount_sol": .1, "entry_notional_usd": 10., "entry_sol_usd": 100.,
            "entry_qty": 1000, "qty_lamports": 1000, "realized_qty": 0,
            "realized_proceeds_sol": 0., "realized_proceeds_usd": 0., "execution_fill_count": 1,
            "estimated_fees_sol": .000025, "estimated_fees_usd": .0025,
            "quantity_basis": "quoted_raw_spl_units", "buy_price_usd": 1.,
            "entry_route_quote": {"in_amount": 100000000, "out_amount": 1000, "route_count": 1,
                                  "impact_bps": 20., "max_impact_pct": 8.},
            "execution_cost_model": {"version": "estimated-v1", "observed_execution": False,
                                     "slippage_bps": 0., "fee_sol_per_fill": .000025}}
        subject = {**copy.deepcopy(prefix), "qty_lamports": 0, "realized_qty": 1000, "execution_fill_count": 2,
                   "realized_proceeds_sol": .2, "realized_proceeds_usd": 20.,
                   "estimated_fees_sol": .00005, "estimated_fees_usd": .005}
        terminal = {"closed": True, "closed_at": close.isoformat(), "subject": subject,
            "net_pnl_sol": .09995, "net_pnl_usd": 9.995, "fills": [{"filled_at": close.isoformat(),
            "intent_at": (close - dt.timedelta(seconds=10)).isoformat(), "observed_execution": False,
            "input_raw_spl": 1000, "output_lamports": 200000000, "sol_usd": 100., "impact_bps": 20.,
            "route_count": 1, "proceeds_sol": .2, "proceeds_usd": 20., "fee_sol": .000025}]}
        case = {"plan_id": plan_id, "token": mint, "decision_at": decision.isoformat(), "features": features,
            "baseline_buy": i >= 30, "challenger_buy": True, "outcomes_complete": True,
            "observation_gap_limit_exceeded": False, "observation_count": 1560,
            "exit_rule_id": plan["exit_rule_id"], "cash_rule": "one_common_frozen_entry_and_exit_for_both_gate_arms",
            "cash": {"prefix": prefix, "terminal": terminal}}
        case["case_id"] = policy.digest([plan_id, mint, case["decision_at"], features])
        cases.append(case)
    plan.update(enrollment_complete=True, case_ids=[case["case_id"] for case in cases])
    return plan, cases, start + dt.timedelta(hours=27)


def install(root, cfg):
    plan, cases, now = cohort(cfg)
    evaluation = transport.compare_cohort(plan, cases, cfg, now=now)
    assert evaluation["accepted"]
    bundle = {"plan": plan, "evaluation": evaluation}
    directory = root / "data/research/entry_gate_forward"
    (directory / "closed").mkdir(parents=True)
    (directory / "evaluations").mkdir()
    name = policy.digest(bundle) + ".json"
    for case in cases:
        (directory / "closed" / f"{case['case_id']}.json").write_text(json.dumps(case))
    (directory / "evaluations" / name).write_text(json.dumps(bundle))
    manifest = {"version": transport.VERSION, "role": transport.ROLE, "revision": "a" * 20,
        "selected_at": now.isoformat(), "expires_at": (now + dt.timedelta(days=7)).isoformat(),
        "evidence_name": name, "evidence_sha256": policy.digest(bundle), "parameters": PARAMETERS}
    (directory / "active_policy.json").write_text(json.dumps(manifest))
    transport._VERIFIED_CACHE.clear()
    return directory, manifest, now


@pytest.mark.parametrize("parameters", [
    {"DRY_RUN": True}, {"RESEARCH_RANK_CANARY_SIZE_SOL": .1}, {"STOP_LOSS_PCT": -30},
    {"LATE_MOMENTUM_WATCH_MAX_PRICE_IMPACT_PCT": 20}, {"RESEARCH_RANK_CANARY_MIN_SCORE": float("nan")},
    {"RESEARCH_RANK_CANARY_MIN_SCORE": True}, {"RESEARCH_RANK_CANARY_MIN_SCORE": 50},
    {"RESEARCH_RANK_CANARY_MIN_SCORE": 60, "LATE_MOMENTUM_WATCH_MIN_PRICE5M": 250}, {},
    {"RESEARCH_RANK_CANARY_MIN_SCORE": 65},
])
def test_unsafe_inert_nonfinite_or_unbounded_parameters_are_rejected(parameters):
    with pytest.raises((ValueError, TypeError, AttributeError)):
        policy.validate_parameters(config(), parameters)


def test_scoped_threshold_is_real_immutable_and_does_not_leak():
    cfg = config()
    token = cohort(cfg)[1][0]["features"]
    assert not transport.gate_decision("rank_canary", token, cfg)
    with policy.parameter_scope(cfg, PARAMETERS, revision="synthetic"):
        assert transport.gate_decision("rank_canary", token, cfg)
        assert cfg.RESEARCH_RANK_CANARY_MIN_SCORE == 65
        view = policy.entry_config(cfg)
        assert view.RESEARCH_RANK_CANARY_SIZE_SOL == cfg.RESEARCH_RANK_CANARY_SIZE_SOL
        with pytest.raises(AttributeError):
            view.DRY_RUN = False
        snapshot = policy.snapshot()
        snapshot["parameters"]["RESEARCH_RANK_CANARY_MIN_SCORE"] = 0
        assert policy.snapshot()["parameters"] == PARAMETERS
        assert policy.entry_config(replace(cfg)) is not view
        assert policy.entry_config(cfg, live=True) is cfg
        assert policy.entry_config(cfg, dry_run=False) is cfg
        with policy.baseline_scope():
            assert policy.snapshot() is None
        assert policy.snapshot() is not None
    assert policy.snapshot() is None
    assert not transport.gate_decision("rank_canary", token, cfg)


def test_live_configuration_cannot_bind():
    with pytest.raises(ValueError):
        with policy.parameter_scope(replace(config(), DRY_RUN=False), PARAMETERS, revision="synthetic"):
            pass


def test_sniper_and_moonshot_scopes_reach_their_actual_filters():
    from analytics.sniper_research_subprofiles import evaluate_sniper_research_subprofile
    from analytics.moonshot_micro_lottery import evaluate_moonshot_micro_lottery
    cfg = replace(config(), SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M=100,
        SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M=150, SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M=500,
        SNIPER_RESEARCH_MOMENTUM_MIN_LIQUIDITY_USD=15000, SNIPER_RESEARCH_MOMENTUM_MAX_MCAP_USD=70000,
        MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M=300, MOONSHOT_MICRO_LOTTERY_MIN_TXNS_5M=80)
    token = {"entry_lane": "pump_early_sniper_research", "dex_id": "pumpswap", "price_pct_5m": 250,
        "liquidity_usd": 20000, "txns_last_5m": 800, "market_cap_usd": 50000,
        "has_jupiter_route": True, "trend": "up", "helius_top10_share_pct": 20}
    assert not evaluate_sniper_research_subprofile(token, cfg=cfg).allowed
    with policy.parameter_scope(cfg, {"SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M": 300}, revision="synthetic"):
        assert evaluate_sniper_research_subprofile(token, cfg=cfg).allowed
        assert not evaluate_sniper_research_subprofile({**token, "cluster_bad": True}, cfg=cfg).allowed
    moon = {"source": "pumpfun", "price_pct_5m": 350, "txns_last_5m": 320,
            "market_cap_usd": 80000, "age_minutes": 2, "has_jupiter_route": False}
    assert evaluate_moonshot_micro_lottery(moon, cfg=cfg, dry_run=True, live=False).allowed
    with policy.parameter_scope(cfg, {"MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M": 400}, revision="synthetic"):
        assert not evaluate_moonshot_micro_lottery(moon, cfg=cfg, dry_run=True, live=False).allowed
        assert not evaluate_moonshot_micro_lottery(moon, cfg=cfg, dry_run=False, live=True).allowed


def test_counterfactual_gate_evaluation_does_not_write_actual_audit(monkeypatch):
    from analytics import research_rank_canary
    monkeypatch.setattr(research_rank_canary, "_record_audit", lambda *args, **kw: pytest.fail("audit write"))
    monkeypatch.setattr(research_rank_canary, "_record_event", lambda *args, **kw: pytest.fail("event write"))
    cfg = config()
    plan, cases, now = cohort(cfg)
    assert transport.compare_cohort(plan, cases, cfg, now=now)["accepted"]


def test_concurrent_tasks_and_cancelled_evaluations_are_isolated():
    cfg = config()
    async def simulate(value):
        with policy.parameter_scope(cfg, {"RESEARCH_RANK_CANARY_MIN_SCORE": value}, revision=str(value)):
            await asyncio.sleep(0)
            assert policy.entry_config(cfg).RESEARCH_RANK_CANARY_MIN_SCORE == value
            await asyncio.sleep(0)
        assert policy.snapshot() is None
    async def main():
        await asyncio.gather(simulate(60), simulate(62))
        assert policy.entry_config(cfg) is cfg
        with pytest.raises(asyncio.TimeoutError):
            with policy.parameter_scope(cfg, PARAMETERS, revision="cancelled"):
                await asyncio.wait_for(asyncio.sleep(.1), timeout=.001)
        assert policy.snapshot() is None
    asyncio.run(main())


def test_nonapplicable_component_is_not_buy_permission():
    assert transport.gate_decision("sniper_subprofile", {"entry_lane": "other"}, config()) is None
    assert transport.gate_decision("rank_canary", {"entry_lane": "other"}, config()) is None


def test_late_watch_scope_can_evaluate_above_1000_without_bypassing_guards():
    from analytics.late_momentum_watch import evaluate_late_momentum_watch
    cfg = replace(config(), LATE_MOMENTUM_WATCH_MAX_PRICE5M=1000,
        LATE_MOMENTUM_WATCH_BUY_ENABLED=True, LATE_MOMENTUM_WATCH_PAPER_CANARY_ENABLED=True)
    token = {"price_pct_5m": 1400, "txns_last_5m": 800, "liquidity_usd": 20000,
             "market_cap_usd": 20000, "rank_score": 75, "has_jupiter_route": True, "price_impact_pct": 2}
    assert evaluate_late_momentum_watch(token, dry_run=True, live=False, cfg=cfg).action == "shadow"
    with policy.parameter_scope(cfg, {"LATE_MOMENTUM_WATCH_MAX_PRICE5M": 1500}, revision="synthetic"):
        assert evaluate_late_momentum_watch(token, dry_run=True, live=False, cfg=cfg).action == "buy"
        assert evaluate_late_momentum_watch({**token, "price_impact_pct": 50}, dry_run=True, live=False, cfg=cfg).action == "shadow"
    assert cfg.LATE_MOMENTUM_WATCH_MAX_PRICE5M == 1000


def test_cash_checked_cohort_is_component_only_not_full_strategy_proof():
    cfg = config()
    plan, cases, now = cohort(cfg)
    result = transport.compare_cohort(plan, cases, cfg, now=now)
    assert result["accepted"]
    assert result["unique_tokens"] == 50 and result["changed_decisions"] == 30
    assert result["paired_lower_mean_sol"] > 0
    assert result["observed_execution"] is False
    assert result["full_strategy_profitability_established"] is False


@pytest.mark.parametrize("fault", ["missing_case", "duplicate_token", "missing_cash", "bad_amount",
    "invented_pnl", "bad_quote", "late_plan", "stale", "wrong_decision", "future_features",
    "unsettled", "coverage_gap", "test_event", "unlisted_case", "unknown_numeric"])
def test_incomplete_replayed_uncosted_or_altered_evidence_cannot_select(fault):
    cfg = config()
    plan, cases, now = cohort(cfg)
    if fault == "missing_case": cases.pop()
    elif fault == "duplicate_token": cases[1]["token"] = cases[0]["token"]
    elif fault == "missing_cash": cases[0]["cash"] = None
    elif fault == "bad_amount": cases[0]["cash"]["prefix"]["amount_sol"] = .02
    elif fault == "invented_pnl": cases[0]["cash"]["terminal"]["net_pnl_sol"] = 1000
    elif fault == "bad_quote": cases[0]["cash"]["terminal"]["fills"][0]["input_raw_spl"] = 999
    elif fault == "late_plan": plan["planned_at"] = (now - dt.timedelta(hours=1)).isoformat()
    elif fault == "stale": now += dt.timedelta(days=3)
    elif fault == "wrong_decision": cases[0]["baseline_buy"] = True
    elif fault == "future_features": cases[0]["features"]["rank_score"] = 99
    elif fault == "unsettled": cases[0]["outcomes_complete"] = False
    elif fault == "coverage_gap": cases[0]["observation_gap_limit_exceeded"] = True
    elif fault == "test_event": cases[0]["test_event"] = True
    elif fault == "unlisted_case": plan["case_ids"].pop()
    elif fault == "unknown_numeric": cases[0]["features"]["rank_score"] = "inf"
    assert not transport.compare_cohort(plan, cases, cfg, now=now)["accepted"]


def test_manifest_rechecks_sources_configuration_expiry_and_role(tmp_path):
    cfg = config()
    directory, manifest, now = install(tmp_path, cfg)
    assert transport.load_selection(cfg, root=tmp_path, now=now)["parameters"] == PARAMETERS
    assert transport.load_selection(cfg, root=tmp_path, now=now + dt.timedelta(days=8)) is None
    assert transport.load_selection(replace(cfg, DRY_RUN=False), root=tmp_path, now=now) is None
    assert transport.load_selection(replace(cfg, PAPER_ENTRY_GATE_AUTO_APPLY=False), root=tmp_path, now=now) is None
    assert transport.load_selection(replace(cfg, RESEARCH_RANK_CANARY_REQUIRE_ROUTE_PAPER=False), root=tmp_path, now=now) is None
    path = next((directory / "closed").glob("*.json"))
    path.write_text("{}")
    assert transport.load_selection(cfg, root=tmp_path, now=now) is None
    manifest["role"] = "profile_exported_not_applied"
    (directory / "active_policy.json").write_text(json.dumps(manifest))
    assert transport.load_selection(cfg, root=tmp_path, now=now) is None


def test_guarded_real_call_path_binds_snapshot_and_restores_it(tmp_path, monkeypatch):
    cfg = config()
    _, _, now = install(tmp_path, cfg)
    original = transport.load_selection
    monkeypatch.setattr(transport, "load_selection", lambda cfg, **kw: original(cfg, root=kw["root"], now=now))
    calls = []
    async def evaluate(token, session):
        await asyncio.sleep(0)
        calls.append((policy.entry_config(cfg).RESEARCH_RANK_CANARY_MIN_SCORE, token["paper_entry_policy"]))
    source = ast.parse((Path(__file__).parents[1] / "run_bot.py").read_text(encoding="utf-8"))
    node = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_evaluate_and_buy_guarded")
    namespace = {"asyncio": asyncio, "CFG": cfg, "PROJECT_ROOT": tmp_path, "SessionLocal": object,
        "EVALUATE_TOKEN_TIMEOUT_S": .1, "_evaluate_and_buy": evaluate,
        "_note_runtime_error": lambda *args: pytest.fail(str(args)), "log": SimpleNamespace(error=lambda *args: None)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "run_bot.py", "exec"), namespace)
    token = {"address": "synthetic", "paper_entry_policy": {"obsolete": True}}
    asyncio.run(namespace["_evaluate_and_buy_guarded"](token, None, source="unit"))
    assert calls[0][0] == 60
    assert calls[0][1]["parameters"] == PARAMETERS
    assert policy.snapshot() is None and cfg.RESEARCH_RANK_CANARY_MIN_SCORE == 65
