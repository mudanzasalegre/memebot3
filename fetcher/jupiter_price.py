# fetcher/jupiter_price.py
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
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
from utils.solana_addr import normalize_mint  # preserva mints válidos y sanea sufijos inválidos
from utils.market_observation import market_number
from fetcher.jupiter_price_v3 import (PriceBatch, PricePoint, decode_price_body,
                                     parse_price_payload, read_price_body, unknown_batch)

try:
    from analytics.api_budget import (
        provider_status as _provider_status,
        record_provider_event as _record_provider_event,
        reset_provider_circuits as _reset_provider_circuits,
    )
except Exception:  # pragma: no cover - keeps this fetcher importable in isolation
    _provider_status = None  # type: ignore
    _record_provider_event = None  # type: ignore
    _reset_provider_circuits = None  # type: ignore

logger = logging.getLogger("jupiter_price")

# ────────────────────────────────────────────────────────────────────────────────
# Config por entorno (con defaults seguros)
# ────────────────────────────────────────────────────────────────────────────────
from utils import jupiter_access

JUPITER_PRICE_URL: str = _CFG_URL or os.getenv("JUPITER_PRICE_URL", "https://api.jup.ag/price/v3")
try:
    JUPITER_PRICE_URL = jupiter_access.endpoint(JUPITER_PRICE_URL, "price")
except ValueError:
    pass  # Invalid optional transport is refused per request, not at paper import.

# Additional local price-only cap; the shared plan budget applies to all requests.
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
_JUP_API_KEY: str = os.getenv("JUP_API_KEY", "").strip()

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
    """Price evidence only. Routing and executable quantities require a quote."""
    status: Status           # "OK" | "NIL" | "ERR"
    price_usd: Optional[float]
    has_route: Optional[bool] = None  # Always unknown from this Price API.
    routes_count: Optional[int] = None
    confidence: str = "none"
    provider_degraded: bool = False
    received_at: Optional[float] = None  # Original client HTTP receipt, never cache-read time.
    block_id: Optional[int] = None
    decimals: Optional[int] = None
    evidence_kind: str = "unknown"
    reason: str = "unknown"
    market_asof_verified: bool = False

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
_ok_received_at: Dict[str, float] = {}
_ok_points: Dict[str, PricePoint] = {}

# Caché de negativos: mint -> expiry_monotonic
_nil_cache: Dict[str, float] = {}
_nil_received_at: Dict[str, float] = {}

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
                "[jupiter_price] Ready (gateway=Jupiter, rpm=%d, ttl_ok=%ds, ttl_nil=[%d..%ds])",
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
        headers = {"User-Agent": os.getenv("JUPITER_UA", "MemeBot3/1.0 (+bot)")}
        _SESSION = aiohttp.ClientSession(timeout=_HTTP_TIMEOUT, headers=headers)
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
    _ok_received_at.pop(mint, None)
    _ok_points.pop(mint, None)
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
    _nil_received_at.pop(mint, None)
    return False


def _cache_set_ok(mint: str, price: float, *, received_at: Optional[float] = None,
                  point: Optional[PricePoint] = None):
    _ok_cache[mint] = (price, _now() + JUPITER_TTL_OK)
    if received_at is not None:
        _ok_received_at[mint] = received_at
    else:
        _ok_received_at.pop(mint, None)  # Constant shortcuts are not market observations.
    if point is not None:
        _ok_points[mint] = point
    else:
        _ok_points.pop(mint, None)
    # Resetear estado NIL/backoff
    _nil_cache.pop(mint, None)
    _nil_received_at.pop(mint, None)
    _nil_backoff.pop(mint, None)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("[jupiter_price] cache OK set %s → %.8f (ttl=%ds)", _fmt_id(mint), price, JUPITER_TTL_OK)


