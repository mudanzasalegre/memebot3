from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from config.config import CFG

if TYPE_CHECKING:
    from api.settings import APISettings


ACCEPTED_REPLAY_STATUSES = {"accepted_replay", "accepted_paper"}
LIVE_PROFILE_NAME = "ui_live_start_profile.env"
RUNTIME_FRESH_S = 15
RUNTIME_STALE_S = 60
RUNTIME_ERROR_S = 180


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def _parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _runtime_snapshot_freshness(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return "error"
    updated_at = _parse_dt(snapshot.get("updated_at"))
    if updated_at is None:
        return "error"
    age_s = max(0.0, (dt.datetime.now(dt.timezone.utc) - updated_at).total_seconds())
    if age_s > RUNTIME_ERROR_S:
        return "error"
    if age_s > RUNTIME_STALE_S:
        return "degraded"
    if snapshot.get("last_error"):
        return "degraded"
    if age_s > RUNTIME_FRESH_S:
        return "stale"
    return "fresh"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _gate(gate_id: str, label: str, passed: bool, detail: str, *, value: Any = None, required: Any = None) -> dict[str, Any]:
    return {
        "id": gate_id,
        "label": label,
        "status": "pass" if passed else "block",
        "detail": detail,
        "value": value,
        "required": required,
    }


def _latest_accepted_candidate(settings: APISettings) -> dict[str, Any] | None:
    path = settings.data_dir / "research_runs" / "scoreboard.json"
    payload = _read_json(path)
    rows = payload if isinstance(payload, list) else []
    accepted = [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("status") or "").strip().lower() in ACCEPTED_REPLAY_STATUSES
        and _float(row.get("objective_score"), 0.0) > 0.0
    ]
    if not accepted:
        return None
    return sorted(accepted, key=lambda row: str(row.get("evaluated_at_utc") or row.get("created_at_utc") or ""), reverse=True)[0]


