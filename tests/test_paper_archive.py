"""Isolated synthetic financial evidence; no bot, network or operator-data writes."""
from __future__ import annotations

import ast
import asyncio
import copy
import datetime as dt
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from analytics.forward_evidence import collect_forward_evidence, forward_acceptance
from analytics.report_utils import dedupe_position_rows, load_deduped_positions
from runtime import paper_archive as archive
from utils.atomic_json import read_json_strict, write_json_atomic

MINT = "A" * 32
IDENTITY = "a" * 32


def closed_row(identity=IDENTITY, *, token=MINT):
    opened = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=26)
    return {"entry_intent_id": identity, "buy_signature": "SIM-" + identity,
        "token_address": token, "run_id": "trial", "config_profile": "challenger", "config_hash": "hash",
        "dry_run": True, "closed": True, "opened_at": opened.isoformat(),
        "closed_at": (opened + dt.timedelta(hours=1)).isoformat(), "qty_lamports": 0,
        "entry_qty": 1980198, "entry_notional_usd": 10., "amount_sol": .1,
        "execution_cost_model": {"version": "estimated-v1", "slippage_bps": 100,
            "fee_sol_per_fill": .000025, "observed_execution": False},
        "net_total_pnl_usd": 1., "net_total_pnl_pct": 10., "total_pnl_usd": 1.005,
        "estimated_fees_usd": .005, "estimated_fees_sol": .00005, "execution_fill_count": 2,
        "net_total_pnl_sol": .01, "total_proceeds_sol": .11005,
        "entry_route_quote": {"in_amount": 100000000, "out_amount": 2000000,
            "impact_bps": 1, "max_impact_pct": 3}, "quantity_basis": "quoted_raw_spl_units",
        "price_source_close": "jupiter_reverse_quote", "highest_pnl_pct": 10000.}


def evidence(root):
    return collect_forward_evidence(root, run_id="trial", profile="challenger", config_hash="hash",
        started_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48))


