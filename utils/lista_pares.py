# memebot3/utils/lista_pares.py
"""
Mantiene la **cola de pares pendientes** con re-intentos controlados y
una caché en disco de los mints ya procesados.

Cambios 2025-07-26
──────────────────
• Límite duro de tamaño de cola con `MAX_QUEUE_SIZE` (def. 300)
• `requeue()` acepta motivo y back-off variable; registra intentos
• Puede descartar el elemento más antiguo cuando la cola está llena
• `stats()` sigue exponiendo métricas para el dashboard
"""
from __future__ import annotations

import logging
import os
import pathlib
import time
from typing import Dict, Optional

# ─── configuración ────────────────────────────────────────────
MAX_RETRIES        = int(os.getenv("INCOMPLETE_RETRIES", "3"))
MAX_QUEUE_SIZE     = int(os.getenv("MAX_QUEUE_SIZE",    "300"))  # ← NUEVO
BACKOFF_SEC        = 120          # espera tras cada fallo
MAX_INCOMPLETE_SEC = 600          # 10 min → descartar

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
BASE_DIR.mkdir(exist_ok=True)
CACHE_FILE = BASE_DIR / "pares_procesados.txt"

log = logging.getLogger("lista_pares")

# ─── estructuras internas ─────────────────────────────────────
_pair_watch: Dict[str, Dict[str, float | int | str]] = {}
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
        # descarta el elemento más antiguo con menos reintentos pendientes
        old = sorted(
            _pair_watch.items(),
            key=lambda it: (it[1]["retries"], it[1]["first_seen"]),
        )[0][0]
        log.debug("[lista_pares] Cola llena → drop %s", old[:6])
        eliminar_par(old)

    now = time.time()
    _pair_watch[addr] = {
        "retries": retries or MAX_RETRIES,
        "first_seen": now,
        "next_try": now,  # inmediato
        "attempts": 0,
    }

def obtener_pares() -> list[str]:
    """
    Devuelve los pares listos para procesar **ahora** (sin cooldown).
    """
    now = time.time()
    return [a for a, meta in _pair_watch.items() if meta["next_try"] <= now]

def requeue(addr: str, *, reason: str = "", backoff: int | None = None) -> None:
    """
    Reduce el contador y programa el siguiente intento.
    Guarda el motivo y aumenta el nº de intentos.
    """
    meta = _pair_watch.get(addr)
    if not meta:
        return

    meta["retries"] -= 1
    meta["attempts"] = int(meta.get("attempts", 0)) + 1
    meta["reason"] = reason or meta.get("reason", "")
    meta["next_try"] = time.time() + (backoff or BACKOFF_SEC)

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

def meta(addr: str) -> Optional[Dict[str, float | int | str]]:
    """Devuelve el diccionario interno asociado a *addr* (o None)."""
    return _pair_watch.get(addr)

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
