from __future__ import annotations

import analytics.green_sniper_gate as gate


def test_late_momentum_canary_buys_only_when_confirmed() -> None:
    decision = gate.evaluate_green_sniper(
        {
            "address": "LATE",
            "entry_regime": "pump_early",
            "age_minutes": 1,
            "liquidity_usd": 10000,
            "market_cap_usd": 20000,
            "price_pct_5m": 400,
            "txns_last_5m": 500,
            "has_jupiter_route": True,
            "price_impact_pct": 4,
            "rank_score": 60,
        },
        dry_run=True,
        live=False,
    )
    assert decision.action == "buy"
    assert decision.lane == "pump_early_late_momentum_watch"
    assert decision.gate_profile == "late_momentum_watch"
