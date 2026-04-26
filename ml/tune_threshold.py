from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from utils.venv_bootstrap import ensure_project_venv

ensure_project_venv(__file__, module_name=__spec__.name if __spec__ else None)

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score, roc_curve

from config.config import CFG

METRICS_DIR = CFG.FEATURES_DIR.parent / "metrics"
VAL_PREDS_CSV = METRICS_DIR / "val_preds.csv"
OUT_JSON = METRICS_DIR / "recommended_threshold.json"
META_PATH = CFG.MODEL_PATH.with_suffix(".meta.json")
RETURN_COL_CANDIDATES = ("target_total_pnl_pct", "total_pnl_pct", "pnl_pct")


@dataclass
class ThresholdPoint:
    threshold: float
    precision: float
    recall: float
    f1: float
    f05: float
    f2: float
    selected_rows: int
    tp: int
    fp: int
    tn: int
    fn: int
    realized_selected_rows: int
    avg_realized_pnl_pct: Optional[float]
    median_realized_pnl_pct: Optional[float]
    total_realized_pnl_pct_points: Optional[float]


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if np.isnan(value) or np.isinf(value):
            return None
        return float(value)
    if isinstance(value, (np.floating,)):
        value_f = float(value)
        if np.isnan(value_f) or np.isinf(value_f):
            return None
        return value_f
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _safe_fscore(precision: float, recall: float, beta: float = 1.0) -> float:
    if precision <= 0.0 and recall <= 0.0:
        return 0.0
    b2 = beta * beta
    denom = (b2 * precision) + recall
    return 0.0 if denom <= 0.0 else (1.0 + b2) * ((precision * recall) / denom)


def _resolve_return_col(df: pd.DataFrame) -> str | None:
    for col in RETURN_COL_CANDIDATES:
        if col in df.columns:
            return col
    return None


def _grid_from_probs(probs: np.ndarray, max_points: int = 400) -> np.ndarray:
    probs = probs[np.isfinite(probs)]
    probs = probs[(probs >= 0.0) & (probs <= 1.0)]
    if probs.size == 0:
        return np.linspace(0.01, 0.99, 99)
    q_count = min(max_points, max(25, probs.size))
    qs = np.linspace(0.01, 0.99, q_count)
    thr = np.unique(np.quantile(probs, qs))
    return np.unique(np.clip(np.concatenate(([0.01], thr, [0.99])), 0.0, 1.0))


def _confusion(y_true: np.ndarray, y_prob: np.ndarray, thr: float) -> tuple[int, int, int, int]:
    yhat = (y_prob >= thr).astype(np.uint8)
    tp = int(((yhat == 1) & (y_true == 1)).sum())
    fp = int(((yhat == 1) & (y_true == 0)).sum())
    tn = int(((yhat == 0) & (y_true == 0)).sum())
    fn = int(((yhat == 0) & (y_true == 1)).sum())
    return tp, fp, tn, fn


def _point_for_threshold(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    realized_returns: pd.Series | None,
    threshold: float,
) -> ThresholdPoint:
    tp, fp, tn, fn = _confusion(y_true, y_prob, threshold)
    selected = y_prob >= threshold
    selected_rows = int(selected.sum())
    precision = (float(tp) / float(tp + fp)) if (tp + fp) > 0 else 0.0
    recall = (float(tp) / float(tp + fn)) if (tp + fn) > 0 else 0.0

    avg_realized = None
    median_realized = None
    total_realized = None
    realized_selected_rows = 0

    if realized_returns is not None and selected_rows > 0:
        selected_returns = pd.to_numeric(realized_returns[selected], errors="coerce").dropna()
        realized_selected_rows = int(len(selected_returns))
        if realized_selected_rows > 0:
            avg_realized = float(selected_returns.mean())
            median_realized = float(selected_returns.median())
            total_realized = float(selected_returns.sum())

    return ThresholdPoint(
        threshold=float(threshold),
        precision=float(precision),
        recall=float(recall),
        f1=float(_safe_fscore(precision, recall, beta=1.0)),
        f05=float(_safe_fscore(precision, recall, beta=0.5)),
        f2=float(_safe_fscore(precision, recall, beta=2.0)),
        selected_rows=selected_rows,
        tp=tp,
        fp=fp,
        tn=tn,
        fn=fn,
        realized_selected_rows=realized_selected_rows,
        avg_realized_pnl_pct=avg_realized,
        median_realized_pnl_pct=median_realized,
        total_realized_pnl_pct_points=total_realized,
    )


