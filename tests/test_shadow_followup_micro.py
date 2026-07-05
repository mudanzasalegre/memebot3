from __future__ import annotations

from types import SimpleNamespace

from analytics.shadow_followup_micro import apply_shadow_followup_micro_context, evaluate_shadow_followup_micro
from analytics.pump_entry_lane_selector import select_pump_entry_lane


def test_shadow_pnl_25_within_3m_triggers_micro() -> None:
    decision = evaluate_shadow_followup_micro(
        {
            "shadow_pnl_pct": 25,
            "minutes_since_first_seen": 2.5,
            "market_cap_usd": 80_000,
            "has_jupiter_route": True,
        },
        dry_run=True,
        live=False,
    )

    assert decision.allowed is True
    assert decision.reason == "shadow_followup_micro:shadow_pnl_25_within_3m"
    assert decision.amount_sol <= 0.003


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
    cfg = SimpleNamespace(SHADOW_FOLLOWUP_MICRO_MAX_OPEN=1, SHADOW_FOLLOWUP_MICRO_MAX_DAILY_BUYS=5)
    open_cap = evaluate_shadow_followup_micro({"candidate_partial_pnl_pct": 55}, open_count=1, cfg=cfg)
    daily_cap = evaluate_shadow_followup_micro({"candidate_partial_pnl_pct": 55}, daily_buys=5, cfg=cfg)

    assert open_cap.reason == "shadow_followup_open_cap"
    assert daily_cap.reason == "shadow_followup_daily_cap"


def test_shadow_followup_zero_caps_are_unlimited() -> None:
    cfg = SimpleNamespace(SHADOW_FOLLOWUP_MICRO_MAX_OPEN=0, SHADOW_FOLLOWUP_MICRO_MAX_DAILY_BUYS=0)
    decision = evaluate_shadow_followup_micro(
        {"candidate_partial_pnl_pct": 55, "market_cap_usd": 80_000, "has_jupiter_route": True},
        open_count=99,
        daily_buys=99,
        cfg=cfg,
    )

    assert decision.allowed is True


def test_shadow_followup_live_uses_live_flag() -> None:
    disabled = evaluate_shadow_followup_micro(
        {"candidate_partial_pnl_pct": 55},
        dry_run=False,
        live=True,
        cfg=SimpleNamespace(SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED=False),
    )
    enabled = evaluate_shadow_followup_micro(
        {"candidate_partial_pnl_pct": 55, "market_cap_usd": 80_000, "has_jupiter_route": True},
        dry_run=False,
        live=True,
        cfg=SimpleNamespace(SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED=True),
    )

    assert disabled.reason == "shadow_followup_live_disabled"
    assert enabled.allowed is True


def test_shadow_followup_allowed_context_passes_selector() -> None:
    row = {
        "entry_regime": "pump_early",
        "shadow_pnl_pct": 30,
        "minutes_since_first_seen": 2,
        "market_cap_usd": 70_000,
        "has_jupiter_route": True,
    }
    decision = evaluate_shadow_followup_micro(row)

    apply_shadow_followup_micro_context(row, decision)
    selected = select_pump_entry_lane(row)

    assert selected.allowed is True
    assert selected.selected_lane == "pump_early_shadow_followup_micro"
    assert selected.reason == "shadow_followup_momentum"
    assert row["entry_lane"] == "pump_early_shadow_followup_micro"
    assert row["gate_profile"] == "shadow_followup_micro"
    assert row["profit_lane_tier"] == "pump_early_shadow_followup_micro"
    assert row["lane_policy_category"] == "shadow_followup_micro"


def test_shadow_followup_cluster_bad_blocks_except_moonshot_micro_mode() -> None:
    blocked = evaluate_shadow_followup_micro(
        {
            "shadow_pnl_pct": 30,
            "minutes_since_first_seen": 2,
            "market_cap_usd": 70_000,
            "has_jupiter_route": True,
            "cluster_bad": True,
        }
    )
    allowed_cfg = SimpleNamespace(SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL=0.001)
    allowed = evaluate_shadow_followup_micro(
        {
            "shadow_pnl_pct": 30,
            "minutes_since_first_seen": 2,
            "market_cap_usd": 70_000,
            "has_jupiter_route": True,
            "cluster_bad": True,
            "mode": "moonshot",
        },
        cfg=allowed_cfg,
    )

    assert blocked.allowed is False
    assert "cluster_bad" in blocked.failures
    assert allowed.allowed is True
    assert allowed.amount_sol == 0.001


def test_shadow_followup_no_route_marks_route_proxy_without_blocking_paper() -> None:
    decision = evaluate_shadow_followup_micro(
        {
            "shadow_pnl_pct": 30,
            "minutes_since_first_seen": 2,
            "market_cap_usd": 70_000,
            "has_jupiter_route": False,
        },
        dry_run=True,
        live=False,
    )

    assert decision.allowed is True
    assert decision.route_proxy is True


def test_shadow_followup_real_liquidity_breakout_allows_cluster_escape() -> None:
    decision = evaluate_shadow_followup_micro(
        {
            "liquidity_usd": 19_031,
            "liquidity_is_proxy": 0,
            "has_jupiter_route": True,
            "txns_last_5m": 866,
            "volume_24h_usd": 75_636,
            "market_cap_usd": 69_261,
            "age_minutes": 9.2,
            "price_impact_pct": 7.33,
            "price_pct_5m": 8.8,
            "rank_score": 60.3,
            "cluster_bad": True,
        },
        dry_run=True,
        live=False,
    )

    assert decision.allowed is True
    assert decision.reason == "shadow_followup_micro:real_liquidity_breakout"


def test_shadow_followup_honors_configured_amount_without_hidden_cap() -> None:
    cfg = SimpleNamespace(SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL=0.02)
    decision = evaluate_shadow_followup_micro(
        {
            "candidate_partial_pnl_pct": 55,
            "market_cap_usd": 80_000,
            "has_jupiter_route": True,
        },
        dry_run=True,
        live=False,
        cfg=cfg,
    )

    assert decision.allowed is True
    assert decision.amount_sol == 0.02
