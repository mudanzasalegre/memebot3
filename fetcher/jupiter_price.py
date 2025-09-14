# fetcher/jupiter_price.py
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

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
from utils.solana_addr import normalize_mint  # saneo de mints ('pump' y validación SPL)

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
# Atajos de precio instantáneo (para subir hit-rate y ahorrar cupo)
# ────────────────────────────────────────────────────────────────────────────────
# WSOL (Wrapped SOL) – normalmente no queremos pedirlo aquí (lo cotiza todo lo demás)
_WSPL_SOL_MINT = "So11111111111111111111111111111111111111112"

# Stables más comunes en Solana mainnet
# (Se pueden añadir más vía env si quieres, pero estos dos cubren la práctica totalidad)
_KNOWN_STABLES: Dict[str, float] = {
    # USDC (Circle)
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 1.0,
    # USDT (Tether)
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": 1.0,
}

# Sentinela para “sáltalo, no cuentes miss ni hagas fetch”
_FAST_SKIP = object()

# ──────────────────────────── Tipos enriquecidos ───────────────────────────────
Status = str  # Literal["OK", "NIL", "ERR"] (evitamos Literal por compatibilidad py<3.8)

@dataclass(frozen=True)
class PriceInfo:
    """Salida enriquecida: status + precio + info de ruta."""
    status: Status           # "OK" | "NIL" | "ERR"
    price_usd: Optional[float]
    has_route: bool          # True si hay precio real (asumimos ruta ejecutable)
    routes_count: int        # Sin quote real: 1 si OK, 0 si NIL/ERR

    @property
    def ok(self) -> bool:
        return self.status == "OK"


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
        # UA explícito: a veces mejora la aceptación en algunos proxies/CDNs
        _SESSION = aiohttp.ClientSession(
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": os.getenv("JUPITER_UA", "MemeBot3/1.0 (+bot)")},
        )
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
    # Aviso cuando llegamos al tope de backoff → probablemente no listado aún en Jupiter
    if _nil_backoff.get(mint) == JUPITER_TTL_NIL_MAX:
        logger.warning("[jupiter_price] Token %s sin precio tras varios intentos – posiblemente no soportado aún", _fmt_id(mint))


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


# ───────────────────────────────── HTTP (batch, crudo) ────────────────────────
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
        # Estructuras posibles:
        #   • {"data": { "<mint>": { "usdPrice": 1.23, ... }, ... }}
        #   • { "<mint>": { "usdPrice": 1.23, ... }, ... }
        payload = data.get("data", data)  # ← soporta ambas variantes
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


async def _fetch_batch_with_status(mints: List[str]) -> Dict[str, Tuple[Status, Optional[float]]]:
    """
    Igual que _fetch_batch, pero distinguiendo NIL vs ERR a nivel de batch.
    • Si la respuesta HTTP es 200: los que no vengan en payload → "NIL".
    • Si hay error HTTP/timeout/etc: todos los mints del chunk → "ERR".
    """
    if not mints:
        return {}

    # Validación light
    for m in mints:
        if not _is_probably_mint(m):
            logger.warning("[jupiter_price] ID no parece mint SPL: %s", _fmt_id(m))

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("[jupiter_price] solicitando batch de %d mints", len(mints))
        if _VERBOSE:
            logger.debug("[jupiter_price] mints: %s", ", ".join(_fmt_id(m) for m in mints))

    ids = quote(",".join(mints), safe=",")
    url = f"{JUPITER_PRICE_URL}?ids={ids}"

    await _throttle()
    sess = await _ensure_session()

    # Por defecto marcamos todo como ERR; lo iremos corrigiendo
    err_default: Dict[str, Tuple[Status, Optional[float]]] = {m: ("ERR", None) for m in mints}

    try:
        async with sess.get(url) as resp:
            if resp.status == 429:
                logger.warning("[jupiter_price] 429 Too Many Requests; backing off…")
                await asyncio.sleep(max(1.0, _MIN_DELAY_S * 2))
                return await _fetch_batch_with_status(mints)

            if resp.status >= 500:
                logger.warning("[jupiter_price] %s → %s", JUPITER_PRICE_URL, resp.status)
                return err_default

            if resp.status != 200:
                logger.debug("[jupiter_price] Non-200 (%s) para %s", resp.status, JUPITER_PRICE_URL)
                return err_default

            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.debug("[jupiter_price] HTTP error para %s -> %s", JUPITER_PRICE_URL, e)
        return err_default
    except Exception as e:
        logger.exception("[jupiter_price] Unexpected error parsing response: %s", e)
        return err_default

    # Si llegamos aquí, la respuesta es 200 → NIL/OK según payload
    out: Dict[str, Tuple[Status, Optional[float]]] = {m: ("NIL", None) for m in mints}
    found = 0
    try:
        payload = data.get("data", data)
        for m in mints:
            entry = payload.get(m)
            if not entry:
                continue
            val = entry.get("usdPrice", entry.get("price"))
            parsed: Optional[float] = None
            if isinstance(val, (int, float)):
                parsed = float(val)
            else:
                try:
                    parsed = float(val)
                except Exception:
                    parsed = None
            if parsed is not None:
                out[m] = ("OK", parsed)
                found += 1
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


