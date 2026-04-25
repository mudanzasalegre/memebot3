from __future__ import annotations

from types import SimpleNamespace

import pytest

try:
    import pandas as pd
    import ml.train as ml_train
except Exception as exc:  # pragma: no cover - environment-specific dependency gate
    pytest.skip(f"pandas/ml stack unavailable: {exc}", allow_module_level=True)


def _cfg(**overrides: object) -> SimpleNamespace:
    base = {
        "ML_TRAIN_ENTRY_LANE_ALLOWLIST": "pump_early_pumpswap_profit,pump_early_pumpswap_prime",
        "ML_TRAIN_ALLOW_MISSING_ENTRY_LANE": True,
        "ML_TRAIN_DEX_ALLOWLIST": "pumpswap",
        "PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD": 5_000.0,
        "PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL": 35,
        "PUMP_EARLY_PROFIT_MIN_AGE_MIN": 3.0,
        "PUMP_EARLY_PROFIT_MAX_AGE_MIN": 30.0,
        "PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT": 10.0,
        "PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD": 25_000.0,
        "PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD": 50_000.0,
        "PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES": "0:25,50:100",
        "PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED": True,
        "PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD": 500_000.0,
        "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_PCT": 300.0,
        "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_MIN_MCAP_USD": 100_000.0,
        "PUMP_EARLY_PROFIT_DEEP_NEG_PRICE5M_PCT": -40.0,
        "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_TXNS_5M": 1_500,
        "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_VOLUME_USD_24H": 150_000.0,
        "PUMP_EARLY_PROFIT_DEAD_VOLUME_MIN_USD_24H": 15_000.0,
        "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_USD_24H": 30_000.0,
        "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_TXNS_5M": 1_000,
        "PUMP_EARLY_PROFIT_HOT_PRICE5M_MIN_PCT": 100.0,
        "PUMP_EARLY_PROFIT_HOT_PRICE5M_MAX_PCT": 180.0,
        "PUMP_EARLY_PROFIT_HOT_MCAP_MIN_USD": 50_000.0,
        "PUMP_EARLY_PROFIT_HOT_MIN_LIQUIDITY_USD": 20_000.0,
        "PUMP_EARLY_PROFIT_HOT_MIN_TXNS_5M": 600,
        "PUMP_EARLY_PROFIT_HOT_MIN_VOLUME_USD_24H": 50_000.0,
        "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H": 15_000.0,
        "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_TXNS_5M": 500,
        "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_PRICE5M_PCT": 50.0,
        "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_TXNS_5M": 350,
        "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_VOLUME_USD_24H": 100_000.0,
        "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD": 100_000.0,
        "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT": 40.0,
        "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT": 50.0,
        "PUMP_EARLY_METEOR_PRIME_ENABLED": True,
        "PUMP_EARLY_METEOR_PRIME_MIN_LIQUIDITY_USD": 4_000.0,
        "PUMP_EARLY_METEOR_PRIME_MAX_LIQUIDITY_USD": 30_000.0,
        "PUMP_EARLY_METEOR_PRIME_MIN_MARKET_CAP_USD": 5_000.0,
        "PUMP_EARLY_METEOR_PRIME_MAX_MARKET_CAP_USD": 30_000.0,
        "PUMP_EARLY_METEOR_PRIME_MIN_PRICE_PCT_5M": 110.0,
        "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_PCT_5M": 300.0,
        "PUMP_EARLY_METEOR_PRIME_MIN_TXNS_5M": 220,
        "PUMP_EARLY_METEOR_PRIME_MIN_SCORE_TOTAL": 30,
        "PUMP_EARLY_METEOR_PRIME_MIN_AGE_MIN": 3.0,
        "PUMP_EARLY_METEOR_PRIME_MAX_AGE_MIN": 18.0,
        "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_IMPACT_PCT": 12.0,
        "PUMP_EARLY_METEOR_PRIME_MIN_VOLUME_USD_24H": 8_000.0,
        "ML_MIN_DATASET_ROWS": 190,
        "ML_MIN_POSITIVES": 40,
        "ML_MIN_UNIQUE_TOKENS": 190,
        "ML_MIN_REALIZED_RETURN_ROWS": 50,
        "ML_MIN_HOLDOUT_ROWS": 30,
        "ML_MIN_HOLDOUT_POSITIVES": 10,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_productive_lane_mask_reconstructs_missing_lane_metadata(monkeypatch) -> None:
    monkeypatch.setattr(ml_train, "CFG", _cfg())
    frame = pd.DataFrame(
        [
            {
                "entry_regime": "pump_early",
                "dex_id": "pumpswap",
                "has_jupiter_route": 1,
                "liquidity_usd": 10_000.0,
                "score_total": 42,
                "age_minutes": 8.0,
                "price_impact_pct": 4.0,
                "market_cap_usd": 20_000.0,
                "price_pct_5m": 120.0,
                "txns_last_5m": 700,
                "volume_24h_usd": 80_000.0,
                "liquidity_is_proxy": 0,
                "entry_lane": None,
            },
            {
                "entry_regime": "pump_early",
                "dex_id": "pumpfun",
                "has_jupiter_route": 1,
                "liquidity_usd": 10_000.0,
                "score_total": 42,
                "age_minutes": 8.0,
                "price_impact_pct": 4.0,
                "market_cap_usd": 20_000.0,
                "price_pct_5m": 120.0,
                "txns_last_5m": 700,
                "volume_24h_usd": 80_000.0,
                "liquidity_is_proxy": 0,
                "entry_lane": None,
            },
        ]
    )

    mask, meta = ml_train._productive_lane_mask(frame)

    assert mask.tolist() == [True, False]
    assert meta["rows_missing_lane_metadata"] == 2
    assert meta["rows_missing_lane_metadata_reconstructed"] == 1
    assert meta["fallback_rows"] == 1


def test_quality_readiness_reports_next_model_deficits() -> None:
    quality = ml_train.DatasetQuality(
        passed=False,
        reasons=["rows<190", "unique_tokens<190"],
        source_rows=220,
        source_positives=70,
        source_unique_tokens=200,
        rows=184,
        positives=52,
        unique_tokens=184,
        outcome_rows=184,
        legacy_outcome_rows=0,
        policy_reject_rows=0,
        realized_return_rows=184,
        numeric_feature_candidates=20,
        non_constant_numeric_features=12,
        holdout_rows=28,
        holdout_positives=9,
        holdout_unique_tokens=28,
        sample_type_counts={"trade_close": 184},
    )

    readiness = ml_train._quality_readiness(quality)

    assert readiness["rows_to_next_model"] == 6
    assert readiness["unique_tokens_to_next_model"] == 6
    assert readiness["holdout_rows_to_next_model"] == 2
    assert readiness["holdout_positives_to_next_model"] == 1
    assert readiness["skip_reasons"] == ["rows<190", "unique_tokens<190"]
    assert readiness["blocker"] == "rows<190,unique_tokens<190"
