"""Synthetic clocks/prices/quotes only; no real run or profit is certified."""
import asyncio
import datetime as dt
import json
from unittest.mock import AsyncMock
from dataclasses import replace
from types import SimpleNamespace

import base58
import pytest
from research_loop.paper_exit_receipt import make_intent

from analytics import exit_policy, api_budget
from config.config import CFG
from research_loop import entry_gate_forward as bank, entry_gate_policy as evaluator, forward_budget as store
from runtime import paper_entry_policy as policy
from utils.sol_price import SolUsdObservation

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)


def fx(stamp, **changes):
    return SolUsdObservation(**{"status": "OK", "price_usd": 100., "received_at": stamp.timestamp(),
                               "market_updated_at": stamp.timestamp(), **changes})


def synthetic_cash_decision(record, stamp, output):
    """Explicit synthetic full-cadence fixture using the actual cash consumer."""
    from research_loop import runner_forward
    row, arm_id = bank.cash_case(record)
    arm = row["arms"][arm_id]
    arm.update(cash_observation_count=1559, cash_last_observed_at=(stamp - dt.timedelta(seconds=60)).isoformat(),
               cash_observation_gap_limit_exceeded=False)
    q = quote(quantity=arm["subject"]["qty_lamports"], output=output, source=record["token"], now=stamp)
    assert runner_forward._observe_cash(row, arm_id, q, fx(stamp), stamp, request_decision=False, quote_started_at=stamp)
    return {"current": arm["cash_last_mark"], "remaining_peak": arm["cash_peak_mark"],
            "total_peak": arm["cash_total_peak_mark"], "quote_started_at": stamp.isoformat()}


def config(**changes):
    return replace(CFG, **{"DRY_RUN": True, "PAPER_ENTRY_RESEARCH_ENABLED": True,
        "PAPER_ENTRY_GATE_AUTO_APPLY": True, "PAPER_ENTRY_RESEARCH_SAMPLE_S": 900,
        "RESEARCH_RANK_CANARY_MIN_SCORE": 65, "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_RANK_SCORE": 65,
        "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_TXNS_5M": 300,
        "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_LIQUIDITY_USD": 15000, **changes})


