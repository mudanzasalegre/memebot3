from __future__ import annotations

from types import SimpleNamespace

import runtime.live_canary as canary
import runtime.live_canary_v2 as canary_v2


def test_live_canary_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(canary, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=False, GREEN_SNIPER_LIVE_ENABLED=False))
    ok, reason = canary.evaluate_green_live_canary({"has_jupiter_route": 1})
    assert ok is False
    assert reason == "green_live_disabled"


def test_live_canary_blocked_by_strategy_optimization_lock(monkeypatch) -> None:
    monkeypatch.setattr(canary, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=True, GREEN_SNIPER_LIVE_ENABLED=True))
    ok, reason = canary.evaluate_green_live_canary({"has_jupiter_route": 1})
    assert ok is False
    assert reason == "strategy_optimization_lock"


def test_live_canary_requires_route(monkeypatch) -> None:
    monkeypatch.setattr(
        canary,
        "CFG",
        SimpleNamespace(
            STRATEGY_OPTIMIZATION_LOCK=False,
            GREEN_SNIPER_LIVE_ENABLED=True,
            GREEN_SNIPER_REQUIRE_ROUTE_LIVE=True,
            GREEN_SNIPER_LIVE_MAX_DAILY_BUYS=3,
            GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL=0.05,
            GREEN_SNIPER_LIVE_MAX_CONSECUTIVE_LOSSES=2,
            GREEN_SNIPER_LIVE_MAX_PRICE_IMPACT_PCT=12,
        ),
    )
    canary.STATE.daily_buys.clear()
    canary.STATE.daily_loss_sol.clear()
    canary.STATE.consecutive_losses = 0
    canary.STATE.disabled_until = None
    ok, reason = canary.evaluate_green_live_canary({"has_jupiter_route": 0, "price_impact_pct": 1})
    assert ok is False
    assert reason == "no_route"


def test_live_canary_v2_blocked_by_strategy_optimization_lock(monkeypatch) -> None:
    monkeypatch.setattr(
        canary_v2,
        "CFG",
        SimpleNamespace(
            STRATEGY_OPTIMIZATION_LOCK=True,
            LIVE_CANARY_ENABLED=True,
            LIVE_CANARY_MAX_OPEN=1,
            LIVE_CANARY_MAX_DAILY_BUYS=3,
            LIVE_CANARY_DAILY_LOSS_CAP_SOL=0.05,
            LIVE_CANARY_SIZE_SOL=0.01,
        ),
    )

    decision = canary_v2.evaluate_live_canary_v2(
        {"has_jupiter_route": 1},
        candidate_policy_passed=True,
        paper_forward_passed=True,
        manual_approval=True,
        provider_health_ok=True,
    )

    assert decision.allowed is False
    assert decision.reason == "strategy_optimization_lock"
