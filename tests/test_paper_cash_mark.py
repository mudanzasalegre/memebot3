"""Synthetic exact-size cash observations; no provider/order/operator data."""
import asyncio
import ast
import copy
import datetime as dt
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock

import pytest

from execution import paper_cash_mark as cash
from execution.quote_receipt import capture_summary
from quote_fixtures import SOL, TOKEN, v1_quote, v2_quote
from utils.sol_price import SolUsdObservation

T0 = dt.datetime(2026, 10, 8, 17, tzinfo=dt.timezone.utc)
OWNER = "buy:" + "a" * 32


def entry(**changes):
    opened = T0 - dt.timedelta(seconds=20)
    route = capture_summary(v1_quote(SOL, TOKEN, 100000000, 1000, now=opened),
        input_mint=SOL, output_mint=TOKEN, amount=100000000,
        slippage=v1_quote(SOL, TOKEN, 100000000, 1000).other["slippageBps"], limit=8., now=opened)
    return {"dry_run": True, "closed": False, "token_address": TOKEN,
        "entry_intent_id": "a" * 32, "buy_signature": "SIM-" + "a" * 32,
        "opened_at": opened.isoformat(), "run_id": "synthetic-cash-case",
        "entry_qty": 1000, "qty_lamports": 1000, "realized_qty": 0,
        "entry_notional_usd": 10., "buy_price_usd": .02, "amount_sol": .1,
        "realized_proceeds_usd": 0., "estimated_fees_usd": .0025,
        "entry_route_quote": route, "quantity_basis": "quoted_raw_spl_units",
        "execution_cost_model": {"version": "estimated-v1", "observed_execution": False,
            "slippage_bps": 0., "fee_sol_per_fill": .000025}, **changes}


def fx(**changes):
    return SolUsdObservation(**{"status": "OK", "price_usd": 100.,
        "received_at": T0.timestamp(), "market_updated_at": T0.timestamp(), **changes})


def quote(quantity=1000, output=110000000, *, now=T0):
    return v1_quote(TOKEN, SOL, quantity, output, now=now)


def capture(row, quoted=None, rate=None, *, now=T0, slippage=None):
    quoted = quote() if quoted is None else quoted
    return cash.capture(row, quoted, fx() if rate is None else rate, token=TOKEN, owner=OWNER,
                        now=now, slippage_bps=quoted.other["slippageBps"] if slippage is None else slippage)


def position(row):
    return SimpleNamespace(source_position_key=OWNER, buy_tx_sig=row["buy_signature"],
        token_mint=TOKEN, address=TOKEN, dry_run=True, closed=False,
        qty=row["qty_lamports"], entry_qty=row["entry_qty"], realized_qty=row["realized_qty"],
        buy_amount_sol=row["amount_sol"], buy_price_usd=row["buy_price_usd"],
        entry_notional_usd=row["entry_notional_usd"], realized_proceeds_usd=row["realized_proceeds_usd"])


@pytest.fixture(autouse=True)
def no_external_observations(monkeypatch):
    from trader import papertrading as paper
    from utils import sol_price
    failure = AssertionError("A synthetic cash-mark test must not call a provider")
    monkeypatch.setattr(paper.jupiter_router, "get_routing_quote", AsyncMock(side_effect=failure))
    monkeypatch.setattr(sol_price, "get_sol_usd_observation", AsyncMock(side_effect=failure))
    monkeypatch.setattr(paper, "get_sol_usd", AsyncMock(side_effect=failure))
    monkeypatch.setattr(paper.price_service, "get_price_usd", AsyncMock(side_effect=failure))


def test_raw_spot_is_not_the_quoted_cash_return_or_a_physical_price():
    row = entry()
    mark = capture(row)
    values = mark.to_dict()["values"]
    assert values["quoted_proceeds_usd"] == 11.
    assert values["gross_remaining_return_pct"] == pytest.approx(10.)
    assert values["policy_reference_price_usd"] == pytest.approx(.022)
    assert values["estimated_total_liquidation_net_pnl_usd"] == pytest.approx(.995)
    assert mark.to_dict()["physical_token_price"] is False
    assert (.04 / row["buy_price_usd"] - 1) * 100 == 100.  # Different spot proxy.
    assert cash.checked_price(mark, row, token=TOKEN, owner=OWNER, now=T0) == pytest.approx(.022)
    detached = mark.to_dict()
    detached["values"]["policy_reference_price_usd"] = 999.
    assert mark.to_dict()["values"]["policy_reference_price_usd"] == pytest.approx(.022)
    with pytest.raises(FrozenInstanceError):
        mark.receipt_json = "{}"


