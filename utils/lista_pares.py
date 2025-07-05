# memebot3/utils/lista_pares.py
"""
Mantiene la **cola de pares pendientes** con re-intentos controlados
y una caché en disco de los mints que ya procesamos.

Novedades 2025-06-28
────────────────────
• `_pair_watch` pasa a ser dict {mint: retries_left}.
• `MAX_RETRIES` configurable (env INCOMPLETE_RETRIES, por defecto 3).
• Funciones nuevas: `requeue()` y `stats()`.
"""

from __future__ import annotations

import logging
import os
import pathlib

# ─── configuración ─────────────────────────────────────────────
MAX_RETRIES = int(os.getenv("INCOMPLETE_RETRIES", "3"))

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
BASE_DIR.mkdir(exist_ok=True)
CACHE_FILE = BASE_DIR / "pares_procesados.txt"

# ─── estructuras internas ────────────────────────────
_pair_watch: dict[str, int] = {}   # {mint: retries_left}
_processed:  set[str]        = set()

log = logging.getLogger("lista_pares")

# ─── helpers caché disco ───────────────────────────────────────
def _load_cache() -> set[str]:
    if not CACHE_FILE.exists():
        return set()
    with CACHE_FILE.open() as f:
        return {ln.strip() for ln in f if ln.strip()}

_processed.update(_load_cache())

def _persist(addr: str) -> None:
    try:
        with CACHE_FILE.open("a") as f:
            f.write(addr + "\n")
    except Exception as e:                       # noqa: BLE001
        log.warning("[lista_pares] No se pudo escribir cache: %s", e)

# ─── API pública ───────────────────────────────────────────────
def agregar_si_nuevo(addr: str, retries: int | None = None) -> None:
    """
    Mete *addr* en la cola si nunca lo vimos.
    `retries` → nº de intentos (None ⇒ MAX_RETRIES).
    """
    if addr in _processed or addr in _pair_watch:
        return
    _pair_watch[addr] = retries or MAX_RETRIES

def obtener_pares() -> list[str]:
    """Snapshot de la cola (orden indeterminado)."""
    return list(_pair_watch.keys())

def requeue(addr: str) -> None:
    """
    Reduce en 1 el contador.  Si llega a 0 ⇒ lo marcamos como procesado.
    """
    if addr not in _pair_watch:
        return
    _pair_watch[addr] -= 1
    if _pair_watch[addr] <= 0:
        eliminar_par(addr)           # agota intentos

def eliminar_par(addr: str) -> None:
    """
    Saca el mint de la cola y lo añade a la caché “procesados”.
    """
    _pair_watch.pop(addr, None)
    if addr not in _processed:
        _processed.add(addr)
        _persist(addr)

def retries_left(addr: str) -> int:
    return _pair_watch.get(addr, 0)

# —— métricas para logs ————————————————————————————————
def stats() -> tuple[int, int]:
    """
    Devuelve (pendientes_totales, requeued) donde *requeued* son
    los que ya llevan ≥1 intento fallido.
    """
    requeued = sum(1 for v in _pair_watch.values() if v < MAX_RETRIES)
    return len(_pair_watch), requeued
