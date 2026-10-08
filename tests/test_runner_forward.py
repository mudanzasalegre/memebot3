from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from analytics import exit_policy, runner_price_policy
from research_loop import runner_forward as rf
from research_loop.paper_exit_receipt import make_intent
from paper_fx_fixtures import observation as synthetic_fx
from utils.solana_addr import is_valid_base58_32

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
MINT = "So11111111111111111111111111111111111111112"


def cfg(**changes):
    return SimpleNamespace(**{
        "DRY_RUN": True, "PAPER_RUNNER_RESEARCH_ENABLED": True,
        "PAPER_RUNNER_RESEARCH_AUTO_APPLY": True, "PAPER_RUNNER_RESEARCH_INTERVAL_S": 60,
        "RUNNER_PRICE_TRAILING_PAPER_ENABLED": True, "RUNNER_PRICE_TRAILING_MIN_PEAK_PCT": 300,
        "RUNNER_PRICE_TRAILING_DRAWDOWN_PCT": 20, "RUNNER_PRICE_TRAILING_MAX_HOLD_H": 24,
        **changes,
    })


@pytest.fixture(autouse=True)
def isolated_exit_policy(monkeypatch):
    monkeypatch.setattr(exit_policy, "CFG", replace(
        exit_policy.CFG, DRY_RUN=True, TP_PARTIAL_ENABLED=True,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=True, BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED=True,
        BIRD_TP1_PCT=25, BIRD_TP1_FRACTION=.25, BIRD_TP2_PCT=75, BIRD_TP2_FRACTION=.25,
        BIRD_TP3_PCT=150, BIRD_TP3_FRACTION=.20, BIRD_TP4_PCT=500, BIRD_TP4_FRACTION=.15,
        BIRD_TP5_PCT=1000, BIRD_TP5_FRACTION=.07, BIRD_TP6_PCT=2000, BIRD_TP6_FRACTION=.05,
    ))
    from analytics.api_budget import reset_provider_circuits
    rf._VERIFIED_CACHE.clear()
    rf._ACTIVE_INDEX.clear()
    reset_provider_circuits()
    yield
    reset_provider_circuits()


def entry(**changes):
    row = {
        "dry_run": True, "closed": False, "run_id": "FORWARD_REAL_PAPER",
        "run_started_at": T0.isoformat(), "opened_at": T0.isoformat(), "token_address": MINT,
        "amount_sol": .1, "entry_notional_usd": 10.0, "buy_price_usd": 1.0,
        "entry_qty": 1000, "qty_lamports": 800, "realized_qty": 200,
        "realized_proceeds_usd": 4.0, "realized_proceeds_sol": .04,
        "estimated_fees_usd": .005, "estimated_fees_sol": .00005,
        "execution_fill_count": 2, "execution_cost_model": {
            "version": "estimated-v1", "observed_execution": False,
            "slippage_bps": 0.0, "fee_sol_per_fill": .000025,
        },
        "entry_route_quote": {"in_amount": 100000000, "out_amount": 1000, "max_impact_pct": 8.0, "route_count": 1},
        "quantity_basis": "quoted_raw_spl_units", "partial_taken": True, "partial_count": 1, "partial_fill_events": 1,
        "entry_lane": "normal", "entry_regime": "pump_early", "highest_pnl_pct": 100,
        "runner_trailing_policy": runner_price_policy.freeze_policy(cfg(), dry_run=True),
        **changes,
    }
    if "entry_route_quote" not in changes:
        from execution.quote_receipt import capture_summary
        from quote_fixtures import v1_quote, SOL
        opened = dt.datetime.fromisoformat(row["opened_at"])
        q = v1_quote(SOL, row["token_address"], 100000000, 1000, now=opened)
        row["entry_route_quote"] = capture_summary(q, input_mint=SOL, output_mint=row["token_address"],
            amount=100000000, slippage=q.other["slippageBps"], limit=8., now=opened)
    return row


