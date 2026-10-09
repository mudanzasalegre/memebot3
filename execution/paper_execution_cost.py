"""Original estimated PAPER assumptions, not observed/live execution fees.

Keep a detached buy-time copy through recovery and financial consumers. No
current environment lookup, historical backfill or provider authentication.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping

from execution.paper_execution_fx import _time

VERSION = "paper_original_execution_cost_v1"
ENTRY_FIELDS = ("paper_execution_cost_version", "entry_execution_cost_model", "entry_costed_at")
MODEL_FIELDS = {"version", "observed_execution", "slippage_bps", "fee_sol_per_fill"}


def _model(model):
    if (not isinstance(model, Mapping) or set(model) != MODEL_FIELDS
            or model["version"] != "estimated-v1" or model["observed_execution"] is not False):
        raise ValueError("Unknown original PAPER cost assumptions")
    for name in ("slippage_bps", "fee_sol_per_fill"):
        value = model[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("Invalid original PAPER cost number")
    if model["slippage_bps"] >= 10000:
        raise ValueError("Invalid original PAPER slippage")
    return dict(model)


def capture(model, *, at):
    return dict(paper_execution_cost_version=VERSION, entry_execution_cost_model=copy.deepcopy(_model(model)),
                entry_costed_at=_time(at).isoformat())


def validate_entry(row, *, not_before=None, not_after=None, required=False):
    if not isinstance(row, Mapping):
        raise ValueError("Unknown original PAPER cost record")
    if all(row.get(name) is None for name in ENTRY_FIELDS):
        if required:
            raise ValueError("Missing original PAPER cost basis")
        return False  # Preserve legacy records without manufacturing proof.
    if row.get("paper_execution_cost_version") != VERSION:
        raise ValueError("Unknown original PAPER cost version")
    model, at = _model(row.get("entry_execution_cost_model")), _time(row.get("entry_costed_at"))
    if (not_before is not None and at < _time(not_before)
            or not_after is not None and at > _time(not_after)):
        raise ValueError("Original PAPER cost clock conflicts")
    for name in ("opened_at", "entry_valued_at"):
        if row.get(name) is not None and _time(row[name]) != at:
            raise ValueError("Original PAPER cost belongs to another entry clock")
    if "execution_cost_model" in row and _model(row["execution_cost_model"]) != model:
        raise ValueError("PAPER cost assumptions changed after their original buy")
    return True
