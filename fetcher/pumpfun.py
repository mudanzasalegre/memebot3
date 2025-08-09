# memebot3/fetcher/pumpfun.py
"""
Pump.fun (nuevos tokens) vía **PumpPortal** WebSocket (FREE).

- Conecta a:   wss://pumpportal.fun/api/data
- Suscribe:    {"method": "subscribeNewToken"}
- Mantiene UNA única conexión WS (evita ban), con backoff y keepalive.
- Normaliza eventos al esquema DexScreener-like (address/symbol/name/created_at…).
- Buffer circular en memoria + cache suave para no sobrecargar la UI/pipeline.

Requisitos: aiohttp (ya presente en el proyecto).
Docs: ver PumpPortal → Data API → Real-time Updates.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from collections import deque
from typing import Any, Dict, List, Optional

import aiohttp

from utils.data_utils import sanitize_token_data
from utils.simple_cache import cache_get, cache_set
from utils.time import utc_now

log = logging.getLogger("pumpfun")

# ─────────────────────────── Config ────────────────────────────
_WS_URL = "wss://pumpportal.fun/api/data"

# nº máx. de tokens a devolver en cada llamada pública
_LIMIT_RETURN = int(os.getenv("PUMPFUN_LIMIT_RETURN", "12"))

# cache “suave” de la lista (segundos)
_CACHE_TTL = int(os.getenv("PUMPFUN_CACHE_TTL", "5"))

# buffer circular y ventana de frescura (minutos)
_BUFFER_MAX = int(os.getenv("PUMPFUN_BUFFER_MAX", "200"))
_WINDOW_MIN = float(os.getenv("PUMPFUN_WINDOW_MIN", "20"))

# backoff de reconexión (segundos)
_BACKOFFS = [2, 4, 8, 16, 30, 60, 90]

# ─────────────────────────── Estado global ─────────────────────
_buffer: deque[Dict[str, Any]] = deque(maxlen=_BUFFER_MAX)
_seen: set[str] = set()
_ws_task: Optional[asyncio.Task] = None
_ws_lock = asyncio.Lock()     # garantiza una sola conexión viva
_started = asyncio.Event()    # para esperar a que arranque la suscripción


# ────────────────────────── Helpers internos ───────────────────
def _to_dt(ts: Any) -> dt.datetime:
    """
    Convierte distintos formatos de timestamp a UTC.
    Admite:
      - int/float en segundos o milisegundos
      - ISO8601 str
      - si no hay timestamp, usa utc_now()
    """
    if ts is None:
        return utc_now()
    try:
        if isinstance(ts, (int, float)):
            # Heurística: milisegundos si es muy grande
            if ts > 1e12:
                return dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc)
            if ts > 1e10:  # ns → a ms a veces
                return dt.datetime.fromtimestamp(ts / 1e9, tz=dt.timezone.utc)
            return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        if isinstance(ts, str):
            s = ts.rstrip("Z")
            dtobj = dt.datetime.fromisoformat(s)
            return dtobj if dtobj.tzinfo else dtobj.replace(tzinfo=dt.timezone.utc)
    except Exception:
        pass
    return utc_now()


def _within_window(d: Dict[str, Any]) -> bool:
    """True si el token sigue dentro de la ventana de frescura."""
    try:
        ts = d["created_at"]
        if isinstance(ts, str):
            ts = dt.datetime.fromisoformat(ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        age_min = (utc_now() - ts).total_seconds() / 60.0
        return age_min <= _WINDOW_MIN
    except Exception:
        return True


def _extract_first(d: Dict[str, Any], *keys: str) -> Any:
    """Devuelve el primer valor no vacío encontrado en d para cualquiera de las keys (admite nested 'data')."""
    for k in keys:
        if k in d and d[k] not in (None, "", 0):
            return d[k]
    payload = d.get("data") if isinstance(d.get("data"), dict) else None
    if payload:
        for k in keys:
            if k in payload and payload[k] not in (None, "", 0):
                return payload[k]
    return None


def _parse_event(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Convierte un evento PumpPortal → dict normalizado.
    Formato del WS no está 100% fijado: intentamos extraer con tolerancia.

    Campos que solemos ver:
      - mint (CA del token)  – obligatorio para nosotros
      - name, symbol         – opcionales (a veces no vienen)
      - timestamp / ts       – opcional
      - creator / user       – opcional
    """
    try:
        mint = _extract_first(msg, "mint", "ca", "address", "token", "tokenAddress")
        if not mint:
            # Algunos envíos anidan aún más:
            dat = msg.get("data") or {}
            mint = _extract_first(dat, "mint", "ca", "address", "token", "tokenAddress")
        if not mint:
            return None

        name   = (_extract_first(msg, "name", "tokenName") or "").strip()
        symbol = (_extract_first(msg, "symbol", "tokenSymbol") or "").strip()

        ts_in  = _extract_first(msg, "timestamp", "ts", "time", "createdAt", "created_at")
        ts     = _to_dt(ts_in)

        creator = _extract_first(msg, "creator", "user", "owner", "signer") or ""

        now = utc_now()
        tok = {
            "address": mint,
            "symbol": (symbol or "NEW")[:16],
            "name": name or "",
            "created_at": ts,

            # dummy: se rellenarán después por DexScreener/Birdeye/GeckoTerminal
            "liquidity_usd": 0.0,
            "volume_24h_usd": 0.0,
            "market_cap_usd": 0.0,
            "holders": 0,

            # meta
            "discovered_via": "pumpfun",
            "age_minutes": (now - ts).total_seconds() / 60.0,
            "creator": creator,
        }
        return sanitize_token_data(tok)
    except Exception as exc:  # pragma: no cover
        log.debug("[PumpFun] evento mal formado: %s", exc)
        return None


