from __future__ import annotations

from pathlib import Path
from typing import Any

import json

from analytics.green_sniper_restricted_report import restricted_failures
from analytics.lane_policy_categories import (
    POLICY_GREEN_SNIPER_PURE,
    POLICY_GREEN_SNIPER_RESTRICTED_BUY,
    POLICY_GREEN_SNIPER_SHADOW,
    POLICY_LATE_MOMENTUM_WATCH,
    classify_policy_category,
)
from analytics.report_utils import fnum, is_severe_exit, load_candidate_outcomes, load_paper_positions, load_sqlite_positions, metrics_dir, write_json, write_markdown
from config.config import PROJECT_ROOT


POLICIES = (
    "current",
    "rules_only",
    "fix_missed_only",
    "risk_guard",
    "risk_guard_v2",
    "liq_guard",
    "risk_model_only",
    "rank_canary",
    "research_rank_canary",
    "score_recalibrated",
    "ev_model_only",
    "runner_model_only",
    "late_momentum_watch",
    "continuation_model",
    "early_dump",
    "early_dump_cut",
    "post_partial_protected",
    "combined_v1",
    "combined_policy_v1",
    "combined_policy_v2",
)

POST_ADJUSTMENT_POLICIES = (
    "baseline_48h",
    "research_rank_priority",
    "green_sniper_shadow_first",
    "green_sniper_restricted",
    "late_momentum_research_only",
    "post_partial_protected",
    "early_dump_candidates",
    "combined_adjusted_v1",
)


def _base_pnl(row: dict[str, Any]) -> float:
    return fnum(row.get("realized_pnl_pct") or row.get("total_pnl_pct") or row.get("pnl_pct") or row.get("target_total_pnl_pct"), 0.0)


def _simulate(row: dict[str, Any], policy: str) -> float:
    pnl = _base_pnl(row)
    reason = str(row.get("exit_reason") or row.get("reason") or "").upper()
    peak = fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct") or row.get("max_pnl_pct"), pnl)
    combined = policy in {"combined_v1", "combined_policy_v1", "combined_policy_v2"}
    if policy in {"risk_guard", "risk_guard_v2", "risk_model_only"} or combined:
        if reason in {"ADVERSE_TICK", "LIQUIDITY_CRUSH"}:
            return max(pnl, -18.0)
    if policy == "liq_guard" and reason == "LIQUIDITY_CRUSH":
        return max(pnl, -15.0)
    if policy == "risk_model_only":
        return pnl
    if policy == "ev_model_only" and fnum(row.get("ev_pred_pct"), pnl) < 0:
        return 0.0
    if policy == "runner_model_only":
        peak = fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct") or row.get("max_pnl_pct"), pnl)
        return max(pnl, peak * 0.30) if peak >= 100 else pnl
    if policy == "continuation_model" and str(row.get("entry_lane") or "") == "pump_early_late_momentum_watch":
        return max(pnl, fnum(row.get("continuation_peak_after_seen_3m"), pnl) * 0.25)
    if policy in {"early_dump", "early_dump_cut"} or combined:
        if pnl < -25 and peak < 15:
            return max(pnl, -12.0)
    if policy in {"post_partial_protected"} or combined:
        if peak >= 100 and pnl > 0:
            capture = 0.40 if policy == "combined_policy_v2" else 0.35
            return max(pnl, peak * capture)
    if policy in {"rank_canary", "research_rank_canary"} and str(row.get("entry_lane") or "").endswith("sniper_research") and fnum(row.get("rank_score"), 0) >= 61:
        return pnl
    return pnl


