from __future__ import annotations

import glob
import hashlib
import json
import os
import pathlib
import tempfile
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional, Sequence

from utils.venv_bootstrap import ensure_project_venv

ensure_project_venv(__file__, module_name=__spec__.name if __spec__ else None)

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from config.config import CFG
from ml.data_contract import (
    apply_data_contract,
    normalize_dex_id as contract_normalize_dex_id,
    normalize_entry_regime as contract_normalize_entry_regime,
    normalize_sample_type,
)
from ml.feature_matrix import coerce_feature_frame
from ml.model_registry import promote_candidate, write_candidate
from ml.segment_report import SEGMENT_JSON, build_segment_report, write_segment_outputs
from ml.tune_threshold import tune_from_frame

DATA_DIR: pathlib.Path = CFG.FEATURES_DIR
MODEL_PATH: pathlib.Path = CFG.MODEL_PATH
META_PATH: pathlib.Path = MODEL_PATH.with_suffix(".meta.json")
METRICS_DIR: pathlib.Path = DATA_DIR.parent / "metrics"
VAL_PREDS_CSV: pathlib.Path = METRICS_DIR / "val_preds.csv"
RECOMMENDED_JSON: pathlib.Path = METRICS_DIR / "recommended_threshold.json"
DATASET_QUALITY_JSON: pathlib.Path = METRICS_DIR / "dataset_quality.json"
TRAIN_STATUS_JSON: pathlib.Path = METRICS_DIR / "train_status.json"

HOLDOUT_DAYS: Optional[int] = getattr(CFG, "TRAIN_FORWARD_HOLDOUT_DAYS", None)
HOLDOUT_PCT: Optional[float] = getattr(CFG, "TRAIN_FORWARD_HOLDOUT_PCT", None)
TRAIN_WINDOW_DAYS: Optional[int] = getattr(CFG, "TRAINING_WINDOW_DAYS", None)
PREC_AT_K_PCT: float = float(getattr(CFG, "PRECISION_AT_K_PCT", 0.10))
RETURN_COL_CANDIDATES = ("target_total_pnl_pct", "total_pnl_pct", "pnl_pct")
OUTCOME_SAMPLE_TYPES = (
    "trade_close",
    "shadow_close",
    "green_sniper_reject_shadow",
    "late_momentum_watch_shadow",
    "research_rank_shadow",
)

_FORBIDDEN_SUBSTR = (
    "pnl",
    "future",
    "close_price",
    "_at_close",
    "_after_",
    "outcome",
    "result",
    "exit",
    "tp_",
    "sl_",
)
_META_COLS = (
    "label",
    "timestamp",
    "ts",
    "created_at",
    "listed_at",
    "address",
    "token_address",
    "pair_address",
    "symbol",
    "name",
    "discovered_via",
    "entry_regime",
    "entry_lane",
    "gate_profile",
    "profit_lane_tier",
    "dex_id",
    "dexId",
    "buy_dex_id",
    "price_source",
    "sample_type",
    "mint",
)


@dataclass
class DatasetQuality:
    passed: bool
    reasons: list[str]
    source_rows: int
    source_positives: int
    source_unique_tokens: int
    rows: int
    positives: int
    unique_tokens: int
    outcome_rows: int
    legacy_outcome_rows: int
    policy_reject_rows: int
    realized_return_rows: int
    numeric_feature_candidates: int
    non_constant_numeric_features: int
    holdout_rows: int
    holdout_positives: int
    holdout_unique_tokens: int
    sample_type_counts: dict[str, int]


@dataclass
class TrainResult:
    trained: bool
    status: str
    dataset_quality: DatasetQuality
    selection_metric: str | None
    selection_score: float | None
    model_path: str | None = None
    meta_path: str | None = None
    val_preds_path: str | None = None
    recommended_threshold_path: str | None = None


@dataclass
class CandidateResult:
    name: str
    model_family: str
    tune_result: dict[str, Any]
    auc_mean: float
    ap_mean: float
    precision_at_k: float
    val_preds: pd.DataFrame
    feature_signal: list[dict[str, float]]

    @property
    def selection_metric(self) -> str | None:
        raw = self.tune_result.get("selection_metric")
        return str(raw) if isinstance(raw, str) and raw else None

    @property
    def selection_score(self) -> float | None:
        raw = self.tune_result.get("selection_score")
        if isinstance(raw, (int, float)) and np.isfinite(float(raw)):
            return float(raw)
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "model_family": self.model_family,
            "selection_metric": self.selection_metric,
            "selection_score": self.selection_score,
            "auc_forward_or_cv_mean": self.auc_mean,
            "auc_pr_forward_or_cv_mean": self.ap_mean,
            "precision_at_k_pct": float(PREC_AT_K_PCT),
            "precision_at_k_val": self.precision_at_k,
            "threshold_result": _json_safe(self.tune_result),
            "feature_signal_top": self.feature_signal[:15],
        }


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if np.isnan(value) or np.isinf(value):
            return None
        return float(value)
    if isinstance(value, (np.floating,)):
        value_f = float(value)
        if np.isnan(value_f) or np.isinf(value_f):
            return None
        return value_f
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(payload), indent=2), encoding="utf-8")


def _coerce_timestamp(df: pd.DataFrame) -> pd.DataFrame:
    for cand in ("timestamp", "ts", "created_at", "listed_at"):
        if cand in df.columns:
            df["timestamp"] = pd.to_datetime(df[cand], utc=True, errors="coerce")
            break
    else:
        df["timestamp"] = pd.to_datetime("now", utc=True)
    return df


