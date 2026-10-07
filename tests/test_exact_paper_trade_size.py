from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from analytics.lane_sizing import resolve_lane_buy_amount
from research_loop.candidate_generator import CandidateGenerationError, applicable_generation_spaces, generate_candidate_policies
from analytics.risk_guards import evaluate_pre_entry_risk, ACTION_BUY, ACTION_SHADOW


def _cfg(**overrides):
    return SimpleNamespace(PAPER_EXACT_TRADE_SIZE_ENABLED=True, PAPER_EXACT_TRADE_SIZE_SOL=.1,
                           PAPER_MAX_TRADE_AMOUNT_SOL=overrides.pop("cap", .1), **overrides)


@pytest.mark.parametrize("lane", ["pump_early_moonshot_micro_lottery", "pump_early_shadow_followup_micro",
                                  "pump_early_birth_probe_micro_canary", "pump_early_paper_bootstrap_micro",
                                  "pump_early_paper_exploration_micro", "pump_early_research_rank_canary", "unknown"])
def test_every_paper_lane_uses_exact_requested_amount(lane):
    result = resolve_lane_buy_amount({"entry_lane": lane}, computed_amount_sol=.003,
                                     dry_run=True, live=False, cfg=_cfg())
    assert result.amount_sol == .1 and result.reason == "exact_paper_trade_amount"


@pytest.mark.parametrize("cap", [.03, .099, -1, float("inf"), float("nan")])
def test_incompatible_or_invalid_cap_blocks_instead_of_shrinking(cap):
    result = resolve_lane_buy_amount({"entry_lane": "normal"}, computed_amount_sol=.1,
                                     dry_run=True, live=False, cfg=_cfg(cap=cap))
    assert result.amount_sol == 0 and result.fallback_blocked is True


@pytest.mark.parametrize("amount", [0, -1, float("nan"), float("inf"), "bad"])
def test_bad_computed_amount_does_not_create_an_order(amount):
    result = resolve_lane_buy_amount({}, computed_amount_sol=amount, dry_run=True, live=False, cfg=_cfg())
    assert result.amount_sol == 0


def test_exact_paper_size_cannot_change_live_amount():
    result = resolve_lane_buy_amount({"entry_lane": "normal"}, computed_amount_sol=.02,
                                     dry_run=False, live=True, cfg=_cfg(MAX_TRADE_AMOUNT_SOL=.1))
    assert result.amount_sol == .02


def test_risk_caution_is_shadowed_instead_of_executing_a_smaller_buy():
    row = {"price_pct_5m": 20, "liquidity_usd": 5000, "market_cap_usd": 30000,
           "txns_last_5m": 1000, "has_jupiter_route": True, "liquidity_is_proxy": False}
    decision = evaluate_pre_entry_risk(row, amount_sol=.1, dry_run=True, live=False, cfg=_cfg())
    assert not decision.allowed and decision.action == ACTION_SHADOW and decision.force_shadow
    assert decision.amount_sol == 0 and decision.original_amount_sol == .1
    row["liquidity_usd"] = 20000
    decision = evaluate_pre_entry_risk(row, amount_sol=.1, dry_run=True, live=False, cfg=_cfg())
    assert decision.allowed and decision.action == ACTION_BUY and decision.amount_sol == .1


@pytest.mark.parametrize("amount", [.001, .01, .03, .099, .101, .2])
def test_buyer_rejects_every_different_size_before_quote_or_portfolio_write(monkeypatch, amount):
    from trader import papertrading as paper
    monkeypatch.setattr(paper, "CFG", replace(paper.CFG, PAPER_EXACT_TRADE_SIZE_ENABLED=True,
                                             PAPER_EXACT_TRADE_SIZE_SOL=.1))
    router = AsyncMock()
    monkeypatch.setattr(paper, "_has_jupiter_route", router)
    monkeypatch.setattr(paper, "_PORTFOLIO", {})
    result = asyncio.run(paper.buy("So11111111111111111111111111111111111111112", amount))
    assert result["signature"] == "EXACT_PAPER_SIZE_REQUIRED"
    assert not paper._PORTFOLIO
    router.assert_not_called()


def test_exact_mode_requires_real_quote_proof_even_if_price_route_api_says_true(monkeypatch):
    from trader import papertrading as paper
    monkeypatch.setattr(paper, "CFG", replace(paper.CFG, PAPER_EXACT_TRADE_SIZE_ENABLED=True,
                                             PAPER_EXACT_TRADE_SIZE_SOL=.1))
    monkeypatch.setattr(paper, "_PORTFOLIO", {})
    monkeypatch.setattr(paper, "_has_jupiter_route", AsyncMock(return_value=(True, "UNVERIFIED")))
    result = asyncio.run(paper.buy("So11111111111111111111111111111111111111112", .1,
                                   require_jupiter_for_buy=False))
    assert result["signature"] == "NO_ROUTE" and not paper._PORTFOLIO

def test_exact_size_excludes_inert_amount_parameters_from_research():
    for name in ("rank_canary", "moonshot_micro", "shadow_followup", "paper_bootstrap"):
        candidates = generate_candidate_policies(space_name=name, n=1, mode="grid", cfg=SimpleNamespace(PAPER_EXACT_TRADE_SIZE_ENABLED=True))
        assert candidates
        assert not any(key.endswith(("_AMOUNT_SOL", "_SIZE_SOL")) for key in candidates[0]["changes"])


def test_exact_size_disables_pure_lane_sizing_research():
    cfg = SimpleNamespace(PAPER_EXACT_TRADE_SIZE_ENABLED=True)
    assert applicable_generation_spaces(("lane_sizing", "runner_exit"), cfg=cfg) == ("runner_exit",)
    with pytest.raises(CandidateGenerationError, match="search_space_inapplicable"):
        generate_candidate_policies(space_name="lane_sizing", n=1, cfg=cfg)
