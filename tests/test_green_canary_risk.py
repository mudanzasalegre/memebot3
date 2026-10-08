"""Original synthetic SDK/RPC/journal/SQL identities; no provider or bot run."""
from __future__ import annotations

import ast
import copy
import datetime as dt
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from db.models import Position
from execution import chain_reconciliation as chain
from execution import jupiter_managed_contract as managed
from runtime import live_canary as canary, green_canary_risk as risk, execution_provenance as provenance
from runtime.buy_recovery import BuyRecoveryStore
from runtime.sell_recovery import SellRecoveryStore
from runtime.close_recovery import build_recovery_record
from utils.atomic_json import read_json_strict, write_json_atomic
from test_live_signing_boundaries import signer
from test_jupiter_managed_contract import order_payload, execution_payload, TOKEN
from chain_fixtures import evidence

TOKEN_OBSERVATION = {"has_jupiter_route": 1, "price_impact_pct": 1.}


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(canary, "STORE", None)
    monkeypatch.setattr(canary, "STATE", canary.LiveCanaryState())
    monkeypatch.setattr(canary, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=False,
        GREEN_SNIPER_LIVE_ENABLED=True, GREEN_SNIPER_REQUIRE_ROUTE_LIVE=True,
        GREEN_SNIPER_LIVE_MAX_DAILY_BUYS=3, GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL=.5,
        GREEN_SNIPER_LIVE_SIZE_SOL=.1,  # Synthetic fixture; live startup caps remain .01.
        GREEN_SNIPER_LIVE_MAX_CONSECUTIVE_LOSSES=2, GREEN_SNIPER_LIVE_MAX_OPEN=1,
        GREEN_SNIPER_LIVE_MAX_PRICE_IMPACT_PCT=12.))
    canary.initialize(tmp_path, [])
    return canary.STORE


def position():
    return Position(id=1, address=TOKEN, token_mint=TOKEN, qty=0, entry_qty=0,
        opened_at=dt.datetime.now(dt.timezone.utc), closed=False, dry_run=False,
        entry_lane=risk.LANE, buy_amount_sol=.1, buy_price_usd=0., entry_notional_usd=0.,
        run_id="synthetic-risk-run")