def test_partial_quantity_cash_and_remaining_return_are_distinct_from_whole_trade_pnl():
    row = entry(qty_lamports=500, realized_qty=500, realized_proceeds_usd=7., estimated_fees_usd=.005)
    mark = capture(row, quote(500, 60000000))
    values = mark.to_dict()["values"]
    assert values["remaining_cost_usd"] == 5.
    assert values["gross_remaining_return_pct"] == pytest.approx(20.)
    assert values["policy_reference_price_usd"] == pytest.approx(.024)
    assert values["estimated_total_liquidation_net_pnl_usd"] == pytest.approx(2.9925)
    assert cash.checked_price(mark, entry(), token=TOKEN, owner=OWNER, now=T0) is None


@pytest.mark.parametrize("family", ["metis", "jupiterz", "dflow", "okx"])
def test_all_router_quote_cash_is_checked_in_exact_raw_quantity(family):
    q = v2_quote(TOKEN, SOL, 1000, 110000000, now=T0, family=family)
    mark = capture(entry(), q)
    assert cash.checked_price(mark, entry(), token=TOKEN, owner=OWNER, now=T0) == pytest.approx(.022)


@pytest.mark.parametrize("field,value", [("entry_notional_usd", True), ("entry_notional_usd", "10"),
    ("entry_notional_usd", None), ("entry_notional_usd", float("inf")), ("buy_price_usd", 0),
    ("entry_qty", 999), ("qty_lamports", True), ("realized_qty", 1), ("amount_sol", None),
    ("dry_run", False), ("closed", True), ("buy_signature", "wrong"), ("entry_intent_id", "b" * 32),
    ("quantity_basis", "synthetic_paper_units"), ("entry_route_quote", {}),
    ("estimated_fees_usd", float("nan")), ("realized_proceeds_usd", -1)])
def test_unknown_or_conflicting_original_basis_is_not_valued(field, value):
    with pytest.raises((ValueError, TypeError, OverflowError)):
        capture(entry(**{field: value}))


@pytest.mark.parametrize("change", [{"received_at": T0.timestamp() - 61},
    {"market_updated_at": T0.timestamp() - 121}, {"price_usd": True}, {"price_usd": float("nan")},
    {"assumed": True}, {"source": "configured_override"}, {"status": "ERR"}])
def test_original_fx_unknown_is_not_current_cash(change):
    with pytest.raises(ValueError):
        capture(entry(), rate=fx(**change))


@pytest.mark.parametrize("seconds,valid", [(0, True), (10, True), (11, False), (-1, False)])
def test_original_quote_and_mark_age_are_not_renewed(seconds, valid):
    row = entry()
    mark = capture(row)
    assert (cash.checked_price(mark, row, token=TOKEN, owner=OWNER,
        now=T0 + dt.timedelta(seconds=seconds)) is not None) is valid
    assert mark.to_dict()["route_quote"]["observation_receipt"]["other"]["received_at_utc"] == T0.isoformat()


@pytest.mark.parametrize("field,value", [("qty", 999), ("entry_qty", 999), ("realized_qty", 1),
    ("entry_notional_usd", 11.), ("buy_price_usd", .03), ("buy_amount_sol", .2),
    ("realized_proceeds_usd", 2.), ("source_position_key", "buy:" + "b" * 32),
    ("buy_tx_sig", "another"), ("token_mint", SOL), ("closed", True), ("dry_run", False)])
def test_sql_financial_or_lineage_conflict_cannot_consume_the_mark(field, value):
    row = entry()
    pos = position(row)
    assert cash.matches_sql(row, pos, token=TOKEN)
    setattr(pos, field, value)
    assert not cash.matches_sql(row, pos, token=TOKEN)


