from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from analytics.current_run import current_run_identity, row_in_current_run
from analytics.current_run_reports import _closed
from analytics.forward_evidence import collect_forward_evidence, forward_acceptance
from analytics.paper_bootstrap import _observed_bool, _quality_failures
from fetcher.dexscreener import _norm_from_pair
from trader import papertrading as paper
from fetcher import jupiter_router as router
from test_jupiter_quote_contract import payload, hop, AMOUNT, TOKEN


MINT = "So11111111111111111111111111111111111111112"


def test_untagged_new_event_cannot_replace_current_run():
    rows = [
        {"run_id": "old", "run_started_at": "2026-07-01T00:00:00Z", "ts_utc": "2026-10-03T00:00:00Z"},
        {"run_id": "new", "run_started_at": "2026-07-15T00:00:00Z", "ts_utc": "2026-07-15T00:02:00Z"},
        {"event_type": "social_refresh", "ts_utc": "2026-10-04T00:00:00Z"},
    ]
    identity = current_run_identity(rows=rows)
    assert identity["run_id"] == "new"
    assert not row_in_current_run(rows[-1], identity)


def test_missing_run_start_uses_first_event_not_latest_old_run_activity():
    assert current_run_identity(rows=[
        {"run_id": "old", "ts_utc": "2026-07-01T00:00:00Z"},
        {"run_id": "new", "ts_utc": "2026-07-02T00:00:00Z"},
        {"run_id": "old", "ts_utc": "2026-07-03T00:00:00Z"},
    ])["run_id"] == "new"


def test_open_partial_with_realized_pnl_is_not_closed():
    assert not _closed({"closed": False, "exit_reason": "partial_tp", "realized_pnl_pct": 20})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 2, "unknown"])
def test_unknown_booleans_are_not_evidence(value):
    assert _observed_bool(value) is None


@pytest.mark.parametrize("field", ["price_usd", "liquidity_usd", "market_cap_usd", "score_total", "txns_last_5m", "price_pct_5m", "price_impact_pct"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), "not-a-number"])
def test_bootstrap_rejects_nonfinite_and_malformed_observations(field, value):
    row = {"price_usd": 1, "liquidity_usd": 10000, "market_cap_usd": 25000,
           "score_total": 50, "txns_last_5m": 100, "price_pct_5m": 0, "price_impact_pct": 0,
           "liquidity_is_proxy": False, "has_jupiter_route": True, field: value}
    assert _quality_failures(row, cfg=SimpleNamespace(), require_observed_route=True)


@pytest.mark.parametrize("liquidity,expected", [({"usd": 20000}, False), ({"base": 100}, None), ({"usd": "nan"}, None)])
def test_dex_liquidity_provenance(liquidity, expected):
    row = _norm_from_pair({"baseToken": {"address": MINT}, "priceUsd": "1", "liquidity": liquidity})
    assert row["liquidity_usd_is_proxy"] is expected


def test_inferred_liquidity_is_not_direct_observation():
    assert _norm_from_pair({"baseToken": {"address": MINT}, "priceUsd": "1", "liquidityLocked": 20000})["liquidity_is_proxy"] is True


@pytest.fixture
def isolated_paper(monkeypatch, tmp_path):
    from dataclasses import replace
    # Legacy synthetic-price tests explicitly opt out of the production exact
    # quote/size contract; exact-mode tests below opt in and use real validation.
    monkeypatch.setattr(paper, "CFG", replace(paper.CFG, PAPER_EXACT_TRADE_SIZE_ENABLED=False))
    monkeypatch.setattr(paper, "_PORTFOLIO", {})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "paper_portfolio.json")
    monkeypatch.setattr(paper, "_has_jupiter_route", AsyncMock(return_value=(True, "QUOTE_OK")))
    monkeypatch.setattr(paper, "_resolve_buy_price_usd", AsyncMock(return_value=(1.0, "jupiter")))
    monkeypatch.setattr(paper, "_resolve_entry_notional_usd", AsyncMock(return_value=10.0))
    monkeypatch.setattr(paper, "get_sol_usd", AsyncMock(return_value=100.0))
    monkeypatch.setattr(paper.jupiter_price, "get_usd_price", AsyncMock(return_value=1.0))
    monkeypatch.delenv("TRADING_HOURS", raising=False)
    monkeypatch.delenv("TRADING_HOURS_EXTRA", raising=False)
    monkeypatch.setenv("PAPER_FILL_SLIPPAGE_BPS", "100")
    monkeypatch.setenv("PAPER_FILL_FEE_SOL", "0.000025")
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [None, 0, float("inf"), float("nan")])
async def test_unpriced_exit_never_fabricates_break_even_fill(isolated_paper, monkeypatch, price):
    await paper.buy(MINT, 0.1, require_jupiter_for_buy=False)
    monkeypatch.setattr(paper, "_resolve_close_price_usd", AsyncMock(return_value=(price, "missing")))
    result = await paper.sell(MINT, 100000000)
    assert result["ok"] is False
    assert result["qty_sold"] == 0
    assert not paper._PORTFOLIO[MINT]["closed"]
    assert paper._PORTFOLIO[MINT]["qty_lamports"] == 100000000
    assert not (isolated_paper / "paper_closed_trades.jsonl").exists()


