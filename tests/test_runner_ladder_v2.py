from __future__ import annotations

from analytics.runner_ladder import plan_ladder_partials


def _plan(peak: float, *, state: dict | None = None) -> dict:
    return plan_ladder_partials(
        pnl_pct=peak,
        entry_qty=1000,
        remaining_qty=1000,
        realized_qty=0,
        state=state,
    )


def test_peak_25_executes_tp1() -> None:
    plan = _plan(25)

    assert plan["pending_step_count"] == 1
    assert [step["step_id"] for step in plan["pending_steps"]] == ["tp1"]


def test_peak_100_executes_tp1_tp2_tp3() -> None:
    plan = _plan(100)

    assert plan["pending_step_count"] == 3
    assert [step["step_id"] for step in plan["pending_steps"]] == ["tp1", "tp2", "tp3"]


def test_peak_300_executes_tp1_to_tp4() -> None:
    plan = _plan(300)

    assert plan["pending_step_count"] == 4
    assert [step["step_id"] for step in plan["pending_steps"]] == ["tp1", "tp2", "tp3", "tp4"]


def test_peak_1000_executes_tp1_to_tp6() -> None:
    plan = _plan(1000)

    assert plan["pending_step_count"] == 6
    assert [step["step_id"] for step in plan["pending_steps"]] == ["tp1", "tp2", "tp3", "tp4", "tp5", "tp6"]


def test_peak_2787_does_not_stay_partial_count_one() -> None:
    plan = _plan(2787)

    assert plan["pending_step_count"] == 6


def test_does_not_duplicate_partials() -> None:
    first = _plan(1000)
    second = _plan(1000, state=first["next_state"])

    assert second["pending_step_count"] == 0
    assert second["sell_fraction_of_remaining"] == 0.0


def test_does_not_sell_more_than_100_percent() -> None:
    plan = _plan(2787)

    assert plan["target_secured_fraction"] <= 0.97
    assert plan["sell_fraction_of_remaining"] <= 1.0
