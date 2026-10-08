"""Original PAPER execution FX, checked at its event clock, never backfilled.

These receipts certify the recorded conversion, not market depth or live fills.
Legacy records without the version remain explicitly unproved and unchanged.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import fields
from typing import Mapping

from utils.sol_price import SolUsdObservation, fresh_sol_usd

VERSION = "paper_original_execution_fx_v1"
ENTRY_FIELDS = ("paper_execution_fx_version", "entry_fx_observation", "entry_valued_at")
EXIT_FIELDS = ("paper_execution_fx_version", "fill_fx_observation", "quote_sol_usd")


def _time(value):
    if not isinstance(value, (dt.datetime, str)):
        raise ValueError("Original FX event clock is missing")
    stamp = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Original FX event needs an explicit timezone")
    return stamp.astimezone(dt.timezone.utc)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("Original money is missing or untyped")
    return value


def _rate(value, at):
    if not isinstance(value, Mapping) or set(value) != {f.name for f in fields(SolUsdObservation)}:
        raise ValueError("Original FX receipt fields differ")
    rate = fresh_sol_usd(SolUsdObservation(**value), now=_time(at).timestamp())
    if rate is None:
        raise ValueError("Original FX was unavailable at the execution clock")
    return rate


def _declared(row, names):
    if all(row.get(name) is None for name in names):
        return False
    if row.get("paper_execution_fx_version") != VERSION:
        raise ValueError("Unknown original PAPER FX version")
    return True


def validate_entry(row, *, amount_sol, not_before=None, not_after=None):
    if not _declared(row, ENTRY_FIELDS):
        return False
    stamp = _time(row["entry_valued_at"])
    if (not_before is not None and stamp < _time(not_before)
            or not_after is not None and stamp > _time(not_after)):
        raise ValueError("Original entry FX clock conflicts with its buy")
    rate = _rate(row["entry_fx_observation"], stamp)
    notional = _number(amount_sol) * rate
    if not math.isfinite(notional) or _number(row["entry_notional_usd"]) != notional:
        raise ValueError("Entry notional differs from its original FX conversion")
    return True


def validate_exit(row):
    # Old exits already stored the scalar quote_sol_usd. It is not a receipt.
    if not _declared(row, EXIT_FIELDS[:2]):
        return False
    rate = _rate(row["fill_fx_observation"], row["filled_at"])
    if _number(row["quote_sol_usd"]) != rate:
        raise ValueError("Sell scalar differs from its original FX receipt")
    return True