# ───────────────────────────── API enriquecida (batch) ─────────────────────────
async def get_many_prices(mints: List[str]) -> Dict[str, PriceInfo]:
    """
    Devuelve dict mint -> PriceInfo(status, price_usd, has_route, routes_count).
    Reglas:
      • status ∈ {"OK","NIL","ERR"}
      • has_route=True iff status=="OK" (precio real → asumimos ruta ejecutable)
      • routes_count=1 si OK, si no 0 (no hay quote de rutas reales aquí)
    Mantiene los logs «batch OK: X precios».
    """
    _log_boot_if_needed()

    if not mints:
        return {}

    # 0) Normaliza entradas y dedup
    mints = _normalize_incoming_list(mints)
    if not mints:
        return {}

    # 0.5) Atajos instantáneos (estables) y WSOL skip
    result: Dict[str, PriceInfo] = {}
    filtered: List[str] = []
    instant_ok = 0
    instant_skips = 0
    for m in mints:
        # WSOL → skip “rápido”
        if m == _WSPL_SOL_MINT:
            instant_skips += 1
            continue
        fp = _KNOWN_STABLES.get(m)
        if fp is not None:
            # Mete en caché OK y resultado enriquecido
            _cache_set_ok(m, float(fp))
            result[m] = PriceInfo(status="OK", price_usd=float(fp), has_route=True, routes_count=1)
            instant_ok += 1
            continue
        filtered.append(m)

    mints = filtered
    if not mints:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[jupiter_price] solo atajos: OK_inst=%d, skips=%d",
                instant_ok, instant_skips
            )
        return result

    # 1) resolver por caché
    misses: List[str] = []
    cache_hits_ok = 0
    cache_hits_nil = 0

    for m in mints:
        hit = _cache_get_ok(m)
        if hit is not None:
            result[m] = PriceInfo(status="OK", price_usd=hit, has_route=True, routes_count=1)
            cache_hits_ok += 1
            continue
        if _cache_get_nil(m):
            result[m] = PriceInfo(status="NIL", price_usd=None, has_route=False, routes_count=0)
            cache_hits_nil += 1
            continue
        misses.append(m)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[jupiter_price] cache: OK=%d NIL=%d MISS=%d (total=%d, instOK=%d, skipWSOL=%d)",
            cache_hits_ok, cache_hits_nil, len(misses), len(mints), instant_ok, instant_skips,
        )

    # 2) fetch para misses en chunks de 50 (con distinción NIL/ERR)
    fetched_ok = 0
    nil_hits = cache_hits_nil
    for i in range(0, len(misses), _BATCH_MAX):
        chunk = misses[i : i + _BATCH_MAX]
        if not chunk:
            continue
        fetched = await _fetch_batch_with_status(chunk)
        for mint, (st, price) in fetched.items():
            if st == "OK" and price is not None:
                _cache_set_ok(mint, price)
                result[mint] = PriceInfo(status="OK", price_usd=price, has_route=True, routes_count=1)
                fetched_ok += 1
            elif st == "NIL":
                _cache_set_nil(mint)
                result[mint] = PriceInfo(status="NIL", price_usd=None, has_route=False, routes_count=0)
                nil_hits += 1
            else:  # "ERR"
                # No cacheamos errores transitorios; devolvemos ERR
                result[mint] = PriceInfo(status="ERR", price_usd=None, has_route=False, routes_count=0)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[jupiter_price] resultado: total_ok=%d (instOK=%d, cacheOK=%d, fetchedOK=%d) | misses_restantes=%d | nil_hits=%d",
            sum(1 for v in result.values() if v.status == "OK"),
            instant_ok,
            cache_hits_ok,
            fetched_ok,
            max(0, len(misses) - fetched_ok),
            nil_hits,
        )

    return result