def quote(quantity=800, output=160000000, *, source=MINT, now=T0, **changes):
    from quote_fixtures import v1_quote, SOL
    q = v1_quote(source, SOL, quantity, output, now=now)
    return SimpleNamespace(**{**vars(q), **changes})


def read_active(root):
    return rf._read(next((rf._directory(root) / "active").glob("*.json")))


def synthetic_cash_decision(case, arm_id, stamp, output):
    """Explicit synthetic complete-cadence fixture, never production evidence."""
    from utils.sol_price import SolUsdObservation
    arm = case["arms"][arm_id]
    arm["subject"].update(closed=False, paper_cash_owner="case:" + rf._hash([case["case_id"], arm_id]))
    for key in ("cash_last_mark", "cash_peak_mark", "cash_total_peak_mark"):
        arm.pop(key, None)
    arm.update(cash_last_observed_at=(stamp - dt.timedelta(seconds=60)).isoformat(),
        cash_observation_count=1559, cash_observation_gap_limit_exceeded=False)
    rate = SolUsdObservation("OK", 100., stamp.timestamp(), stamp.timestamp())
    q = quote(quantity=arm["subject"]["qty_lamports"], output=output, source=case["token"], now=stamp)
    assert rf._observe_cash(case, arm_id, q, rate, stamp, request_decision=False, quote_started_at=stamp)
    return {"current": arm["cash_last_mark"], "remaining_peak": arm["cash_peak_mark"],
            "total_peak": arm["cash_total_peak_mark"], "quote_started_at": stamp.isoformat()}


def closed_cohort(root, *, n=50, baseline=20, start=T0, same_token=False):
    result = []
    for i in range(n):
        # Unique, case-sensitive valid base58 mints, not an imputed IID trade count.
        import base58
        mint = MINT if same_token else base58.b58encode((i + 1).to_bytes(32, "big")).decode()
        prefix = entry(token_address=mint, run_started_at=start.isoformat(), opened_at=start.isoformat(),
                       runner_trailing_policy=runner_price_policy.freeze_policy(
                           cfg(RUNNER_PRICE_TRAILING_DRAWDOWN_PCT=baseline), dry_run=True))
        registered = start + dt.timedelta(minutes=1)
        assert rf.register_partial(prefix, root=root, cfg=cfg(), now=registered)
        path = next(p for p in (rf._directory(root) / "active").glob("*.json")
                    if rf._read(p)["token"] == mint)
        case = rf._read(path)
        # Synthetic complete-cadence metadata, not an observed production run.
        case["observation_count"] = 1560
        for arm_id, arm in case["arms"].items():
            dd = arm["parameters"]["max_price_drawdown_pct"]
            # Synthetic fixture quotes are deliberately not profitability evidence.
            out = {baseline - 5: 200000000, baseline: 160000000, baseline + 5: 120000000}.get(dd, 160000000)
            fill_at = start + dt.timedelta(hours=26, minutes=int((dd - baseline + 5) / 5))
            evidence = synthetic_cash_decision(case, arm_id, fill_at, out)
            arm["intent"] = make_intent(arm["subject"], quantity=800, reason="TIMEOUT_RUNNER", now=fill_at,
                                        cash_valuation=evidence)
            assert rf._apply_quote(case, arm, quote(output=out, source=mint, now=fill_at), 100.0, fill_at,
                                   quote_started_at=fill_at, fx_observation=synthetic_fx(fill_at))
        rf._write(rf._directory(root) / "closed" / path.name, case)
        path.unlink()
        result.append(case)
    return result


def test_registration_uses_shared_prefix_and_is_idempotent(tmp_path):
    original = entry(private_key="must_never_be_copied")
    assert rf.register_partial(original, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    assert not rf.register_partial(original, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=2))
    case = read_active(tmp_path)
    assert "private_key" not in json.dumps(case)
    assert len(case["arms"]) == 3
    assert {arm["subject"]["realized_proceeds_sol"] for arm in case["arms"].values()} == {.04}
    assert original["qty_lamports"] == 800


