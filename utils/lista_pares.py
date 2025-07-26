# memebot3/utils/lista_pares.py
"""
Mantiene la **cola de pares pendientes** con re-intentos controlados y
una caché en disco de los mints ya procesados.

Cambios 2025-07-25
──────────────────
• Límite duro de tamaño de cola con `MAX_QUEUE_SIZE`
• Métricas `stats()` inalteradas, pero `run_bot` ahora las registra cada 60 s
• Log cuando la cola se llena
"""
from __future__ import annotations

import logging
import os
import pathlib
import time
from typing import Dict

# ─── configuración ────────────────────────────────────────────
MAX_RETRIES        = int(os.getenv("INCOMPLETE_RETRIES", "3"))
MAX_QUEUE_SIZE     = int(os.getenv("MAX_QUEUE_SIZE",    "1000"))  # ← NUEVO
BACKOFF_SEC        = 120          # espera tras cada fallo
MAX_INCOMPLETE_SEC = 600          # 10 min → descartar

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
BASE_DIR.mkdir(exist_ok=True)
CACHE_FILE = BASE_DIR / "pares_procesados.txt"

log = logging.getLogger("lista_pares")

# ─── estructuras internas ─────────────────────────────────────
_pair_watch: Dict[str, Dict[str, float | int]] = {}
_processed:  set[str] = set()

# ─── helpers caché disco ──────────────────────────────────────
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
    except Exception as e:  # noqa: BLE001
        log.warning("[lista_pares] No se pudo escribir cache: %s", e)

# ─── API pública ───────────────────────────────────────────────
def agregar_si_nuevo(addr: str, retries: int | None = None) -> None:
    """
    Mete *addr* en la cola si nunca lo vimos y hay espacio disponible.
    """
    if addr in _processed or addr in _pair_watch:
        return

    if len(_pair_watch) >= MAX_QUEUE_SIZE:
        log.debug("[lista_pares] Cola llena (%s)", MAX_QUEUE_SIZE)
        return

    now = time.time()
    _pair_watch[addr] = {
        "retries": retries or MAX_RETRIES,
        "first_seen": now,
        "next_try": now,  # inmediato
    }

def obtener_pares() -> list[str]:
    """
    Devuelve los pares listos para procesar **ahora** (sin cooldown).
    """
    now = time.time()
    return [a for a, meta in _pair_watch.items() if meta["next_try"] <= now]

def requeue(addr: str) -> None:
    """
    Reduce el contador y programa el siguiente intento tras BACKOFF_SEC.
    Si se agota el contador o supera MAX_INCOMPLETE_SEC → elimina.
    """
    meta = _pair_watch.get(addr)
    if not meta:
        return

    meta["retries"] -= 1
    meta["next_try"] = time.time() + BACKOFF_SEC

    # sin retries
    if meta["retries"] <= 0:
        log.debug("[lista_pares] Agota reintentos %s", addr[:6])
        eliminar_par(addr)
        return

    # timeout incompleto
    if time.time() - meta["first_seen"] > MAX_INCOMPLETE_SEC:
        log.debug("[lista_pares] Timeout incompleto %s", addr[:6])
        eliminar_par(addr)

def eliminar_par(addr: str) -> None:
    """
    Saca el mint de la cola y lo añade a la caché “procesados”.
    """
    _pair_watch.pop(addr, None)
    if addr not in _processed:
        _processed.add(addr)
        _persist(addr)

def retries_left(addr: str) -> int:
    meta = _pair_watch.get(addr)
    return int(meta["retries"]) if meta else 0

# ─── métricas para logs ───────────────────────────────────────
def stats() -> tuple[int, int, int]:
    """
    Returns
    -------
    pendientes_totales : int
        Elementos aún en cola (incluyendo los en cooldown)
    requeued : int
        Elementos que ya sufrieron ≥1 re-intento
    cooldown : int
        Elementos actualmente en espera (next_try > now)
    """
    now = time.time()
    requeued = sum(1 for m in _pair_watch.values() if m["retries"] < MAX_RETRIES)
    cooldown = sum(1 for m in _pair_watch.values() if m["next_try"] > now)
    return len(_pair_watch), requeued, cooldown