def _cache_set_nil(mint: str, *, received_at: Optional[float] = None):
    _ok_cache.pop(mint, None)
    _ok_received_at.pop(mint, None)
    _ok_points.pop(mint, None)
    ttl = _nil_backoff.get(mint, JUPITER_TTL_NIL_SHORT)
    _nil_cache[mint] = _now() + ttl
    if received_at is not None:
        _nil_received_at[mint] = received_at
    else:
        _nil_received_at.pop(mint, None)
    # Backoff exponencial acotado
    _nil_backoff[mint] = min(ttl * 2, JUPITER_TTL_NIL_MAX)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("[jupiter_price] cache NIL set %s (ttl=%ds -> next=%ds)", _fmt_id(mint), ttl, _nil_backoff[mint])
    # Omission has no per-mint reason; it does not prove unsupported routing.
    if _nil_backoff.get(mint) == JUPITER_TTL_NIL_MAX:
        logger.warning("[jupiter_price] Token %s omitido del precio; motivo y rutas desconocidos", _fmt_id(mint))


def clear_caches():
    """Borra todas las cachés (útil en tests o cambios de entorno)."""
    _ok_cache.clear()
    _ok_received_at.clear()
    _ok_points.clear()
    _nil_cache.clear()
    _nil_received_at.clear()
    _nil_backoff.clear()
    if _reset_provider_circuits is not None:
        try:
            _reset_provider_circuits()
        except Exception:
            pass
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("[jupiter_price] caches borradas")


# ───────────────────────── normalización de entradas ─────────────────────────
def _jupiter_degraded() -> bool:
    if _provider_status is None:
        return False
    try:
        return bool(_provider_status("jupiter").get("degraded"))
    except Exception:
        return False


def _pi(status: Status, price_usd: Optional[float], *, received_at: Optional[float] = None,
        point: Optional[PricePoint] = None, evidence_kind: str = "unknown",
        reason: str = "unknown") -> PriceInfo:
    return PriceInfo(
        status=status,
        price_usd=price_usd,
        confidence=("assumed" if evidence_kind == "constant_shortcut" else "high")
                   if status == "OK" and price_usd is not None else "none",
        provider_degraded=_jupiter_degraded(),
        received_at=received_at,
        block_id=point.block_id if point else None,
        decimals=point.decimals if point else None,
        evidence_kind=evidence_kind,
        reason=point.reason if point else reason,
    )


def _record_rate_limit() -> None:
    if _record_provider_event is None:
        return
    try:
        _record_provider_event("jupiter", "rate_limit")
    except Exception:
        pass