def token(i=1, **changes):
    return {"address": base58.b58encode(i.to_bytes(32, "big")).decode(),
        "entry_lane": "pump_early_sniper_research", "rank_score": 62, "price_pct_5m": 70,
        "liquidity_usd": 20000, "market_cap_usd": 50000, "txns_last_5m": 600,
        "has_jupiter_route": True, "liquidity_is_proxy": False, **changes}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("PAPER_FILL_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("PAPER_FILL_FEE_SOL", "0.000025")
    monkeypatch.setattr(exit_policy, "CFG", replace(CFG, TP_PARTIAL_ENABLED=False))
    api_budget.reset_provider_circuits()
    evaluator._VERIFIED_CACHE.clear()
    from utils import sol_price
    monkeypatch.setattr(sol_price, "get_sol_usd_observation",
        AsyncMock(side_effect=AssertionError("Synthetic entry tests must not call an FX provider")))
    yield
    api_budget.reset_provider_circuits()


def capture(root, cfg, row=None, now=T0, start=T0, gate="rank_canary"):
    with bank.capture_scope(cfg, root=root, run_context={"run_id": "SYNTHETIC_COLLECTOR_FIXTURE",
            "started_at": start.isoformat(), "test_event": False}, allow_test_capture=True, submit_tasks=False):
        return bank.capture_gate(gate, row or token(), cfg, now=now)


def quote(quantity=100000000, output=1000, *, source=None, target=None, now=T0, **changes):
    from quote_fixtures import v1_quote, SOL
    q = v1_quote(source or token()["address"], target or SOL, quantity, output, now=now)
    return SimpleNamespace(**{**vars(q), **changes})


def fill(root, cfg, case_id, now=T0 + dt.timedelta(seconds=1), changes=None):
    async def prices(tokens): return {mint: 1. for mint in tokens}
    async def sol(): return 100.
    async def rate(): return fx(now)
    async def quoted(**kwargs): return quote(source=kwargs["input_mint"], target=kwargs["output_mint"], now=now, **(changes or {}))
    return asyncio.run(bank.fill_entry(case_id, root=root, cfg=cfg, now=now,
        prices_func=prices, quote_func=quoted, sol_price_func=sol, fx_func=rate))


def test_entry_fx_unavailable_after_quote_cannot_create_cash_prefix(tmp_path):
    cfg = config()
    identity = capture(tmp_path, cfg)
    rates = iter([100., None])
    async def prices(tokens): return {mint: 1. for mint in tokens}
    async def sol(): return next(rates)
    async def quoted(**kwargs):
        return quote(source=kwargs["input_mint"], target=kwargs["output_mint"], now=T0 + dt.timedelta(seconds=1))
    assert not asyncio.run(bank.fill_entry(identity, root=tmp_path, cfg=cfg, now=T0 + dt.timedelta(seconds=1),
        prices_func=prices, quote_func=quoted, sol_price_func=sol))
    record = case(tmp_path, identity, state="invalid")
    assert record["cash"] is None and record["outcomes_complete"] is False


def test_entry_gate_exit_rechecks_fx_after_quote_without_fabricating_fill(tmp_path):
    cfg = config()
    identity = capture(tmp_path, cfg)
    assert fill(tmp_path, cfg, identity)
    before = case(tmp_path, identity)["cash"]["terminal"]["subject"]
    rates, calls = iter([100., None]), []
    async def prices(_): return {}
    async def sol(): return next(rates)
    async def quoted(**kwargs):
        calls.append(kwargs)
        return quote(quantity=kwargs["amount_lamports"], output=200000000,
            source=kwargs["input_mint"], target=kwargs["output_mint"], now=T0 + dt.timedelta(hours=25))
    result = asyncio.run(bank.tick(root=tmp_path, cfg=cfg, now=T0 + dt.timedelta(hours=25),
        prices_func=prices, quote_func=quoted, sol_price_func=sol))
    assert result["quote_calls"] == len(calls) == 1
    record = case(tmp_path, identity)
    assert record["outcomes_complete"] is False and not record["cash"]["terminal"]["fills"]
    assert record["cash"]["terminal"]["subject"]["qty_lamports"] == before["qty_lamports"]


def case(root, case_id, state="active"):
    return store.read(bank.directory(root) / state / f"{case_id}.json")


def test_registration_precedes_quote_is_sampled_and_has_no_secrets(tmp_path):
    cfg = config()
    identity = capture(tmp_path, cfg, token(private_key="NEVER_STORE", target_pnl_pct=5000))
    assert identity
    record = case(tmp_path, identity)
    assert record["baseline_buy"] is False and record["challenger_buy"] is True
    assert record["cash"] is None and record["outcomes_complete"] is False
    assert "NEVER_STORE" not in json.dumps(record)
    assert "target_pnl_pct" not in record["features"]
    assert capture(tmp_path, cfg, token(2), now=T0 + dt.timedelta(seconds=61)) is None
    assert capture(tmp_path, cfg, token(), now=T0 + dt.timedelta(minutes=16)) is None
    assert fill(tmp_path, cfg, identity)
    assert case(tmp_path, identity)["cash"]["prefix"]["amount_sol"] == .1
    assert not fill(tmp_path, cfg, identity)


@pytest.mark.parametrize("fault", [{"ok": False}, {"in_amount": 20000000}, {"in_amount": 100000000.}, {"out_amount": 0},
    {"price_impact_bps": float("nan")}, {"price_impact_bps": 100000}, {"other": {"routePlan_len": 0}},
    {"other": {"routePlan_len": True}}])
def test_invalid_exact_entry_never_becomes_zero_or_a_filled_trade(tmp_path, fault):
    cfg = config()
    identity = capture(tmp_path, cfg)
    assert not fill(tmp_path, cfg, identity, changes=fault)
    invalid = case(tmp_path, identity, "invalid")
    assert invalid["cash"] is None and invalid["outcomes_complete"] is False
    assert "net_pnl_sol" not in invalid


def test_expired_or_unreserved_entry_does_not_call_provider(tmp_path):
    cfg = config()
    identity = capture(tmp_path, cfg)
    assert not fill(tmp_path, cfg, identity, now=T0 + dt.timedelta(seconds=31))
    assert case(tmp_path, identity, "invalid")["invalid_reason"] == "entry_quote_deadline_exceeded"


def test_both_explicit_skips_need_no_fabricated_cash(tmp_path):
    cfg = config()
    identity = capture(tmp_path, cfg, token(rank_score=10))
    record = case(tmp_path, identity, "closed")
    assert record["outcomes_complete"] and record["cash"] is None
    assert not record["baseline_buy"] and not record["challenger_buy"]


@pytest.mark.parametrize("context", [{}, {"run_id": "test", "started_at": T0.isoformat(), "test_event": True}])
def test_missing_or_smoke_identity_cannot_collect(tmp_path, context):
    cfg = config()
    with bank.capture_scope(cfg, root=tmp_path, run_context=context, allow_test_capture=True):
        assert bank.capture_gate("rank_canary", token(), cfg, now=T0) is None
    assert not list(tmp_path.rglob("*.json"))


def test_live_or_standard_test_execution_does_not_collect(tmp_path):
    cfg = config(DRY_RUN=False)
    assert capture(tmp_path, cfg) is None
    cfg = config()
    with bank.capture_scope(cfg, root=tmp_path, run_context={"run_id": "test", "started_at": T0.isoformat()}):
        assert bank.capture_gate("rank_canary", token(), cfg, now=T0) is None
    assert not list(tmp_path.rglob("*.json"))


def test_shared_budget_is_global_fair_backwards_and_corruption_safe(tmp_path):
    assert store.claim(tmp_path, "runner_exit", now=T0)
    assert not store.claim(tmp_path, "entry_gate", now=T0 + dt.timedelta(seconds=59))
    assert not store.claim(tmp_path, "runner_exit", now=T0 + dt.timedelta(seconds=60), other_pending=True)
    assert store.claim(tmp_path, "entry_gate", now=T0 + dt.timedelta(seconds=60), other_pending=True)
    assert not store.claim(tmp_path, "runner_exit", now=T0 - dt.timedelta(hours=1))
    path = tmp_path / "data/research/paired_forward_budget.json"
    path.write_text("broken")
    assert not store.claim(tmp_path, "entry_gate", now=T0 + dt.timedelta(hours=3))
    assert path.read_text() == "broken"


def test_virtual_case_survives_primary_absence_and_requires_exact_pending_quote(tmp_path):
    cfg = config()
    identity = capture(tmp_path, cfg)
    assert fill(tmp_path, cfg, identity)
    record = case(tmp_path, identity)
    q = record["cash"]["prefix"]["entry_qty"]
    record["cash"]["terminal"]["intent"] = make_intent(record["cash"]["terminal"]["subject"],
        quantity=q, reason="test_stop", now=T0 + dt.timedelta(minutes=1))
    store.write(bank.directory(tmp_path) / "active" / f"{identity}.json", record)
    mint = record["token"]
    assert bank.observe_quote(mint, quote(quantity=q - 1, output=200000000, now=T0 + dt.timedelta(minutes=2)), 100., root=tmp_path, cfg=cfg, now=T0 + dt.timedelta(minutes=2), quote_started_at=T0 + dt.timedelta(minutes=2)) == 0
    assert bank.observe_quote(mint, quote(quantity=q, output=200000000, now=T0 + dt.timedelta(minutes=2)), 100., root=tmp_path, cfg=cfg, now=T0 + dt.timedelta(minutes=2), quote_started_at=T0 + dt.timedelta(minutes=2), fx_observation=fx(T0 + dt.timedelta(minutes=2))) == 1
    assert not (bank.directory(tmp_path) / "active" / f"{identity}.json").exists()
    assert case(tmp_path, identity, "closed")["cash"]["terminal"]["net_pnl_sol"] == pytest.approx(.09995)


def test_corrupt_auxiliary_hooks_do_not_abort_primary_work(tmp_path):
    base = bank.directory(tmp_path)
    store.write(base / "active" / "malformed.json", {"token": token()["address"], "cash": "broken"})
    assert bank.observe_quote(token()["address"], quote(), 100., root=tmp_path, cfg=config()) == 0
    assert bank.observe_market(token()["address"], 1., root=tmp_path, cfg=config()) == 0


def complete(root, cfg, *, start=T0, losing=False, gate="rank_canary", features_func=None):
    for i in range(50):
        decision = start + dt.timedelta(seconds=900 * i)
        row = token(i + 1, rank_score=62 if i < 30 else 72)
        if features_func is not None:
            row.update(features_func(i))
        identity = capture(root, cfg, row, now=decision, start=start, gate=gate)
        assert identity and fill(root, cfg, identity, now=decision + dt.timedelta(seconds=1))
        record = case(root, identity)
        # Synthetic complete cadence, not a production observation claim.
        record["observation_count"] = 1560
        quantity = record["cash"]["prefix"]["entry_qty"]
        closed = start + dt.timedelta(hours=26, seconds=i)
        output = 30000000 if losing and i < 30 else 200000000
        evidence = synthetic_cash_decision(record, closed, output)
        record["cash"]["terminal"]["intent"] = make_intent(record["cash"]["terminal"]["subject"],
            quantity=quantity, reason="synthetic_common_exit", now=closed, cash_valuation=evidence)
        store.write(bank.directory(root) / "active" / f"{identity}.json", record)
        assert bank.observe_quote(record["token"], quote(quantity=quantity, output=output, source=record["token"], now=closed), 100., root=root, cfg=cfg, now=closed, quote_started_at=closed, fx_observation=fx(closed)) == 1
    base = bank.directory(root)
    plan_id = store.read(base / "open_plan.json")["plan_id"]
    store.write(base / "heartbeats" / f"{plan_id}.json", {"times": [(start + dt.timedelta(minutes=i)).isoformat() for i in range(1441)]})
    return plan_id, start + dt.timedelta(hours=27)


def test_original_collector_records_select_and_checked_negative_cohort_rolls_back(tmp_path):
    cfg = config()
    identity, now = complete(tmp_path, cfg)
    result = bank.evaluate_plan(tmp_path, cfg, identity, now=now)
    assert result["accepted"]
    selected = evaluator.load_selection(cfg, root=tmp_path, now=now)
    assert selected and selected["parameters"]["RESEARCH_RANK_CANARY_MIN_SCORE"] == 60
    base = bank.directory(tmp_path)
    (base / "open_plan.json").unlink()  # Isolated cohort transition fixture.
    identity2, now2 = complete(tmp_path, cfg, start=T0 + dt.timedelta(days=3), losing=True)
    result2 = bank.evaluate_plan(tmp_path, cfg, identity2, now=now2)
    assert result2["status"] == "rolled_back_to_configured"
    assert not evaluator.selection_path(tmp_path, "rank_canary").exists()
    assert list((base / "rollbacks").glob("*.json"))


def test_changed_original_journal_or_missing_heartbeat_disables_selection(tmp_path):
    cfg = config()
    identity, now = complete(tmp_path, cfg)
    assert bank.evaluate_plan(tmp_path, cfg, identity, now=now)["accepted"]
    base = bank.directory(tmp_path)
    path = base / "journals" / f"{identity}.json"
    journal = store.read(path)
    journal["events"].pop()
    store.write(path, journal)
    assert evaluator.load_selection(cfg, root=tmp_path, now=now) is None


def test_fresh_price_mode_bypasses_both_positive_and_negative_caches(monkeypatch):
    from fetcher import jupiter_price as prices
    mint = token()["address"]
    monkeypatch.setattr(prices, "_cache_get_ok", lambda _: pytest.fail("positive cache read"))
    monkeypatch.setattr(prices, "_cache_get_nil", lambda _: pytest.fail("negative cache read"))
    async def fetched(tokens):
        return prices.parse_price_payload({key: {"usdPrice": 2.} for key in tokens}, tokens)
    monkeypatch.setattr(prices, "_fetch_batch_with_status", fetched)
    assert asyncio.run(prices.get_many_usd_prices([mint], force_refresh=True))[mint] == 2.


def test_fresh_price_mode_does_not_fabricate_a_fixed_stable_price(monkeypatch):
    from fetcher import jupiter_price as prices
    mint = next(iter(prices._KNOWN_STABLES))
    called = []
    async def fetched(tokens):
        called.append(tokens)
        return prices.parse_price_payload({key: {"usdPrice": .91} for key in tokens}, tokens)
    monkeypatch.setattr(prices, "_fetch_batch_with_status", fetched)
    assert asyncio.run(prices.get_many_usd_prices([mint], force_refresh=True))[mint] == .91
    assert called == [[mint]]


@pytest.mark.parametrize("fault", [{"in_amount": True}, {"other": {"routePlan_len": True}}])
def test_raw_exit_quote_never_accepts_boolean_amount_or_route_count(tmp_path, fault):
    cfg = config()
    identity = capture(tmp_path, cfg)
    assert fill(tmp_path, cfg, identity)
    record = case(tmp_path, identity)
    record["cash"]["terminal"]["intent"] = make_intent(record["cash"]["terminal"]["subject"],
        quantity=1, reason="synthetic_partial", now=T0 + dt.timedelta(seconds=60))
    store.write(bank.directory(tmp_path) / "active" / f"{identity}.json", record)
    bad = quote(quantity=1, output=1000000, now=T0 + dt.timedelta(seconds=61), **fault)
    assert bank.observe_quote(record["token"], bad, 100., root=tmp_path, cfg=cfg,
                              now=T0 + dt.timedelta(seconds=61), quote_started_at=T0 + dt.timedelta(seconds=61)) == 0
    assert case(tmp_path, identity)["cash"]["terminal"]["subject"]["realized_qty"] == 0


@pytest.mark.parametrize("checkpoint", ["open_plan.json", "proposal_cursor.json"])
@pytest.mark.parametrize("payload", ["broken", "{}"])
def test_corrupt_checkpoints_are_preserved_never_reset(tmp_path, checkpoint, payload):
    path = bank.directory(tmp_path) / checkpoint
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload)
    assert capture(tmp_path, config()) is None
    assert path.read_text() == payload
    assert not list((bank.directory(tmp_path) / "plans").glob("*.json"))


