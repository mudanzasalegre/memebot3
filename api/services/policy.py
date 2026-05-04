from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from analytics.baseline_snapshot import build_current_baseline_snapshot
from analytics.drift_monitor import drift_snapshot
from analytics.funnel_attribution import build_funnel_attribution
from analytics.runner_capture import build_runner_capture
from analytics.trade_diagnostics import build_trade_diagnostics
from api.repositories.filesystem import file_mtime, load_jsonl_rows, read_json_file
from api.schemas.common import Envelope, SourceStatus
from api.services.common import build_envelope, iso_or_none, make_source_status, utc_now
from api.services.sources import json_status, jsonl_status, paper_portfolio_status, sqlite_main_status
from api.settings import APISettings
from backtest.policy_replay import build_policy_replay
from config.config import CFG
from tools.config_effect_audit import build_config_effect_audit


MODEL_FAMILIES = ("risk", "ev", "runner", "continuation", "exit")
DISPLAY_POLICIES = (
    "current",
    "risk_guard",
    "liq_guard",
    "early_dump",
    "late_momentum_watch",
    "research_rank_canary",
    "combined_v1",
    "combined_policy_v2",
)


def _metrics_path(settings: APISettings, name: str) -> Path:
    return settings.metrics_dir / name


def _proposal_root(settings: APISettings) -> Path:
    return settings.project_root / "strategy_proposals"


def _model_registry_path(settings: APISettings) -> Path:
    return settings.project_root / "ml" / "model_registry.json"


def _model_family_dir(settings: APISettings, family: str) -> Path:
    return settings.project_root / "ml" / "models" / family


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _status_from_artifact(settings: APISettings, name: str, *, optional: bool = True) -> SourceStatus:
    return json_status(
        source_key=f"metrics.{name.removesuffix('.json')}",
        path=_metrics_path(settings, name),
        generated_field="generated_at_utc",
        optional=optional,
        empty_when_missing=optional,
    )


def _derived_status(source_key: str, detail: str = "computed_from_local_sources") -> SourceStatus:
    return make_source_status(
        source_key=source_key,
        kind="derived",
        status="ok",
        updated_at=utc_now(),
        detail=detail,
    )