# ───────────────────────────── API enriquecida (unitario) ──────────────────────
async def get_price(mint: str) -> PriceInfo:
    """
    Devuelve PriceInfo(status, price_usd, has_route, routes_count) para un mint.
    Reglas:
      • status ∈ {"OK","NIL","ERR"}
      • has_route=True iff status=="OK"
      • routes_count=1 si OK, si no 0
    """
    _log_boot_if_needed()

    if not mint:
        return PriceInfo(status="NIL", price_usd=None, has_route=False, routes_count=0)

    nm = normalize_mint(mint)
    if not nm:
        logger.debug("[jupiter_price] descartado unitario (no mint SPL): %r", mint)
        return PriceInfo(status="NIL", price_usd=None, has_route=False, routes_count=0)

    # Atajo estables
    fp = _KNOWN_STABLES.get(nm)
    if fp is not None:
        _cache_set_ok(nm, float(fp))
        return PriceInfo(status="OK", price_usd=float(fp), has_route=True, routes_count=1)

    # WSOL → skip
    if nm == _WSPL_SOL_MINT:
        return PriceInfo(status="NIL", price_usd=None, has_route=False, routes_count=0)

    # 1) caché
    hit = _cache_get_ok(nm)
    if hit is not None:
        return PriceInfo(status="OK", price_usd=hit, has_route=True, routes_count=1)
    if _cache_get_nil(nm):
        return PriceInfo(status="NIL", price_usd=None, has_route=False, routes_count=0)

    # 2) fetch (vía batch enriquecido)
    if logger.isEnabledFor(logging.DEBUG):
        if nm != mint:
            logger.debug("[jupiter_price] miss unitario → normalizado %r → %s", mint, _fmt_id(nm))
        else:
            logger.debug("[jupiter_price] miss unitario → solicitando %s vía batch", _fmt_id(nm))

    fetched = await get_many_prices([nm])
    return fetched.get(nm, PriceInfo(status="ERR", price_usd=None, has_route=False, routes_count=0))


# ──────────────────────────── API legacy (compat) ──────────────────────────────
async def get_many_usd_prices(mints: List[str]) -> Dict[str, float]:
    """
    **Compat**: mantiene la firma original devolviendo sólo precios OK.
    Internamente usa la versión enriquecida y filtra por status=="OK".
    """
    enriched = await get_many_prices(mints)
    return {m: pi.price_usd for m, pi in enriched.items() if pi.status == "OK" and pi.price_usd is not None}


async def get_usd_price(mint: str) -> Optional[float]:
    """
    **Compat**: mantiene la firma original.
    Devuelve price_usd si status=="OK", si no None.
    """
    pi = await get_price(mint)
    return pi.price_usd if pi.status == "OK" else None


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
    # Enriquecidos
    "PriceInfo",
    "get_price",
    "get_many_prices",
    # Legacy/compat
    "get_usd_price",
    "get_many_usd_prices",
    # Utils
    "clear_caches",
    "aclose",
]