def _normalize_incoming_list(mints: Iterable[str]) -> List[str]:
    """
    • Aplica normalize_mint (preserva mints válidos y sanea sufijos inválidos).
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
    """Legacy scalar mapping, using the same checked V3 transport as status reads."""
    batch = await _fetch_batch_with_status(mints)
    return {mint: value if status == "OK" else None for mint, (status, value) in batch.items()}


async def _fetch_batch_with_status(mints: List[str]) -> PriceBatch:
    """NIL only for documented omission in a valid V3 response; faults stay ERR."""
    if not mints:
        return PriceBatch({})
    checked_request = parse_price_payload({}, mints)
    if any(point.reason == "invalid_request" for point in checked_request.points.values()):
        return checked_request
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("[jupiter_price] solicitando batch de %d mints", len(mints))
    ids = quote(",".join(mints), safe=",")
    try:
        url = jupiter_access.endpoint(JUPITER_PRICE_URL, "price") + f"?ids={ids}"
        headers = jupiter_access.headers(_JUP_API_KEY, url)
        await _throttle()
        await jupiter_access.acquire(url, api_key=_JUP_API_KEY, priority=3, max_wait=6)
        sess = await _ensure_session()
        async with sess.get(url, headers=headers, allow_redirects=False) as resp:
            jupiter_access.observe(url, api_key=_JUP_API_KEY, response=resp)
            if resp.status == 429:
                _record_rate_limit()
                return unknown_batch(mints, "http_rate_limit")
            if resp.status != 200:
                return unknown_batch(mints, "http_unavailable")
            body = await read_price_body(resp)
            # Record before parsing/context teardown, not when the caller consumes it.
            received_at = time.time()
            batch = decode_price_body(body, mints, received_at=received_at)
    except (aiohttp.ClientError, asyncio.TimeoutError, jupiter_access.BudgetUnavailable) as exc:
        logger.debug("[jupiter_price] unavailable (%s)", type(exc).__name__)
        return unknown_batch(mints)
    except Exception as exc:
        logger.debug("[jupiter_price] response unavailable (%s)", type(exc).__name__)
        return unknown_batch(mints, "invalid_response")
    logger.debug("[jupiter_price] checked batch: OK=%d NIL=%d ERR=%d",
                 *(sum(point.status == status for point in batch.points.values())
                   for status in ("OK", "NIL", "ERR")))
    found = sum(point.status == "OK" for point in batch.points.values())
    if found:
        logger.info("[jupiter_price] batch OK: %d precios", found)
    return batch


def _dedup_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


# ───────────────────────────── API enriquecida (batch) ─────────────────────────
async def get_many_prices(mints: List[str], *, force_refresh: bool = False) -> Dict[str, PriceInfo]:
    """
    Devuelve dict mint -> PriceInfo con evidencia de precio, nunca de rutas.
    Reglas:
      • status ∈ {"OK","NIL","ERR"}
      • has_route y routes_count son None: un precio no prueba ejecutabilidad.
      • NIL se cachea sólo para una omisión documentada; ERR no se cachea.
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
        if fp is not None and not force_refresh:
            # Mete en caché OK y resultado enriquecido
            _cache_set_ok(m, float(fp))
            result[m] = _pi("OK", float(fp), evidence_kind="constant_shortcut", reason="fixed_stable_assumption")
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
        if force_refresh:
            _ok_cache.pop(m, None)
            _ok_received_at.pop(m, None)
            _ok_points.pop(m, None)
            _nil_cache.pop(m, None)
            _nil_received_at.pop(m, None)
        hit = None if force_refresh else _cache_get_ok(m)
        if hit is not None:
            result[m] = _pi("OK", hit, received_at=_ok_received_at.get(m), point=_ok_points.get(m),
                            evidence_kind="http_price" if m in _ok_received_at else "unknown")
            cache_hits_ok += 1
            continue
        if not force_refresh and _cache_get_nil(m):
            result[m] = _pi("NIL", None, received_at=_nil_received_at.get(m), reason="provider_omitted_price",
                            evidence_kind="http_omission" if m in _nil_received_at else "unknown")
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
        # Only our checked transport may supply original HTTP receipt/omission.
        if not isinstance(fetched, PriceBatch):
            fetched = unknown_batch(chunk, "untyped_price_evidence")
        received_at = fetched.received_at
        for mint in chunk:
            point = fetched.points.get(mint, PricePoint("ERR", reason="missing_price_evidence"))
            st, price = point.status, point.price_usd
            price = market_number(price, "price_usd")
            if st == "OK" and price is not None:
                _cache_set_ok(mint, price, received_at=received_at, point=point)
                result[mint] = _pi("OK", price, received_at=received_at, point=point,
                                   evidence_kind="http_price" if received_at is not None else "unknown")
                fetched_ok += 1
            elif st == "NIL" and point.reason == "provider_omitted_price" and received_at is not None:
                _cache_set_nil(mint, received_at=received_at)
                result[mint] = _pi("NIL", None, point=point, received_at=received_at, evidence_kind="http_omission")
                nil_hits += 1
            else:  # "ERR"
                # No cacheamos errores transitorios; devolvemos ERR
                result[mint] = _pi("ERR", None, point=point)

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
async def get_price(mint: str, *, force_refresh: bool = False) -> PriceInfo:
    """
    Devuelve PriceInfo(status, price_usd, has_route, routes_count) para un mint.
    Reglas:
      • status ∈ {"OK","NIL","ERR"}
      • has_route y routes_count son None: se necesita una quote independiente.
    """
    _log_boot_if_needed()

    if not mint:
        return _pi("ERR", None, reason="invalid_request")

    nm = normalize_mint(mint)
    if not nm:
        logger.debug("[jupiter_price] descartado unitario (no mint SPL): %r", mint)
        return _pi("ERR", None, reason="invalid_request")

    # Atajo estables
    fp = _KNOWN_STABLES.get(nm)
    if fp is not None and not force_refresh:
        _cache_set_ok(nm, float(fp))
        return _pi("OK", float(fp), evidence_kind="constant_shortcut", reason="fixed_stable_assumption")

    # WSOL → skip
    if nm == _WSPL_SOL_MINT:
        return _pi("ERR", None, reason="source_policy_skip")

    # 1) caché
    hit = None if force_refresh else _cache_get_ok(nm)
    if hit is not None:
        return _pi("OK", hit, received_at=_ok_received_at.get(nm), point=_ok_points.get(nm),
                   evidence_kind="http_price" if nm in _ok_received_at else "unknown")
    if not force_refresh and _cache_get_nil(nm):
        return _pi("NIL", None, received_at=_nil_received_at.get(nm), reason="provider_omitted_price",
                   evidence_kind="http_omission" if nm in _nil_received_at else "unknown")

    # 2) fetch (vía batch enriquecido)
    if logger.isEnabledFor(logging.DEBUG):
        if nm != mint:
            logger.debug("[jupiter_price] miss unitario → normalizado %r → %s", mint, _fmt_id(nm))
        else:
            logger.debug("[jupiter_price] miss unitario → solicitando %s vía batch", _fmt_id(nm))

    fetched = await get_many_prices([nm], force_refresh=True) if force_refresh else await get_many_prices([nm])
    return fetched.get(nm, _pi("ERR", None, reason="missing_price_evidence"))


