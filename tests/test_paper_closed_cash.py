"""Original closed PAPER cash, synthetic isolated fills only; no live run."""
import copy
import datetime as dt
import json

import pytest

from analytics.forward_evidence import _costed_close
from execution.paper_closed_cash import reconstruct
from runtime import paper_archive, trade_learning
from test_paper_execution_fx import isolated
from test_trade_learning import closed, clean_learning
from test_paper_archive import paper
from trader import papertrading as paper_impl
from quote_fixtures import TOKEN


async def original_close(clock):
    await paper_impl.buy(TOKEN, .1, entry_intent_id="a" * 32)
    clock.now += dt.timedelta(minutes=1)
    clock.price = 200.
    await paper_impl.sell(TOKEN, 250, exit_intent_id="b" * 32)
    clock.now += dt.timedelta(minutes=1)
    clock.price, clock.output = 50., 300_000_000
    await paper_impl.sell(TOKEN, 750, exit_intent_id="c" * 32)
    reconstruct(paper_archive.paper_snapshot(paper_impl._PORTFOLIO[TOKEN], TOKEN))
    path = next((paper_impl._DATA_PATH.parent / "paper_closed_trades").glob("*.json"))
    return path, json.loads(path.read_text())


def altered(trade, fault):
    if fault == "gross_cash":
        trade["total_pnl_usd"] += 100.
    elif fault == "usd_fees":
        trade["estimated_fees_usd"] /= 2
    elif fault == "sol_cash":
        trade["total_proceeds_sol"] += 1.
    else:
        raise AssertionError(fault)
    trade["net_total_pnl_usd"] = trade["total_pnl_usd"] - trade["estimated_fees_usd"]
    trade["net_total_pnl_pct"] = 100 * trade["net_total_pnl_usd"] / trade["entry_notional_usd"]
    trade["net_total_pnl_sol"] = trade["total_proceeds_sol"] - trade["amount_sol"] - trade["estimated_fees_sol"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["gross_cash", "usd_fees", "sol_cash"])
async def test_coherent_rehashed_original_close_is_not_cash_proof(isolated, fault):
    path, doc = await original_close(isolated)
    assert _costed_close(doc["trade"])[-1] is True
    altered(doc["trade"], fault)
    doc["payload_sha256"] = paper_archive._digest(doc["trade"])
    assert _costed_close(doc["trade"]) is None
    with pytest.raises(paper_archive.PaperArchiveError):
        paper_archive._validate_record(doc, path.name)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["usd_fees", "sol_cash"])
async def test_rehashed_learning_source_cannot_replace_original_money(closed, fault):
    source = copy.deepcopy(trade_learning.prepare_close(closed.identity, root=closed.root))
    altered(source["trade"], fault)
    source["payload_sha256"] = trade_learning._hash({k: v for k, v in source.items() if k != "payload_sha256"})
    with pytest.raises((trade_learning.TradeLearningError, paper_archive.PaperArchiveError)):
        trade_learning.validate_source(source)
    from ml.financial_targets import checked_financial_frame, checked_net_return
    import pandas as pd
    row = dict(sample_type="trade_close", outcome_return_basis=trade_learning.VERSION,
        outcome_execution_proof=json.dumps(source), outcome_trade_id=source["trade_id"],
        outcome_source_sha256=source["payload_sha256"], address=source["trade"]["token_address"],
        timestamp=source["entry_features"]["vector"]["timestamp"],
        outcome_closed_at=source["trade"]["closed_at"],
        target_total_pnl_pct=source["trade"]["net_total_pnl_pct"], total_pnl_pct=99999.)
    assert checked_net_return(row) is None
    frame, report = checked_financial_frame(pd.DataFrame([row]))
    assert frame.empty and report["unchecked_rows"] == 1 and report["ready"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["qty_before", "qty_sold", "qty_left", "duplicate_intent", "reordered",
    "partial_flag", "early_final", "price", "entry_qty", "entry_signature", "prefix_cash",
    "prefix_qty", "prefix_pnl", "partial_count", "last_partial_clock", "total_proceeds_missing",
    "bool_fees", "string_pnl", "nan_pnl", "negative_fee", "wrong_cost_model", "wrong_fill_count"])
