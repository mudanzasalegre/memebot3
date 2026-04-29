from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from config.config import CFG


def _bool(name: str, default: bool = False) -> bool:
    return bool(getattr(CFG, name, default))


def _float(name: str, default: float) -> float:
    try:
        return float(getattr(CFG, name, default))
    except Exception:
        return float(default)


def _int(name: str, default: int) -> int:
    try:
        return int(getattr(CFG, name, default))
    except Exception:
        return int(default)


def checks() -> list[str]:
    errors: list[str] = []
    if _bool("GREEN_SNIPER_LIVE_ENABLED", False):
        if _bool("DRY_RUN", True):
            errors.append("GREEN_SNIPER_LIVE_ENABLED=true requires DRY_RUN=0")
        if not _bool("GREEN_SNIPER_REQUIRE_ROUTE_LIVE", True):
            errors.append("live green sniper requires GREEN_SNIPER_REQUIRE_ROUTE_LIVE=true")
        if _float("GREEN_SNIPER_LIVE_SIZE_SOL", 0.10) > 0.10:
            errors.append("GREEN_SNIPER_LIVE_SIZE_SOL must be <=0.10 for canary")
        if _int("GREEN_SNIPER_LIVE_MAX_OPEN", 1) > 2:
            errors.append("GREEN_SNIPER_LIVE_MAX_OPEN must be <=2")
        if _float("GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL", 0.0) <= 0:
            errors.append("GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL must be defined")
        if _int("GREEN_SNIPER_LIVE_MAX_DAILY_BUYS", 0) <= 0:
            errors.append("GREEN_SNIPER_LIVE_MAX_DAILY_BUYS must be defined")
    if _bool("PAPER_SNIPER_MODE", False):
        if _bool("PAPER_PNL_STRICT_HEALTH", True) and _bool("PAPER_SNIPER_CONTINUE_ON_HEALTH", True):
            errors.append("PAPER_SNIPER_MODE with strict health contradicts PAPER_SNIPER_CONTINUE_ON_HEALTH")
    ranges = str(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES", "") or "")
    if "25:999" in ranges and _bool("GREEN_SNIPER_ENABLED", True):
        errors.append("PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES=25:999 can mask green sniper momentum")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warn-only", action="store_true")
    args = parser.parse_args()
    errors = checks()
    if errors:
        for error in errors:
            print(f"sniper_quality_gate=fail {error}")
        if not args.warn_only:
            raise SystemExit(1)
    print("sniper_quality_gate=ok" if not errors else "sniper_quality_gate=warn")


if __name__ == "__main__":
    main()
