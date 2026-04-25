from __future__ import annotations

from typing import Any

import pandas as pd

from analytics.reporting import load_positions_frame, load_tokens_frame

from api.schemas.common import Envelope, SourceStatus
from api.services.common import build_envelope, to_jsonable
from api.services.sources import sqlite_table_status
from api.settings import APISettings


def _normalize_datetime_columns(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
    out = frame.copy()
    for column in columns:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    return out


def _merged_positions_tokens(settings: APISettings) -> pd.DataFrame:
    positions = load_positions_frame(settings.db_path)
    if positions.empty:
        return pd.DataFrame()
    positions = _normalize_datetime_columns(
        positions,
        ("opened_at", "closed_at", "first_partial_at", "last_partial_at"),
    )

    tokens = load_tokens_frame(settings.db_path)
    if tokens.empty:
        return positions

    tokens = _normalize_datetime_columns(tokens, ("created_at", "discovered_at"))
    rename_map = {
        column: f"token_{column}"
        for column in tokens.columns
        if column != "address"
    }
    tokens = tokens.rename(columns=rename_map)
    return positions.merge(tokens, on="address", how="left")


def _position_row_payload(row: pd.Series) -> dict[str, Any]:
    symbol = row.get("symbol")
    if symbol is None or pd.isna(symbol):
        symbol = row.get("token_symbol")

    return {
        "trade_id": int(row.get("id")),
        "address": row.get("address"),
        "symbol": symbol,
        "opened_at": row.get("opened_at"),
        "qty": int(row.get("qty", 0) or 0),
        "buy_price_usd": row.get("buy_price_usd"),
        "buy_amount_sol": row.get("buy_amount_sol"),
        "entry_regime": row.get("entry_regime"),
        "size_bucket": row.get("size_bucket"),
        "size_multiplier": row.get("size_multiplier"),
        "entry_ai_proba": row.get("entry_ai_proba"),
        "entry_score_total": row.get("entry_score_total"),
        "buy_liquidity_usd": row.get("buy_liquidity_usd"),
        "buy_market_cap_usd": row.get("buy_market_cap_usd"),
        "peak_price_usd": row.get("peak_price_usd"),
        "highest_pnl_pct": row.get("highest_pnl_pct"),
        "max_pnl_pct_seen": row.get("max_pnl_pct_seen"),
        "runner_exit_profile": row.get("runner_exit_profile"),
        "time_to_partial_sec": row.get("time_to_partial_sec"),
        "time_to_peak_sec": row.get("time_to_peak_sec"),
        "peak_after_partial_pct": row.get("peak_after_partial_pct"),
    }


def get_open_positions_envelope(
    settings: APISettings,
    *,
    address: str | None = None,
    limit: int = 50,
) -> Envelope:
    frame = _merged_positions_tokens(settings)
    if frame.empty:
        items: list[dict[str, Any]] = []
    else:
        closed_series = frame.get("closed", pd.Series(0, index=frame.index)).fillna(0).astype(int)
        filtered = frame[closed_series == 0].copy()
        if address:
            filtered = filtered[filtered["address"].astype("string") == address].copy()
        filtered = filtered.sort_values("opened_at", ascending=False, na_position="last")
        items = [_position_row_payload(row) for _, row in filtered.head(limit).iterrows()]

    statuses: list[SourceStatus] = [
        sqlite_table_status(settings, table="positions", source_key="sqlite.positions"),
        sqlite_table_status(settings, table="tokens", source_key="sqlite.tokens"),
    ]
    data = {
        "items": [to_jsonable(item) for item in items],
        "count": len(items),
        "filters": {
            "address": address,
            "limit": limit,
        },
    }
    return build_envelope(
        data,
        source_status=statuses,
        empty=not items,
        degraded=any(item.status in {"missing", "error"} for item in statuses),
        stale=any(item.status == "stale" for item in statuses),
    )
