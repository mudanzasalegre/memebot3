from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _read(container: Any, key: str, default: Any = None) -> Any:
    if isinstance(container, Mapping):
        return container.get(key, default)
    return getattr(container, key, default)


def _resolve_entry_notional_usd(
    entry_notional_usd: Any,
    *,
    entry_qty: int,
    buy_price_usd: float,
) -> float | None:
    explicit = _to_float(entry_notional_usd, 0.0)
    if explicit > 0.0:
        return float(explicit)
    if entry_qty > 0 and buy_price_usd > 0.0:
        return float(entry_qty) * float(buy_price_usd)
    return None


def resolve_take_profit_and_win_pct(
    *,
    take_profit_pct_raw: Optional[str],
    win_pct_raw: Optional[str],
    default_take_profit_pct: float = 35.0,
) -> Tuple[float, float]:
    if take_profit_pct_raw and win_pct_raw:
        tp = _to_float(str(take_profit_pct_raw).split()[0], default_take_profit_pct)
        win = _to_float(str(win_pct_raw).split()[0], default_take_profit_pct / 100.0)
        return float(tp), float(win)

    if take_profit_pct_raw:
        tp = _to_float(str(take_profit_pct_raw).split()[0], default_take_profit_pct)
        return float(tp), float(tp) / 100.0

    if win_pct_raw:
        win = _to_float(str(win_pct_raw).split()[0], default_take_profit_pct / 100.0)
        return float(win) * 100.0, float(win)

    return float(default_take_profit_pct), float(default_take_profit_pct) / 100.0


def resolve_entry_qty(
    entry_qty: Any,
    remaining_qty: Any,
    realized_qty: Any = 0,
) -> int:
    entry_n = _to_int(entry_qty, 0)
    remaining_n = max(0, _to_int(remaining_qty, 0))
    realized_n = max(0, _to_int(realized_qty, 0))

    if entry_n > 0:
        return entry_n

    inferred = remaining_n + realized_n
    if inferred > 0:
        return inferred
    return remaining_n


@dataclass(frozen=True)
class TradeTotals:
    entry_qty: int
    realized_qty: int
    remaining_qty: int
    realized_proceeds_usd: float
    realized_cost_usd: float
    realized_pnl_usd: float
    unrealized_proceeds_usd: float
    unrealized_cost_usd: float
    unrealized_pnl_usd: float
    total_proceeds_usd: float
    total_cost_usd: float
    total_pnl_usd: float
    total_pnl_pct: float
    effective_exit_price_usd: Optional[float]


def summarize_trade(
    *,
    entry_qty: Any,
    remaining_qty: Any,
    buy_price_usd: Any,
    entry_notional_usd: Any = None,
    realized_qty: Any = 0,
    realized_proceeds_usd: Any = 0.0,
    close_price_usd: Any = None,
) -> TradeTotals:
    entry_n = resolve_entry_qty(entry_qty, remaining_qty, realized_qty)
    remaining_n = max(0, _to_int(remaining_qty, 0))
    realized_n = max(0, min(entry_n, _to_int(realized_qty, 0)))

    buy_px = _to_float(buy_price_usd, 0.0)
    realized_proceeds = _to_float(realized_proceeds_usd, 0.0)
    close_px = _to_float(close_price_usd, 0.0)
    entry_notional = _resolve_entry_notional_usd(
        entry_notional_usd,
        entry_qty=entry_n,
        buy_price_usd=buy_px,
    )
    unit_cost = (float(entry_notional) / float(entry_n)) if entry_notional and entry_n > 0 else buy_px
    unresolved_n = max(0, entry_n - realized_n)
    remaining_n = min(remaining_n, unresolved_n)
    priced_qty = remaining_n
    # Closed records and backfills often persist qty=0 after the sell, but still carry
    # close_price_usd. In that case, the unresolved portion of the trade must be priced
    # at close_px rather than treated as a total loss.
    if close_px > 0.0 and priced_qty <= 0 and unresolved_n > 0:
        priced_qty = unresolved_n

    if entry_notional and entry_n > 0:
        realized_cost = float(entry_notional) * (float(realized_n) / float(entry_n))
    else:
        realized_cost = float(realized_n) * float(unit_cost)
    realized_pnl = realized_proceeds - realized_cost

    if entry_notional and entry_n > 0:
        unrealized_cost = float(entry_notional) * (float(priced_qty) / float(entry_n))
        if priced_qty > 0 and close_px > 0 and buy_px > 0:
            unrealized_proceeds = (
                float(entry_notional)
                * (float(priced_qty) / float(entry_n))
                * (float(close_px) / float(buy_px))
            )
        else:
            unrealized_proceeds = 0.0
    else:
        unrealized_proceeds = float(priced_qty) * close_px if priced_qty > 0 and close_px > 0 else 0.0
        unrealized_cost = float(priced_qty) * float(unit_cost)
    unrealized_pnl = unrealized_proceeds - unrealized_cost

    total_proceeds = realized_proceeds + unrealized_proceeds
    total_cost = float(entry_notional) if entry_notional is not None else (float(entry_n) * buy_px)
    total_pnl = total_proceeds - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100.0) if total_cost > 0 else 0.0
    if entry_notional and buy_px > 0 and total_proceeds > 0:
        effective_exit = float(buy_px) * (float(total_proceeds) / float(entry_notional))
    else:
        effective_exit = (total_proceeds / float(entry_n)) if entry_n > 0 and total_proceeds > 0 else None

    return TradeTotals(
        entry_qty=entry_n,
        realized_qty=realized_n,
        remaining_qty=remaining_n,
        realized_proceeds_usd=realized_proceeds,
        realized_cost_usd=realized_cost,
        realized_pnl_usd=realized_pnl,
        unrealized_proceeds_usd=unrealized_proceeds,
        unrealized_cost_usd=unrealized_cost,
        unrealized_pnl_usd=unrealized_pnl,
        total_proceeds_usd=total_proceeds,
        total_cost_usd=total_cost,
        total_pnl_usd=total_pnl,
        total_pnl_pct=total_pnl_pct,
        effective_exit_price_usd=effective_exit,
    )


