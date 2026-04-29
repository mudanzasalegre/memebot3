from __future__ import annotations

from analytics.research_rank_canary import evaluate_research_rank_canary


def test_research_rank_canary_allows_rank_high_paper() -> None:
    token = {"entry_lane": "pump_early_sniper_research", "liquidity_usd": 3000}
    decision = evaluate_research_rank_canary(token, {"rank_score": 70}, dry_run=True, live=False)
    assert decision.allowed
    assert decision.entry_lane == "pump_early_research_rank_canary"


def test_research_rank_canary_live_disabled_by_default() -> None:
    token = {"entry_lane": "pump_early_sniper_research", "liquidity_usd": 3000, "has_jupiter_route": True}
    decision = evaluate_research_rank_canary(token, {"rank_score": 70}, dry_run=False, live=True)
    assert not decision.allowed
    assert decision.reason == "live_disabled"