def test_changed_entry_configuration_or_run_cannot_extend_original_population(tmp_path):
    cfg = config()
    assert capture(tmp_path, cfg)
    base = bank.directory(tmp_path)
    identity = store.read(base / "open_plan.json")["plan_id"]
    original = store.read(base / "journals" / f"{identity}.json")
    assert capture(tmp_path, replace(cfg, RESEARCH_RANK_CANARY_MIN_SCORE=66), token(2),
                   now=T0 + dt.timedelta(minutes=16)) is None
    with bank.capture_scope(cfg, root=tmp_path, run_context={"run_id": "OTHER_SYNTHETIC_RUN",
            "started_at": T0.isoformat()}, allow_test_capture=True, submit_tasks=False):
        assert bank.capture_gate("rank_canary", token(2), cfg, now=T0 + dt.timedelta(minutes=16)) is None
    assert store.read(base / "journals" / f"{identity}.json") == original


def test_missing_price_or_changed_exit_marks_trial_unknown(tmp_path, monkeypatch):
    cfg = config()
    identity = capture(tmp_path, cfg)
    assert fill(tmp_path, cfg, identity)
    assert bank.observe_market(token()["address"], None, root=tmp_path, cfg=cfg,
                               now=T0 + dt.timedelta(seconds=302)) == 1
    assert case(tmp_path, identity)["observation_gap_limit_exceeded"]
    record = case(tmp_path, identity)
    record["observation_gap_limit_exceeded"] = False
    store.write(bank.directory(tmp_path) / "active" / f"{identity}.json", record)
    monkeypatch.setattr(exit_policy, "CFG", replace(exit_policy.CFG, RUNNER_PRICE_TRAILING_DRAWDOWN_PCT=35))
    assert bank.observe_market(token()["address"], 100., root=tmp_path, cfg=cfg,
                               now=T0 + dt.timedelta(seconds=303)) == 1
    assert case(tmp_path, identity)["observation_gap_limit_exceeded"]
    assert "intent" not in case(tmp_path, identity)["cash"]["terminal"]


