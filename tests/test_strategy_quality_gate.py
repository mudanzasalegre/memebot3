from __future__ import annotations

import scripts.strategy_quality_gate as gate


def test_strategy_quality_gate_returns_list() -> None:
    assert isinstance(gate.checks(), list)
