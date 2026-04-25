from __future__ import annotations

from trade_pnl import apply_partial_fill, summarize_trade, total_pnl_pct_from_record


def test_closed_snapshot_without_partials_uses_close_price_not_zero() -> None:
    totals = summarize_trade(
        entry_qty=25_000_000,
        remaining_qty=0,
        buy_price_usd=3.548617865682351e-05,
        entry_notional_usd=2.08325,
        realized_qty=0,
        realized_proceeds_usd=0.0,
        close_price_usd=2.822781651147922e-05,
    )

    assert round(totals.total_pnl_pct, 4) == -20.4541
    assert round(totals.total_pnl_usd, 6) == -0.426109


def test_closed_snapshot_after_partial_prices_remaining_leg() -> None:
    partial = apply_partial_fill(
        entry_qty=100,
        remaining_qty=100,
        buy_price_usd=1.0,
        entry_notional_usd=10.0,
        realized_qty=0,
        realized_proceeds_usd=0.0,
        qty_sold=40,
        fill_price_usd=1.5,
    )

    totals = summarize_trade(
        entry_qty=partial.entry_qty,
        remaining_qty=0,
        buy_price_usd=1.0,
        entry_notional_usd=10.0,
        realized_qty=partial.realized_qty,
        realized_proceeds_usd=partial.realized_proceeds_usd,
        close_price_usd=0.8,
    )

    assert totals.realized_qty == 40
    assert round(totals.total_pnl_pct, 4) == 8.0
    assert round(totals.total_pnl_usd, 6) == 0.8


def test_total_pnl_pct_from_record_recomputes_stale_direct_value() -> None:
    record = {
        "entry_qty": 25_000_000,
        "qty_lamports": 0,
        "buy_price_usd": 3.548617865682351e-05,
        "entry_notional_usd": 2.08325,
        "realized_qty": 0,
        "realized_proceeds_usd": 0.0,
        "close_price_usd": 2.822781651147922e-05,
        "total_pnl_pct": -100.0,
    }

    pnl_pct = total_pnl_pct_from_record(record)
    assert round(pnl_pct, 4) == -20.4541
