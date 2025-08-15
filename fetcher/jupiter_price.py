# fetcher/jupiter_price.py
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, Iterable, List, Optional

try:
    from config.config import (  # type: ignore
        JUPITER_PRICE_URL as _CFG_URL,
        JUPITER_RPM as _CFG_RPM,
        JUPITER_TTL_OK as _CFG_TTL_OK,
        JUPITER_TTL_NIL_SHORT as _CFG_TTL_NIL_SHORT,
        JUPITER_TTL_NIL_MAX as _CFG_TTL_NIL_MAX,
    )
except Exception:
    _CFG_URL = None
    _CFG_RPM = None
    _CFG_TTL_OK = None
    _CFG_TTL_NIL_SHORT = None
    _CFG_TTL_NIL_MAX = None

import aiohttp
from urllib.parse import quote
from utils.solana_addr import normalize_mint  # ← saneo de mints ('pump' y validación SPL)

logger = logging.getLogger("jupiter_price")

# ────────────────────────────────────────────────────────────────────────────────
# Config por entorno (con defaults seguros)
# ────────────────────────────────────────────────────────────────────────────────
JUPITER_PRICE_URL: str = (
    _CFG_URL
    or os.getenv("JUPITER_PRICE_URL", "https://lite-api.jup.ag/price/v3")
)

# Límite Lite típico: 60 req / min → 1 req/seg
JUPITER_RPM: int = int(os.getenv("JUPITER_RPM", str(_CFG_RPM or 60)))
_MIN_DELAY_S: float = max(0.0, 60.0 / max(1, JUPITER_RPM))

# TTLs (en segundos)
JUPITER_TTL_OK: int = int(os.getenv("JUPITER_TTL_OK", str(_CFG_TTL_OK or 120)))
JUPITER_TTL_NIL_SHORT: int = int(
    os.getenv("JUPITER_TTL_NIL_SHORT", str(_CFG_TTL_NIL_SHORT or 120))
)
JUPITER_TTL_NIL_MAX: int = int(
    os.getenv("JUPITER_TTL_NIL_MAX", str(_CFG_TTL_NIL_MAX or 600))
)

# Batch máximo permitido por la API
_BATCH_MAX = 50

# Timeout HTTP
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=6.0)

# Verbose opcional (0/1)
_VERBOSE: bool = os.getenv("JUPITER_VERBOSE", "0") == "1"

# ────────────────────────────────────────────────────────────────────────────────
# Estado global: sesión HTTP, rate-limiter y cachés
# ────────────────────────────────────────────────────────────────────────────────
_SESSION: Optional[aiohttp.ClientSession] = None

# Rate limit muy simple: 1 petición cada _MIN_DELAY_S segundos
_rate_lock = asyncio.Lock()
_last_request_t = 0.0  # monotonic()

# Caché de aciertos: mint -> (price, expiry_monotonic)
_ok_cache: Dict[str, tuple[float, float]] = {}

# Caché de negativos: mint -> expiry_monotonic
_nil_cache: Dict[str, float] = {}

# Backoff NIL: mint -> ttl_nil_actual
_nil_backoff: Dict[str, int] = {}

# Banner on-demand (para que no se pierda antes de configurar logging)
_BOOT_LOGGED = False


def _now() -> float:
    return time.monotonic()


def _is_probably_mint(s: str) -> bool:
    # Mint SPL típico: Base58 ~32–44 chars (dejamos margen 30–50)
    return 30 <= len(s) <= 50 and not s.startswith("0x")


def _fmt_id(m: str) -> str:
    if not m:
        return "<empty>"
    if len(m) <= 12:
        return m
    return f"{m[:6]}…{m[-4:]}(len={len(m)})"


def _log_boot_if_needed():
    global _BOOT_LOGGED
    if not _BOOT_LOGGED:
        _BOOT_LOGGED = True
        try:
            logger.info(
                "[jupiter_price] Ready (url=%s, rpm=%d, ttl_ok=%ds, ttl_nil=[%d..%ds])",
                JUPITER_PRICE_URL,
                JUPITER_RPM,
                JUPITER_TTL_OK,
                JUPITER_TTL_NIL_SHORT,
                JUPITER_TTL_NIL_MAX,
            )
        except Exception:
            pass


async def _ensure_session() -> aiohttp.ClientSession:
    _log_boot_if_needed()
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[jupiter_price] creando nueva sesión HTTP (timeout=%ss)", _HTTP_TIMEOUT.total)
        _SESSION = aiohttp.ClientSession(timeout=_HTTP_TIMEOUT)
    return _SESSION


