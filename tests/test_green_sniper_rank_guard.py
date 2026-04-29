from __future__ import annotations

from types import SimpleNamespace

import analytics.green_sniper_rank_guard as guard


def test_green_sniper_rank_guard_allows_above_min(monkeypatch) -> None:
    monkeypatch.setattr(
        guard,
        "CFG",
        SimpleNamespace(GREEN_SNIPER_RANK_GUARD_ENABLED=True, GREEN_SNIPER_RANK_GUARD_MIN_SCORE=60.0),
    )

    decision = guard.evaluate_green_sniper_rank_guard({"rank_score": 60.1})

    assert decision.allowed is True
    assert decision.reason == "rank_ok"


def test_green_sniper_rank_guard_blocks_weak_rank(monkeypatch) -> None:
    monkeypatch.setattr(
        guard,
        "CFG",
        SimpleNamespace(GREEN_SNIPER_RANK_GUARD_ENABLED=True, GREEN_SNIPER_RANK_GUARD_MIN_SCORE=60.0),
    )

    decision = guard.evaluate_green_sniper_rank_guard({"rank_score": 42.5})

    assert decision.allowed is False
    assert decision.reason.startswith("rank_score_below_min")


def test_green_sniper_rank_guard_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        guard,
        "CFG",
        SimpleNamespace(GREEN_SNIPER_RANK_GUARD_ENABLED=False, GREEN_SNIPER_RANK_GUARD_MIN_SCORE=60.0),
    )

    decision = guard.evaluate_green_sniper_rank_guard({"rank_score": 1.0})

    assert decision.allowed is True
    assert decision.reason == "disabled"
