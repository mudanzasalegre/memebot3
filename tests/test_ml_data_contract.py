from __future__ import annotations

import pandas as pd

from ml.data_contract import (
    is_policy_reject,
    is_shadow_sample,
    is_live_trade_sample,
    normalize_dex_id,
    normalize_entry_lane,
    normalize_sample_type,
    reconstruct_entry_lane,
)
from ml.lane_taxonomy import LANE_PUMP_EARLY_PROFIT, LANE_RESEARCH_SNIPER, LANE_UNKNOWN


def test_lane_known_and_legacy() -> None:
    assert normalize_entry_lane("pump_early_pumpswap_profit") == LANE_PUMP_EARLY_PROFIT
    assert normalize_entry_lane("pumpswap_breakout_probe") == "pump_early_pumpswap_breakout_probe"
    assert normalize_entry_lane("pump_early_sniper") == LANE_RESEARCH_SNIPER


def test_lane_empty_is_unknown() -> None:
    assert normalize_entry_lane("") == LANE_UNKNOWN
    assert reconstruct_entry_lane({}) == LANE_UNKNOWN


def test_reconstruct_lane_from_gate_profile() -> None:
    assert reconstruct_entry_lane({"gate_profile": "pumpswap_profit_prime"}) == "pump_early_pumpswap_prime"
    assert reconstruct_entry_lane({"gate_profile": "pumpswap_breakout_probe"}) == "pump_early_pumpswap_breakout_probe"


def test_sample_type_invalid_and_predicates() -> None:
    assert normalize_sample_type("bad-value") == "unknown"
    assert is_live_trade_sample({"sample_type": "trade_close"})
    assert is_shadow_sample({"sample_type": "shadow"})
    assert is_policy_reject({"sample_type": "reject"})


def test_dex_id_variants() -> None:
    assert normalize_dex_id("pump-swap") == "pumpswap"
    assert normalize_dex_id("pump_swap") == "pumpswap"
    assert normalize_dex_id("pumpswap") == "pumpswap"


def test_apply_contract_keeps_dataframe_shape() -> None:
    from ml.data_contract import apply_data_contract

    frame = pd.DataFrame([{"address": "a", "sample_type": "trade_close"}])
    out = apply_data_contract(frame)
    assert len(out) == 1
    assert out.loc[0, "entry_lane"] == LANE_UNKNOWN
