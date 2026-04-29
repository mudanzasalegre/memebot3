from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.config import CFG  # noqa: E402


METRICS_DIR = CFG.FEATURES_DIR.parent / "metrics"
DATASET_QUALITY_JSON = METRICS_DIR / "dataset_quality.json"
TRAIN_STATUS_JSON = METRICS_DIR / "train_status.json"
RECOMMENDED_JSON = METRICS_DIR / "recommended_threshold.json"
VAL_PREDS_CSV = METRICS_DIR / "val_preds.csv"
MODEL_META_JSON = CFG.MODEL_PATH.with_suffix(".meta.json")


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _val_summary() -> dict:
    if not VAL_PREDS_CSV.exists():
        return {}
    try:
        df = pd.read_csv(VAL_PREDS_CSV)
    except Exception:
        return {}
    if df.empty:
        return {"rows": 0}

    summary = {"rows": int(len(df))}
    if "target_total_pnl_pct" in df.columns:
        realized = pd.to_numeric(df["target_total_pnl_pct"], errors="coerce").dropna()
        summary["realized_rows"] = int(len(realized))
        summary["avg_realized_pnl_pct"] = float(realized.mean()) if not realized.empty else None
        summary["median_realized_pnl_pct"] = float(realized.median()) if not realized.empty else None
    return summary


def render_markdown() -> str:
    dataset_quality = _read_json(DATASET_QUALITY_JSON)
    train_status = _read_json(TRAIN_STATUS_JSON)
    threshold = _read_json(RECOMMENDED_JSON)
    meta = _read_json(MODEL_META_JSON)
    val = _val_summary()

    dq = dataset_quality or {}
    ts = train_status or {}

    lines = [
        "# ML Report",
        "",
        f"- Metrics dir: `{METRICS_DIR}`",
        f"- Model meta: `{MODEL_META_JSON}`",
        "",
        "## Dataset Quality",
        "",
    ]

    if dq:
        for key in (
            "passed",
            "reasons",
            "rows",
            "positives",
            "unique_tokens",
            "realized_return_rows",
            "non_constant_numeric_features",
            "holdout_rows",
            "holdout_positives",
            "holdout_unique_tokens",
        ):
            lines.append(f"- `{key}`: `{dq.get(key)}`")
    else:
        lines.append("- Sin datos")

    lines.extend(["", "## Training Status", ""])
    if ts:
        lines.append(f"- `status`: `{ts.get('status')}`")
        lines.append(f"- `feature_set_hash`: `{ts.get('feature_set_hash')}`")
        lines.append(f"- `split_meta`: `{ts.get('split_meta')}`")
        lines.append(f"- `auc_pr_forward_or_cv_mean`: `{ts.get('auc_pr_forward_or_cv_mean')}`")
        lines.append(f"- `precision_at_k_val`: `{ts.get('precision_at_k_val')}`")
    else:
        lines.append("- Sin datos")

    lines.extend(["", "## Threshold", ""])
    if threshold:
        for key in (
            "picked",
            "objective_requested",
            "objective_applied",
            "activation_ready",
            "activation_reason",
            "precision_at_picked",
            "recall_at_picked",
            "f1_at_picked",
            "avg_realized_pnl_pct_at_picked",
            "total_realized_pnl_pct_points_at_picked",
            "selected_rows_at_picked",
            "realized_selected_rows_at_picked",
        ):
            lines.append(f"- `{key}`: `{threshold.get(key)}`")
    else:
        lines.append("- Sin datos")

    lines.extend(["", "## Model Meta", ""])
    if meta:
        for key in (
            "activation_ready",
            "dataset_quality_passed",
            "model_selection_metric",
            "model_selection_score",
            "rows",
            "feature_set_hash",
        ):
            lines.append(f"- `{key}`: `{meta.get(key)}`")
    else:
        lines.append("- Sin datos")

    lines.extend(["", "## Validation Snapshot", ""])
    if val:
        for key, value in val.items():
            lines.append(f"- `{key}`: `{value}`")
    else:
        lines.append("- Sin datos")

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera reporte ML a partir de metrics/*.json y val_preds.csv.")
    parser.add_argument(
        "--write-docs",
        default="docs/ML_REPORT.md",
        help="Ruta del markdown a escribir. Usa cadena vacia para no escribir.",
    )
    args = parser.parse_args()

    markdown = render_markdown()
    print(markdown)

    target = str(args.write_docs or "").strip()
    if target:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
