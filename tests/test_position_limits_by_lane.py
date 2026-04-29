from __future__ import annotations

from types import SimpleNamespace

import runtime.position_limits as limits


def test_green_slots_are_separate_from_other_lanes(monkeypatch) -> None:
    monkeypatch.setattr(limits, "CFG", SimpleNamespace(GREEN_SNIPER_MAX_OPEN_PAPER=2))
    open_positions = [
        {"entry_lane": "pump_early_pumpswap_profit"},
        {"entry_lane": "pump_early_green_candle_sniper"},
    ]
    decision = limits.evaluate_lane_position_limit(
        "pump_early_green_candle_sniper",
        open_positions,
        dry_run=True,
        live=False,
    )
    assert decision.allowed is True
    assert decision.open_count == 1
    assert decision.cap == 2