# ─────────────────────────── WS Consumer ───────────────────────
async def _ws_consumer() -> None:
    """
    Mantiene la suscripción viva y vuelca eventos al buffer.
    Solo debe existir UNA tarea de este consumidor.
    """
    backoff_idx = 0

    while True:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(
                    _WS_URL,
                    heartbeat=30,
                    timeout=aiohttp.ClientTimeout(total=0),  # streaming
                ) as ws:
                    # Suscripción a nuevos tokens
                    await ws.send_json({"method": "subscribeNewToken"})
                    log.info("[PumpFun] Suscripción activa a subscribeNewToken")
                    _started.set()
                    backoff_idx = 0  # reset tras conectar

                    # Bucle de mensajes
                    while True:
                        msg = await ws.receive()

                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except Exception:
                                continue

                            parsed = _parse_event(data)
                            if parsed:
                                addr = parsed["address"]
                                if addr not in _seen:
                                    _seen.add(addr)
                                    _buffer.appendleft(parsed)

                                # purga por ventana
                                while _buffer and not _within_window(_buffer[-1]):
                                    old = _buffer.pop()
                                    _seen.discard(old["address"])

                        elif msg.type == aiohttp.WSMsgType.PING:
                            await ws.pong()

                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                            raise ConnectionError("WS closed")

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            raise ConnectionError("WS error frame recibido")

        except asyncio.CancelledError:
            log.info("[PumpFun] WS consumer cancelado")
            raise
        except Exception as exc:
            wait = _BACKOFFS[min(backoff_idx, len(_BACKOFFS) - 1)]
            backoff_idx += 1
            log.warning("[PumpFun] WS desconectado (%s). Reintentando en %ss…", exc, wait)
            await asyncio.sleep(wait)
            # intentará reconectar


async def _ensure_started() -> None:
    """Inicializa el consumidor si no está arrancado."""
    global _ws_task
    if _ws_task and not _ws_task.done():
        return

    async with _ws_lock:
        if _ws_task and not _ws_task.done():
            return
        _started.clear()
        _ws_task = asyncio.create_task(_ws_consumer(), name="pumpportal-ws-consumer")

    # esperar arranque inicial un momento (evita race)
    try:
        await asyncio.wait_for(_started.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        pass


# ───────────────────────── API pública ─────────────────────────
async def get_latest_pumpfun() -> List[Dict[str, Any]]:
    """
    Devuelve hasta `_LIMIT_RETURN` tokens recientes descubiertos en Pump.fun.
    No realiza llamadas HTTP por petición; lee de un buffer alimentado por WS.
    """
    # cache suave
    if (res := cache_get("pumpfun:latest")) is not None:
        return res

    # inicia el stream si hace falta
    await _ensure_started()

    # filtra por ventana y limita
    fresh = [d for d in list(_buffer) if _within_window(d)]
    out = fresh[:_LIMIT_RETURN]

    cache_set("pumpfun:latest", out, ttl=_CACHE_TTL)
    log.debug("[PumpFun] entregados %d (buffer=%d)", len(out), len(_buffer))
    return out
