# memebot3/db/database.py
"""
Motor SQLite asíncrono + helper CLI.

Funciona tanto si se ejecuta como:
    python -m memebot3.db.database
como:
    python -m db.database          (dentro de la raíz del repo)

• Si SQLITE_DB es relativa, siempre se ubica bajo memebot3/data/.
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
import sys
from pathlib import Path
from typing import Optional

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

# ─────────── Init helper ───────────
async def async_init_db() -> None:
    """
    Crea las tablas si no existen.  Ejecutar también para regenerar
    la BD tras cambios de esquema.
    """
    from . import models  # noqa: F401  — registra modelos

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # WAL = mejor concurrencia
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL;")

    print(f"[DB] OK  →  {DB_PATH}")


# ─────────── CLI helper ───────────
if __name__ == "__main__":
    asyncio.run(async_init_db())
