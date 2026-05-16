from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("CONFIG_PROFILE", "paper_hotfix_runner_v2")

from analytics import runner_turbo_monitor
from analytics.core_report_scheduler import REQUIRED_CORE_REPORTS, regenerate_core_reports
from analytics.runner_ladder import plan_ladder_partials
from analytics.untagged_buy_block import evaluate_untagged_buy_guard
from config.config import CFG
from scripts.strategy_quality_gate import checks as quality_checks
import analytics.exit_policy as exit_policy


def _ok(name: str, passed: bool, detail: object = None) -> dict[str, object]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def main() -> int:
    results: list[dict[str, object]] = []

    profile_path = ROOT / "config" / "profiles" / "paper_hotfix_runner_v2.env"
    results.append(_ok("config_loads", profile_path.exists() and getattr(CFG, "CONFIG_PROFILE", "") == "paper_hotfix_runner_v2"))

    gate_errors = quality_checks()
    results.append(_ok("quality_gate_ok_or_warn", isinstance(gate_errors, list), {"warnings": gate_errors[:20]}))

    report_summary = regenerate_core_reports(ROOT)
    generated_reports = report_summary.get("reports") if isinstance(report_summary, dict) else {}
    results.append(
        _ok(
            "reports_generate",
            all((ROOT / "data" / "metrics" / name).exists() for name in REQUIRED_CORE_REPORTS),
            {"reports": sorted((generated_reports or {}).keys())},
        )
    )

    blocked = evaluate_untagged_buy_guard({"entry_regime": "pump_early", "discovered_via": "dex"})
    results.append(_ok("untagged_buy_blocked", not blocked.allowed and blocked.reason == "untagged_buy_blocked"))

    rank = evaluate_untagged_buy_guard(
        {
            "entry_lane": "pump_early_research_rank_canary",
            "gate_profile": "research_rank_canary",
            "profit_lane_tier": "pump_early_research_rank_canary",
        }
    )
    results.append(_ok("rank_canary_conserves_lane", rank.allowed, rank.reason))

    rebound = evaluate_untagged_buy_guard(
        {
            "entry_lane": "pump_early_pumpswap_rebound_prime",
            "gate_profile": "pumpswap_rebound_prime",
            "profit_lane_tier": "pump_early_pumpswap_rebound_prime",
        }
    )
    results.append(_ok("rebound_conserves_lane", rebound.allowed, rebound.reason))

    ladder = plan_ladder_partials(pnl_pct=1000, entry_qty=1000, remaining_qty=1000, realized_qty=0)
    results.append(
        _ok(
            "ladder_executes_multiple_steps",
            int(ladder.get("pending_step_count") or 0) >= 6
            and len((ladder.get("next_state") or {}).get("executed_steps") or []) >= 6,
            ladder,
        )
    )

    now = dt.datetime.now(dt.timezone.utc)
    floor_subject = {
        "entry_regime": "pump_early",
        "opened_at": now - dt.timedelta(minutes=5),
        "buy_price_usd": 1.0,
        "highest_pnl_pct": 300.0,
        "partial_taken": True,
    }
    floor_reason = exit_policy.should_exit(floor_subject, price_now=2.0, now=now, pnl_pct=100.0)
    results.append(_ok("dynamic_floor_applies", floor_reason == "DYNAMIC_RUNNER_FLOOR", floor_reason))

    runner_turbo_monitor.reset_state()
    turbo_enter = runner_turbo_monitor.observe_position("SMOKE", peak_pct=100, dry_run=True)
    turbo_exit = runner_turbo_monitor.mark_closed("SMOKE")
    results.append(
        _ok(
            "turbo_enter_exit",
            turbo_enter.get("active") is True and turbo_exit.get("reason") == "closed",
            {"enter": turbo_enter.get("reason"), "exit": turbo_exit.get("reason")},
        )
    )

    live_off = (
        bool(getattr(CFG, "DRY_RUN", False))
        and not bool(getattr(CFG, "LIVE_CANARY_ENABLED", False))
        and not bool(getattr(CFG, "GREEN_SNIPER_LIVE_ENABLED", False))
        and not bool(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED", False))
        and bool(getattr(CFG, "RUNNER_TURBO_PAPER_ONLY", True))
    )
    results.append(_ok("live_still_off", live_off))

    failed = [item for item in results if not item["passed"]]
    print(json.dumps({"hotfix_smoke": "fail" if failed else "ok", "checks": results}, indent=2, sort_keys=True, default=str))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
