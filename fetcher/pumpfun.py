# memebot3/fetcher/pumpfun.py
"""
Pump.fun (nuevos tokens) vía **PumpPortal** WebSocket (FREE).

- Conecta a:   wss://pumpportal.fun/api/data?api-key=... (vía PUMPPORTAL_API_KEY)
- Suscribe:    {"method": "subscribeNewToken"}
- Mantiene UNA única conexión WS (evita ban), con backoff y keepalive.
- Normaliza eventos al esquema DexScreener-like (address/symbol/name/created_at…).
- Buffer circular en memoria + cache suave para no sobrecargar la UI/pipeline.

Requisitos: aiohttp (ya presente en el proyecto).
Docs: ver PumpPortal → Data API → Real-time Updates.

Mejoras:
• Normalización de mint con utils.solana_addr.normalize_mint (preserva mints válidos y sanea sufijos inválidos).
• Fechas robustas con utils.time.parse_iso_utc (evita errores `.replace`).
• Métricas críticas como None (no 0.0) para no “matar” señales tempranas.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from collections import deque
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from utils.data_utils import sanitize_token_data
from utils.simple_cache import cache_get, cache_set
from utils.time import utc_now, parse_iso_utc
from utils.solana_addr import normalize_mint

log = logging.getLogger("pumpfun")

# ─────────────────────────── Config ────────────────────────────
_DEFAULT_WS_URL = "wss://pumpportal.fun/api/data"
_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return default


def _url_has_api_key(url: str) -> bool:
    query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    return any(k.lower() in {"api-key", "api_key", "apikey"} and bool(v.strip()) for k, v in query)


def _build_ws_url(base_url: str, api_key: str = "") -> str:
    url = (base_url or _DEFAULT_WS_URL).strip() or _DEFAULT_WS_URL
    api_key = (api_key or "").strip()
    if not api_key or _url_has_api_key(url):
        return url

    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("api-key", api_key))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _redact_ws_url(url: str) -> str:
    parts = urlsplit(url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in {"api-key", "api_key", "apikey"} and value:
            query.append((key, "***"))
        else:
            query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _resolve_ws_config(
    *,
    base_url: str,
    api_key: str,
    require_api_key: bool,
    enabled: bool,
) -> tuple[str, Optional[str]]:
    url = _build_ws_url(base_url, api_key)
    if not enabled:
        return url, "PUMPFUN_WS_ENABLED=0"
    if require_api_key and not _url_has_api_key(url):
        return (
            url,
            "falta PUMPPORTAL_API_KEY o PUMPPORTAL_WS_URL con api-key; "
            "PumpPortal ahora publica el WS con api-key en la URL",
        )
    return url, None


_WS_URL, _WS_DISABLED_REASON = _resolve_ws_config(
    base_url=os.getenv("PUMPPORTAL_WS_URL") or os.getenv("PUMPFUN_WS_URL") or _DEFAULT_WS_URL,
    api_key=os.getenv("PUMPPORTAL_API_KEY") or os.getenv("PUMPFUN_API_KEY") or "",
    require_api_key=_env_bool("PUMPPORTAL_REQUIRE_API_KEY", True),
    enabled=_env_bool("PUMPFUN_WS_ENABLED", True),
)
_WS_URL_SAFE = _redact_ws_url(_WS_URL)
_API_KEY_FOR_REDACTION = (os.getenv("PUMPPORTAL_API_KEY") or os.getenv("PUMPFUN_API_KEY") or "").strip()

# nº máx. de tokens a devolver en cada llamada pública
_LIMIT_RETURN = int(os.getenv("PUMPFUN_LIMIT_RETURN", "75"))

# cache “suave” de la lista (segundos)
_CACHE_TTL = int(os.getenv("PUMPFUN_CACHE_TTL", "1"))

# buffer circular y ventana de frescura (minutos)
_BUFFER_MAX = int(os.getenv("PUMPFUN_BUFFER_MAX", "1500"))
_WINDOW_MIN = float(os.getenv("PUMPFUN_WINDOW_MIN", "60"))

# backoff de reconexión (segundos)
_BACKOFFS = [2, 4, 8, 16, 30, 60, 90]
_MAX_CONSECUTIVE_5XX = int(os.getenv("PUMPFUN_WS_MAX_CONSECUTIVE_5XX", "1"))
_CIRCUIT_BREAK_S = int(os.getenv("PUMPFUN_WS_CIRCUIT_BREAK_S", "3600"))

# ─────────────────────────── Estado global ─────────────────────
_buffer: deque[Dict[str, Any]] = deque(maxlen=_BUFFER_MAX)
_seen: set[str] = set()
_ws_task: Optional[asyncio.Task] = None
_ws_lock = asyncio.Lock()     # garantiza una sola conexión viva
_started = asyncio.Event()    # para esperar a que arranque la suscripción
_disabled_logged = False


# ────────────────────────── Helpers internos ───────────────────
def _to_dt(ts: Any) -> dt.datetime:
    """
    Convierte distintos formatos de timestamp a UTC.
    Admite:
      - int/float en segundos o milisegundos
      - ISO8601 str → parse_iso_utc
      - si no hay timestamp, usa utc_now()
    """
    if ts is None:
        return utc_now()
    try:
        if isinstance(ts, (int, float)):
            # Heurística: milisegundos si es muy grande
            if ts > 1e12:
                return dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc)
            if ts > 1e10:  # ns → a s (por si acaso)
                return dt.datetime.fromtimestamp(ts / 1e9, tz=dt.timezone.utc)
            return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        if isinstance(ts, str):
            dtobj = parse_iso_utc(ts)
            return dtobj or utc_now()
    except Exception:
        pass
    return utc_now()


def _within_window(d: Dict[str, Any]) -> bool:
    """True si el token sigue dentro de la ventana de frescura."""
    try:
        ts = d.get("created_at")
        if isinstance(ts, str):
            ts_parsed = parse_iso_utc(ts)
            ts = ts_parsed or utc_now()
        if isinstance(ts, dt.datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            age_min = (utc_now() - ts).total_seconds() / 60.0
            return age_min <= _WINDOW_MIN
    except Exception:
        return True
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


def _format_ws_error(exc: Exception) -> str:
    if isinstance(exc, aiohttp.WSServerHandshakeError):
        return f"handshake HTTP {exc.status} ({exc.message}, url='{_WS_URL_SAFE}')"

    text = str(exc)
    text = text.replace(_WS_URL, _WS_URL_SAFE)
    if _API_KEY_FOR_REDACTION:
        text = text.replace(_API_KEY_FOR_REDACTION, "***")
    return text


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
        mint_raw = _extract_first(msg, "mint", "ca", "address", "token", "tokenAddress")
        if not mint_raw:
            dat = msg.get("data") or {}
            mint_raw = _extract_first(dat, "mint", "ca", "address", "token", "tokenAddress")
        mint = normalize_mint(mint_raw or "")
        if not mint:
            return None

        name   = (_extract_first(msg, "name", "tokenName") or "").strip()
        symbol = (_extract_first(msg, "symbol", "tokenSymbol") or "").strip()

        ts_in  = _extract_first(msg, "timestamp", "ts", "time", "createdAt", "created_at")
        ts     = _to_dt(ts_in)

        creator = _extract_first(msg, "creator", "user", "owner", "signer") or ""

        now = utc_now()
        age_minutes = (now - ts).total_seconds() / 60.0

        tok = {
            "address": mint,
            "symbol": (symbol or "NEW")[:16],
            "name": name or "",
            "created_at": ts,

            # Métricas críticas: None (se rellenarán por DexScreener/Birdeye/GT)
            "liquidity_usd": None,
            "volume_24h_usd": None,
            "market_cap_usd": None,
            "holders": None,

            # meta
            "discovered_via": "pumpfun",
            "age_minutes": age_minutes,
            "age_min": age_minutes,   # alias útil para lectores
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
    consecutive_5xx = 0

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
                    consecutive_5xx = 0

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
            is_handshake_5xx = isinstance(exc, aiohttp.WSServerHandshakeError) and 500 <= exc.status <= 599
            consecutive_5xx = consecutive_5xx + 1 if is_handshake_5xx else 0
            wait = _BACKOFFS[min(backoff_idx, len(_BACKOFFS) - 1)]
            backoff_idx += 1
            circuit_open = False
            if is_handshake_5xx and _MAX_CONSECUTIVE_5XX > 0 and consecutive_5xx >= _MAX_CONSECUTIVE_5XX:
                wait = max(_CIRCUIT_BREAK_S, _BACKOFFS[-1])
                backoff_idx = 0
                consecutive_5xx = 0
                circuit_open = True
            exc = _format_ws_error(exc)
            if circuit_open:
                log.warning(
                    "[PumpFun] WS handshake 5xx de PumpPortal (%s). Pausando %ss antes de reintentar.",
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            log.warning("[PumpFun] WS desconectado (%s). Reintentando en %ss…", exc, wait)
            await asyncio.sleep(wait)
            # intentará reconectar


async def _ensure_started() -> None:
    """Inicializa el consumidor si no está arrancado."""
    global _ws_task, _disabled_logged
    if _WS_DISABLED_REASON:
        if not _disabled_logged:
            log.warning("[PumpFun] WS desactivado: %s", _WS_DISABLED_REASON)
            _disabled_logged = True
        return
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