def original_execution(signer, *, side, amount, output, confirmation="finalized"):
    raw = order_payload(str(signer.PUBLIC_KEY), amount=amount)
    raw.update(outAmount=str(output), otherAmountThreshold=str(max(1, output * 99 // 100)))
    if side == "sell":
        raw.update(inputMint=TOKEN, outputMint=chain.SOL, feeMint=TOKEN)
    req = managed.ManagedRequest(raw["inputMint"], raw["outputMint"], amount, raw["taker"], 100)
    order = managed.check_order(raw, req)
    signed = signer.sign_base64_transaction(raw["transaction"])
    cap = chain.make_capsule(order, signed, managed.check_signed_packet(order, signed), rpc_source_sha256="a" * 64)
    ex = execution_payload(raw, signer)
    ex.update(outputAmountResult=str(output), totalOutputAmount=str(output))
    tx, status = evidence(cap, ex, confirmation=confirmation)
    receipt = chain.reconcile(cap, ex, tx, status)
    return cap, ex, receipt


def journal_execution(attempt, execution):
    cap, ex, receipt = execution
    with attempt.execution_scope():
        provenance.record("prepared_submission", cap)
        provenance.record("dispatch_started", {"capsule_sha256": cap["sha256"]})
        provenance.record("provider_response", ex)
        provenance.record("chain_receipt", receipt)


def opened(tmp_path, signer, *, confirmation="finalized", reserve=False, position_id=1):
    buys = BuyRecoveryStore(tmp_path / "data/metrics/buy_recovery")
    p = position()
    p.id = position_id
    with buys.scope():
        attempt = buys.begin(p, paper=False, amount_sol=.1)
        if reserve:
            assert canary.reserve_green_live_buy(attempt.row, TOKEN_OBSERVATION) == (True, "ok")
        execution = original_execution(signer, side="buy", amount=100_000_000, output=1000, confirmation=confirmation)
        journal_execution(attempt, execution)
        receipt = execution[2]
        response = {"qty_lamports": 1000, "signature": receipt["provider_reported_signature"],
            "buy_price_usd": 1., "entry_notional_usd": 10., "execution_receipt": receipt}
        attempt.receive(response)
        p.qty = p.entry_qty = 1000
        p.buy_price_usd, p.entry_notional_usd = 1., 10.
        p.buy_tx_sig = response["signature"]
        p.opened_at = dt.datetime.now(dt.timezone.utc)
        attempt.capture_position(p)
        attempt.confirm(p)
    return p, attempt


def closed(tmp_path, signer, *, outputs=(80_000_000,), confirmation="finalized", reserve=False, position_id=1):
    p, buy = opened(tmp_path, signer, reserve=reserve, position_id=position_id)
    sells = SellRecoveryStore(tmp_path / "data/metrics/sell_recovery")
    intents = []
    for n, output in enumerate(outputs):
        quantity = p.qty if n == len(outputs) - 1 else p.qty // 2
        attempt = sells.begin(p, quantity, paper=False, reason="STOP_LOSS")
        execution = original_execution(signer, side="sell", amount=quantity, output=output, confirmation=confirmation)
        journal_execution(attempt, execution)
        receipt = execution[2]
        response = attempt.receive({"signature": receipt["provider_reported_signature"], "price_used_usd": 1.,
            "execution_receipt": receipt, "ok": True})
        p.qty = response["qty_left"]
        p.closed = p.qty == 0
        p.closed_at = dt.datetime.fromisoformat(response["filled_at"]) if p.closed else None
        p.exit_tx_sig = response["signature"]
        p.exit_reason = "STOP_LOSS" if p.closed else None
        record = build_recovery_record(p, event_type="close" if p.closed else "partial_fill", reason="STOP_LOSS",
            sell_response=response, trade_event={"event_type": "close" if p.closed else "partial_fill",
                "ts_utc": response["filled_at"], "qty": quantity, "price_usd": 1.})
        sells.prepare_sql(record)
        sells.acknowledge(record)
        sells.finish(attempt)
        intents.append(attempt)
    return p, buy, intents


def test_import_and_paper_diagnostics_do_not_create_live_operator_state(tmp_path, monkeypatch):
    monkeypatch.setattr(canary, "STORE", None)
    assert canary.evaluate_green_live_canary(TOKEN_OBSERVATION)[0] is False
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("outputs,pnl", [((80_000_000,), -20_012_000), ((20_000_000, 60_000_000), -20_018_000),
    ((120_000_000,), 19_988_000), ((1_000_000_000,), 899_988_000)])
def test_original_cash_and_all_partials_survive_restart_without_duplicate_loss(configured, tmp_path, signer, outputs, pnl):
    p, buy, sells = closed(tmp_path, signer, outputs=outputs, reserve=True)
    first = configured.reconcile([p])
    day = p.closed_at.date().isoformat()
    assert read_json_strict(configured.path)["records"][p.source_position_key]["pnl_lamports"] == pnl
    assert first["daily_loss_lamports"].get(day, 0) == max(0, -pnl)
    assert first["daily_buys"][day] == 1 and first["unvalued_closes"] == 0
    restarted = risk.GreenCanaryRiskStore(tmp_path)
    assert not restarted.ready
    assert restarted.reconcile([p]) == first
    assert restarted.reconcile([p]) == first


def test_cumulative_losses_not_offset_by_winners_and_original_days(configured, tmp_path, signer):
    p, buy, sells = closed(tmp_path, signer)
    configured.reconcile([p])
    doc = read_json_strict(configured.path)
    assert configured.snapshot()["consecutive_losses"] == 1
    tomorrow = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    assert configured.snapshot(now=tomorrow)["daily_loss_lamports"] == configured.snapshot()["daily_loss_lamports"]
    assert doc["records"][p.source_position_key]["buy_at"] == buy.row["fill_received_at"]


def test_later_winner_cannot_erase_daily_losses_or_another_unknown_close(configured, tmp_path, signer):
    first, _, _ = closed(tmp_path, signer)
    winner, _, _ = closed(tmp_path, signer, outputs=(200_000_000,), position_id=2)
    snap = configured.reconcile([first, winner])
    assert sum(snap["daily_loss_lamports"].values()) == 20_012_000 and snap["consecutive_losses"] == 0
    third, _, _ = closed(tmp_path, signer, position_id=3)
    for path in (tmp_path / "data/metrics/sell_recovery/resolved").glob("*.json"):
        if read_json_strict(path)["position_id"] == 3: path.unlink()
    snap = configured.reconcile([first, winner, third])
    assert snap["unvalued_closes"] == 1
    assert canary.evaluate_green_live_canary(TOKEN_OBSERVATION) == (False, "pnl_valuation_unavailable")


@pytest.mark.parametrize("size_cap,loss_cap,reason", [(.01, .5, "live_size_cap"), (.1, .05, "remaining_loss_budget"),
    (float("nan"), .5, "size_cap_required")])
def test_reservation_preserves_live_size_and_remaining_loss_budget(configured, tmp_path, size_cap, loss_cap, reason):
    canary.CFG.GREEN_SNIPER_LIVE_SIZE_SOL, canary.CFG.GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL = size_cap, loss_cap
    buys = BuyRecoveryStore(tmp_path / "data/metrics/buy_recovery")
    with buys.scope():
        attempt = buys.begin(position(), paper=False, amount_sol=.1)
        assert canary.reserve_green_live_buy(attempt.row, TOKEN_OBSERVATION) == (False, reason)
        assert configured.snapshot()["pending_buys"] == 0


@pytest.mark.parametrize("kind", ["prepared", "missing_sql", "missing_journal", "unfinalized_buy", "unfinalized_sell", "missing_partial"])
def test_original_uncertainties_do_not_expire_or_clear_on_restart(configured, tmp_path, signer, kind):
    if kind == "prepared":
        buys = BuyRecoveryStore(tmp_path / "data/metrics/buy_recovery")
        with buys.scope():
            buys.begin(position(), paper=False, amount_sol=.1)
        positions = []
    elif kind == "unfinalized_buy":
        p, _ = opened(tmp_path, signer, confirmation="confirmed")
        positions = [p]
    else:
        p, buy, sells = closed(tmp_path, signer, outputs=(20_000_000, 60_000_000),
            confirmation="confirmed" if kind == "unfinalized_sell" else "finalized")
        positions = [p]
        if kind == "missing_sql": positions = []
        if kind == "missing_journal": (tmp_path / "data/metrics/buy_recovery/resolved" / (buy.intent_id + ".json")).unlink()
        if kind == "missing_partial": (tmp_path / "data/metrics/sell_recovery/resolved" / (sells[0].intent_id + ".json")).unlink()
    if kind in {"missing_sql", "missing_partial"}:
        with pytest.raises(risk.GreenCanaryRiskError): configured.reconcile(positions)
        assert not configured.ready
    else:
        snapshot = configured.reconcile(positions)
        assert snapshot["pending_buys"] or snapshot["unproved_positions"] or snapshot["unvalued_closes"]
        restarted = risk.GreenCanaryRiskStore(tmp_path)
        assert restarted.reconcile(positions) == snapshot
        assert canary.evaluate_green_live_canary(TOKEN_OBSERVATION)[0] is False


@pytest.mark.parametrize("corruption", ["delete_source", "delete_state", "bad_json", "bad_hash", "rewind_close", "changed_reason", "duplicate_source", "future_close"])
def test_corrupt_or_rewound_sources_fail_closed_preserving_previous_document(configured, tmp_path, signer, corruption):
    p, buy, sells = closed(tmp_path, signer)
    configured.reconcile([p])
    previous = configured.path.read_bytes()
    source = tmp_path / "data/metrics/buy_recovery/resolved" / (buy.intent_id + ".json")
    if corruption == "delete_source": source.unlink()
    elif corruption == "delete_state": configured.path.unlink()
    elif corruption == "bad_json": configured.path.write_text("{", encoding="utf-8")
    elif corruption == "bad_hash":
        doc = read_json_strict(configured.path); doc["sha256"] = "0" * 64; write_json_atomic(configured.path, doc)
    elif corruption == "rewind_close": p.closed, p.closed_at = False, None
    elif corruption == "changed_reason": p.exit_reason = "PROFIT"
    elif corruption == "future_close": p.closed_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    elif corruption == "duplicate_source":
        original = read_json_strict(source); original["created_at"] = "2020-01-01T00:00:00+00:00"
        write_json_atomic(source.parent.parent / source.name, original)
    with pytest.raises(risk.GreenCanaryRiskError): configured.reconcile([p])
    assert canary.evaluate_green_live_canary(TOKEN_OBSERVATION) == (False, "risk_ledger_unavailable")
    if corruption not in {"delete_state", "bad_json", "bad_hash"}: assert configured.path.read_bytes() == previous


def test_reservation_before_dispatch_is_idempotent_pending_and_no_fill_release(configured, tmp_path):
    buys = BuyRecoveryStore(tmp_path / "data/metrics/buy_recovery")
    with buys.scope():
        attempt = buys.begin(position(), paper=False, amount_sol=.1)
        assert canary.reserve_green_live_buy(attempt.row, TOKEN_OBSERVATION) == (True, "ok")
        assert canary.reserve_green_live_buy(attempt.row, TOKEN_OBSERVATION) == (False, "buy_intent_already_reserved")
        assert canary.evaluate_green_live_canary(TOKEN_OBSERVATION) == (False, "original_execution_unresolved")
        attempt.receive({"qty_lamports": 0, "signature": "CANARY_RISK_LIMIT"})
    assert configured.reconcile([])["daily_buys"] == {}
    assert canary.evaluate_green_live_canary(TOKEN_OBSERVATION) == (True, "ok")


def test_two_local_reservations_cannot_overbook_budget(configured, tmp_path):
    rows = []
    # Separate already-constructed owners simulate concurrent stale views;
    # normal production BuyRecoveryStore independently serializes entry too.
    owners = [BuyRecoveryStore(tmp_path / "data/metrics/buy_recovery") for _ in range(2)]
    for address, buys in zip((TOKEN, "B" * 32), owners):
        p = position(); p.address = p.token_mint = address
        with buys.scope(): rows.append(buys.begin(p, paper=False, amount_sol=.1).row)
    barrier, results = threading.Barrier(2), []
    def reserve(row):
        barrier.wait(); results.append(canary.reserve_green_live_buy(row, TOKEN_OBSERVATION))
    threads = [threading.Thread(target=reserve, args=(row,)) for row in rows]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(ok for ok, _ in results) == [False, True]
    assert configured.snapshot()["pending_buys"] == 1


def test_disk_failure_before_reservation_prevents_dispatch_and_retains_original_intent(configured, tmp_path, monkeypatch):
    buys = BuyRecoveryStore(tmp_path / "data/metrics/buy_recovery")
    with buys.scope():
        attempt = buys.begin(position(), paper=False, amount_sol=.1)
        previous = configured.path.read_bytes()
        monkeypatch.setattr(risk, "write_json_atomic", Mock(side_effect=OSError("synthetic disk failure")))
        dispatch = Mock()
        with pytest.raises(risk.GreenCanaryRiskError):
            canary.reserve_green_live_buy(attempt.row, TOKEN_OBSERVATION)
            dispatch()
        dispatch.assert_not_called()
        assert configured.path.read_bytes() == previous and attempt.row["state"] == "prepared"


def test_disable_deadline_survives_restart_without_renewal(configured, tmp_path):
    configured.disable("operator-risk", minutes=60)
    first = configured.snapshot()
    restarted = risk.GreenCanaryRiskStore(tmp_path)
    assert restarted.reconcile([])["disabled_until"] == first["disabled_until"]
    assert restarted.snapshot()["disabled"]
    assert not restarted.snapshot(now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))["disabled"]


@pytest.mark.parametrize("field,value,reason", [
    ("GREEN_SNIPER_LIVE_MAX_DAILY_BUYS", True, "daily_buy_cap_required"),
    ("GREEN_SNIPER_LIVE_MAX_DAILY_BUYS", "3", "daily_buy_cap_required"),
    ("GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL", float("nan"), "daily_loss_cap_required"),
    ("GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL", 0, "daily_loss_cap_required"),
    ("GREEN_SNIPER_LIVE_MAX_CONSECUTIVE_LOSSES", 0, "loss_streak_cap_required"),
    ("GREEN_SNIPER_LIVE_MAX_OPEN", False, "open_cap_required"),
    ("GREEN_SNIPER_LIVE_MAX_PRICE_IMPACT_PCT", float("inf"), "price_impact_cap_required")])
def test_invalid_config_never_coerces_to_an_open_risk_budget(configured, field, value, reason):
    setattr(canary.CFG, field, value)
    assert canary.evaluate_green_live_canary(TOKEN_OBSERVATION) == (False, reason)


@pytest.mark.parametrize("impact", [None, True, "1", float("nan"), float("inf"), -1.])
def test_missing_or_invalid_impact_is_not_zero(configured, impact):
    assert canary.evaluate_green_live_canary({**TOKEN_OBSERVATION, "price_impact_pct": impact}) == (False, "price_impact_unavailable")


def test_legacy_scalar_counters_cannot_change_production_state(configured):
    before = configured.path.read_bytes()
    with pytest.raises(risk.GreenCanaryRiskError): canary.record_green_live_buy()
    with pytest.raises(risk.GreenCanaryRiskError): canary.record_green_live_close(pnl_sol=100.)
    assert configured.path.read_bytes() == before


@pytest.mark.parametrize("cap,reason", [("daily", "daily_buy_cap"), ("loss", "daily_loss_cap"),
    ("streak", "loss_streak_cap"), ("open", "open_cap")])
def test_actual_limits_use_original_state_after_restart(configured, tmp_path, signer, cap, reason):
    if cap == "open": p, _ = opened(tmp_path, signer)
    else: p, _, _ = closed(tmp_path, signer)
    configured.reconcile([p])
    if cap == "daily": canary.CFG.GREEN_SNIPER_LIVE_MAX_DAILY_BUYS = 1
    if cap == "loss": canary.CFG.GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL = .01
    if cap == "streak": canary.CFG.GREEN_SNIPER_LIVE_MAX_CONSECUTIVE_LOSSES = 1
    canary.initialize(tmp_path, [p])
    assert canary.evaluate_green_live_canary(TOKEN_OBSERVATION) == (False, reason)


def test_missing_finalized_partial_never_credits_spot_sql_pnl(configured, tmp_path, signer):
    p, _, sells = closed(tmp_path, signer)
    p.total_pnl_usd = 1e30
    source = tmp_path / "data/metrics/sell_recovery/resolved" / (sells[0].intent_id + ".json")
    source.unlink()
    snapshot = configured.reconcile([p])
    assert snapshot["unvalued_closes"] == 1 and snapshot["daily_loss_sol"] == {}
    assert canary.evaluate_green_live_canary(TOKEN_OBSERVATION) == (False, "pnl_valuation_unavailable")


@pytest.mark.parametrize("field,value", [("run_id", "unrelated-run"), ("token_mint", "B" * 32),
    ("exit_tx_sig", "unrelated-signature")])
def test_sql_causal_or_terminal_identity_cannot_borrow_original_cash(configured, tmp_path, signer, field, value):
    p, _, _ = closed(tmp_path, signer)
    setattr(p, field, value)
    with pytest.raises(risk.GreenCanaryRiskError): configured.reconcile([p])
    assert not configured.ready


def test_liquidity_crush_deadline_uses_original_close_time_not_replay_time(configured, tmp_path, signer):
    p, _, _ = closed(tmp_path, signer)
    p.exit_reason = "LIQUIDITY_CRUSH"
    first = configured.reconcile([p])
    deadline = p.closed_at + dt.timedelta(minutes=240)
    assert first["disabled_until"] == deadline.isoformat()
    assert configured.reconcile([p])["disabled_until"] == deadline.isoformat()
    canary.initialize(tmp_path, [p])
    assert canary.evaluate_green_live_canary(TOKEN_OBSERVATION) == (False, "liquidity_crush")


@pytest.mark.asyncio
@pytest.mark.parametrize("paper,failed", [(True, False), (False, True), (False, False)])
async def test_actual_refresh_consumer_is_paper_inert_and_fails_closed(configured, paper, failed):
    source = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    fn = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_refresh_green_live_risk")
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    session = SimpleNamespace(execute=AsyncMock(side_effect=OSError("synthetic SQL failure") if failed else None, return_value=result))
    ns = {"SessionLocal": object, "DRY_RUN": paper, "Position": Position, "PROJECT_ROOT": configured.root,
        "select": __import__("sqlalchemy").select, "live_canary": canary, "_note_runtime_error": Mock(), "log": Mock()}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "run_bot.py", "exec"), ns)
    assert await ns[fn.name](session) is (not failed)
    if paper: session.execute.assert_not_awaited()
    if failed: assert canary.evaluate_green_live_canary(TOKEN_OBSERVATION) == (False, "risk_ledger_unavailable")