@pytest.mark.parametrize("changes", [
    {"dry_run": False}, {"test_event": True}, {"run_id": ""}, {"run_started_at": None},
    {"amount_sol": .02}, {"partial_taken": False}, {"quantity_basis": "synthetic_paper_units"},
    {"qty_lamports": 999}, {"estimated_fees_sol": float("nan")}, {"runner_trailing_policy": "broken"},
    {"entry_lane": "pump_early_birth_probe_micro_canary"},
    {"partial_fill_events": 2},
])
def test_invalid_live_synthetic_or_micro_prefix_cannot_enroll(tmp_path, changes):
    assert not rf.register_partial(entry(**changes), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    assert not list(tmp_path.rglob("*.json"))


def test_capacity_gap_is_explicit_without_vetoing_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "MAX_ACTIVE", 1)
    assert rf.register_partial(entry(), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    assert not rf.register_partial(entry(opened_at=(T0 + dt.timedelta(seconds=1)).isoformat()),
                                   root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    assert len(list((rf._directory(tmp_path) / "coverage_gaps").glob("*.json"))) == 1


@pytest.mark.parametrize("changes", [{"ok": False}, {"in_amount": 801}, {"out_amount": 0},
                                     {"price_impact_bps": 900}, {"price_impact_bps": float("nan")}])
def test_bad_exit_quote_preserves_quantity_and_intent(tmp_path, changes):
    rf.register_partial(entry(), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    case = read_active(tmp_path)
    arm = next(iter(case["arms"].values()))
    arm["intent"] = make_intent(arm["subject"], quantity=800, reason="stop", now=T0 + dt.timedelta(minutes=2))
    before = copy.deepcopy(arm)
    assert not rf._apply_quote(case, arm, quote(now=T0 + dt.timedelta(minutes=3), **changes), 100.0,
                               T0 + dt.timedelta(minutes=3), quote_started_at=T0 + dt.timedelta(minutes=3),
                               fx_observation=synthetic_fx(T0 + dt.timedelta(minutes=3)))
    assert arm == before


def test_tick_quotes_once_for_identical_arms_and_keeps_unknown_unfilled(tmp_path):
    rf.register_partial(entry(), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    calls = []
    async def prices(_): return {}
    async def sol(): return 100.0
    async def original_fx(): pytest.fail("An unavailable quote must not fetch FX")
    async def failed(**kwargs):
        calls.append(kwargs)
        return quote(ok=False)
    result = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(hours=25),
                                 prices_func=prices, quote_func=failed, sol_price_func=sol, fx_func=original_fx))
    assert result["quote_calls"] == len(calls) == 1
    case = read_active(tmp_path)
    assert all(arm["subject"]["qty_lamports"] == 800 and not arm["closed"] for arm in case["arms"].values())
    assert all(arm["intent"]["reason"] == "TIMEOUT_NOPRICE" for arm in case["arms"].values())
    throttled = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(hours=25, seconds=1),
                                    prices_func=prices, quote_func=failed, sol_price_func=sol))
    assert throttled["status"] == "throttled" and len(calls) == 1


def test_runner_tick_rechecks_fx_after_quote_and_keeps_all_arms_unfilled(tmp_path):
    rf.register_partial(entry(), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    rates, calls = iter([100., None]), []
    async def prices(_): return {}
    async def sol(): return next(rates)
    async def original_fx(): pytest.fail("Unknown scalar FX must remain unfilled without another provider call")
    async def quoted(**kwargs):
        calls.append(kwargs)
        return quote(quantity=kwargs["amount_lamports"], now=T0 + dt.timedelta(hours=25))
    result = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(hours=25),
        prices_func=prices, quote_func=quoted, sol_price_func=sol, fx_func=original_fx))
    assert result["quote_calls"] == len(calls) == 1
    case = read_active(tmp_path)
    assert all(arm["subject"]["qty_lamports"] == 800 and not arm["closed"]
        and not arm["fills"] and arm.get("intent") for arm in case["arms"].values())
    assert case["quote_failures"] == len(case["arms"])