@pytest.mark.asyncio
async def test_actual_paper_cash_probe_is_owned_and_rechecks_sql_after_await(monkeypatch, tmp_path):
    from trader import papertrading as paper
    row, calls = entry(), []
    pos = position(row)
    monkeypatch.setattr(paper, "_PORTFOLIO", {TOKEN: row})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "data" / "paper_portfolio.json")
    monkeypatch.setattr(paper, "utc_now", lambda: T0)
    async def quoted(**kwargs):
        assert TOKEN in paper._SELL_LOCKS and paper._SELL_LOCKS[TOKEN][0].locked()
        calls.append(kwargs)
        return quote()
    async def price():
        pos.qty = 999
        return fx()
    assert await paper.get_exit_cash_mark(TOKEN, expected_position=pos, quote_func=quoted, fx_func=price) is None
    assert calls == [{"input_mint": TOKEN, "output_mint": SOL, "amount_lamports": 1000,
                      "slippage_bps": quote().other["slippageBps"]}]
    assert TOKEN not in paper._SELL_LOCKS and not paper._DATA_PATH.exists()
    pos.qty = 1000
    async def good_fx(): return fx()
    mark = await paper.get_exit_cash_mark(TOKEN, expected_position=pos, quote_func=quoted, fx_func=good_fx)
    assert paper.checked_cash_price(TOKEN, mark, expected_position=pos) == pytest.approx(.022)
    assert row["qty_lamports"] == 1000 and not paper._DATA_PATH.exists()


@pytest.mark.asyncio
async def test_cancellation_drains_cash_probe_without_fill_or_orphan_lock(monkeypatch, tmp_path):
    from trader import papertrading as paper
    row = entry()
    monkeypatch.setattr(paper, "_PORTFOLIO", {TOKEN: row})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "paper_portfolio.json")
    entered = asyncio.Event()
    async def quoted(**kwargs):
        entered.set()
        await asyncio.Future()
    task = asyncio.create_task(paper.get_exit_cash_mark(TOKEN, quote_func=quoted))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert TOKEN not in paper._SELL_LOCKS and row["qty_lamports"] == 1000
    assert not paper._DATA_PATH.exists()


def test_request_slippage_is_not_taken_from_the_response():
    with pytest.raises(ValueError):
        capture(entry(), slippage=quote().other["slippageBps"] + 1)