async def _throttle():
    """Rate limiter básico: garantiza un retraso mínimo entre peticiones."""
    global _last_request_t
    async with _rate_lock:
        now = _now()
        delta = now - _last_request_t
        if delta < _MIN_DELAY_S:
            sleep_for = _MIN_DELAY_S - delta
            if logger.isEnabledFor(logging.DEBUG) and sleep_for > 0:
                logger.debug("[jupiter_price] throttle: durmiendo %.3fs (rpm=%d)", sleep_for, JUPITER_RPM)
            await asyncio.sleep(sleep_for)
        _last_request_t = _now()


def _cache_get_ok(mint: str) -> Optional[float]:
    entry = _ok_cache.get(mint)
    if not entry:
        return None
    price, exp = entry
    if _now() <= exp:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[jupiter_price] cache OK hit para %s → %.8f (ttl restante=%.1fs)", _fmt_id(mint), price, exp - _now())
        return price
    # Expirado
    _ok_cache.pop(mint, None)
    return None


def _cache_get_nil(mint: str) -> bool:
    exp = _nil_cache.get(mint)
    if exp is None:
        return False
    if _now() <= exp:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[jupiter_price] cache NIL hit para %s (ttl restante=%.1fs)", _fmt_id(mint), exp - _now())
        return True
    # Expirado
    _nil_cache.pop(mint, None)
    return False


def _cache_set_ok(mint: str, price: float):
    _ok_cache[mint] = (price, _now() + JUPITER_TTL_OK)
    # Resetear estado NIL/backoff
    _nil_cache.pop(mint, None)
    _nil_backoff.pop(mint, None)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("[jupiter_price] cache OK set %s → %.8f (ttl=%ds)", _fmt_id(mint), price, JUPITER_TTL_OK)


def _cache_set_nil(mint: str):
    ttl = _nil_backoff.get(mint, JUPITER_TTL_NIL_SHORT)
    _nil_cache[mint] = _now() + ttl
    # Backoff exponencial acotado
    _nil_backoff[mint] = min(ttl * 2, JUPITER_TTL_NIL_MAX)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("[jupiter_price] cache NIL set %s (ttl=%ds -> next=%ds)", _fmt_id(mint), ttl, _nil_backoff[mint])


def clear_caches():
    """Borra todas las cachés (útil en tests o cambios de entorno)."""
    _ok_cache.clear()
    _nil_cache.clear()
    _nil_backoff.clear()
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("[jupiter_price] caches borradas")


# ───────────────────────── normalización de entradas ─────────────────────────
def _normalize_incoming_list(mints: Iterable[str]) -> List[str]:
    """
    • Aplica normalize_mint (quita 'pump', trim, valida).
    • Dedup preservando orden.
    • Loggea los descartes por no parecer mint SPL.
    """
    seen = set()
    out: List[str] = []
    for raw in mints:
        if not raw:
            continue
        nm = normalize_mint(raw)
        if not nm:
            logger.debug("[jupiter_price] descartado (no mint SPL): %r", raw)
            continue
        if nm not in seen:
            seen.add(nm)
            out.append(nm)
        elif logger.isEnabledFor(logging.DEBUG) and raw != nm:
            logger.debug("[jupiter_price] normalizado duplicado %r → %s (dedup)", raw, _fmt_id(nm))
    return out


# ───────────────────────────────── HTTP (batch) ──────────────────────────────
async def _fetch_batch(mints: List[str]) -> Dict[str, Optional[float]]:
    """
    Hace una llamada a Jupiter Price v3 para hasta 50 mints.
    Devuelve dict mint -> price (float) o None si no hay precio.
    """
    if not mints:
        return {}

    # Validación light (por si llegara algo raro tras la normalización)
    for m in mints:
        if not _is_probably_mint(m):
            logger.warning("[jupiter_price] ID no parece mint SPL: %s", _fmt_id(m))

    # Log de batch
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("[jupiter_price] solicitando batch de %d mints", len(mints))
        if _VERBOSE:
            logger.debug("[jupiter_price] mints: %s", ", ".join(_fmt_id(m) for m in mints))

    ids = quote(",".join(mints), safe=",")
    url = f"{JUPITER_PRICE_URL}?ids={ids}"

    await _throttle()
    sess = await _ensure_session()

    try:
        async with sess.get(url) as resp:
            if resp.status == 429:
                logger.warning("[jupiter_price] 429 Too Many Requests; backing off…")
                await asyncio.sleep(max(1.0, _MIN_DELAY_S * 2))
                return await _fetch_batch(mints)

            if resp.status >= 500:
                logger.warning("[jupiter_price] %s → %s", JUPITER_PRICE_URL, resp.status)
                return {m: None for m in mints}

            if resp.status != 200:
                logger.debug("[jupiter_price] Non-200 (%s) para %s", resp.status, JUPITER_PRICE_URL)
                return {m: None for m in mints}

            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.debug("[jupiter_price] HTTP error para %s -> %s", JUPITER_PRICE_URL, e)
        return {m: None for m in mints}
    except Exception as e:
        logger.exception("[jupiter_price] Unexpected error parsing response: %s", e)
        return {m: None for m in mints}

    out: Dict[str, Optional[float]] = {m: None for m in mints}
    found = 0
    try:
        # Estructura esperada: {"data": { "<mint>": { "usdPrice": 1.23, ... }, ... }}
        payload = data.get("data") or {}
        for m in mints:
            entry = payload.get(m)
            if not entry:
                continue
            val = entry.get("usdPrice", entry.get("price"))
            if isinstance(val, (int, float)):
                out[m] = float(val)
                found += 1
            else:
                try:
                    out[m] = float(val)
                    found += 1
                except Exception:
                    out[m] = None
    except Exception as e:
        logger.debug("[jupiter_price] Malformed payload: %s", e)

    missing = len(mints) - found
    if found and missing:
        logger.info("[jupiter_price] batch OK: %d precios, %d sin datos", found, missing)
    elif found:
        logger.info("[jupiter_price] batch OK: %d precios", found)
    else:
        logger.debug("[jupiter_price] batch sin resultados")

    return out