def test_budgeted_shadow_continues_after_actual_position_disappears(tmp_path):
    rf.register_partial(entry(), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    async def prices(_): return {MINT: 1.1}
    async def sol(): return 100.0
    async def valid(**kwargs): return quote(quantity=kwargs["amount_lamports"], now=T0 + dt.timedelta(hours=25))
    async def original_fx(): return synthetic_fx(T0 + dt.timedelta(hours=25))
    asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(hours=25),
                        prices_func=prices, quote_func=valid, sol_price_func=sol, fx_func=original_fx))
    closed = rf._read(next((rf._directory(tmp_path) / "closed").glob("*.json")))
    assert all(arm["closed"] for arm in closed["arms"].values())
    for arm in closed["arms"].values():
        assert arm["net_pnl_sol"] == pytest.approx(.099925)
        assert arm["net_pnl_usd"] == pytest.approx(9.9925)
    assert not (rf._directory(tmp_path) / "active_policy.json").exists()


def test_paired_selection_applies_only_to_new_paper_entries_and_can_rollback(tmp_path):
    closed_cohort(tmp_path)
    before = rf.entry_policy(cfg(), root=tmp_path, now=T0 + dt.timedelta(hours=27))
    selected = rf.evaluate_completed_cohorts(root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(hours=27))
    assert selected["status"] == "selected"
    after = rf.entry_policy(cfg(), root=tmp_path, now=T0 + dt.timedelta(hours=27))
    assert runner_price_policy.parse_policy(before)["max_price_drawdown_pct"] == 20
    assert runner_price_policy.parse_policy(after)["max_price_drawdown_pct"] == 15
    assert runner_price_policy.parse_policy(before)["max_price_drawdown_pct"] == 20  # Entry snapshot unchanged.
    assert json.loads(rf.entry_policy(cfg(DRY_RUN=False), root=tmp_path))["enabled"] is False
    # Next prospective cohort explicitly makes the previous incumbent superior.
    later = T0 + dt.timedelta(days=3)
    cases = closed_cohort(tmp_path, baseline=15, start=later)
    for case in cases:
        # Rebuild conserved cash using a better quote for the 20% challenger.
        prefix = case["prefix"]
        for arm_id, arm in case["arms"].items():
            arm["subject"] = copy.deepcopy(prefix)
            arm["subject"]["runner_trailing_policy"] = json.dumps(arm["parameters"])
            arm.update(closed=False, fills=[])
            fill_at = later + dt.timedelta(hours=26, minutes=int(arm["parameters"]["max_price_drawdown_pct"]))
            out = 240000000 if arm["parameters"]["max_price_drawdown_pct"] == 20 else 160000000
            evidence = synthetic_cash_decision(case, arm_id, fill_at, out)
            arm["intent"] = make_intent(arm["subject"], quantity=800, reason="TIMEOUT_RUNNER", now=fill_at,
                                        cash_valuation=evidence)
            assert rf._apply_quote(case, arm, quote(output=out, source=case["token"], now=fill_at), 100.0, fill_at,
                                   quote_started_at=fill_at, fx_observation=synthetic_fx(fill_at))
        rf._write(rf._directory(tmp_path) / "closed" / f"{case['case_id']}.json", case)
    rollback = rf.evaluate_completed_cohorts(root=tmp_path, cfg=cfg(), now=later + dt.timedelta(hours=27))
    assert rollback["status"] == "selected" and rollback["action"] == "rollback"
    assert runner_price_policy.parse_policy(rf.entry_policy(cfg(), root=tmp_path, now=later + dt.timedelta(hours=27)))["max_price_drawdown_pct"] == 20
    assert len(list((rf._directory(tmp_path) / "history").glob("*.json"))) == 2