def test_cancelled_dispatcher_is_drained_and_not_replayed_after_restart(tmp_path, monkeypatch):
    cfg, actual_fill = config(), bank.fill_entry
    async def check():
        started = asyncio.Event()
        async def pending_prices(tokens):
            started.set()
            await asyncio.Event().wait()
        async def sol(): return 100.
        async def forbidden_quote(**kwargs): pytest.fail("cancelled entry must not quote")
        async def dispatch(identity, **kwargs):
            return await actual_fill(identity, **kwargs, now=T0 + dt.timedelta(seconds=1),
                prices_func=pending_prices, sol_price_func=sol, quote_func=forbidden_quote)
        monkeypatch.setattr(bank, "fill_entry", dispatch)
        with bank.capture_scope(cfg, root=tmp_path, run_context={"run_id": "SYNTHETIC_CANCEL_FIXTURE",
                "started_at": T0.isoformat()}, allow_test_capture=True):
            identity = bank.capture_gate("rank_canary", token(), cfg, now=T0)
        await started.wait()
        await bank.stop_background_tasks()
        assert not bank._TASKS
        assert case(tmp_path, identity)["entry_quote_dispatched"]
        assert case(tmp_path, identity)["cash"] is None
        assert not await actual_fill(identity, root=tmp_path, cfg=cfg, now=T0 + dt.timedelta(seconds=2),
                                     quote_func=forbidden_quote)
        async def missing(tokens): return {}
        await bank.tick(root=tmp_path, cfg=cfg, now=T0 + dt.timedelta(seconds=61),
                        prices_func=missing, quote_func=forbidden_quote, sol_price_func=sol)
        assert case(tmp_path, identity, "invalid")["invalid_reason"] == "entry_quote_unresolved_after_restart"
    asyncio.run(check())


