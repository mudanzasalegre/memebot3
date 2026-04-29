from __future__ import annotations

from analytics.profit_pnl_guard import evaluate_profit_pnl_guard


def test_pnl_guard_blocks_broad_50k_100k_weak_high_txns() -> None:
    decision = evaluate_profit_pnl_guard(
        {
            "gate_profile": "pumpswap_profit_broad",
            "market_cap_usd": 88_000,
            "price_pct_5m": 6,
            "txns_last_5m": 1_100,
        },
        gate_profile="pumpswap_profit_broad",
    )

    assert decision.allowed is False
    assert "pnl_guard_50k_100k_weak_high_txns" in decision.failures


def test_pnl_guard_blocks_local_top_broad_without_blocking_prime() -> None:
    token = {
        "gate_profile": "pumpswap_profit_broad",
        "market_cap_usd": 58_000,
        "price_pct_5m": 65,
        "txns_last_5m": 500,
    }

    assert evaluate_profit_pnl_guard(token, gate_profile="pumpswap_profit_broad").allowed is False
    assert evaluate_profit_pnl_guard(token, gate_profile="pumpswap_profit_prime", prime=True).allowed is True


def test_pnl_guard_keeps_jackpot_momentum_broad() -> None:
    decision = evaluate_profit_pnl_guard(
        {
            "gate_profile": "pumpswap_profit_broad",
            "market_cap_usd": 90_000,
            "price_pct_5m": 240,
            "txns_last_5m": 900,
        },
        gate_profile="pumpswap_profit_broad",
    )

    assert decision.allowed is True


def test_pnl_guard_does_not_apply_to_green_sniper() -> None:
    decision = evaluate_profit_pnl_guard(
        {
            "entry_lane": "pump_early_green_candle_sniper",
            "gate_profile": "green_sniper",
            "market_cap_usd": 88_000,
            "price_pct_5m": 60,
            "txns_last_5m": 1_100,
        },
        gate_profile="green_sniper",
    )

    assert decision.allowed is True
