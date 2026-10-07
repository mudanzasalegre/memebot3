"""Synthetic clocks/prices/quotes only; no real run or profit is certified."""
import asyncio
import datetime as dt
import json
from dataclasses import replace
from types import SimpleNamespace

import base58
import pytest

from analytics import exit_policy, api_budget
from config.config import CFG
from research_loop import entry_gate_forward as bank, entry_gate_policy as evaluator, forward_budget as store
from runtime import paper_entry_policy as policy

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)


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
    yield
    api_budget.reset_provider_circuits()


def capture(root, cfg, row=None, now=T0, start=T0):
    with bank.capture_scope(cfg, root=root, run_context={"run_id": "SYNTHETIC_COLLECTOR_FIXTURE",
            "started_at": start.isoformat(), "test_event": False}, allow_test_capture=True, submit_tasks=False):
        return bank.capture_gate("rank_canary", row or token(), cfg, now=now)


def quote(quantity=100000000, output=1000, **changes):
    return SimpleNamespace(**{"ok": True, "in_amount": quantity, "out_amount": output,
                              "price_impact_bps": 20., "other": {"routePlan_len": 1}, **changes})


def fill(root, cfg, case_id, now=T0 + dt.timedelta(seconds=1), changes=None):
    async def prices(tokens): return {mint: 1. for mint in tokens}
    async def sol(): return 100.
    async def quoted(**kwargs): return quote(**(changes or {}))
    return asyncio.run(bank.fill_entry(case_id, root=root, cfg=cfg, now=now,
        prices_func=prices, quote_func=quoted, sol_price_func=sol))


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
    record["cash"]["terminal"]["intent"] = {"quantity": q, "reason": "test_stop", "requested_at": (T0 + dt.timedelta(minutes=1)).isoformat()}
    store.write(bank.directory(tmp_path) / "active" / f"{identity}.json", record)
    mint = record["token"]
    assert bank.observe_quote(mint, quote(quantity=q - 1, output=200000000), 100., root=tmp_path, cfg=cfg, now=T0 + dt.timedelta(minutes=2)) == 0
    assert bank.observe_quote(mint, quote(quantity=q, output=200000000), 100., root=tmp_path, cfg=cfg, now=T0 + dt.timedelta(minutes=2)) == 1
    assert not (bank.directory(tmp_path) / "active" / f"{identity}.json").exists()
    assert case(tmp_path, identity, "closed")["cash"]["terminal"]["net_pnl_sol"] == pytest.approx(.09995)


def test_corrupt_auxiliary_hooks_do_not_abort_primary_work(tmp_path):
    base = bank.directory(tmp_path)
    store.write(base / "active" / "malformed.json", {"token": token()["address"], "cash": "broken"})
    assert bank.observe_quote(token()["address"], quote(), 100., root=tmp_path, cfg=config()) == 0
    assert bank.observe_market(token()["address"], 1., root=tmp_path, cfg=config()) == 0


def complete(root, cfg, *, start=T0, losing=False):
    for i in range(50):
        decision = start + dt.timedelta(seconds=900 * i)
        identity = capture(root, cfg, token(i + 1, rank_score=62 if i < 30 else 72), now=decision, start=start)
        assert identity and fill(root, cfg, identity, now=decision + dt.timedelta(seconds=1))
        record = case(root, identity)
        # Synthetic complete cadence, not a production observation claim.
        record["observation_count"] = 1560
        quantity = record["cash"]["prefix"]["entry_qty"]
        closed = start + dt.timedelta(hours=26, seconds=i)
        record["cash"]["terminal"]["intent"] = {"quantity": quantity, "reason": "synthetic_common_exit", "requested_at": closed.isoformat()}
        store.write(bank.directory(root) / "active" / f"{identity}.json", record)
        output = 30000000 if losing and i < 30 else 200000000
        assert bank.observe_quote(record["token"], quote(quantity=quantity, output=output), 100., root=root, cfg=cfg, now=closed) == 1
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
    assert not (base / "active_policy.json").exists()
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
    async def fetched(tokens): return {key: ("OK", 2.) for key in tokens}
    monkeypatch.setattr(prices, "_fetch_batch_with_status", fetched)
    assert asyncio.run(prices.get_many_usd_prices([mint], force_refresh=True))[mint] == 2.


def test_fresh_price_mode_does_not_fabricate_a_fixed_stable_price(monkeypatch):
    from fetcher import jupiter_price as prices
    mint = next(iter(prices._KNOWN_STABLES))
    called = []
    async def fetched(tokens):
        called.append(tokens)
        return {key: ("OK", .91) for key in tokens}
    monkeypatch.setattr(prices, "_fetch_batch_with_status", fetched)
    assert asyncio.run(prices.get_many_usd_prices([mint], force_refresh=True))[mint] == .91
    assert called == [[mint]]


@pytest.mark.parametrize("fault", [{"in_amount": True}, {"other": {"routePlan_len": True}}])
def test_raw_exit_quote_never_accepts_boolean_amount_or_route_count(tmp_path, fault):
    cfg = config()
    identity = capture(tmp_path, cfg)
    assert fill(tmp_path, cfg, identity)
    record = case(tmp_path, identity)
    record["cash"]["terminal"]["intent"] = {"quantity": 1, "reason": "synthetic_partial",
        "requested_at": (T0 + dt.timedelta(seconds=60)).isoformat()}
    store.write(bank.directory(tmp_path) / "active" / f"{identity}.json", record)
    bad = quote(quantity=1, output=1000000, **fault)
    assert bank.observe_quote(record["token"], bad, 100., root=tmp_path, cfg=cfg,
                              now=T0 + dt.timedelta(seconds=61)) == 0
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