def test_actual_gate_hook_registers_before_return_without_recursive_capture(tmp_path):
    from analytics.research_rank_canary import evaluate_research_rank_canary
    cfg, row = config(), token()
    with bank.capture_scope(cfg, root=tmp_path, run_context={"run_id": "SYNTHETIC_HOOK_FIXTURE",
            "started_at": T0.isoformat()}, allow_test_capture=True, submit_tasks=False):
        result = evaluate_research_rank_canary(row, {"rank_score": 62}, dry_run=True, live=False,
                                               cfg=cfg, record_audit=False)
    assert not result.allowed
    pointer = store.read(bank.directory(tmp_path) / "open_plan.json")
    events = store.read(bank.directory(tmp_path) / "journals" / f"{pointer['plan_id']}.json")["events"]
    assert len(events) == 1
    assert case(tmp_path, events[0]["case_id"])["challenger_buy"]


def test_missing_original_heartbeat_disables_a_previously_checked_selection(tmp_path):
    cfg = config()
    identity, now = complete(tmp_path, cfg)
    assert bank.evaluate_plan(tmp_path, cfg, identity, now=now)["accepted"]
    assert evaluator.load_selection(cfg, root=tmp_path, now=now)
    (bank.directory(tmp_path) / "heartbeats" / f"{identity}.json").unlink()
    assert evaluator.load_selection(cfg, root=tmp_path, now=now) is None


def champion_fixture(root):
    cfg = config(RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE=60,
        # Exercise the child admission threshold: the generic normal fallback
        # would otherwise accept the same low-rank features after it rejects.
        RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED=False, RESEARCH_RANK_CANARY_PRIORITY_ONLY=True,
        RESEARCH_RANK_CANARY_PRIORITY_MIN_PRICE5M=100,
        RESEARCH_RANK_CANARY_PRIORITY_MAX_PRICE5M=200,
        RESEARCH_RANK_CANARY_PRIORITY_MIN_TXNS_5M=300,
        RESEARCH_RANK_CANARY_PRIORITY_MIN_LIQUIDITY_USD=15000)
    identity, now = complete(root, cfg)
    assert bank.evaluate_plan(root, cfg, identity, now=now)["accepted"]
    base = bank.directory(root)
    manifest = store.read(evaluator.selection_path(root, "rank_canary"))
    (base / "open_plan.json").unlink()  # Synthetic transition, no runtime restart.
    return cfg, manifest


def successor_fixture(root, cfg, *, normal_output=30000000, priority_output=200000000):
    start = T0 + dt.timedelta(days=2)
    base = bank.directory(root)
    # Neighbor 2 resets only the paper-normal threshold, retaining the relaxed
    # parent threshold. It can buy priority opportunities while skipping normal ones.
    store.write(base / "proposal_cursor.json", {"index": 0, "component_indices": {"rank_canary": 2}})
    for i in range(50):
        decision = start + dt.timedelta(seconds=900 * i)
        row = token(i + 101, rank_score=62 if i < 40 else 72,
                    price_pct_5m=150 if 20 <= i < 40 else 70)
        identity = capture(root, cfg, row, now=decision, start=start)
        assert identity and fill(root, cfg, identity, now=decision + dt.timedelta(seconds=1))
        record = case(root, identity)
        record["observation_count"] = 1560  # Synthetic complete cadence only.
        closed = start + dt.timedelta(hours=26, seconds=i)
        quantity = record["cash"]["prefix"]["entry_qty"]
        output = normal_output if i < 20 else priority_output if i < 40 else 200000000
        evidence = synthetic_cash_decision(record, closed, output)
        record["cash"]["terminal"]["intent"] = make_intent(record["cash"]["terminal"]["subject"],
            quantity=quantity, reason="synthetic_three_arm_exit", now=closed, cash_valuation=evidence)
        store.write(base / "active" / f"{identity}.json", record)
        assert bank.observe_quote(row["address"], quote(quantity=quantity, output=output, source=row["address"], now=closed), 100.,
            root=root, cfg=cfg, now=closed, quote_started_at=closed, fx_observation=fx(closed)) == 1
    plan_id = store.read(base / "open_plan.json")["plan_id"]
    store.write(base / "heartbeats" / f"{plan_id}.json", {"times": [(start + dt.timedelta(minutes=i)).isoformat() for i in range(1441)]})
    return plan_id, start + dt.timedelta(hours=27)


