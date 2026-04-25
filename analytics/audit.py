from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from analytics.reporting import build_baseline_snapshot, load_positions_frame, summarize_edge
from config.config import CFG, PROJECT_ROOT
from trade_pnl import summarize_trade, total_pnl_pct_from_record


UTC = dt.timezone.utc


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return _iso_or_none(value)
    if isinstance(value, dt.datetime):
        return _iso_or_none(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return _json_value(value.item())
        except Exception:
            pass
    return value


def _round(value: Any, digits: int = 6) -> Any:
    try:
        if value is None or pd.isna(value):
            return None
        return round(float(value), digits)
    except Exception:
        return value


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        if value.tzinfo is None:
            value = value.tz_localize("UTC")
        return value.isoformat()
    if isinstance(value, dt.datetime):
        value = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return None


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(value), tz=UTC)
        except Exception:
            return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _read_json_file(path: Path | None) -> Any | None:
    if path is None or not Path(path).exists():
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_jsonl_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not Path(path).exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with Path(path).open("r", encoding="utf-8", errors="ignore") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except Exception:
        return []
    return rows


def _normalize_regime(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"pump_early", "pump", "pumpfun", "pump_fun"}:
        return "pump_early"
    if raw in {"revival", "revive", "revived"}:
        return "revival"
    return "dex_mature"


def _file_mtime(path: Path | None) -> dt.datetime | None:
    if path is None or not Path(path).exists():
        return None
    try:
        return dt.datetime.fromtimestamp(Path(path).stat().st_mtime, tz=UTC)
    except Exception:
        return None


def _compute_total_pnl_usd(row: pd.Series) -> float:
    totals = summarize_trade(
        entry_qty=row.get("entry_qty"),
        remaining_qty=row.get("qty"),
        buy_price_usd=row.get("buy_price_usd"),
        entry_notional_usd=row.get("entry_notional_usd"),
        realized_qty=row.get("realized_qty"),
        realized_proceeds_usd=row.get("realized_proceeds_usd"),
        close_price_usd=row.get("close_price_usd"),
    )
    return float(totals.total_pnl_usd)


def _compute_total_cost_usd(row: pd.Series) -> float:
    totals = summarize_trade(
        entry_qty=row.get("entry_qty"),
        remaining_qty=row.get("qty"),
        buy_price_usd=row.get("buy_price_usd"),
        entry_notional_usd=row.get("entry_notional_usd"),
        realized_qty=row.get("realized_qty"),
        realized_proceeds_usd=row.get("realized_proceeds_usd"),
        close_price_usd=row.get("close_price_usd"),
    )
    return float(totals.total_cost_usd)


def load_closed_positions_context(db_path: Path | None = None) -> pd.DataFrame:
    frame = load_positions_frame(db_path=db_path)
    if frame.empty:
        return frame
    out = frame.copy()
    out["opened_at"] = pd.to_datetime(out.get("opened_at"), utc=True, errors="coerce")
    out["closed_at"] = pd.to_datetime(out.get("closed_at"), utc=True, errors="coerce")
    out = out[out.get("closed", 0).fillna(0).astype(int) == 1].copy()
    if out.empty:
        return out
    out["computed_total_pnl_pct"] = out.apply(total_pnl_pct_from_record, axis=1)
    out["computed_total_pnl_usd"] = out.apply(_compute_total_pnl_usd, axis=1)
    out["computed_total_cost_usd"] = out.apply(_compute_total_cost_usd, axis=1)
    out["hold_minutes"] = (out["closed_at"] - out["opened_at"]).dt.total_seconds() / 60.0
    out["giveback_pct"] = (
        pd.to_numeric(out.get("highest_pnl_pct"), errors="coerce").fillna(0.0)
        - pd.to_numeric(out["computed_total_pnl_pct"], errors="coerce").fillna(0.0)
    )
    return out