@pytest.mark.asyncio
async def test_costs_are_frozen_and_all_partials_charge_one_fee(isolated_paper, monkeypatch):
    result = await paper.buy(MINT, 0.1, require_jupiter_for_buy=False)
    assert result["buy_price_usd"] == pytest.approx(1.01)
    monkeypatch.setenv("PAPER_FILL_SLIPPAGE_BPS", "900")
    monkeypatch.setattr(paper, "_resolve_close_price_usd", AsyncMock(return_value=(1.0, "jupiter")))
    await paper.sell(MINT, 40000000)
    assert not paper._PORTFOLIO[MINT]["closed"]
    await paper.sell(MINT, 60000000)
    entry = paper._PORTFOLIO[MINT]
    assert entry["execution_fill_count"] == 3
    assert entry["estimated_fees_usd"] == pytest.approx(0.0075)
    assert entry["net_total_pnl_usd"] == pytest.approx(10 * (0.99 / 1.01 - 1) - 0.0075)
    assert entry["net_total_pnl_sol"] == pytest.approx(0.1 * (0.99 / 1.01 - 1) - 0.000075)
    from analytics.forward_evidence import _costed_close
    assert _costed_close(entry) is not None
    assert _costed_close(entry)[4] is False  # synthetic fixture is not executable quote proof
    from runtime.paper_archive import read_closed_evidence
    archived, issues = read_closed_evidence(isolated_paper)
    assert len(archived) == 1 and not issues


@pytest.mark.asyncio
async def test_existing_open_position_cannot_be_overwritten(isolated_paper):
    await paper.buy(MINT, 0.1, require_jupiter_for_buy=False)
    assert (await paper.buy(MINT, 0.1))["signature"] == "POSITION_ALREADY_OPEN"


@pytest.mark.asyncio
async def test_paper_buy_freezes_runner_policy_in_return_and_persisted_portfolio(isolated_paper, monkeypatch):
    from dataclasses import replace
    from analytics.runner_price_policy import parse_policy
    monkeypatch.setattr(paper, "CFG", replace(paper.CFG, RUNNER_PRICE_TRAILING_PAPER_ENABLED=True,
                                             RUNNER_PRICE_TRAILING_DRAWDOWN_PCT=20))
    result = await paper.buy(MINT, 0.1, require_jupiter_for_buy=False)
    frozen = result["runner_trailing_policy"]
    assert parse_policy(frozen)["max_price_drawdown_pct"] == 20
    saved = json.loads((isolated_paper / "paper_portfolio.json").read_text())
    assert saved[MINT]["runner_trailing_policy"] == frozen
    monkeypatch.setattr(paper, "CFG", replace(paper.CFG, RUNNER_PRICE_TRAILING_DRAWDOWN_PCT=40))
    assert paper._PORTFOLIO[MINT]["runner_trailing_policy"] == frozen


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -0.1, float("nan"), float("inf")])
async def test_bad_amount_never_creates_position(isolated_paper, value):
    assert (await paper.buy(MINT, value))["qty_lamports"] == 0
    assert not paper._PORTFOLIO


