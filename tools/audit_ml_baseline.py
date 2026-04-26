from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.config import CFG
from ml.segment_report import build_segment_report, load_feature_history, render_segment_report_md

METRICS_DIR = CFG.FEATURES_DIR.parent / "metrics"
VAL_PREDS_CSV = METRICS_DIR / "val_preds.csv"
MODEL_META_JSON = CFG.MODEL_PATH.with_suffix(".meta.json")
OUT_JSON = METRICS_DIR / "baseline_report.json"
OUT_MD = METRICS_DIR / "baseline_report.md"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _empty_report(features: pd.DataFrame, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "features_only",
        "model_meta": {
            "path": str(MODEL_META_JSON),
            "feature_set_hash": meta.get("feature_set_hash"),
            "activation_ready": meta.get("activation_ready"),
            "ai_threshold_recommended": meta.get("ai_threshold_recommended"),
            "training_scope": meta.get("training_scope"),
        },
        "feature_rows": int(len(features)),
        "sample_type_summary": {},
        "lane_summary": {},
        "dex_summary": {},
        "threshold_summary": {},
        "missed_jackpots": [],
        "model_selected_trades": [],
        "model_rejected_real_winners": [],
        "model_accepted_real_losers": [],
    }


def _opportunity_lists(frame: pd.DataFrame, threshold: float) -> dict[str, list[dict[str, Any]]]:
    if frame.empty or "y_prob" not in frame.columns:
        return {
            "missed_jackpots": [],
            "model_selected_trades": [],
            "model_rejected_real_winners": [],
            "model_accepted_real_losers": [],
        }
    df = frame.copy()
    df["y_prob"] = pd.to_numeric(df["y_prob"], errors="coerce")
    df["target_total_pnl_pct"] = pd.to_numeric(df.get("target_total_pnl_pct"), errors="coerce")
    selected = df["y_prob"].ge(float(threshold))
    jackpot = df["target_total_pnl_pct"].ge(100.0)
    winners = df["target_total_pnl_pct"].gt(0.0)
    losers = df["target_total_pnl_pct"].lt(0.0)
    cols = [
        col
        for col in ("mint", "address", "sample_type", "entry_lane", "dex_id", "price_source", "y_prob", "target_total_pnl_pct")
        if col in df.columns
    ]

    def _records(mask: pd.Series, limit: int = 50) -> list[dict[str, Any]]:
        return df.loc[mask, cols].sort_values("target_total_pnl_pct", ascending=False).head(limit).to_dict(orient="records")

    return {
        "missed_jackpots": _records(~selected & jackpot),
        "model_selected_trades": _records(selected),
        "model_rejected_real_winners": _records(~selected & winners),
        "model_accepted_real_losers": _records(selected & losers),
    }


def build_baseline_audit(*, features_dir: Path | None = None, val_preds_path: Path = VAL_PREDS_CSV) -> dict[str, Any]:
    features = load_feature_history(features_dir)
    meta = _read_json(MODEL_META_JSON)
    if not val_preds_path.exists():
        return _empty_report(features, meta)

    preds = pd.read_csv(val_preds_path)
    threshold = float(meta.get("ai_threshold_recommended") or getattr(CFG, "AI_THRESHOLD", 0.5) or 0.5)
    segment_report = build_segment_report(preds, features=features, threshold=threshold)
    enriched = build_segment_report(preds, features=features, threshold=threshold)
    out = {
        "source": "val_preds",
        "model_meta": {
            "path": str(MODEL_META_JSON),
            "feature_set_hash": meta.get("feature_set_hash"),
            "activation_ready": meta.get("activation_ready"),
            "ai_threshold_recommended": meta.get("ai_threshold_recommended"),
            "training_scope": meta.get("training_scope"),
        },
        "threshold": threshold,
        "feature_rows": int(len(features)),
        "val_pred_rows": int(len(preds)),
        "sample_type_summary": (segment_report.get("segments") or {}).get("sample_type", {}),
        "lane_summary": (segment_report.get("segments") or {}).get("entry_lane", {}),
        "dex_summary": (segment_report.get("segments") or {}).get("dex_id", {}),
        "price_source_summary": (segment_report.get("segments") or {}).get("price_source", {}),
        "threshold_summary": segment_report.get("global", {}),
    }
    # Re-enrich once for record-level lists.
    from ml.segment_report import enrich_val_predictions

    enriched_frame = enrich_val_predictions(preds, features)
    out.update(_opportunity_lists(enriched_frame, threshold))
    _ = enriched
    return out


def render_baseline_audit_md(report: dict[str, Any]) -> str:
    lines = [
        "# ML Baseline Audit",
        "",
        f"- Source: `{report.get('source')}`",
        f"- Threshold: `{report.get('threshold')}`",
        f"- Feature rows: `{report.get('feature_rows')}`",
        f"- Validation rows: `{report.get('val_pred_rows')}`",
        "",
        "## Threshold Summary",
        "",
    ]
    for key, value in (report.get("threshold_summary") or {}).items():
        if key in {"threshold_result"}:
            continue
        lines.append(f"- `{key}`: `{value}`")
    for title, key in (("Sample Types", "sample_type_summary"), ("Lanes", "lane_summary"), ("DEX", "dex_summary")):
        lines.extend(["", f"## {title}", ""])
        rows = report.get(key) or {}
        if not rows:
            lines.append("- Sin datos")
            continue
        for name, row in rows.items():
            lines.append(f"- `{name}`: rows=`{row.get('rows')}` total=`{row.get('total_pnl_pct_points')}` selected=`{row.get('selected_total_pnl')}`")
    lines.extend(["", "## Opportunity Lists", ""])
    for key in ("missed_jackpots", "model_rejected_real_winners", "model_accepted_real_losers"):
        lines.append(f"- `{key}`: `{len(report.get(key) or [])}`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit ML baseline without changing runtime behavior.")
    parser.add_argument("--out-json", default=str(OUT_JSON))
    parser.add_argument("--out-md", default=str(OUT_MD))
    args = parser.parse_args()
    report = build_baseline_audit()
    out_json = Path(args.out_json)
    out_md = Path(args.out_md)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    out_md.write_text(render_baseline_audit_md(report), encoding="utf-8")
    print(render_segment_report_md({"generated_at_utc": None, "threshold": report.get("threshold"), "rows": report.get("val_pred_rows"), "global": report.get("threshold_summary"), "segments": {}}))
    print(f"[audit_ml_baseline] wrote {out_json} and {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