def apply_partial_fill(
    *,
    entry_qty: Any,
    remaining_qty: Any,
    buy_price_usd: Any,
    entry_notional_usd: Any = None,
    realized_qty: Any = 0,
    realized_proceeds_usd: Any = 0.0,
    qty_sold: Any,
    fill_price_usd: Any,
) -> TradeTotals:
    remaining_n = max(0, _to_int(remaining_qty, 0))
    sold_n = max(0, min(remaining_n, _to_int(qty_sold, 0)))
    new_remaining = max(0, remaining_n - sold_n)
    new_realized_qty = max(0, _to_int(realized_qty, 0)) + sold_n
    realized_proceeds = _to_float(realized_proceeds_usd, 0.0)
    entry_n = resolve_entry_qty(entry_qty, remaining_qty, realized_qty)
    buy_px = _to_float(buy_price_usd, 0.0)
    entry_notional = _resolve_entry_notional_usd(
        entry_notional_usd,
        entry_qty=entry_n,
        buy_price_usd=buy_px,
    )
    if entry_notional and entry_n > 0 and buy_px > 0:
        sold_proceeds = (
            float(entry_notional)
            * (float(sold_n) / float(entry_n))
            * (_to_float(fill_price_usd, 0.0) / float(buy_px))
        )
    else:
        sold_proceeds = float(sold_n) * _to_float(fill_price_usd, 0.0)
    new_realized_proceeds = realized_proceeds + sold_proceeds

    return summarize_trade(
        entry_qty=entry_qty,
        remaining_qty=new_remaining,
        buy_price_usd=buy_price_usd,
        entry_notional_usd=entry_notional_usd,
        realized_qty=new_realized_qty,
        realized_proceeds_usd=new_realized_proceeds,
        close_price_usd=None,
    )


def total_pnl_pct_from_record(record: Any, *, close_price_usd: Any = None) -> float:
    entry_qty = _read(record, "entry_qty", _read(record, "entry_qty_lamports", None))
    remaining_qty = _read(record, "qty", _read(record, "qty_lamports", 0))
    buy_price = _read(record, "buy_price_usd", 0.0)
    entry_notional = _read(record, "entry_notional_usd", None)
    realized_qty = _read(record, "realized_qty", _read(record, "realized_qty_lamports", 0))
    realized_proceeds = _read(record, "realized_proceeds_usd", 0.0)
    close_px = close_price_usd if close_price_usd is not None else _read(record, "close_price_usd", None)

    totals = summarize_trade(
        entry_qty=entry_qty,
        remaining_qty=remaining_qty,
        buy_price_usd=buy_price,
        entry_notional_usd=entry_notional,
        realized_qty=realized_qty,
        realized_proceeds_usd=realized_proceeds,
        close_price_usd=close_px,
    )
    if totals.total_cost_usd > 0.0:
        return float(totals.total_pnl_pct)

    direct = _read(record, "total_pnl_pct", None)
    if direct is not None:
        try:
            return float(direct)
        except Exception:
            pass
    return 0.0


def total_pnl_ratio_from_record(record: Any, *, close_price_usd: Any = None) -> float:
    return total_pnl_pct_from_record(record, close_price_usd=close_price_usd) / 100.0
