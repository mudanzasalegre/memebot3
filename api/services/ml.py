from __future__ import annotations

from pathlib import Path
from typing import Any

from analytics.audit import build_trade_consistency
from analytics.ai_predict import model_runtime_status
from config.config import CFG

from api.repositories.filesystem import file_mtime, load_jsonl_rows, parse_timestamp, read_json_file
from api.schemas.common import Envelope, SourceStatus
from api.services.common import build_envelope, iso_or_none, make_source_status
from api.services.sources import json_status, jsonl_status
from api.services.runtime import (
    get_runtime_snapshot,
    get_runtime_source_status,
    runtime_snapshot_freshness,
    runtime_snapshot_is_stale,
)
from api.settings import APISettings


_RUNTIME_GATE_RUNTIME_KEYS = (
    "activation_ready",
    "dataset_quality_passed",
    "model_loaded",
    "features_count",
    "threshold_metric",
    "training_scope",
    "bootstrap_used",
    "rows",
    "eligible_rows",
    "eligible_unique_tokens",
    "eligible_positives",
    "holdout_rows",
    "rows_missing_lane_metadata",
    "last_train_attempt_at",
    "last_train_status",
    "skip_reasons",
    "rows_to_next_model",
    "blocker",
)
_RUNTIME_GATE_KEYS = ("mode", "enforced", "threshold", "activation_ready")


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _to_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except Exception:
        return None


def _scorecard_status_with_consistency(settings: APISettings, consistency: dict[str, Any]) -> SourceStatus:
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


def _merge_runtime_payload(runtime: dict[str, Any], runtime_gate: dict[str, Any]) -> dict[str, Any]:
    merged = dict(runtime)
    for key in _RUNTIME_GATE_RUNTIME_KEYS:
        value = runtime_gate.get(key)
        if value is not None:
            merged[key] = value
    return merged


