from __future__ import annotations

import asyncio
import datetime as dt
import json
import pytest

import analytics.exit_policy as exit_policy
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


def test_plan_next_state_records_executed_ladder_steps() -> None:
    plan = _plan(100)
    state = plan["next_state"]

    assert plan["pending_step_count"] == 3
    assert state["executed_steps"] == ["tp1", "tp2", "tp3"]
    assert state["sold_fraction"] == 0.70
    assert state["last_pending_step_count"] == 3


def test_papertrading_partial_executes_ladder_fraction_and_persists_state(monkeypatch) -> None:
    import trader.papertrading as papertrading

    address = "So11111111111111111111111111111111111111112"
    portfolio = {
        address: {
            "qty_lamports": 1000,
            "entry_qty": 1000,
            "buy_price_usd": 1.0,
            "peak_price": 1.0,
            "opened_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "closed": False,
            "dry_run": True,
            "token_address": address,
            "partial_taken": False,
            "partial_count": 0,
            "realized_qty": 0,
            "realized_proceeds_usd": 0.0,
            "realized_cost_usd": 0.0,
            "realized_pnl_usd": 0.0,
            "entry_notional_usd": 1000.0,
        }
    }

    async def fake_price(*_args, **_kwargs) -> float:
        return 2.0

    async def fake_close_price(*_args, **_kwargs) -> tuple[float, str]:
        return 2.0, "test"

    monkeypatch.setattr(papertrading, "_PORTFOLIO", portfolio)
    monkeypatch.setattr(papertrading, "_save", lambda: None)
    monkeypatch.setattr(papertrading.price_service, "get_price_usd", fake_price)
    monkeypatch.setattr(papertrading, "_resolve_close_price_usd", fake_close_price)

    assert asyncio.run(papertrading.check_exit_conditions(address)) is True

    entry = portfolio[address]
    state = json.loads(entry["partial_ladder_state"])
    assert entry["qty_lamports"] == 300
    assert entry["realized_qty"] == 700
    assert entry["partial_count"] == 3
    assert entry["exit_state"] == "post_partial"
    assert state["executed_steps"] == ["tp1", "tp2", "tp3"]


def test_seller_partial_helper_uses_ladder_fraction_and_persists_state(monkeypatch) -> None:
    import trader.seller as seller

    captured: dict[str, int] = {}
    position = {
        "address": "So11111111111111111111111111111111111111112",
        "qty_lamports": 1000,
        "entry_qty": 1000,
        "buy_price_usd": 1.0,
        "partial_taken": False,
        "partial_count": 0,
        "realized_qty": 0,
        "dry_run": True,
    }

    async def fake_sell(_token_addr: str, qty: int, **_kwargs) -> dict[str, object]:
        captured["qty"] = qty
        return {"ok": True, "qty_sold": qty}

    monkeypatch.setattr(seller, "sell", fake_sell)

    result = asyncio.run(
        seller.apply_partial_tp(
            position,
            price_hint=2.0,
            price_source_hint="test",
            liquidity_usd=None,
        )
    )

    state = json.loads(position["partial_ladder_state"])
    assert result is not None
    assert captured["qty"] == 700
    assert position["qty_lamports"] == 300
    assert position["realized_qty"] == 700
    assert position["partial_count"] == 3
    assert position["exit_state"] == "post_partial"
    assert state["executed_steps"] == ["tp1", "tp2", "tp3"]


def test_moonshot_keeps_runner_after_tp1_partial() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = {
        "entry_regime": "pump_early",
        "entry_lane": "pump_early_moonshot_micro_lottery",
        "gate_profile": "moonshot_micro_lottery",
        "opened_at": now - dt.timedelta(minutes=5),
        "buy_price_usd": 1.0,
        "entry_qty": 1000,
        "qty": 800,
        "realized_qty": 200,
        "realized_proceeds_usd": 400.0,
        "partial_taken": True,
        "partial_count": 1,
        "highest_pnl_pct": 100.0,
        "dry_run": True,
    }

    assert exit_policy.should_exit(dict(subject), price_now=1.95, now=now, pnl_pct=95.0) is None
    assert exit_policy.should_exit(dict(subject), price_now=1.85, now=now, pnl_pct=85.0) is None
    assert exit_policy.runner_giveback_emergency_reason(subject, pnl_pct=70.0, peak=100.0) is None
    assert exit_policy.dynamic_runner_floor_pct(subject, peak=100.0) is None


@pytest.mark.parametrize("pnl_pct", [200.0, 500.0, 1000.0, 5000.0, 10000.0, 100000.0])
def test_extreme_upward_runner_is_not_closed_by_a_profit_ceiling(pnl_pct: float) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    subject = {
        "entry_regime": "pump_early", "entry_lane": "pump_early_moonshot_micro_lottery",
        "gate_profile": "moonshot_micro_lottery", "opened_at": now - dt.timedelta(minutes=1),
        "buy_price_usd": 1.0, "entry_qty": 1000, "qty": 250,
        "realized_qty": 750, "realized_proceeds_usd": 1500.0,
        "partial_taken": True, "partial_count": 3, "highest_pnl_pct": pnl_pct,
        "dry_run": True,
    }
    assert exit_policy.should_exit(subject, price_now=1 + pnl_pct / 100, now=now, pnl_pct=pnl_pct) is None
    assert subject["qty"] == 250


def test_extreme_partial_ladder_keeps_tail_and_does_not_repeat_sales() -> None:
    from analytics.runner_ladder import RunnerLadderStep
    steps = (RunnerLadderStep("tp1", 100, 0.25), RunnerLadderStep("tp2", 500, 0.25),
             RunnerLadderStep("tp3", 1000, 0.25))
    plan = plan_ladder_partials(pnl_pct=100000, entry_qty=1000, remaining_qty=1000,
                               realized_qty=0, steps=steps, moonbag_fraction=0.25)
    assert plan["target_secured_fraction"] == 0.75
    repeated = plan_ladder_partials(pnl_pct=200000, entry_qty=1000, remaining_qty=250,
                                   realized_qty=750, steps=steps, moonbag_fraction=0.25,
                                   state=plan["next_state"])
    assert repeated["pending_step_count"] == 0 and repeated["sell_fraction_of_remaining"] == 0
