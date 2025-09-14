# memebot3/db/database.py
"""
Motor SQLite asíncrono + helpers de actualización de posiciones.

Funciona tanto si se ejecuta como:
    python -m memebot3.db.database
como:
    python -m db.database          (dentro de la raíz del repo)

• Si SQLITE_DB es relativa, siempre se ubica bajo memebot3/data/.
• Asegura columnas útiles en Position:
    - partial_taken (INTEGER NOT NULL DEFAULT 0)
    - peak_price    (REAL    NOT NULL DEFAULT 0.0)
• Expone utilidades asíncronas para actualizar peak, parciales y cierre.
"""
from __future__ import annotations

# ---- silenciar RuntimeWarning del launcher -m db.database ------------
import warnings, runpy    #  NO mover después de otros imports
warnings.filterwarnings(
    "ignore",
    message=r".*'db\.database' found in sys\.modules.*",
    category=RuntimeWarning,
    module=runpy.__name__,
)

import asyncio
from datetime import datetime, timezone
import sys
from pathlib import Path
from typing import Optional, List, Tuple

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# ───────────────── localizar paquete ──────────────────
HERE = Path(__file__).resolve()
PKG_ROOT = HERE.parents[1]            # …/memebot3
REPO_ROOT = PKG_ROOT.parent

if str(REPO_ROOT) not in sys.path:    # garantiza import config
    sys.path.insert(0, str(REPO_ROOT))

from config import SQLITE_DB          # type: ignore

# ─────── ruta definitiva de la BD ───────
sqlite_path = Path(SQLITE_DB).expanduser()
if not sqlite_path.is_absolute():
    sqlite_path = (PKG_ROOT / sqlite_path).resolve()

sqlite_path.parent.mkdir(parents=True, exist_ok=True)
DB_PATH: Path = sqlite_path

# ───────── Declarative Base / Engine ─────────
class Base(DeclarativeBase):  # type: ignore
    """Declarative base (async)."""

engine: AsyncEngine = create_async_engine(
    f"sqlite+aiosqlite:///{DB_PATH.as_posix()}",
    echo=False,
    future=True,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)

# ─────────── helpers de esquema (SQLite) ───────────
async def _table_has_column(conn, table: str, column: str) -> bool:
    """
    Devuelve True si `column` existe en `table` (SQLite).
    """
    res = await conn.exec_driver_sql(f"PRAGMA table_info({table});")
    rows = res.fetchall()
    cols = {r[1] for r in rows}  # (cid, name, type, notnull, dflt_value, pk)
    return column in cols

async def _ensure_position_columns() -> None:
    """
    Asegura que la tabla Position contiene las columnas necesarias:
      - partial_taken INTEGER NOT NULL DEFAULT 0
      - peak_price    REAL    NOT NULL DEFAULT 0.0
    Usa ALTER TABLE condicional (safe para SQLite).
    """
    async with engine.begin() as conn:
        # Si la tabla Position no existe aún, create_all las creará después.
        # Comprobamos columnas solo si la tabla ya está creada.
        res = await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='position';"
        )
        tbl = res.scalar_one_or_none()
        if not tbl:
            return  # la crearemos en create_all()

        # partial_taken
        if not await _table_has_column(conn, "position", "partial_taken"):
            await conn.exec_driver_sql(
                "ALTER TABLE position ADD COLUMN partial_taken INTEGER NOT NULL DEFAULT 0;"
            )

        # peak_price
        if not await _table_has_column(conn, "position", "peak_price"):
            await conn.exec_driver_sql(
                "ALTER TABLE position ADD COLUMN peak_price REAL NOT NULL DEFAULT 0.0;"
            )

# ─────────── Init helper ───────────
async def async_init_db() -> None:
    """
    Crea las tablas si no existen y asegura columnas nuevas (SQLite).
    Ejecutar también para regenerar la BD tras cambios de esquema.
    """
    from . import models  # noqa: F401  — registra modelos

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # WAL = mejor concurrencia
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL;")

    # Asegura columnas adicionales en SQLite (ALTER TABLE si faltan)
    await _ensure_position_columns()

    print(f"[DB] OK  →  {DB_PATH}")

# ─────────── Helpers de dominio (Position) ───────────
# Nota: asumimos un modelo Position con (campos habituales):
#   id, token_mint, qty_lamports, buy_price_usd, peak_price,
#   opened_at, closed, closed_at, close_price_usd, pnl_pct,
#   exit_reason, price_source_close, partial_taken, ...
from .models import Position  # type: ignore