def test_incomplete_repeated_or_tampered_cohorts_cannot_select(tmp_path):
    cases = closed_cohort(tmp_path)
    stamp = T0 + dt.timedelta(hours=27)
    assert rf.compare_cohort(cases, now=stamp)["accepted"]
    altered = copy.deepcopy(cases)
    altered[0]["arms"][altered[0]["baseline_id"]]["closed"] = False
    assert not rf.compare_cohort(altered, now=stamp)["accepted"]
    altered = copy.deepcopy(cases)
    for case in altered: case["token"] = MINT
    assert "insufficient_independent_tokens" in rf.compare_cohort(altered, now=stamp)["reasons"]
    altered = copy.deepcopy(cases)
    altered[0]["arms"][altered[0]["baseline_id"]]["net_pnl_sol"] = 999
    assert not rf.compare_cohort(altered, now=stamp)["accepted"]
    assert not rf.compare_cohort(cases, now=T0 + dt.timedelta(days=10))["accepted"]


def test_selected_manifest_requires_original_closed_evidence_and_expires(tmp_path):
    cases = closed_cohort(tmp_path)
    stamp = T0 + dt.timedelta(hours=27)
    rf.evaluate_completed_cohorts(root=tmp_path, cfg=cfg(), now=stamp)
    assert runner_price_policy.parse_policy(rf.entry_policy(cfg(), root=tmp_path, now=stamp))["max_price_drawdown_pct"] == 15
    assert runner_price_policy.parse_policy(rf.entry_policy(cfg(), root=tmp_path, now=stamp + dt.timedelta(days=8)))["max_price_drawdown_pct"] == 20
    path = rf._directory(tmp_path) / "closed" / f"{cases[0]['case_id']}.json"
    damaged = rf._read(path)
    damaged["prefix"]["estimated_fees_sol"] = 0
    rf._write(path, damaged)
    assert runner_price_policy.parse_policy(rf.entry_policy(cfg(), root=tmp_path, now=stamp))["max_price_drawdown_pct"] == 20


def test_unreadable_file_prevents_survivor_only_selection(tmp_path):
    closed_cohort(tmp_path)
    bad = rf._directory(tmp_path) / "closed" / "broken.json"
    bad.write_text("{not-json", encoding="utf-8")
    result = rf.evaluate_completed_cohorts(root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(hours=27))
    assert result["status"] == "awaiting_comparable_evidence"
    assert not (rf._directory(tmp_path) / "active_policy.json").exists()


def test_live_tick_never_queries_or_writes(tmp_path):
    result = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(DRY_RUN=False)))
    assert result["status"] == "disabled" and not list(tmp_path.rglob("*"))


def test_malformed_external_files_fail_closed_without_crashing(tmp_path):
    assert not rf.compare_cohort([{"cohort_id": "../../unsafe"}], now=T0)["accepted"]
    assert not rf.compare_cohort([{"arms": []}], now=T0)["accepted"]
    rf._write(rf._directory(tmp_path) / "active_policy.json", {"parameters": [], "role": "paper_runner_exit_only"})
    assert runner_price_policy.parse_policy(rf.entry_policy(cfg(), root=tmp_path, now=T0))["max_price_drawdown_pct"] == 20


