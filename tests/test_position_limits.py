from __future__ import annotations

from types import SimpleNamespace

import runtime.position_limits as limits


def test_cap_zero_blocks_lane(monkeypatch) -> None:
    monkeypatch.setattr(limits, "CFG", SimpleNamespace(LATE_MOMENTUM_WATCH_MAX_OPEN_LIVE=0))
    decision = limits.evaluate_lane_position_limit(
        "pump_early_late_momentum_watch",
        [],
        dry_run=False,
        live=True,
    )
    assert decision.allowed is False
    assert decision.cap == 0


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


def test_cap_n_allows_until_count_reaches_n(monkeypatch) -> None:
    monkeypatch.setattr(limits, "CFG", SimpleNamespace(RESEARCH_RANK_CANARY_MAX_OPEN=2))
    one_open = [{"entry_lane": "pump_early_research_rank_canary"}]
    two_open = one_open * 2
    assert limits.evaluate_lane_position_limit("pump_early_research_rank_canary", one_open, dry_run=True, live=False).allowed
    assert not limits.evaluate_lane_position_limit("pump_early_research_rank_canary", two_open, dry_run=True, live=False).allowed


def test_profit_and_breakout_caps_preserve_zero(monkeypatch) -> None:
    monkeypatch.setattr(
        limits,
        "CFG",
        SimpleNamespace(PUMP_EARLY_PROFIT_MAX_OPEN_LIVE_CANARY=0, PUMP_EARLY_BREAKOUT_MAX_OPEN_PAPER=0),
    )
    assert not limits.evaluate_lane_position_limit("pump_early_pumpswap_profit", [], dry_run=False, live=True).allowed
    assert not limits.evaluate_lane_position_limit("pumpswap_breakout", [], dry_run=True, live=False).allowed
