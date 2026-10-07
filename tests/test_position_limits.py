from __future__ import annotations

from types import SimpleNamespace

import runtime.position_limits as limits


def test_cap_zero_is_unlimited(monkeypatch) -> None:
    monkeypatch.setattr(
        limits,
        "CFG",
        SimpleNamespace(LATE_MOMENTUM_WATCH_LIVE_ENABLED=True, LATE_MOMENTUM_WATCH_MAX_OPEN_LIVE=0),
    )
    positions = [{"entry_lane": "pump_early_late_momentum_watch"} for _ in range(50)]
    decision = limits.evaluate_lane_position_limit(
        "pump_early_late_momentum_watch",
        positions,
        dry_run=False,
        live=True,
    )
    assert decision.allowed is True
    assert decision.cap == 0
    assert decision.warning == "cap_zero_unlimited"


def test_cap_minus_one_is_unlimited(monkeypatch) -> None:
    monkeypatch.setattr(limits, "CFG", SimpleNamespace(GREEN_SNIPER_MAX_OPEN_PAPER=-1))
    positions = [{"entry_lane": "pump_early_green_candle_sniper"} for _ in range(50)]
    decision = limits.evaluate_lane_position_limit(
        "pump_early_green_candle_sniper",
        positions,
        dry_run=True,
        live=False,
    )
    assert decision.allowed is True
    assert decision.cap == -1
    assert decision.warning == "cap_negative_unlimited"


def test_cap_n_allows_until_count_reaches_n(monkeypatch) -> None:
    monkeypatch.setattr(limits, "CFG", SimpleNamespace(RESEARCH_RANK_CANARY_MAX_OPEN=2))
    one_open = [{"entry_lane": "pump_early_research_rank_canary"}]
    two_open = one_open * 2
    assert limits.evaluate_lane_position_limit("pump_early_research_rank_canary", one_open, dry_run=True, live=False).allowed
    assert not limits.evaluate_lane_position_limit("pump_early_research_rank_canary", two_open, dry_run=True, live=False).allowed


def test_sniper_research_micro_fallback_is_paper_only(monkeypatch) -> None:
    monkeypatch.setattr(
        limits,
        "CFG",
        SimpleNamespace(SNIPER_RESEARCH_MICRO_FALLBACK_MAX_OPEN=1),
    )
    lane = "pump_early_sniper_research_micro_fallback"

    paper_open = limits.evaluate_lane_position_limit(lane, [], dry_run=True, live=False)
    paper_full = limits.evaluate_lane_position_limit(lane, [{"entry_lane": lane}], dry_run=True, live=False)
    live = limits.evaluate_lane_position_limit(lane, [], dry_run=False, live=True)

    assert paper_open.allowed is True
    assert paper_open.cap == 1
    assert paper_full.allowed is False
    assert live.allowed is False
    assert live.cap == 0
    assert live.reason == "lane_live_disabled"


def test_sniper_research_micro_fallback_live_zero_cap_unlimited(monkeypatch) -> None:
    monkeypatch.setattr(
        limits,
        "CFG",
        SimpleNamespace(SNIPER_RESEARCH_MICRO_FALLBACK_LIVE_ENABLED=True, SNIPER_RESEARCH_MICRO_FALLBACK_MAX_OPEN=0),
    )
    lane = "pump_early_sniper_research_micro_fallback"
    positions = [{"entry_lane": lane} for _ in range(50)]

    decision = limits.evaluate_lane_position_limit(lane, positions, dry_run=False, live=True)

    assert decision.allowed is True
    assert decision.cap == 0
    assert decision.warning == "cap_zero_unlimited"


def test_profit_and_breakout_caps_preserve_zero(monkeypatch) -> None:
    monkeypatch.setattr(
        limits,
        "CFG",
        SimpleNamespace(PUMP_EARLY_PROFIT_MAX_OPEN_LIVE_CANARY=0, PUMP_EARLY_BREAKOUT_MAX_OPEN_PAPER=0),
    )
    profit = limits.evaluate_lane_position_limit("pump_early_pumpswap_profit", [], dry_run=False, live=True)
    breakout = limits.evaluate_lane_position_limit("pumpswap_breakout", [], dry_run=True, live=False)

    assert profit.allowed
    assert profit.warning == "cap_zero_unlimited"
    assert breakout.allowed
    assert breakout.warning == "cap_zero_unlimited"
