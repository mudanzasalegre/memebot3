"""
Persiste cada vector de features en un Parquet mensual
(features_YYYYMM.parquet) con esquema **fijo**.

🆕 2025-07-21
─────────────
• Se mantiene un contador in-memory (`_ROW_COUNT`) que se incrementa
  en cada `append()`.  
• Cada 100 filas escritas se imprime en el log:

        [features] Features acumuladas: <TOTAL>

  Así puedes seguir el tamaño del dataset sin abrir el Parquet.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Mapping

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from config.config import CFG
from features.builder import COLUMNS as _FEAT_COLS

log = logging.getLogger("features")

# ───────────────────────── paths ───────────────────────────────
DATA_DIR: Path = CFG.FEATURES_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

_PARQUET_COLS = _FEAT_COLS + ["label", "ts"]

# —— esquema fijo ——————————————————————————————
_COL_TYPES = OrderedDict([
    # meta
    ("address",             pa.string()),
    ("timestamp",           pa.timestamp("us")),
    ("discovered_via",      pa.string()),
    # liquidez / actividad
    ("age_minutes",         pa.float32()),
    ("liquidity_usd",       pa.float32()),
    ("volume_24h_usd",      pa.float32()),
    ("txns_last_5m",        pa.int32()),
    ("holders",             pa.int32()),
    # riesgo
    ("rug_score",           pa.int32()),
    ("cluster_bad",         pa.int8()),
    ("mint_auth_renounced", pa.int8()),
    # momentum
    ("price_pct_1m",        pa.float32()),
    ("price_pct_5m",        pa.float32()),
    ("volume_pct_5m",       pa.float32()),
    # social
    ("social_ok",           pa.int8()),
    ("twitter_followers",   pa.int32()),
    ("discord_members",     pa.int32()),
    # señales internas
    ("score_total",         pa.int32()),
    ("trend",               pa.int8()),
    # flag
    ("is_incomplete",       pa.int8()),
    # label + ts
    ("label",               pa.int8()),
    ("ts",                  pa.timestamp("us")),
])

_SCHEMA = pa.schema([(c, _COL_TYPES[c]) for c in _PARQUET_COLS])

# ───────────────────────── helpers ─────────────────────────────
def _file_for_now(clock: dt.datetime | None = None) -> Path:
    ts = clock or dt.datetime.now(dt.timezone.utc)
    return DATA_DIR / f"features_{ts:%Y%m}.parquet"


def _enforce_schema(table: pa.Table) -> pa.Table:
    """Asegura que la tabla cumpla exactamente el esquema fijo."""
    for col in _PARQUET_COLS:
        if col not in table.schema.names:
            table = table.append_column(
                col,
                pa.array([None] * table.num_rows, type=_COL_TYPES[col]),
            )
    table = table.select(_PARQUET_COLS)
    return table.cast(_SCHEMA, safe=False)

# ─────────── contador in-memory ───────────────────────────────
_ROW_COUNT = 0          # se incrementa en cada append()

# ───────────────────── low-level IO ────────────────────────────
def _write(table: pa.Table, path: Path) -> None:
    table = _enforce_schema(table)

    if path.exists():
        existing = _enforce_schema(pq.read_table(path))
        table = pa.concat_tables(
            [existing, table],
            promote_options="default",   # sin FutureWarning desde pyarrow 20
        )

    pq.write_table(
        table,
        path,
        compression="snappy",
        use_deprecated_int96_timestamps=False,
    )

# ───────────────────── API pública ─────────────────────────────
def append(vec: Mapping[str, float | int], label: int) -> None:
    """
    Añade una fila al Parquet mensual y muestra el total cada 100 filas.
    """
    global _ROW_COUNT

    if isinstance(vec, pd.Series):
        vec = vec.to_dict()

    row = {c: vec.get(c, 0) for c in _FEAT_COLS}
    row["label"] = int(label)
    row["ts"] = dt.datetime.now(dt.timezone.utc)

    pa_table = pa.Table.from_pydict({k: [v] for k, v in row.items()})

    try:
        _write(pa_table, _file_for_now())
        _ROW_COUNT += 1
        if _ROW_COUNT % 100 == 0:
            log.info("Features acumuladas: %s", _ROW_COUNT)
    except Exception as exc:           # noqa: BLE001
        log.error("Parquet append error → %s", exc)


def update_pnl(address: str, pnl_pct: float) -> None:
    """Actualiza la columna pnl_pct en la última fila del token."""
    path = _file_for_now()
    if not path.exists():
        return

    try:
        table = pq.read_table(path)
        idxs = [i for i, v in enumerate(table.column("address"))
                if v.as_py() == address]
        if not idxs:
            return
        last = idxs[-1]

        if "pnl_pct" not in table.schema.names:
            table = table.append_column(
                "pnl_pct", pa.array([None] * table.num_rows)
            )

        pnl_vals = [table.column("pnl_pct")[i].as_py()
                    for i in range(table.num_rows)]
        pnl_vals[last] = float(pnl_pct)

        new_table = table.set_column(
            table.schema.names.index("pnl_pct"),
            "pnl_pct",
            pa.array(pnl_vals),
        )
        pq.write_table(new_table, path, compression="snappy")
    except Exception as exc:           # noqa: BLE001
        log.error("update_pnl error → %s", exc)
