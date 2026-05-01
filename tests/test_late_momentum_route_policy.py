from __future__ import annotations

from types import SimpleNamespace

import analytics.late_momentum_watch as late


def _token() -> dict:
    return {
        "address": "LATE",
        "price_pct_5m": 400,
        "txns_last_5m": 500,
        "liquidity_usd": 10000,
        "market_cap_usd": 30000,
        "price_impact_pct": 4,
        "rank_score": 70,
        "has_jupiter_route": 0,
    }


def test_late_momentum_paper_no_route_can_buy_with_proxy_tag(monkeypatch) -> None:
    monkeypatch.setattr(
        late,
        "CFG",
        SimpleNamespace(
            LATE_MOMENTUM_WATCH_ENABLED=True,
            LATE_MOMENTUM_WATCH_MIN_PRICE5M=300,
            LATE_MOMENTUM_WATCH_MAX_PRICE5M=750,
            LATE_MOMENTUM_WATCH_MIN_RANK_SCORE=55,
            LATE_MOMENTUM_WATCH_MIN_TXNS_5M=300,
            LATE_MOMENTUM_WATCH_MIN_LIQUIDITY_USD=2000,
            LATE_MOMENTUM_WATCH_MAX_PRICE_IMPACT_PCT=12,
            LATE_MOMENTUM_WATCH_ALLOW_RANK_MISSING_PAPER=True,
            LATE_MOMENTUM_WATCH_REQUIRE_ROUTE_PAPER=False,
            LATE_MOMENTUM_WATCH_REQUIRE_ROUTE_LIVE=True,
            LATE_MOMENTUM_WATCH_PAPER_ROUTE_PROXY_TAG=True,
            LATE_MOMENTUM_WATCH_PAPER_CANARY_ENABLED=True,
        ),
    )
    decision = late.evaluate_late_momentum_watch(_token(), dry_run=True, live=False)
    assert decision.action == "buy"
    assert decision.route_proxy is True


def test_late_momentum_live_no_route_blocks(monkeypatch) -> None:
    monkeypatch.setattr(
        late,
        "CFG",
        SimpleNamespace(
            LATE_MOMENTUM_WATCH_ENABLED=True,
            LATE_MOMENTUM_WATCH_MIN_PRICE5M=300,
            LATE_MOMENTUM_WATCH_MAX_PRICE5M=750,
            LATE_MOMENTUM_WATCH_MIN_RANK_SCORE=55,
            LATE_MOMENTUM_WATCH_MIN_TXNS_5M=300,
            LATE_MOMENTUM_WATCH_MIN_LIQUIDITY_USD=2000,
            LATE_MOMENTUM_WATCH_MAX_PRICE_IMPACT_PCT=12,
            LATE_MOMENTUM_WATCH_REQUIRE_ROUTE_PAPER=False,
            LATE_MOMENTUM_WATCH_REQUIRE_ROUTE_LIVE=True,
            LATE_MOMENTUM_WATCH_PAPER_ROUTE_PROXY_TAG=True,
            LATE_MOMENTUM_WATCH_LIVE_ENABLED=True,
        ),
    )
    decision = late.evaluate_late_momentum_watch(_token(), dry_run=False, live=True)
    assert decision.action == "shadow"
    assert "no_route_live" in decision.reject_reasons
