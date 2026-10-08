"""Synthetic original conversion/actual PAPER paths; no provider or bot run."""
import asyncio
import copy
import datetime as dt
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from analytics.forward_evidence import _costed_close
from db.models import Position
from execution import paper_execution_fx as contract
from quote_fixtures import SOL, TOKEN, v1_quote
from runtime import buy_recovery, paper_archive, sell_recovery
from trader import papertrading as paper
from utils.sol_price import SolUsdObservation

T0 = dt.datetime(2026, 10, 8, 17, tzinfo=dt.timezone.utc)


def fx(stamp=T0, price=100., **changes):
    return SolUsdObservation(**{"status": "OK", "price_usd": price,
        "received_at": stamp.timestamp(), "market_updated_at": stamp.timestamp(), **changes})


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    clock = SimpleNamespace(now=T0, price=100., output=100_000_000)
    monkeypatch.setattr(paper, "CFG", replace(paper.CFG, DRY_RUN=True,
        PAPER_EXACT_TRADE_SIZE_ENABLED=True, PAPER_EXACT_TRADE_SIZE_SOL=.1,
        PAPER_RUNNER_RESEARCH_ENABLED=False, PAPER_ENTRY_RESEARCH_ENABLED=False))
    monkeypatch.setattr(paper, "_PORTFOLIO", {})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "data/paper_portfolio.json")
    monkeypatch.setattr(paper, "utc_now", lambda: clock.now)
    monkeypatch.setattr(paper, "_resolve_buy_price_usd", AsyncMock(return_value=(.01, "synthetic")))
    monkeypatch.setattr(paper, "_resolve_execution_fx", AsyncMock(side_effect=lambda: fx(clock.now, clock.price)))
    monkeypatch.setattr(paper, "get_sol_usd", AsyncMock(side_effect=AssertionError("Unowned scalar FX read")))
    monkeypatch.setattr(paper, "_REQUIRE_JUP_PRICE", False)
    monkeypatch.setattr(buy_recovery, "_now", lambda: clock.now.isoformat())
    monkeypatch.setattr(sell_recovery, "now_iso", lambda: clock.now.isoformat())
    for name in ("TRADING_HOURS", "TRADING_HOURS_EXTRA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PAPER_FILL_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("PAPER_FILL_FEE_SOL", "0.000025")
    async def quoted(**kw):
        output = 1000 if kw["input_mint"] == SOL else clock.output
        amount = kw.get("amount_lamports", round(kw.get("amount_sol", .1)*1e9))
        return v1_quote(kw["input_mint"], kw["output_mint"], amount, output, now=clock.now)
    monkeypatch.setattr(paper.jupiter_router, "get_routing_quote", AsyncMock(side_effect=quoted))
    monkeypatch.setattr(paper.runner_forward, "register_partial", lambda *a, **kw: None)
    return clock


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [100., None, fx(status="ERR"), fx(assumed=True), fx(price=True),
    fx(price="100"), fx(received_at=T0.timestamp()-61), fx(market_updated_at=T0.timestamp()-121)])
async def test_unknown_original_entry_fx_never_creates_a_paper_buy(isolated, monkeypatch, bad):
    monkeypatch.setattr(paper, "_resolve_execution_fx", AsyncMock(return_value=bad))
    result = await paper.buy(TOKEN, .1, entry_intent_id="a"*32)
    assert result["qty_lamports"] == 0 and result["signature"] == "ENTRY_PRICE_OR_NOTIONAL_UNAVAILABLE"
    assert paper._PORTFOLIO == {} and not paper._DATA_PATH.exists()


@pytest.mark.asyncio
async def test_entry_and_each_sell_keep_their_own_conversion_in_archive(isolated):
    bought = await paper.buy(TOKEN, .1, entry_intent_id="a"*32)
    assert bought["entry_notional_usd"] == 10.
    assert contract.validate_entry(bought, amount_sol=.1, not_after=T0)
    isolated.now += dt.timedelta(minutes=2)
    isolated.price = 200.
    partial = await paper.sell(TOKEN, 250, exit_intent_id="b"*32)
    assert partial["quote_sol_usd"] == 200. and contract.validate_exit(partial)
    isolated.now += dt.timedelta(minutes=2)
    isolated.price, isolated.output = 50., 300_000_000
    closed = await paper.sell(TOKEN, 750, exit_intent_id="c"*32)
    assert closed["quote_sol_usd"] == 50. and contract.validate_exit(closed)
    rows, issues = paper_archive.read_closed_evidence(paper._DATA_PATH.parent)
    assert not issues and len(rows) == 1
    trade = rows[0]
    assert trade["entry_fx_observation"] == bought["entry_fx_observation"]
    assert [e["response"]["fill_fx_observation"]["price_usd"] for e in trade["exit_fill_events"]] == [200., 50.]
    assert trade["estimated_fees_usd"] == pytest.approx(.000025*(100+200+50))
    assert trade["total_proceeds_sol"] == pytest.approx(.4)
    assert trade["net_total_pnl_usd"] == pytest.approx(25-.000025*350)
    assert _costed_close(trade)[-1] is True  # Original clocks, not current freshness.


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "changed_rate", "bool_rate", "late_clock", "future_receipt",
                                   "all_exit_fx_removed", "all_exits_removed"])
