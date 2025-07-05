"""
Sub-paquete de persistencia.

Importar así:

    from memebot3.db import async_init_db, SessionLocal, Token, Position
"""
from __future__ import annotations

import sys
from types import ModuleType
from typing import cast

# ───────────────────── alias deduplicación ─────────────────────
_this_mod: ModuleType = cast(ModuleType, sys.modules[__name__])

if __name__ == "memebot3.db":                    # import normal dentro del paquete
    # Permite lanzar «python -m db.database» sin warning
    sys.modules.setdefault("db", _this_mod)
elif __name__ == "db":                           # lanzado como paquete raíz abreviado
    # Permite «import memebot3.db …» desde otros módulos
    sys.modules.setdefault("memebot3.db", _this_mod)

# ───────────────────── re-exports públicos ────────────────────
from .database import async_init_db, SessionLocal, Base  # noqa: E402,F401
from .models   import Token, Position                    # noqa: E402,F401

__all__ = [
    "async_init_db",
    "SessionLocal",
    "Base",
    "Token",
    "Position",
]