async def get_price_status(mint: str) -> Dict[str, object]:
    pi = await get_price(mint)
    return {
        "status": pi.status,
        "price_usd": pi.price_usd,
        "has_route": pi.has_route,
        "routes_count": pi.routes_count,
        "price_confidence": pi.confidence,
        "provider_degraded": pi.provider_degraded,
        "received_at": pi.received_at,
        "block_id": pi.block_id,
        "decimals": pi.decimals,
        "evidence_kind": pi.evidence_kind,
        "reason": pi.reason,
        "market_asof_verified": pi.market_asof_verified,
    }


async def get_quote_status(mint: str) -> Dict[str, object]:
    """Deprecated price-only alias: never returns true/false routing evidence."""
    return await get_price_status(mint)


# ──────────────────────────── API legacy (compat) ──────────────────────────────
async def get_many_usd_prices(mints: List[str], *, force_refresh: bool = False) -> Dict[str, float]:
    """
    **Compat**: mantiene la firma original devolviendo sólo precios OK.
    Internamente usa la versión enriquecida y filtra por status=="OK".
    """
    enriched = await get_many_prices(mints, force_refresh=True) if force_refresh else await get_many_prices(mints)
    return {m: pi.price_usd for m, pi in enriched.items() if pi.status == "OK" and pi.price_usd is not None}


async def get_usd_price(mint: str, *, force_refresh: bool = False) -> Optional[float]:
    """
    **Compat**: mantiene la firma original.
    Devuelve price_usd si status=="OK", si no None.
    """
    pi = await get_price(mint, force_refresh=True) if force_refresh else await get_price(mint)
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
    "get_price_status",
    "get_quote_status",
    # Legacy/compat
    "get_usd_price",
    "get_many_usd_prices",
    # Utils
    "clear_caches",
    "aclose",
]
