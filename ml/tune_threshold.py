# ml/tune_threshold.py
"""
Sintoniza el umbral del modelo (AI_THRESHOLD) a partir de val_preds.csv.

Entrada
-------
data/metrics/val_preds.csv con columnas:
  - y_true (0/1), y_prob (probabilidad modelo)
  - (opcionales) mint, timestamp, hour, liquidity_usd, volume_24h_usd, ...

Salida
------
data/metrics/recommended_threshold.json con:
  {
    "picked": 0.37,
    "objective": "f1",
    "f1_at_picked": 0.62,
    "precision_at_picked": 0.68,
    "recall_at_picked": 0.57,
    "auc_pr": 0.73,
    "roc_auc": 0.81,
    "samples": 1234,
    "positives": 321,
    "source_csv": ".../data/metrics/val_preds.csv",
    "generated_at_utc": "2025-08-23T21:34:00Z",
    "alternatives": {
        "youden_j": {"threshold": 0.41, "j": 0.43},
        "max_f05":  {"threshold": 0.49, "f05": 0.59},
        "max_f2":   {"threshold": 0.28, "f2":  0.66}
    }
  }

Uso
---
python -m ml.tune_threshold --objective f1
python -m ml.tune_threshold --precision-floor 0.65  # elige t con recall máximo s.t. precision>=0.65
python -m ml.tune_threshold --recall-floor 0.30     # elige t con precision máximo s.t. recall>=0.30
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    precision_recall_curve,
    roc_auc_score,
    roc_curve,
)

from config.config import CFG

# Paths
METRICS_DIR = CFG.FEATURES_DIR.parent / "metrics"
VAL_PREDS_CSV = METRICS_DIR / "val_preds.csv"
OUT_JSON = METRICS_DIR / "recommended_threshold.json"


@dataclass
class PRPoint:
    threshold: float
    precision: float
    recall: float
    f1: float
    f05: float
    f2: float
    tp: int
    fp: int
    tn: int
    fn: int


def _safe_f1(prec: float, rec: float, beta: float = 1.0) -> float:
    if prec <= 0 and rec <= 0:
        return 0.0
    if beta == 1.0:
        denom = prec + rec
        return 0.0 if denom == 0 else 2 * prec * rec / denom
    # F-beta general
    b2 = beta * beta
    denom = (b2 * prec) + rec
    return 0.0 if denom == 0 else (1 + b2) * (prec * rec) / denom


def _confusion(y_true: np.ndarray, y_prob: np.ndarray, thr: float) -> Tuple[int, int, int, int]:
    yhat = (y_prob >= thr).astype(np.uint8)
    tp = int(((yhat == 1) & (y_true == 1)).sum())
    fp = int(((yhat == 1) & (y_true == 0)).sum())
    tn = int(((yhat == 0) & (y_true == 0)).sum())
    fn = int(((yhat == 0) & (y_true == 1)).sum())
    return tp, fp, tn, fn


def _precision(tp: int, fp: int) -> float:
    denom = tp + fp
    return float(tp) / denom if denom > 0 else 0.0


def _recall(tp: int, fn: int) -> float:
    denom = tp + fn
    return float(tp) / denom if denom > 0 else 0.0


def _grid_from_probs(probs: np.ndarray, max_points: int = 400) -> np.ndarray:
    """Crea un grid de umbrales basado en cuantiles de probs (estable y denso en los bordes)."""
    probs = probs[np.isfinite(probs)]
    probs = probs[(probs >= 0) & (probs <= 1)]
    if probs.size == 0:
        return np.linspace(0.01, 0.99, 99)
    qs = np.linspace(0.01, 0.99, min(max_points, max(20, probs.size)))
    thr = np.unique(np.quantile(probs, qs))
    # incluye extremos razonables
    thr = np.unique(np.clip(np.concatenate([[0.01], thr, [0.99]]), 0.0, 1.0))
    return thr


def _compute_curve(y: np.ndarray, p: np.ndarray, thresholds: Iterable[float]) -> Tuple[Dict[float, PRPoint], float, float]:
    points: Dict[float, PRPoint] = {}
    # Métricas globales independientes del umbral
    try:
        auc_pr = float(average_precision_score(y, p))
    except Exception:
        auc_pr = float("nan")
    try:
        roc_auc = float(roc_auc_score(y, p))
    except Exception:
        roc_auc = float("nan")

    for t in thresholds:
        tp, fp, tn, fn = _confusion(y, p, t)
        prec = _precision(tp, fp)
        rec = _recall(tp, fn)
        points[float(t)] = PRPoint(
            threshold=float(t),
            precision=prec,
            recall=rec,
            f1=_safe_f1(prec, rec, beta=1.0),
            f05=_safe_f1(prec, rec, beta=0.5),
            f2=_safe_f1(prec, rec, beta=2.0),
            tp=tp, fp=fp, tn=tn, fn=fn,
        )
    return points, auc_pr, roc_auc


def _pick_by_f1(points: Dict[float, PRPoint]) -> Tuple[float, PRPoint]:
    best_t = max(points, key=lambda t: (points[t].f1, points[t].precision))
    return best_t, points[best_t]


def _pick_by_youden(y: np.ndarray, p: np.ndarray, thresholds: Iterable[float]) -> Tuple[float, float]:
    try:
        fpr, tpr, thr = roc_curve(y, p)
        j = tpr - fpr
        idx = int(np.argmax(j))
        return float(thr[idx]), float(j[idx])
    except Exception:
        # fallback simple: J sobre grid manual
        best_t, best_j = 0.5, -1.0
        for t in thresholds:
            tp, fp, tn, fn = _confusion(y, p, float(t))
            rec = _recall(tp, fn)               # TPR
            fpr = float(fp) / (fp + tn + 1e-9)  # FPR
            j = rec - fpr
            if j > best_j:
                best_t, best_j = float(t), float(j)
        return best_t, best_j


def _pick_with_constraints(points: Dict[float, PRPoint], precision_floor: float | None, recall_floor: float | None) -> Tuple[float, PRPoint] | None:
    candidates = list(points.items())
    if precision_floor is not None:
        candidates = [(t, pt) for t, pt in candidates if pt.precision >= precision_floor]
        if not candidates:
            return None
        # maximiza recall bajo la restricción
        t, pt = max(candidates, key=lambda kv: (kv[1].recall, kv[1].f1))
        return t, pt
    if recall_floor is not None:
        candidates = [(t, pt) for t, pt in candidates if pt.recall >= recall_floor]
        if not candidates:
            return None
        # maximiza precisión bajo la restricción
        t, pt = max(candidates, key=lambda kv: (kv[1].precision, kv[1].f1))
        return t, pt
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Tune AI threshold from val preds.")
    parser.add_argument("--csv", type=str, default=str(VAL_PREDS_CSV), help="Ruta a val_preds.csv")
    parser.add_argument("--objective", type=str, default="f1", choices=["f1", "youden"], help="Criterio principal")
    parser.add_argument("--precision-floor", type=float, default=None, help="Elige t con RECALL máximo s.t. precision>=X")
    parser.add_argument("--recall-floor", type=float, default=None, help="Elige t con PRECISIÓN máxima s.t. recall>=X")
    parser.add_argument("--out", type=str, default=str(OUT_JSON), help="Ruta de salida JSON")
    parser.add_argument("--max-grid", type=int, default=400, help="Puntos máximos en el grid de thresholds")
    args = parser.parse_args()

    csv_path = pd.Path(args.csv) if hasattr(pd, "Path") else args.csv  # compat pandas <2.2
    try:
        df = pd.read_csv(args.csv)
    except FileNotFoundError as e:
        raise SystemExit(f"[tune_threshold] No existe {args.csv}. Ejecuta ml/train.py primero.") from e

    # sanitiza
    if "y_true" not in df.columns or "y_prob" not in df.columns:
        raise SystemExit("[tune_threshold] val_preds.csv debe contener columnas 'y_true' y 'y_prob'.")

    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=["y_true", "y_prob"]).copy()
    df["y_true"] = df["y_true"].astype(int)
    df["y_prob"] = df["y_prob"].astype(float).clip(0, 1)

    y = df["y_true"].values
    p = df["y_prob"].values

    n = int(len(df))
    pos = int(df["y_true"].sum())
    if pos == 0 or pos == n:
        # dataset degenerado — fija 0.5 por defecto
        out = {
            "picked": 0.5,
            "objective": "degenerate",
            "auc_pr": float("nan"),
            "roc_auc": float("nan"),
            "samples": n,
            "positives": pos,
            "source_csv": str(VAL_PREDS_CSV),
            "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "alternatives": {},
            "note": "Clase única en validación; usa 0.5 como placeholder.",
        }
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        print("[tune_threshold] Dataset sin variedad de clases; escrito placeholder 0.5")
        return 0

    thr_grid = _grid_from_probs(p, max_points=args.max_grid)
    points, auc_pr, roc_auc = _compute_curve(y, p, thr_grid)

    # candidatos
    picked_obj = None
    picked_t = None
    picked_pt = None

    # restricciones (si las hay) tienen prioridad
    constrained = _pick_with_constraints(points, args.precision_floor, args.recall_floor)
    if constrained is not None:
        picked_t, picked_pt = constrained
        picked_obj = f"constraint({'precision>=' + str(args.precision_floor) if args.precision_floor is not None else 'recall>=' + str(args.recall_floor)})"
    else:
        if args.objective == "f1":
            picked_t, picked_pt = _pick_by_f1(points)
            picked_obj = "f1"
        else:
            # youden J
            picked_t, best_j = _pick_by_youden(y, p, thr_grid)
            tp, fp, tn, fn = _confusion(y, p, picked_t)
            pr = _precision(tp, fp)
            rc = _recall(tp, fn)
            picked_pt = PRPoint(
                threshold=picked_t,
                precision=pr,
                recall=rc,
                f1=_safe_f1(pr, rc, 1.0),
                f05=_safe_f1(pr, rc, 0.5),
                f2=_safe_f1(pr, rc, 2.0),
                tp=tp, fp=fp, tn=tn, fn=fn,
            )
            picked_obj = "youden"

    # alternativas útiles para el JSON
    # - Youden J
    alt_youden_t, alt_youden_j = _pick_by_youden(y, p, thr_grid)
    # - F0.5 y F2
    t_f05 = max(points, key=lambda t: points[t].f05)
    t_f2  = max(points, key=lambda t: points[t].f2)

    result = {
        "picked": float(picked_t),
        "objective": picked_obj,
        "f1_at_picked": float(picked_pt.f1),
        "precision_at_picked": float(picked_pt.precision),
        "recall_at_picked": float(picked_pt.recall),
        "auc_pr": float(auc_pr),
        "roc_auc": float(roc_auc),
        "samples": n,
        "positives": pos,
        "source_csv": str(VAL_PREDS_CSV),
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alternatives": {
            "youden_j": {"threshold": float(alt_youden_t), "j": float(alt_youden_j)},
            "max_f05":  {"threshold": float(t_f05), "f05": float(points[t_f05].f05)},
            "max_f2":   {"threshold": float(t_f2),  "f2":  float(points[t_f2].f2)},
        },
        "note": "Copia 'picked' a AI_THRESHOLD en tu .env (o léelo dinámicamente al arrancar).",
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(
        f"[tune_threshold] picked={result['picked']:.3f} "
        f"({picked_obj}) · F1={result['f1_at_picked']:.3f} "
        f"P={picked_pt.precision:.3f} R={picked_pt.recall:.3f} "
        f"AP={auc_pr:.3f} AUC={roc_auc:.3f}"
    )
    print(f"[tune_threshold] JSON → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