def test_incumbent_only_buy_still_gets_cash_when_both_other_arms_skip(tmp_path):
    cfg, manifest = champion_fixture(tmp_path)
    base = bank.directory(tmp_path)
    store.write(base / "proposal_cursor.json", {"index": 0, "component_indices": {"rank_canary": 2}})
    start = T0 + dt.timedelta(days=2)
    identity = capture(tmp_path, cfg, token(101), now=start, start=start)
    record = case(tmp_path, identity)
    assert record["incumbent_buy"] and not record["baseline_buy"] and not record["challenger_buy"]
    assert not record["outcomes_complete"]
    assert fill(tmp_path, cfg, identity, now=start + dt.timedelta(seconds=1))
    plan = store.read(base / "plans" / f"{record['plan_id']}.json")
    assert plan["incumbent"]["manifest"] == manifest


def test_fresh_successor_beats_both_configured_and_incumbent_and_is_consumed(tmp_path):
    cfg, original = champion_fixture(tmp_path)
    identity, now = successor_fixture(tmp_path, cfg)
    result = bank.evaluate_plan(tmp_path, cfg, identity, now=now)
    assert result["accepted"] and result["action"] == "successor"
    base = bank.directory(tmp_path)
    active = store.read(evaluator.selection_path(tmp_path, "rank_canary"))
    assert active["parameters"] == {"RESEARCH_RANK_CANARY_MIN_SCORE": 60}
    assert active["revision"] != original["revision"]
    assert store.read(base / "history" / f"{original['revision']}.json") == original
    selected = evaluator.load_selection(cfg, root=tmp_path, now=now)
    assert selected and selected["parameters"] == active["parameters"]
    report = store.read(base / "evaluations" / result["evaluation"])["evaluation"]
    assert report["changed_challenger_vs_incumbent"] == 20
    assert report["paired_lower_mean_sol"] > 0 and report["challenger_vs_incumbent_lower_mean_sol"] > 0
    assert report["effective_transition_changes"] == 1


def test_positive_against_configured_but_worse_than_incumbent_cannot_replace(tmp_path):
    cfg, original = champion_fixture(tmp_path)
    identity, now = successor_fixture(tmp_path, cfg, normal_output=200000000, priority_output=150000000)
    result = bank.evaluate_plan(tmp_path, cfg, identity, now=now)
    assert not result["accepted"]
    base = bank.directory(tmp_path)
    assert store.read(evaluator.selection_path(tmp_path, "rank_canary")) == original
    report = store.read(base / "evaluations" / result["evaluation"])["evaluation"]
    assert report["paired_lower_mean_sol"] > 0
    assert report["challenger_vs_incumbent_lower_mean_sol"] < 0


def test_rank_incumbent_does_not_monopolize_sniper_collection_or_selection(tmp_path):
    cfg = config(SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M=100,
        SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M=150, SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M=500,
        SNIPER_RESEARCH_MOMENTUM_MIN_LIQUIDITY_USD=15000, SNIPER_RESEARCH_MOMENTUM_MAX_MCAP_USD=70000)
    rank_plan, now = complete(tmp_path, cfg)
    assert bank.evaluate_plan(tmp_path, cfg, rank_plan, now=now)["accepted"]
    base = bank.directory(tmp_path)
    rank_path = evaluator.selection_path(tmp_path, "rank_canary")
    rank = store.read(rank_path)
    (base / "open_plan.json").unlink()
    store.write(base / "proposal_cursor.json", {"index": 1, "component_indices": {"rank_canary": 1}})
    sniper_plan, now = complete(tmp_path, cfg, start=T0 + dt.timedelta(days=2), gate="sniper_subprofile",
        features_func=lambda i: {"price_pct_5m": 200 if i < 30 else 120,
                                "trend": "up", "helius_top10_share_pct": 20})
    registered = store.read(base / "plans" / f"{sniper_plan}.json")
    assert registered["incumbent"]["manifest"] is None
    assert registered["parameters"] == {"SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M": 250}
    assert bank.evaluate_plan(tmp_path, cfg, sniper_plan, now=now)["accepted"]
    assert store.read(rank_path) == rank
    selections = evaluator.load_selections(cfg, root=tmp_path, now=now)
    assert set(selections) == {"rank_canary", "sniper_subprofile"}
    assert len(list((base / "plans").glob("*.json"))) == 2


def test_absent_preferred_component_rotates_without_using_returns(tmp_path):
    cfg = config(LATE_MOMENTUM_WATCH_BUY_ENABLED=False, LATE_MOMENTUM_WATCH_PAPER_CANARY_ENABLED=False)
    base = bank.directory(tmp_path)
    store.write(base / "proposal_cursor.json", {"index": 1})  # Waiting for sniper.
    assert capture(tmp_path, cfg) is None
    assert capture(tmp_path, cfg, now=T0 + dt.timedelta(seconds=299)) is None
    assert capture(tmp_path, cfg, now=T0 + dt.timedelta(seconds=300)) is None  # Next moonshot is also absent.
    identity = capture(tmp_path, cfg, now=T0 + dt.timedelta(seconds=600))
    assert identity
    plan = store.read(base / "plans" / f"{case(tmp_path, identity)['plan_id']}.json")
    assert plan["proposal_schedule"] == {"version": bank.SCHEDULE, "index": 3, "component_index": 0}
    assert len(list((base / "plans").glob("*.json"))) == 1
    assert not case(tmp_path, identity).get("cash")  # No outcome or quote caused rotation.


