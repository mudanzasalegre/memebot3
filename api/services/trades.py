from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import HTTPException

from analytics.audit import build_trade_consistency
from analytics.reporting import load_positions_frame, load_tokens_frame
from api.repositories.filesystem import load_jsonl_rows, parse_timestamp
from api.schemas.common import Envelope, SourceStatus
from api.services.common import build_envelope, to_jsonable
from api.services.events import _normalize_event
from api.services.sources import json_status, jsonl_status, latest_parquet_status, paper_portfolio_status, sqlite_table_status
from api.settings import APISettings
from trade_pnl import summarize_trade, total_pnl_pct_from_record


def _scorecard_status_with_consistency(status: SourceStatus, consistency: dict[str, Any]) -> SourceStatus:
    if status.status in {"missing", "error", "empty"}:
        return status
    lag_rows = consistency.get("lag_rows")
    scorecard_stale = bool(consistency.get("scorecard_stale_vs_latest_close"))
    if not scorecard_stale and lag_rows in (None, 0):
        return status
    detail = (
        f"db_closed={consistency.get('db_closed_rows')} "
        f"scorecard_live_closed={consistency.get('scorecard_live_closed')} "
        f"lag_rows={lag_rows}"
    )
    return status.model_copy(update={"status": "stale", "detail": detail})


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


def _effective_symbol(row: pd.Series) -> Any:
    symbol = row.get("symbol")
    if symbol is None or pd.isna(symbol):
        symbol = row.get("token_symbol")
    return symbol


def _safe_bool(value: Any) -> bool | None:
    if value is None or pd.isna(value):
        return None
    return bool(value)


def _effective_outcome(row: pd.Series, pnl_pct: float) -> str | None:
    raw = row.get("outcome")
    if raw is not None and not pd.isna(raw):
        return str(raw)
    exit_reason_value = row.get("exit_reason")
    exit_reason = "" if exit_reason_value is None or pd.isna(exit_reason_value) else str(exit_reason_value)
    if "TIME" in exit_reason:
        return "fail_timeout"
    if pnl_pct > 0.0:
        return "win"
    if pnl_pct < 0.0:
        return "fail"
    return None


def _trade_totals_payload(row: pd.Series) -> dict[str, Any]:
    totals = summarize_trade(
        entry_qty=row.get("entry_qty"),
        remaining_qty=row.get("qty"),
        buy_price_usd=row.get("buy_price_usd"),
        entry_notional_usd=row.get("entry_notional_usd"),
        realized_qty=row.get("realized_qty"),
        realized_proceeds_usd=row.get("realized_proceeds_usd"),
        close_price_usd=row.get("close_price_usd"),
    )
    hold_minutes = None
    opened_at = row.get("opened_at")
    closed_at = row.get("closed_at")
    if isinstance(opened_at, pd.Timestamp) and isinstance(closed_at, pd.Timestamp):
        hold_minutes = (closed_at - opened_at).total_seconds() / 60.0

    pnl_pct = float(total_pnl_pct_from_record(row))
    return {
        "entry_qty": totals.entry_qty,
        "remaining_qty": totals.remaining_qty,
        "realized_qty": totals.realized_qty,
        "realized_proceeds_usd": totals.realized_proceeds_usd,
        "realized_cost_usd": totals.realized_cost_usd,
        "realized_pnl_usd": totals.realized_pnl_usd,
        "unrealized_proceeds_usd": totals.unrealized_proceeds_usd,
        "unrealized_cost_usd": totals.unrealized_cost_usd,
        "unrealized_pnl_usd": totals.unrealized_pnl_usd,
        "total_proceeds_usd": totals.total_proceeds_usd,
        "total_cost_usd": totals.total_cost_usd,
        "total_pnl_usd": totals.total_pnl_usd,
        "total_pnl_pct": pnl_pct,
        "effective_exit_price_usd": totals.effective_exit_price_usd,
        "hold_minutes": hold_minutes,
        "outcome": _effective_outcome(row, pnl_pct),
    }