async def get_open_positions(session: AsyncSession) -> List[Position]:
    stmt = select(Position).where(Position.closed.is_(False))
    res = await session.execute(stmt)
    return list(res.scalars())

async def get_position_by_mint(session: AsyncSession, token_mint: str) -> Optional[Position]:
    stmt = select(Position).where(Position.token_mint == token_mint).limit(1)
    res = await session.execute(stmt)
    return res.scalars().first()

async def update_peak_price(session: AsyncSession, pos_id: int, new_peak: float) -> Optional[Position]:
    """
    Sube el peak_price si `new_peak` es mayor que el actual.
    """
    pos = await session.get(Position, pos_id)
    if not pos:
        return None
    try:
        current = float(pos.peak_price or 0.0)
    except Exception:
        current = 0.0
    if new_peak > 0.0 and new_peak > current:
        pos.peak_price = float(new_peak)
        await session.commit()
        await session.refresh(pos)
    return pos

async def set_qty_lamports(session: AsyncSession, pos_id: int, qty_lamports: int) -> Optional[Position]:
    """
    Fija cantidad restante en lamports (no negativa).
    """
    pos = await session.get(Position, pos_id)
    if not pos:
        return None
    pos.qty_lamports = max(0, int(qty_lamports))
    await session.commit()
    await session.refresh(pos)
    return pos

async def mark_partial_and_reduce_qty(
    session: AsyncSession,
    pos_id: int,
    qty_sold: int,
    *,
    last_partial_price_usd: Optional[float] = None,
) -> Optional[Position]:
    """
    Marca `partial_taken=True` y descuenta `qty_sold` de `qty_lamports`.
    Guarda opcionalmente precio de la parcial (si existe el campo).
    """
    pos = await session.get(Position, pos_id)
    if not pos:
        return None
    remaining = max(0, int(getattr(pos, "qty_lamports", 0)) - int(qty_sold))
    pos.qty_lamports = remaining
    # flag parcial
    try:
        pos.partial_taken = True  # type: ignore[attr-defined]
    except Exception:
        # Si el modelo aún no tiene la propiedad en runtime, ignorar (compat)
        pass
    # opcional: si el modelo incluye estos campos, se setean
    if last_partial_price_usd is not None and hasattr(pos, "last_partial_price_usd"):
        setattr(pos, "last_partial_price_usd", float(last_partial_price_usd))
        setattr(pos, "last_partial_at", datetime.now(timezone.utc))
    await session.commit()
    await session.refresh(pos)
    return pos

async def close_position_safe(
    session: AsyncSession,
    pos_id: int,
    *,
    close_price_usd: float,
    exit_reason: str,
    price_source_close: Optional[str] = None,
    closed_at_iso: Optional[str] = None,
) -> Optional[Position]:
    """
    Cierra la posición y sella campos de cierre. Calcula pnl_pct si hay buy_price.
    """
    pos = await session.get(Position, pos_id)
    if not pos:
        return None

    # timestamps
    if closed_at_iso:
        try:
            closed_at = datetime.fromisoformat(closed_at_iso)
            if closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=timezone.utc)
        except Exception:
            closed_at = datetime.now(timezone.utc)
    else:
        closed_at = datetime.now(timezone.utc)

    # PnL
    try:
        bp = float(getattr(pos, "buy_price_usd", 0.0) or 0.0)
    except Exception:
        bp = 0.0
    if bp > 0.0:
        pnl_pct = ((float(close_price_usd) - bp) / bp) * 100.0
    else:
        pnl_pct = 0.0

    # aplica cambios
    pos.close_price_usd = float(close_price_usd)
    pos.pnl_pct = float(pnl_pct)
    pos.exit_reason = str(exit_reason)
    pos.closed_at = closed_at
    pos.closed = True
    if price_source_close is not None and hasattr(pos, "price_source_close"):
        pos.price_source_close = price_source_close  # type: ignore[attr-defined]

    # al cerrar, cantidad remanente a 0 (si existe el campo)
    if hasattr(pos, "qty_lamports"):
        pos.qty_lamports = 0

    await session.commit()
    await session.refresh(pos)
    return pos

# ─────────── CLI helper ───────────
if __name__ == "__main__":
    asyncio.run(async_init_db())
