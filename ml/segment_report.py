from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from config.config import CFG
from ml.activation_policy import build_recommended_thresholds_by_lane
from ml.data_contract import apply_data_contract
from ml.tune_threshold import tune_from_frame

METRICS_DIR = CFG.FEATURES_DIR.parent / "metrics"
VAL_PREDS_CSV = METRICS_DIR / "val_preds.csv"
SEGMENT_JSON = METRICS_DIR / "segment_report.json"
SEGMENT_MD = METRICS_DIR / "segment_report.md"
LANE_THRESHOLDS_JSON = METRICS_DIR / "recommended_thresholds.by_lane.json"
RECOMMENDED_JSON = METRICS_DIR / "recommended_threshold.json"


def _json_safe(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value_f = float(value)
        if np.isnan(value_f) or np.isinf(value_f):
            return None
        return value_f
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def load_feature_history(features_dir: Path | None = None) -> pd.DataFrame:
    features_dir = features_dir or CFG.FEATURES_DIR
    frames: list[pd.DataFrame] = []
    parquet_files = sorted(features_dir.glob("features_*.parquet"))
    csv_files = sorted(features_dir.glob("features_*.csv"))
    for path in parquet_files or csv_files:
        try:
            frames.append(pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    return apply_data_contract(df)


def enrich_val_predictions(preds: pd.DataFrame, features: pd.DataFrame | None = None) -> pd.DataFrame:
    if preds.empty:
        return preds.copy()
    out = preds.copy()
    if "mint" not in out.columns and "address" in out.columns:
        out["mint"] = out["address"]
    out = apply_data_contract(out)
    context_cols = ("sample_type", "entry_lane", "entry_regime", "dex_id", "price_source", "mcap_bucket", "price5m_bucket", "market_cap_usd", "price_pct_5m")
    missing = [col for col in context_cols if col not in out.columns or out[col].isna().all() or out[col].astype("string").fillna("").eq("").all()]
    if missing and features is not None and not features.empty:
        feat = features.copy()
        if "address" in feat.columns:
            feat["mint"] = feat.get("mint", feat["address"])
        feat = apply_data_contract(feat)
        keep = ["mint", *[col for col in context_cols if col in feat.columns]]
        if "timestamp" in feat.columns:
            feat["timestamp"] = pd.to_datetime(feat["timestamp"], utc=True, errors="coerce")
            keep.append("timestamp")
            feat = feat.sort_values("timestamp")
        latest = feat.dropna(subset=["mint"]).drop_duplicates("mint", keep="last")[keep]
        out = out.merge(latest, on="mint", how="left", suffixes=("", "_feature"))
        for col in context_cols:
            fcol = f"{col}_feature"
            if fcol in out.columns:
                if col not in out.columns:
                    out[col] = out[fcol]
                else:
                    out[col] = out[col].where(out[col].notna() & ~out[col].astype("string").fillna("").eq(""), out[fcol])
                out = out.drop(columns=[fcol])
    return apply_data_contract(out)


def _current_threshold(threshold: float | None = None) -> float:
    if threshold is not None:
        return float(threshold)
    payload = _read_json(RECOMMENDED_JSON)
    try:
        return float(payload.get("picked"))
    except Exception:
        return float(getattr(CFG, "AI_THRESHOLD", 0.5) or 0.5)


def _segment_metrics(frame: pd.DataFrame, threshold: float) -> dict[str, Any]:
    df = frame.replace([np.inf, -np.inf], np.nan).copy()
    y_true = pd.to_numeric(df.get("y_true", df.get("label")), errors="coerce").fillna(0).astype(int)
    y_prob = pd.to_numeric(df.get("y_prob"), errors="coerce").clip(0.0, 1.0)
    pnl = pd.to_numeric(df.get("target_total_pnl_pct"), errors="coerce")
    selected = y_prob.ge(float(threshold)).fillna(False)
    rejected = ~selected
    realized = pnl.dropna()
    positives = int(y_true.sum())
    jackpots = pnl.ge(100.0).fillna(False)
    losers = pnl.lt(0.0).fillna(False)
    selected_pnl = pnl[selected].dropna()
    rejected_pnl = pnl[rejected].dropna()
    baseline_total = float(realized.sum()) if not realized.empty else 0.0
    selected_total = float(selected_pnl.sum()) if not selected_pnl.empty else 0.0
    total_jackpots = int(jackpots.sum())
    selected_jackpots = int((selected & jackpots).sum())
    unique_tokens = int(df.get("mint", df.get("address", pd.Series(index=df.index))).astype("string").nunique(dropna=True)) if len(df) else 0

    try:
        tune = tune_from_frame(
            pd.DataFrame({"y_true": y_true, "y_prob": y_prob, "target_total_pnl_pct": pnl}).dropna(subset=["y_prob"]),
            min_selected=max(1, min(10, len(df))),
            min_realized_selected=max(1, min(5, int(pnl.notna().sum()))),
            source_csv=str(VAL_PREDS_CSV),
        )
    except Exception:
        tune = {}

    out = {
        "rows": int(len(df)),
        "positives": positives,
        "unique_tokens": unique_tokens,
        "win_rate": float(positives / len(df)) if len(df) else None,
        "avg_pnl_pct": float(realized.mean()) if not realized.empty else None,
        "median_pnl_pct": float(realized.median()) if not realized.empty else None,
        "total_pnl_pct_points": baseline_total,
        "max_pnl_pct": float(realized.max()) if not realized.empty else None,
        "min_pnl_pct": float(realized.min()) if not realized.empty else None,
        "selected_by_current_threshold": int(selected.sum()),
        "selected_avg_pnl": float(selected_pnl.mean()) if not selected_pnl.empty else None,
        "rejected_avg_pnl": float(rejected_pnl.mean()) if not rejected_pnl.empty else None,
        "selected_total_pnl": selected_total,
        "rejected_total_pnl": float(rejected_pnl.sum()) if not rejected_pnl.empty else 0.0,
        "jackpot_count": total_jackpots,
        "jackpot_capture_rate": float(selected_jackpots / total_jackpots) if total_jackpots else None,
        "missed_jackpot_count": int((rejected & jackpots).sum()),
        "missed_jackpot_total_pnl": float(pnl[rejected & jackpots].sum()) if total_jackpots else 0.0,
        "accepted_loser_count": int((selected & losers).sum()),
        "accepted_loser_total_pnl": float(pnl[selected & losers].sum()) if int((selected & losers).sum()) else 0.0,
        "model_vs_baseline_total_pnl_delta": selected_total - baseline_total,
        "model_improves_total_pnl": bool(selected_total >= baseline_total),
        "do_not_enforce": bool(selected_total < baseline_total or (total_jackpots > 0 and selected_jackpots / total_jackpots < float(getattr(CFG, "ML_MIN_JACKPOT_CAPTURE_RATE", 0.80) or 0.80))),
        "picked_threshold": tune.get("picked", threshold),
        "threshold_result": tune,
    }
    return _json_safe(out)


def build_segment_report(preds: pd.DataFrame, *, features: pd.DataFrame | None = None, threshold: float | None = None) -> dict[str, Any]:
    threshold_f = _current_threshold(threshold)
    df = enrich_val_predictions(preds, features)
    groups: dict[str, dict[str, Any]] = {}
    group_cols = ("sample_type", "entry_lane", "dex_id", "price_source", "mcap_bucket", "price5m_bucket")
    for col in group_cols:
        groups[col] = {}
        if col not in df.columns:
            continue
        for value, grp in df.groupby(df[col].fillna("unknown").astype("string"), dropna=False):
            groups[col][str(value)] = _segment_metrics(grp, threshold_f)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "threshold": threshold_f,
        "rows": int(len(df)),
        "global": _segment_metrics(df, threshold_f),
        "segments": groups,
    }
    return _json_safe(report)


def render_segment_report_md(report: dict[str, Any]) -> str:
    lines = [
        "# Segment Report",
        "",
        f"- Generated at UTC: `{report.get('generated_at_utc')}`",
        f"- Threshold: `{report.get('threshold')}`",
        f"- Rows: `{report.get('rows')}`",
        "",
        "## Global",
        "",
    ]
    global_row = report.get("global") or {}
    for key in ("rows", "positives", "win_rate", "avg_pnl_pct", "total_pnl_pct_points", "selected_total_pnl", "model_vs_baseline_total_pnl_delta", "jackpot_capture_rate", "do_not_enforce"):
        lines.append(f"- `{key}`: `{global_row.get(key)}`")
    for section in ("sample_type", "entry_lane", "dex_id", "price_source", "mcap_bucket", "price5m_bucket"):
        lines.extend(["", f"## {section}", ""])
        rows = ((report.get("segments") or {}).get(section) or {})
        if not rows:
            lines.append("- Sin datos")
            continue
        for name, row in rows.items():
            status = "do_not_enforce" if row.get("do_not_enforce") else "candidate"
            lines.append(
                f"- `{name}`: rows=`{row.get('rows')}` total=`{row.get('total_pnl_pct_points')}` "
                f"selected=`{row.get('selected_total_pnl')}` jackpot_capture=`{row.get('jackpot_capture_rate')}` `{status}`"
            )
    lines.append("")
    return "\n".join(lines)


def write_segment_outputs(report: dict[str, Any], *, json_path: Path = SEGMENT_JSON, md_path: Path = SEGMENT_MD, thresholds_path: Path = LANE_THRESHOLDS_JSON) -> dict[str, Any]:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(_json_safe(report), indent=2), encoding="utf-8")
    md_path.write_text(render_segment_report_md(report), encoding="utf-8")
    thresholds = build_recommended_thresholds_by_lane(report, _read_json(RECOMMENDED_JSON))
    thresholds_path.write_text(json.dumps(_json_safe(thresholds), indent=2), encoding="utf-8")
    return thresholds


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate ML segment report from val_preds.csv.")
    parser.add_argument("--csv", default=str(VAL_PREDS_CSV))
    parser.add_argument("--no-fail-if-missing", action="store_true")
    args = parser.parse_args()
    path = Path(args.csv)
    if not path.exists():
        if args.no_fail_if_missing:
            return 0
        raise SystemExit(f"No existe {path}")
    preds = pd.read_csv(path)
    report = build_segment_report(preds, features=load_feature_history())
    write_segment_outputs(report)
    print(f"[segment_report] wrote {SEGMENT_JSON} and {SEGMENT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "load_feature_history",
    "enrich_val_predictions",
    "build_segment_report",
    "render_segment_report_md",
    "write_segment_outputs",
]