def build_live_promotion_preflight(
    settings: APISettings,
    *,
    runtime_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metrics_dir = settings.metrics_dir
    current_summary = _read_json(metrics_dir / "current_run_summary.json")
    api_budget = _read_json(settings.data_dir / "research_runs" / "api_budget.json")
    if not isinstance(api_budget, dict):
        api_budget = _read_json(metrics_dir / "api_budget_report.json")
    if not isinstance(current_summary, dict):
        current_summary = {}
    if not isinstance(api_budget, dict):
        api_budget = {}

    wallet_sol = _float((runtime_snapshot or {}).get("wallet_sol"), -1.0)
    min_trade_sol = max(
        _float(getattr(CFG, "MIN_BUY_SOL", 0.1), 0.1),
        _float(getattr(CFG, "LIVE_CANARY_SIZE_SOL", 0.01), 0.01),
        _float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_SIZE_SOL", 0.02), 0.02),
    )
    required_wallet_sol = max(
        _float(getattr(CFG, "MIN_SOL_BALANCE", 0.01), 0.01)
        + _float(getattr(CFG, "GAS_RESERVE_SOL", 0.05), 0.05)
        + min_trade_sol,
        0.02,
    )
    api_429_count = _int(api_budget.get("api_429_count") or current_summary.get("api_429_count"))
    if api_429_count <= 0:
        api_429_count = sum(
            _int(api_budget.get(key))
            for key in (
                "birdeye_429_count",
                "gecko_429_count",
                "jupiter_rate_limit_count",
                "jupiter_429_count",
            )
        )
    provider_degraded_minutes = _int(
        api_budget.get("provider_degraded_minutes") or current_summary.get("provider_degraded_minutes")
    )
    max_api_429 = _int(getattr(CFG, "LIVE_PROMOTION_MAX_API_429_COUNT", 50), 50)
    max_provider_degraded = _int(getattr(CFG, "LIVE_PROMOTION_MAX_PROVIDER_DEGRADED_MINUTES", 0), 0)
    closed_trades = _int(current_summary.get("closed_trades") or current_summary.get("closed_positions"))
    min_closed_trades = _int(getattr(CFG, "LIVE_PROMOTION_MIN_PAPER_CLOSED_TRADES", 25), 25)
    buys = _int(current_summary.get("buys") or current_summary.get("buy_count"))
    accepted_candidate = _latest_accepted_candidate(settings)
    freshness = _runtime_snapshot_freshness(runtime_snapshot)

    gates = [
        _gate(
            "wallet_sol",
            "Wallet SOL",
            wallet_sol >= required_wallet_sol,
            "Enough SOL is available for one live canary buy plus gas reserve."
            if wallet_sol >= required_wallet_sol
            else "Wallet balance is missing or below the live canary reserve.",
            value=None if wallet_sol < 0 else round(wallet_sol, 6),
            required=round(required_wallet_sol, 6),
        ),
        _gate(
            "api_budget",
            "API budget",
            api_429_count <= max_api_429 and provider_degraded_minutes <= max_provider_degraded,
            "Provider budget is within live bounds."
            if api_429_count <= max_api_429 and provider_degraded_minutes <= max_provider_degraded
            else "Provider/API health is too degraded for live promotion.",
            value={"api_429_count": api_429_count, "provider_degraded_minutes": provider_degraded_minutes},
            required={"api_429_count_max": max_api_429, "provider_degraded_minutes_max": max_provider_degraded},
        ),
        _gate(
            "paper_sample",
            "Paper sample",
            closed_trades >= min_closed_trades or buys >= min_closed_trades,
            "Paper mode has enough closed/bought samples."
            if closed_trades >= min_closed_trades or buys >= min_closed_trades
            else "Paper mode has not collected enough buy/outcome samples yet.",
            value={"buys": buys, "closed_trades": closed_trades},
            required={"min_closed_trades_or_buys": min_closed_trades},
        ),
        _gate(
            "accepted_research",
            "Accepted research candidate",
            accepted_candidate is not None,
            "AutoResearch has an accepted positive candidate."
            if accepted_candidate is not None
            else "AutoResearch has not accepted a positive candidate yet.",
            value={
                "run_id": accepted_candidate.get("run_id"),
                "objective_score": accepted_candidate.get("objective_score"),
            }
            if accepted_candidate
            else None,
            required="accepted_replay_or_accepted_paper_with_positive_score",
        ),
        _gate(
            "runtime_not_live",
            "Runtime posture",
            not bool((runtime_snapshot or {}).get("dry_run") is False and freshness in {"fresh", "stale"}),
            "No fresh live runtime is already active."
            if not bool((runtime_snapshot or {}).get("dry_run") is False and freshness in {"fresh", "stale"})
            else "A live runtime already appears active.",
            value={"dry_run": (runtime_snapshot or {}).get("dry_run"), "freshness": freshness},
            required="no_active_live_runtime",
        ),
    ]
    passed = all(str(gate["status"]) == "pass" for gate in gates)
    return {
        "generated_at_utc": _utc_now(),
        "passed": passed,
        "mode": "live_ready" if passed else "paper_acquisition",
        "gates": gates,
        "profile_path": str(settings.runtime_dir / LIVE_PROFILE_NAME),
        "accepted_candidate": accepted_candidate,
    }


def write_live_start_profile(settings: APISettings, preflight: dict[str, Any]) -> Path:
    if not bool(preflight.get("passed")):
        raise RuntimeError("live_preflight_not_passed")
    path = settings.runtime_dir / LIVE_PROFILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    live_canary_size = min(_float(getattr(CFG, "LIVE_CANARY_SIZE_SOL", 0.01), 0.01), 0.01)
    rank_priority_size = min(_float(getattr(CFG, "RESEARCH_RANK_CANARY_PRIORITY_SIZE_SOL", 0.02), 0.02), 0.02)
    values = {
        "DRY_RUN": "0",
        "STRATEGY_OPTIMIZATION_LOCK": "false",
        "REQUIRE_ENTRY_LANE_FOR_BUY": "true",
        "ALLOW_UNTAGGED_STANDARD_BUY": "false",
        "DEX_MATURE_STANDARD_BUY_ENABLED": "false",
        "PUMPFUN_STANDARD_BUY_ENABLED": "false",
        "LIVE_CANARY_ENABLED": "true",
        "LIVE_REQUIRE_ROUTE": "true",
        "LIVE_REQUIRE_PROVIDER_HEALTH": "true",
        "LIVE_CANARY_MAX_OPEN": "1",
        "LIVE_CANARY_MAX_DAILY_BUYS": "3",
        "LIVE_CANARY_SIZE_SOL": f"{live_canary_size:.6f}",
        "RESEARCH_RANK_CANARY_LIVE_ENABLED": "true",
        "RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED": "false",
        "RESEARCH_RANK_CANARY_PRIORITY_ONLY": "true",
        "RESEARCH_RANK_CANARY_PRIORITY_SIZE_SOL": f"{rank_priority_size:.6f}",
        "RESEARCH_RANK_CANARY_MAX_OPEN": "1",
        "RESEARCH_RANK_CANARY_MAX_DAILY_BUYS": "3",
    }
    lines = [
        "# Generated by UI live promotion preflight.",
        f"# generated_at_utc={_utc_now()}",
    ]
    for key, value in values.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


__all__ = [
    "LIVE_PROFILE_NAME",
    "build_live_promotion_preflight",
    "write_live_start_profile",
]
