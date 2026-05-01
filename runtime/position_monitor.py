from __future__ import annotations

import datetime as dt
from typing import Any

from analytics.exit_policy import should_exit


def evaluate_position_exit(
    position: Any,
    *,
    price_now: float | None,
    now: dt.datetime | None = None,
    liq_now: float | None = None,
    pnl_pct: float | None = None,
) -> str | None:
    return should_exit(position, price_now, now or dt.datetime.now(dt.timezone.utc), liq_now=liq_now, pnl_pct=pnl_pct)


__all__ = ["evaluate_position_exit"]
