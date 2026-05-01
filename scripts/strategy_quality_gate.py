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


def checks() -> list[str]:
    errors: list[str] = []
    replay = ROOT / "data" / "metrics" / "policy_replay.json"
    if _bool("POLICY_REPLAY_REQUIRED", False) and not replay.exists():
        errors.append("POLICY_REPLAY_REQUIRED=true but data/metrics/policy_replay.json is missing")
    if _bool("AUTO_PROMOTE_LIVE", False):
        errors.append("AUTO_PROMOTE_LIVE must remain false")
    if _bool("LLM_TRADING_ENABLED", False):
        errors.append("LLM_TRADING_ENABLED must remain false")
    if _bool("SOCIALS_HOT_PATH_BLOCKING", False) or _bool("GREEN_SNIPER_REQUIRE_SOCIALS", False):
        errors.append("socials must not be a hard gate")
    for name in ("GREEN_SNIPER_POLICY_MODE", "LATE_MOMENTUM_POLICY_MODE", "RESEARCH_RANK_POLICY_MODE"):
        if str(getattr(CFG, name, "") or "").strip().lower() == "enforce" and not _bool("ALLOW_LIVE_POLICY_ENFORCE", False):
            errors.append(f"{name}=enforce requires explicit ALLOW_LIVE_POLICY_ENFORCE")
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