@pytest.mark.parametrize("peak", [200, 500, 1000, 5000, 10000, 100000])
def test_exact_cash_marks_and_peaks_have_no_upward_profit_ceiling(monkeypatch, tmp_path, peak):
    from trader import papertrading as paper
    row = entry(highest_pnl_pct=999999., max_pnl_pct_seen=999999., peak_price=999999.,
                max_adverse_pnl_pct=-99., peak_pnl_pct=999999.)
    pos = position(row)
    for name, value in {"highest_pnl_pct": 999999., "max_pnl_pct_seen": 999999.,
        "max_adverse_pnl_pct": -99., "peak_price": 999999., "peak_price_usd": 999999.,
        "exit_state": "old", "time_to_peak_sec": 1, "peak_after_partial_pct": None}.items():
        setattr(pos, name, value)
    monkeypatch.setattr(paper, "_PORTFOLIO", {TOKEN: row})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "paper_portfolio.json")
    monkeypatch.setattr(paper, "utc_now", lambda: T0)
    mark = capture(row, quote(output=100000000 * (1 + peak // 100)))
    assert paper.record_cash_observation(TOKEN, mark, expected_position=pos)
    assert row["legacy_market_peak_diagnostic"]["values"]["highest_pnl_pct"] == 999999.
    assert row["legacy_market_peak_diagnostic"]["role"] == "unverified_valuation_only"
    assert row["highest_pnl_pct"] == pytest.approx(peak)
    assert row["max_adverse_pnl_pct"] == 0.
    assert row["time_to_peak_sec"] == 20
    assert paper.sync_cash_peak_metrics(TOKEN, mark, pos)
    assert pos.highest_pnl_pct == pytest.approx(peak)
    assert not paper.sync_cash_peak_metrics(TOKEN, mark, pos)
    assert json.loads(paper._DATA_PATH.read_text())[TOKEN]["last_cash_mark"] == mark.to_dict()


def test_failed_cash_peak_write_preserves_original_reference_and_file(monkeypatch, tmp_path):
    from trader import papertrading as paper
    row = entry(highest_pnl_pct=999.)
    before = copy.deepcopy(row)
    monkeypatch.setattr(paper, "_PORTFOLIO", {TOKEN: row})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "paper_portfolio.json")
    monkeypatch.setattr(paper, "utc_now", lambda: T0)
    paper._save(strict=True)
    contents = paper._DATA_PATH.read_bytes()
    def failed(*args): raise OSError("Synthetic disk failure")
    monkeypatch.setattr(paper, "write_json_atomic", failed)
    with pytest.raises(paper.PaperPortfolioError): paper.record_cash_observation(TOKEN, capture(row))
    assert paper._PORTFOLIO[TOKEN] is row and row == before
    assert paper._DATA_PATH.read_bytes() == contents


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_actual_standalone_uses_cash_or_unknown_without_a_spot_fallback(monkeypatch, tmp_path, unknown):
    from trader import papertrading as paper
    row = entry()
    monkeypatch.setattr(paper, "_PORTFOLIO", {TOKEN: row})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "paper_portfolio.json")
    monkeypatch.setattr(paper, "utc_now", lambda: T0)
    monkeypatch.setattr(paper.jupiter_router, "get_routing_quote", AsyncMock(return_value=quote()))
    from utils import sol_price
    monkeypatch.setattr(sol_price, "get_sol_usd_observation", AsyncMock(return_value=fx(status="ERR") if unknown else fx()))
    seen = []
    monkeypatch.setattr(paper.exit_policy, "should_take_partial", lambda obj, pnl: False)
    monkeypatch.setattr(paper.exit_policy, "should_exit", lambda obj, price, now, **kw: seen.append((price, kw["pnl_pct"])) or None)
    assert not await paper.check_exit_conditions(TOKEN)
    assert seen == ([(None, None)] if unknown else [(pytest.approx(.022), pytest.approx(10.))])
    paper.price_service.get_price_usd.assert_not_awaited()
    if unknown:
        assert "highest_pnl_pct" not in row and not paper._DATA_PATH.exists()


@pytest.mark.asyncio
async def test_partial_execution_is_cash_accounting_not_a_remaining_quantity_peak(monkeypatch, tmp_path):
    from trader import papertrading as paper
    row = entry()
    monkeypatch.setattr(paper, "_PORTFOLIO", {TOKEN: row})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "paper_portfolio.json")
    monkeypatch.setattr(paper, "utc_now", lambda: T0)
    monkeypatch.setattr(paper, "CFG", SimpleNamespace(PAPER_RUNNER_RESEARCH_ENABLED=False))
    monkeypatch.setattr(paper.runner_forward, "register_partial", lambda *a, **kw: None)
    monkeypatch.setattr(paper.runner_forward, "observe_quote", lambda *a, **kw: None)
    from research_loop import entry_gate_forward
    monkeypatch.setattr(entry_gate_forward, "observe_quote", lambda *a, **kw: None)
    mark = capture(row)
    assert paper.record_cash_observation(TOKEN, mark)
    monkeypatch.setattr(paper.jupiter_router, "get_routing_quote", AsyncMock(return_value=quote(250, 125000000)))
    monkeypatch.setattr(paper, "get_sol_usd", AsyncMock(return_value=100.))
    response = await paper.sell(TOKEN, 250, partial_ladder_plan={"pending_step_count": 1})
    assert response["ok"] is True and response["qty_left"] == 750
    assert row["realized_proceeds_usd"] == pytest.approx(12.5)
    assert row["highest_pnl_pct"] == pytest.approx(10.)  # Small fill's 400% is not a whole-position cash peak.
    assert paper.checked_cash_price(TOKEN, mark) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "unavailable", "expired_after_commit", "wrong_sql_after_commit",
                                  "disk_failure", "partial_expired_after_commit"])