def test_paper_buy_consumes_selection_and_first_partial_enrolls_isolated_store(tmp_path, monkeypatch):
    from trader import papertrading as paper
    from fetcher import jupiter_router as router
    from test_jupiter_quote_contract import payload, hop
    closed_cohort(tmp_path)
    stamp = T0 + dt.timedelta(hours=27)
    rf.evaluate_completed_cohorts(root=tmp_path, cfg=cfg(), now=stamp)
    monkeypatch.setattr(rf, "_now", lambda: stamp)
    monkeypatch.setattr(paper, "utc_now", lambda: stamp)
    monkeypatch.setattr(paper, "CFG", replace(
        paper.CFG, DRY_RUN=True, PAPER_EXACT_TRADE_SIZE_ENABLED=True,
        PAPER_RUNNER_RESEARCH_ENABLED=True, PAPER_RUNNER_RESEARCH_AUTO_APPLY=True,
        RUNNER_PRICE_TRAILING_PAPER_ENABLED=True, RUNNER_PRICE_TRAILING_MIN_PEAK_PCT=300,
        RUNNER_PRICE_TRAILING_DRAWDOWN_PCT=20, RUNNER_PRICE_TRAILING_MAX_HOLD_H=24,
    ))
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "data" / "paper_portfolio.json")
    monkeypatch.setattr(paper, "_PORTFOLIO", {})
    monkeypatch.setattr(paper, "runtime_context_payload", lambda: {"run_id": "isolated_forward", "run_started_at": T0.isoformat()})
    monkeypatch.delenv("TRADING_HOURS", raising=False)
    monkeypatch.delenv("TRADING_HOURS_EXTRA", raising=False)
    monkeypatch.setenv("PAPER_FILL_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("PAPER_FILL_FEE_SOL", "0.000025")
    token = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    async def get_quote(**kwargs):
        if kwargs["input_mint"] == MINT:
            amount, output = 100000000, 1000
        else:
            amount = kwargs["amount_lamports"]
            output = amount * 200000
        source, target = kwargs["input_mint"], kwargs["output_mint"]
        body = payload(amount=amount, output=output)
        body.update(inputMint=source, outputMint=target, priceImpactPct="0.002",
                    slippageBps=router.DEFAULT_SLIPPAGE_BPS)
        body["routePlan"] = [hop(source, target, amount, output)]
        q = router._checked_quote(body, input_mint=source, output_mint=target,
            amount=amount, slippage=router.DEFAULT_SLIPPAGE_BPS, direct=False)
        q.other["received_at_utc"] = stamp.isoformat()
        return q
    async def buy_price(**_): return (1.0, "jupiter")
    async def jupiter_price(_): return 1.0
    async def entry_notional(_): return 10.0
    async def sol(): return 100.0
    monkeypatch.setattr(paper.jupiter_router, "get_routing_quote", get_quote)
    monkeypatch.setattr(paper.jupiter_router, "routing_quote_slippage_bps", lambda: router.DEFAULT_SLIPPAGE_BPS)
    monkeypatch.setattr(paper.jupiter_price, "get_usd_price", jupiter_price)
    monkeypatch.setattr(paper, "_resolve_buy_price_usd", buy_price)
    from paper_fx_fixtures import install
    install(monkeypatch, paper)
    monkeypatch.setattr(paper, "get_sol_usd", sol)
    async def run():
        bought = await paper.buy(token, .1, entry_regime="pump_early", entry_lane="normal", require_jupiter_for_buy=True)
        assert bought["qty_lamports"] == 1000
        snapshot = bought["runner_trailing_policy"]
        assert runner_price_policy.parse_policy(snapshot)["max_price_drawdown_pct"] == 15
        assert paper._PORTFOLIO[token]["entry_route_quote"]["route_count"] == 1
        await paper.sell(token, 200)
        case = read_active(tmp_path)
        assert case["prefix"]["partial_fill_events"] == 1
        assert case["baseline_id"] == rf._policy_id(runner_price_policy.parse_policy(snapshot))
        assert case["prefix"]["highest_pnl_pct"] == pytest.approx(100)
        assert case["prefix"]["qty_lamports"] == 800
        await paper.sell(token, 100)
        assert read_active(tmp_path)["prefix"]["qty_lamports"] == 800
        assert paper._PORTFOLIO[token]["runner_trailing_policy"] == snapshot
    asyncio.run(run())


def test_idle_tick_evaluates_closed_cohort_without_any_provider_call(tmp_path):
    closed_cohort(tmp_path)
    async def forbidden(*args, **kwargs):
        pytest.fail("An idle forward selection must not query a provider")
    result = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(hours=27),
                                 prices_func=forbidden, quote_func=forbidden, sol_price_func=forbidden))
    assert result["status"] == "idle" and result["quote_calls"] == 0
    assert result["selection"]["status"] == "selected"