def _simulate_post_adjustment(row: dict[str, Any], policy: str) -> float:
    pnl = _base_pnl(row)
    reason = str(row.get("exit_reason") or row.get("reason") or "").upper()
    peak = fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct") or row.get("max_pnl_pct"), pnl)
    category = classify_policy_category(row)
    if policy in {"baseline_48h", "research_rank_priority"}:
        return pnl
    if policy == "green_sniper_shadow_first":
        return 0.0 if category in {POLICY_GREEN_SNIPER_PURE, POLICY_GREEN_SNIPER_SHADOW} else pnl
    if policy == "green_sniper_restricted":
        if category in {POLICY_GREEN_SNIPER_PURE, POLICY_GREEN_SNIPER_SHADOW, POLICY_GREEN_SNIPER_RESTRICTED_BUY}:
            return pnl if not restricted_failures(row) else 0.0
        return pnl
    if policy == "late_momentum_research_only":
        return 0.0 if category == POLICY_LATE_MOMENTUM_WATCH else pnl
    if policy == "post_partial_protected":
        if peak >= 35 and pnl > 0:
            return max(pnl, max(20.0, peak - 5.0))
        return pnl
    if policy == "early_dump_candidates":
        if reason == "EARLY_DUMP_CUT" or (pnl < -25 and peak < 15):
            return max(pnl, -12.0)
        return pnl
    if policy == "combined_adjusted_v1":
        if category in {POLICY_GREEN_SNIPER_PURE, POLICY_GREEN_SNIPER_SHADOW, POLICY_GREEN_SNIPER_RESTRICTED_BUY}:
            pnl = pnl if not restricted_failures(row) else 0.0
        if category == POLICY_LATE_MOMENTUM_WATCH:
            pnl = 0.0
        if reason == "EARLY_DUMP_CUT" or (pnl < -25 and peak < 15):
            pnl = max(pnl, -12.0)
        if peak >= 35 and pnl > 0:
            pnl = max(pnl, max(20.0, peak - 5.0))
        return pnl
    return pnl


