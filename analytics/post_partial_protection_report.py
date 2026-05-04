from __future__ import annotations

from pathlib import Path
from typing import Any

from analytics.report_utils import metrics_dir, write_json, write_markdown
from backtest.exit_replay_optimizer import build_exit_replay
from config.config import PROJECT_ROOT


def build_post_partial_protection_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    replay = build_exit_replay(root)
    current = replay.get("current", {})
    protected = replay.get("post_partial_protected", {})
    return {
        "current": current,
        "post_partial_protected": protected,
        "delta": {
            "total_pnl": round(float(protected.get("total_pnl") or 0.0) - float(current.get("total_pnl") or 0.0), 3),
            "avg_pnl": round(float(protected.get("avg_pnl") or 0.0) - float(current.get("avg_pnl") or 0.0), 3),
            "win_rate": round(float(protected.get("win_rate") or 0.0) - float(current.get("win_rate") or 0.0), 3),
            "severe_losses": int(protected.get("severe_losses") or 0) - int(current.get("severe_losses") or 0),
            "runner_capture": round(
                float(protected.get("runner_capture") or 0.0) - float(current.get("runner_capture") or 0.0),
                4,
            ),
        },
    }


def write_post_partial_protection_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_post_partial_protection_report(root)
    write_json(metrics_dir(root) / "post_partial_protection_report.json", report)
    delta = report["delta"]
    lines = [
        "# Post Partial Protection Report",
        "",
        "| Profile | Trades | Win rate | Avg PnL | Total PnL | Severe | Runner capture |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key in ("current", "post_partial_protected"):
        stats: dict[str, Any] = report[key]
        lines.append(
            f"| {key} | {stats.get('trades', 0)} | {stats.get('win_rate', 0):.2f}% | "
            f"{stats.get('avg_pnl', 0):.2f}% | {stats.get('total_pnl', 0):.2f} | "
            f"{stats.get('severe_losses', 0)} | {stats.get('runner_capture', 0):.3f} |"
        )
    lines.extend(
        [
            "",
            "## Delta",
            "",
            f"- Total PnL: `{delta['total_pnl']:.3f}`",
            f"- Avg PnL: `{delta['avg_pnl']:.3f}`",
            f"- Win rate: `{delta['win_rate']:.3f}`",
            f"- Severe losses: `{delta['severe_losses']}`",
            f"- Runner capture: `{delta['runner_capture']:.4f}`",
        ]
    )
    write_markdown(root / "docs" / "POST_PARTIAL_PROTECTION_REPORT.md", lines)
    return report


__all__ = ["build_post_partial_protection_report", "write_post_partial_protection_report"]
