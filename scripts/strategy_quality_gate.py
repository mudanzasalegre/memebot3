from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.config import CFG
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
        "LATE_MOMENTUM_WATCH_AUTORESEARCH_ENABLED",
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
    if values.get("RESEARCH_RANK_CANARY_MIN_SCORE", "").strip() not in {"0.647", "64.7"}:
        errors.append("paper_rank_research_v1 requires RESEARCH_RANK_CANARY_MIN_SCORE=0.647")


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
    ranges = str(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES", "") or "")
    if "25:999" in ranges and _bool("GREEN_SNIPER_ENABLED", True):
        errors.append("price5m 25:999 block contradicts green sniper")
    missed = ROOT / "data" / "metrics" / "missed_pumps.json"
    if missed.exists():
        try:
            payload = json.loads(missed.read_text(encoding="utf-8", errors="ignore"))
            if payload and "confirmed_later_peak_pct" not in payload[0]:
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