def build_trade_consistency(
    *,
    db_path: Path | None = None,
    paper_portfolio_path: Path | None = None,
    research_scorecard_path: Path | None = None,
    closed_positions: pd.DataFrame | None = None,
) -> dict[str, Any]:
    closed = closed_positions.copy() if closed_positions is not None else load_closed_positions_context(db_path=db_path)

    db_closed_rows = int(len(closed))
    db_total_pnl_usd = (
        _round(pd.to_numeric(closed.get("computed_total_pnl_usd"), errors="coerce").sum(), 8) if not closed.empty else 0.0
    )
    latest_closed_at = _iso_or_none(closed["closed_at"].max()) if not closed.empty and "closed_at" in closed.columns else None

    paper_path = paper_portfolio_path if paper_portfolio_path is not None else (PROJECT_ROOT / "data" / "paper_portfolio.json")
    paper_portfolio = _read_json_file(paper_path)
    paper_closed_rows = None
    paper_total_pnl_usd = None
    if isinstance(paper_portfolio, dict):
        paper_rows = list(paper_portfolio.values())
        paper_closed = [row for row in paper_rows if bool((row or {}).get("closed"))]
        paper_closed_rows = int(len(paper_closed))
        paper_total_pnl_usd = _round(sum(float((row or {}).get("total_pnl_usd") or 0.0) for row in paper_closed), 8)

    scorecard_path = research_scorecard_path if research_scorecard_path is not None else (PROJECT_ROOT / "data" / "metrics" / "research_scorecard.json")
    scorecard = _read_json_file(scorecard_path)
    scorecard_live_closed = None
    scorecard_generated_at = None
    if isinstance(scorecard, dict):
        raw_live_closed = scorecard.get("live_closed")
        scorecard_live_closed = int(raw_live_closed) if raw_live_closed is not None else None
        scorecard_generated_at = _iso_or_none(_parse_timestamp(scorecard.get("generated_at_utc")))

    lag_rows = int(db_closed_rows - scorecard_live_closed) if scorecard_live_closed is not None else None
    paper_matches_db = bool(paper_closed_rows == db_closed_rows) if paper_closed_rows is not None else None
    pnl_matches_db = (
        bool(abs(float(paper_total_pnl_usd) - float(db_total_pnl_usd or 0.0)) < 1e-8) if paper_total_pnl_usd is not None else None
    )

    is_consistent = True
    if paper_matches_db is False or pnl_matches_db is False:
        is_consistent = False
    if scorecard_live_closed is not None and scorecard_live_closed != db_closed_rows:
        is_consistent = False

    latest_closed_dt = _parse_timestamp(latest_closed_at)
    scorecard_stale_vs_latest_close = False
    if latest_closed_dt is not None and scorecard_generated_at is not None:
        scorecard_stale_vs_latest_close = bool(_parse_timestamp(scorecard_generated_at) < latest_closed_dt)

    return {
        "db_closed_rows": db_closed_rows,
        "paper_closed_rows": paper_closed_rows,
        "scorecard_live_closed": scorecard_live_closed,
        "scorecard_generated_at_utc": scorecard_generated_at,
        "latest_closed_at": latest_closed_at,
        "lag_rows": lag_rows,
        "db_total_pnl_usd": db_total_pnl_usd,
        "paper_total_pnl_usd": paper_total_pnl_usd,
        "paper_matches_db": paper_matches_db,
        "pnl_matches_db": pnl_matches_db,
        "scorecard_stale_vs_latest_close": scorecard_stale_vs_latest_close,
        "is_consistent": bool(is_consistent),
    }


