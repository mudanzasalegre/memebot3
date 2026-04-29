from __future__ import annotations

from ml.data_contract import (
    SAMPLE_EXECUTION_BLOCKED_NO_ROUTE,
    SAMPLE_EXECUTION_BLOCKED_ZERO_QTY,
    SAMPLE_GREEN_SNIPER_REJECT_SHADOW,
    normalize_sample_type,
)


def test_execution_blocked_sample_types_are_separate() -> None:
    assert normalize_sample_type("execution_blocked_no_route") == SAMPLE_EXECUTION_BLOCKED_NO_ROUTE
    assert normalize_sample_type("execution_blocked_zero_qty") == SAMPLE_EXECUTION_BLOCKED_ZERO_QTY
    assert normalize_sample_type("green_sniper_reject_shadow") == SAMPLE_GREEN_SNIPER_REJECT_SHADOW