async def test_rehashed_archive_and_direct_costed_close_reject_bad_original_fx(isolated, fault):
    await paper.buy(TOKEN, .1, entry_intent_id="a"*32)
    await paper.sell(TOKEN, 1000, exit_intent_id="b"*32)
    path = next((paper._DATA_PATH.parent / "paper_closed_trades").glob("*.json"))
    doc = json.loads(path.read_text())
    trade = doc["trade"]
    response = trade["exit_fill_events"][0]["response"]
    if fault == "missing":
        response.pop("fill_fx_observation")
    elif fault == "changed_rate": response["fill_fx_observation"]["price_usd"] = 999.
    elif fault == "bool_rate": response["quote_sol_usd"] = True
    elif fault == "late_clock": trade["entry_valued_at"] = (T0+dt.timedelta(seconds=1)).isoformat()
    elif fault == "future_receipt": response["fill_fx_observation"]["received_at"] += 1
    elif fault == "all_exit_fx_removed":
        response.pop("fill_fx_observation")
        response.pop("paper_execution_fx_version")
    else: trade.pop("exit_fill_events")
    assert _costed_close(trade) is None
    doc["payload_sha256"] = paper_archive._digest(trade)
    with pytest.raises(paper_archive.PaperArchiveError): paper_archive._validate_record(doc, path.name)


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [100., fx(status="ERR"), fx(received_at=T0.timestamp()-61)])
async def test_second_original_fx_read_is_required_before_any_sell_money_write(isolated, monkeypatch, after):
    await paper.buy(TOKEN, .1, entry_intent_id="a"*32)
    before, disk = copy.deepcopy(paper._PORTFOLIO), paper._DATA_PATH.read_bytes()
    reader = AsyncMock(side_effect=[fx(), after])
    monkeypatch.setattr(paper, "_resolve_execution_fx", reader)
    failed = await paper.sell(TOKEN, 1000, exit_intent_id="b"*32)
    assert failed["ok"] is False and reader.await_count == 2
    assert paper._PORTFOLIO == before and paper._DATA_PATH.read_bytes() == disk


@pytest.mark.asyncio
async def test_response_loss_recovers_original_entry_fx_into_durable_buy_and_sql(isolated, tmp_path):
    from test_buy_recovery import database
    engine, sessions = await database(tmp_path)
    store = buy_recovery.BuyRecoveryStore(tmp_path / "data/metrics/buy_recovery")
    p = Position(address=TOKEN, token_mint=TOKEN, dry_run=True, qty=0, entry_qty=0,
        buy_price_usd=0., entry_notional_usd=0., buy_amount_sol=.1, opened_at=T0, run_id="synthetic-fx")
    with store.scope():
        attempt = store.begin(p, paper=True, amount_sol=.1)
        bought = await paper.buy(TOKEN, .1, entry_intent_id=attempt.intent_id)
    restarted = buy_recovery.BuyRecoveryStore(store.directory)
    async with sessions() as ses:
        result = await restarted.recover(ses, paper_portfolio=paper._PORTFOLIO)
    assert result["resolved"] == [attempt.intent_id] and not result["failed"]
    row = json.loads((store.directory / "resolved" / (attempt.intent_id+".json")).read_text())
    assert row["fill"]["entry_fx_observation"] == bought["entry_fx_observation"]
    assert contract.validate_entry(row["fill"], amount_sol=.1, not_before=row["created_at"], not_after=row["fill_received_at"])
    assert row["position"]["source_position_key"] == "buy:"+attempt.intent_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_secondary_banks_get_identical_typed_fx_only_after_durable_primary_fill(isolated, monkeypatch):
    from research_loop import entry_gate_forward
    await paper.buy(TOKEN, .1, entry_intent_id="a"*32)
    calls = []
    def first(token, q, rate, **kw):
        assert paper._PORTFOLIO[token]["qty_lamports"] == 0
        assert json.loads(paper._DATA_PATH.read_text())[token]["qty_lamports"] == 0
        calls.append((rate, kw))
        raise OSError("isolated bank failure")
    def second(token, q, rate, **kw): calls.append((rate, kw))
    monkeypatch.setattr(paper.runner_forward, "observe_quote", first)
    monkeypatch.setattr(entry_gate_forward, "observe_quote", second)
    closed = await paper.sell(TOKEN, 1000, exit_intent_id="b"*32)
    assert closed["ok"] and len(calls) == 2
    assert calls[0][1]["fx_observation"] is calls[1][1]["fx_observation"]
    assert calls[0][1]["fx_observation"].to_dict() == closed["fill_fx_observation"]
    assert calls[0][1]["now"] == calls[1][1]["now"] == T0


