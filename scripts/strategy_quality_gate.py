from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.config import CFG
from analytics.core_report_scheduler import REQUIRED_CORE_REPORTS
from runtime.provider_health import provider_health_snapshot

CRITICAL_MODEL_WARNINGS = {
    "in_sample_only",
    "not_enough_rows",
    "single_class",
    "low_precision_at_k",
    "unstable_by_lane",
    "not_ready_for_enforcement",
}


def _bool(name: str, default: bool = False) -> bool:
    return bool(getattr(CFG, name, default))


def _float(name: str, default: float = 0.0) -> float:
    try:
        return float(getattr(CFG, name, default))
    except Exception:
        return default


def _int(name: str, default: int = 0) -> int:
    try:
        return int(getattr(CFG, name, default))
    except Exception:
        return default


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _truthy_text(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _falsey_text(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "n", "off"}


def _validate_paper_rank_research_profile(errors: list[str]) -> None:
    profile = ROOT / "config" / "profiles" / "paper_rank_research_v1.env"
    if not profile.exists():
        if _bool("PAPER_RANK_RESEARCH_PROFILE_REQUIRED", False):
            errors.append("paper_rank_research_v1 profile is required")
        return
    values = _parse_env_file(profile)
    required_true = (
        "DRY_RUN",
        "PAPER_SNIPER_MODE",
        "STRATEGY_OPTIMIZATION_LOCK",
        "RESEARCH_RANK_CANARY_ENABLED",
        "RESEARCH_RANK_CANARY_PAPER_ENABLED",
        "RESEARCH_RANK_CANARY_PREFER_REAL_LIQUIDITY",
        "GREEN_SNIPER_BUY_RESTRICTED_ENABLED",
        "LATE_MOMENTUM_WATCH_RESEARCH_ENABLED",
        "POST_PARTIAL_PROTECTION_ENABLED",
        "POST_PARTIAL_PROTECTION_PAPER_ENABLED",
    )
    required_false = (
        "LIVE_CANARY_ENABLED",
        "AUTO_PROMOTE_LIVE",
        "MODEL_AUTO_PROMOTE",
        "ML_AUTO_PROMOTE_LANES",
        "ML_ALLOW_RESEARCH_LIVE",
        "ML_ALLOW_UNKNOWN_LIVE",
        "ALLOW_LIVE_POLICY_ENFORCE",
        "RESEARCH_RANK_CANARY_LIVE_ENABLED",
        "GREEN_SNIPER_LIVE_ENABLED",
        "LATE_MOMENTUM_WATCH_BUY_ENABLED",
        "LATE_MOMENTUM_WATCH_AUTORESEARCH_ENABLED",
        "LATE_MOMENTUM_WATCH_LIVE_ENABLED",
        "POST_PARTIAL_PROTECTION_LIVE_ENABLED",
        "SOCIALS_HOT_PATH_BLOCKING",
        "GREEN_SNIPER_REQUIRE_SOCIALS",
    )
    for name in required_true:
        if not _truthy_text(values.get(name)):
            errors.append(f"paper_rank_research_v1 requires {name}=true")
    for name in required_false:
        if not _falsey_text(values.get(name)):
            errors.append(f"paper_rank_research_v1 requires {name}=false")
    if values.get("GREEN_SNIPER_POLICY_MODE", "").strip().lower() != "shadow":
        errors.append("paper_rank_research_v1 requires GREEN_SNIPER_POLICY_MODE=shadow")
    if values.get("RESEARCH_RANK_CANARY_MIN_SCORE", "").strip() not in {"0.647", "64.7", "64.81"}:
        errors.append("paper_rank_research_v1 requires RESEARCH_RANK_CANARY_MIN_SCORE=64.81")
    if values.get("RESEARCH_RANK_CANARY_MIN_PRICE5M", "").strip() != "40":
        errors.append("paper_rank_research_v1 requires RESEARCH_RANK_CANARY_MIN_PRICE5M=40")


def _model_enforcement_requested() -> bool:
    mode = str(getattr(CFG, "ML_GATE_MODE", "shadow") or "shadow").strip().lower()
    if mode in {"legacy", "enforce"}:
        return True
    if mode == "lane_aware":
        lane_modes = (
            str(getattr(CFG, "ML_RESEARCH_MODE", "shadow") or "shadow").strip().lower(),
            str(getattr(CFG, "ML_LIVE_PROFIT_MODE", "sizing_only") or "sizing_only").strip().lower(),
            str(getattr(CFG, "ML_UNKNOWN_LANE_MODE", "shadow") or "shadow").strip().lower(),
        )
        if "enforce" in lane_modes:
            return True
    return bool(
        _bool("GREEN_SNIPER_ML_BLOCK_ENABLED", False)
        or _bool("ML_GREEN_SNIPER_BLOCK_ENABLED", False)
        or (_bool("ML_RISK_VETO_ENABLED", False) and not _bool("ML_RISK_SHADOW_ONLY", True))
    )


def _critical_warnings_from_payload(payload: object) -> set[str]:
    found: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key in ("critical_warnings", "warnings"):
                items = value.get(key)
                if isinstance(items, list):
                    found.update(str(item) for item in items if str(item) in CRITICAL_MODEL_WARNINGS)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return found


def _validate_model_enforcement_warnings(errors: list[str]) -> None:
    if not _model_enforcement_requested():
        return
    report_paths = (
        ROOT / "data" / "metrics" / "model_training_report.json",
        ROOT / "data" / "metrics" / "risk_model_report.json",
        ROOT / "data" / "metrics" / "ev_model_report.json",
        ROOT / "data" / "metrics" / "runner_model_report.json",
        ROOT / "data" / "metrics" / "continuation_model_report.json",
    )
    existing = [path for path in report_paths if path.exists()]
    if not existing:
        errors.append("model enforcement requires model training reports without critical warnings")
        return
    for path in existing:
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            errors.append(f"model enforcement cannot parse {path.relative_to(ROOT)}")
            continue
        critical = sorted(_critical_warnings_from_payload(payload))
        if critical:
            errors.append(
                f"model enforcement blocked by critical warnings in {path.relative_to(ROOT)}: {','.join(critical)}"
            )


def _payload_has_test_event(value: object) -> bool:
    if isinstance(value, dict):
        if str(value.get("run_id") or "").strip().upper() == "SMOKE":
            return True
        raw = value.get("test_event")
        if isinstance(raw, bool) and raw:
            return True
        if str(raw or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
        return any(_payload_has_test_event(child) for child in value.values())
    if isinstance(value, list):
        return any(_payload_has_test_event(child) for child in value)
    return False


def checks() -> list[str]:
    errors: list[str] = []
    replay = ROOT / "data" / "metrics" / "policy_replay.json"
    paper_forward = ROOT / "data" / "metrics" / "paper_forward_report.json"
    model_root = ROOT / "ml" / "models"
    _validate_paper_rank_research_profile(errors)
    _validate_model_enforcement_warnings(errors)
    if _bool("STRATEGY_OPTIMIZATION_LOCK", True):
        if not _bool("DRY_RUN", True):
            errors.append("STRATEGY_OPTIMIZATION_LOCK=true requires DRY_RUN=true")
        blocked_flags = (
            "LIVE_CANARY_ENABLED",
            "GREEN_SNIPER_LIVE_ENABLED",
            "RESEARCH_RANK_CANARY_LIVE_ENABLED",
            "LATE_MOMENTUM_WATCH_LIVE_ENABLED",
            "LIVE_AGGRESSIVE_TRADING_ENABLED",
            "BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED",
            "RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED",
            "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED",
            "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED",
            "AUTO_PROMOTE_LIVE",
            "MODEL_AUTO_PROMOTE",
            "ML_AUTO_PROMOTE_LANES",
            "ML_ALLOW_RESEARCH_LIVE",
            "ML_ALLOW_UNKNOWN_LIVE",
            "ALLOW_LIVE_POLICY_ENFORCE",
        )
        for name in blocked_flags:
            if _bool(name, False):
                errors.append(f"STRATEGY_OPTIMIZATION_LOCK=true blocks {name}=true")
    if _bool("POLICY_REPLAY_REQUIRED", False) and not replay.exists():
        errors.append("POLICY_REPLAY_REQUIRED=true but data/metrics/policy_replay.json is missing")
    if _bool("AUTO_PROMOTE_LIVE", False):
        errors.append("AUTO_PROMOTE_LIVE must remain false")
    if _bool("MODEL_AUTO_PROMOTE", False):
        errors.append("MODEL_AUTO_PROMOTE must remain false")
    if _bool("LATE_MOMENTUM_WATCH_AUTORESEARCH_ENABLED", False):
        errors.append("LATE_MOMENTUM_WATCH_AUTORESEARCH_ENABLED must remain false")
    if not _bool("REQUIRE_ENTRY_LANE_FOR_BUY", True):
        errors.append("REQUIRE_ENTRY_LANE_FOR_BUY must remain true")
    if _bool("ALLOW_UNTAGGED_STANDARD_BUY", False):
        errors.append("ALLOW_UNTAGGED_STANDARD_BUY must remain false")
    if not _bool("PUMPSWAP_PRIME_STRICT_ENABLED", True):
        errors.append("PUMPSWAP_PRIME_STRICT_ENABLED must remain true")
    if _bool("PUMP_EARLY_PROFIT_LANE_ENABLED", False) and not _bool("PUMPSWAP_PRIME_STRICT_ENABLED", True):
        errors.append("PUMP_EARLY_PROFIT_LANE_ENABLED=true requires PUMPSWAP_PRIME_STRICT_ENABLED=true")
    if _bool("PUMPSWAP_PRIME_STRICT_ENABLED", True) and not _bool("PUMPSWAP_PRIME_SHADOW_IF_NOT_STRICT", True):
        errors.append("PUMPSWAP_PRIME_STRICT_ENABLED=true requires PUMPSWAP_PRIME_SHADOW_IF_NOT_STRICT=true")
    if _bool("POST_PARTIAL_PROTECTION_LIVE_ENABLED", False):
        errors.append("POST_PARTIAL_PROTECTION_LIVE_ENABLED must remain false")
    if _bool("BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED", False):
        errors.append("BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED must remain false")
    if _bool("RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED", False):
        errors.append("RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED must remain false")
    if _bool("BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED", False):
        errors.append("BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED must remain false")
    if _bool("MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED", False):
        errors.append("MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED must remain false")
    if _float("MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL", 0.002) > 0.005:
        errors.append("MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL must stay <=0.005")
    if _float("MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_AMOUNT_SOL", 0.001) > 0.002:
        errors.append("MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_AMOUNT_SOL must stay <=0.002")
    if _int("MOONSHOT_MICRO_LOTTERY_MAX_OPEN", 1) > 1:
        errors.append("MOONSHOT_MICRO_LOTTERY_MAX_OPEN must stay <=1")
    if _float("PAPER_EXPLORATION_AMOUNT_SOL", 0.005) > 0.01:
        errors.append("PAPER_EXPLORATION_AMOUNT_SOL must stay <=0.01")
    if _float("RESEARCH_RANK_CANARY_SIZE_SOL", 0.01) > 0.02:
        errors.append("RESEARCH_RANK_CANARY_SIZE_SOL must stay <=0.02")
    if _float("RESEARCH_RANK_CANARY_MAX_SIZE_SOL", 0.02) > 0.02:
        errors.append("RESEARCH_RANK_CANARY_MAX_SIZE_SOL must stay <=0.02")
    if _float("RESEARCH_RANK_CANARY_PULLBACK_TAIL_AMOUNT_SOL", 0.005) > 0.005:
        errors.append("RESEARCH_RANK_CANARY_PULLBACK_TAIL_AMOUNT_SOL must stay <=0.005")
    if _bool("STRATEGY_OPTIMIZATION_LOCK", True):
        if _bool("RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED", False):
            errors.append("STRATEGY_OPTIMIZATION_LOCK=true requires RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED=false")
        if _bool("RESEARCH_RANK_CANARY_PULLBACK_BUY_ENABLED", False):
            errors.append("STRATEGY_OPTIMIZATION_LOCK=true requires RESEARCH_RANK_CANARY_PULLBACK_BUY_ENABLED=false")
    if _float("BIRD_TP1_PCT", 25.0) <= 0:
        errors.append("partial ladder requires BIRD_TP1_PCT > 0")
    if _bool("STRATEGY_OPTIMIZATION_LOCK", True) and not _bool("RUNNER_TURBO_PAPER_ONLY", True):
        errors.append("STRATEGY_OPTIMIZATION_LOCK=true requires RUNNER_TURBO_PAPER_ONLY=true")
    if _bool("LLM_TRADING_ENABLED", False):
        errors.append("LLM_TRADING_ENABLED must remain false")
    if _bool("SOCIALS_HOT_PATH_BLOCKING", False) or _bool("GREEN_SNIPER_REQUIRE_SOCIALS", False):
        errors.append("socials must not be a hard gate")
    for name in ("GREEN_SNIPER_POLICY_MODE", "LATE_MOMENTUM_POLICY_MODE", "RESEARCH_RANK_POLICY_MODE"):
        if str(getattr(CFG, name, "") or "").strip().lower() == "enforce" and not _bool("ALLOW_LIVE_POLICY_ENFORCE", False):
            errors.append(f"{name}=enforce requires explicit ALLOW_LIVE_POLICY_ENFORCE")
    if _bool("LIVE_CANARY_ENABLED", False):
        if _int("LIVE_CANARY_MAX_OPEN", 1) > 1:
            errors.append("LIVE_CANARY_MAX_OPEN must stay <=1")
        if _int("LIVE_CANARY_MAX_DAILY_BUYS", 3) > 3:
            errors.append("LIVE_CANARY_MAX_DAILY_BUYS must stay <=3")
        if _float("LIVE_CANARY_DAILY_LOSS_CAP_SOL", 0.05) <= 0:
            errors.append("LIVE_CANARY_DAILY_LOSS_CAP_SOL is required")
        if not _bool("LIVE_REQUIRE_ROUTE", True):
            errors.append("LIVE_CANARY requires LIVE_REQUIRE_ROUTE=true")
        if not _bool("LIVE_REQUIRE_PROVIDER_HEALTH", True):
            errors.append("LIVE_CANARY requires LIVE_REQUIRE_PROVIDER_HEALTH=true")
        if not _bool("LIVE_CANARY_MANUAL_APPROVAL", False):
            errors.append("LIVE_CANARY requires LIVE_CANARY_MANUAL_APPROVAL=true")
        if not replay.exists():
            errors.append("LIVE_CANARY requires data/metrics/policy_replay.json")
        if not paper_forward.exists():
            errors.append("LIVE_CANARY requires data/metrics/paper_forward_report.json")
        if not model_root.exists():
            errors.append("LIVE_CANARY requires ml/models registry directory")
    if _bool("GREEN_SNIPER_LIVE_ENABLED", False):
        if _bool("DRY_RUN", True):
            errors.append("live canary requires DRY_RUN=0")
        if not _bool("GREEN_SNIPER_REQUIRE_ROUTE_LIVE", True):
            errors.append("live canary requires GREEN_SNIPER_REQUIRE_ROUTE_LIVE=true")
        if _float("GREEN_SNIPER_LIVE_SIZE_SOL", 0.01) > 0.01:
            errors.append("GREEN_SNIPER_LIVE_SIZE_SOL must stay <=0.01 in safe canary")
        if _int("GREEN_SNIPER_LIVE_MAX_OPEN", 1) > 1:
            errors.append("GREEN_SNIPER_LIVE_MAX_OPEN must stay <=1 in safe canary")
        if _float("GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL", 0.0) <= 0:
            errors.append("GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL is required")
        if _int("GREEN_SNIPER_LIVE_MAX_DAILY_BUYS", 0) <= 0:
            errors.append("GREEN_SNIPER_LIVE_MAX_DAILY_BUYS is required")
        provider_health = provider_health_snapshot()
        if provider_health.get("overall_status") == "critical":
            errors.append("provider health critical; live canary must not start")
    if _bool("PAPER_SNIPER_MODE", False):
        if not _bool("GREEN_SNIPER_REJECT_SHADOW_ENABLED", True):
            errors.append("paper sniper requires GREEN_SNIPER_REJECT_SHADOW_ENABLED=true for high-risk shadows")
    missing_core_reports = [
        name
        for name in REQUIRED_CORE_REPORTS
        if not (ROOT / "data" / "metrics" / name).exists()
    ]
    if missing_core_reports and not _bool("CORE_REPORTS_AUTO_REGEN_ENABLED", True):
        errors.append(
            "CORE_REPORTS_AUTO_REGEN_ENABLED=false with missing critical reports: "
            + ",".join(missing_core_reports)
        )
    metrics_root = ROOT / "data" / "metrics"
    current_run_summary = metrics_root / "current_run_summary.json"
    reports_present = metrics_root.exists() and any((metrics_root / name).exists() for name in REQUIRED_CORE_REPORTS)
    if reports_present and not current_run_summary.exists():
        errors.append("data/metrics/current_run_summary.json is missing")
    for name in REQUIRED_CORE_REPORTS:
        path = ROOT / "data" / "metrics" / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        if _payload_has_test_event(payload):
            errors.append(f"{name} includes test_event/SMOKE data by default")
    ranges = str(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES", "") or "")
    if "25:999" in ranges and _bool("GREEN_SNIPER_ENABLED", True):
        errors.append("price5m 25:999 block contradicts green sniper")
    missed = ROOT / "data" / "metrics" / "missed_pumps.json"
    if missed.exists():
        try:
            payload = json.loads(missed.read_text(encoding="utf-8", errors="ignore"))
            rows = payload
            if isinstance(payload, dict):
                rows = payload.get("data") or payload.get("rows") or []
            if rows and isinstance(rows, list) and "confirmed_later_peak_pct" not in rows[0]:
                errors.append("missed_pumps.json uses legacy schema; regenerate tools/missed_pumps_report.py")
        except Exception:
            errors.append("missed_pumps.json cannot be parsed")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warn-only", action="store_true")
    args = parser.parse_args()
    errors = checks()
    for error in errors:
        print(f"strategy_quality_gate=fail {error}")
    if errors and not args.warn_only:
        raise SystemExit(1)
    print("strategy_quality_gate=ok" if not errors else "strategy_quality_gate=warn")


if __name__ == "__main__":
    main()
