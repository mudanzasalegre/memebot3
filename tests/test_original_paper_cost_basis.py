"""Freeze original estimated PAPER costs; isolated receipts, no market run."""
import copy
import datetime as dt
import json

import pytest

from runtime import trade_learning as learning
from execution import paper_execution_cost as cost
from test_paper_execution_fx import isolated, T0, TOKEN
from test_paper_archive import paper
from test_trade_learning import closed, clean_learning


def model(**changes):
    return dict(version="estimated-v1", observed_execution=False,
                slippage_bps=0., fee_sol_per_fill=.000025, **changes)


def original():
    current = model()
    return dict(execution_cost_model=current, opened_at=T0.isoformat(),
                **cost.capture(current, at=T0))


@pytest.mark.parametrize("fee", [0., .000025, .002])
def test_capture_is_detached_and_accepts_typed_zero_fee(fee):
    current = model()
    current["fee_sol_per_fill"] = fee
    record = cost.capture(current, at=T0.astimezone(dt.timezone(dt.timedelta(hours=2))))
    assert record["entry_costed_at"] == T0.isoformat()
    assert cost.validate_entry(record, not_before=T0, not_after=T0, required=True)
    current["fee_sol_per_fill"] = 999.
    assert record["entry_execution_cost_model"]["fee_sol_per_fill"] == fee


@pytest.mark.parametrize("field,value", [
    ("version", "unknown"), ("observed_execution", True), ("observed_execution", 0),
    ("fee_sol_per_fill", True), ("fee_sol_per_fill", "0"), ("fee_sol_per_fill", -1.),
    ("fee_sol_per_fill", float("inf")), ("fee_sol_per_fill", float("nan")),
    ("slippage_bps", True), ("slippage_bps", "0"), ("slippage_bps", -1.),
    ("slippage_bps", 10000.), ("slippage_bps", float("inf")),
    ("unknown", 1), ("fee_sol_per_fill", None),
])
def test_original_assumptions_reject_unknown_or_untyped_models(field, value):
    current = model()
    current[field] = value
    with pytest.raises((ValueError, TypeError, OverflowError)):
        cost.capture(current, at=T0)


@pytest.mark.parametrize("stamp", [None, True, 1., T0.replace(tzinfo=None), "invalid"])
def test_original_clock_requires_explicit_valid_timezone(stamp):
    with pytest.raises(ValueError):
        cost.capture(model(), at=stamp)


@pytest.mark.parametrize("fault", ["missing_all", *cost.ENTRY_FIELDS, "current_fee",
    "current_slippage", "current_version", "current_unknown", "original_fee",
    "original_slippage", "opened", "fx_clock", "too_early", "too_late", "nonmapping"])
def test_required_original_cost_conflicts_fail_closed(fault):
    row, bounds = original(), {}
    if fault == "missing_all":
        for name in cost.ENTRY_FIELDS: row.pop(name)
    elif fault in cost.ENTRY_FIELDS: row.pop(fault)
    elif fault.startswith("current_"):
        field = {"current_fee": "fee_sol_per_fill", "current_slippage": "slippage_bps",
                 "current_version": "version", "current_unknown": "extra"}[fault]
        row["execution_cost_model"][field] = "unknown" if field == "version" else 1.
    elif fault.startswith("original_"):
        row["entry_execution_cost_model"]["fee_sol_per_fill" if fault == "original_fee" else "slippage_bps"] = 1.
    elif fault == "opened": row["opened_at"] = (T0+dt.timedelta(seconds=1)).isoformat()
    elif fault == "fx_clock": row["entry_valued_at"] = (T0+dt.timedelta(seconds=1)).isoformat()
    elif fault == "too_early": bounds["not_before"] = T0+dt.timedelta(seconds=1)
    elif fault == "too_late": bounds["not_after"] = T0-dt.timedelta(seconds=1)
    elif fault == "nonmapping": row = None
    with pytest.raises((ValueError, TypeError, KeyError)):
        cost.validate_entry(row, required=True, **bounds)