def _best_by_f1(points: list[ThresholdPoint]) -> ThresholdPoint:
    return max(points, key=lambda pt: (pt.f1, pt.precision, pt.selected_rows))


def _best_by_youden(y_true: np.ndarray, y_prob: np.ndarray, points: list[ThresholdPoint]) -> ThresholdPoint:
    try:
        fpr, tpr, thr = roc_curve(y_true, y_prob)
        j = tpr - fpr
        picked = float(thr[int(np.argmax(j))])
        return min(points, key=lambda pt: abs(pt.threshold - picked))
    except Exception:
        return max(
            points,
            key=lambda pt: (
                (float(pt.recall) - (float(pt.fp) / max(float(pt.fp + pt.tn), 1.0))),
                pt.precision,
            ),
        )


def tune_from_frame(
    frame: pd.DataFrame,
    *,
    objective: str = "expected_pnl_precision_floor",
    precision_floor: float = 0.60,
    max_grid: int = 400,
    min_selected: int = 10,
    min_realized_selected: int = 5,
    source_csv: str | None = None,
) -> dict[str, Any]:
    if "y_true" not in frame.columns or "y_prob" not in frame.columns:
        raise ValueError("El frame debe contener 'y_true' y 'y_prob'")

    df = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=["y_true", "y_prob"]).copy()
    df["y_true"] = df["y_true"].astype(int)
    df["y_prob"] = pd.to_numeric(df["y_prob"], errors="coerce").clip(0.0, 1.0)
    df = df.dropna(subset=["y_prob"])

    y = df["y_true"].to_numpy(dtype=np.int32)
    p = df["y_prob"].to_numpy(dtype=float)
    n = int(len(df))
    pos = int(df["y_true"].sum())
    return_col = _resolve_return_col(df)
    realized_returns = pd.to_numeric(df[return_col], errors="coerce") if return_col else None
    realized_return_rows = int(realized_returns.notna().sum()) if realized_returns is not None else 0

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if n == 0 or pos == 0 or pos == n:
        return {
            "picked": 0.5,
            "objective_requested": objective,
            "objective_applied": "degenerate",
            "activation_ready": False,
            "activation_reason": "validation_class_degenerate",
            "f1_at_picked": 0.0,
            "precision_at_picked": 0.0,
            "recall_at_picked": 0.0,
            "auc_pr": None,
            "roc_auc": None,
            "samples": n,
            "positives": pos,
            "realized_return_rows": realized_return_rows,
            "realized_return_col": return_col,
            "selected_rows_at_picked": 0,
            "realized_selected_rows_at_picked": 0,
            "avg_realized_pnl_pct_at_picked": None,
            "total_realized_pnl_pct_points_at_picked": None,
            "selection_metric": "none",
            "selection_score": None,
            "source_csv": source_csv,
            "generated_at_utc": generated_at,
            "alternatives": {},
        }

    try:
        auc_pr = float(average_precision_score(y, p))
    except Exception:
        auc_pr = float("nan")
    try:
        roc_auc = float(roc_auc_score(y, p))
    except Exception:
        roc_auc = float("nan")

    thresholds = _grid_from_probs(p, max_points=max_grid)
    points = [_point_for_threshold(y, p, realized_returns, float(thr)) for thr in thresholds]
    best_f1 = _best_by_f1(points)
    best_youden = _best_by_youden(y, p, points)

    pnl_candidates = [
        pt for pt in points
        if pt.selected_rows >= int(min_selected)
        and pt.realized_selected_rows >= int(min_realized_selected)
        and pt.avg_realized_pnl_pct is not None
    ]
    precision_floor_candidates = [
        pt for pt in pnl_candidates if pt.precision >= float(precision_floor)
    ]

    best_expected_pnl = max(
        pnl_candidates,
        key=lambda pt: (
            float(pt.avg_realized_pnl_pct or -1e9),
            float(pt.total_realized_pnl_pct_points or -1e9),
            pt.precision,
            pt.selected_rows,
        ),
    ) if pnl_candidates else None

    best_precision_floor = max(
        precision_floor_candidates,
        key=lambda pt: (
            float(pt.avg_realized_pnl_pct or -1e9),
            float(pt.total_realized_pnl_pct_points or -1e9),
            pt.precision,
            pt.selected_rows,
        ),
    ) if precision_floor_candidates else None

    objective_requested = str(objective or "expected_pnl_precision_floor").strip().lower()
    picked = best_f1
    objective_applied = "f1"
    activation_ready = False
    activation_reason = "f1_fallback"

    if objective_requested in {"expected_pnl_precision_floor", "expected_pnl"}:
        if realized_return_rows < int(min_realized_selected):
            activation_reason = "insufficient_realized_returns"
        elif objective_requested == "expected_pnl_precision_floor" and best_precision_floor is not None:
            picked = best_precision_floor
            objective_applied = "expected_pnl_precision_floor"
            activation_ready = bool((picked.avg_realized_pnl_pct or 0.0) > 0.0)
            activation_reason = "precision_floor_met" if activation_ready else "non_positive_expected_pnl"
        elif best_expected_pnl is not None:
            picked = best_expected_pnl
            objective_applied = "expected_pnl"
            activation_ready = bool((picked.avg_realized_pnl_pct or 0.0) > 0.0)
            activation_reason = "expected_pnl_positive" if activation_ready else "non_positive_expected_pnl"
        else:
            activation_reason = "no_threshold_with_min_sample_support"
    elif objective_requested == "youden":
        picked = best_youden
        objective_applied = "youden"
        activation_reason = "youden_not_pnl_aligned"
    else:
        picked = best_f1
        objective_applied = "f1"
        activation_reason = "f1_not_pnl_aligned"

    alternatives = {
        "max_f1": asdict(best_f1),
        "youden": asdict(best_youden),
    }
    if best_expected_pnl is not None:
        alternatives["max_expected_pnl"] = asdict(best_expected_pnl)
    if best_precision_floor is not None:
        alternatives["precision_floor_best"] = asdict(best_precision_floor)

    selection_metric = "avg_realized_pnl_pct_at_picked" if picked.avg_realized_pnl_pct is not None else "f1_at_picked"
    selection_score = picked.avg_realized_pnl_pct if picked.avg_realized_pnl_pct is not None else picked.f1

    return {
        "picked": float(picked.threshold),
        "objective_requested": objective_requested,
        "objective_applied": objective_applied,
        "activation_ready": bool(activation_ready),
        "activation_reason": activation_reason,
        "f1_at_picked": float(picked.f1),
        "precision_at_picked": float(picked.precision),
        "recall_at_picked": float(picked.recall),
        "auc_pr": _json_safe(auc_pr),
        "roc_auc": _json_safe(roc_auc),
        "samples": n,
        "positives": pos,
        "realized_return_rows": realized_return_rows,
        "realized_return_col": return_col,
        "selected_rows_at_picked": int(picked.selected_rows),
        "realized_selected_rows_at_picked": int(picked.realized_selected_rows),
        "avg_realized_pnl_pct_at_picked": _json_safe(picked.avg_realized_pnl_pct),
        "median_realized_pnl_pct_at_picked": _json_safe(picked.median_realized_pnl_pct),
        "total_realized_pnl_pct_points_at_picked": _json_safe(picked.total_realized_pnl_pct_points),
        "selection_metric": selection_metric,
        "selection_score": _json_safe(selection_score),
        "source_csv": source_csv,
        "generated_at_utc": generated_at,
        "alternatives": _json_safe(alternatives),
    }


