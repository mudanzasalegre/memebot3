from __future__ import annotations

from analytics.shadow_followup_micro import evaluate_shadow_followup_micro


def test_shadow_partial_50_triggers_micro_route_proxy() -> None:
    decision = evaluate_shadow_followup_micro(
        {
            "candidate_partial_pnl_pct": 55,
            "market_cap_usd": 80_000,
            "has_jupiter_route": False,
        },
        dry_run=True,
        live=False,
    )

    assert decision.allowed is True
    assert decision.route_proxy is True
    assert decision.amount_sol == 0.003


def test_shadow_followup_toxic_blocks() -> None:
    decision = evaluate_shadow_followup_micro(
        {
            "candidate_partial_pnl_pct": 55,
            "market_cap_usd": 80_000,
            "has_jupiter_route": True,
            "toxic_initial_sell_pressure": True,
        },
        dry_run=True,
        live=False,
    )

    assert decision.allowed is False
    assert "toxic_initial_sell_pressure" in decision.failures


def test_shadow_followup_caps_respected() -> None:
    open_cap = evaluate_shadow_followup_micro({"candidate_partial_pnl_pct": 55}, open_count=1)
    daily_cap = evaluate_shadow_followup_micro({"candidate_partial_pnl_pct": 55}, daily_buys=5)

    assert open_cap.reason == "shadow_followup_open_cap"
    assert daily_cap.reason == "shadow_followup_daily_cap"

