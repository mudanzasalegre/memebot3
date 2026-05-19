from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analytics.pumpswap_prime_strict import evaluate_pumpswap_prime_strict
from analytics.report_utils import (
    address_of,
    boolish,
    fnum,
    load_candidate_outcomes,
    load_paper_positions,
    load_runtime_events,
    load_sqlite_positions,
    metrics_dir,
    write_json,
    write_markdown,
)
from config.config import CFG, PROJECT_ROOT


VALID_DIRECT_LANES = {
    "pump_early_research_rank_canary",
    "pump_early_pumpswap_rebound_prime",
    "pump_early_sniper_research",
    "pump_early_birth_probe_micro_canary",
    "pump_early_moonshot_micro_lottery",
}
REASON_UNTAGGED_BLOCKED = "untagged_buy_blocked"


@dataclass(frozen=True)
class UntaggedBuyDecision:
    allowed: bool
    reason: str
    entry_lane: str
    gate_profile: str
    profit_lane_tier: str
    failures: tuple[str, ...]


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _strict_prime_passed(row: dict[str, Any], *, cfg: Any = CFG) -> bool:
    if boolish(_first(row, "pumpswap_prime_strict_passed", "strict_passed"), False):
        return True
    if not bool(getattr(cfg, "PUMPSWAP_PRIME_STRICT_ENABLED", True)):
        return False
    return evaluate_pumpswap_prime_strict(row, cfg=cfg).allowed


def evaluate_untagged_buy_guard(row: dict[str, Any], *, cfg: Any = CFG) -> UntaggedBuyDecision:
    lane = _norm(_first(row, "entry_lane", "lane"))
    gate = _norm(_first(row, "gate_profile", "sniper_gate_profile", "live_profit_gate_profile"))
    tier = _norm(_first(row, "profit_lane_tier", "size_bucket"))
    discovered = _norm(_first(row, "discovered_via", "source"))
    regime = _norm(_first(row, "entry_regime"))

    if not bool(getattr(cfg, "REQUIRE_ENTRY_LANE_FOR_BUY", True)):
        return UntaggedBuyDecision(True, "entry_lane_guard_disabled", lane, gate, tier, ())

    failures: list[str] = []
    if not lane or lane in {"none", "null", "unknown", "legacy_none"}:
        failures.append("entry_lane_missing")
    if not gate or gate in {"none", "null", "unknown", "legacy_none"}:
        failures.append("gate_profile_missing")
    if not tier or tier in {"none", "null", "unknown", "legacy_none"}:
        failures.append("profit_lane_tier_missing")
    if boolish(_first(row, "pumpswap_prime_strict_blocked"), False) or tier == "pumpswap_prime_strict_blocked":
        failures.append("pumpswap_prime_not_strict")

    if lane in VALID_DIRECT_LANES and gate:
        if lane == "pump_early_sniper_research" and bool(
            getattr(cfg, "SNIPER_RESEARCH_SUBPROFILES_ENABLED", True)
        ):
            subprofile = _norm(_first(row, "sniper_research_subprofile", "entry_subprofile"))
            if subprofile not in {"sniper_research_high_activity", "sniper_research_momentum_ignition"}:
                failures.append("sniper_research_subprofile_missing")
        if not failures or failures == ["profit_lane_tier_missing"]:
            return UntaggedBuyDecision(True, "valid_tagged_lane", lane, gate, tier, ())

    if lane == "pump_early_pumpswap_profit":
        if gate != "pumpswap_profit_prime":
            failures.append("pumpswap_profit_not_prime")
        if not _strict_prime_passed(row, cfg=cfg):
            failures.append("pumpswap_prime_not_strict")
        if not failures:
            return UntaggedBuyDecision(True, "pumpswap_prime_strict", lane, gate, tier, ())

    if not bool(getattr(cfg, "ALLOW_UNTAGGED_STANDARD_BUY", False)):
        failures.append("untagged_standard_buy_disabled")
    if regime == "dex_mature" and not bool(getattr(cfg, "DEX_MATURE_STANDARD_BUY_ENABLED", False)):
        failures.append("dex_mature_standard_buy_disabled")
    if discovered in {"pumpfun", "pump", "pump_fun", "pumpportal"} and not bool(
        getattr(cfg, "PUMPFUN_STANDARD_BUY_ENABLED", False)
    ):
        failures.append("pumpfun_standard_buy_disabled")

    deduped = tuple(dict.fromkeys(failures or ["invalid_entry_lane_context"]))
    return UntaggedBuyDecision(False, REASON_UNTAGGED_BLOCKED, lane, gate, tier, deduped)


