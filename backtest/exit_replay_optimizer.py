from __future__ import annotations

from pathlib import Path
from typing import Any

from analytics.report_utils import fnum, is_severe_exit, load_candidate_outcomes, load_paper_positions, load_sqlite_positions, metrics_dir, write_json, write_markdown
from config.config import PROJECT_ROOT


EXIT_PROFILES = ("current", "defensive", "balanced", "runner", "moonbag", "post_partial_protected")


def _pnl(row: dict[str, Any]) -> float:
    return fnum(row.get("realized_pnl_pct") or row.get("total_pnl_pct") or row.get("pnl_pct") or row.get("target_total_pnl_pct"), 0.0)


def _peak(row: dict[str, Any], default: float) -> float:
    return fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct") or row.get("max_pnl_pct"), default)


def simulate_exit_profile(row: dict[str, Any], profile: str) -> float:
    current = _pnl(row)
    peak = _peak(row, current)
    if profile == "current":
        return current
    if profile == "defensive":
        return max(current, -12.0)
    if profile == "balanced" and peak >= 50:
        return max(current, peak * 0.30)
    if profile == "runner" and peak >= 100:
        return max(current, peak * 0.40)
    if profile == "moonbag" and peak >= 300:
        return max(current, peak * 0.45)
    if profile == "post_partial_protected" and peak >= 35:
        return max(current, max(20.0, peak - 5.0))
    return current


def build_exit_replay(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    report: dict[str, Any] = {}
    for profile in EXIT_PROFILES:
        pnls = [simulate_exit_profile(row, profile) for row in rows]
        report[profile] = {
            "trades": len(rows),
            "total_pnl": round(sum(pnls), 3) if pnls else 0.0,
            "avg_pnl": round(sum(pnls) / len(pnls), 3) if pnls else 0.0,
            "win_rate": round(100.0 * sum(1 for value in pnls if value > 0) / len(pnls), 3) if pnls else 0.0,
            "severe_losses": sum(1 for row, pnl in zip(rows, pnls) if is_severe_exit(row) or pnl <= -25),
            "runner_capture": round(
                sum(max(pnl, 0.0) / max(_peak(row, pnl), 1.0) for row, pnl in zip(rows, pnls)) / len(pnls),
                4,
            )
            if pnls
            else 0.0,
        }
    best = max(report.items(), key=lambda item: item[1]["total_pnl"])[0] if report else "current"
    report["recommendation"] = {"profile": best}
    return report


def write_exit_replay(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_exit_replay(root)
    write_json(metrics_dir(root) / "exit_replay_report.json", report)
    lines = ["# Exit Replay", "", "| Profile | Trades | Win rate | Avg PnL | Total PnL | Severe | Runner capture |", "|---|---:|---:|---:|---:|---:|---:|"]
    for profile in EXIT_PROFILES:
        stats = report[profile]
        lines.append(
            f"| {profile} | {stats['trades']} | {stats['win_rate']:.2f}% | {stats['avg_pnl']:.2f}% | "
            f"{stats['total_pnl']:.2f} | {stats['severe_losses']} | {stats['runner_capture']:.3f} |"
        )
    lines.append(f"\nRecommendation: `{report['recommendation']['profile']}`.")
    write_markdown(root / "docs" / "EXIT_REPLAY.md", lines)
    return report


__all__ = ["EXIT_PROFILES", "build_exit_replay", "simulate_exit_profile", "write_exit_replay"]
