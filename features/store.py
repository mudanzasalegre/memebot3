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
import os
import tempfile
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from config.config import CFG
from features.builder import COLUMNS as _FEAT_COLS
from features.auxiliary_semantics import PROOF_COLUMN
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

_OUTCOME_COLS = ["max_pnl_pct_seen", "outcome_closed_at", "outcome_trade_id", "outcome_source_sha256",
                 "outcome_return_basis", "outcome_gross_pnl_pct", "outcome_execution_proof"]
_PARQUET_COLS = _FEAT_COLS + [PROOF_COLUMN, "label", "target_total_pnl_pct", "sample_type", "ts"] + _OUTCOME_COLS

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
    ("green_sniper_score", pa.float32()),
    ("green_sniper_action", pa.string()),
    ("green_sniper_reason", pa.string()),
    ("green_sniper_paper_birth_probe", pa.int8()),
        ("profit_pnl_guard_failures", pa.string()),
        ("volume_pct_5m", pa.float32()),
        ("price_impact_pct", pa.float32()),
        ("impact_zero_flag", pa.int8()),
        # social
        ("social_ok", pa.int8()),
        ("social_status", pa.string()),
        ("twitter_present", pa.int8()),
        ("telegram_present", pa.int8()),
        ("discord_present", pa.int8()),
        ("website_present", pa.int8()),
        ("social_link_count", pa.int32()),
        ("social_confidence_bonus", pa.float32()),
        ("social_risk_flags", pa.string()),
        ("social_latency_ms", pa.int32()),
        ("twitter_followers", pa.int32()),
        ("discord_members", pa.int32()),
        # señales internas
        ("score_total", pa.int32()),
        ("trend", pa.int8()),
        ("has_jupiter_route", pa.int8()),
        ("require_jupiter_for_buy", pa.int8()),
        ("route_proxy", pa.int8()),
        ("liquidity_is_proxy", pa.int8()),
        ("green_sniper_risk_level", pa.string()),
        ("green_sniper_risk_reasons", pa.string()),
        ("green_sniper_size_multiplier", pa.float32()),
        ("liquidity_risk_level", pa.string()),
        ("liquidity_risk_reasons", pa.string()),
        ("venue_is_pumpswap", pa.int8()),
        ("mcap_bucket", pa.string()),
        ("mcap_bucket_code", pa.int8()),
        ("missing_liquidity", pa.int8()),
        ("missing_volume", pa.int8()),
        ("missing_holders", pa.int8()),
        ("missing_rug_score", pa.int8()),
        ("missing_socials", pa.int8()),
        ("missing_trend", pa.int8()),
        ("strategy_version", pa.string()),
        ("experiment_id", pa.string()),
        ("exit_profile", pa.string()),
        ("config_hash", pa.string()),
        # flag
        ("is_incomplete", pa.int8()),
        (PROOF_COLUMN, pa.string()),
        # label + ts
        ("label", pa.int8()),
        ("target_total_pnl_pct", pa.float32()),
        ("sample_type", pa.string()),
        ("ts", pa.timestamp("us")),
        ("max_pnl_pct_seen", pa.float64()),
        ("outcome_closed_at", pa.timestamp("us")),
        ("outcome_trade_id", pa.string()),
        ("outcome_source_sha256", pa.string()),
        ("outcome_return_basis", pa.string()),
        ("outcome_gross_pnl_pct", pa.float64()),
        ("outcome_execution_proof", pa.string()),
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

_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_STALE_SECONDS = 300.0
_LOCK_POLL_SECONDS = 0.05
_REPLACE_TIMEOUT_SECONDS = 10.0