def _load_one(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
    return _coerce_timestamp(df)


def _load_dataset() -> pd.DataFrame:
    parquet_files = sorted(glob.glob(str(DATA_DIR / "features_*.parquet")))
    csv_files = sorted(glob.glob(str(DATA_DIR / "features_*.csv")))
    files = parquet_files if parquet_files else csv_files
    if not files:
        raise FileNotFoundError(f"No se encontro features_*.parquet/csv en {DATA_DIR}")

    df = pd.concat([_load_one(f) for f in files], ignore_index=True)

    for col in (
        "cluster_bad",
        "mint_auth_renounced",
        "social_ok",
        "is_incomplete",
        "has_jupiter_route",
        "require_jupiter_for_buy",
    ):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "label" not in df.columns:
        raise ValueError("El dataset no contiene la columna 'label'")
    df = df.dropna(subset=["label"]).copy()
    df["label"] = pd.to_numeric(df["label"], errors="coerce")
    df = df.dropna(subset=["label"]).copy()
    df["label"] = df["label"].astype(int)

    if "mint" not in df.columns:
        if "address" in df.columns:
            df["mint"] = df["address"]
        else:
            df["mint"] = df.get("token_address", pd.Series(index=df.index, dtype="object"))
    df["mint"] = df["mint"].astype("string")
    df = apply_data_contract(df)
    df["mint"] = df["mint"].astype("string")
    return df


def _apply_training_window(df: pd.DataFrame) -> pd.DataFrame:
    if TRAIN_WINDOW_DAYS and "timestamp" in df.columns:
        tmax = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").max()
        cutoff = tmax - pd.Timedelta(days=int(TRAIN_WINDOW_DAYS))
        df = df[df["timestamp"] >= cutoff].copy()
        print(f"[WIN] Ventana de entrenamiento aplicada: ultimos {TRAIN_WINDOW_DAYS} dias (cutoff={cutoff})")
    return df


def _resolve_return_col(df: pd.DataFrame) -> str | None:
    for col in RETURN_COL_CANDIDATES:
        if col in df.columns:
            return col
    return None


def _sample_type_series(df: pd.DataFrame) -> pd.Series:
    if "sample_type" not in df.columns:
        return pd.Series(pd.NA, index=df.index, dtype="string")
    return df["sample_type"].map(normalize_sample_type).astype("string")


def _normalize_regime(value: Any) -> str:
    return contract_normalize_entry_regime(value)


def _csv_allowlist(value: Any) -> set[str]:
    return {
        str(item).strip().lower()
        for item in str(value or "").split(",")
        if str(item).strip()
    }


def _normalize_dex_id(value: Any) -> str:
    return contract_normalize_dex_id(value)


def _coalesced_string_series(df: pd.DataFrame, columns: Sequence[str]) -> pd.Series:
    out = pd.Series(pd.NA, index=df.index, dtype="string")
    for col in columns:
        if col in df.columns:
            values = df[col].astype("string")
            out = out.mask(out.isna() | out.eq(""), values)
    return out


def _productive_regime_mask(df: pd.DataFrame) -> pd.Series:
    if "entry_regime" in df.columns:
        regime_series = df["entry_regime"]
    elif "discovered_via" in df.columns:
        regime_series = df["discovered_via"]
    else:
        regime_series = pd.Series("dex_mature", index=df.index, dtype="string")
    normalized = regime_series.astype("string").map(_normalize_regime)
    return normalized.eq("pump_early")


def _numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return pd.to_numeric(df[column], errors="coerce")


def _bool_like_series(df: pd.DataFrame, columns: Sequence[str]) -> pd.Series:
    out = pd.Series(np.nan, index=df.index, dtype="float64")
    for column in columns:
        if column not in df.columns:
            continue
        values = pd.to_numeric(df[column], errors="coerce")
        out = out.mask(out.isna(), values)
    return out.fillna(0.0).gt(0.0)


def _parse_price5m_ranges() -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    raw = str(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES", "25:999") or "25:999")
    for item in raw.split(","):
        if ":" not in item:
            continue
        left, right = item.split(":", 1)
        try:
            lo = float(left)
            hi = float(right)
        except Exception:
            continue
        ranges.append((min(lo, hi), max(lo, hi)))
    return ranges


def _between(series: pd.Series, low: float, high: float) -> pd.Series:
    return series.ge(float(low)) & series.le(float(high))


def _meteor_prime_mask(df: pd.DataFrame) -> pd.Series:
    liquidity = _numeric_series(df, "liquidity_usd")
    mcap = _numeric_series(df, "market_cap_usd")
    price5m = _numeric_series(df, "price_pct_5m")
    txns_5m = _numeric_series(df, "txns_last_5m")
    impact = _numeric_series(df, "price_impact_pct")
    score_total = _numeric_series(df, "score_total")
    age = _numeric_series(df, "age_minutes").fillna(_numeric_series(df, "age_min"))
    volume_24h = _numeric_series(df, "volume_24h_usd").fillna(_numeric_series(df, "volume_usd_24h"))
    return (
        liquidity.ge(float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_LIQUIDITY_USD", 4_000.0) or 4_000.0))
        & liquidity.le(float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_LIQUIDITY_USD", 30_000.0) or 30_000.0))
        & mcap.ge(float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_MARKET_CAP_USD", 5_000.0) or 5_000.0))
        & mcap.le(float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_MARKET_CAP_USD", 30_000.0) or 30_000.0))
        & price5m.ge(float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_PRICE_PCT_5M", 110.0) or 110.0))
        & price5m.le(float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_PCT_5M", 300.0) or 300.0))
        & txns_5m.ge(float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_TXNS_5M", 220) or 220))
        & score_total.ge(float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_SCORE_TOTAL", 30) or 30))
        & age.ge(float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_AGE_MIN", 3.0) or 3.0))
        & age.le(float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_AGE_MIN", 18.0) or 18.0))
        & impact.le(float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_IMPACT_PCT", 12.0) or 12.0))
        & volume_24h.ge(float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_VOLUME_USD_24H", 8_000.0) or 8_000.0))
    )


def _profit_shape_guard_mask(df: pd.DataFrame) -> pd.Series:
    if not bool(getattr(CFG, "PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED", True)):
        return pd.Series(True, index=df.index)

    price5m = _numeric_series(df, "price_pct_5m")
    txns_5m = _numeric_series(df, "txns_last_5m")
    liquidity = _numeric_series(df, "liquidity_usd")
    mcap = _numeric_series(df, "market_cap_usd")
    volume24h = _numeric_series(df, "volume_24h_usd").fillna(_numeric_series(df, "volume_usd_24h"))

    ok = pd.Series(True, index=df.index)
    ok &= mcap.lt(float(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD", 200_000.0) or 200_000.0))
    ok &= ~(
        price5m.ge(float(getattr(CFG, "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_PCT", 300.0) or 300.0))
        & mcap.ge(float(getattr(CFG, "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_MIN_MCAP_USD", 100_000.0) or 100_000.0))
    )
    ok &= ~(
        price5m.le(float(getattr(CFG, "PUMP_EARLY_PROFIT_DEEP_NEG_PRICE5M_PCT", -40.0) or -40.0))
        & txns_5m.lt(float(getattr(CFG, "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_TXNS_5M", 1_500) or 1_500))
        & volume24h.lt(float(getattr(CFG, "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_VOLUME_USD_24H", 150_000.0) or 150_000.0))
    )
    ok &= ~(
        volume24h.ge(float(getattr(CFG, "PUMP_EARLY_PROFIT_DEAD_VOLUME_MIN_USD_24H", 15_000.0) or 15_000.0))
        & volume24h.lt(float(getattr(CFG, "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_USD_24H", 30_000.0) or 30_000.0))
        & txns_5m.lt(float(getattr(CFG, "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_TXNS_5M", 1_000) or 1_000))
    )
    ok &= ~(
        price5m.ge(float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_PRICE5M_MIN_PCT", 100.0) or 100.0))
        & price5m.le(float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_PRICE5M_MAX_PCT", 180.0) or 180.0))
        & mcap.ge(float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MCAP_MIN_USD", 50_000.0) or 50_000.0))
        & (
            liquidity.lt(float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MIN_LIQUIDITY_USD", 20_000.0) or 20_000.0))
            | txns_5m.lt(float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MIN_TXNS_5M", 600) or 600))
            | volume24h.lt(float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MIN_VOLUME_USD_24H", 50_000.0) or 50_000.0))
        )
    )
    low_volume_no_momentum_max = float(
        getattr(CFG, "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H", 0.0) or 0.0
    )
    ok &= ~(
        (low_volume_no_momentum_max > 0.0)
        & volume24h.lt(low_volume_no_momentum_max)
        & txns_5m.lt(float(getattr(CFG, "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_TXNS_5M", 500) or 500))
        & price5m.lt(
            float(getattr(CFG, "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_PRICE5M_PCT", 50.0) or 50.0)
        )
    )
    ok &= ~(
        mcap.lt(25_000.0)
        & price5m.ge(25.0)
        & price5m.lt(50.0)
        & txns_5m.lt(float(getattr(CFG, "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_TXNS_5M", 350) or 350))
        & volume24h.lt(float(getattr(CFG, "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_VOLUME_USD_24H", 100_000.0) or 100_000.0))
    )
    ok &= ~(
        mcap.ge(float(getattr(CFG, "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD", 100_000.0) or 100_000.0))
        & price5m.ge(float(getattr(CFG, "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT", 40.0) or 40.0))
        & price5m.lt(float(getattr(CFG, "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT", 50.0) or 50.0))
    )
    return ok


def _productive_lane_fallback_mask(df: pd.DataFrame) -> tuple[pd.Series, dict[str, Any]]:
    dex_series = _coalesced_string_series(df, ("dex_id", "dexId", "buy_dex_id")).map(_normalize_dex_id)
    liquidity = _numeric_series(df, "liquidity_usd")
    score_total = _numeric_series(df, "score_total")
    age = _numeric_series(df, "age_minutes").fillna(_numeric_series(df, "age_min"))
    impact = _numeric_series(df, "price_impact_pct")
    mcap = _numeric_series(df, "market_cap_usd")
    price5m = _numeric_series(df, "price_pct_5m")
    txns_5m = _numeric_series(df, "txns_last_5m")
    has_route = _bool_like_series(df, ("has_jupiter_route",))
    proxy_liquidity = _bool_like_series(df, ("liquidity_is_proxy", "liquidity_usd_is_proxy"))

    ranges = _parse_price5m_ranges()
    blocked_price5m = pd.Series(False, index=df.index)
    for low, high in ranges:
        blocked_price5m |= _between(price5m, low, high)

    standard_mask = (
        dex_series.eq("pumpswap")
        & has_route
        & (~proxy_liquidity)
        & liquidity.ge(float(getattr(CFG, "PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD", 5_000.0) or 5_000.0))
        & score_total.ge(float(getattr(CFG, "PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL", 35) or 35))
        & age.ge(float(getattr(CFG, "PUMP_EARLY_PROFIT_MIN_AGE_MIN", 3.0) or 3.0))
        & age.le(float(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_AGE_MIN", 30.0) or 30.0))
        & impact.le(float(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT", 10.0) or 10.0))
        & ~(
            (float(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD", 0.0) or 0.0) > 0.0)
            & (float(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD", 0.0) or 0.0) > 0.0)
            & mcap.ge(float(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD", 0.0) or 0.0))
            & mcap.le(float(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD", 0.0) or 0.0))
        )
        & (~blocked_price5m)
        & _profit_shape_guard_mask(df)
    )
    meteor_enabled = bool(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_ENABLED", False))
    meteor_mask = (
        _meteor_prime_mask(df) & dex_series.eq("pumpswap") & has_route & (~proxy_liquidity)
        if meteor_enabled
        else pd.Series(False, index=df.index)
    )
    volume24h = _numeric_series(df, "volume_24h_usd").fillna(_numeric_series(df, "volume_usd_24h"))
    breakout_mask = (
        pd.Series(False, index=df.index)
        if not bool(getattr(CFG, "PUMP_EARLY_BREAKOUT_PROBE_ENABLED", True))
        else (
            dex_series.eq("pumpswap")
            & has_route
            & (~proxy_liquidity)
            & liquidity.ge(float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_LIQUIDITY_USD", 5_000.0) or 5_000.0))
            & liquidity.le(float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_LIQUIDITY_USD", 30_000.0) or 30_000.0))
            & mcap.ge(float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_MARKET_CAP_USD", 5_000.0) or 5_000.0))
            & mcap.le(float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_MARKET_CAP_USD", 60_000.0) or 60_000.0))
            & price5m.ge(float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_PRICE_PCT_5M", 25.0) or 25.0))
            & price5m.le(float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_PRICE_PCT_5M", 120.0) or 120.0))
            & txns_5m.ge(float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_TXNS_5M", 300) or 300))
            & volume24h.ge(float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_VOLUME_USD_24H", 20_000.0) or 20_000.0))
            & score_total.ge(float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_SCORE_TOTAL", 35) or 35))
            & age.ge(float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MIN_AGE_MIN", 2.0) or 2.0))
            & age.le(float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_AGE_MIN", 15.0) or 15.0))
            & impact.le(float(getattr(CFG, "PUMP_EARLY_BREAKOUT_MAX_PRICE_IMPACT_PCT", 8.0) or 8.0))
        )
    )
    fallback = standard_mask | meteor_mask | breakout_mask
    return fallback, {
        "fallback_rows": int(fallback.sum()),
        "fallback_standard_rows": int(standard_mask.sum()),
        "fallback_meteor_rows": int(meteor_mask.sum()) if isinstance(meteor_mask, pd.Series) else 0,
        "fallback_breakout_rows": int(breakout_mask.sum()) if isinstance(breakout_mask, pd.Series) else 0,
    }


def _productive_lane_mask(
    df: pd.DataFrame,
    *,
    entry_lane_allowlist: Any | None = None,
    allow_missing_entry_lane: bool | None = None,
) -> tuple[pd.Series, dict[str, Any]]:
    if entry_lane_allowlist is None:
        entry_lane_allowlist = getattr(CFG, "ML_TRAIN_ENTRY_LANE_ALLOWLIST", "")
    allowed = _csv_allowlist(entry_lane_allowlist)
    lane_series = _coalesced_string_series(df, ("entry_lane", "profit_lane_tier", "size_bucket"))
    normalized = lane_series.fillna("").astype("string").str.strip().str.lower()
    if not allowed:
        return pd.Series(True, index=df.index), {"entry_lane_allowlist": [], "entry_lane_rows": int(len(df))}
    mask = normalized.isin(allowed)
    missing_lane = normalized.eq("")
    allow_missing = (
        bool(getattr(CFG, "ML_TRAIN_ALLOW_MISSING_ENTRY_LANE", True))
        if allow_missing_entry_lane is None
        else bool(allow_missing_entry_lane)
    )
    if allow_missing:
        fallback_mask, fallback_meta = _productive_lane_fallback_mask(df)
        reconstructed = missing_lane & fallback_mask
        mask = mask | reconstructed
    else:
        fallback_meta = {"fallback_rows": 0, "fallback_standard_rows": 0, "fallback_meteor_rows": 0}
    counts = normalized.replace("", "<missing>").value_counts(dropna=False)
    return mask, {
        "entry_lane_allowlist": sorted(allowed),
        "entry_lane_rows": int(mask.sum()),
        "entry_lane_missing_rows_allowed": int((missing_lane & mask).sum()) if allow_missing else 0,
        "entry_lane_missing_allowed": bool(allow_missing),
        "rows_missing_lane_metadata": int(missing_lane.sum()),
        "rows_missing_lane_metadata_reconstructed": int((missing_lane & mask).sum()) if allow_missing else 0,
        "entry_lane_counts": {str(idx): int(count) for idx, count in counts.items()},
        **fallback_meta,
    }


def _productive_dex_mask(
    df: pd.DataFrame,
    *,
    dex_allowlist: Any | None = None,
) -> tuple[pd.Series, dict[str, Any]]:
    if dex_allowlist is None:
        dex_allowlist = getattr(CFG, "ML_TRAIN_DEX_ALLOWLIST", "")
    allowed = {_normalize_dex_id(item) for item in _csv_allowlist(dex_allowlist)}
    dex_series = _coalesced_string_series(df, ("dex_id", "dexId", "buy_dex_id"))
    normalized = dex_series.map(_normalize_dex_id)
    if not allowed:
        return pd.Series(True, index=df.index), {"dex_allowlist": [], "dex_rows": int(len(df))}
    mask = normalized.isin(allowed)
    counts = normalized.replace("", "<missing>").value_counts(dropna=False)
    return mask, {
        "dex_allowlist": sorted(allowed),
        "dex_rows": int(mask.sum()),
        "dex_counts": {str(idx): int(count) for idx, count in counts.items()},
    }


def _filter_outcome_training_rows(
    df: pd.DataFrame,
    *,
    entry_lane_allowlist: Any | None = None,
    dex_allowlist: Any | None = None,
    allow_missing_entry_lane: bool | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    sample_type = _sample_type_series(df)
    return_col = _resolve_return_col(df)
    if return_col:
        realized_mask = pd.to_numeric(df[return_col], errors="coerce").notna()
    else:
        realized_mask = pd.Series(False, index=df.index)

    allowed_mask = sample_type.isin(OUTCOME_SAMPLE_TYPES)
    legacy_outcome_mask = (sample_type.isna() | sample_type.eq("unknown")) & realized_mask
    regime_mask = _productive_regime_mask(df)
    lane_mask, lane_meta = _productive_lane_mask(
        df,
        entry_lane_allowlist=entry_lane_allowlist,
        allow_missing_entry_lane=allow_missing_entry_lane,
    )
    dex_mask, dex_meta = _productive_dex_mask(df, dex_allowlist=dex_allowlist)
    eligible_mask = (allowed_mask | legacy_outcome_mask) & regime_mask & lane_mask & dex_mask

    counts_raw = sample_type.fillna("<NA>").value_counts(dropna=False)
    sample_type_counts = {str(idx): int(count) for idx, count in counts_raw.items()}

    meta = {
        "sample_type_counts": sample_type_counts,
        "outcome_rows": int(allowed_mask.sum()),
        "legacy_outcome_rows": int(legacy_outcome_mask.sum()),
        "policy_reject_rows": int(sample_type.fillna("").eq("policy_reject").sum()),
        "eligible_rows": int(eligible_mask.sum()),
        "productive_regime_rows": int(regime_mask.sum()),
        **lane_meta,
        **dex_meta,
    }
    return df.loc[eligible_mask].copy(), meta


def _select_feature_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str], list[str]]:
    excluded_effective: list[str] = []
    keep_candidate: list[str] = []

    for col in df.columns:
        if col in _META_COLS:
            excluded_effective.append(col)
            continue
        lc = col.lower()
        if any(sub in lc for sub in _FORBIDDEN_SUBSTR):
            excluded_effective.append(col)
            continue
        if pd.api.types.is_numeric_dtype(df[col]):
            keep_candidate.append(col)

    if keep_candidate:
        coerced = coerce_feature_frame(df, keep_candidate)
        zero_cols = [c for c in keep_candidate if float(coerced[c].var(ddof=0)) == 0.0]
    else:
        zero_cols = []

    x_cols = [c for c in keep_candidate if c not in zero_cols]
    excluded_effective.extend(zero_cols)
    return df, x_cols, excluded_effective


def _precision_at_k(y_true: np.ndarray, y_prob: np.ndarray, k_pct: float = 0.1) -> float:
    n = y_prob.shape[0]
    if n == 0:
        return float("nan")
    k = max(1, int(round(n * float(k_pct))))
    order = np.argsort(-y_prob)
    top_idx = order[:k]
    return float(np.mean(y_true[top_idx]))


def _forward_holdout_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    if not (HOLDOUT_DAYS or HOLDOUT_PCT):
        raise RuntimeError("Hold-out forward no configurado")

    t_series = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    tmin, tmax = t_series.min(), t_series.max()
    if HOLDOUT_DAYS:
        cutoff = tmax - pd.Timedelta(days=int(HOLDOUT_DAYS))
    else:
        df_sorted = df.sort_values("timestamp")
        n = len(df_sorted)
        k = max(1, int(round(n * float(HOLDOUT_PCT))))
        cutoff = df_sorted.iloc[-k]["timestamp"]

    first_ts = df.groupby("mint", dropna=False)["timestamp"].min()
    val_mints = first_ts[first_ts >= cutoff].index
    train_mints = first_ts[first_ts < cutoff].index
    tr_df = df[df["mint"].isin(train_mints)].copy()
    te_df = df[df["mint"].isin(val_mints)].copy()
    return tr_df, te_df, {
        "mode": "forward_holdout",
        "cutoff": str(cutoff),
        "tmin": str(tmin),
        "tmax": str(tmax),
        "train_mints": int(tr_df["mint"].nunique()),
        "val_mints": int(te_df["mint"].nunique()),
    }


def _walk_forward_splits_by_mint(
    df: pd.DataFrame,
    *,
    n_splits: int = 5,
    min_train_blocks: int = 2,
) -> list[tuple[np.ndarray, np.ndarray]]:
    if "mint" not in df.columns or "timestamp" not in df.columns:
        raise ValueError("Faltan columnas mint/timestamp para walk-forward agrupado")

    first_ts = df.groupby("mint", dropna=False)["timestamp"].min().sort_values(kind="mergesort")
    mints_sorted = first_ts.index.to_numpy()
    mint_blocks: list[np.ndarray] = [np.asarray(block) for block in np.array_split(mints_sorted, n_splits) if len(block) > 0]

    splits: list[tuple[np.ndarray, np.ndarray]] = []
    start_block = max(1, int(min_train_blocks))
    for block_idx in range(start_block, len(mint_blocks)):
        train_mints = np.concatenate(mint_blocks[:block_idx])
        test_mints = mint_blocks[block_idx]
        train_mask = df["mint"].isin(train_mints)
        test_mask = df["mint"].isin(test_mints)
        tr_idx = np.where(train_mask.values)[0]
        te_idx = np.where(test_mask.values)[0]
        if len(tr_idx) == 0 or len(te_idx) == 0:
            continue
        splits.append((tr_idx, te_idx))
    return splits


def _build_walk_forward_scheme(df: pd.DataFrame) -> tuple[list[tuple[np.ndarray, np.ndarray]], dict[str, Any]]:
    unique_mints = int(df["mint"].nunique()) if "mint" in df.columns and not df.empty else 0
    n_splits = max(2, min(5, unique_mints)) if unique_mints else 2
    min_train_blocks = 1 if unique_mints <= 4 else 2
    splits = _walk_forward_splits_by_mint(
        df,
        n_splits=n_splits,
        min_train_blocks=min_train_blocks,
    )
    return splits, {
        "mode": "walk_forward_grouped_by_mint",
        "splits": int(len(splits)),
        "n_splits_requested": int(n_splits),
        "min_train_blocks": int(min_train_blocks),
    }


def _initial_quality(
    df_source: pd.DataFrame,
    df_train: pd.DataFrame,
    x_cols: list[str],
    filtering_meta: dict[str, Any],
) -> DatasetQuality:
    return_col = _resolve_return_col(df_train)
    realized_return_rows = int(pd.to_numeric(df_train[return_col], errors="coerce").notna().sum()) if return_col else 0
    rows = int(len(df_train))
    positives = int(pd.to_numeric(df_train["label"], errors="coerce").fillna(0).sum()) if not df_train.empty else 0
    unique_tokens = int(df_train["mint"].nunique()) if "mint" in df_train.columns and not df_train.empty else 0
    source_rows = int(len(df_source))
    source_positives = int(pd.to_numeric(df_source["label"], errors="coerce").fillna(0).sum()) if not df_source.empty else 0
    source_unique_tokens = int(df_source["mint"].nunique()) if "mint" in df_source.columns and not df_source.empty else 0

    reasons: list[str] = []
    if rows < int(getattr(CFG, "ML_MIN_DATASET_ROWS", 250)):
        reasons.append(f"rows<{CFG.ML_MIN_DATASET_ROWS}")
    if positives < int(getattr(CFG, "ML_MIN_POSITIVES", 40)):
        reasons.append(f"positives<{CFG.ML_MIN_POSITIVES}")
    if unique_tokens < int(getattr(CFG, "ML_MIN_UNIQUE_TOKENS", 200)):
        reasons.append(f"unique_tokens<{CFG.ML_MIN_UNIQUE_TOKENS}")
    if realized_return_rows < int(getattr(CFG, "ML_MIN_REALIZED_RETURN_ROWS", 50)):
        reasons.append(f"realized_return_rows<{CFG.ML_MIN_REALIZED_RETURN_ROWS}")
    if len(x_cols) < int(getattr(CFG, "ML_MIN_NON_CONSTANT_FEATURES", 12)):
        reasons.append(f"non_constant_numeric_features<{CFG.ML_MIN_NON_CONSTANT_FEATURES}")

    return DatasetQuality(
        passed=not reasons,
        reasons=reasons,
        source_rows=source_rows,
        source_positives=source_positives,
        source_unique_tokens=source_unique_tokens,
        rows=rows,
        positives=positives,
        unique_tokens=unique_tokens,
        outcome_rows=int(filtering_meta.get("outcome_rows", 0)),
        legacy_outcome_rows=int(filtering_meta.get("legacy_outcome_rows", 0)),
        policy_reject_rows=int(filtering_meta.get("policy_reject_rows", 0)),
        realized_return_rows=realized_return_rows,
        numeric_feature_candidates=len(x_cols),
        non_constant_numeric_features=len(x_cols),
        holdout_rows=0,
        holdout_positives=0,
        holdout_unique_tokens=0,
        sample_type_counts=dict(filtering_meta.get("sample_type_counts", {})),
    )


def _finalize_quality(quality: DatasetQuality, val_df: pd.DataFrame) -> DatasetQuality:
    reasons = list(quality.reasons)
    holdout_rows = int(len(val_df))
    holdout_positives = int(pd.to_numeric(val_df.get("label"), errors="coerce").fillna(0).sum()) if not val_df.empty else 0
    holdout_unique_tokens = int(val_df["mint"].nunique()) if not val_df.empty and "mint" in val_df.columns else 0
    if holdout_rows < int(getattr(CFG, "ML_MIN_HOLDOUT_ROWS", 40)):
        reasons.append(f"holdout_rows<{CFG.ML_MIN_HOLDOUT_ROWS}")
    if holdout_positives < int(getattr(CFG, "ML_MIN_HOLDOUT_POSITIVES", 8)):
        reasons.append(f"holdout_positives<{CFG.ML_MIN_HOLDOUT_POSITIVES}")
    return DatasetQuality(
        passed=not reasons,
        reasons=reasons,
        source_rows=quality.source_rows,
        source_positives=quality.source_positives,
        source_unique_tokens=quality.source_unique_tokens,
        rows=quality.rows,
        positives=quality.positives,
        unique_tokens=quality.unique_tokens,
        outcome_rows=quality.outcome_rows,
        legacy_outcome_rows=quality.legacy_outcome_rows,
        policy_reject_rows=quality.policy_reject_rows,
        realized_return_rows=quality.realized_return_rows,
        numeric_feature_candidates=quality.numeric_feature_candidates,
        non_constant_numeric_features=quality.non_constant_numeric_features,
        holdout_rows=holdout_rows,
        holdout_positives=holdout_positives,
        holdout_unique_tokens=holdout_unique_tokens,
        sample_type_counts=dict(quality.sample_type_counts),
    )


def _status_payload(
    *,
    status: str,
    quality: DatasetQuality,
    feature_hash: str,
    features: list[str],
    excluded_effective: list[str],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "status": status,
        "dataset_quality": asdict(quality),
        "feature_set_hash": feature_hash,
        "features": features,
        "excluded_columns": sorted(excluded_effective),
    }
    if extra:
        payload.update(extra)
    return payload


def _quality_readiness(quality: DatasetQuality) -> dict[str, Any]:
    thresholds = {
        "rows": int(getattr(CFG, "ML_MIN_DATASET_ROWS", 250)),
        "positives": int(getattr(CFG, "ML_MIN_POSITIVES", 40)),
        "unique_tokens": int(getattr(CFG, "ML_MIN_UNIQUE_TOKENS", 200)),
        "realized_return_rows": int(getattr(CFG, "ML_MIN_REALIZED_RETURN_ROWS", 50)),
        "holdout_rows": int(getattr(CFG, "ML_MIN_HOLDOUT_ROWS", 40)),
        "holdout_positives": int(getattr(CFG, "ML_MIN_HOLDOUT_POSITIVES", 8)),
    }
    actuals = {
        "rows": int(quality.rows),
        "positives": int(quality.positives),
        "unique_tokens": int(quality.unique_tokens),
        "realized_return_rows": int(quality.realized_return_rows),
        "holdout_rows": int(quality.holdout_rows),
        "holdout_positives": int(quality.holdout_positives),
    }
    deficits = {key: max(0, thresholds[key] - actuals[key]) for key in thresholds}
    return {
        "actuals": actuals,
        "thresholds": thresholds,
        "deficits": deficits,
        "skip_reasons": list(quality.reasons),
        "rows_to_next_model": max(deficits["rows"], deficits["unique_tokens"]),
        "positives_to_next_model": deficits["positives"],
        "unique_tokens_to_next_model": deficits["unique_tokens"],
        "holdout_rows_to_next_model": deficits["holdout_rows"],
        "holdout_positives_to_next_model": deficits["holdout_positives"],
        "blocker": ",".join(quality.reasons) if quality.reasons else None,
    }


def _build_training_context(
    df_source: pd.DataFrame,
    *,
    training_scope: str,
    entry_lane_allowlist: Any | None = None,
    dex_allowlist: Any | None = None,
    allow_missing_entry_lane: bool | None = None,
) -> dict[str, Any]:
    df_trainable, filtering_meta = _filter_outcome_training_rows(
        df_source,
        entry_lane_allowlist=entry_lane_allowlist,
        dex_allowlist=dex_allowlist,
        allow_missing_entry_lane=allow_missing_entry_lane,
    )
    df_trainable, x_cols, excluded_effective = _select_feature_columns(df_trainable)

    feat_hash = hashlib.md5(",".join(sorted(x_cols)).encode("utf-8")).hexdigest()[:10]
    base_quality = _initial_quality(df_source, df_trainable, x_cols, filtering_meta)

    split_meta: dict[str, Any]
    use_forward = bool(HOLDOUT_DAYS or HOLDOUT_PCT)
    tr_df = pd.DataFrame()
    te_df = pd.DataFrame()
    cv_splits: list[tuple[np.ndarray, np.ndarray]] = []
    if use_forward:
        tr_df, te_df, forward_meta = _forward_holdout_split(df_trainable)
        split_meta = dict(forward_meta)
        val_quality_df = te_df
        needs_fallback = tr_df.empty or te_df.empty or tr_df["label"].nunique(dropna=False) < 2
        if needs_fallback:
            cv_splits, walk_meta = _build_walk_forward_scheme(df_trainable)
            if cv_splits:
                use_forward = False
                val_quality_df = pd.concat(
                    [df_trainable.iloc[te_idx] for _, te_idx in cv_splits],
                    ignore_index=True,
                )
                split_meta = {
                    **walk_meta,
                    "fallback_from_forward_holdout": True,
                    "forward_holdout_meta": forward_meta,
                }
            else:
                split_meta = {
                    **forward_meta,
                    "fallback_from_forward_holdout": True,
                    "forward_holdout_unusable": True,
                }
    else:
        cv_splits, split_meta = _build_walk_forward_scheme(df_trainable)
        val_quality_df = (
            pd.concat([df_trainable.iloc[te_idx] for _, te_idx in cv_splits], ignore_index=True)
            if cv_splits
            else pd.DataFrame()
        )

    quality = _finalize_quality(base_quality, val_quality_df)
    if use_forward and tr_df.empty:
        quality = DatasetQuality(
            passed=False,
            reasons=[*quality.reasons, "train_rows_empty"],
            source_rows=quality.source_rows,
            source_positives=quality.source_positives,
            source_unique_tokens=quality.source_unique_tokens,
            rows=quality.rows,
            positives=quality.positives,
            unique_tokens=quality.unique_tokens,
            outcome_rows=quality.outcome_rows,
            legacy_outcome_rows=quality.legacy_outcome_rows,
            policy_reject_rows=quality.policy_reject_rows,
            realized_return_rows=quality.realized_return_rows,
            numeric_feature_candidates=quality.numeric_feature_candidates,
            non_constant_numeric_features=quality.non_constant_numeric_features,
            holdout_rows=quality.holdout_rows,
            holdout_positives=quality.holdout_positives,
            holdout_unique_tokens=quality.holdout_unique_tokens,
            sample_type_counts=dict(quality.sample_type_counts),
        )

    return {
        "training_scope": training_scope,
        "df_trainable": df_trainable,
        "x_cols": x_cols,
        "excluded_effective": excluded_effective,
        "feat_hash": feat_hash,
        "filtering_meta": filtering_meta,
        "use_forward": use_forward,
        "tr_df": tr_df,
        "te_df": te_df,
        "cv_splits": cv_splits,
        "split_meta": split_meta,
        "quality": quality,
        "readiness": _quality_readiness(quality),
    }


def _quality_public_summary(quality: DatasetQuality, readiness: dict[str, Any]) -> dict[str, Any]:
    return {
        "passed": bool(quality.passed),
        "reasons": list(quality.reasons),
        "eligible_rows": int(quality.rows),
        "eligible_unique_tokens": int(quality.unique_tokens),
        "eligible_positives": int(quality.positives),
        "holdout_rows": int(quality.holdout_rows),
        "holdout_positives": int(quality.holdout_positives),
        "rows_to_next_model": readiness.get("rows_to_next_model"),
        "positives_to_next_model": readiness.get("positives_to_next_model"),
        "unique_tokens_to_next_model": readiness.get("unique_tokens_to_next_model"),
        "blocker": readiness.get("blocker"),
    }


def _predict_scores(model: Any, frame: pd.DataFrame, x_cols: Sequence[str]) -> np.ndarray:
    X = coerce_feature_frame(frame, x_cols)
    try:
        scores = model.predict_proba(X)[:, 1]
    except AttributeError:
        scores = model.predict(X)
    return np.asarray(scores, dtype=float)


def _fit_logreg_calibrated(train_df: pd.DataFrame, x_cols: list[str]) -> Any:
    X = coerce_feature_frame(train_df, x_cols)
    y = train_df["label"].to_numpy(dtype=int)
    pos = int(y.sum())
    neg = int(len(y) - pos)
    if pos <= 0 or neg <= 0:
        raise ValueError("LogReg requiere ambas clases en train")

    base = Pipeline(
        steps=[
            ("scaler", StandardScaler()),
            (
                "clf",
                LogisticRegression(
                    C=0.35,
                    class_weight="balanced",
                    max_iter=2000,
                    solver="lbfgs",
                    random_state=42,
                ),
            ),
        ]
    )
    calib_cv = min(3, pos, neg)
    if calib_cv >= 2:
        model = CalibratedClassifierCV(estimator=base, method="sigmoid", cv=calib_cv)
    else:
        model = base
    model.fit(X, y)
    return model


def _fit_lightgbm_small(train_df: pd.DataFrame, x_cols: list[str]) -> Any:
    X = coerce_feature_frame(train_df, x_cols)
    y = train_df["label"].to_numpy(dtype=int)
    pos = int(y.sum())
    neg = int(len(y) - pos)
    if pos <= 0 or neg <= 0:
        raise ValueError("LightGBM requiere ambas clases en train")

    min_data_in_leaf = int(max(20, min(80, round(len(train_df) * 0.08))))
    params = dict(
        objective="binary",
        metric="auc",
        learning_rate=0.05,
        num_leaves=15,
        max_depth=4,
        min_data_in_leaf=min_data_in_leaf,
        feature_fraction=0.8,
        bagging_fraction=0.8,
        bagging_freq=1,
        lambda_l1=1.0,
        lambda_l2=2.0,
        min_gain_to_split=0.05,
        is_unbalance=True,
        feature_pre_filter=False,
        verbosity=-1,
        seed=42,
    )
    rounds = int(max(80, min(220, max(len(train_df) // 2, 80))))
    train_set = lgb.Dataset(X, y, feature_name=list(x_cols), free_raw_data=True)
    return lgb.train(params, train_set, num_boost_round=rounds)


def _extract_feature_signal(model: Any, x_cols: list[str], *, limit: int = 20) -> list[dict[str, float]]:
    pairs: list[tuple[str, float]] = []
    try:
        if hasattr(model, "feature_importance"):
            imp = np.asarray(model.feature_importance(), dtype=float)
            pairs = list(zip(x_cols, imp.tolist()))
        else:
            coef_vectors: list[np.ndarray] = []
            calibrated = getattr(model, "calibrated_classifiers_", None)
            if calibrated:
                for calibrated_model in calibrated:
                    estimator = getattr(calibrated_model, "estimator", None)
                    if isinstance(estimator, Pipeline):
                        clf = estimator.named_steps.get("clf")
                        if clf is not None and hasattr(clf, "coef_"):
                            coef_vectors.append(np.abs(np.asarray(clf.coef_[0], dtype=float)))
            elif isinstance(model, Pipeline):
                clf = model.named_steps.get("clf")
                if clf is not None and hasattr(clf, "coef_"):
                    coef_vectors.append(np.abs(np.asarray(clf.coef_[0], dtype=float)))

            if coef_vectors:
                avg_coef = np.mean(np.vstack(coef_vectors), axis=0)
                pairs = list(zip(x_cols, avg_coef.tolist()))
    except Exception:
        pairs = []

    pairs = [(name, float(score)) for name, score in pairs if np.isfinite(float(score))]
    pairs.sort(key=lambda item: -abs(item[1]))
    return [{"feature": name, "importance": float(score)} for name, score in pairs[:limit]]


def _candidate_builders() -> list[tuple[str, str, Callable[[pd.DataFrame, list[str]], Any]]]:
    return [
        ("logreg_calibrated", "sklearn_logreg", _fit_logreg_calibrated),
        ("lightgbm_small", "lightgbm", _fit_lightgbm_small),
    ]


def _attach_validation_context(fold_df: pd.DataFrame, source_df: pd.DataFrame) -> pd.DataFrame:
    """Add non-feature context columns to validation predictions for segment reports."""
    out = fold_df.copy()
    for col in (
        "sample_type",
        "entry_lane",
        "entry_regime",
        "dex_id",
        "price_source",
        "mcap_bucket",
        "price5m_bucket",
        "market_cap_usd",
        "price_pct_5m",
    ):
        if col in source_df.columns and col not in out.columns:
            out[col] = source_df[col].values
    if "sample_type" in out.columns:
        out["sample_type"] = out["sample_type"].map(normalize_sample_type).astype("string")
    return out


def _evaluate_candidate(
    *,
    name: str,
    model_family: str,
    builder: Callable[[pd.DataFrame, list[str]], Any],
    x_cols: list[str],
    use_forward: bool,
    tr_df: pd.DataFrame | None = None,
    te_df: pd.DataFrame | None = None,
    full_df: pd.DataFrame | None = None,
    cv_splits: list[tuple[np.ndarray, np.ndarray]] | None = None,
) -> CandidateResult:
    val_preds_rows: list[pd.DataFrame] = []
    auc_values: list[float] = []
    ap_values: list[float] = []

    if use_forward:
        assert tr_df is not None and te_df is not None
        if tr_df.empty or te_df.empty:
            raise ValueError(f"{name}: split forward vacio")
        if tr_df["label"].nunique(dropna=False) < 2:
            raise ValueError(f"{name}: train forward sin ambas clases")

        model = builder(tr_df, x_cols)
        y_val = te_df["label"].to_numpy(dtype=int)
        y_prob = _predict_scores(model, te_df, x_cols)
        try:
            auc_values.append(float(roc_auc_score(y_val, y_prob)))
        except Exception:
            auc_values.append(float("nan"))
        try:
            ap_values.append(float(average_precision_score(y_val, y_prob)))
        except Exception:
            ap_values.append(float("nan"))

        fold_df = pd.DataFrame(
            {
                "mint": te_df["mint"].values,
                "y_true": y_val,
                "y_prob": y_prob,
                "timestamp": te_df["timestamp"].values,
            }
        )
        return_col = _resolve_return_col(te_df)
        if return_col:
            fold_df["target_total_pnl_pct"] = pd.to_numeric(te_df[return_col], errors="coerce").values
        fold_df = _attach_validation_context(fold_df, te_df)
        val_preds_rows.append(fold_df)
    else:
        assert full_df is not None and cv_splits is not None
        if not cv_splits:
            raise ValueError(f"{name}: sin splits walk-forward validos")

        for fold, (tr_idx, te_idx) in enumerate(cv_splits, start=1):
            fold_train = full_df.iloc[tr_idx].copy()
            fold_val = full_df.iloc[te_idx].copy()
            if fold_train.empty or fold_val.empty:
                continue
            if fold_train["label"].nunique(dropna=False) < 2:
                continue

            model = builder(fold_train, x_cols)
            y_val = fold_val["label"].to_numpy(dtype=int)
            y_prob = _predict_scores(model, fold_val, x_cols)
            try:
                auc_values.append(float(roc_auc_score(y_val, y_prob)))
            except Exception:
                auc_values.append(float("nan"))
            try:
                ap_values.append(float(average_precision_score(y_val, y_prob)))
            except Exception:
                ap_values.append(float("nan"))

            fold_df = pd.DataFrame(
                {
                    "mint": fold_val["mint"].values,
                    "y_true": y_val,
                    "y_prob": y_prob,
                    "timestamp": fold_val["timestamp"].values,
                    "fold": fold,
                }
            )
            return_col = _resolve_return_col(fold_val)
            if return_col:
                fold_df["target_total_pnl_pct"] = pd.to_numeric(fold_val[return_col], errors="coerce").values
            fold_df = _attach_validation_context(fold_df, fold_val)
            val_preds_rows.append(fold_df)

        if not val_preds_rows:
            raise ValueError(f"{name}: ningun fold entrenable con ambas clases")

    val_preds = pd.concat(val_preds_rows, ignore_index=True)
    val_preds["hour"] = pd.to_datetime(val_preds["timestamp"], utc=True, errors="coerce").dt.hour
    ordered = [
        "mint",
        "y_true",
        "y_prob",
        "target_total_pnl_pct",
        "sample_type",
        "entry_lane",
        "entry_regime",
        "dex_id",
        "price_source",
        "mcap_bucket",
        "price5m_bucket",
        "market_cap_usd",
        "price_pct_5m",
        "timestamp",
        "hour",
        "fold",
    ]
    ordered = [col for col in ordered if col in val_preds.columns]
    val_preds = val_preds[ordered]

    tune_result = tune_from_frame(
        val_preds,
        objective=str(getattr(CFG, "ML_TUNE_OBJECTIVE", "expected_pnl_precision_floor")),
        precision_floor=float(getattr(CFG, "ML_TUNE_PRECISION_FLOOR", 0.60)),
        max_grid=400,
        min_selected=int(getattr(CFG, "ML_TUNE_MIN_SELECTED", 10)),
        min_realized_selected=int(getattr(CFG, "ML_TUNE_MIN_REALIZED_SELECTED", 5)),
        source_csv=str(VAL_PREDS_CSV),
    )
    auc_mean = float(np.nanmean(auc_values)) if auc_values else float("nan")
    ap_mean = float(np.nanmean(ap_values)) if ap_values else float("nan")
    prec_at_k = _precision_at_k(
        val_preds["y_true"].to_numpy(dtype=int),
        val_preds["y_prob"].to_numpy(dtype=float),
        k_pct=PREC_AT_K_PCT,
    )

    final_probe_train = tr_df if use_forward else full_df
    assert final_probe_train is not None
    probe_model = builder(final_probe_train, x_cols)
    feature_signal = _extract_feature_signal(probe_model, x_cols)

    return CandidateResult(
        name=name,
        model_family=model_family,
        tune_result=tune_result,
        auc_mean=auc_mean,
        ap_mean=ap_mean,
        precision_at_k=prec_at_k,
        val_preds=val_preds,
        feature_signal=feature_signal,
    )


def _candidate_rank(candidate: CandidateResult) -> tuple[float, ...]:
    tune = candidate.tune_result
    selection_score = candidate.selection_score if candidate.selection_score is not None else -1e9
    selection_metric_bonus = 1.0 if candidate.selection_metric == "avg_realized_pnl_pct_at_picked" else 0.0
    return (
        1.0 if bool(tune.get("activation_ready")) else 0.0,
        selection_metric_bonus,
        float(selection_score),
        float(candidate.ap_mean) if np.isfinite(candidate.ap_mean) else -1e9,
        float(candidate.precision_at_k) if np.isfinite(candidate.precision_at_k) else -1e9,
    )


def _save_model(model: Any) -> None:
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=MODEL_PATH.parent, prefix=".tmp_model_", suffix=".pkl")
    os.close(tmp_fd)
    joblib.dump(model, tmp_path)
    pathlib.Path(tmp_path).replace(MODEL_PATH)


def train_and_save() -> TrainResult:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    attempted_at = pd.Timestamp.now(tz="UTC").isoformat()

    df_source = _load_dataset()
    df_source = _apply_training_window(df_source)
    context = _build_training_context(df_source, training_scope="productive_strict")
    strict_context = context
    bootstrap_context: dict[str, Any] | None = None
    bootstrap_used = False

    bootstrap_allowed = bool(getattr(CFG, "ML_BOOTSTRAP_RESEARCH_SHADOW_ENABLED", True))
    if bool(getattr(CFG, "ML_BOOTSTRAP_ONLY_WHEN_MODEL_MISSING", True)) and MODEL_PATH.exists():
        bootstrap_allowed = False
    if not bool(context["quality"].passed) and bootstrap_allowed:
        bootstrap_context = _build_training_context(
            df_source,
            training_scope="bootstrap_research_shadow",
            entry_lane_allowlist=getattr(CFG, "ML_BOOTSTRAP_ENTRY_LANE_ALLOWLIST", ""),
            dex_allowlist=getattr(CFG, "ML_BOOTSTRAP_DEX_ALLOWLIST", ""),
            allow_missing_entry_lane=getattr(CFG, "ML_TRAIN_ALLOW_MISSING_ENTRY_LANE", True),
        )
        if bool(bootstrap_context["quality"].passed):
            context = bootstrap_context
            bootstrap_used = True

    df_trainable = context["df_trainable"]
    x_cols = context["x_cols"]
    excluded_effective = context["excluded_effective"]
    feat_hash = context["feat_hash"]
    filtering_meta = context["filtering_meta"]
    use_forward = context["use_forward"]
    tr_df = context["tr_df"]
    te_df = context["te_df"]
    cv_splits = context["cv_splits"]
    split_meta = context["split_meta"]
    quality = context["quality"]
    readiness = context["readiness"]
    training_scope = str(context["training_scope"])

    strict_summary = _quality_public_summary(strict_context["quality"], strict_context["readiness"])
    bootstrap_summary = (
        _quality_public_summary(bootstrap_context["quality"], bootstrap_context["readiness"])
        if bootstrap_context is not None
        else None
    )
    _write_json(DATASET_QUALITY_JSON, asdict(quality))

    print(
        f"[ROWS] scope={training_scope} source={quality.source_rows} "
        f"eligible={quality.rows} policy_reject={quality.policy_reject_rows}"
    )
    print(f"[X] Excluyendo columnas (efectivas, {len(excluded_effective)}): {sorted(excluded_effective)}")
    print(f"[X] Features finales ({len(x_cols)}). Hash={feat_hash}")

    if not quality.passed:
        payload = _status_payload(
            status="skipped_insufficient_dataset",
            quality=quality,
            feature_hash=feat_hash,
            features=x_cols,
            excluded_effective=excluded_effective,
            extra={
                "last_train_attempt_at": attempted_at,
                "last_train_status": "skipped_insufficient_dataset",
                "training_scope": training_scope,
                "bootstrap_used": bool(bootstrap_used),
                "strict_productive_dataset": strict_summary,
                "bootstrap_candidate_dataset": bootstrap_summary,
                "eligible_rows": int(quality.rows),
                "eligible_unique_tokens": int(quality.unique_tokens),
                "eligible_positives": int(quality.positives),
                "holdout_rows": int(quality.holdout_rows),
                "rows_missing_lane_metadata": int(filtering_meta.get("rows_missing_lane_metadata", 0)),
                "skip_reasons": list(quality.reasons),
                **readiness,
                "filtering_meta": filtering_meta,
                "split_meta": split_meta,
                "outcome_sample_types": list(OUTCOME_SAMPLE_TYPES),
            },
        )
        _write_json(TRAIN_STATUS_JSON, payload)
        print(f"[ML] Entrenamiento omitido: {quality.reasons}")
        return TrainResult(
            trained=False,
            status="skipped_insufficient_dataset",
            dataset_quality=quality,
            selection_metric=None,
            selection_score=None,
        )

    candidates: list[CandidateResult] = []
    candidate_errors: dict[str, str] = {}
    candidate_builders = {name: builder for name, _, builder in _candidate_builders()}

    for name, model_family, builder in _candidate_builders():
        try:
            candidate = _evaluate_candidate(
                name=name,
                model_family=model_family,
                builder=builder,
                x_cols=x_cols,
                use_forward=use_forward,
                tr_df=tr_df if use_forward else None,
                te_df=te_df if use_forward else None,
                full_df=df_trainable if not use_forward else None,
                cv_splits=cv_splits if not use_forward else None,
            )
            candidates.append(candidate)
            print(
                "[CAND] {} AUC={:.4f} AP={:.4f} Prec@{:.0f}%={:.4f} metric={} score={} thr={} activation_ready={}".format(
                    candidate.name,
                    candidate.auc_mean,
                    candidate.ap_mean,
                    PREC_AT_K_PCT * 100.0,
                    candidate.precision_at_k,
                    candidate.selection_metric,
                    candidate.selection_score,
                    candidate.tune_result.get("picked"),
                    candidate.tune_result.get("activation_ready"),
                )
            )
        except Exception as exc:
            candidate_errors[name] = str(exc)
            print(f"[CAND] {name} fallo: {exc}")

    if not candidates:
        payload = _status_payload(
            status="failed_candidate_training",
            quality=quality,
            feature_hash=feat_hash,
            features=x_cols,
            excluded_effective=excluded_effective,
            extra={
                "last_train_attempt_at": attempted_at,
                "last_train_status": "failed_candidate_training",
                "training_scope": training_scope,
                "bootstrap_used": bool(bootstrap_used),
                "strict_productive_dataset": strict_summary,
                "bootstrap_candidate_dataset": bootstrap_summary,
                "eligible_rows": int(quality.rows),
                "eligible_unique_tokens": int(quality.unique_tokens),
                "eligible_positives": int(quality.positives),
                "holdout_rows": int(quality.holdout_rows),
                "rows_missing_lane_metadata": int(filtering_meta.get("rows_missing_lane_metadata", 0)),
                "skip_reasons": list(quality.reasons),
                **readiness,
                "filtering_meta": filtering_meta,
                "split_meta": split_meta,
                "candidate_errors": candidate_errors,
                "outcome_sample_types": list(OUTCOME_SAMPLE_TYPES),
            },
        )
        _write_json(TRAIN_STATUS_JSON, payload)
        return TrainResult(
            trained=False,
            status="failed_candidate_training",
            dataset_quality=quality,
            selection_metric=None,
            selection_score=None,
        )

    selected = max(candidates, key=_candidate_rank)
    selected.val_preds.to_csv(VAL_PREDS_CSV, index=False)
    print(f"[ML] Predicciones de validacion -> {VAL_PREDS_CSV} (rows={len(selected.val_preds)})")

    final_model = candidate_builders[selected.name](df_trainable, x_cols)
    final_feature_signal = _extract_feature_signal(final_model, x_cols)

    if final_feature_signal:
        print("[IMP] Top features:")
        for row in final_feature_signal[: min(15, len(final_feature_signal))]:
            print(f"      {row['feature']:30s}  {row['importance']:.4f}")

    tune_result = selected.tune_result
    _write_json(RECOMMENDED_JSON, tune_result)
    meta_payload = {
        "last_train_attempt_at": attempted_at,
        "last_train_status": "trained",
        "training_scope": training_scope,
        "bootstrap_used": bool(bootstrap_used),
        "strict_productive_dataset": strict_summary,
        "bootstrap_candidate_dataset": bootstrap_summary,
        "selected_model_name": selected.name,
        "model_family": selected.model_family,
        "auc_forward_or_cv_mean": selected.auc_mean,
        "auc_pr_forward_or_cv_mean": selected.ap_mean,
        "precision_at_k_pct": float(PREC_AT_K_PCT),
        "precision_at_k_val": selected.precision_at_k,
        "ai_threshold_recommended": tune_result.get("picked"),
        "threshold_metric": tune_result.get("objective_applied"),
        "activation_ready": tune_result.get("activation_ready"),
        "threshold_result": tune_result,
        "dataset_quality_passed": quality.passed,
        "dataset_quality": asdict(quality),
        "rows": int(len(df_trainable)),
        "eligible_rows": int(quality.rows),
        "eligible_unique_tokens": int(quality.unique_tokens),
        "eligible_positives": int(quality.positives),
        "holdout_rows": int(quality.holdout_rows),
        "rows_missing_lane_metadata": int(filtering_meta.get("rows_missing_lane_metadata", 0)),
        **readiness,
        "features": x_cols,
        "feature_set_hash": feat_hash,
        "excluded_columns": sorted(excluded_effective),
        "model_path": str(MODEL_PATH),
        "filtering_meta": filtering_meta,
        "validation_split": split_meta,
        "model_selection_metric": selected.selection_metric,
        "model_selection_score": selected.selection_score,
        "candidate_summaries": {candidate.name: candidate.summary() for candidate in candidates},
        "candidate_errors": candidate_errors,
        "outcome_sample_types": list(OUTCOME_SAMPLE_TYPES),
        "feature_signal_top": final_feature_signal[:15],
    }
    lane_thresholds = None
    try:
        segment_report = build_segment_report(selected.val_preds, threshold=tune_result.get("picked"))
        lane_thresholds = write_segment_outputs(segment_report)
        meta_payload["thresholds_by_lane"] = lane_thresholds
    except Exception as exc:
        print(f"[SEG] segment_report omitido: {exc}")
    artifact = write_candidate(
        model=final_model,
        meta=meta_payload,
        thresholds=lane_thresholds,
        val_preds_path=VAL_PREDS_CSV,
        segment_report_path=SEGMENT_JSON,
    )
    promotion_status: dict[str, Any] = {
        "attempted": True,
        "promoted": False,
        "candidate_model_id": artifact.model_id,
        "candidate_model_path": str(artifact.model_path),
        "candidate_meta_path": str(artifact.meta_path),
        "reason": "not_attempted",
    }
    try:
        registry_payload = promote_candidate(artifact, active_model_path=MODEL_PATH)
        promotion_status.update(
            {
                "promoted": True,
                "reason": "promoted",
                "registry": registry_payload,
            }
        )
    except RuntimeError as exc:
        if "STRATEGY_OPTIMIZATION_LOCK=true blocks model promotion" not in str(exc):
            raise
        promotion_status["reason"] = str(exc)
        print(f"[ML] Promocion activa omitida: {exc}")

    train_status = _status_payload(
        status="trained",
        quality=quality,
        feature_hash=feat_hash,
        features=x_cols,
        excluded_effective=excluded_effective,
        extra={
            "last_train_attempt_at": attempted_at,
            "last_train_status": "trained",
            "training_scope": training_scope,
            "bootstrap_used": bool(bootstrap_used),
            "strict_productive_dataset": strict_summary,
            "bootstrap_candidate_dataset": bootstrap_summary,
            "eligible_rows": int(quality.rows),
            "eligible_unique_tokens": int(quality.unique_tokens),
            "eligible_positives": int(quality.positives),
            "holdout_rows": int(quality.holdout_rows),
            "rows_missing_lane_metadata": int(filtering_meta.get("rows_missing_lane_metadata", 0)),
            "skip_reasons": [],
            **readiness,
            "filtering_meta": filtering_meta,
            "split_meta": split_meta,
            "selected_model_name": selected.name,
            "model_family": selected.model_family,
            "auc_forward_or_cv_mean": selected.auc_mean,
            "auc_pr_forward_or_cv_mean": selected.ap_mean,
            "precision_at_k_pct": float(PREC_AT_K_PCT),
            "precision_at_k_val": selected.precision_at_k,
            "threshold_result": tune_result,
            "promotion": promotion_status,
            "candidate_summaries": {candidate.name: candidate.summary() for candidate in candidates},
            "candidate_errors": candidate_errors,
            "outcome_sample_types": list(OUTCOME_SAMPLE_TYPES),
        },
    )
    _write_json(TRAIN_STATUS_JSON, train_status)

    print(
        "[VAL] model={} AUC={:.4f} AP={:.4f} Prec@{:.0f}%={:.4f} picked_thr={} objective={} activation_ready={}".format(
            selected.name,
            selected.auc_mean,
            selected.ap_mean,
            PREC_AT_K_PCT * 100.0,
            selected.precision_at_k,
            tune_result.get("picked"),
            tune_result.get("objective_applied"),
            tune_result.get("activation_ready"),
        )
    )
    if promotion_status.get("promoted"):
        print(f"[ML] Modelo activo + meta guardados en {MODEL_PATH}")
    else:
        print(f"[ML] Candidato guardado en {artifact.model_path} (activo bloqueado por lock)")

    return TrainResult(
        trained=True,
        status="trained",
        dataset_quality=quality,
        selection_metric=selected.selection_metric,
        selection_score=selected.selection_score,
        model_path=str(MODEL_PATH if promotion_status.get("promoted") else artifact.model_path),
        meta_path=str(META_PATH if promotion_status.get("promoted") else artifact.meta_path),
        val_preds_path=str(VAL_PREDS_CSV),
        recommended_threshold_path=str(RECOMMENDED_JSON),
    )


if __name__ == "__main__":
    train_and_save()