async def test_original_cash_rejects_lineage_quantity_price_and_primary_prefix(isolated, fault):
    _, doc = await original_close(isolated)
    trade, events = doc["trade"], doc["trade"]["exit_fill_events"]
    response = events[0]["response"]
    if fault == "qty_before": events[0]["qty_before"] -= 1
    elif fault == "qty_sold": response["qty_sold"] += 1
    elif fault == "qty_left": response["qty_left"] -= 1
    elif fault == "duplicate_intent":
        events[1]["intent_id"] = response["exit_intent_id"]
        events[1]["response"]["exit_intent_id"] = response["exit_intent_id"]
        events[1]["response"]["signature"] = response["signature"]
    elif fault == "reordered": events.reverse()
    elif fault == "partial_flag": response["partial"] = False
    elif fault == "early_final": trade["closed_at"] = response["filled_at"]
    elif fault == "price": response["price_used_usd"] *= 2
    elif fault == "entry_qty": trade["entry_qty"] += 1
    elif fault == "entry_signature": trade["buy_signature"] = "SIM-" + "d" * 32
    elif fault == "prefix_cash": trade["realized_proceeds_sol"] += .01
    elif fault == "prefix_qty": trade["realized_qty"] += 1
    elif fault == "prefix_pnl": trade["net_realized_pnl_usd"] += 1
    elif fault == "partial_count": trade["partial_fill_events"] = 2
    elif fault == "last_partial_clock": trade["last_partial_at"] = trade["closed_at"]
    elif fault == "total_proceeds_missing": trade.pop("total_proceeds_sol")
    elif fault == "bool_fees": trade["estimated_fees_usd"] = True
    elif fault == "string_pnl": trade["net_total_pnl_usd"] = str(trade["net_total_pnl_usd"])
    elif fault == "nan_pnl": trade["net_total_pnl_usd"] = float("nan")
    elif fault == "negative_fee": trade["execution_cost_model"]["fee_sol_per_fill"] = -1
    elif fault == "wrong_cost_model": trade["execution_cost_model"]["observed_execution"] = True
    elif fault == "wrong_fill_count": trade["execution_fill_count"] = True
    else: raise AssertionError(fault)
    with pytest.raises((ValueError, paper_archive.PaperArchiveError)):
        reconstruct(trade)
    assert _costed_close(trade) is None
    with pytest.raises(paper_archive.PaperArchiveError):
        paper_archive._validate_trade(trade)


@pytest.mark.asyncio
@pytest.mark.parametrize("output,slip,fee", [(1_000_000, "0", "0.000025"),
    (100_000_000, "0", "0"), (1_100_000_000, "100", "0.000025"),
    (1_000_100_000_000, "0", "0.000025")])
async def test_signed_loss_break_even_slippage_and_uncapped_extreme_close(isolated, monkeypatch, output, slip, fee):
    monkeypatch.setenv("PAPER_FILL_SLIPPAGE_BPS", slip)
    monkeypatch.setenv("PAPER_FILL_FEE_SOL", fee)
    bought = await paper_impl.buy(TOKEN, .1, entry_intent_id="a" * 32)
    isolated.now += dt.timedelta(minutes=1)
    isolated.output = output
    sold = await paper_impl.sell(TOKEN, bought["qty_lamports"], exit_intent_id="b" * 32)
    assert sold["ok"] and sold["qty_left"] == 0
    rows, issues = paper_archive.read_closed_evidence(paper_impl._DATA_PATH.parent)
    assert not issues and len(rows) == 1
    values = reconstruct(rows[0])
    expected_sol = output / 1e9 * (1 - float(slip) / 10000)
    assert values["total_proceeds_sol"] == pytest.approx(expected_sol)
    assert values["net_total_pnl_sol"] == pytest.approx(expected_sol - .1 - 2 * float(fee))
    assert values["net_total_pnl_usd"] == pytest.approx(100 * (expected_sol - .1 - 2 * float(fee)))
    assert _costed_close(rows[0])[-1] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [{"v2": family} for family in ("metis", "jupiterz", "dflow", "okx")], indirect=True)
async def test_all_router_original_receipts_reconstruct_without_primary_optional_fields(closed):
    source = trade_learning.prepare_close(closed.identity, root=closed.root)
    values = reconstruct(source["trade"])
    assert values["net_total_pnl_pct"] == pytest.approx(-3.)
    assert trade_learning.validate_source(source) == pytest.approx(-3.)


@pytest.mark.asyncio
async def test_multiple_original_partials_conserve_prefix_and_final_separately(isolated):
    await paper_impl.buy(TOKEN, .1, entry_intent_id="a" * 32)
    for quantity, rate, output, intent in ((250, 200., 100_000_000, "b"),
        (250, 50., 200_000_000, "c"), (500, 150., 300_000_000, "d")):
        isolated.now += dt.timedelta(minutes=1)
        isolated.price, isolated.output = rate, output
        assert (await paper_impl.sell(TOKEN, quantity, exit_intent_id=intent * 32))["ok"]
    rows, issues = paper_archive.read_closed_evidence(paper_impl._DATA_PATH.parent)
    assert not issues
    row = rows[0]
    assert row["realized_qty"] == 500 and row["partial_fill_events"] == 2
    assert row["realized_proceeds_sol"] == pytest.approx(.3)
    values = reconstruct(row)
    assert values["total_proceeds_sol"] == pytest.approx(.6)
    assert values["total_pnl_usd"] == pytest.approx(65.)
    assert values["estimated_fees_usd"] == pytest.approx(.000025 * (100+200+50+150))
