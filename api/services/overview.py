from __future__ import annotations

from typing import Any

from analytics.reporting import load_positions_frame, summarize_positions

from api.repositories.filesystem import parse_timestamp, read_json_file
from api.schemas.common import Envelope, SourceStatus
from api.services.bot_process import get_bot_process_snapshot, runtime_state_is_expected_to_be_absent
from api.services.common import build_envelope, iso_or_none
from api.services.ml import get_ml_status_envelope
from api.services.runtime import (
    DEFAULT_BOT_ID,
    get_runtime_snapshot,
    get_runtime_source_status,
    runtime_snapshot_freshness,
    runtime_snapshot_is_stale,
)
from api.services.sources import json_status, sqlite_main_status
from api.settings import APISettings


def _overview_ml_summary(
    settings: APISettings,
    snapshot: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[SourceStatus]]:
    ml_gate = (snapshot or {}).get("ml_gate_json") or {}
    if ml_gate:
        return (
            {
                "model_loaded": ml_gate.get("model_loaded"),
                "activation_ready": ml_gate.get("activation_ready"),
                "threshold": ml_gate.get("threshold"),
            },
            [],
        )

    envelope = get_ml_status_envelope(settings)
    payload = envelope.data if isinstance(envelope.data, dict) else {}
    runtime = payload.get("runtime") or {}
    gate = payload.get("gate") or {}
    return (
        {
            "model_loaded": runtime.get("model_loaded"),
            "activation_ready": gate.get("activation_ready", runtime.get("activation_ready")),
            "threshold": gate.get("threshold"),
        },
        list(envelope.meta.source_status),
    )


def _overview_research_summary(
    settings: APISettings,
    snapshot: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[SourceStatus]]:
    research = (snapshot or {}).get("research_json") or {}
    scorecard_generated_at = parse_timestamp(research.get("scorecard_generated_at"))
    statuses: list[SourceStatus] = []

    if scorecard_generated_at is None:
        scorecard = read_json_file(settings.research_scorecard_json)
        if isinstance(scorecard, dict):
            scorecard_generated_at = parse_timestamp(scorecard.get("generated_at_utc"))
        statuses.append(
            json_status(
                source_key="metrics.research_scorecard",
                path=settings.research_scorecard_json,
                generated_field="generated_at_utc",
                optional=True,
                empty_when_missing=False,
            )
        )

    return (
        {
            "open_shadow_count": research.get("open_shadow_count"),
            "scorecard_generated_at": iso_or_none(scorecard_generated_at),
        },
        statuses,
    )


def get_overview_envelope(settings: APISettings, *, bot_id: str = DEFAULT_BOT_ID) -> Envelope:
    snapshot = get_runtime_snapshot(settings, bot_id=bot_id)
    freshness = runtime_snapshot_freshness(snapshot)
    runtime_status = get_runtime_source_status(settings, snapshot, bot_id=bot_id)
    process_payload, process_status = get_bot_process_snapshot(settings, snapshot=snapshot, bot_id=bot_id)
    positions = summarize_positions(load_positions_frame(settings.db_path))
    positions_summary = {
        "open_rows": positions.get("open_rows"),
        "closed_rows": positions.get("closed_rows"),
        "win_rate_pct": positions.get("win_rate_pct"),
        "avg_pnl_pct": positions.get("avg_pnl_pct"),
    }
    ml_summary, ml_statuses = _overview_ml_summary(settings, snapshot)
    research_summary, research_statuses = _overview_research_summary(settings, snapshot)

    statuses: list[SourceStatus] = [
        runtime_status,
        process_status,
        sqlite_main_status(settings),
        *ml_statuses,
        *research_statuses,
    ]

    data = {
        "bot": {
            "bot_id": (snapshot or {}).get("bot_id") or bot_id,
            "process_state": (snapshot or {}).get("process_state"),
            "dry_run": (snapshot or {}).get("dry_run"),
            "heartbeat_at": (snapshot or {}).get("heartbeat_at"),
            "staleness": freshness,
            "orchestration_status": process_payload.get("status"),
            "ui_managed": process_payload.get("managed"),
            "ui_can_start": process_payload.get("can_start"),
            "ui_can_stop": process_payload.get("can_stop"),
        },
        "runtime": {
            "discovery_paused": (snapshot or {}).get("discovery_paused"),
            "buys_paused": (snapshot or {}).get("buys_paused"),
            "retrain_state": (snapshot or {}).get("retrain_state"),
            "reports_refresh_state": (snapshot or {}).get("reports_refresh_state"),
        },
        "queue": {
            "pending": (snapshot or {}).get("queue_pending"),
            "requeued": (snapshot or {}).get("queue_requeued"),
            "cooldown": (snapshot or {}).get("queue_cooldown"),
            "oldest_first_seen_at": (snapshot or {}).get("queue_oldest_first_seen_at"),
        },
        "wallet": {
            "wallet_sol": (snapshot or {}).get("wallet_sol"),
            "wallet_checked_at": (snapshot or {}).get("wallet_checked_at"),
        },
        "positions": positions_summary,
        "ml": ml_summary,
        "research": research_summary,
    }

    empty = bool(
        snapshot is None
        and int(positions.get("rows", 0) or 0) == 0
        and research_summary.get("scorecard_generated_at") is None
    )
    runtime_absent_is_expected = runtime_state_is_expected_to_be_absent(process_payload)
    degraded = (
        (freshness in {"degraded", "error"} and not runtime_absent_is_expected)
        or any(item.status in {"missing", "error"} for item in statuses if item.source_key != "sqlite.bot_runtime_state")
        or (runtime_status.status in {"missing", "error"} and not runtime_absent_is_expected)
    )
    stale = (
        runtime_snapshot_is_stale(snapshot) and not runtime_absent_is_expected
    ) or any(item.status == "stale" for item in statuses if item.source_key != "sqlite.bot_runtime_state")
    return build_envelope(
        data,
        source_status=statuses,
        empty=empty,
        degraded=degraded,
        stale=stale,
    )
