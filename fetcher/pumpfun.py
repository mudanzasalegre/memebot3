# memebot3/fetcher/pumpfun.py
"""
Pump.fun (nuevos tokens) vía **PumpPortal** WebSocket (FREE).

- Conecta a:   wss://pumpportal.fun/api/data?api-key=... (vía PUMPPORTAL_API_KEY)
- Suscribe:    {"method": "subscribeNewToken"}
- Mantiene UNA única conexión WS (evita ban), con backoff y keepalive.
- Normaliza eventos al esquema DexScreener-like (address/symbol/name/created_at…).
- Cola FIFO acotada en memoria: cada evento se entrega una sola vez al pipeline.

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
import math
import os
from collections import deque
from copy import deepcopy
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from utils.data_utils import sanitize_token_data
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

# cola acotada, ventana de frescura y TTL de deduplicación (minutos)
_BUFFER_MAX = int(os.getenv("PUMPFUN_BUFFER_MAX", "1500"))
_WINDOW_MIN = float(os.getenv("PUMPFUN_WINDOW_MIN", "60"))
_SEEN_TTL_MIN = float(os.getenv("PUMPFUN_SEEN_TTL_MIN", str(max(_WINDOW_MIN, 60.0))))

# backoff de reconexión (segundos)
_BACKOFFS = [2, 4, 8, 16, 30, 60, 90]
_MAX_CONSECUTIVE_5XX = int(os.getenv("PUMPFUN_WS_MAX_CONSECUTIVE_5XX", "1"))
_CIRCUIT_BREAK_S = int(os.getenv("PUMPFUN_WS_CIRCUIT_BREAK_S", "3600"))

# ─────────────────────────── Estado global ─────────────────────
_buffer: deque[Dict[str, Any]] = deque()
# Pending receipt ownership is separate from actual API delivery history.
_pending: set[str] = set()
_seen: Dict[str, dt.datetime] = {}
_ws_task: Optional[asyncio.Task] = None
_ws_lock = asyncio.Lock()     # garantiza una sola conexión viva
_started = asyncio.Event()    # para esperar a que arranque la suscripción
_disabled_logged = False


# ────────────────────────── Helpers internos ───────────────────
def _to_dt(ts: Any) -> dt.datetime | None:
    """
    Convierte distintos formatos de timestamp a UTC.
    Admite:
      - int/float en segundos, milisegundos, microsegundos o nanosegundos
      - ISO8601 str → parse_iso_utc
      - un reloj ausente o invalido permanece desconocido
    """
    if ts is None or isinstance(ts, bool):
        return None
    try:
        if isinstance(ts, dt.datetime):
            return ts.replace(tzinfo=dt.timezone.utc) if ts.tzinfo is None else ts.astimezone(dt.timezone.utc)
        if isinstance(ts, (int, float)):
            if not math.isfinite(ts) or ts <= 0:
                return None
            # Normalize magnitude without manufacturing a missing clock.
            if ts >= 1e17:
                ts /= 1e9
            elif ts >= 1e14:
                ts /= 1e6
            elif ts >= 1e11:
                ts /= 1e3
            return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        if isinstance(ts, str):
            return parse_iso_utc(ts)
    except Exception:
        pass
    return None


def _within_window(d: Dict[str, Any], *, now: dt.datetime | None = None) -> bool:
    """Bound original receipt residence; known birth must also be recent.

    Receipt time is not token creation time. Unknown births stay eligible for
    fresh downstream discovery, but cannot remain in this buffer forever.
    """
    now = now or utc_now()
    clocks = []
    for key in ("discovered_at", "created_at"):
        if d.get(key) is not None:
            stamp = _to_dt(d[key])
            if stamp is None:
                return False
            clocks.append(stamp)
    return bool(clocks) and all(0 <= (now - stamp).total_seconds() / 60 <= _WINDOW_MIN for stamp in clocks)


def _prune_queue_state() -> tuple[int, int]:
    """Purga eventos caducados y mints cuyo TTL de deduplicación terminó."""
    now = utc_now()
    seen_cutoff = now - dt.timedelta(minutes=max(_SEEN_TTL_MIN, 0.0))
    expired_seen = [address for address, seen_at in _seen.items() if seen_at < seen_cutoff]
    for address in expired_seen:
        _seen.pop(address, None)

    if not _buffer:
        _pending.clear()
        return 0, len(expired_seen)

    fresh = [token for token in _buffer if _within_window(token, now=now)]
    expired_events = len(_buffer) - len(fresh)
    if expired_events:
        _buffer.clear()
        _buffer.extend(fresh)
    _pending.clear()
    _pending.update(str(token.get("address") or "") for token in fresh)
    return expired_events, len(expired_seen)


def _enqueue_event(token: Dict[str, Any]) -> bool:
    """Añade un evento al final de la cola respetando deduplicación y capacidad."""
    _prune_queue_state()
    address = str(token.get("address") or "").strip()
    if not address or address in _seen or address in _pending:
        return False

    token = deepcopy(token)
    token["address"] = address
    if token.get("discovered_at") is None:
        token["discovered_at"] = utc_now()
    if not _within_window(token):
        return False
    capacity = max(_BUFFER_MAX, 1)
    if len(_buffer) >= capacity:
        dropped = _buffer.popleft()
        _pending.discard(str(dropped.get("address") or ""))
        log.warning(
            "[PumpFun] cola llena (capacidad=%d); descartado el evento FIFO más antiguo: %s",
            capacity,
            dropped.get("address"),
        )
    _buffer.append(token)
    _pending.add(address)
    return True


def _drain_events(limit: int) -> List[Dict[str, Any]]:
    """Consume en orden FIFO hasta ``limit`` eventos frescos, sin repetirlos."""
    _prune_queue_state()
    out: List[Dict[str, Any]] = []
    for _ in range(max(int(limit), 0)):
        if not _buffer:
            break
        token = _buffer.popleft()
        address = str(token.get("address") or "")
        _pending.discard(address)
        _seen[address] = utc_now()
        out.append(token)
    return out


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


def _extract_clock(d: Dict[str, Any], *keys: str) -> Any:
    """Preserve an explicitly supplied invalid/zero clock, not a later alias."""
    for payload in (d, d.get("data")):
        if isinstance(payload, dict):
            for key in keys:
                if key in payload:
                    return payload[key]
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

        now = utc_now()
        ts = _to_dt(_extract_clock(msg, "created_at", "createdAt"))
        event_at = _to_dt(_extract_clock(msg, "timestamp", "ts", "time"))
        if ts is not None and ts > now:
            ts = None
        if event_at is not None and event_at > now:
            event_at = None

        creator = _extract_first(msg, "creator", "user", "owner", "signer") or ""

        age_minutes = (now - ts).total_seconds() / 60.0 if ts is not None else None

        tok = {
            "address": mint,
            "symbol": (symbol or "NEW")[:16],
            "name": name or "",
            "created_at": ts,
            "discovered_at": now,
            "pumpportal_event_at": event_at,
            "pumpportal_created_at_basis": "provider_created_at" if ts is not None else "unknown",
            "fetched_at": now,

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
        clean = sanitize_token_data(tok)
        # Keep the age at the original client receipt, not a second sanitizer
        # clock. Generic event timestamps and receipt time are never birth.
        clean["age_minutes"] = clean["age_min"] = age_minutes
        return clean
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
                                _enqueue_event(parsed)

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
async def stop_background_tasks() -> None:
    """Drain the one owned WebSocket before runtime stopped publication.

    Discovery callers must already be drained by their runtime supervisor.
    Retain pending/delivered original records; stopping is not queue reset.
    A later explicit runtime start can create a new consumer.
    """
    global _ws_task
    cancelled = False
    async with _ws_lock:
        task = _ws_task
        if task is not None:
            if not task.done():
                task.cancel()
            drained = asyncio.gather(task, return_exceptions=True)
            while not drained.done():
                try:
                    await asyncio.shield(drained)
                except asyncio.CancelledError:
                    # Repeated caller cancellation must not interrupt the
                    # socket/session's already owned asynchronous cleanup.
                    cancelled = True
            if _ws_task is task:
                _ws_task = None
        _started.clear()
    if cancelled:
        raise asyncio.CancelledError


async def get_latest_pumpfun() -> List[Dict[str, Any]]:
    """
    Devuelve hasta `_LIMIT_RETURN` tokens recientes descubiertos en Pump.fun.
    No realiza llamadas HTTP por petición; lee de un buffer alimentado por WS.
    """
    # inicia el stream si hace falta
    await _ensure_started()

    out = _drain_events(_LIMIT_RETURN)
    log.debug("[PumpFun] consumidos %d (pendientes=%d)", len(out), len(_buffer))
    return out