def _trade_list_item(row: pd.Series) -> dict[str, Any]:
    computed = _trade_totals_payload(row)
    return {
        "trade_id": int(row.get("id")),
        "address": row.get("address"),
        "symbol": _effective_symbol(row),
        "opened_at": row.get("opened_at"),
        "closed_at": row.get("closed_at"),
        "entry_regime": row.get("entry_regime"),
        "exit_reason": row.get("exit_reason"),
        "outcome": computed["outcome"],
        "buy_amount_sol": row.get("buy_amount_sol"),
        "size_bucket": row.get("size_bucket"),
        "size_multiplier": row.get("size_multiplier"),
        "buy_price_usd": row.get("buy_price_usd"),
        "close_price_usd": row.get("close_price_usd"),
        "effective_exit_price_usd": computed["effective_exit_price_usd"],
        "total_pnl_usd": computed["total_pnl_usd"],
        "total_pnl_pct": computed["total_pnl_pct"],
        "highest_pnl_pct": row.get("highest_pnl_pct"),
        "max_pnl_pct_seen": row.get("max_pnl_pct_seen"),
        "partial_taken": bool(_safe_bool(row.get("partial_taken"))),
        "runner_exit_profile": row.get("runner_exit_profile"),
        "price_source_at_buy": row.get("price_source_at_buy"),
        "price_source_at_close": row.get("price_source_at_close"),
    }


def _trade_payload(row: pd.Series) -> dict[str, Any]:
    payload = {
        "trade_id": int(row.get("id")),
        "token_mint": row.get("token_mint"),
        "address": row.get("address"),
        "symbol": _effective_symbol(row),
        "qty": int(row.get("qty", 0) or 0),
        "entry_qty": row.get("entry_qty"),
        "buy_price_usd": row.get("buy_price_usd"),
        "price_source_at_buy": row.get("price_source_at_buy"),
        "buy_tx_sig": row.get("buy_tx_sig"),
        "entry_regime": row.get("entry_regime"),
        "size_bucket": row.get("size_bucket"),
        "size_multiplier": row.get("size_multiplier"),
        "buy_amount_sol": row.get("buy_amount_sol"),
        "entry_notional_usd": row.get("entry_notional_usd"),
        "entry_ai_proba": row.get("entry_ai_proba"),
        "entry_score_total": row.get("entry_score_total"),
        "buy_liquidity_usd": row.get("buy_liquidity_usd"),
        "buy_market_cap_usd": row.get("buy_market_cap_usd"),
        "buy_volume_24h_usd": row.get("buy_volume_24h_usd"),
        "peak_price_usd": row.get("peak_price_usd"),
        "peak_price": row.get("peak_price"),
        "opened_at": row.get("opened_at"),
        "closed": bool(_safe_bool(row.get("closed"))),
        "closed_at": row.get("closed_at"),
        "close_price_usd": row.get("close_price_usd"),
        "exit_tx_sig": row.get("exit_tx_sig"),
        "price_source_at_close": row.get("price_source_at_close"),
        "exit_reason": row.get("exit_reason"),
        "outcome": row.get("outcome"),
        "highest_pnl_pct": row.get("highest_pnl_pct"),
        "max_pnl_pct_seen": row.get("max_pnl_pct_seen"),
        "realized_qty": row.get("realized_qty"),
        "realized_proceeds_usd": row.get("realized_proceeds_usd"),
        "realized_cost_usd": row.get("realized_cost_usd"),
        "realized_pnl_usd": row.get("realized_pnl_usd"),
        "effective_exit_price_usd": row.get("effective_exit_price_usd"),
        "total_pnl_usd": row.get("total_pnl_usd"),
        "total_pnl_pct": row.get("total_pnl_pct"),
        "partial_taken": bool(_safe_bool(row.get("partial_taken"))),
        "partial_count": row.get("partial_count"),
        "first_partial_at": row.get("first_partial_at"),
        "last_partial_at": row.get("last_partial_at"),
        "last_partial_qty": row.get("last_partial_qty"),
        "last_partial_price_usd": row.get("last_partial_price_usd"),
        "runner_exit_profile": row.get("runner_exit_profile"),
        "time_to_partial_sec": row.get("time_to_partial_sec"),
        "time_to_peak_sec": row.get("time_to_peak_sec"),
        "peak_after_partial_pct": row.get("peak_after_partial_pct"),
        "exit_from_peak_giveback_pct": row.get("exit_from_peak_giveback_pct"),
    }
    return payload


