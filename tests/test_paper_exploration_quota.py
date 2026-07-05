from __future__ import annotations

from types import SimpleNamespace

from analytics.lane_sizing import resolve_lane_buy_amount
from analytics.paper_exploration_quota import (
    apply_paper_exploration_quota_context,
    should_allow_paper_exploration,
)
from analytics.pump_entry_lane_selector import select_pump_entry_lane
from runtime.position_limits import evaluate_lane_position_limit


def _eligible_shadow() -> dict[str, object]:
    return {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_sniper_research",
        "gate_profile": "paper_aggressive_research_guard",
        "entry_subprofile": "sniper_research_momentum_ignition",
        "reason": "sniper_research_high_shadow_ev",
        "price_usd": 0.00001,
        "market_cap_usd": 70_000,
        "has_jupiter_route": True,
        "cluster_bad": False,
        "toxic_initial_sell_pressure": False,
    }


def test_idle_eligible_shadow_becomes_paper_exploration_buy_lane() -> None:
    row = _eligible_shadow()
    decision = should_allow_paper_exploration(
        row,
        hours_without_buy=3.1,
        open_count=0,
        daily_buys=0,
        dry_run=True,
        live=False,
    )

    apply_paper_exploration_quota_context(row, decision)
    selected = select_pump_entry_lane(row)
    sized = resolve_lane_buy_amount(row, computed_amount_sol=0.1, dry_run=True, live=False)

    assert decision.allowed is True
    assert decision.reason == "paper_exploration_quota"
    assert row["entry_lane"] == "pump_early_paper_exploration_micro"
    assert row["gate_profile"] == "paper_exploration_quota"
    assert selected.allowed is True
    assert selected.selected_lane == "pump_early_paper_exploration_micro"
    assert sized.amount_sol == 0.1


def test_paper_exploration_not_idle_blocks() -> None:
    decision = should_allow_paper_exploration(
        _eligible_shadow(),
        hours_without_buy=2.9,
        open_count=0,
        daily_buys=0,
        cfg=SimpleNamespace(PAPER_IDLE_AFTER_HOURS=3),
    )

    assert decision.allowed is False
    assert decision.reason == "paper_exploration_idle_window_not_met"


def test_paper_exploration_zero_idle_window_is_immediate() -> None:
    decision = should_allow_paper_exploration(
        _eligible_shadow(),
        hours_without_buy=0.0,
        open_count=0,
        daily_buys=0,
        cfg=SimpleNamespace(PAPER_IDLE_AFTER_HOURS=0),
    )

    assert decision.allowed is True


def test_paper_exploration_open_cap_blocks() -> None:
    decision = should_allow_paper_exploration(
        _eligible_shadow(),
        hours_without_buy=3.1,
        open_count=1,
        daily_buys=0,
        cfg=SimpleNamespace(PAPER_EXPLORATION_MAX_OPEN=1),
    )

    assert decision.allowed is False
    assert decision.reason == "paper_exploration_open_cap"


def test_paper_exploration_zero_caps_are_unlimited() -> None:
    decision = should_allow_paper_exploration(
        _eligible_shadow(),
        hours_without_buy=3.1,
        open_count=99,
        daily_buys=99,
        cfg=SimpleNamespace(PAPER_EXPLORATION_MAX_OPEN=0, PAPER_IDLE_MAX_DAILY_BUYS=0),
    )

    assert decision.allowed is True


def test_paper_exploration_toxic_blocks() -> None:
    row = _eligible_shadow()
    row["toxic_initial_sell_pressure"] = True

    decision = should_allow_paper_exploration(
        row,
        hours_without_buy=3.1,
        open_count=0,
        daily_buys=0,
    )

    assert decision.allowed is False
    assert decision.reason == "paper_exploration_blocked:toxic_initial_sell_pressure"


def test_paper_exploration_live_blocks() -> None:
    decision = should_allow_paper_exploration(
        _eligible_shadow(),
        hours_without_buy=3.1,
        open_count=0,
        daily_buys=0,
        dry_run=False,
        live=True,
    )

    assert decision.allowed is False
    assert decision.reason == "paper_exploration_paper_only"


def test_paper_exploration_no_route_marks_proxy_without_blocking_paper() -> None:
    row = _eligible_shadow()
    row["has_jupiter_route"] = False

    decision = should_allow_paper_exploration(
        row,
        hours_without_buy=3.1,
        open_count=0,
        daily_buys=0,
    )

    assert decision.allowed is True
    assert decision.route_proxy is True


def test_paper_exploration_api_budget_and_proxy_liquidity_policy() -> None:
    api_block = should_allow_paper_exploration(
        _eligible_shadow(),
        hours_without_buy=3.1,
        open_count=0,
        daily_buys=0,
        api_budget_ok=False,
    )
    proxy_row = _eligible_shadow()
    proxy_row["liquidity_is_proxy"] = True
    proxy_allowed = should_allow_paper_exploration(
        proxy_row,
        hours_without_buy=3.1,
        open_count=0,
        daily_buys=0,
    )
    proxy_block = should_allow_paper_exploration(
        proxy_row,
        hours_without_buy=3.1,
        open_count=0,
        daily_buys=0,
        cfg=SimpleNamespace(PAPER_EXPLORATION_BLOCK_PROXY_LIQUIDITY=True),
    )

    assert api_block.reason == "paper_exploration_api_budget_blocked"
    assert proxy_allowed.allowed is True
    assert proxy_allowed.reason == "paper_exploration_quota"
    assert proxy_block.reason == "paper_exploration_blocked:known_proxy_liquidity"


def test_paper_exploration_position_limit_is_paper_only() -> None:
    paper = evaluate_lane_position_limit(
        "pump_early_paper_exploration_micro",
        [],
        dry_run=True,
        live=False,
    )
    live = evaluate_lane_position_limit(
        "pump_early_paper_exploration_micro",
        [],
        dry_run=False,
        live=True,
    )

    assert paper.allowed is True
    assert paper.cap == 0
    assert live.allowed is False
    assert live.cap == 0