def _dedup_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


# ───────────────────────────── API pública (batch) ────────────────────────────
async def get_many_usd_prices(mints: List[str]) -> Dict[str, float]:
    """
    Devuelve un dict mint -> usdPrice (solo para los que se hayan podido obtener).
    Aplica caché TTL y NIL adaptativo. Se hacen llamadas solo para los misses.
    Normaliza y valida los mints de entrada (evita pedir '<mint>pump', etc.).
    """
    _log_boot_if_needed()

    if not mints:
        return {}

    # 0) Normaliza entradas y dedup
    mints = _normalize_incoming_list(mints)
    if not mints:
        return {}

    # 1) resolver por caché
    result: Dict[str, float] = {}
    misses: List[str] = []
    cache_hits_ok = 0
    cache_hits_nil = 0

    for m in mints:
        hit = _cache_get_ok(m)
        if hit is not None:
            result[m] = hit
            cache_hits_ok += 1
            continue
        if _cache_get_nil(m):
            cache_hits_nil += 1
            continue
        misses.append(m)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[jupiter_price] cache: OK=%d NIL=%d MISS=%d (total=%d)",
            cache_hits_ok, cache_hits_nil, len(misses), len(mints),
        )

    # 2) fetch para misses en chunks de 50
    fetched_total = 0
    for i in range(0, len(misses), _BATCH_MAX):
        chunk = misses[i : i + _BATCH_MAX]
        if not chunk:
            continue
        fetched = await _fetch_batch(chunk)
        for mint, price in fetched.items():
            if price is None:
                _cache_set_nil(mint)
            else:
                _cache_set_ok(mint, price)
                result[mint] = price
                fetched_total += 1

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[jupiter_price] resultado batch: %d/%d precios disponibles",
            len(result), len(mints),
        )

    # 3) Devolver solo los disponibles
    return result


# ───────────────────────────── API pública (unitario) ─────────────────────────
async def get_usd_price(mint: str) -> Optional[float]:
    """
    Devuelve el precio USD de un mint concreto o None si no disponible.
    Usa caché TTL (aciertos) y NIL adaptativo (fallos).
    Normaliza/valida el mint antes de consultar (evita '<mint>pump', etc.).
    """
    _log_boot_if_needed()

    if not mint:
        return None
    nm = normalize_mint(mint)
    if not nm:
        logger.debug("[jupiter_price] descartado unitario (no mint SPL): %r", mint)
        return None

    # 1) caché
    hit = _cache_get_ok(nm)
    if hit is not None:
        return hit
    if _cache_get_nil(nm):
        return None

    # 2) fetch (vía batch, por simplicidad)
    if logger.isEnabledFor(logging.DEBUG):
        if nm != mint:
            logger.debug("[jupiter_price] miss unitario → normalizado %r → %s", mint, _fmt_id(nm))
        else:
            logger.debug("[jupiter_price] miss unitario → solicitando %s vía batch", _fmt_id(nm))

    fetched = await get_many_usd_prices([nm])
    # IMPORTANTE: get_many_usd_prices ya setea caches OK/NIL; no dupliques aquí
    return fetched.get(nm)


# ───────────────────────────── cierre de sesión ───────────────────────────────
async def aclose():
    """Cierra la sesión HTTP (opcional; el runner puede llamarlo al apagar)."""
    global _SESSION
    if _SESSION and not _SESSION.closed:
        await _SESSION.close()
        _SESSION = None
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[jupiter_price] sesión HTTP cerrada")


__all__ = [
    "get_usd_price",
    "get_many_usd_prices",
    "clear_caches",
    "aclose",
]