def apply_untagged_buy_shadow_context(row: dict[str, Any], decision: UntaggedBuyDecision) -> dict[str, Any]:
    row["untagged_buy_shadow"] = 1
    row["untagged_buy_block_reason"] = REASON_UNTAGGED_BLOCKED
    row["untagged_buy_block_failures"] = ",".join(decision.failures)
    return row


def _pnl(row: dict[str, Any]) -> float:
    return fnum(
        _first(row, "realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct"),
        0.0,
    )


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_pnl(row) for row in rows]
    if not rows:
        return {
            "rows": 0,
            "win_rate_pct": 0.0,
            "avg_pnl_pct": 0.0,
            "median_pnl_pct": 0.0,
            "total_pnl_pct_points": 0.0,
        }
    return {
        "rows": len(rows),
        "win_rate_pct": round(100.0 * sum(1 for value in pnls if value > 0.0) / len(pnls), 3),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        "median_pnl_pct": round(statistics.median(pnls), 3),
        "total_pnl_pct_points": round(sum(pnls), 3),
    }


def build_untagged_buy_block_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = (
        load_runtime_events(root)
        + load_candidate_outcomes(root)
        + load_paper_positions(root)
        + load_sqlite_positions(root)
    )
    blocked: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []
    by_reason: dict[str, int] = {}
    by_lane: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        decision = evaluate_untagged_buy_guard(row)
        target = allowed if decision.allowed else blocked
        target.append(row)
        lane = decision.entry_lane or "missing"
        by_lane.setdefault(lane, []).append(row)
        if not decision.allowed:
            for reason in decision.failures:
                by_reason[reason] = by_reason.get(reason, 0) + 1

    blocked_events = [
        row
        for row in rows
        if str(row.get("reason") or row.get("shadow_reason") or "").startswith(REASON_UNTAGGED_BLOCKED)
        or boolish(row.get("untagged_buy_shadow"), False)
    ]
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "require_entry_lane_for_buy": bool(getattr(CFG, "REQUIRE_ENTRY_LANE_FOR_BUY", True)),
            "allow_untagged_standard_buy": bool(getattr(CFG, "ALLOW_UNTAGGED_STANDARD_BUY", False)),
            "dex_mature_standard_buy_enabled": bool(getattr(CFG, "DEX_MATURE_STANDARD_BUY_ENABLED", False)),
            "pumpfun_standard_buy_enabled": bool(getattr(CFG, "PUMPFUN_STANDARD_BUY_ENABLED", False)),
            "untagged_buy_shadow_enabled": bool(getattr(CFG, "UNTAGGED_BUY_SHADOW_ENABLED", True)),
        },
        "summary": {
            "rows_evaluated": len(rows),
            "allowed_context_rows": len(allowed),
            "blocked_context_rows": len(blocked),
            "runtime_blocked_events": len(blocked_events),
        },
        "blocked_preview": [
            {
                "address": address_of(row),
                "entry_lane": row.get("entry_lane"),
                "gate_profile": row.get("gate_profile") or row.get("sniper_gate_profile"),
                "profit_lane_tier": row.get("profit_lane_tier"),
                "reason": row.get("reason") or row.get("shadow_reason"),
            }
            for row in blocked[:100]
        ],
        "blocked_by_reason": dict(sorted(by_reason.items(), key=lambda item: item[1], reverse=True)),
        "by_lane": {lane: _summary(items) for lane, items in sorted(by_lane.items())},
    }


def write_untagged_buy_block_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_untagged_buy_block_report(root)
    write_json(metrics_dir(root) / "untagged_buy_block_report.json", report)
    lines = [
        "# Untagged Buy Block",
        "",
        "Paper buys without a valid entry lane, gate profile and lane tier are routed to shadow.",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Rows evaluated | {report['summary']['rows_evaluated']} |",
        f"| Blocked context rows | {report['summary']['blocked_context_rows']} |",
        f"| Runtime blocked events | {report['summary']['runtime_blocked_events']} |",
        "",
        "## Blocked Reasons",
        "",
    ]
    for reason, count in report["blocked_by_reason"].items():
        lines.append(f"- `{reason}`: {count}")
    write_markdown(root / "docs" / "UNTAGGED_BUY_BLOCK.md", lines)
    return report


__all__ = [
    "REASON_UNTAGGED_BLOCKED",
    "UntaggedBuyDecision",
    "apply_untagged_buy_shadow_context",
    "build_untagged_buy_block_report",
    "evaluate_untagged_buy_guard",
    "write_untagged_buy_block_report",
]