def test_legacy_absence_remains_unproved_without_modification():
    row = dict(execution_cost_model=model(), opened_at=T0.isoformat())
    saved = copy.deepcopy(row)
    assert cost.validate_entry(row) is False and row == saved


@pytest.mark.asyncio
async def test_actual_buy_response_journal_and_primary_keep_detached_original_cost(isolated, tmp_path, monkeypatch):
    from db.models import Position
    from runtime.buy_recovery import BuyRecoveryStore
    from runtime.paper_archive import read_closed_evidence
    from trader import papertrading as engine
    store = BuyRecoveryStore(tmp_path / "journal")
    p = Position(address=TOKEN, token_mint=TOKEN, dry_run=True, buy_amount_sol=.1,
                 qty=0, entry_qty=0, buy_price_usd=0., entry_notional_usd=0., opened_at=T0)
    with store.scope():
        attempt = store.begin(p, paper=True, amount_sol=.1)
        response = await engine.buy(TOKEN, .1, entry_intent_id=attempt.intent_id)
        attempt.receive(response)
    primary = engine._PORTFOLIO[TOKEN]
    for name in cost.ENTRY_FIELDS:
        assert response[name] == primary[name] == attempt.row["fill"][name]
    response["entry_execution_cost_model"]["fee_sol_per_fill"] = 999.
    assert primary["entry_execution_cost_model"]["fee_sol_per_fill"] == .000025
    assert attempt.row["fill"]["entry_execution_cost_model"]["fee_sol_per_fill"] == .000025
    # Later environment changes must not relabel already purchased positions.
    monkeypatch.setenv("PAPER_FILL_FEE_SOL", "0.002")
    monkeypatch.setenv("PAPER_FILL_SLIPPAGE_BPS", "200")
    isolated.now += dt.timedelta(minutes=1)
    assert (await engine.sell(TOKEN, 1000, exit_intent_id="c"*32))["ok"]
    trade = read_closed_evidence(engine._DATA_PATH.parent)[0][0]
    assert trade["estimated_fees_sol"] == pytest.approx(.00005)
    assert trade["execution_cost_model"] == model()
    assert cost.validate_entry(trade, required=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["fee", "slippage", "clock", "partial_cost"])
async def test_corrupt_primary_blocks_sell_before_any_quote_or_money_write(isolated, fault):
    from trader import papertrading as engine
    await engine.buy(TOKEN, .1, entry_intent_id="a"*32)
    row = engine._PORTFOLIO[TOKEN]
    if fault == "fee": row["execution_cost_model"]["fee_sol_per_fill"] = 0.
    elif fault == "slippage": row["execution_cost_model"]["slippage_bps"] = 1.
    elif fault == "clock": row["entry_costed_at"] = (T0+dt.timedelta(seconds=1)).isoformat()
    else: row.pop("entry_execution_cost_model")
    before, persisted = copy.deepcopy(row), engine._DATA_PATH.read_bytes()
    engine.jupiter_router.get_routing_quote.reset_mock()
    engine._resolve_execution_fx.reset_mock()
    response = await engine.sell(TOKEN, 1000, exit_intent_id="b"*32)
    assert response["ok"] is False and response["error"] == "ORIGINAL_COST_BASIS_INVALID"
    assert row == before and engine._DATA_PATH.read_bytes() == persisted
    engine.jupiter_router.get_routing_quote.assert_not_called()
    engine._resolve_execution_fx.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepared", "fill_received"])