@pytest.mark.asyncio
async def test_price_api_success_is_not_a_quote_route(isolated_paper, monkeypatch):
    # Restore the real route checker, while the price endpoint still returns $1.
    monkeypatch.setattr(paper.jupiter_router, "get_routing_quote", AsyncMock(return_value=SimpleNamespace(ok=False)))
    # isolated fixture patches this symbol: use original implementation below via saved reference.
    assert (await REAL_ROUTE_CHECK(MINT, 0.1))[0] is None


REAL_ROUTE_CHECK = paper._has_jupiter_route


@pytest.mark.asyncio
@pytest.mark.parametrize("in_amount,out_amount,impact,expected", [
    (100000000, 100, 1, True), (50000000, 100, 1, None),
    (100000000, 0, 1, None), (100000000, 100, None, None),
    (100000000, 100, float("nan"), None), (100000000, 100, 5000, False),
])
async def test_fresh_quote_checks_exact_size_output_and_impact(monkeypatch, in_amount, out_amount, impact, expected):
    body = payload(amount=in_amount, output=out_amount)
    body["priceImpactPct"] = str(impact / 10000) if impact is not None else None
    quote = router._checked_quote(body, input_mint=MINT, output_mint=TOKEN, amount=AMOUNT, slippage=100, direct=False)
    mocked = AsyncMock(return_value=quote)
    monkeypatch.setattr(paper.jupiter_router, "get_routing_quote", mocked)
    monkeypatch.setattr(paper.jupiter_router, "routing_quote_slippage_bps", lambda: 100)
    assert (await REAL_ROUTE_CHECK(TOKEN, 0.1))[0] is expected
    mocked.assert_awaited_once_with(input_mint=MINT, output_mint=TOKEN, amount_sol=0.1, slippage_bps=100)


def test_dedupe_preserves_repeat_trades_case_and_net_costs():
    from analytics.report_utils import dedupe_position_rows
    sql = {"address": "Abc", "opened_at": "2026-10-01T00:00:00Z", "closed": True, "id": 1}
    companion = {**sql, "execution_cost_model": {"version": "estimated-v1"}, "net_total_pnl_usd": -1}
    later = {"address": "Abc", "opened_at": "2026-10-02T00:00:00Z", "closed": True}
    different_case = {"address": "abc", "opened_at": "2026-10-01T00:00:00Z", "closed": True}
    rows = dedupe_position_rows([companion, later, different_case], [sql])
    assert len(rows) == 3
    assert rows[0]["net_total_pnl_usd"] == -1


def test_empty_replay_never_passes_forward(tmp_path, monkeypatch):
    import analytics.paper_forward as forward
    monkeypatch.setattr(forward, "build_policy_replay", lambda root: {})
    report = forward.evaluate_paper_forward({}, root=tmp_path)
    assert report["passed"] is False
    assert "insufficient_forward_trades" in report["forward_acceptance"]["rejection_reasons"]


