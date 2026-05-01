from __future__ import annotations

from pathlib import Path
from typing import Any

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


def _summarize(rows: list[dict[str, Any]], policy: str) -> dict[str, Any]:
    pnls = [_simulate(row, policy) for row in rows]
    if not pnls:
        return {"trades": 0}
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
        "runner_capture_ratio": round(
            sum(max(_simulate(row, policy), 0.0) / max(fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct"), _simulate(row, policy)), 1.0) for row in rows)
            / len(rows),
            4,
        ),
    }


def build_policy_replay(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    return {policy: _summarize(rows, policy) for policy in POLICIES}


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


__all__ = ["POLICIES", "build_policy_replay", "write_policy_replay"]