@pytest.mark.parametrize("mutation", ["observation_gap", "zero_observations", "late_settlement", "entry_window", "conflicting_quote"])
def test_incomplete_temporal_or_quote_coverage_cannot_select(tmp_path, mutation):
    cases = closed_cohort(tmp_path)
    case = cases[0]
    if mutation == "observation_gap":
        case["observation_gap_limit_exceeded"] = True
    elif mutation == "zero_observations":
        case["observation_count"] = 0
    elif mutation == "late_settlement":
        next(iter(case["arms"].values()))["closed_at"] = (T0 + dt.timedelta(days=4)).isoformat()
    elif mutation == "entry_window":
        case["registered_at"] = (T0 + dt.timedelta(days=2)).isoformat()
    else:
        base_arm = case["arms"][case["baseline_id"]]
        other = next(arm for key, arm in case["arms"].items() if key != case["baseline_id"])
        other["fills"][0]["filled_at"] = base_arm["fills"][0]["filled_at"]
        other["fills"][0]["intent_at"] = base_arm["fills"][0]["intent_at"]
        other["closed_at"] = base_arm["closed_at"]
    assert not rf.compare_cohort(cases, now=T0 + dt.timedelta(days=4))["accepted"]


def test_provider_throttle_pauses_secondary_work_and_keeps_pending_arm(tmp_path):
    rf.register_partial(entry(), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    calls = []
    async def prices(_): return {}
    async def sol(): return 100.0
    async def original_fx(): pytest.fail("An unavailable quote must not fetch FX")
    async def limited(**kwargs):
        calls.append(kwargs)
        return quote(ok=False, other={"status": 429})
    first = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(hours=25),
                                prices_func=prices, quote_func=limited, sol_price_func=sol, fx_func=original_fx))
    assert first["quote_calls"] == 1
    later = asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(hours=25, minutes=1),
                                prices_func=prices, quote_func=limited, sol_price_func=sol))
    assert later["status"] == "provider_degraded" and len(calls) == 1
    assert all(arm.get("intent") for arm in read_active(tmp_path)["arms"].values())


def test_monitor_market_and_exact_quote_are_reused_without_extra_requests(tmp_path):
    rf.register_partial(entry(buy_liquidity_usd=10000), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    stamp = T0 + dt.timedelta(hours=25)
    assert rf.observe_market(MINT, 1.1, liq_now=1, root=tmp_path, cfg=cfg(), now=stamp) == 1
    assert rf.observe_quote(MINT, quote(quantity=799, now=stamp), 100.0, root=tmp_path, cfg=cfg(), now=stamp,
                            quote_started_at=stamp, fx_observation=synthetic_fx(stamp)) == 0
    assert rf.observe_quote(MINT, quote(now=stamp), 100.0, root=tmp_path, cfg=cfg(), now=stamp,
                            quote_started_at=stamp, fx_observation=synthetic_fx(stamp)) == 3
    assert rf.observe_quote(MINT, quote(now=stamp), 100.0, root=tmp_path, cfg=cfg(), now=stamp,
                            quote_started_at=stamp, fx_observation=synthetic_fx(stamp)) == 0
    assert all(len(arm["fills"]) == 1 and arm["subject"]["qty_lamports"] == 0
               for arm in read_active(tmp_path)["arms"].values())


def test_quote_hook_during_secondary_network_wait_cannot_duplicate_fill(tmp_path):
    rf.register_partial(entry(), root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    stamp = T0 + dt.timedelta(hours=25)
    async def prices(_): return {}
    async def sol(): return 100.0
    async def original_fx(): return synthetic_fx(stamp)
    async def interleaved(**kwargs):
        assert rf.observe_quote(MINT, quote(now=stamp), 100.0, root=tmp_path, cfg=cfg(), now=stamp,
                                quote_started_at=stamp, fx_observation=synthetic_fx(stamp)) == 3
        return quote(now=stamp)
    asyncio.run(rf.tick(root=tmp_path, cfg=cfg(), now=stamp, prices_func=prices,
                        quote_func=interleaved, sol_price_func=sol, fx_func=original_fx))
    case = rf._read(next((rf._directory(tmp_path) / "closed").glob("*.json")))
    assert all(len(arm["fills"]) == 1 and arm["net_pnl_sol"] == pytest.approx(.099925)
               for arm in case["arms"].values())