@pytest.mark.asyncio
async def test_failed_primary_save_does_not_feed_secondary_execution(isolated, monkeypatch):
    from research_loop import entry_gate_forward
    await paper.buy(TOKEN, .1, entry_intent_id="a"*32)
    calls = []
    for component in (paper.runner_forward, entry_gate_forward):
        monkeypatch.setattr(component, "observe_quote", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(paper, "_save", lambda **kw: (_ for _ in ()).throw(OSError("synthetic disk failure")))
    with pytest.raises(OSError): await paper.sell(TOKEN, 1000, exit_intent_id="b"*32)
    assert calls == [] and paper._PORTFOLIO[TOKEN]["qty_lamports"] == 1000


def test_legacy_scalar_records_are_not_retroactively_certified():
    assert contract.validate_entry({"entry_notional_usd": 10.}, amount_sol=.1) is False
    assert contract.validate_exit({"quote_sol_usd": 100., "filled_at": T0.isoformat()}) is False


@pytest.mark.parametrize("kind", ["entry", "runner"])
@pytest.mark.parametrize("fault", ["missing", "scalar", "stale", "assumed", "conflicting"])
def test_actual_banks_require_original_fill_fx_and_keep_pending_ownership(isolated, tmp_path, kind, fault):
    from test_research_cash_reuse import prepared, arms, incoming, observer
    from research_loop import entry_gate_forward as bank, runner_forward as runner
    from research_loop.paper_exit_receipt import make_intent
    cfg, record, path = prepared(kind, tmp_path)
    terminal = next(iter(arms(kind, record).values()))
    stamp = dt.datetime.fromisoformat(record.get("registered_at") or record["decision_at"]) + dt.timedelta(minutes=2)
    quantity = terminal["subject"]["qty_lamports"]
    terminal["intent"] = make_intent(terminal["subject"], quantity=quantity, reason="synthetic_stop", now=stamp)
    bank.storage.write(path, record)
    runner._ACTIVE_INDEX.clear()
    q = incoming(kind, record, stamp)
    original = path.read_bytes()
    bad = {"missing": None, "scalar": 100., "stale": fx(stamp, received_at=stamp.timestamp()-61),
           "assumed": fx(stamp, assumed=True), "conflicting": fx(stamp, price=999.)}[fault]
    args = dict(root=tmp_path, cfg=cfg, now=stamp, quote_started_at=stamp)
    assert observer(kind).observe_quote(record["token"], q, 100., fx_observation=bad, **args) == 0
    assert path.read_bytes() == original
    assert observer(kind).observe_quote(record["token"], q, 100., fx_observation=fx(stamp), **args) == 1
    updated = bank.storage.read(path) if path.exists() else bank.storage.read(path.parent.parent / "closed" / path.name)
    filled = next(a for a in arms(kind, updated).values() if a["fills"])
    assert filled["fills"][0]["fx_observation"] == fx(stamp).to_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "lost_fx", "wrong_clock", "lost_mode", "lost_all_fx"])