@pytest.mark.parametrize("state", [
    {"index": 0, "component_indices": {"rank_canary": True}},
    {"index": 0, "component_indices": {"unknown": 0}},
    {"index": 0, "component_indices": {"moonshot": -1}},
    {"index": 0, "waiting_since_at": "2026-10-01T00:00:00"},
    {"index": 0, "version": "unknown"},
])
def test_invalid_component_schedule_is_preserved(tmp_path, state):
    path = bank.directory(tmp_path) / "proposal_cursor.json"
    store.write(path, state)
    assert capture(tmp_path, config()) is None
    assert store.read(path) == state


def test_schedule_cannot_advance_twice_after_pointer_cleanup_interruption(tmp_path):
    cfg = config()
    identity, now = complete(tmp_path, cfg)
    base = bank.directory(tmp_path)
    # Simulate a crash after atomic cursor advance, before completed/pointer cleanup.
    store.write(base / "proposal_cursor.json", {"version": bank.SCHEDULE, "index": 1,
        "component_indices": {"rank_canary": 1}, "waiting_since_at": now.isoformat()})
    async def no_prices(tokens):
        assert not tokens
        return {}
    result = asyncio.run(bank.tick(root=tmp_path, cfg=cfg, now=now, prices_func=no_prices))
    assert result["status"] == "observed"
    state = store.read(base / "proposal_cursor.json")
    assert state["index"] == 1 and state["component_indices"]["rank_canary"] == 1
    assert not (base / "open_plan.json").exists()
    assert store.read(base / "completed" / f"{identity}.json")


def test_green_route_registers_bounded_late_opportunity_before_baseline_dispatch(tmp_path, monkeypatch):
    from analytics import green_sniper_gate, late_momentum_watch
    cfg = config(LATE_MOMENTUM_WATCH_MIN_PRICE5M=300, LATE_MOMENTUM_WATCH_BUY_ENABLED=True,
                 LATE_MOMENTUM_WATCH_PAPER_CANARY_ENABLED=True)
    monkeypatch.setattr(green_sniper_gate, "CFG", cfg)
    monkeypatch.setattr(late_momentum_watch, "CFG", cfg)
    base = bank.directory(tmp_path)
    store.write(base / "proposal_cursor.json", {"index": 2, "component_indices": {"late_momentum": 1}})
    row = token(price_pct_5m=280, rank_score=75, market_cap_usd=20000, age_minutes=2,
                price_impact_pct=2, price_usd=1)
    # Use the test clock explicitly; production callbacks use current UTC.
    original = bank.capture_gate
    monkeypatch.setattr(bank, "capture_gate", lambda gate, row, cfg, *, now=None: original(gate, row, cfg, now=T0))
    with bank.capture_scope(cfg, root=tmp_path, run_context={"run_id": "SYNTHETIC_ROUTING_ENVELOPE",
            "started_at": T0.isoformat()}, allow_test_capture=True, submit_tasks=False):
        actual = green_sniper_gate.evaluate_green_sniper(dict(row), dry_run=True, live=False)
    assert actual.gate_profile != "late_momentum_watch"
    pointer = store.read(base / "open_plan.json")
    events = store.read(base / "journals" / f"{pointer['plan_id']}.json")["events"]
    assert len(events) == 1
    registered = case(tmp_path, events[0]["case_id"])
    assert not registered["baseline_buy"] and registered["challenger_buy"]
    assert not registered["cash"] and not registered["outcomes_complete"]


def test_late_router_duplicate_callback_does_not_reserve_two_cases_or_quotes(tmp_path, monkeypatch):
    from analytics import green_sniper_gate, late_momentum_watch
    cfg = config(LATE_MOMENTUM_WATCH_MIN_PRICE5M=300, LATE_MOMENTUM_WATCH_BUY_ENABLED=True,
                 LATE_MOMENTUM_WATCH_PAPER_CANARY_ENABLED=True)
    monkeypatch.setattr(green_sniper_gate, "CFG", cfg)
    monkeypatch.setattr(late_momentum_watch, "CFG", cfg)
    base = bank.directory(tmp_path)
    store.write(base / "proposal_cursor.json", {"index": 2})
    original = bank.capture_gate
    monkeypatch.setattr(bank, "capture_gate", lambda gate, row, cfg, *, now=None: original(gate, row, cfg, now=T0))
    row = token(price_pct_5m=350, rank_score=75, market_cap_usd=20000, age_minutes=2, price_impact_pct=2)
    with bank.capture_scope(cfg, root=tmp_path, run_context={"run_id": "SYNTHETIC_ROUTING_DUPLICATE",
            "started_at": T0.isoformat()}, allow_test_capture=True, submit_tasks=False):
        actual = green_sniper_gate.evaluate_green_sniper(dict(row), dry_run=True, live=False)
    assert actual.gate_profile == "late_momentum_watch" and actual.action == "buy"
    pointer = store.read(base / "open_plan.json")
    events = store.read(base / "journals" / f"{pointer['plan_id']}.json")["events"]
    assert len(events) == 1 and len(list((base / "active").glob("*.json"))) == 1
    assert store.read(tmp_path / "data/research/paired_forward_budget.json")["request_id"] == events[0]["case_id"]