def _safe_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _count_by(rows: list[dict[str, Any]], key: str, *, limit: int = 20) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return [
        {"key": value, "count": count}
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _policy_rows(replay: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ordered = [policy for policy in DISPLAY_POLICIES if policy in replay]
    ordered.extend(policy for policy in replay if policy not in ordered)
    for policy in ordered:
        metrics = replay.get(policy)
        if isinstance(metrics, dict):
            rows.append({"policy": policy, **metrics})
    return rows


def _best_policy(replay: dict[str, Any]) -> str | None:
    rows = _policy_rows(replay)
    if not rows:
        return None
    return max(rows, key=lambda row: _safe_number(row.get("total_pnl"))).get("policy")


def _proposal_items(settings: APISettings, *, limit: int = 25) -> list[dict[str, Any]]:
    root = _proposal_root(settings)
    items: list[dict[str, Any]] = []
    for folder in ("candidates", "accepted", "rejected"):
        directory = root / folder
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            payload = read_json_file(path)
            if not isinstance(payload, dict):
                continue
            updated_at = iso_or_none(file_mtime(path))
            items.append(
                {
                    **payload,
                    "folder": folder,
                    "status": folder.rstrip("s"),
                    "proposal_id": payload.get("proposal_id") or path.stem,
                    "path": str(path),
                    "updated_at": updated_at,
                }
            )
    items.sort(
        key=lambda item: (
            _safe_number((item.get("expected_metrics") or {}).get("score") if isinstance(item.get("expected_metrics"), dict) else None),
            str(item.get("updated_at") or ""),
        ),
        reverse=True,
    )
    return items[: max(1, min(int(limit), 100))]


def _proposal_counts(settings: APISettings) -> dict[str, int]:
    root = _proposal_root(settings)
    return {
        folder: len(list((root / folder).glob("*.json"))) if (root / folder).exists() else 0
        for folder in ("candidates", "accepted", "rejected")
    }


def get_preflight_envelope(settings: APISettings) -> Envelope:
    path = _metrics_path(settings, "preflight_status.json")
    payload = read_json_file(path) or {}
    status = _status_from_artifact(settings, "preflight_status.json")
    return build_envelope(payload, source_status=[status], empty=not bool(payload))


def get_config_effect_audit_envelope(settings: APISettings) -> Envelope:
    path = _metrics_path(settings, "config_effect_audit.json")
    payload = read_json_file(path)
    status = _status_from_artifact(settings, "config_effect_audit.json")
    if not isinstance(payload, dict):
        payload = build_config_effect_audit(settings.project_root)
        status = _derived_status("metrics.config_effect_audit", "computed_not_persisted")
    return build_envelope(payload, source_status=[status], empty=not bool(payload.get("flags")))


def get_current_baseline_envelope(settings: APISettings) -> Envelope:
    payload = build_current_baseline_snapshot(settings.project_root)
    statuses = [
        sqlite_main_status(settings),
        paper_portfolio_status(settings),
        jsonl_status(source_key="metrics.candidate_outcomes", path=_metrics_path(settings, "candidate_outcomes.jsonl"), optional=True),
        jsonl_status(source_key="metrics.runtime_events", path=settings.runtime_events_path, optional=True),
        _derived_status("policy.current_baseline"),
    ]
    return build_envelope(payload, source_status=statuses, empty=not bool(payload.get("trades", {}).get("rows")))


def get_funnel_attribution_envelope(settings: APISettings, *, limit: int = 50) -> Envelope:
    rows = build_funnel_attribution(settings.project_root)
    data = {
        "count": len(rows),
        "summary": {
            "final_states": _count_by(rows, "final_state"),
            "blocking_reasons": _count_by(rows, "final_blocking_reason"),
            "primary_stages": _count_by(rows, "primary_stage"),
        },
        "items": rows[: max(1, min(int(limit), 250))],
    }
    statuses = [
        jsonl_status(source_key="metrics.runtime_events", path=settings.runtime_events_path, optional=True),
        jsonl_status(source_key="metrics.candidate_outcomes", path=_metrics_path(settings, "candidate_outcomes.jsonl"), optional=True),
        paper_portfolio_status(settings),
        sqlite_main_status(settings),
        _derived_status("policy.funnel_attribution"),
    ]
    return build_envelope(data, source_status=statuses, empty=not rows)


def get_decision_ledger_envelope(settings: APISettings, *, limit: int = 50) -> Envelope:
    path = _metrics_path(settings, "decision_ledger.jsonl")
    rows = load_jsonl_rows(path)
    by_action = _count_by(rows, "decision")
    by_lane = _count_by(rows, "lane")
    data = {
        "count": len(rows),
        "summary": {
            "rows": len(rows),
            "by_action": {item["key"]: item["count"] for item in by_action},
            "by_lane": {item["key"]: item["count"] for item in by_lane},
        },
        "items": list(reversed(rows[-max(1, min(int(limit), 250)) :])),
    }
    return build_envelope(
        data,
        source_status=[jsonl_status(source_key="metrics.decision_ledger", path=path, optional=True)],
        empty=not rows,
    )


def get_trade_diagnostics_envelope(settings: APISettings) -> Envelope:
    payload = build_trade_diagnostics(settings.project_root)
    statuses = [
        jsonl_status(source_key="metrics.candidate_outcomes", path=_metrics_path(settings, "candidate_outcomes.jsonl"), optional=True),
        paper_portfolio_status(settings),
        sqlite_main_status(settings),
        _derived_status("policy.trade_diagnostics"),
    ]
    return build_envelope(payload, source_status=statuses, empty=not bool(payload.get("summary")))


def get_runner_capture_envelope(settings: APISettings) -> Envelope:
    payload = build_runner_capture(settings.project_root)
    statuses = [
        jsonl_status(source_key="metrics.candidate_outcomes", path=_metrics_path(settings, "candidate_outcomes.jsonl"), optional=True),
        paper_portfolio_status(settings),
        sqlite_main_status(settings),
        _derived_status("policy.runner_capture"),
    ]
    return build_envelope(payload, source_status=statuses, empty=not bool(payload.get("top_runners")))


def get_policy_replay_envelope(settings: APISettings) -> Envelope:
    replay = build_policy_replay(settings.project_root)
    rows = _policy_rows(replay)
    current = replay.get("current") if isinstance(replay.get("current"), dict) else None
    data = {
        "current": current,
        "best_by_total_pnl": _best_policy(replay),
        "policies": rows,
        "raw": replay,
    }
    statuses = [
        jsonl_status(source_key="metrics.candidate_outcomes", path=_metrics_path(settings, "candidate_outcomes.jsonl"), optional=True),
        paper_portfolio_status(settings),
        sqlite_main_status(settings),
        _derived_status("policy.replay"),
    ]
    return build_envelope(data, source_status=statuses, empty=not rows)


def get_paper_forward_envelope(settings: APISettings) -> Envelope:
    path = _metrics_path(settings, "paper_forward_report.json")
    payload = read_json_file(path) or {}
    status = _status_from_artifact(settings, "paper_forward_report.json")
    return build_envelope(payload, source_status=[status], empty=not bool(payload))


def get_proposals_envelope(settings: APISettings, *, limit: int = 25) -> Envelope:
    items = _proposal_items(settings, limit=limit)
    counts = _proposal_counts(settings)
    schema_status = json_status(
        source_key="strategy_proposals.schema",
        path=_proposal_root(settings) / "schema.json",
        optional=True,
        empty_when_missing=True,
    )
    data = {"count": len(items), "counts": counts, "items": items}
    return build_envelope(data, source_status=[schema_status], empty=not items)


def get_model_registry_envelope(settings: APISettings) -> Envelope:
    registry_path = _model_registry_path(settings)
    registry = read_json_file(registry_path)
    if not isinstance(registry, dict):
        registry = {}
    family_rows = []
    registry_families = registry.get("families") if isinstance(registry.get("families"), dict) else {}
    for family in MODEL_FAMILIES:
        family_dir = _model_family_dir(settings, family)
        candidates = [path for path in family_dir.iterdir() if path.is_dir()] if family_dir.exists() else []
        active_model = family_dir / "active_model.pkl"
        active_meta = family_dir / "active_model.meta.json"
        family_rows.append(
            {
                "family": family,
                "candidate_count": len(candidates),
                "active_model_exists": active_model.exists(),
                "active_meta_exists": active_meta.exists(),
                "registry": registry_families.get(family, {}),
            }
        )
    data = {"registry": registry, "families": family_rows}
    status = json_status(source_key="ml.model_registry", path=registry_path, optional=True, empty_when_missing=True)
    return build_envelope(data, source_status=[status], empty=not bool(registry))


def get_drift_envelope(settings: APISettings) -> Envelope:
    payload = drift_snapshot(events_path=settings.runtime_events_path)
    status = jsonl_status(source_key="metrics.runtime_events", path=settings.runtime_events_path, optional=True)
    return build_envelope(payload, source_status=[status], empty=_safe_int(payload.get("rows")) == 0)


def get_policy_safety_envelope(settings: APISettings) -> Envelope:
    preflight = read_json_file(_metrics_path(settings, "preflight_status.json")) or {}
    config_audit = read_json_file(_metrics_path(settings, "config_effect_audit.json"))
    if not isinstance(config_audit, dict):
        config_audit = build_config_effect_audit(settings.project_root)
    replay = build_policy_replay(settings.project_root)
    replay_current = replay.get("current") if isinstance(replay.get("current"), dict) else {}
    replay_candidate = replay.get("combined_policy_v2") or replay.get("combined_v1") or {}
    paper_forward = read_json_file(_metrics_path(settings, "paper_forward_report.json")) or {}
    model_registry = read_json_file(_model_registry_path(settings)) or {}
    drift = drift_snapshot(events_path=settings.runtime_events_path)
    proposal_counts = _proposal_counts(settings)

    auto_promote_live = _env_bool("AUTO_PROMOTE_LIVE", False)
    llm_trading_enabled = _env_bool("LLM_TRADING_ENABLED", False)
    socials_hot_path_blocking = bool(getattr(CFG, "SOCIALS_HOT_PATH_BLOCKING", False))
    green_require_socials = bool(getattr(CFG, "GREEN_SNIPER_REQUIRE_SOCIALS", False))
    live_canary_enabled = bool(getattr(CFG, "LIVE_CANARY_ENABLED", False))
    live_manual_approval = _env_bool("LIVE_CANARY_MANUAL_APPROVAL", False)
    live_require_route = bool(getattr(CFG, "LIVE_REQUIRE_ROUTE", True))
    live_require_provider_health = _env_bool("LIVE_REQUIRE_PROVIDER_HEALTH", True)

    replay_present = bool(replay_current)
    replay_candidate_passed = bool(
        replay_present
        and _safe_number(replay_candidate.get("total_pnl")) >= _safe_number(replay_current.get("total_pnl"))
        and _safe_int(replay_candidate.get("severe_loss_count")) <= _safe_int(replay_current.get("severe_loss_count"))
        and _safe_number(replay_candidate.get("runner_capture_ratio"))
        >= _safe_number(replay_current.get("runner_capture_ratio"))
    )
    paper_forward_passed = bool(paper_forward.get("passed"))
    model_families = (model_registry.get("families") or {}) if isinstance(model_registry, dict) else {}

    gates = [
        {
            "id": "preflight",
            "label": "Preflight",
            "status": "pass" if preflight.get("ok") else "missing",
            "detail": "latest preflight ok" if preflight.get("ok") else "run tools/preflight.py --run-tests",
        },
        {
            "id": "policy_replay",
            "label": "Policy replay",
            "status": "pass" if replay_candidate_passed else "warn" if replay_present else "missing",
            "detail": f"best={_best_policy(replay) or 'n/a'}",
        },
        {
            "id": "paper_forward",
            "label": "Paper forward",
            "status": "pass" if paper_forward_passed else "block" if live_canary_enabled else "warn",
            "detail": str(paper_forward.get("reason") or paper_forward.get("policy_name") or "paper forward report missing"),
        },
        {
            "id": "manual_approval",
            "label": "Manual approval",
            "status": "pass" if not live_canary_enabled or live_manual_approval else "block",
            "detail": "required before live canary",
        },
        {
            "id": "route_provider",
            "label": "Route and provider",
            "status": "pass" if live_require_route and live_require_provider_health else "block",
            "detail": f"route={live_require_route} provider_health={live_require_provider_health}",
        },
        {
            "id": "no_auto_live",
            "label": "No auto live",
            "status": "pass" if not auto_promote_live and not llm_trading_enabled else "block",
            "detail": f"auto_promote_live={auto_promote_live} llm_trading={llm_trading_enabled}",
        },
        {
            "id": "socials_soft_gate",
            "label": "Socials soft gate",
            "status": "pass" if not socials_hot_path_blocking and not green_require_socials else "block",
            "detail": f"hot_path_blocking={socials_hot_path_blocking} require_socials={green_require_socials}",
        },
        {
            "id": "model_registry",
            "label": "Model registry",
            "status": "pass" if model_families else "warn",
            "detail": f"families={len(model_families)}",
        },
        {
            "id": "drift",
            "label": "Drift monitor",
            "status": "warn" if drift.get("degraded") else "pass",
            "detail": str(drift.get("reason") or "ok"),
        },
    ]

    data = {
        "gates": gates,
        "invariants": {
            "auto_promote_live": auto_promote_live,
            "llm_trading_enabled": llm_trading_enabled,
            "socials_hot_path_blocking": socials_hot_path_blocking,
            "green_sniper_require_socials": green_require_socials,
            "live_canary_enabled": live_canary_enabled,
            "live_canary_manual_approval": live_manual_approval,
            "live_require_route": live_require_route,
            "live_require_provider_health": live_require_provider_health,
        },
        "preflight": {
            "ok": bool(preflight.get("ok")),
            "generated_at_utc": preflight.get("generated_at_utc"),
            "interpreter": preflight.get("interpreter"),
        },
        "config_effect_summary": config_audit.get("summary", {}),
        "policy_replay": {
            "current": replay_current,
            "candidate": replay_candidate,
            "candidate_passed": replay_candidate_passed,
            "best_by_total_pnl": _best_policy(replay),
        },
        "paper_forward": paper_forward,
        "model_registry": {
            "active_model_id": model_registry.get("active_model_id") if isinstance(model_registry, dict) else None,
            "families": model_families,
        },
        "drift": drift,
        "proposals": proposal_counts,
    }
    statuses = [
        _status_from_artifact(settings, "preflight_status.json"),
        _status_from_artifact(settings, "config_effect_audit.json"),
        _status_from_artifact(settings, "paper_forward_report.json"),
        json_status(source_key="ml.model_registry", path=_model_registry_path(settings), optional=True, empty_when_missing=True),
        jsonl_status(source_key="metrics.runtime_events", path=settings.runtime_events_path, optional=True),
        _derived_status("policy.safety"),
    ]
    return build_envelope(data, source_status=statuses, empty=False)

