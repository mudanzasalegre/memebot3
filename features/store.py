# memebot3/features/store.py
"""
Persiste cada vector de features en un Parquet mensual
(features_YYYYMM.parquet) con esquema **fijo**.

🆕 2025-07-21
─────────────
• Se mantiene un contador in-memory (`_ROW_COUNT`) que se incrementa
  en cada `append()`.
• Cada 100 filas escritas se imprime en el log:
        [features] Features acumuladas: <TOTAL>

🆕 2025-07-26
─────────────
• Añadida la columna **market_cap_usd** al esquema fijo para reflejar
  la estrategia de micro-caps (5 k – 20 k USD).

🆕 2025-09-13
─────────────
• Verificación explícita de compatibilidad entre el esquema fijo y
  las columnas actuales de `features.builder.COLUMNS`.
• `append()` deja de rellenar vacíos con 0: usa `None` (→ null en Parquet)
  para mantener la semántica de *dato ausente* (coherente con NaN en pandas).
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
from ml.data_contract import (
    normalize_dex_id,
    normalize_entry_lane,
    normalize_entry_regime,
    normalize_price_source,
    normalize_sample_type,
)

log = logging.getLogger("features")

# ───────────────────────── paths ───────────────────────────────
DATA_DIR: Path = CFG.FEATURES_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

_PARQUET_COLS = _FEAT_COLS + ["label", "target_total_pnl_pct", "sample_type", "ts"]

# —— esquema fijo ——————————————————————————————
# Nota: Si añades nuevas columnas en builder.COLUMNS, debes reflejarlas aquí
# con un tipo apropiado. Esta verificación se hace al cargar el módulo.
_COL_TYPES = OrderedDict(
    [
        # meta
        ("address", pa.string()),
        ("timestamp", pa.timestamp("us")),
        ("discovered_via", pa.string()),
        ("discovered_via_code", pa.int8()),
        ("entry_regime", pa.string()),
        ("entry_regime_code", pa.int8()),
        ("entry_lane", pa.string()),
        ("gate_profile", pa.string()),
        ("profit_lane_tier", pa.string()),
        ("dex_id", pa.string()),
        ("dex_id_code", pa.int8()),
        ("price_source", pa.string()),
        ("price_source_quality", pa.int8()),
        # liquidez / actividad
        ("age_minutes", pa.float32()),
        ("queue_attempts", pa.int32()),
        ("queue_age_minutes", pa.float32()),
        ("snapshot_missing_fields", pa.int8()),
        ("coverage_core_fields", pa.int8()),
        ("liquidity_usd", pa.float32()),
        ("volume_24h_usd", pa.float32()),
        ("market_cap_usd", pa.float32()),
        ("txns_last_5m", pa.int32()),
        ("txns_last_5m_buys", pa.int32()),
        ("txns_last_5m_sells", pa.int32()),
        ("holders", pa.int32()),
        # riesgo
        ("rug_score", pa.int32()),
        ("cluster_bad", pa.int8()),
        ("mint_auth_renounced", pa.int8()),
        # momentum
        ("price_pct_1m", pa.float32()),
        ("price_pct_5m", pa.float32()),
        ("price5m_bucket", pa.string()),
        ("price5m_bucket_code", pa.int8()),
        ("volume_pct_5m", pa.float32()),
        ("price_impact_pct", pa.float32()),
        ("impact_zero_flag", pa.int8()),
        # social
        ("social_ok", pa.int8()),
        ("twitter_followers", pa.int32()),
        ("discord_members", pa.int32()),
        # señales internas
        ("score_total", pa.int32()),
        ("trend", pa.int8()),
        ("has_jupiter_route", pa.int8()),
        ("require_jupiter_for_buy", pa.int8()),
        ("liquidity_is_proxy", pa.int8()),
        ("venue_is_pumpswap", pa.int8()),
        ("mcap_bucket", pa.string()),
        ("mcap_bucket_code", pa.int8()),
        ("missing_liquidity", pa.int8()),
        ("missing_volume", pa.int8()),
        ("missing_holders", pa.int8()),
        ("missing_rug_score", pa.int8()),
        ("missing_socials", pa.int8()),
        ("missing_trend", pa.int8()),
        # flag
        ("is_incomplete", pa.int8()),
        # label + ts
        ("label", pa.int8()),
        ("target_total_pnl_pct", pa.float32()),
        ("sample_type", pa.string()),
        ("ts", pa.timestamp("us")),
    ]
)

# Construcción del schema (se valida abajo contra _FEAT_COLS)
def _build_schema() -> pa.Schema:
    return pa.schema([(c, _COL_TYPES[c]) for c in _PARQUET_COLS])

_SCHEMA = _build_schema()

# ───────────── verificación de compatibilidad de esquema ─────────────
def _verify_schema_matches_builder() -> None:
    """Comprueba que todas las columnas de builder.COLUMNS tienen tipo en _COL_TYPES."""
    missing = [c for c in _FEAT_COLS if c not in _COL_TYPES]
    extra = [c for c in _COL_TYPES.keys() if c not in _PARQUET_COLS]
    if missing:
        log.error(
            "Esquema Parquet INCOMPLETO: faltan tipos para columnas de builder: %s",
            missing,
        )
        # No lanzamos excepción para no romper en producción, pero es crítico arreglarlo.
    if extra:
        log.warning(
            "Esquema Parquet tiene tipos definidos que no están en builder: %s",
            extra,
        )

_verify_schema_matches_builder()

# ───────────────────────── helpers ─────────────────────────────
def _file_for_now(clock: dt.datetime | None = None) -> Path:
    ts = clock or dt.datetime.now(dt.timezone.utc)
    return DATA_DIR / f"features_{ts:%Y%m}.parquet"


def _enforce_schema(table: pa.Table) -> pa.Table:
    """Asegura que la tabla cumpla exactamente el esquema fijo (orden y tipos)."""
    # Añade columnas ausentes como nulas
    for col in _PARQUET_COLS:
        if col not in table.schema.names:
            table = table.append_column(
                col,
                pa.array([None] * table.num_rows, type=_COL_TYPES[col]),
            )
    # Selecciona y castea al schema fijo
    table = table.select(_PARQUET_COLS)
    return table.cast(_SCHEMA, safe=False)


def _normalize_scalar(val: object) -> object:
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except Exception:
        pass
    return val


# ─────────── contador in-memory ───────────────────────────────
_ROW_COUNT = 0  # se incrementa en cada append()

# ───────────────────── low-level IO ───────────────────────────
def _write(table: pa.Table, path: Path) -> None:
    table = _enforce_schema(table)

    if path.exists():
        existing = _enforce_schema(pq.read_table(path))
        table = pa.concat_tables(
            [existing, table],
            promote_options="default",  # sin FutureWarning desde pyarrow 20
        )

    pq.write_table(
        table,
        path,
        compression="snappy",
        use_deprecated_int96_timestamps=False,
    )


# ───────────────────── API pública ─────────────────────────────
def append(
    vec: Mapping[str, object] | pd.Series,
    label: int,
    *,
    target_total_pnl_pct: float | None = None,
    sample_type: str | None = None,
) -> None:
    """
    Añade una fila al Parquet mensual y muestra el total cada 100 filas.
    - No rellena con 0: usa None para preservar la semántica de 'dato ausente'.
    """
    global _ROW_COUNT

    if isinstance(vec, pd.Series):
        vec = vec.to_dict()

    # Construye la fila respetando el set de columnas actual y la semántica de NaN/None
    row: dict[str, object] = {}
    for c in _FEAT_COLS:
        row[c] = _normalize_scalar(vec.get(c, None))

    row["entry_regime"] = normalize_entry_regime(row.get("entry_regime"))
    row["entry_lane"] = normalize_entry_lane(row.get("entry_lane"))
    row["dex_id"] = normalize_dex_id(row.get("dex_id"))
    row["price_source"] = normalize_price_source(row.get("price_source"))

    row["label"] = int(label)
    row["target_total_pnl_pct"] = _normalize_scalar(target_total_pnl_pct)
    row["sample_type"] = normalize_sample_type(sample_type)
    row["ts"] = dt.datetime.now(dt.timezone.utc)

    pa_table = pa.Table.from_pydict({k: [v] for k, v in row.items()})

    try:
        _write(pa_table, _file_for_now())
        _ROW_COUNT += 1
        if _ROW_COUNT % 100 == 0:
            log.info("Features acumuladas: %s", _ROW_COUNT)
    except Exception as exc:  # noqa: BLE001
        log.error("Parquet append error → %s", exc)


def update_pnl(address: str, pnl_pct: float) -> None:
    """Legacy helper: actualiza pnl_pct y target_total_pnl_pct en la última fila del token."""
    path = _file_for_now()
    if not path.exists():
        return

    try:
        table = pq.read_table(path)
        # Nota: 'address' es string(); .to_pylist sería costoso; iteramos columna
        addrs_col = table.column("address")
        idxs = [i for i in range(table.num_rows) if addrs_col[i].as_py() == address]
        if not idxs:
            return
        last = idxs[-1]

        for col in ("pnl_pct", "target_total_pnl_pct"):
            if col not in table.schema.names:
                table = table.append_column(col, pa.array([None] * table.num_rows))

        legacy_vals = [table.column("pnl_pct")[i].as_py() for i in range(table.num_rows)]
        legacy_vals[last] = float(pnl_pct)
        new_table = table.set_column(
            table.schema.names.index("pnl_pct"),
            "pnl_pct",
            pa.array(legacy_vals),
        )

        target_vals = [new_table.column("target_total_pnl_pct")[i].as_py() for i in range(new_table.num_rows)]
        target_vals[last] = float(pnl_pct)
        new_table = new_table.set_column(
            new_table.schema.names.index("target_total_pnl_pct"),
            "target_total_pnl_pct",
            pa.array(target_vals),
        )
        pq.write_table(new_table, path, compression="snappy")
    except Exception as exc:  # noqa: BLE001
        log.error("update_pnl error → %s", exc)


def export_csv() -> None:
    """Vuelca el Parquet actual a CSV para inspección offline."""
    path = _file_for_now()
    if not path.exists():
        return
    csv_path = path.with_suffix(".csv")
    try:
        table = pq.read_table(path)
        df = table.to_pandas()
        df.to_csv(csv_path, index=False)
    except Exception as exc:  # noqa: BLE001
        log.error("export_csv error → %s", exc)
