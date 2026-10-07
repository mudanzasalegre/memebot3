"""Frozen, paper-only tail protection measured as drawdown of the peak price.

Profit percentage points are not price drawdown percentages.  At +5,000%,
a 20% decline in price corresponds to +3,980%, not +4,980%.
This policy never supplies a price, quantity, entry signal or execution proof.
"""
from __future__ import annotations

import json
import math
from typing import Any

VERSION = "paper_runner_price_drawdown_v1"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def freeze_policy(cfg: Any, *, dry_run: bool) -> str:
    """Capture settings at entry; subsequent learning must not retune open trades."""
    enabled = dry_run is True and getattr(cfg, "RUNNER_PRICE_TRAILING_PAPER_ENABLED", False) is True
    payload = {
        "version": VERSION,
        "role": "paper_exit_only",
        "enabled": enabled,
        "activation_peak_pct": getattr(cfg, "RUNNER_PRICE_TRAILING_MIN_PEAK_PCT", 300.0),
        "max_price_drawdown_pct": getattr(cfg, "RUNNER_PRICE_TRAILING_DRAWDOWN_PCT", 20.0),
        "max_holding_h": getattr(cfg, "RUNNER_PRICE_TRAILING_MAX_HOLD_H", 24.0),
    }
    # Invalid configuration is disabled, not silently clipped to a different policy.
    if not enabled or parse_policy(payload) is None:
        payload = {"version": VERSION, "role": "paper_exit_only", "enabled": False}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_policy(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, dict) or value.get("version") != VERSION:
        return None
    if value.get("role") != "paper_exit_only" or value.get("enabled") is not True:
        return None
    peak = _number(value.get("activation_peak_pct"))
    drawdown = _number(value.get("max_price_drawdown_pct"))
    hours = _number(value.get("max_holding_h"))
    if peak is None or drawdown is None or hours is None:
        return None
    if not (100.0 <= peak <= 2000.0 and 5.0 <= drawdown <= 40.0 and 1.0 <= hours <= 24.0):
        return None
    return {"version": VERSION, "role": "paper_exit_only", "enabled": True,
            "activation_peak_pct": peak, "max_price_drawdown_pct": drawdown, "max_holding_h": hours}


def price_drawdown_floor_pct(peak_pct: Any, drawdown_pct: Any) -> float | None:
    peak, drawdown = _number(peak_pct), _number(drawdown_pct)
    if peak is None or peak < 0.0 or drawdown is None or not 0.0 < drawdown < 100.0:
        return None
    floor = (100.0 + peak) * (1.0 - drawdown / 100.0) - 100.0
    return floor if math.isfinite(floor) else None