def test_changed_incumbent_during_enrollment_prevents_stale_replacement(tmp_path):
    cfg, original = champion_fixture(tmp_path)
    identity, now = successor_fixture(tmp_path, cfg)
    changed = {**original, "expires_at": (store.time(original["expires_at"]) - dt.timedelta(hours=1)).isoformat()}
    base = bank.directory(tmp_path)
    store.write(evaluator.selection_path(tmp_path, "rank_canary"), changed)
    result = bank.evaluate_plan(tmp_path, cfg, identity, now=now)
    assert result["status"] == "retained_changed_incumbent"
    assert store.read(evaluator.selection_path(tmp_path, "rank_canary")) == changed


def test_auto_apply_off_prevents_even_evidence_backed_rollback(tmp_path):
    cfg, original = champion_fixture(tmp_path)
    identity, now = complete(tmp_path, cfg, start=T0 + dt.timedelta(days=2), losing=True)
    disabled = replace(cfg, PAPER_ENTRY_GATE_AUTO_APPLY=False)
    result = bank.evaluate_plan(tmp_path, disabled, identity, now=now)
    assert result["status"] == "evaluated_auto_apply_disabled"
    base = bank.directory(tmp_path)
    assert store.read(evaluator.selection_path(tmp_path, "rank_canary")) == original
    assert store.read(base / "evaluations" / result["evaluation"])["evaluation"]["rollback_to_configured"]
    assert not list((base / "rollbacks").glob("*.json"))


@pytest.mark.parametrize("damage", ["history", "original_case"])
def test_successor_loader_rechecks_immediate_incumbent_original_proof(tmp_path, damage):
    cfg, original = champion_fixture(tmp_path)
    identity, now = successor_fixture(tmp_path, cfg)
    assert bank.evaluate_plan(tmp_path, cfg, identity, now=now)["accepted"]
    assert evaluator.load_selection(cfg, root=tmp_path, now=now)
    base = bank.directory(tmp_path)
    if damage == "history":
        (base / "history" / f"{original['revision']}.json").unlink()
    else:
        prior_bundle = store.read(base / "evaluations" / original["evidence_name"])
        path = base / "closed" / f"{prior_bundle['plan']['case_ids'][0]}.json"
        record = store.read(path)
        record["cash"]["terminal"]["subject"]["estimated_fees_sol"] += .01
        store.write(path, record)
    assert evaluator.load_selection(cfg, root=tmp_path, now=now + dt.timedelta(seconds=6)) is None


def test_incumbent_neighbors_count_resets_and_stay_inside_checked_envelope():
    cfg = config()
    before = {"RESEARCH_RANK_CANARY_MIN_SCORE": 60, "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_RANK_SCORE": 60}
    neighbors = bank.incumbent_neighbors(cfg, before)
    assert neighbors[0] == ("rank_canary", before)
    assert ("rank_canary", {"RESEARCH_RANK_CANARY_MIN_SCORE": 60}) in neighbors
    for gate, profile in neighbors:
        assert policy.validate_transition(cfg, before, profile, gate=gate) == profile


def test_same_profile_is_revalidated_with_fresh_cohort_and_preserved_history(tmp_path):
    cfg, original = champion_fixture(tmp_path)
    identity, now = complete(tmp_path, cfg, start=T0 + dt.timedelta(days=2))
    result = bank.evaluate_plan(tmp_path, cfg, identity, now=now)
    assert result["accepted"] and result["action"] == "revalidation"
    active = store.read(evaluator.selection_path(tmp_path, "rank_canary"))
    assert active["parameters"] == original["parameters"] and active["revision"] != original["revision"]
    assert evaluator.load_selection(cfg, root=tmp_path, now=now)


def test_valid_successor_is_preferred_over_rollback_of_a_losing_incumbent(tmp_path):
    cfg, _ = champion_fixture(tmp_path)
    identity, now = successor_fixture(tmp_path, cfg, normal_output=1000000, priority_output=101000000)
    result = bank.evaluate_plan(tmp_path, cfg, identity, now=now)
    assert result["accepted"] and result["action"] == "successor"
    base = bank.directory(tmp_path)
    report = store.read(base / "evaluations" / result["evaluation"])["evaluation"]
    assert report["rollback_to_configured"]
    assert not list((base / "rollbacks").glob("*.json"))


def test_replaying_an_already_selected_cohort_cannot_refresh_expiry(tmp_path):
    cfg = config()
    identity, now = complete(tmp_path, cfg)
    assert bank.evaluate_plan(tmp_path, cfg, identity, now=now)["accepted"]
    base = bank.directory(tmp_path)
    original = store.read(evaluator.selection_path(tmp_path, "rank_canary"))
    replay = bank.evaluate_plan(tmp_path, cfg, identity, now=now + dt.timedelta(hours=1))
    assert replay["status"] == "retained_changed_incumbent"
    assert store.read(evaluator.selection_path(tmp_path, "rank_canary")) == original
