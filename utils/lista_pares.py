# utils/lista_pares.py
"""
Cola de pares pendientes con **dos** niveles de re-intento:

1.  *INCOMPLETE_RETRIES* (rápidos, en run_bot – NO se gestionan aquí).
2.  *MAX_RETRIES* (re-queues) ‒ cada vez que el par vuelve a la cola,
    este contador se reduce; al llegar a 0 se descarta para siempre.

Este módulo solo controla el segundo nivel.

Cambios 2025-08-02
──────────────────
• Lee los nuevos envs `INCOMPLETE_RETRIES` y `MAX_RETRIES`.
• Mantiene `attempts` para que run_bot decida si activará GeckoTerminal.
"""

from __future__ import annotations

import logging
import os
import pathlib
import time
from typing import Dict, Optional

# ─── configuración ────────────────────────────────────────────
INCOMPLETE_RETRIES = int(os.getenv("INCOMPLETE_RETRIES", "3"))  # ← nivel rápido (referencia)
MAX_RETRIES        = int(os.getenv("MAX_RETRIES", "5"))         # ← re-queues permitidos
MAX_QUEUE_SIZE     = int(os.getenv("MAX_QUEUE_SIZE", "300"))
BACKOFF_SEC        = 120          # espera tras cada fallo (s)
MAX_INCOMPLETE_SEC = 600          # 10 min sin datos → drop

BASE_DIR   = pathlib.Path(__file__).resolve().parent.parent / "data"
CACHE_FILE = BASE_DIR / "pares_procesados.txt"
BASE_DIR.mkdir(exist_ok=True)

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
    except Exception as exc:  # noqa: BLE001
        log.warning("[lista_pares] No se pudo escribir cache: %s", exc)

# ─── API pública ───────────────────────────────────────────────
def agregar_si_nuevo(addr: str, retries: int | None = None) -> None:
    """
    Mete *addr* en la cola si nunca se procesó y hay espacio.
    """
    if addr in _processed or addr in _pair_watch:
        return

    # cola llena → descarta más antiguo
    if len(_pair_watch) >= MAX_QUEUE_SIZE:
        old = min(
            _pair_watch.items(),
            key=lambda it: (it[1]["retries"], it[1]["first_seen"]),
        )[0]
        log.debug("[lista_pares] Cola llena → drop %s", old[:6])
        eliminar_par(old)

    now = time.time()
    _pair_watch[addr] = {
        "retries": retries if retries is not None else MAX_RETRIES,
        "first_seen": now,
        "next_try": now,      # inmediato
        "attempts": 0,        # veces re-encolado
        "reason": "",
    }

def obtener_pares() -> list[str]:
    """Devuelve los pares listos para procesar (sin cooldown)."""
    now = time.time()
    return [a for a, meta in _pair_watch.items() if meta["next_try"] <= now]

def requeue(addr: str, *, reason: str = "", backoff: int | None = None) -> None:
    """
    Re-encola *addr* aplicando back-off y reduciendo `retries`.
    """
    meta = _pair_watch.get(addr)
    if not meta:
        return

    meta["retries"]  -= 1
    meta["attempts"]  = int(meta.get("attempts", 0)) + 1
    meta["reason"]    = reason or meta.get("reason", "")
    delay             = backoff or BACKOFF_SEC
    meta["next_try"]  = time.time() + delay

    log.debug("↩️  %s re-queue (%s, delay=%ss)", addr[:4], meta["reason"], delay)

    # sin re-queues restantes
    if meta["retries"] <= 0:
        log.debug("[lista_pares] Agota re-queues %s", addr[:6])
        eliminar_par(addr)
        return

    # timeout de incompleto
    if time.time() - meta["first_seen"] > MAX_INCOMPLETE_SEC:
        log.debug("[lista_pares] Timeout incompleto %s", addr[:6])
        eliminar_par(addr)

def eliminar_par(addr: str) -> None:
    """Saca el mint de la cola y lo marca como procesado definitivamente."""
    _pair_watch.pop(addr, None)
    if addr not in _processed:
        _processed.add(addr)
        _persist(addr)

def retries_left(addr: str) -> int:
    meta = _pair_watch.get(addr)
    return int(meta["retries"]) if meta else 0

def meta(addr: str) -> Optional[Dict[str, float | int | str]]:
    """Devuelve el dict interno asociado a *addr* (o None)."""
    return _pair_watch.get(addr)

# ─── métricas para logs ───────────────────────────────────────
def stats() -> tuple[int, int, int]:
    """
    Returns
    -------
    pendientes_totales : int
        Elementos aún en cola (incluyendo cooldown).
    requeued : int
        Elementos que ya sufrieron ≥1 re-queue.
    cooldown : int
        Elementos actualmente en espera (next_try > now).
    """
    now = time.time()
    requeued = sum(1 for m in _pair_watch.values() if m["attempts"] > 0)
    cooldown = sum(1 for m in _pair_watch.values() if m["next_try"] > now)
    return len(_pair_watch), requeued, cooldown