def test_forward_excludes_old_wrong_profile_partial_and_tests(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    base = {"token_address": MINT, "dry_run": True, "closed": True, "run_id": "new", "config_profile": "candidate",
            "opened_at": "2026-10-03T00:00:00Z", "closed_at": "2026-10-04T00:00:00Z",
            "execution_cost_model": {"version": "estimated-v1", "slippage_bps": 100, "fee_sol_per_fill": .000025, "observed_execution": False},
            "net_total_pnl_usd": 1, "net_total_pnl_pct": 10, "total_pnl_usd": 1.005,
            "entry_notional_usd": 10, "amount_sol": .1, "entry_qty": 100, "qty_lamports": 0,
            "execution_fill_count": 2, "estimated_fees_usd": .005, "estimated_fees_sol": .00005,
            "net_total_pnl_sol": .01}
    rows = [base, {**base, "run_id": "old"}, {**base, "config_profile": "wrong"},
            {**base, "closed": False}, {**base, "test_event": True}]
    (data / "paper_closed_trades.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    (data / "paper_portfolio.json").write_text(json.dumps({MINT: base}))
    evidence = collect_forward_evidence(tmp_path, run_id="new", started_at="2026-10-02T00:00:00Z", profile="candidate")
    assert evidence["closed_trades"] == 1  # archive + portfolio do not double count
    assert evidence["total_pnl_usd"] == 1
    assert evidence["open_positions"] == 0  # stale pre-close snapshot is superseded
    assert not forward_acceptance(evidence)["passed"]


def test_stale_cached_positive_research_metrics_cannot_pass_forward(tmp_path):
    from research_loop.paper_forward import _load_current_paper_metrics
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "current_run_summary.json").write_text(json.dumps({"closed_trades": 1000, "total_pnl_usd": 10000, "elapsed_hours": 300}))
    evidence = _load_current_paper_metrics(tmp_path, {"started_at_utc": "2026-10-01T00:00:00Z", "paper_profile": "candidate"})
    assert evidence["closed_trades"] == 0
    assert evidence["elapsed_hours"] == 0
    assert evidence["evidence_rejections"]


@pytest.mark.asyncio
async def test_exact_quote_units_and_reverse_exit_are_accounted_end_to_end(isolated_paper, monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(paper, "CFG", replace(paper.CFG, PAPER_EXACT_TRADE_SIZE_ENABLED=True))
    token = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    reverse_ok = False

    async def quote(**kwargs):
        if kwargs["input_mint"] == MINT:
            assert kwargs["amount_sol"] == 0.1
            amount, output, source, target = AMOUNT, 2000000, MINT, token
        else:
            assert kwargs["input_mint"] == token
            amount, source, target = kwargs["amount_lamports"], token, MINT
            output = amount * AMOUNT // 2000000
            if not reverse_ok:
                return router.QuoteResult(False, None, None, None, {}, {})
        body = payload(amount=amount, output=output)
        body.update(inputMint=source, outputMint=target)
        body["routePlan"] = [hop(source, target, amount, output)]
        return router._checked_quote(body, input_mint=source, output_mint=target, amount=amount, slippage=100, direct=False)

    monkeypatch.setattr(paper, "_has_jupiter_route", REAL_ROUTE_CHECK)
    monkeypatch.setattr(paper.jupiter_router, "get_routing_quote", quote)
    monkeypatch.setattr(paper.jupiter_router, "routing_quote_slippage_bps", lambda: 100)
    result = await paper.buy(token, 0.1, require_jupiter_for_buy=True)
    qty = result["qty_lamports"]
    assert qty == int(2000000 / 1.01)
    rejected = await paper.sell(token, qty, price_hint=1000000)
    assert rejected["ok"] is False
    assert rejected["error"] == "EXIT_QUOTE_UNAVAILABLE"
    assert not paper._PORTFOLIO[token]["closed"]
    reverse_ok = True
    partial = qty // 2
    await paper.sell(token, partial)
    await paper.sell(token, qty - partial)
    entry = paper._PORTFOLIO[token]
    assert entry["quantity_basis"] == "quoted_raw_spl_units"
    assert entry["net_total_pnl_sol"] == pytest.approx(0.1 * 0.99 / 1.01 - 0.1 - 0.000075, abs=1e-8)
    assert entry["execution_fill_count"] == 3
    from analytics.forward_evidence import _costed_close
    assert _costed_close(entry) is not None
    assert _costed_close(entry)[4] is True


def test_global_diagnostics_and_replay_do_not_count_decisions_or_partials_as_trades(tmp_path):
    from analytics.trade_diagnostics import build_trade_diagnostics
    from backtest.policy_replay import build_policy_replay
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    rows = [
        {"address": "A", "event_type": "candidate_outcome", "pnl_pct": -10, "exit_reason": "timeout"},
        {"address": "A", "event_type": "candidate_partial", "pnl_pct": 100, "exit_reason": "partial_tp"},
        {"address": "B", "event_type": "candidate_decision", "pnl_pct": 0, "exit_reason": "policy_reject"},
        {"address": "C", "event_type": "candidate_stage", "pnl_pct": 50},
    ]
    (metrics / "candidate_outcomes.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    assert build_trade_diagnostics(tmp_path)["summary"]["trades"] == 1
    assert build_policy_replay(tmp_path)["current"]["trades"] == 1


def test_profitable_closed_trades_cannot_hide_unsettled_positions(tmp_path):
    from analytics.forward_evidence import forward_acceptance
    result = forward_acceptance({"closed_trades": 100, "elapsed_hours": 48,
                                 "total_pnl_usd": 100, "mean_return_lower_95_normal_approx_pct": 1,
                                 "open_positions": 1})
    assert not result["passed"]
    assert "unsettled_paper_positions" in result["rejection_reasons"]