async def test_sell_journal_and_close_replay_retain_the_original_fx(isolated, tmp_path, fault):
    from runtime import close_recovery
    bought = await paper.buy(TOKEN, .1, entry_intent_id="a"*32)
    p = Position(id=1, address=TOKEN, token_mint=TOKEN, dry_run=True, closed=False, qty=1000, entry_qty=1000,
        buy_price_usd=bought["buy_price_usd"], entry_notional_usd=10., buy_amount_sol=.1, opened_at=T0,
        run_id="synthetic-fx", source_position_key="buy:"+"a"*32, buy_tx_sig=bought["signature"])
    store = sell_recovery.SellRecoveryStore(tmp_path / "data/metrics/sell_recovery")
    attempt = store.begin(p, 1000, paper=True, reason="synthetic_close", paper_before=copy.deepcopy(paper._PORTFOLIO[TOKEN]))
    response = await paper.sell(TOKEN, 1000, exit_intent_id=attempt.intent_id)
    checked = attempt.receive(response)
    assert attempt.row["fill"]["fill_fx_observation"] == response["fill_fx_observation"]
    p.qty, p.closed, p.closed_at = 0, True, T0
    event = {"event_type": "close", "ts_utc": T0.isoformat(), "qty": 1000, "price_usd": response["price_used_usd"]}
    record = close_recovery.build_recovery_record(p, event_type="close", reason="synthetic_close",
                                                 sell_response=checked, trade_event=event)
    close_recovery._validate_record(record)
    assert record["paper_fill_fx"]["fill_fx_observation"] == response["fill_fx_observation"]
    if fault == "lost_fx":
        record["paper_fill_fx"].pop("fill_fx_observation")
    elif fault == "wrong_clock":
        record["paper_fill_fx"]["filled_at"] = (T0+dt.timedelta(seconds=1)).isoformat()
    elif fault == "lost_mode":
        record.pop("paper_fill_dry_run")
    elif fault == "lost_all_fx":
        record.pop("paper_fill_fx")
    if fault != "none":
        with pytest.raises((ValueError, KeyError)): close_recovery._validate_record(record)


@pytest.mark.asyncio
@pytest.mark.parametrize("sql_mode", [True, False])
async def test_original_paper_close_fx_replay_checks_sql_mode_without_changing_it(isolated, tmp_path, sql_mode):
    from runtime import close_recovery
    from test_buy_recovery import database
    await paper.buy(TOKEN, .1, entry_intent_id="a"*32)
    response = await paper.sell(TOKEN, 1000, exit_intent_id="b"*32)
    response["_qty_before"] = 1000
    engine, sessions = await database(tmp_path)
    async with sessions() as ses:
        p = Position(address=TOKEN, token_mint=TOKEN, dry_run=sql_mode, closed=False, qty=1000, entry_qty=1000,
            buy_price_usd=.01, entry_notional_usd=10., buy_amount_sol=.1, opened_at=T0,
            run_id="synthetic-fx", source_position_key="buy:"+"a"*32)
        ses.add(p)
        await ses.commit()
        position_id = p.id
        # The immutable receipt is from a PAPER execution; the target SQL row
        # may not acquire PAPER mode merely by replaying that close snapshot.
        p.dry_run, p.qty, p.closed, p.closed_at = True, 0, True, T0
        record = close_recovery.build_recovery_record(p, event_type="close", reason="synthetic_close",
            sell_response=response, trade_event={"event_type": "close", "ts_utc": T0.isoformat(), "qty": 1000,
                                                "price_usd": response["price_used_usd"]})
        await ses.rollback()
    async with sessions() as ses:
        if sql_mode:
            restored = await close_recovery._apply_record(ses, record)
            assert restored.dry_run is True and restored.qty == 0 and restored.closed is True
        else:
            with pytest.raises(ValueError, match="LIVE SQL"):
                await close_recovery._apply_record(ses, record)
            restored = await ses.get(Position, position_id)
            assert restored.dry_run is False and restored.qty == 1000 and restored.closed is False
        await ses.rollback()
    await engine.dispose()


def test_entry_gate_original_typed_prefix_is_not_retroactively_reversioned(isolated, tmp_path):
    from test_entry_gate_cash_forward import closed_fixture
    from research_loop import entry_gate_policy
    row, plan, stamp = closed_fixture(tmp_path)
    prefix, subject = row["cash"]["prefix"], row["cash"]["terminal"]["subject"]
    assert contract.validate_entry(prefix, amount_sol=.1, not_after=prefix["opened_at"])
    expected = entry_gate_policy._entry_cash(row, plan, stamp)
    for values in (prefix, subject):
        values.pop("paper_execution_fx_version")
        values.pop("entry_valued_at")
    assert entry_gate_policy._entry_cash(row, plan, stamp) == expected
    assert "paper_execution_fx_version" not in prefix  # no historical backfill