def _effective_gate(
    runtime: dict[str, Any],
    recommended_threshold: dict[str, Any] | None,
    runtime_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mode = str(getattr(CFG, "ML_GATE_MODE", "legacy") or "legacy").strip().lower()
    if mode not in {"legacy", "shadow", "enforce", "off"}:
        mode = "legacy"

    activation_ready_raw = runtime.get("activation_ready")
    activation_ready = bool(activation_ready_raw) if activation_ready_raw is not None else False
    if mode in {"off", "shadow"}:
        enforced = False
    elif mode == "enforce":
        enforced = activation_ready
    else:
        enforced = True

    threshold = None
    if isinstance(recommended_threshold, dict):
        threshold = recommended_threshold.get("picked")
    if threshold is None:
        threshold = float(getattr(CFG, "AI_THRESHOLD", 0.0) or 0.0)

    gate = {
        "mode": mode,
        "enforced": bool(enforced),
        "threshold": float(threshold),
        "activation_ready": activation_ready_raw,
    }
    if isinstance(runtime_gate, dict):
        for key in _RUNTIME_GATE_KEYS:
            value = runtime_gate.get(key)
            if value is not None:
                gate[key] = value
    return gate


def _model_source_status(runtime: dict[str, Any]) -> list[SourceStatus]:
    model_path = Path(str(runtime.get("model_path") or "")).resolve()
    meta_path = Path(str(runtime.get("meta_path") or "")).resolve()
    model_loaded = bool(runtime.get("model_loaded"))
    model_exists = bool(runtime.get("model_exists"))
    meta_exists = bool(runtime.get("meta_exists"))

    return [
        make_source_status(
            source_key="ml.model",
            kind="file",
            status="ok" if model_exists else "missing",
            updated_at=file_mtime(model_path),
            detail=f"loaded={model_loaded}",
            path=model_path,
        ),
        make_source_status(
            source_key="ml.meta",
            kind="json",
            status="ok" if meta_exists else "missing",
            updated_at=file_mtime(meta_path),
            detail=f"features_count={runtime.get('features_count')}",
            path=meta_path,
        ),
    ]


def _profit_outcome_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnl_values: list[float] = []
    positive = 0
    for row in rows:
        raw_pnl = (
            row.get("realized_pnl_pct")
            if row.get("realized_pnl_pct") is not None
            else row.get("target_total_pnl_pct", row.get("total_pnl_pct", row.get("pnl_pct")))
        )
        pnl = _to_float(raw_pnl)
        if pnl is None:
            continue
        pnl_values.append(pnl)
        if pnl > 0.0:
            positive += 1
    count = len(pnl_values)
    return {
        "count": count,
        "positives": positive,
        "avg_realized_pnl_pct": (sum(pnl_values) / count) if count else None,
        "win_rate_pct": (positive / count * 100.0) if count else None,
    }


def _promotion_readiness(runtime: dict[str, Any], profit_metrics: dict[str, Any]) -> dict[str, Any]:
    thresholds = {
        "eligible_rows": 250,
        "eligible_positives": 80,
        "eligible_unique_tokens": 220,
        "holdout_auc_min": 0.55,
        "avg_realized_pnl_pct_min": 3.0,
        "productive_closes_min": 50,
    }
    actuals = {
        "eligible_rows": _to_int(runtime.get("eligible_rows") or runtime.get("rows")) or 0,
        "eligible_positives": _to_int(runtime.get("eligible_positives")) or 0,
        "eligible_unique_tokens": _to_int(runtime.get("eligible_unique_tokens")) or 0,
        "holdout_auc": _to_float(runtime.get("auc_forward_or_cv_mean")),
        "avg_realized_pnl_pct": _to_float(profit_metrics.get("avg_realized_pnl_pct")),
        "productive_closes": _to_int(profit_metrics.get("count")) or 0,
    }
    checks = {
        "eligible_rows": actuals["eligible_rows"] >= thresholds["eligible_rows"],
        "eligible_positives": actuals["eligible_positives"] >= thresholds["eligible_positives"],
        "eligible_unique_tokens": actuals["eligible_unique_tokens"] >= thresholds["eligible_unique_tokens"],
        "holdout_auc": actuals["holdout_auc"] is not None and actuals["holdout_auc"] >= thresholds["holdout_auc_min"],
        "avg_realized_pnl_pct": actuals["avg_realized_pnl_pct"] is not None
        and actuals["avg_realized_pnl_pct"] >= thresholds["avg_realized_pnl_pct_min"],
        "productive_closes": actuals["productive_closes"] >= thresholds["productive_closes_min"],
    }
    blockers = [name for name, passed in checks.items() if not passed]
    return {
        "ready": not blockers,
        "checks": checks,
        "actuals": actuals,
        "thresholds": thresholds,
        "blockers": blockers,
    }


def get_ml_status_envelope(settings: APISettings) -> Envelope:
    runtime = model_runtime_status()
    runtime_snapshot = get_runtime_snapshot(settings)
    runtime_gate = ((runtime_snapshot or {}).get("ml_gate_json") or {}) if runtime_snapshot else {}
    strategy_health = ((runtime_snapshot or {}).get("strategy_health_json") or {}) if runtime_snapshot else {}
    pump_health = dict((strategy_health or {}).get("pump_early") or {})
    if runtime_gate:
        runtime = _merge_runtime_payload(runtime, runtime_gate)
    recommended = read_json_file(settings.recommended_threshold_json)
    train_status = read_json_file(settings.train_status_json)
    dataset_quality = read_json_file(settings.dataset_quality_json)
    if runtime.get("dataset_quality_passed") is None and isinstance(dataset_quality, dict) and "passed" in dataset_quality:
        runtime["dataset_quality_passed"] = dataset_quality.get("passed")
    if isinstance(train_status, dict):
        for key in (
            "eligible_rows",
            "eligible_unique_tokens",
            "eligible_positives",
            "holdout_rows",
            "rows_missing_lane_metadata",
            "last_train_attempt_at",
            "last_train_status",
            "skip_reasons",
            "rows_to_next_model",
            "positives_to_next_model",
            "unique_tokens_to_next_model",
            "holdout_rows_to_next_model",
            "holdout_positives_to_next_model",
            "blocker",
            "auc_forward_or_cv_mean",
            "training_scope",
            "bootstrap_used",
            "strict_productive_dataset",
            "bootstrap_candidate_dataset",
        ):
            if train_status.get(key) is not None:
                runtime[key] = train_status.get(key)
    gate = _effective_gate(runtime, recommended if isinstance(recommended, dict) else None, runtime_gate)
    if runtime_gate:
        if runtime_gate.get("live_threshold_origin") is not None:
            gate["live_threshold_origin"] = runtime_gate.get("live_threshold_origin")
        if runtime_gate.get("live_rank_gate") is not None:
            gate["live_rank_gate"] = runtime_gate.get("live_rank_gate")
        if runtime_gate.get("live_uses_rank_score") is not None:
            gate["live_uses_rank_score"] = bool(runtime_gate.get("live_uses_rank_score"))
        if runtime_gate.get("live_uses_heuristic_only") is not None:
            gate["live_uses_heuristic_only"] = bool(runtime_gate.get("live_uses_heuristic_only"))
    if pump_health:
        gate["last_auto_demote_at"] = iso_or_none(parse_timestamp(pump_health.get("last_auto_demote_at"))) or pump_health.get(
            "last_auto_demote_at"
        )
        gate["last_auto_recover_at"] = iso_or_none(parse_timestamp(pump_health.get("last_auto_recover_at"))) or pump_health.get(
            "last_auto_recover_at"
        )
    gate["live_threshold_origin"] = gate.get("live_threshold_origin") or "ml_shadow_not_live"
    gate["live_uses_rank_score"] = False
    gate["live_uses_heuristic_only"] = True
    gate["productive_gate"] = "pump_early_pumpswap_profit"
    gate["sniper_lane_enabled"] = bool(getattr(CFG, "PUMP_EARLY_SNIPER_ENABLED", True))
    gate["sniper_mode"] = str(getattr(CFG, "PUMP_EARLY_SNIPER_MODE", "canary_aggressive") or "canary_aggressive")

    research_rows = load_jsonl_rows(settings.research_events_path)
    outcome_events = [
        row
        for row in research_rows
        if str(row.get("event_type") or "") in {"candidate_outcome", "live_trade_close", "shadow_close"}
    ]
    lane_counts: dict[str, int] = {}
    dex_counts: dict[str, int] = {}
    productive_lane_counts: dict[str, int] = {}
    productive_dex_counts: dict[str, int] = {}
    for row in outcome_events:
        lane = str(row.get("entry_lane") or row.get("sniper_gate_profile") or "").strip()
        if lane:
            lane_counts[lane] = lane_counts.get(lane, 0) + 1
        dex = str(row.get("dex_id") or "").strip().lower()
        if dex:
            dex_counts[dex] = dex_counts.get(dex, 0) + 1

    productive_events = [
        row
        for row in outcome_events
        if str(row.get("source") or "").strip().lower() in {"live_trade", "paper_trade", "paper_portfolio"}
        or row.get("shadow_kind") is None
    ]
    productive_event_ids = {id(row) for row in productive_events}
    for row in productive_events:
        lane = str(row.get("entry_lane") or row.get("sniper_gate_profile") or "").strip()
        if lane:
            productive_lane_counts[lane] = productive_lane_counts.get(lane, 0) + 1
        dex = str(row.get("dex_id") or "").strip().lower()
        if dex:
            productive_dex_counts[dex] = productive_dex_counts.get(dex, 0) + 1

    sniper_closes = [
        row
        for row in productive_events
        if (
            str(row.get("entry_lane") or "").startswith("pump_early_sniper")
            or str(row.get("entry_lane") or "").startswith("pump_early_pumpswap")
            or str(row.get("sniper_gate_profile") or "").startswith("sniper")
            or str(row.get("sniper_gate_profile") or "").startswith("pumpswap_profit")
        )
    ]
    pump_closes = [
        row
        for row in productive_events
        if str(row.get("regime") or "") == "pump_early"
    ]
    profit_closes = [
        row
        for row in productive_events
        if str(row.get("entry_lane") or "") == "pump_early_pumpswap_profit"
    ]
    research_profit_like = [
        row
        for row in outcome_events
        if id(row) not in productive_event_ids
        and (
            str(row.get("entry_lane") or "") == "pump_early_pumpswap_profit"
            or str(row.get("gate_profile") or row.get("sniper_gate_profile") or "").startswith("pumpswap_profit")
        )
    ]
    profit_positive = 0
    for row in profit_closes:
        raw_pnl = (
            row.get("realized_pnl_pct")
            if row.get("realized_pnl_pct") is not None
            else row.get("target_total_pnl_pct", row.get("total_pnl_pct", row.get("pnl_pct")))
        )
        try:
            if float(raw_pnl) > 0:
                profit_positive += 1
        except Exception:
            continue
    profit_metrics = _profit_outcome_metrics(profit_closes)
    promotion = _promotion_readiness(runtime, profit_metrics)
    blocker = runtime.get("blocker") or (",".join(promotion["blockers"]) if promotion["blockers"] else None)

    data = {
        "runtime": runtime,
        "gate": gate,
        "lane_readiness": {
            "pump_early_outcomes": len(pump_closes),
            "pump_early_sniper_outcomes": len(sniper_closes),
            "pump_early_pumpswap_profit_outcomes": len(profit_closes),
            "pump_early_pumpswap_profit_positives": profit_positive,
            "pump_early_pumpswap_profit_avg_realized_pnl_pct": profit_metrics.get("avg_realized_pnl_pct"),
            "pump_early_pumpswap_profit_win_rate_pct": profit_metrics.get("win_rate_pct"),
            "outcomes_by_lane": productive_lane_counts,
            "outcomes_by_dex": productive_dex_counts,
            "all_outcomes_by_lane": lane_counts,
            "all_outcomes_by_dex": dex_counts,
            "research_profit_like_outcomes": len(research_profit_like),
            "productive_lane_promotion_readiness": promotion,
            "ml_live_ready": bool(promotion["ready"]),
            "required_pump_early_outcomes": 100,
            "required_productive_lane_outcomes": 100,
            "required_positive_outcomes": 35,
            "training_filters": {
                "entry_lane_allowlist": str(
                    getattr(CFG, "ML_TRAIN_ENTRY_LANE_ALLOWLIST", "pump_early_pumpswap_profit,pump_early_pumpswap_prime")
                    or ""
                ),
                "dex_allowlist": str(getattr(CFG, "ML_TRAIN_DEX_ALLOWLIST", "pumpswap") or ""),
            },
        },
        "next_model": {
            "model_exists": bool(runtime.get("model_exists")),
            "last_train_attempt_at": runtime.get("last_train_attempt_at"),
            "last_train_status": runtime.get("last_train_status"),
            "eligible_rows": runtime.get("eligible_rows"),
            "eligible_unique_tokens": runtime.get("eligible_unique_tokens"),
            "eligible_positives": runtime.get("eligible_positives"),
            "holdout_rows": runtime.get("holdout_rows"),
            "rows_missing_lane_metadata": runtime.get("rows_missing_lane_metadata"),
            "rows_to_next_model": runtime.get("rows_to_next_model"),
            "positives_to_next_model": runtime.get("positives_to_next_model"),
            "unique_tokens_to_next_model": runtime.get("unique_tokens_to_next_model"),
            "holdout_rows_to_next_model": runtime.get("holdout_rows_to_next_model"),
            "holdout_positives_to_next_model": runtime.get("holdout_positives_to_next_model"),
            "skip_reasons": runtime.get("skip_reasons") or [],
            "blocker": blocker,
            "training_scope": runtime.get("training_scope"),
            "bootstrap_used": runtime.get("bootstrap_used"),
            "strict_productive_dataset": runtime.get("strict_productive_dataset"),
            "bootstrap_candidate_dataset": runtime.get("bootstrap_candidate_dataset"),
        },
        "train_status": train_status,
        "recommended_threshold": recommended,
        "dataset_quality": dataset_quality,
    }
    statuses = [
        *_model_source_status(runtime),
        json_status(
            source_key="metrics.recommended_threshold",
            path=settings.recommended_threshold_json,
            optional=True,
            empty_when_missing=True,
        ),
        json_status(
            source_key="metrics.train_status",
            path=settings.train_status_json,
            optional=True,
            empty_when_missing=True,
        ),
        json_status(
            source_key="metrics.dataset_quality",
            path=settings.dataset_quality_json,
            optional=True,
            empty_when_missing=True,
        ),
    ]
    if runtime_snapshot is not None:
        statuses.insert(0, get_runtime_source_status(settings, runtime_snapshot))

    freshness = runtime_snapshot_freshness(runtime_snapshot) if runtime_snapshot is not None else None
    degraded = (
        not bool(runtime.get("model_exists"))
        or not bool(runtime.get("meta_exists"))
        or freshness in {"degraded", "error"}
    )
    stale = runtime_snapshot_is_stale(runtime_snapshot) if runtime_snapshot is not None else False
    return build_envelope(data, source_status=statuses, degraded=degraded, stale=stale)


def get_ml_research_envelope(settings: APISettings) -> Envelope:
    scorecard = read_json_file(settings.research_scorecard_json)
    thresholds = read_json_file(settings.research_thresholds_json)
    post_partial_experiment = read_json_file(settings.post_partial_experiment_json)
    research_rows = load_jsonl_rows(settings.research_events_path)
    last_event_at = max((parse_timestamp(row.get("ts_utc")) for row in research_rows), default=None)

    scorecard_generated_at = None
    if isinstance(scorecard, dict):
        scorecard_generated_at = parse_timestamp(scorecard.get("generated_at_utc"))

    thresholds_generated_at = None
    if isinstance(thresholds, dict):
        thresholds_generated_at = parse_timestamp(thresholds.get("generated_at_utc"))

    consistency = build_trade_consistency(
        db_path=settings.db_path,
        paper_portfolio_path=settings.paper_portfolio_path,
        research_scorecard_path=settings.research_scorecard_json,
    )
    scorecard_status = _scorecard_status_with_consistency(settings, consistency)
    thresholds_status = json_status(
        source_key="metrics.research_thresholds",
        path=settings.research_thresholds_json,
        generated_field="generated_at_utc",
        optional=True,
        empty_when_missing=True,
    )
    post_partial_experiment_status = json_status(
        source_key="metrics.post_partial_experiment",
        path=settings.post_partial_experiment_json,
        generated_field="generated_at_utc",
        optional=True,
        empty_when_missing=True,
    )
    events_status = jsonl_status(source_key="metrics.research_events", path=settings.research_events_path)

    stale = False
    if last_event_at is not None and scorecard_generated_at is not None and scorecard_generated_at < last_event_at:
        scorecard_status = scorecard_status.model_copy(update={"status": "stale", "updated_at": iso_or_none(scorecard_generated_at)})
        stale = True
    if last_event_at is not None and thresholds_generated_at is not None and thresholds_generated_at < last_event_at:
        thresholds_status = thresholds_status.model_copy(
            update={"status": "stale", "updated_at": iso_or_none(thresholds_generated_at)}
        )
        stale = True

    data = {
        "scorecard": scorecard,
        "thresholds": thresholds,
        "post_partial_experiment": post_partial_experiment,
        "research_events": {
            "rows": len(research_rows),
            "last_event_at": iso_or_none(last_event_at),
        },
        "consistency": consistency,
    }
    statuses = [scorecard_status, thresholds_status, post_partial_experiment_status, events_status]
    empty = not research_rows and not scorecard and not thresholds and not post_partial_experiment
    degraded = scorecard is None and bool(research_rows)
    return build_envelope(data, source_status=statuses, empty=empty, degraded=degraded, stale=stale)