def _sort_closed_trades(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    sorted_frame = frame.copy()
    if "id" in sorted_frame.columns:
        sorted_frame["id"] = pd.to_numeric(sorted_frame["id"], errors="coerce")
    return sorted_frame.sort_values(["closed_at", "id"], ascending=[False, False], na_position="last", kind="mergesort")


def _closed_trades_summary(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {
            "closed_count": 0,
            "win_rate_pct": None,
            "avg_pnl_pct": None,
            "median_pnl_pct": None,
            "total_pnl_usd": 0.0,
            "latest_closed_at": None,
        }

    pnl_pct = frame.apply(total_pnl_pct_from_record, axis=1)
    pnl_usd = frame.apply(lambda row: summarize_trade(
        entry_qty=row.get("entry_qty"),
        remaining_qty=row.get("qty"),
        buy_price_usd=row.get("buy_price_usd"),
        entry_notional_usd=row.get("entry_notional_usd"),
        realized_qty=row.get("realized_qty"),
        realized_proceeds_usd=row.get("realized_proceeds_usd"),
        close_price_usd=row.get("close_price_usd"),
    ).total_pnl_usd, axis=1)
    latest_closed_at = frame.get("closed_at")
    latest_value = latest_closed_at.max() if latest_closed_at is not None else None
    return {
        "closed_count": int(len(frame)),
        "win_rate_pct": float((pd.to_numeric(pnl_pct, errors="coerce").gt(0).mean() * 100.0)) if len(frame) else None,
        "avg_pnl_pct": float(pd.to_numeric(pnl_pct, errors="coerce").mean()) if len(frame) else None,
        "median_pnl_pct": float(pd.to_numeric(pnl_pct, errors="coerce").median()) if len(frame) else None,
        "total_pnl_usd": float(pd.to_numeric(pnl_usd, errors="coerce").sum()),
        "latest_closed_at": latest_value,
    }


def _token_payload(row: pd.Series) -> dict[str, Any]:
    if "token_symbol" not in row.index:
        return {}
    return {
        "address": row.get("address"),
        "symbol": row.get("token_symbol"),
        "name": row.get("token_name"),
        "created_at": row.get("token_created_at"),
        "liquidity_usd": row.get("token_liquidity_usd"),
        "volume_24h_usd": row.get("token_volume_24h_usd"),
        "market_cap_usd": row.get("token_market_cap_usd"),
        "holders": row.get("token_holders"),
        "rug_score": row.get("token_rug_score"),
        "cluster_bad": _safe_bool(row.get("token_cluster_bad")),
        "social_ok": _safe_bool(row.get("token_social_ok")),
        "trend": row.get("token_trend"),
        "insider_sig": _safe_bool(row.get("token_insider_sig")),
        "score_total": row.get("token_score_total"),
        "dex_id": row.get("token_dex_id"),
        "discovered_via": row.get("token_discovered_via"),
        "discovered_at": row.get("token_discovered_at"),
    }


def _trade_frame_row(settings: APISettings, trade_id: int) -> pd.Series:
    frame = _merged_positions_tokens(settings)
    if frame.empty:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

    match = frame[frame["id"] == int(trade_id)]
    if match.empty:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")
    return match.iloc[0]


def _filter_closed_trades(
    frame: pd.DataFrame,
    *,
    outcome: str | None,
    exit_reason: str | None,
    entry_regime: str | None,
) -> pd.DataFrame:
    filtered = frame.copy()
    closed_series = filtered.get("closed", pd.Series(0, index=filtered.index)).fillna(0).astype(int)
    filtered = filtered[closed_series == 1].copy()

    if exit_reason:
        filtered = filtered[filtered["exit_reason"].astype("string") == exit_reason].copy()
    if entry_regime:
        filtered = filtered[filtered["entry_regime"].astype("string") == entry_regime].copy()
    if outcome:
        effective_outcomes = filtered.apply(
            lambda row: _effective_outcome(row, float(total_pnl_pct_from_record(row))),
            axis=1,
        )
        filtered = filtered[effective_outcomes.astype("string") == outcome].copy()
    return filtered


def _apply_closed_trade_cursor(
    frame: pd.DataFrame,
    *,
    before_ts: str | None,
    before_id: int | None,
) -> pd.DataFrame:
    filtered = frame.copy()
    if not before_ts:
        return filtered
    before_dt = parse_timestamp(before_ts)
    if before_dt is None or "closed_at" not in filtered.columns:
        return filtered
    if before_id is not None and "id" in filtered.columns:
        id_series = pd.to_numeric(filtered["id"], errors="coerce")
        return filtered[
            (filtered["closed_at"] < before_dt)
            | ((filtered["closed_at"] == before_dt) & id_series.lt(int(before_id)))
        ].copy()
    return filtered[filtered["closed_at"] < before_dt].copy()


def _event_timeline(path: Path, *, address: str) -> list[dict[str, Any]]:
    rows = load_jsonl_rows(path)
    filtered = []
    for index, row in enumerate(rows):
        if str(row.get("address") or "") != address:
            continue
        ts = parse_timestamp(row.get("ts_utc"))
        filtered.append((ts, _normalize_event(row, index)))
    filtered.sort(
        key=lambda pair: pair[0] or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    )
    return [item for _, item in filtered]


def _read_feature_rows_for_address(settings: APISettings, *, address: str) -> pd.DataFrame:
    files = sorted(settings.features_dir.glob("features_*.parquet"))
    frames: list[pd.DataFrame] = []
    for path in files:
        try:
            frame = pd.read_parquet(path)
        except Exception:
            continue
        if "address" not in frame.columns:
            continue
        subset = frame[frame["address"].astype("string") == address].copy()
        if subset.empty:
            continue
        subset["_source_file"] = path.name
        frames.append(subset)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    for column in ("timestamp", "ts"):
        if column in combined.columns:
            combined[column] = pd.to_datetime(combined[column], utc=True, errors="coerce")
    return combined


def _nearest_entry_snapshot(settings: APISettings, *, address: str, opened_at: Any) -> dict[str, Any] | None:
    frame = _read_feature_rows_for_address(settings, address=address)
    if frame.empty:
        return None

    opened_ts = pd.to_datetime(opened_at, utc=True, errors="coerce")
    snapshot_ts_col = "timestamp" if "timestamp" in frame.columns else ("ts" if "ts" in frame.columns else None)
    if snapshot_ts_col is not None:
        frame["_snapshot_at"] = pd.to_datetime(frame[snapshot_ts_col], utc=True, errors="coerce")
    else:
        frame["_snapshot_at"] = pd.NaT

    if snapshot_ts_col is not None and pd.notna(opened_ts):
        deltas = (frame["_snapshot_at"] - opened_ts).abs()
        deltas = deltas.fillna(pd.Timedelta.max)
        row = frame.iloc[int(deltas.argmin())]
    else:
        row = frame.iloc[0]

    payload = {key: row[key] for key in frame.columns if not key.startswith("_")}
    payload["source_file"] = row.get("_source_file")
    payload["snapshot_at"] = row.get("_snapshot_at")
    return to_jsonable(payload)


def _derive_replay_metrics(
    row: pd.Series,
    runtime_timeline: list[dict[str, Any]],
    research_timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    first_seen_at: dt.datetime | None = None
    for item in runtime_timeline:
        payload = item.get("payload") or {}
        first_seen_raw = payload.get("first_seen_epoch_s")
        candidate = parse_timestamp(first_seen_raw)
        if candidate is None:
            candidate = parse_timestamp(item.get("ts_utc"))
        if candidate is None:
            continue
        if first_seen_at is None or candidate < first_seen_at:
            first_seen_at = candidate

    if first_seen_at is None:
        all_times = [
            parse_timestamp(item.get("ts_utc"))
            for item in runtime_timeline + research_timeline
        ]
        all_times = [item for item in all_times if item is not None]
        first_seen_at = min(all_times) if all_times else None

    opened_at = row.get("opened_at")
    closed_at = row.get("closed_at")
    minutes_first_seen_to_buy = None
    hold_minutes = None
    if isinstance(opened_at, pd.Timestamp) and first_seen_at is not None:
        minutes_first_seen_to_buy = (opened_at.to_pydatetime() - first_seen_at).total_seconds() / 60.0
    if isinstance(opened_at, pd.Timestamp) and isinstance(closed_at, pd.Timestamp):
        hold_minutes = (closed_at - opened_at).total_seconds() / 60.0

    return {
        "first_seen_at": first_seen_at,
        "minutes_first_seen_to_buy": minutes_first_seen_to_buy,
        "hold_minutes": hold_minutes,
    }


def get_closed_trades_envelope(
    settings: APISettings,
    *,
    limit: int = 50,
    before_ts: str | None = None,
    before_id: int | None = None,
    outcome: str | None = None,
    exit_reason: str | None = None,
    entry_regime: str | None = None,
) -> Envelope:
    frame = _merged_positions_tokens(settings)
    filtered = pd.DataFrame()
    summary = _closed_trades_summary(filtered)
    consistency = build_trade_consistency(
        db_path=settings.db_path,
        paper_portfolio_path=settings.paper_portfolio_path,
        research_scorecard_path=settings.research_scorecard_json,
    )
    next_before_ts = None
    next_before_id = None
    has_more = False
    if frame.empty:
        items: list[dict[str, Any]] = []
    else:
        filtered = _filter_closed_trades(
            frame,
            outcome=outcome,
            exit_reason=exit_reason,
            entry_regime=entry_regime,
        )
        filtered = _sort_closed_trades(filtered)
        summary = _closed_trades_summary(filtered)
        page_source = _apply_closed_trade_cursor(filtered, before_ts=before_ts, before_id=before_id)
        page = page_source.head(limit).copy()
        has_more = bool(len(page_source) > len(page))
        if has_more and not page.empty:
            last_row = page.iloc[-1]
            next_before_ts = last_row.get("closed_at")
            trade_id = last_row.get("id")
            try:
                next_before_id = int(trade_id) if trade_id is not None and not pd.isna(trade_id) else None
            except Exception:
                next_before_id = None
        items = [_trade_list_item(row) for _, row in page.iterrows()]

    scorecard_status = _scorecard_status_with_consistency(
        json_status(
            source_key="metrics.research_scorecard",
            path=settings.research_scorecard_json,
            generated_field="generated_at_utc",
            optional=True,
            empty_when_missing=False,
        ),
        consistency,
    )
    statuses: list[SourceStatus] = [
        sqlite_table_status(settings, table="positions", source_key="sqlite.positions"),
        sqlite_table_status(settings, table="tokens", source_key="sqlite.tokens"),
        paper_portfolio_status(settings),
        scorecard_status,
    ]
    data = {
        "items": [to_jsonable(item) for item in items],
        "count": len(items),
        "page_count": len(items),
        "total_count": int(len(filtered)),
        "has_more": has_more,
        "next_before_ts": to_jsonable(next_before_ts),
        "next_before_id": next_before_id,
        "filters": {
            "limit": limit,
            "before_ts": before_ts,
            "before_id": before_id,
            "outcome": outcome,
            "exit_reason": exit_reason,
            "entry_regime": entry_regime,
        },
        "summary": to_jsonable(summary),
        "consistency": to_jsonable(consistency),
    }
    return build_envelope(
        data,
        source_status=statuses,
        empty=bool(int(len(filtered)) == 0),
        degraded=any(item.status in {"missing", "error"} for item in statuses),
        stale=any(item.status == "stale" for item in statuses),
    )


def get_trade_detail_envelope(settings: APISettings, *, trade_id: int) -> Envelope:
    row = _trade_frame_row(settings, trade_id)
    trade = _trade_payload(row)
    token = _token_payload(row)
    computed = _trade_totals_payload(row)
    execution = {
        "buy_tx_sig": row.get("buy_tx_sig"),
        "exit_tx_sig": row.get("exit_tx_sig"),
        "price_source_at_buy": row.get("price_source_at_buy"),
        "price_source_at_close": row.get("price_source_at_close"),
        "partial_taken": bool(_safe_bool(row.get("partial_taken"))),
        "partial_count": row.get("partial_count"),
        "first_partial_at": row.get("first_partial_at"),
        "last_partial_at": row.get("last_partial_at"),
        "last_partial_qty": row.get("last_partial_qty"),
        "last_partial_price_usd": row.get("last_partial_price_usd"),
    }

    statuses: list[SourceStatus] = [
        sqlite_table_status(settings, table="positions", source_key="sqlite.positions"),
        sqlite_table_status(settings, table="tokens", source_key="sqlite.tokens"),
    ]
    data = {
        "trade": to_jsonable(trade),
        "token": to_jsonable(token),
        "computed": to_jsonable(computed),
        "execution": to_jsonable(execution),
    }
    return build_envelope(
        data,
        source_status=statuses,
        empty=False,
        degraded=any(item.status in {"missing", "error"} for item in statuses),
        stale=any(item.status == "stale" for item in statuses),
    )


def get_trade_replay_envelope(settings: APISettings, *, trade_id: int) -> Envelope:
    row = _trade_frame_row(settings, trade_id)
    trade = _trade_payload(row)
    token = _token_payload(row)
    runtime_timeline = _event_timeline(settings.runtime_events_path, address=str(row.get("address") or ""))
    research_timeline = _event_timeline(settings.research_events_path, address=str(row.get("address") or ""))
    entry_snapshot = _nearest_entry_snapshot(
        settings,
        address=str(row.get("address") or ""),
        opened_at=row.get("opened_at"),
    )
    derived = _derive_replay_metrics(row, runtime_timeline, research_timeline)

    statuses: list[SourceStatus] = [
        sqlite_table_status(settings, table="positions", source_key="sqlite.positions"),
        sqlite_table_status(settings, table="tokens", source_key="sqlite.tokens"),
        jsonl_status(source_key="metrics.runtime_events", path=settings.runtime_events_path),
        jsonl_status(source_key="metrics.research_events", path=settings.research_events_path),
        latest_parquet_status(settings),
    ]
    data = {
        "trade": to_jsonable(trade),
        "token": to_jsonable(token),
        "entry_snapshot": entry_snapshot,
        "runtime_timeline": to_jsonable(runtime_timeline),
        "research_timeline": to_jsonable(research_timeline),
        "derived": to_jsonable(derived),
    }
    return build_envelope(
        data,
        source_status=statuses,
        empty=False,
        degraded=any(item.status in {"missing", "error"} for item in statuses),
        stale=any(item.status == "stale" for item in statuses),
    )
