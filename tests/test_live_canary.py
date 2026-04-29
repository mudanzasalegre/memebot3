from __future__ import annotations

from types import SimpleNamespace

import runtime.live_canary as canary


def test_live_canary_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(canary, "CFG", SimpleNamespace(GREEN_SNIPER_LIVE_ENABLED=False))
    ok, reason = canary.evaluate_green_live_canary({"has_jupiter_route": 1})
    assert ok is False
    assert reason == "green_live_disabled"


def test_live_canary_requires_route(monkeypatch) -> None:
    monkeypatch.setattr(
        canary,
        "CFG",
        SimpleNamespace(
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