async def test_actual_main_monitor_consumes_current_cash_not_batch_spot(monkeypatch, tmp_path, fault):
    from trader import papertrading as paper
    from utils import sol_price, market_observation
    row = entry()
    pos = position(row)
    for name, value in {"highest_pnl_pct": 9000., "max_pnl_pct_seen": 9000.,
        "max_adverse_pnl_pct": -90., "peak_price": 9000., "peak_price_usd": 9000.,
        "exit_state": "pre_partial", "time_to_peak_sec": None, "peak_after_partial_pct": None,
        "buy_liquidity_usd": None, "partial_taken": False, "entry_regime": "normal"}.items():
        setattr(pos, name, value)
    clock = [T0]
    monkeypatch.setattr(paper, "_PORTFOLIO", {TOKEN: row})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "paper_portfolio.json")
    monkeypatch.setattr(paper, "utc_now", lambda: clock[0])
    monkeypatch.setattr(paper.jupiter_router, "get_routing_quote", AsyncMock(return_value=quote()))
    monkeypatch.setattr(sol_price, "get_sol_usd_observation", AsyncMock(return_value=fx(status="ERR") if fault == "unavailable" else fx()))
    physical = []
    monkeypatch.setattr(paper, "record_market_observation", lambda address, price, **kw: physical.append(price))
    if fault == "disk_failure":
        def failed(*args): raise OSError("Synthetic disk failure")
        monkeypatch.setattr(paper, "write_json_atomic", failed)
    async def load(session): return [pos]
    async def ensure(position, session): pass
    async def prefetch(addresses): return {TOKEN: {"price_usd": .04, "price_source": "jupiter"}}
    seen, commits = [], []
    class Session:
        async def commit(self):
            commits.append(True)
            if fault in {"expired_after_commit", "partial_expired_after_commit"}: clock[0] += dt.timedelta(seconds=11)
            if fault == "wrong_sql_after_commit": pos.qty = 999
        async def rollback(self): pass
    async def exit_check(position, price, now, **kw):
        seen.append((price, kw["pnl_pct"]))
    fake_policy = SimpleNamespace(resolve_entry_regime=lambda obj: "normal",
        effective_exit_policy=lambda obj: SimpleNamespace(runner_exit_profile=None,
            tp_partial_enabled=fault == "partial_expired_after_commit", liq_crush_fraction=.1),
        should_take_partial=lambda *args: pytest.fail("A stale quote must not trigger a partial"))
    def no_peak(*args, **kwargs): pytest.fail("The PAPER monitor must not merge a physical SQL spot peak")
    ns = {"SessionLocal": object, "Position": object, "Optional": Optional, "Dict": dict, "dt": dt,
        "DRY_RUN": True, "FORCE_JUP_IN_MONITOR": False, "utc_now": lambda: clock[0],
        "_BUY_RECOVERY": SimpleNamespace(pending_addresses=set()), "_CLOSE_RECOVERY_PENDING": set(),
        "_load_open_positions": load, "_ensure_position_entry_notional": ensure,
        "_prefetch_batch_prices": prefetch, "_update_position_peak_metrics": no_peak,
        "fresh_market_value": lambda tick, field, **kw: (tick or {}).get(field),
        "liquidity_crushed": market_observation.liquidity_crushed,
        "exit_policy": fake_policy, "_should_exit": exit_check,
        "strategy_runtime": SimpleNamespace(record_monitor_coverage=lambda *a: None),
        "runner_turbo_monitor": SimpleNamespace(observe_position=lambda *a, **kw: None),
        "log": SimpleNamespace(**{name: lambda *a, **kw: None for name in ("info", "debug", "warning", "critical")}),
        "json": json}
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_check_positions")
    exec(compile(ast.Module(body=[function], type_ignores=[]), "run_bot.py", "exec"), ns)
    await ns["_check_positions"](Session())
    assert physical == [.04]  # Never stamp the private .022 policy reference as market price.
    assert seen == ([(pytest.approx(.022), pytest.approx(10.))] if fault == "none" else [(None, None)])
    if fault in {"unavailable", "disk_failure"}:
        assert pos.highest_pnl_pct == 9000. and not commits and not paper._DATA_PATH.exists()
    else:
        assert pos.highest_pnl_pct == pytest.approx(10.) and len(commits) == 1


@pytest.mark.parametrize("change", [{"entry_notional_usd": 11.}, {"entry_intent_id": "b" * 32},
    {"realized_qty": 0, "qty_lamports": 1000}, {"estimated_fees_usd": .001},
    {"closed_at": (T0 - dt.timedelta(seconds=1)).isoformat()}])
