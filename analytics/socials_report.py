from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from analytics.reporting import load_feature_snapshots, load_positions_frame
from analytics.social_signal import SOCIAL_ENRICHMENT_EVENTS_PATH
from config.config import PROJECT_ROOT
from trade_pnl import total_pnl_pct_from_record


SOCIALS_REPORT_JSON = PROJECT_ROOT / "data" / "metrics" / "socials_report.json"
SOCIALS_REPORT_MD = PROJECT_ROOT / "docs" / "SOCIALS_REPORT.md"


def _round(value: Any, digits: int = 3) -> Any:
    try:
        if value is None or pd.isna(value):
            return None
        return round(float(value), digits)
    except Exception:
        return value


def _load_social_events(path: Path = SOCIAL_ENRICHMENT_EVENTS_PATH) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    if "ts_utc" in frame.columns:
        frame["ts_utc"] = pd.to_datetime(frame["ts_utc"], utc=True, errors="coerce")
    return frame


def _latest_social_rows(features_dir: Path | None = None) -> pd.DataFrame:
    snapshots = load_feature_snapshots(features_dir=features_dir)
    social_events = _load_social_events()
    frames: list[pd.DataFrame] = []
    if not snapshots.empty and "address" in snapshots.columns:
        keep = [
            col
            for col in (
                "address",
                "entry_lane",
                "social_status",
                "social_ok",
                "social_link_count",
                "social_risk_flags",
            )
            if col in snapshots.columns
        ]
        if keep:
            frame = snapshots[keep].copy()
            frame["source"] = "features"
            frames.append(frame)
    if not social_events.empty and "address" in social_events.columns:
        frame = social_events.sort_values("ts_utc", kind="mergesort").drop_duplicates("address", keep="last").copy()
        if "lane" in frame.columns and "entry_lane" not in frame.columns:
            frame["entry_lane"] = frame["lane"]
        frame["source"] = "events"
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True, sort=False)
    return combined.drop_duplicates("address", keep="last")


def build_socials_report(*, db_path: Path | None = None, features_dir: Path | None = None) -> dict[str, Any]:
    social = _latest_social_rows(features_dir=features_dir)
    positions = load_positions_frame(db_path=db_path)
    closed = pd.DataFrame()
    if not positions.empty:
        closed = positions[positions.get("closed", 0).fillna(0).astype(int) == 1].copy()
        if not closed.empty:
            closed["computed_total_pnl_pct"] = closed.apply(total_pnl_pct_from_record, axis=1)
            closed["social_status"] = "unknown"
            closed["social_link_count"] = pd.NA
            closed["social_risk_flags"] = ""
            if not social.empty:
                for column, default in (
                    ("social_status", "unknown"),
                    ("social_link_count", pd.NA),
                    ("social_risk_flags", ""),
                    ("entry_lane", pd.NA),
                ):
                    if column not in social.columns:
                        social[column] = default
                closed = closed.merge(
                    social[["address", "social_status", "social_link_count", "social_risk_flags", "entry_lane"]],
                    on="address",
                    how="left",
                    suffixes=("", "_social"),
                )
                closed["social_status"] = closed["social_status_social"].fillna(closed["social_status"]).fillna("unknown")
                closed["social_link_count"] = closed["social_link_count_social"].combine_first(closed["social_link_count"])
                closed["social_risk_flags"] = closed["social_risk_flags_social"].fillna(closed["social_risk_flags"])
                if "entry_lane_social" in closed.columns:
                    closed["entry_lane"] = closed.get("entry_lane").fillna(closed["entry_lane_social"])

    status_rows: list[dict[str, Any]] = []
    lane_rows: list[dict[str, Any]] = []
    if not closed.empty:
        for status, group in closed.groupby(closed["social_status"].fillna("unknown")):
            pnl = pd.to_numeric(group["computed_total_pnl_pct"], errors="coerce")
            status_rows.append(
                {
                    "social_status": str(status),
                    "trades": int(len(group)),
                    "win_rate_pct": _round((pnl > 0).mean() * 100.0, 3),
                    "avg_pnl_pct": _round(pnl.mean(), 3),
                    "median_pnl_pct": _round(pnl.median(), 3),
                    "total_pnl_pct_points": _round(pnl.sum(), 3),
                    "severe_losses": int(pnl.le(-25.0).sum()),
                }
            )
        for (lane, status), group in closed.groupby([
            closed.get("entry_lane", pd.Series("unknown", index=closed.index)).fillna("unknown"),
            closed["social_status"].fillna("unknown"),
        ]):
            pnl = pd.to_numeric(group["computed_total_pnl_pct"], errors="coerce")
            lane_rows.append(
                {
                    "entry_lane": str(lane),
                    "social_status": str(status),
                    "trades": int(len(group)),
                    "win_rate_pct": _round((pnl > 0).mean() * 100.0, 3),
                    "avg_pnl_pct": _round(pnl.mean(), 3),
                    "median_pnl_pct": _round(pnl.median(), 3),
                }
            )

    status_counts = (
        social["social_status"].fillna("unknown").value_counts().to_dict()
        if not social.empty and "social_status" in social.columns
        else {}
    )
    present = int(status_counts.get("present", 0) + status_counts.get("suspicious", 0))
    total = int(sum(status_counts.values()))
    return {
        "project_root": str(PROJECT_ROOT),
        "socials_coverage_pct": _round((present / total * 100.0) if total else None, 3),
        "social_rows": int(total),
        "status_counts": {str(key): int(value) for key, value in status_counts.items()},
        "performance_by_status": status_rows,
        "performance_by_lane_status": lane_rows,
        "social_events_rows": int(len(_load_social_events())),
        "closed_trades_joined": int(len(closed)),
    }


def render_socials_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Socials Report",
        "",
        f"- Project root: `{report.get('project_root')}`",
        f"- Social rows: `{report.get('social_rows')}`",
        f"- Coverage pct: `{report.get('socials_coverage_pct')}`",
        f"- Closed trades joined: `{report.get('closed_trades_joined')}`",
        "",
        "## Status Counts",
        "",
    ]
    counts = report.get("status_counts") or {}
    if counts:
        for key, value in sorted(counts.items()):
            lines.append(f"- `{key}`: `{value}`")
    else:
        lines.append("- Sin datos")
    lines.extend(["", "## Performance By Status", ""])
    rows = report.get("performance_by_status") or []
    if rows:
        for row in rows:
            lines.append(
                "- `{social_status}`: trades=`{trades}`, win_rate=`{win_rate_pct}`, avg_pnl=`{avg_pnl_pct}`, median_pnl=`{median_pnl_pct}`, severe_losses=`{severe_losses}`".format(
                    **row
                )
            )
    else:
        lines.append("- Sin datos")
    lines.append("")
    return "\n".join(lines)


def write_socials_report(report: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = report or build_socials_report()
    SOCIALS_REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    SOCIALS_REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    SOCIALS_REPORT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    SOCIALS_REPORT_MD.write_text(render_socials_markdown(payload), encoding="utf-8")
    return payload


__all__ = [
    "SOCIALS_REPORT_JSON",
    "SOCIALS_REPORT_MD",
    "build_socials_report",
    "render_socials_markdown",
    "write_socials_report",
]