def normalize_candidate_outcomes_frame(events_path: Path | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    path = Path(events_path or (PROJECT_ROOT / "data" / "metrics" / "candidate_outcomes.jsonl"))
    rows = _load_jsonl_rows(path)
    if not rows:
        return pd.DataFrame(), {"rows_in": 0, "rows_out": 0, "ambiguous_bought_dropped": 0}

    frame = pd.DataFrame(rows)
    frame["ts_utc"] = pd.to_datetime(frame.get("ts_utc"), utc=True, errors="coerce")
    if "symbol" in frame.columns:
        frame["symbol"] = frame["symbol"].astype("string").str.replace(r"\s+", " ", regex=True).str.strip()

    entry_regime = frame.get("entry_regime")
    if entry_regime is None:
        entry_regime = pd.Series(pd.NA, index=frame.index, dtype="object")
    regime = frame.get("regime")
    if regime is None:
        regime = pd.Series(pd.NA, index=frame.index, dtype="object")
    discovered_via = frame.get("discovered_via")
    if discovered_via is None:
        discovered_via = pd.Series(pd.NA, index=frame.index, dtype="object")
    frame["entry_regime"] = [
        _normalize_regime(a if pd.notna(a) else b if pd.notna(b) else c)
        for a, b, c in zip(entry_regime, regime, discovered_via)
    ]
    frame["regime"] = frame["entry_regime"]

    def _infer_source(row: pd.Series) -> str:
        raw = row.get("source")
        if raw is not None and not pd.isna(raw) and str(raw).strip():
            return str(raw).strip()
        event_type = str(row.get("event_type") or "")
        if event_type == "candidate_outcome":
            return "research_shadow" if str(row.get("shadow_kind") or "").strip() else "live_trade"
        if event_type == "candidate_partial":
            return "candidate_partial"
        if event_type == "candidate_stage":
            return "candidate_stage"
        if event_type == "candidate_decision":
            return "candidate_decision"
        return "unknown"

    frame["source"] = frame.apply(_infer_source, axis=1)
    if "closed_at" in frame.columns:
        frame["closed_at"] = pd.to_datetime(frame["closed_at"], utc=True, errors="coerce")
    if "opened_at" in frame.columns:
        frame["opened_at"] = pd.to_datetime(frame["opened_at"], utc=True, errors="coerce")

    buy_mask = (
        frame.get("event_type", pd.Series("", index=frame.index)).astype("string").eq("candidate_decision")
        & frame.get("decision_action", pd.Series("", index=frame.index)).astype("string").eq("bought")
    )
    buy_rows = frame[buy_mask].copy()
    deduped_buys = buy_rows.sort_values("ts_utc", kind="mergesort").drop_duplicates(subset=["address"], keep="first")
    ambiguous_bought_dropped = int(len(buy_rows) - len(deduped_buys))
    if ambiguous_bought_dropped > 0:
        frame = pd.concat([frame[~buy_mask].copy(), deduped_buys], ignore_index=True, sort=False)
        frame = frame.sort_values("ts_utc", kind="mergesort").reset_index(drop=True)

    stats = {
        "rows_in": int(len(rows)),
        "rows_out": int(len(frame)),
        "ambiguous_bought_dropped": ambiguous_bought_dropped,
        "event_type_counts": dict(Counter(frame.get("event_type", pd.Series(dtype="string")).astype("string"))),
        "source_counts": dict(Counter(frame.get("source", pd.Series(dtype="string")).astype("string"))),
    }
    return frame, stats


def write_normalized_candidate_outcomes(
    *,
    events_path: Path | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    frame, stats = normalize_candidate_outcomes_frame(events_path=events_path)
    target = Path(output_path or (PROJECT_ROOT / "data" / "metrics" / "candidate_outcomes.normalized.jsonl"))
    target.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for row in frame.to_dict(orient="records"):
        payload: dict[str, Any] = {}
        for key, value in row.items():
            payload[str(key)] = _json_value(value)
        lines.append(json.dumps(payload, ensure_ascii=True))

    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return {"path": str(target), **stats}


def _compute_simple_drawdown(frame: pd.DataFrame, pnl_col: str = "computed_total_pnl_pct") -> float | None:
    if frame.empty or pnl_col not in frame.columns:
        return None
    ordered = frame.sort_values(["closed_at", "id"], kind="mergesort")
    pnl = pd.to_numeric(ordered[pnl_col], errors="coerce").fillna(0.0)
    if pnl.empty:
        return None
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax()
    return _round(drawdown.min(), 3)


def _trade_subset_metrics(frame: pd.DataFrame, *, pnl_pct_col: str = "computed_total_pnl_pct", pnl_usd_col: str = "computed_total_pnl_usd") -> dict[str, Any]:
    if frame.empty:
        return {
            "count": 0,
            "win_rate_pct": None,
            "avg_pnl_pct": None,
            "median_pnl_pct": None,
            "total_pnl_usd": 0.0,
            "simple_max_drawdown_pct_points": None,
        }
    pnl_pct = pd.to_numeric(frame[pnl_pct_col], errors="coerce").fillna(0.0)
    pnl_usd = pd.to_numeric(frame[pnl_usd_col], errors="coerce").fillna(0.0)
    return {
        "count": int(len(frame)),
        "win_rate_pct": _round((pnl_pct.gt(0).mean() * 100.0), 3),
        "avg_pnl_pct": _round(pnl_pct.mean(), 3),
        "median_pnl_pct": _round(pnl_pct.median(), 3),
        "total_pnl_usd": _round(pnl_usd.sum(), 8),
        "simple_max_drawdown_pct_points": _compute_simple_drawdown(frame, pnl_col=pnl_pct_col),
    }


def _merge_live_outcomes(closed_positions: pd.DataFrame, normalized_events: pd.DataFrame) -> pd.DataFrame:
    if closed_positions.empty:
        return closed_positions.copy()
    live_outcomes = normalized_events[
        normalized_events.get("source", pd.Series("", index=normalized_events.index)).astype("string") == "live_trade"
    ].copy()
    if live_outcomes.empty:
        return closed_positions.copy()
    live_outcomes = live_outcomes.sort_values("ts_utc", kind="mergesort").drop_duplicates(subset=["address"], keep="last")
    keep_cols = [
        col for col in (
            "address",
            "source",
            "entry_regime",
            "liquidity_usd",
            "volume_24h_usd",
            "market_cap_usd",
            "price_impact_pct",
            "score_total",
            "queue_attempts",
            "queue_age_minutes",
            "snapshot_missing_fields",
            "coverage_core_fields",
            "ts_utc",
        ) if col in live_outcomes.columns
    ]
    return closed_positions.merge(live_outcomes[keep_cols], on="address", how="left", suffixes=("", "_event"))


def _entry_filter_sweeps(positions: pd.DataFrame) -> dict[str, Any]:
    pump = positions[positions.get("entry_regime", pd.Series("", index=positions.index)).astype("string") == "pump_early"].copy()
    baseline = _trade_subset_metrics(pump)
    if pump.empty:
        return {"baseline": baseline, "best": None, "top_candidates": []}

    candidates: list[dict[str, Any]] = []
    for min_liq in (0.0, 2_000.0, 5_000.0, 10_000.0, 15_000.0):
        for min_vol in (0.0, 10_000.0, 25_000.0, 50_000.0, 100_000.0):
            for max_impact in (None, 10.0, 7.5, 5.0, 2.5):
                for max_missing in (None, 2.0, 1.0, 0.0):
                    subset = pump.copy()
                    if min_liq > 0:
                        subset = subset[pd.to_numeric(subset.get("liquidity_usd"), errors="coerce").fillna(0.0) >= min_liq]
                    if min_vol > 0:
                        subset = subset[pd.to_numeric(subset.get("volume_24h_usd"), errors="coerce").fillna(0.0) >= min_vol]
                    if max_impact is not None:
                        subset = subset[pd.to_numeric(subset.get("price_impact_pct"), errors="coerce").fillna(float("inf")) <= float(max_impact)]
                    if max_missing is not None:
                        subset = subset[pd.to_numeric(subset.get("snapshot_missing_fields"), errors="coerce").fillna(float("inf")) <= float(max_missing)]
                    if len(subset) < 20:
                        continue
                    metrics = _trade_subset_metrics(subset)
                    drawdown = metrics["simple_max_drawdown_pct_points"]
                    base_drawdown = baseline["simple_max_drawdown_pct_points"]
                    candidates.append(
                        {
                            "params": {
                                "min_liquidity_usd": min_liq,
                                "min_volume_24h_usd": min_vol,
                                "max_price_impact_pct": max_impact,
                                "max_snapshot_missing_fields": max_missing,
                            },
                            **metrics,
                            "delta_total_pnl_usd": _round(float(metrics["total_pnl_usd"] or 0.0) - float(baseline["total_pnl_usd"] or 0.0), 8),
                            "drawdown_guardrail_passed": bool(
                                drawdown is not None and base_drawdown is not None and float(drawdown) >= float(base_drawdown)
                            ),
                        }
                    )

    guarded = [row for row in candidates if row["drawdown_guardrail_passed"]]
    ranked = sorted(
        guarded or candidates,
        key=lambda row: (
            float(row["total_pnl_usd"] or -1e9),
            float(row["avg_pnl_pct"] or -1e9),
            float(row["simple_max_drawdown_pct_points"] or -1e9),
        ),
        reverse=True,
    )
    return {"baseline": baseline, "best": ranked[0] if ranked else None, "top_candidates": ranked[:5]}


def _requeue_cap_sweeps(positions: pd.DataFrame, runtime_events_path: Path | None = None) -> dict[str, Any]:
    pump = positions[positions.get("entry_regime", pd.Series("", index=positions.index)).astype("string") == "pump_early"].copy()
    baseline = _trade_subset_metrics(pump)
    runtime_rows = _load_jsonl_rows(runtime_events_path or (PROJECT_ROOT / "data" / "metrics" / "runtime_events.jsonl"))
    if pump.empty or not runtime_rows:
        return {"baseline": baseline, "best": None, "top_candidates": []}

    runtime = pd.DataFrame(runtime_rows)
    runtime["ts_utc"] = pd.to_datetime(runtime.get("ts_utc"), utc=True, errors="coerce")
    requeues = runtime[runtime.get("event_type", pd.Series("", index=runtime.index)).astype("string") == "requeue"].copy()
    buys = runtime[runtime.get("event_type", pd.Series("", index=runtime.index)).astype("string") == "buy"].copy()
    if requeues.empty or buys.empty:
        return {"baseline": baseline, "best": None, "top_candidates": []}

    first_buys = buys.sort_values("ts_utc", kind="mergesort").drop_duplicates(subset=["address"], keep="first")
    requeues = requeues.merge(first_buys[["address", "ts_utc"]].rename(columns={"ts_utc": "buy_ts_utc"}), on="address", how="inner")
    requeues = requeues[requeues["ts_utc"] <= requeues["buy_ts_utc"]].copy()
    if requeues.empty:
        return {"baseline": baseline, "best": None, "top_candidates": []}

    requeues["reason"] = requeues.get("reason", pd.Series("unknown", index=requeues.index)).astype("string").fillna("unknown")
    grouped = requeues.groupby(["address", "reason"]).size().unstack(fill_value=0).reset_index()
    merged = pump.merge(grouped, on="address", how="left").fillna(0)

    candidates: list[dict[str, Any]] = []
    for max_confirm in (0, 1, 2, 3, 4, 5):
        for max_no_liq in (0, 1, 2, 3, 5, 8):
            subset = merged.copy()
            if "strategy:confirm_snapshots" in subset.columns:
                subset = subset[subset["strategy:confirm_snapshots"] <= max_confirm]
            if "no_liq" in subset.columns:
                subset = subset[subset["no_liq"] <= max_no_liq]
            if len(subset) < 20:
                continue
            metrics = _trade_subset_metrics(subset)
            drawdown = metrics["simple_max_drawdown_pct_points"]
            base_drawdown = baseline["simple_max_drawdown_pct_points"]
            candidates.append(
                {
                    "params": {
                        "max_strategy_confirm_snapshots_requeues": max_confirm,
                        "max_no_liq_requeues": max_no_liq,
                    },
                    **metrics,
                    "delta_total_pnl_usd": _round(float(metrics["total_pnl_usd"] or 0.0) - float(baseline["total_pnl_usd"] or 0.0), 8),
                    "drawdown_guardrail_passed": bool(
                        drawdown is not None and base_drawdown is not None and float(drawdown) >= float(base_drawdown)
                    ),
                }
            )

    guarded = [row for row in candidates if row["drawdown_guardrail_passed"]]
    ranked = sorted(
        guarded or candidates,
        key=lambda row: (
            float(row["total_pnl_usd"] or -1e9),
            float(row["avg_pnl_pct"] or -1e9),
            float(row["simple_max_drawdown_pct_points"] or -1e9),
        ),
        reverse=True,
    )
    return {"baseline": baseline, "best": ranked[0] if ranked else None, "top_candidates": ranked[:5]}


def _post_partial_protection_sweeps(positions: pd.DataFrame) -> dict[str, Any]:
    pump = positions[positions.get("entry_regime", pd.Series("", index=positions.index)).astype("string") == "pump_early"].copy()
    baseline = _trade_subset_metrics(pump)
    if pump.empty:
        return {"baseline": baseline, "best": None, "top_candidates": []}

    candidates: list[dict[str, Any]] = []
    for lock_floor in (5.0, 10.0, 15.0, 20.0):
        for giveback_cap in (5.0, 10.0, 15.0, 20.0, 30.0, 40.0):
            scenario = pump.copy()
            actual_pct = pd.to_numeric(scenario.get("computed_total_pnl_pct"), errors="coerce").fillna(0.0)
            partial_mask = scenario.get("partial_taken", pd.Series(0, index=scenario.index)).fillna(0).astype(int).eq(1)
            peak = pd.to_numeric(scenario.get("highest_pnl_pct"), errors="coerce").fillna(0.0)
            protected_pct = peak - float(giveback_cap)
            protected_pct = protected_pct.where(peak >= float(lock_floor), other=actual_pct)
            protected_pct = protected_pct.clip(lower=float(lock_floor))
            scenario["scenario_total_pnl_pct"] = actual_pct.where(~partial_mask, other=pd.concat([actual_pct, protected_pct], axis=1).max(axis=1))
            cost = pd.to_numeric(scenario.get("computed_total_cost_usd"), errors="coerce").fillna(0.0)
            scenario["scenario_total_pnl_usd"] = (cost * scenario["scenario_total_pnl_pct"]) / 100.0
            metrics = _trade_subset_metrics(
                scenario,
                pnl_pct_col="scenario_total_pnl_pct",
                pnl_usd_col="scenario_total_pnl_usd",
            )
            drawdown = metrics["simple_max_drawdown_pct_points"]
            base_drawdown = baseline["simple_max_drawdown_pct_points"]
            candidates.append(
                {
                    "params": {
                        "lock_floor_pct": lock_floor,
                        "max_giveback_after_partial_pct": giveback_cap,
                    },
                    **metrics,
                    "delta_total_pnl_usd": _round(float(metrics["total_pnl_usd"] or 0.0) - float(baseline["total_pnl_usd"] or 0.0), 8),
                    "drawdown_guardrail_passed": bool(
                        drawdown is not None and base_drawdown is not None and float(drawdown) >= float(base_drawdown)
                    ),
                }
            )

    guarded = [row for row in candidates if row["drawdown_guardrail_passed"]]
    ranked = sorted(
        guarded or candidates,
        key=lambda row: (
            float(row["total_pnl_usd"] or -1e9),
            float(row["avg_pnl_pct"] or -1e9),
            float(row["simple_max_drawdown_pct_points"] or -1e9),
        ),
        reverse=True,
    )
    return {"baseline": baseline, "best": ranked[0] if ranked else None, "top_candidates": ranked[:5]}


def summarize_log_noise(logs_dir: Path | None = None) -> dict[str, Any]:
    directory = Path(logs_dir or getattr(CFG, "LOG_PATH", PROJECT_ROOT / "logs"))
    files = sorted(directory.glob("*.txt"))
    level_counts = Counter()
    source_counts = Counter()
    warning_counts = Counter()
    pattern = re.compile(r"^(?P<ts>\S+)\s+(?P<level>[A-Z]+)\s+(?P<source>[^:]+):\s*(?P<message>.*)$")

    for path in files:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for raw in handle:
                    line = raw.strip()
                    if not line:
                        continue
                    match = pattern.match(line)
                    if match is None:
                        continue
                    level_clean = match.group("level").strip()
                    level_counts[level_clean] += 1
                    source = match.group("source").strip()
                    message = match.group("message").strip()
                    source_counts[source] += 1
                    if level_clean == "WARNING":
                        normalized = re.sub(r"\s+[—-]\s+\S+$", "", message)
                        normalized = re.sub(
                            r"Token\s+\S+\s+sin precio tras varios intentos",
                            "Token <token> sin precio tras varios intentos",
                            normalized,
                        )
                        warning_counts[normalized.strip()] += 1
        except Exception:
            continue

    return {
        "log_files": len(files),
        "levels": dict(level_counts),
        "top_sources": [{"source": key, "count": int(value)} for key, value in source_counts.most_common(12)],
        "top_warnings": [{"message": key, "count": int(value)} for key, value in warning_counts.most_common(12)],
    }


def build_audit_snapshot(
    *,
    db_path: Path | None = None,
    features_dir: Path | None = None,
    runtime_events_path: Path | None = None,
    research_events_path: Path | None = None,
    paper_portfolio_path: Path | None = None,
    research_portfolio_path: Path | None = None,
    research_scorecard_path: Path | None = None,
    research_thresholds_path: Path | None = None,
    recommended_threshold_path: Path | None = None,
    train_status_path: Path | None = None,
    dataset_quality_path: Path | None = None,
    logs_dir: Path | None = None,
    edge_report_path: Path | None = None,
    ml_report_path: Path | None = None,
) -> dict[str, Any]:
    closed_positions = load_closed_positions_context(db_path=db_path)
    consistency = build_trade_consistency(
        db_path=db_path,
        paper_portfolio_path=paper_portfolio_path,
        research_scorecard_path=research_scorecard_path,
        closed_positions=closed_positions,
    )
    baseline = build_baseline_snapshot(db_path=db_path, features_dir=features_dir)
    edge = summarize_edge(db_path=db_path, features_dir=features_dir, runtime_events_path=runtime_events_path)
    normalized_events, normalized_stats = normalize_candidate_outcomes_frame(events_path=research_events_path)
    merged_live = _merge_live_outcomes(closed_positions, normalized_events)

    scorecard_path = research_scorecard_path or (PROJECT_ROOT / "data" / "metrics" / "research_scorecard.json")
    thresholds_path = research_thresholds_path or (PROJECT_ROOT / "data" / "metrics" / "research_thresholds.json")
    edge_report_file = edge_report_path or (PROJECT_ROOT / "docs" / "EDGE_REPORT.md")
    ml_report_file = ml_report_path or (PROJECT_ROOT / "docs" / "ML_REPORT.md")

    scorecard = _read_json_file(scorecard_path) or {}
    thresholds = _read_json_file(thresholds_path) or {}
    recommended = _read_json_file(recommended_threshold_path or (PROJECT_ROOT / "data" / "metrics" / "recommended_threshold.json")) or {}
    train_status = _read_json_file(train_status_path or (PROJECT_ROOT / "data" / "metrics" / "train_status.json")) or {}
    dataset_quality = _read_json_file(dataset_quality_path or (PROJECT_ROOT / "data" / "metrics" / "dataset_quality.json")) or {}
    research_portfolio = _read_json_file(research_portfolio_path or (PROJECT_ROOT / "data" / "research_portfolio.json")) or {}
    latest_closed_dt = _parse_timestamp(consistency.get("latest_closed_at"))
    edge_report_updated_at = _file_mtime(Path(edge_report_file))
    ml_report_updated_at = _file_mtime(Path(ml_report_file))
    scorecard_updated_at = _file_mtime(Path(scorecard_path))
    thresholds_updated_at = _file_mtime(Path(thresholds_path))

    return {
        "generated_at_utc": dt.datetime.now(tz=UTC).isoformat(),
        "project_root": str(PROJECT_ROOT),
        "baseline_operational_snapshot": {
            "closed_trades": consistency["db_closed_rows"],
            "open_trades": baseline.get("positions", {}).get("open_rows"),
            "total_pnl_usd": consistency["db_total_pnl_usd"],
            "win_rate_pct": baseline.get("positions", {}).get("win_rate_pct"),
            "avg_pnl_pct": baseline.get("positions", {}).get("avg_pnl_pct"),
            "median_pnl_pct": baseline.get("positions", {}).get("median_pnl_pct"),
            "latest_closed_at": consistency["latest_closed_at"],
        },
        "consistency": consistency,
        "baseline": baseline,
        "edge": edge,
        "ml": {
            "recommended_threshold": recommended,
            "train_status": train_status,
            "dataset_quality": dataset_quality,
        },
        "research": {
            "scorecard": scorecard,
            "thresholds": thresholds,
            "portfolio_rows": len(research_portfolio) if isinstance(research_portfolio, dict) else 0,
            "normalized_candidate_events": normalized_stats,
        },
        "artifacts": {
            "research_scorecard": {
                "path": str(scorecard_path),
                "updated_at": _iso_or_none(scorecard_updated_at),
                "generated_at_utc": scorecard.get("generated_at_utc"),
                "stale_vs_live_close": bool(
                    latest_closed_dt is not None and scorecard_updated_at is not None and scorecard_updated_at < latest_closed_dt
                ),
            },
            "research_thresholds": {
                "path": str(thresholds_path),
                "updated_at": _iso_or_none(thresholds_updated_at),
                "generated_at_utc": thresholds.get("generated_at_utc"),
                "stale_vs_live_close": bool(
                    latest_closed_dt is not None and thresholds_updated_at is not None and thresholds_updated_at < latest_closed_dt
                ),
            },
            "edge_report": {
                "path": str(edge_report_file),
                "updated_at": _iso_or_none(edge_report_updated_at),
                "stale_vs_live_close": bool(
                    latest_closed_dt is not None and edge_report_updated_at is not None and edge_report_updated_at < latest_closed_dt
                ),
            },
            "ml_report": {
                "path": str(ml_report_file),
                "updated_at": _iso_or_none(ml_report_updated_at),
                "stale_vs_live_close": bool(
                    latest_closed_dt is not None and ml_report_updated_at is not None and ml_report_updated_at < latest_closed_dt
                ),
            },
        },
        "logs": summarize_log_noise(logs_dir=logs_dir),
        "pump_early_sweeps": {
            "entry_filter": _entry_filter_sweeps(merged_live),
            "requeue_cap": _requeue_cap_sweeps(closed_positions, runtime_events_path=runtime_events_path),
            "post_partial_protection": _post_partial_protection_sweeps(closed_positions),
        },
    }


def render_audit_markdown(snapshot: dict[str, Any]) -> str:
    baseline = snapshot.get("baseline_operational_snapshot", {})
    consistency = snapshot.get("consistency", {})
    sweeps = snapshot.get("pump_early_sweeps", {})
    research = snapshot.get("research", {})
    artifacts = snapshot.get("artifacts", {})
    lines = [
        "# Audit Report",
        "",
        f"- Generated at UTC: `{snapshot.get('generated_at_utc')}`",
        f"- Project root: `{snapshot.get('project_root')}`",
        "",
        "## Live Baseline",
        "",
        f"- Closed trades: `{baseline.get('closed_trades')}`",
        f"- Open trades: `{baseline.get('open_trades')}`",
        f"- Total PnL USD: `{baseline.get('total_pnl_usd')}`",
        f"- Win rate: `{baseline.get('win_rate_pct')}`",
        f"- Avg PnL (%): `{baseline.get('avg_pnl_pct')}`",
        f"- Median PnL (%): `{baseline.get('median_pnl_pct')}`",
        f"- Latest closed at: `{baseline.get('latest_closed_at')}`",
        "",
        "## Ledger Consistency",
        "",
        f"- DB closed rows: `{consistency.get('db_closed_rows')}`",
        f"- Paper closed rows: `{consistency.get('paper_closed_rows')}`",
        f"- Scorecard live closed: `{consistency.get('scorecard_live_closed')}`",
        f"- Lag rows: `{consistency.get('lag_rows')}`",
        f"- Is consistent: `{consistency.get('is_consistent')}`",
        "",
        "## Pump Early Sweeps",
        "",
    ]

    for key, label in (
        ("entry_filter", "Entry Filter"),
        ("requeue_cap", "Requeue Cap"),
        ("post_partial_protection", "Post-Partial Protection"),
    ):
        best = ((sweeps.get(key) or {}).get("best") or {})
        baseline_metrics = ((sweeps.get(key) or {}).get("baseline") or {})
        lines.extend(
            [
                f"### {label}",
                "",
                f"- Baseline count: `{baseline_metrics.get('count')}`",
                f"- Best params: `{best.get('params')}`",
                f"- Best total PnL USD: `{best.get('total_pnl_usd')}`",
                f"- Best avg PnL (%): `{best.get('avg_pnl_pct')}`",
                f"- Best drawdown: `{best.get('simple_max_drawdown_pct_points')}`",
                f"- Guardrail passed: `{best.get('drawdown_guardrail_passed')}`",
                "",
            ]
        )

    normalized = research.get("normalized_candidate_events", {})
    lines.extend(
        [
            "## Research Dataset",
            "",
            f"- Portfolio rows: `{research.get('portfolio_rows')}`",
            f"- Candidate rows in: `{normalized.get('rows_in')}`",
            f"- Candidate rows out: `{normalized.get('rows_out')}`",
            f"- Ambiguous bought rows dropped: `{normalized.get('ambiguous_bought_dropped')}`",
            f"- Source counts: `{normalized.get('source_counts')}`",
            "",
            "## Artifact Freshness",
            "",
        ]
    )
    for key, label in (
        ("research_scorecard", "Research scorecard"),
        ("research_thresholds", "Research thresholds"),
        ("edge_report", "Edge report"),
        ("ml_report", "ML report"),
    ):
        item = artifacts.get(key, {})
        lines.append(
            f"- {label}: updated_at=`{item.get('updated_at')}`, stale_vs_live_close=`{item.get('stale_vs_live_close')}`"
        )
    lines.append("")

    log_summary = snapshot.get("logs", {})
    lines.extend(
        [
            "## Log Noise",
            "",
            f"- Log files: `{log_summary.get('log_files')}`",
            f"- Levels: `{log_summary.get('levels')}`",
            "",
        ]
    )
    for row in log_summary.get("top_warnings", [])[:8]:
        lines.append(f"- Warning `{row.get('count')}`x: `{row.get('message')}`")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "build_audit_snapshot",
    "build_trade_consistency",
    "_json_value",
    "load_closed_positions_context",
    "normalize_candidate_outcomes_frame",
    "render_audit_markdown",
    "summarize_log_noise",
    "write_normalized_candidate_outcomes",
]