@pytest.mark.parametrize("fault", ["none", "current_fee", "original_fee", "partial_cost"])
async def test_restart_uses_original_buy_cost_without_environment_backfill(isolated, tmp_path, monkeypatch, stage, fault):
    from db.models import Position
    from runtime.buy_recovery import BuyRecoveryStore
    from test_buy_recovery import database
    from sqlalchemy import select
    from trader import papertrading as paper_engine
    store = BuyRecoveryStore(tmp_path / "journal")
    p = Position(address=TOKEN, token_mint=TOKEN, dry_run=True, buy_amount_sol=.1,
                 qty=0, entry_qty=0, buy_price_usd=0., entry_notional_usd=0., opened_at=T0)
    with store.scope():
        attempt = store.begin(p, paper=True, amount_sol=.1)
        response = await paper_engine.buy(TOKEN, .1, entry_intent_id=attempt.intent_id)
        if stage == "fill_received": attempt.receive(response)
    primary = copy.deepcopy(paper_engine._PORTFOLIO)
    entry = primary[TOKEN]
    if fault == "current_fee": entry["execution_cost_model"]["fee_sol_per_fill"] = 0.
    if fault == "original_fee": entry["entry_execution_cost_model"]["fee_sol_per_fill"] = 0.
    if fault == "partial_cost": entry.pop("entry_execution_cost_model")
    monkeypatch.setenv("PAPER_FILL_FEE_SOL", "0.02")
    restarted = BuyRecoveryStore(store.directory)
    engine, sessions = await database(tmp_path)
    try:
        async with sessions() as session:
            result = await restarted.recover(session, paper_portfolio=primary)
            rows = (await session.execute(select(Position))).scalars().all()
            if fault == "none":
                assert result["resolved"] == [attempt.intent_id] and not result["failed"]
                assert len(rows) == 1
                journal = json.loads((store.directory / "resolved" / (attempt.intent_id+".json")).read_text())
                assert journal["fill"]["entry_execution_cost_model"] == model()
            else:
                assert result["failed"] and not result["resolved"] and not rows
                assert TOKEN in restarted.pending_addresses
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["trade_missing", "fill_missing", "both_missing", "fill_fee", "fill_clock"])
async def test_rehashed_learning_requires_matching_original_buy_journal_cost(closed, fault):
    source = copy.deepcopy(learning.prepare_close(closed.identity, root=closed.root))
    fill, trade = source["buy_proof"]["fill"], source["trade"]
    for part in ([trade] if fault == "trade_missing" else [fill] if fault == "fill_missing"
                 else [trade, fill] if fault == "both_missing" else []):
        for name in cost.ENTRY_FIELDS: part.pop(name)
    if fault == "fill_fee": fill["entry_execution_cost_model"]["fee_sol_per_fill"] = 0.
    if fault == "fill_clock": fill["entry_costed_at"] = (T0-dt.timedelta(seconds=1)).isoformat()
    source["payload_sha256"] = learning._hash({k: v for k, v in source.items() if k != "payload_sha256"})
    with pytest.raises(learning.TradeLearningError): learning.validate_source(source)


@pytest.mark.asyncio
async def test_legacy_archive_stays_diagnostic_without_forward_recertification(isolated):
    from trader import papertrading as engine
    from analytics.forward_evidence import _costed_close
    from runtime.paper_archive import read_closed_evidence, _validate_trade
    await engine.buy(TOKEN, .1, entry_intent_id="a"*32)
    assert (await engine.sell(TOKEN, 1000, exit_intent_id="b"*32))["ok"]
    legacy = read_closed_evidence(engine._DATA_PATH.parent)[0][0]
    for name in cost.ENTRY_FIELDS: legacy.pop(name)
    saved = copy.deepcopy(legacy)
    _validate_trade(legacy)
    assert _costed_close(legacy) is None and legacy == saved