def _summarize(rows: list[dict[str, Any]], policy: str) -> dict[str, Any]:
    pnls = [_simulate(row, policy) for row in rows]
    if not pnls:
        return {"trades": 0}
    by_category: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[tuple[dict[str, Any], float]]] = {}
    for row, pnl in zip(rows, pnls):
        grouped.setdefault(classify_policy_category(row), []).append((row, pnl))
    for category, items in grouped.items():
        cat_pnls = [pnl for _, pnl in items]
        by_category[category] = {
            "trades": len(cat_pnls),
            "win_rate": round(100.0 * sum(1 for value in cat_pnls if value > 0) / len(cat_pnls), 3),
            "avg_pnl": round(sum(cat_pnls) / len(cat_pnls), 3),
            "severe_loss_count": sum(1 for row, pnl in items if is_severe_exit(row) or pnl <= -25),
        }
    severe = sum(1 for row, pnl in zip(rows, pnls) if is_severe_exit(row) or pnl <= -25)
    return {
        "trades": len(pnls),
        "win_rate": round(100.0 * sum(1 for value in pnls if value > 0) / len(pnls), 3),
        "avg_pnl": round(sum(pnls) / len(pnls), 3),
        "median_pnl": round(sorted(pnls)[len(pnls) // 2], 3),
        "total_pnl": round(sum(pnls), 3),
        "severe_loss_count": severe,
        "missed_confirmed_winners": sum(1 for row in rows if str(row.get("classification") or "") == "confirmed_missed_winner"),
        "avoided_losers": sum(1 for row in rows if str(row.get("classification") or "") == "confirmed_avoided_loser"),
        "max_drawdown_proxy": round(min(0.0, min(pnls)), 3),
        "adverse_tick_count": sum(1 for row in rows if str(row.get("exit_reason") or row.get("reason")).upper() == "ADVERSE_TICK"),
        "liq_crush_count": sum(1 for row in rows if str(row.get("exit_reason") or row.get("reason")).upper() == "LIQUIDITY_CRUSH"),
        "lane_policy_category_breakdown": dict(sorted(by_category.items())),
        "runner_capture_ratio": round(
            sum(max(_simulate(row, policy), 0.0) / max(fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct"), _simulate(row, policy)), 1.0) for row in rows)
            / len(rows),
            4,
        ),
    }


def _summarize_post_adjustment(rows: list[dict[str, Any]], policy: str) -> dict[str, Any]:
    pnls = [_simulate_post_adjustment(row, policy) for row in rows]
    if not pnls:
        return {"trades": 0}
    grouped: dict[str, list[tuple[dict[str, Any], float]]] = {}
    for row, pnl in zip(rows, pnls):
        grouped.setdefault(classify_policy_category(row), []).append((row, pnl))
    by_category: dict[str, dict[str, Any]] = {}
    for category, items in grouped.items():
        cat_pnls = [pnl for _, pnl in items]
        by_category[category] = {
            "trades": len(cat_pnls),
            "win_rate": round(100.0 * sum(1 for value in cat_pnls if value > 0) / len(cat_pnls), 3),
            "avg_pnl": round(sum(cat_pnls) / len(cat_pnls), 3),
            "total_pnl": round(sum(cat_pnls), 3),
            "severe_loss_count": sum(1 for value in cat_pnls if value <= -25),
        }
    return {
        "trades": len(pnls),
        "win_rate": round(100.0 * sum(1 for value in pnls if value > 0) / len(pnls), 3),
        "avg_pnl": round(sum(pnls) / len(pnls), 3),
        "median_pnl": round(sorted(pnls)[len(pnls) // 2], 3),
        "total_pnl": round(sum(pnls), 3),
        "delta_total_pnl_vs_baseline": 0.0,
        "severe_loss_count": sum(1 for value in pnls if value <= -25),
        "max_drawdown_proxy": round(min(0.0, min(pnls)), 3),
        "runner_capture_ratio": round(
            sum(max(pnl, 0.0) / max(fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct"), pnl), 1.0) for row, pnl in zip(rows, pnls))
            / len(rows),
            4,
        ),
        "lane_policy_category_breakdown": dict(sorted(by_category.items())),
    }


def build_policy_replay(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    return {policy: _summarize(rows, policy) for policy in POLICIES}


def _baseline_reference(root: Path) -> dict[str, Any]:
    path = metrics_dir(root) / "post_run_48h_baseline.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "source": str(path),
        "window": payload.get("window") or {},
        "global": payload.get("global") or {},
    }


def build_post_adjustment_policy_replay(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    policies = {policy: _summarize_post_adjustment(rows, policy) for policy in POST_ADJUSTMENT_POLICIES}
    baseline_total = float((policies.get("baseline_48h") or {}).get("total_pnl") or 0.0)
    baseline_severe = int((policies.get("baseline_48h") or {}).get("severe_loss_count") or 0)
    for stats in policies.values():
        stats["delta_total_pnl_vs_baseline"] = round(float(stats.get("total_pnl") or 0.0) - baseline_total, 3)
        stats["delta_severe_loss_vs_baseline"] = int(stats.get("severe_loss_count") or 0) - baseline_severe
    return {
        "baseline_reference": _baseline_reference(root),
        "policies": policies,
    }


def write_policy_replay(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_policy_replay(root)
    write_json(metrics_dir(root) / "policy_replay.json", report)
    lines = ["# Policy Replay", "", "| Policy | Trades | Win rate | Avg PnL | Total PnL | Severe | Runner capture |", "|---|---:|---:|---:|---:|---:|---:|"]
    for key, stats in report.items():
        lines.append(
            f"| {key} | {stats.get('trades', 0)} | {stats.get('win_rate', 0):.2f}% | {stats.get('avg_pnl', 0):.2f}% | "
            f"{stats.get('total_pnl', 0):.2f} | {stats.get('severe_loss_count', 0)} | {stats.get('runner_capture_ratio', 0):.3f} |"
        )
    write_markdown(root / "docs" / "POLICY_REPLAY.md", lines)
    return report


def write_post_adjustment_policy_replay(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_post_adjustment_policy_replay(root)
    write_json(metrics_dir(root) / "post_adjustment_policy_replay.json", report)
    lines = [
        "# Post-adjustment Policy Replay",
        "",
        "| Policy | Trades | Win rate | Avg PnL | Total PnL | Delta PnL | Severe | Delta Severe | Runner capture |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, stats in report["policies"].items():
        lines.append(
            f"| {key} | {stats.get('trades', 0)} | {stats.get('win_rate', 0):.2f}% | "
            f"{stats.get('avg_pnl', 0):.2f}% | {stats.get('total_pnl', 0):.2f} | "
            f"{stats.get('delta_total_pnl_vs_baseline', 0):.2f} | {stats.get('severe_loss_count', 0)} | "
            f"{stats.get('delta_severe_loss_vs_baseline', 0)} | {stats.get('runner_capture_ratio', 0):.3f} |"
        )
    baseline = report.get("baseline_reference", {}).get("global", {})
    if baseline:
        lines.extend(
            [
                "",
                "## Frozen 48h Baseline Reference",
                "",
                f"- Closed trades: `{baseline.get('count')}`",
                f"- Win rate: `{baseline.get('win_rate_pct')}`",
                f"- Avg PnL: `{baseline.get('avg_pnl_pct')}`",
                f"- Severe losses: `{baseline.get('severe_loss_count')}`",
            ]
        )
    write_markdown(root / "docs" / "POST_ADJUSTMENT_REPLAY.md", lines)
    return report


__all__ = [
    "POLICIES",
    "POST_ADJUSTMENT_POLICIES",
    "build_policy_replay",
    "build_post_adjustment_policy_replay",
    "write_policy_replay",
    "write_post_adjustment_policy_replay",
]