def write_threshold_result(result: dict[str, Any], *, out_path: Path = OUT_JSON, meta_path: Path = META_PATH) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(_json_safe(result), indent=2), encoding="utf-8")

    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) or {}
        except Exception:
            meta = {}
    meta["ai_threshold_recommended"] = result.get("picked")
    meta["threshold_metric"] = result.get("objective_applied")
    meta["activation_ready"] = result.get("activation_ready")
    meta["threshold_result"] = {
        "objective_requested": result.get("objective_requested"),
        "objective_applied": result.get("objective_applied"),
        "precision_at_picked": result.get("precision_at_picked"),
        "recall_at_picked": result.get("recall_at_picked"),
        "f1_at_picked": result.get("f1_at_picked"),
        "avg_realized_pnl_pct_at_picked": result.get("avg_realized_pnl_pct_at_picked"),
        "selected_rows_at_picked": result.get("selected_rows_at_picked"),
        "realized_selected_rows_at_picked": result.get("realized_selected_rows_at_picked"),
        "selection_metric": result.get("selection_metric"),
        "selection_score": result.get("selection_score"),
    }
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(_json_safe(meta), indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Tune AI threshold from val_preds.csv.")
    parser.add_argument("--csv", type=str, default=str(VAL_PREDS_CSV), help="Ruta a val_preds.csv")
    parser.add_argument(
        "--objective",
        type=str,
        default=str(getattr(CFG, "ML_TUNE_OBJECTIVE", "expected_pnl_precision_floor")),
        choices=["expected_pnl_precision_floor", "expected_pnl", "f1", "youden"],
        help="Objetivo principal",
    )
    parser.add_argument(
        "--precision-floor",
        type=float,
        default=float(getattr(CFG, "ML_TUNE_PRECISION_FLOOR", 0.60)),
        help="Precision mínima para el objetivo expected_pnl_precision_floor",
    )
    parser.add_argument("--out", type=str, default=str(OUT_JSON), help="Ruta de salida JSON")
    parser.add_argument("--max-grid", type=int, default=400, help="Puntos máximos en el grid de thresholds")
    parser.add_argument(
        "--min-selected",
        type=int,
        default=int(getattr(CFG, "ML_TUNE_MIN_SELECTED", 10)),
        help="Mínimo de filas seleccionadas por threshold",
    )
    parser.add_argument(
        "--min-realized-selected",
        type=int,
        default=int(getattr(CFG, "ML_TUNE_MIN_REALIZED_SELECTED", 5)),
        help="Mínimo de filas con retorno realizado para evaluar EV en un threshold",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"[tune_threshold] No existe {csv_path}. Ejecuta ml.train primero.")

    df = pd.read_csv(csv_path)
    result = tune_from_frame(
        df,
        objective=args.objective,
        precision_floor=float(args.precision_floor),
        max_grid=int(args.max_grid),
        min_selected=int(args.min_selected),
        min_realized_selected=int(args.min_realized_selected),
        source_csv=str(csv_path),
    )
    write_threshold_result(result, out_path=Path(args.out), meta_path=META_PATH)
    try:
        from ml.segment_report import build_segment_report, load_feature_history, write_segment_outputs

        segment_report = build_segment_report(df, features=load_feature_history(), threshold=result.get("picked"))
        write_segment_outputs(segment_report)
    except Exception as exc:
        print(f"[tune_threshold] segment_report omitido: {exc}")
    print(
        "[tune_threshold] picked={picked:.3f} objective={objective_applied} "
        "P={precision_at_picked} R={recall_at_picked} avg_realized_pnl={avg_realized_pnl_pct_at_picked} "
        "activation_ready={activation_ready}".format(**_json_safe(result))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