def test_actual_original_reservation_is_before_any_buyer_and_startup_before_supervision():
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    calls = [(n.lineno, ast.unparse(n.func)) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    reserve = next(line for line, name in calls if name == "live_canary.reserve_green_live_buy")
    begin = max(line for line, name in calls if name == "_BUY_RECOVERY.begin" and line < reserve)
    primary_buyer = min(line for line, name in calls if name == "buyer.buy" and line > begin)
    assert begin < reserve < primary_buyer
    runner = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_runner")
    init = next(n for n in ast.walk(runner) if isinstance(n, ast.Call) and ast.unparse(n.func) == "_refresh_green_live_risk")
    supervise = next(n for n in ast.walk(runner) if isinstance(n, ast.Call) and ast.unparse(n.func) == "supervise")
    assert init.lineno < supervise.lineno and any(k.arg == "initialize" and k.value.value is True for k in init.keywords)


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_actual_green_live_execution_tail_reserves_before_mock_buyer_and_keeps_unknown(configured, tmp_path, authorized):
    from test_buy_recovery import execution_tail_namespace, MINT
    fake_buyer = SimpleNamespace(buy=AsyncMock(side_effect=RuntimeError("synthetic response loss")))
    buys = BuyRecoveryStore(tmp_path / "data/metrics/buy_recovery")
    ns = execution_tail_namespace(tmp_path, buys, fake_buyer)
    ns.update(DRY_RUN=False, green_fast_path=True, live_canary=canary,
        log=SimpleNamespace(error=Mock(), warning=Mock()))
    canary.CFG.GREEN_SNIPER_LIVE_SIZE_SOL = .1 if authorized else .01
    with buys.scope():
        await ns["execution_tail"]({**TOKEN_OBSERVATION, "address": MINT, "entry_lane": risk.LANE}, SimpleNamespace())
    paths = [*(tmp_path / "data/metrics/buy_recovery").glob("*.json"),
        *(tmp_path / "data/metrics/buy_recovery/resolved").glob("*.json")]
    assert len(paths) == 1
    original = read_json_strict(paths[0])
    if not authorized:
        fake_buyer.buy.assert_not_awaited()
        assert original["state"] == "no_fill" and original["rejection"] == "CANARY_RISK_LIMIT"
        assert configured.reconcile([])["pending_buys"] == 0
    else:
        fake_buyer.buy.assert_awaited_once()
        assert original["state"] == "prepared"
        restarted = risk.GreenCanaryRiskStore(tmp_path)
        assert restarted.reconcile([])["pending_buys"] == 1
        assert canary.evaluate_green_live_canary(TOKEN_OBSERVATION) == (False, "original_execution_unresolved")