@contextmanager
def _exclusive_parquet_lock(path: Path) -> Iterator[None]:
    """Serializa read-modify-write entre procesos sin dependencias externas."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    fd: int | None = None

    while fd is None:
        try:
            fd = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            )
            os.write(fd, f"pid={os.getpid()} created_at={time.time()}\n".encode("ascii"))
        except FileExistsError:
            try:
                lock_age = max(0.0, time.time() - lock_path.stat().st_mtime)
                if lock_age >= _LOCK_STALE_SECONDS:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for parquet lock: {lock_path}")
            time.sleep(_LOCK_POLL_SECONDS)

    try:
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _replace_with_retry(source: Path, target: Path) -> None:
    """Tolera lectores breves que mantienen abierto el destino en Windows."""
    deadline = time.monotonic() + _REPLACE_TIMEOUT_SECONDS
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_LOCK_POLL_SECONDS)


def _atomic_write_table(table: pa.Table, path: Path) -> None:
    """Escribe en el mismo directorio y publica con un replace atomico."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        pq.write_table(
            table,
            tmp_path,
            compression="snappy",
            use_deprecated_int96_timestamps=False,
        )
        with tmp_path.open("r+b") as durable:
            os.fsync(durable.fileno())
        _replace_with_retry(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass

# ───────────────────── low-level IO ───────────────────────────
def _write(table: pa.Table, path: Path) -> bool:
    table = _enforce_schema(table)

    with _exclusive_parquet_lock(path):
        if path.exists():
            existing = _enforce_schema(pq.read_table(path))
            incoming = table.column("outcome_trade_id").to_pylist()
            if any(incoming):
                if table.num_rows != 1 or not incoming[0]:
                    raise ValueError("Costed close export requires one causal row")
                matching = [index for index, identity in enumerate(existing.column("outcome_trade_id").to_pylist())
                            if identity == incoming[0]]
                if matching:
                    if len(matching) != 1:
                        raise ValueError("Duplicate causal close rows already exist")
                    prior, current = existing.slice(matching[0], 1).to_pylist()[0], table.to_pylist()[0]
                    receipt_upgrade = False
                    if prior.get(PROOF_COLUMN) is None and current.get(PROOF_COLUMN) is not None:
                        from features.auxiliary_semantics import checked_row_receipt
                        old_proof, new_proof = checked_row_receipt(prior), checked_row_receipt(current)
                        receipt_upgrade = (old_proof is not None and new_proof is not None
                                           and old_proof == new_proof)
                    # Availability time differs on a retry; every original feature,
                    # result and proof must remain identical after schema casting.
                    if any(prior[key] != current[key] for key in _PARQUET_COLS
                           if key != "ts" and not (key == PROOF_COLUMN and receipt_upgrade)):
                        raise ValueError("Conflicting causal close export")
                    return False
            table = pa.concat_tables(
                [existing, table],
                promote_options="default",  # sin FutureWarning desde pyarrow 20
            )

        _atomic_write_table(table, path)
        return True


# ───────────────────── API pública ─────────────────────────────
def append(
    vec: Mapping[str, object] | pd.Series,
    label: int | None,
    *,
    target_total_pnl_pct: float | None = None,
    sample_type: str | None = None,
    outcome_targets: Mapping[str, object] | None = None,
    strict: bool = False,
    partition_at: dt.datetime | None = None,
) -> bool:
    """
    Añade una fila al Parquet mensual y muestra el total cada 100 filas.
    - No rellena con 0: usa None para preservar la semántica de 'dato ausente'.
    """
    global _ROW_COUNT

    proof = getattr(vec, "attrs", {}).get(PROOF_COLUMN)
    if isinstance(vec, pd.Series):
        vec = vec.to_dict()

    # Construye la fila respetando el set de columnas actual y la semántica de NaN/None
    row: dict[str, object] = {}
    for c in _FEAT_COLS:
        row[c] = _normalize_scalar(vec.get(c, None))
    row[PROOF_COLUMN] = _normalize_scalar(proof if proof is not None else vec.get(PROOF_COLUMN))

    row["entry_regime"] = normalize_entry_regime(row.get("entry_regime"))
    row["entry_lane"] = normalize_entry_lane(row.get("entry_lane"))
    row["dex_id"] = normalize_dex_id(row.get("dex_id"))
    row["price_source"] = normalize_price_source(row.get("price_source"))

    row["label"] = int(label) if label is not None else None
    row["target_total_pnl_pct"] = _normalize_scalar(target_total_pnl_pct)
    row["sample_type"] = normalize_sample_type(sample_type)
    row["ts"] = dt.datetime.now(dt.timezone.utc)
    # Outcome-only columns are absent from builder/ALLOWED_FEATURES.
    targets = dict(outcome_targets or {})
    unexpected = set(targets) - set(_OUTCOME_COLS)
    if unexpected:
        raise ValueError(f"Unknown outcome target columns: {sorted(unexpected)}")
    for column in _OUTCOME_COLS:
        row[column] = _normalize_scalar(targets.get(column))

    pa_table = pa.Table.from_pydict({k: [v] for k, v in row.items()})

    try:
        written = _write(pa_table, _file_for_now(partition_at))
        if not written:
            return False
        _ROW_COUNT += 1
        if _ROW_COUNT % 100 == 0:
            log.info("Features acumuladas: %s", _ROW_COUNT)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Parquet append error → %s", exc)
        if strict:
            raise
        return False


def update_pnl(address: str, pnl_pct: float) -> None:
    """Legacy helper: actualiza pnl_pct y target_total_pnl_pct en la última fila del token."""
    path = _file_for_now()

    try:
        with _exclusive_parquet_lock(path):
            if not path.exists():
                return
            table = pq.read_table(path)
            # Nota: 'address' es string(); .to_pylist sería costoso; iteramos columna
            addrs_col = table.column("address")
            idxs = [i for i in range(table.num_rows) if addrs_col[i].as_py() == address]
            if not idxs:
                return
            last = idxs[-1]
            if "outcome_trade_id" in table.schema.names and table.column("outcome_trade_id")[last].as_py():
                raise ValueError("Legacy token-wide PnL update cannot overwrite a checked causal close")

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
            _atomic_write_table(new_table, path)
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
