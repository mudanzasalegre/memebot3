from __future__ import annotations

from analytics.audit import build_trade_consistency
from analytics.reporting import build_baseline_snapshot, summarize_edge

from api.schemas.common import Envelope
from api.services.common import build_envelope
from api.services.sources import json_status, jsonl_status, latest_parquet_status, paper_portfolio_status, sqlite_main_status
from api.settings import APISettings


def _scorecard_status_with_consistency(settings: APISettings, consistency: dict[str, object]):
    status = json_status(
        source_key="metrics.research_scorecard",
        path=settings.research_scorecard_json,
        generated_field="generated_at_utc",
        optional=True,
        empty_when_missing=False,
    )
    if status.status in {"missing", "error", "empty"}:
        return status
    lag_rows = consistency.get("lag_rows")
    if not bool(consistency.get("scorecard_stale_vs_latest_close")) and lag_rows in (None, 0):
        return status
    detail = (
        f"db_closed={consistency.get('db_closed_rows')} "
        f"scorecard_live_closed={consistency.get('scorecard_live_closed')} "
        f"lag_rows={lag_rows}"
    )
    return status.model_copy(update={"status": "stale", "detail": detail})


def get_baseline_envelope(settings: APISettings) -> Envelope:
    snapshot = build_baseline_snapshot(db_path=settings.db_path, features_dir=settings.features_dir)
    snapshot["consistency"] = build_trade_consistency(
        db_path=settings.db_path,
        paper_portfolio_path=settings.paper_portfolio_path,
        research_scorecard_path=settings.research_scorecard_json,
    )
    consistency = snapshot["consistency"]
    statuses = [
        sqlite_main_status(settings),
        latest_parquet_status(settings),
        paper_portfolio_status(settings),
        _scorecard_status_with_consistency(settings, consistency),
    ]
    empty = bool(snapshot.get("positions", {}).get("rows", 0) == 0 and snapshot.get("features", {}).get("rows", 0) == 0)
    return build_envelope(snapshot, source_status=statuses, empty=empty)


def get_edge_envelope(settings: APISettings) -> Envelope:
    snapshot = summarize_edge(
        db_path=settings.db_path,
        features_dir=settings.features_dir,
        runtime_events_path=settings.runtime_events_path,
    )
    snapshot["consistency"] = build_trade_consistency(
        db_path=settings.db_path,
        paper_portfolio_path=settings.paper_portfolio_path,
        research_scorecard_path=settings.research_scorecard_json,
    )
    consistency = snapshot["consistency"]
    statuses = [
        sqlite_main_status(settings),
        latest_parquet_status(settings),
        jsonl_status(source_key="metrics.runtime_events", path=settings.runtime_events_path),
        paper_portfolio_status(settings),
        _scorecard_status_with_consistency(settings, consistency),
    ]
    empty = bool(snapshot.get("overview", {}).get("closed_trades", 0) == 0)
    return build_envelope(snapshot, source_status=statuses, empty=empty)