def test_historical_receipt_is_not_renewed_or_assigned_to_another_financial_generation(change):
    original = entry(qty_lamports=500, realized_qty=500, realized_proceeds_usd=7., estimated_fees_usd=.005)
    mark = capture(original, quote(500, 60000000))
    closed = dict(original, closed=True, qty_lamports=0, closed_at=(T0 + dt.timedelta(hours=1)).isoformat(),
                  estimated_fees_usd=.0075)
    historical = cash.public_historical_mark(mark, closed, token=TOKEN, owner=OWNER)
    assert historical == mark.to_dict() and cash.checked_price(mark, closed, token=TOKEN, owner=OWNER, now=T0) is None
    with pytest.raises(ValueError):
        cash.public_historical_mark(mark, dict(closed, **change), token=TOKEN, owner=OWNER)


@pytest.mark.asyncio
async def test_actual_costed_partial_and_close_archive_keep_original_cash_peak_proof(monkeypatch, tmp_path):
    from trader import papertrading as paper
    from runtime.paper_archive import PaperArchiveError, paper_snapshot
    from research_loop import entry_gate_forward
    row = entry(highest_pnl_pct=9999., peak_price=9999.)
    monkeypatch.setattr(paper, "_PORTFOLIO", {TOKEN: row})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "data" / "paper_portfolio.json")
    monkeypatch.setattr(paper, "utc_now", lambda: T0)
    monkeypatch.setattr(paper, "CFG", SimpleNamespace(PAPER_RUNNER_RESEARCH_ENABLED=False))
    for module in (paper.runner_forward, entry_gate_forward):
        monkeypatch.setattr(module, "observe_quote", lambda *a, **kw: None)
    monkeypatch.setattr(paper.runner_forward, "register_partial", lambda *a, **kw: None)
    original = capture(row, quote(output=600000000))  # +500% exact whole remaining quantity.
    assert paper.record_cash_observation(TOKEN, original)
    monkeypatch.setattr(paper.jupiter_router, "get_routing_quote", AsyncMock(side_effect=[
        quote(250, 125000000), quote(750, 375000000)]))
    monkeypatch.setattr(paper, "get_sol_usd", AsyncMock(return_value=100.))
    assert (await paper.sell(TOKEN, 250, partial_ladder_plan={"pending_step_count": 1}))["ok"]
    assert (await paper.sell(TOKEN, 750))["ok"]
    assert row["closed"] is True and row["qty_lamports"] == 0 and row["highest_pnl_pct"] == 500.
    archives = list((tmp_path / "data" / "paper_closed_trades").glob("*.json"))
    assert len(archives) == 1
    stored = json.loads(archives[0].read_text())
    trade = stored["trade"]
    assert trade["cash_peak_mark"] == trade["last_cash_mark"] == original.to_dict()
    assert trade["cash_peak_mark"]["basis"]["remaining_qty"] == 1000  # Do not relabel it as the later 750/0 quantity.
    assert trade["legacy_market_peak_diagnostic"]["values"]["highest_pnl_pct"] == 9999.
    assert trade["estimated_fees_usd"] == pytest.approx(.0075)
    for change in ({"highest_pnl_pct": 9000.}, {"cash_peak_observed_at": "unknown"}, {"time_to_peak_sec": 99},
                   {"cash_peak_mark": None}):
        with pytest.raises(PaperArchiveError): paper_snapshot(dict(row, **change), TOKEN)


def test_adverse_cash_mark_retains_original_proof_without_inventing_a_positive_peak(monkeypatch, tmp_path):
    from trader import papertrading as paper
    from runtime.paper_archive import paper_snapshot
    row = entry()
    monkeypatch.setattr(paper, "_PORTFOLIO", {TOKEN: row})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "paper_portfolio.json")
    monkeypatch.setattr(paper, "utc_now", lambda: T0)
    mark = capture(row, quote(output=80000000))
    assert paper.record_cash_observation(TOKEN, mark)
    assert row["highest_pnl_pct"] == 0. and "cash_peak_mark" not in row
    assert row["max_adverse_pnl_pct"] == pytest.approx(-20.)
    assert paper_snapshot(row, TOKEN)["cash_max_adverse_mark"] == mark.to_dict()