@pytest.mark.parametrize("fault", ["missing", "current_changed", "original_changed"])
def test_partial_case_and_arms_preserve_original_cost_and_reject_rehashed_conflicts(tmp_path, fault):
    from test_runner_forward import entry, cfg, T0 as runner_t0
    from research_loop import runner_forward as runner
    from runtime import runner_enrollment as intake
    captured = runner_t0+dt.timedelta(minutes=1)
    source = intake.capture_source(entry(token_address=TOKEN), captured_at=captured)
    case = runner.prepare_partial_case(source["prefix"], cfg=cfg(), now=captured)
    assert case
    for name in cost.ENTRY_FIELDS:
        assert case["prefix"][name] == source["prefix"][name]
        assert all(arm["subject"][name] == source["prefix"][name] for arm in case["arms"].values())
    if fault == "missing":
        for name in cost.ENTRY_FIELDS: source["prefix"].pop(name)
    else:
        field = "execution_cost_model" if fault == "current_changed" else "entry_execution_cost_model"
        source["prefix"][field]["fee_sol_per_fill"] = 0.
    source["payload_sha256"] = runner._hash({"captured_at": source["captured_at"], "prefix": source["prefix"]})
    with pytest.raises(intake.RunnerEnrollmentError):
        intake.register_source(source, root=tmp_path, cfg=cfg(), now=captured)
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("fault", ["missing", "changed", "none"])
def test_financial_model_metadata_requires_original_cost_population_version(fault):
    import pandas as pd
    from ml.financial_targets import checked_financial_frame, supported_financial_training, financial_target
    from net_financial_fixtures import net_frame
    frame = net_frame(pd.DataFrame([dict(address=TOKEN, timestamp=T0, target_total_pnl_pct=5000.)]))
    rows, population = checked_financial_frame(frame)
    assert len(rows) == 1 and population["cost_basis_version"] == cost.VERSION
    assert supported_financial_training(dict(financial_training=population))
    if fault == "missing": population.pop("cost_basis_version")
    else: population["cost_basis_version"] = None if fault == "none" else "unknown"
    assert not supported_financial_training(dict(financial_training=population))
    assert not financial_target("runner", "runner_10000")


@pytest.mark.parametrize("fault", ["missing", "changed"])
def test_corrupt_declared_cost_cannot_fall_back_to_gross_training_target(fault):
    import pandas as pd
    from ml.financial_targets import checked_net_return, checked_financial_frame, apply_checked_net_returns
    from net_financial_fixtures import net_frame
    frame = net_frame(pd.DataFrame([dict(address=TOKEN, timestamp=T0, target_total_pnl_pct=5000.)]))
    source = json.loads(frame.loc[0, "outcome_execution_proof"])
    if fault == "missing":
        for name in cost.ENTRY_FIELDS: source["buy_proof"]["fill"].pop(name)
    else: source["trade"]["execution_cost_model"]["fee_sol_per_fill"] = 0.
    source["payload_sha256"] = learning._hash({k: v for k, v in source.items() if k != "payload_sha256"})
    frame.loc[0, "outcome_execution_proof"] = json.dumps(source)
    frame.loc[0, "outcome_source_sha256"] = source["payload_sha256"]
    frame["total_pnl_pct"] = 99999.
    assert checked_net_return(frame.iloc[0]) is None
    assert checked_financial_frame(frame)[0].empty
    assert pd.isna(apply_checked_net_returns(frame).iloc[0].total_pnl_pct)


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [50_000_000, 1_100_000_000, 5_100_000_000], indirect=True)
async def test_original_cost_does_not_cap_extreme_returns_or_hide_losses(closed):
    source = learning.prepare_close(closed.identity, root=closed.root)
    value = learning.validate_source(source)
    assert cost.validate_entry(source["trade"], required=True)
    assert value == pytest.approx(source["trade"]["net_total_pnl_pct"])
    assert value < 0 or value > 200


@pytest.mark.asyncio
async def test_rehashed_close_cannot_replace_original_buy_fee(closed):
    source = copy.deepcopy(learning.prepare_close(closed.identity, root=closed.root))
    original = learning.validate_source(source)
    trade = source["trade"]
    trade["execution_cost_model"]["fee_sol_per_fill"] = 0.
    trade["estimated_fees_sol"] = trade["estimated_fees_usd"] = 0.
    trade["net_total_pnl_usd"] = trade["total_pnl_usd"]
    trade["net_total_pnl_pct"] = 100 * trade["net_total_pnl_usd"] / trade["entry_notional_usd"]
    trade["net_total_pnl_sol"] = trade["total_proceeds_sol"] - trade["amount_sol"]
    assert original < 0 < trade["net_total_pnl_pct"]
    source["payload_sha256"] = learning._hash({k: v for k, v in source.items() if k != "payload_sha256"})
    with pytest.raises(learning.TradeLearningError):
        learning.validate_source(source)