@pytest.fixture
def paper(monkeypatch, tmp_path):
    from trader import papertrading as paper
    monkeypatch.setattr(paper, "CFG", replace(paper.CFG, DRY_RUN=True, PAPER_EXACT_TRADE_SIZE_ENABLED=True,
        PAPER_EXACT_TRADE_SIZE_SOL=.1, PAPER_RUNNER_RESEARCH_AUTO_APPLY=False))
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "data" / "paper_portfolio.json")
    monkeypatch.setattr(paper, "_PORTFOLIO", {})
    monkeypatch.setattr(paper, "_SELL_LOCKS", {})
    monkeypatch.setattr(paper, "_ARCHIVE_REPAIR_STATE", {})
    monkeypatch.setattr(paper, "_research_root", lambda: tmp_path)
    monkeypatch.setattr(paper, "_REQUIRE_JUP_PRICE", False)
    monkeypatch.delenv("TRADING_HOURS", raising=False)
    monkeypatch.delenv("TRADING_HOURS_EXTRA", raising=False)
    monkeypatch.setenv("PAPER_FILL_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("PAPER_FILL_FEE_SOL", "0.000025")
    async def route(mint, amount_sol, *, proof):
        assert amount_sol == .1
        from execution.quote_receipt import capture_summary
        from quote_fixtures import v1_quote, SOL
        q = v1_quote(SOL, mint, 100000000, 1000, now=paper.utc_now(), impact_bps=1)
        proof.update(capture_summary(q, input_mint=SOL, output_mint=mint, amount=100000000,
            slippage=q.other["slippageBps"], limit=3., now=paper.utc_now()))
        return True, "SYNTHETIC_QUOTE"
    async def reverse(**kwargs):
        from quote_fixtures import v1_quote
        return v1_quote(kwargs["input_mint"], kwargs["output_mint"], kwargs["amount_lamports"], 200000000,
            now=paper.utc_now(), impact_bps=1)
    monkeypatch.setattr(paper, "_has_jupiter_route", AsyncMock(side_effect=route))
    monkeypatch.setattr(paper, "_resolve_buy_price_usd", AsyncMock(return_value=(1., "synthetic")))
    from paper_fx_fixtures import install
    install(monkeypatch, paper)
    monkeypatch.setattr(paper, "get_sol_usd", AsyncMock(return_value=100.))
    monkeypatch.setattr(paper.jupiter_router, "get_routing_quote", AsyncMock(side_effect=reverse))
    monkeypatch.setattr(paper.runner_forward, "observe_quote", lambda *args, **kwargs: None)
    monkeypatch.setattr(paper.runner_forward, "register_partial", lambda *args, **kwargs: None)
    import research_loop.entry_gate_forward as entry_forward
    monkeypatch.setattr(entry_forward, "observe_quote", lambda *args, **kwargs: None)
    return paper


def test_first_launch_reader_is_read_only(tmp_path):
    absent = tmp_path / "absent"
    assert archive.read_closed_evidence(absent) == ([], [])
    assert not absent.exists()


def test_archive_is_immutable_idempotent_and_keeps_legacy_bytes(tmp_path):
    legacy = tmp_path / "paper_closed_trades.jsonl"
    legacy.write_bytes(b'{"historic":true}\n')
    row = closed_row()
    assert archive.archive_closed_trade(tmp_path, row, token=MINT) == IDENTITY
    path = tmp_path / "paper_closed_trades" / (IDENTITY + ".json")
    before, modified = path.read_bytes(), path.stat().st_mtime_ns
    assert archive.archive_closed_trade(tmp_path, row, token=MINT) == IDENTITY
    assert path.read_bytes() == before and path.stat().st_mtime_ns == modified
    assert legacy.read_bytes() == b'{"historic":true}\n'
    with pytest.raises(archive.PaperArchiveError, match="cannot be overwritten"):
        archive.archive_closed_trade(tmp_path, {**row, "net_total_pnl_usd": 999}, token=MINT)
    assert path.read_bytes() == before


def test_snapshot_filters_private_payload_and_does_not_alias_input(tmp_path):
    row = closed_row()
    row["private_key"] = "not-a-real-secret"
    row["entry_route_quote"]["authorization"] = "not-a-real-secret"
    row["execution_cost_model"]["credential"] = "not-a-real-secret"
    row["exit_fill_events"] = [{"intent_id": "b" * 32, "qty_before": 1000,
        "response": {"qty_sold": 1000, "provider_private_payload": "not-a-real-secret"}}]
    archive.archive_closed_trade(tmp_path, row, token=MINT)
    row["net_total_pnl_usd"] = -999
    contents = (tmp_path / "paper_closed_trades" / (IDENTITY + ".json")).read_text()
    assert "not-a-real-secret" not in contents
    rows, issues = archive.read_closed_evidence(tmp_path)
    assert not issues and rows[0]["net_total_pnl_usd"] == 1


@pytest.mark.parametrize("change", [{"closed": False}, {"qty_lamports": 1}, {"qty_lamports": False},
    {"dry_run": False}, {"closed_at": "2000-01-01"}, {"entry_intent_id": "bad"},
    {"buy_signature": "LIVE"}, {"token_mint": "other"}, {"net_total_pnl_usd": float("nan")},
    {"entry_intent_id": int("1" * 32)}, {"source_position_key": "buy:" + "b" * 32},
    {"exit_fill_events": ["not a fill object"]}])
def test_invalid_trade_cannot_create_a_cell(tmp_path, change):
    with pytest.raises(archive.PaperArchiveError):
        archive.archive_closed_trade(tmp_path, {**closed_row(), **change}, token=MINT)
    assert not (tmp_path / "paper_closed_trades").exists()


@pytest.mark.parametrize("failure", ["checksum", "torn", "filename", "version"])
def test_corrupt_archive_blocks_acceptance_and_reports_without_hiding_valid_trade(tmp_path, failure):
    data = tmp_path / "data"
    row = closed_row()
    archive.archive_closed_trade(data, row, token=MINT)
    second = {**row, "entry_intent_id": "b" * 32, "buy_signature": "SIM-" + "b" * 32}
    archive.archive_closed_trade(data, second, token=MINT)
    path = data / "paper_closed_trades" / ("b" * 32 + ".json")
    record = read_json_strict(path)
    if failure == "torn": path.write_text('{"unfinished":')
    elif failure == "filename": path.rename(path.with_name("wrong.json"))
    else:
        if failure == "checksum": record["trade"]["net_total_pnl_usd"] = 999
        else: record["version"] = True
        write_json_atomic(path, record)
    rows, issues = archive.read_closed_evidence(data)
    assert len(rows) == len(issues) == 1
    result = evidence(tmp_path)
    assert result["closed_trades"] == 1 and result["total_pnl_usd"] == 1
    assert "paper_closed_archive_unreadable" in forward_acceptance(result)["rejection_reasons"]
    with pytest.raises(archive.PaperArchiveError): load_deduped_positions(tmp_path)


def test_bad_legacy_line_is_visible_and_not_a_silent_missing_loss(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "paper_closed_trades.jsonl").write_text(json.dumps(closed_row()) + '\n{"broken":\n')
    result = evidence(tmp_path)
    assert result["closed_trades"] == 1 and result["closed_archive_integrity_issues"][0]["line"] == 2
    assert "paper_closed_archive_unreadable" in result["evidence_rejections"]


def test_repeat_buys_at_same_mint_timestamp_remain_distinct(tmp_path):
    row = closed_row()
    other = {**row, "entry_intent_id": "b" * 32, "buy_signature": "SIM-" + "b" * 32}
    for trade in (row, other): archive.archive_closed_trade(tmp_path / "data", trade, token=MINT)
    result = evidence(tmp_path)
    assert result["closed_trades"] == 2 and result["distinct_closed_tokens"] == 1
    assert result["total_pnl_usd"] == 2 and result["committed_capital_sol"] == pytest.approx(.2)
    assert result["runner_capture_ratio"] == pytest.approx(.001)  # +10,000% peak is not captured profit.
    assert len(load_deduped_positions(tmp_path)) == 2


@pytest.mark.parametrize("peak", [500, 1000, 5000, 10000, 50000])
def test_extreme_runner_peak_is_preserved_without_claiming_it_was_captured(tmp_path, peak):
    row = {**closed_row(), "highest_pnl_pct": peak}
    archive.archive_closed_trade(tmp_path / "data", row, token=MINT)
    saved, issues = archive.read_closed_evidence(tmp_path / "data")
    result = evidence(tmp_path)
    assert not issues and saved[0]["highest_pnl_pct"] == peak
    assert result["total_pnl_usd"] == 1 and result["runner_capture_ratio"] == pytest.approx(10 / peak)


def test_single_legacy_alias_merges_but_ambiguous_alias_is_not_invented(tmp_path):
    row = closed_row()
    legacy = {key: value for key, value in row.items() if key not in {"entry_intent_id", "buy_signature"}}
    assert len(dedupe_position_rows([row, legacy], [])) == 1
    sql = {**legacy, "source_position_key": "buy:" + IDENTITY, "id": 7}
    assert len(dedupe_position_rows([row, legacy], [sql])) == 1
    second = {**row, "entry_intent_id": "b" * 32, "buy_signature": "SIM-" + "b" * 32}
    with pytest.raises(archive.PaperArchiveError, match="multiple causal"):
        dedupe_position_rows([row, second, legacy], [])
    data = tmp_path / "data"
    for trade in (row, second): archive.archive_closed_trade(data, trade, token=MINT)
    (data / "paper_closed_trades.jsonl").write_text(json.dumps(legacy))
    result = evidence(tmp_path)
    assert result["closed_trades"] == 2 and "ambiguous_legacy_trade_identity" in result["evidence_rejections"]


@pytest.mark.asyncio
async def test_archive_failure_keeps_completed_fill_then_repairs_without_another_order(paper, monkeypatch):
    await paper.buy(MINT, .1, entry_intent_id=IDENTITY)
    original_archive = paper.archive_closed_trade
    def fail(*args, **kwargs): raise archive.PaperArchiveError("synthetic disk failure")
    monkeypatch.setattr(paper, "archive_closed_trade", fail)
    result = await paper.sell(MINT, 1000, exit_intent_id="c" * 32)
    entry = paper._PORTFOLIO[MINT]
    assert result["ok"] and result["qty_left"] == 0 and entry["closed_archive_pending"]
    durable = read_json_strict(paper._DATA_PATH)[MINT]
    assert durable["qty_lamports"] == 0 and len(durable["exit_fill_events"]) == 1
    financial = (entry["net_total_pnl_sol"], entry["execution_fill_count"], entry["estimated_fees_sol"])
    assert await paper.sell(MINT, 1000, exit_intent_id="c" * 32) == result
    assert paper.jupiter_router.get_routing_quote.await_count == 1
    monkeypatch.setattr(paper, "archive_closed_trade", original_archive)
    repaired = await paper.repair_paper_archives(force=True)
    assert repaired == {"status": "ok", "attempted": 1, "failed": 0}
    assert not entry["closed_archive_pending"] and len(archive.read_closed_evidence(paper._DATA_PATH.parent)[0]) == 1
    assert financial == (entry["net_total_pnl_sol"], entry["execution_fill_count"], entry["estimated_fees_sol"])
    assert paper.jupiter_router.get_routing_quote.await_count == 1 and not paper._SELL_LOCKS


@pytest.mark.asyncio
async def test_acknowledgement_write_failure_never_invalidates_the_sale(paper, monkeypatch):
    await paper.buy(MINT, .1, entry_intent_id=IDENTITY)
    writer = paper.write_json_atomic
    writes = []
    def fail_ack(path, payload):
        writes.append(copy.deepcopy(payload))
        if len(writes) == 2: raise OSError("synthetic marker failure")
        return writer(path, payload)
    monkeypatch.setattr(paper, "write_json_atomic", fail_ack)
    result = await paper.sell(MINT, 1000, exit_intent_id="c" * 32)
    assert result["ok"] and paper._PORTFOLIO[MINT]["closed_archive_pending"]
    assert len(archive.read_closed_evidence(paper._DATA_PATH.parent)[0]) == 1
    before = paper.jupiter_router.get_routing_quote.await_count
    await paper.repair_paper_archives(force=True)
    assert not paper._PORTFOLIO[MINT]["closed_archive_pending"]
    assert before == paper.jupiter_router.get_routing_quote.await_count == 1


@pytest.mark.asyncio
async def test_replacement_waits_for_history_before_touching_providers(paper, monkeypatch):
    await paper.buy(MINT, .1, entry_intent_id=IDENTITY)
    archiver = paper.archive_closed_trade
    def fail(*args, **kwargs): raise archive.PaperArchiveError("synthetic disk failure")
    monkeypatch.setattr(paper, "archive_closed_trade", fail)
    await paper.sell(MINT, 1000)
    closed = paper._PORTFOLIO[MINT]
    previous = paper._DATA_PATH.read_bytes()
    route_calls = paper._has_jupiter_route.await_count
    result = await paper.buy(MINT, .1, entry_intent_id="b" * 32)
    assert result["signature"] == "PAPER_ARCHIVE_UNAVAILABLE" and result["qty_lamports"] == 0
    assert paper._PORTFOLIO[MINT] is closed and paper._DATA_PATH.read_bytes() == previous
    assert paper._has_jupiter_route.await_count == route_calls
    reused = await paper.buy(MINT, .1, entry_intent_id=IDENTITY)
    assert reused["signature"] == "ENTRY_INTENT_ALREADY_USED"
    monkeypatch.setattr(paper, "archive_closed_trade", archiver)
    result = await paper.buy(MINT, .1, entry_intent_id="b" * 32)
    assert result["qty_lamports"] == 1000 and paper._PORTFOLIO[MINT]["entry_intent_id"] == "b" * 32
    historical, issues = archive.read_closed_evidence(paper._DATA_PATH.parent)
    assert not issues and len(historical) == 1 and historical[0]["entry_intent_id"] == IDENTITY
    assert historical[0]["net_total_pnl_sol"] > 0


@pytest.mark.asyncio
async def test_concurrent_buys_are_owned_across_quote_await(paper, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    route = paper._has_jupiter_route
    async def waiting(*args, **kwargs):
        started.set()
        await release.wait()
        return await route(*args, **kwargs)
    monkeypatch.setattr(paper, "_has_jupiter_route", AsyncMock(side_effect=waiting))
    first = asyncio.create_task(paper.buy(MINT, .1, entry_intent_id=IDENTITY))
    await started.wait()
    second = asyncio.create_task(paper.buy(MINT, .1, entry_intent_id="b" * 32))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert results[0]["qty_lamports"] == 1000 and results[1]["signature"] == "POSITION_ALREADY_OPEN"
    assert paper._has_jupiter_route.await_count == 1 and not paper._SELL_LOCKS


@pytest.mark.asyncio
async def test_archive_preserves_original_frozen_admission_policy_after_replacement(paper):
    from runtime import paper_entry_policy as policy
    key = "LATE_MOMENTUM_WATCH_MIN_RANK_SCORE"
    baseline = getattr(paper.CFG, key)
    value = baseline + 1 if baseline < 90 else baseline - 1
    with policy.parameter_scope(paper.CFG, {key: value}, revision="synthetic-revision"):
        await paper.buy(MINT, .1, entry_intent_id=IDENTITY)
    frozen = copy.deepcopy(paper._PORTFOLIO[MINT]["paper_entry_policy"])
    await paper.sell(MINT, 1000)
    await paper.buy(MINT, .1, entry_intent_id="b" * 32)
    rows, issues = archive.read_closed_evidence(paper._DATA_PATH.parent)
    assert not issues and rows[0]["paper_entry_policy"] == frozen
    assert paper._PORTFOLIO[MINT]["paper_entry_policy"] is None
    assert getattr(paper.CFG, key) == baseline


@pytest.mark.asyncio
async def test_empty_repair_is_read_only_and_bounded_retry_rotates(paper, monkeypatch):
    assert (await paper.repair_paper_archives(force=True))["attempted"] == 0
    assert not paper._DATA_PATH.parent.exists()
    tokens = [str(i).zfill(32) for i in range(5)]
    paper._PORTFOLIO.update({token: {"closed": True, "closed_archive_pending": True} for token in tokens})
    attempted = []
    def fail(token):
        attempted.append(token)
        raise archive.PaperArchiveError("synthetic error")
    monkeypatch.setattr(paper, "_archive_closed", fail)
    first = await paper.repair_paper_archives(force=True, limit=2)
    assert first == {"status": "pending", "attempted": 2, "failed": 2}
    assert (await paper.repair_paper_archives())["status"] == "throttled"
    await paper.repair_paper_archives(force=True, limit=2)
    assert attempted == tokens[:4] and not paper._SELL_LOCKS


@pytest.mark.asyncio
async def test_runtime_repair_helper_is_paper_only_and_propagates_force(monkeypatch):
    from trader import papertrading as paper
    called = AsyncMock(return_value={"status": "ok", "attempted": 1, "failed": 0})
    monkeypatch.setattr(paper, "repair_paper_archives", called)
    research = AsyncMock(return_value={"status": "ok", "attempted": 0, "failed": 0})
    monkeypatch.setattr(paper, "repair_runner_research", research)
    module = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    function = next(node for node in module.body if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_repair_paper_archive_evidence")
    namespace = {"DRY_RUN": False, "log": SimpleNamespace(warning=lambda *args: None)}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "run_bot.py", "exec"), namespace)
    assert (await namespace[function.name](force=True))["status"] == "disabled"
    called.assert_not_awaited()
    research.assert_not_awaited()
    namespace["DRY_RUN"] = True
    assert (await namespace[function.name](force=True))["attempted"] == 1
    called.assert_awaited_once_with(force=True)
    research.assert_awaited_once_with(force=True)
