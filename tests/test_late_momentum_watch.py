from __future__ import annotations

import analytics.green_sniper_gate as gate


def test_late_momentum_goes_to_watch_reject_not_green_buy() -> None:
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
        },
        dry_run=True,
        live=False,
    )
    assert decision.action == "reject"
    assert "late_momentum_watch" in decision.reject_reasons
