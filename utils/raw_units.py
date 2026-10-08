"""Exact transport units shared by quotes, capital checks and submissions."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from typing import Any

U64_MAX = (1 << 64) - 1


def raw_uint(value: Any, maximum: int = U64_MAX) -> int | None:
    if type(value) is int:
        return value if 0 <= value <= maximum else None
    if (isinstance(value, str) and 0 < len(value) <= 20
            and value.isascii() and value.isdecimal()):
        number = int(value)
        return number if number <= maximum else None
    return None


def sol_to_lamports(value: Any, *, allow_zero: bool = False) -> int | None:
    """Floor at the raw-unit boundary, never multiply binary floats."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            sol = Decimal(str(value))
            if not sol.is_finite() or not 0 <= sol <= Decimal(U64_MAX) / 1_000_000_000:
                return None
            if 0 < sol < Decimal("0.000000001"):
                return None
            units = int((sol * 1_000_000_000).to_integral_value(rounding=ROUND_DOWN))
            return units if units > 0 or allow_zero and sol == 0 else None
    except (InvalidOperation, ValueError, OverflowError):
        return None
