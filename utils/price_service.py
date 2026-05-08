# memebot3/utils/price_service.py
"""
Capa de obtención de precio/liquidez con *fallback* controlado y conversión a USD.

Orden de fuentes (2025-08):
1. Jupiter Price v3 (Lite) → **fuente primaria de price_usd**.
   • Opcional: si hay router (jupiter_router), se expone price_impact/slippage.
2. Birdeye (si está activado en .env) para **liquidez/volumen/mcap** y relleno.
3. GeckoTerminal (si use_gt=True) para huecos restantes.
4. DexScreener como **último recurso** (relleno/visual), NO como primaria.
5. Conversión: price_native × SOL_USD si sigue faltando price_usd.

Extras:
• Reintento corto de toda la cadena ante fallo transitorio.
• Cacheo de aciertos y fallos (TTL configurable vía .env DEXS_TTL_NIL).
• Bloqueo de direcciones no Solana (0x…).
• Modo “solo precio”: acepta sólo price_usd (evita caer a fallback del buy_price).
• Si hay router Jupiter, añade `price_impact_bps` y `price_impact_pct` al dict.
"""

from __future__ import annotations

import asyncio
import math
import os
import logging
from typing import Any, Dict, Optional, Tuple

from utils.simple_cache import cache_get, cache_set
from utils.fallback import fill_missing_fields
from utils.sol_price import get_sol_usd
from utils.solana_addr import normalize_mint

# Adapters
from fetcher.geckoterminal import (
    get_token_data_async as get_gt_data_async,
    USE_GECKO_TERMINAL,
)
from fetcher import birdeye
from fetcher import dexscreener

# Jupiter Price (Lite)
try:
    from fetcher.jupiter_price import get_usd_price as _jup_get_usd_price  # type: ignore
except Exception:  # pragma: no cover
    _jup_get_usd_price = None

# Jupiter Router (opcional) — para exponer price_impact/slippage
try:
    # Debe exponer: get_quote(input_mint, output_mint, amount_sol) -> obj con .ok y .price_impact_bps
    from fetcher import jupiter_router as _jup_router  # type: ignore
    _JUP_ROUTER_AVAILABLE = True
except Exception:  # pragma: no cover
    _jup_router = None  # type: ignore
    _JUP_ROUTER_AVAILABLE = False

logger = logging.getLogger("price_service")

# --- Saneador de claves no-T0 (futuras / de training; NO snapshots T0) ---
_NON_T0_KEYS = {
    "label", "target", "pnl_future",
    "pnl_pct", "pnl_ratio",
    "close_price_usd", "close_price",
    "effective_exit_price_usd",
    "total_pnl_pct", "total_pnl_usd",
    "outcome", "closed_at", "exit_reason",
}
_MERGE_FIELDS = [
    "address",
    "symbol",
    "name",
    "created_at",
    "pairCreatedAt",
    "pairCreatedAtMs",
    "price_usd",
    "liquidity_usd",
    "market_cap_usd",
    "volume_24h_usd",
    "txns_last_5m",
    "txns_last_5m_sells",
    "txns_last_5m_buys",
    "holders",
    "price_pct_1m",
    "price_pct_5m",
    "volume_pct_5m",
    "pair_address",
    "dexId",
]
def _strip_non_t0_keys(d: dict | None) -> dict | None:
    if not isinstance(d, dict):
        return d
    for k in list(d.keys()):
        if k in _NON_T0_KEYS:
            d.pop(k, None)
    return d

# ───────────────────────── configuración / constantes ─────────────────────────
_TTL_OK   = int(os.getenv("DEXS_TTL_OK", "30"))           # s para respuestas válidas
_TTL_ERR  = int(os.getenv("DEXS_TTL_NIL", "15"))          # s para cachear fallos
_CHAIN    = "solana"
try:
    _TTL_PARTIAL = max(_TTL_ERR, int(os.getenv("PRICE_PARTIAL_TTL_S", "180")))
except Exception:
    _TTL_PARTIAL = max(_TTL_ERR, 180)
try:
    _GT_SKIP_TTL = max(_TTL_ERR, int(os.getenv("PRICE_GT_SKIP_TTL_S", "300")))
except Exception:
    _GT_SKIP_TTL = max(_TTL_ERR, 300)

_USE_BIRDEYE    = os.getenv("USE_BIRDEYE", "true").lower() == "true"
_RETRY_ON_FAIL  = int(os.getenv("PRICE_RETRY_ON_FAIL", "1"))  # nº reintentos de la cadena
_RETRY_DELAY_S  = float(os.getenv("PRICE_RETRY_DELAY_S", "2.0"))
try:
    _GT_TIMEOUT_S = max(0.5, float(os.getenv("PRICE_GECKO_TIMEOUT_S", "4.0")))
except Exception:
    _GT_TIMEOUT_S = 4.0
try:
    _GT_HARD_TIMEOUT_S = max(1.0, float(os.getenv("PRICE_GECKO_HARD_TIMEOUT_S", "6.0")))
except Exception:
    _GT_HARD_TIMEOUT_S = 6.0

# Flags Jupiter
_USE_JUPITER_PRICE = os.getenv("USE_JUPITER_PRICE", "true").lower() == "true"
_USE_JUPITER_IMPACT = os.getenv("USE_JUPITER_IMPACT", "true").lower() == "true"
# Cantidad de SOL para la sonda de impacto (no ejecuta swap; solo quote)
try:
    _IMPACT_PROBE_SOL = float(os.getenv("IMPACT_PROBE_SOL", "0.05"))
except Exception:
    _IMPACT_PROBE_SOL = 0.05

_REQUIRED_FOR_FULL  : Tuple[str, ...] = ("price_usd", "liquidity_usd")  # validación completa
_REQUIRED_FOR_PRICE : Tuple[str, ...] = ("price_usd",)                  # solo precio (cierres)


# ─────────────────────────────────── utils ────────────────────────────────────
def _f(x):
    """Convierte a float o devuelve None si no es convertible."""
    try:
        return float(x)
    except Exception:
        return None


def _coerce_tick_numbers(tick: dict | None) -> dict:
    """
    Convierte a float los campos típicos y aplana anidados si el adapter
    devolvió estructuras como {"liquidity":{"usd":...}} o strings.
    Añade price_impact_pct si viene price_impact_bps.
    """
    if not isinstance(tick, dict):
        return {}

    t = dict(tick)

    # Precio USD (varía entre adapters)
    t["price_usd"] = _f(t.get("price_usd") or t.get("priceUsd"))

    # Liquidez USD
    liq = t.get("liquidity_usd")
    if liq is None:
        liq = (t.get("liquidity") or {}).get("usd")
    t["liquidity_usd"] = _f(liq)

    # Volumen 24h USD
    vol = t.get("volume_24h_usd")
    if vol is None:
        vol = (t.get("volume") or {}).get("h24")
    t["volume_24h_usd"] = _f(vol)

    # Market cap / FDV
    t["market_cap_usd"] = _f(t.get("market_cap_usd") or t.get("fdv") or t.get("mcap"))

    for key in (
        "txns_last_5m",
        "txns_last_5m_sells",
        "txns_last_5m_buys",
        "holders",
        "price_pct_1m",
        "price_pct_5m",
        "volume_pct_5m",
    ):
        if key in t:
            t[key] = _f(t.get(key))

    # Precio nativo: evitar dict/list
    pn = t.get("price_native")
    if isinstance(pn, (dict, list, tuple)):
        t["price_native"] = None
    else:
        t["price_native"] = _f(pn)

    # Impacto en % si venía en bps
    if "price_impact_bps" in t and t.get("price_impact_bps") is not None:
        try:
            t["price_impact_pct"] = float(t["price_impact_bps"]) / 100.0
        except Exception:
            t["price_impact_pct"] = None

    return t


def _is_missing(val: Any) -> bool:
    """True si val es None, NaN o 0."""
    if val is None:
        return True
    if isinstance(val, float) and math.isnan(val):
        return True
    return val == 0


def _needs_fields(tok: Dict[str, Any] | None, fields: Tuple[str, ...]) -> bool:
    """True si faltan *cualesquiera* de los campos pedidos."""
    if not tok:
        return True
    return any(_is_missing(tok.get(k)) for k in fields)


def _has_any_signal(tok: Dict[str, Any] | None) -> bool:
    if not tok:
        return False
    for key in (
        "price_usd",
        "liquidity_usd",
        "volume_24h_usd",
        "market_cap_usd",
        "holders",
        "txns_last_5m",
        "price_pct_1m",
        "price_pct_5m",
        "volume_pct_5m",
    ):
        if not _is_missing(tok.get(key)):
            return True
    return False


def _is_solana_address(addr: str) -> bool:
    """
    Filtro defensivo de address:
      • Descarta EVM (0x…)
      • Acepta longitudes típicas base58 (dejamos margen 30–50).
    """
    if not addr or addr.startswith("0x"):
        return False
    return 30 <= len(addr) <= 50


def _extract_pair_address(tok: Dict[str, Any] | None) -> Optional[str]:
    if not isinstance(tok, dict):
        return None
    pair = tok.get("pair_address") or tok.get("pairAddress") or tok.get("poolAddress")
    if isinstance(pair, str) and pair.strip():
        return pair.strip()
    return None


async def _price_native_to_usd(tok: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Convierte ``price_native``→``price_usd`` si procede y es seguro."""
    if not tok or not _is_missing(tok.get("price_usd")):
        return tok

    price_native = tok.get("price_native")
    if _is_missing(price_native):
        return tok

    sol_usd = await get_sol_usd()
    if sol_usd:
        try:
            pn = float(price_native)
            su = float(sol_usd)
            tok["price_usd"] = pn * su
            logger.debug(
                "[price_service] price_native %.6g × SOL_USD %.3f → price_usd %.6g",
                pn, su, tok["price_usd"],
            )
        except Exception:
            tok["price_native"] = None
    return tok


def _normalize_after_merge(tok: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Aplica coerción tras combinar fuentes (post fill_missing_fields)."""
    if tok is None:
        return None
    return _coerce_tick_numbers(tok)


async def _query_gecko_terminal(address: str) -> Optional[Dict[str, Any]]:
    try:
        return await asyncio.wait_for(
            get_gt_data_async(_CHAIN, address, timeout=_GT_TIMEOUT_S),
            timeout=_GT_HARD_TIMEOUT_S,
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "[price_service] GeckoTerminal hard-timeout %.1fs para %s",
            _GT_HARD_TIMEOUT_S,
            address[:6],
        )
    except Exception as exc:
        logger.debug("[price_service] GeckoTerminal error: %s", exc)
    return None


# ─────────── Impacto Jupiter (opcional, si router disponible) ────────────────
SOL_MINT = "So11111111111111111111111111111111111111112"

async def _attach_jupiter_impact(tok: Dict[str, Any] | None, address: str) -> Dict[str, Any] | None:
    """
    Si hay router y el impacto está habilitado, consulta una cotización de ejemplo
    para exponer `price_impact_bps` y `price_impact_pct` en el payload.
    """
    if not _USE_JUPITER_IMPACT or not _JUP_ROUTER_AVAILABLE or _jup_router is None:
        return tok
    try:
        q = await _jup_router.get_quote(input_mint=SOL_MINT, output_mint=address, amount_sol=_IMPACT_PROBE_SOL)
        if getattr(q, "ok", False):
            pib = getattr(q, "price_impact_bps", None)
            if tok is None:
                tok = {}
            tok["price_impact_bps"] = pib
            # price_impact_pct se rellenará en _coerce_tick_numbers
            tok = _coerce_tick_numbers(tok)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[price_service] Jupiter impact error: %s", exc)
    return tok


# ───────────────────── pipeline de fuentes (sin caché) ───────────────────────
async def _query_sources(address: str, *, use_gt: bool, fields_needed: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
    """
    Ejecuta la cadena de fuentes y devuelve `tok` con los campos pedidos
    en 'fields_needed' completados en la medida de lo posible. No cachea.

    Prioridad:
      1) Jupiter (price_usd) [+impact opcional]
      2) Birdeye (liq/vol/mcap y relleno)
      3) DexScreener (metadata + snapshot base)
      4) GeckoTerminal (si se permite, para completar)
      5) Conversión price_native×SOL
    """
    tok: Dict[str, Any] | None = None

    # ① Jupiter price como primaria (si está habilitado)
    if _USE_JUPITER_PRICE and _jup_get_usd_price is not None:
        try:
            jup_price = await _jup_get_usd_price(address)
        except Exception as exc:
            logger.debug("[price_service] Jupiter price error: %s", exc)
            jup_price = None

        if jup_price and not _is_missing(jup_price):
            tok = {"price_usd": float(jup_price), "price_source": "jupiter"}
            # Intentar impacto (no bloqueante)
            tok = await _attach_jupiter_impact(tok, address)
            tok = _coerce_tick_numbers(tok)
            if not _needs_fields(tok, fields_needed):
                return _strip_non_t0_keys(tok)
        # Si Jupiter no dio precio, continuamos con las demás fuentes

    # ② Birdeye (liquidez/volumen/mcap) + relleno de price_usd si faltara
    if _USE_BIRDEYE:
        be: Dict[str, Any] | None = None
        try:
            be = await birdeye.get_token_info(address)
            be = _coerce_tick_numbers(be)
        except Exception as exc:
            logger.debug("[price_service] Birdeye error: %s", exc)
            be = None

        if be:
            logger.debug("[price_service] Merge ← Birdeye para %s…", address[:6])
            merged = fill_missing_fields(tok or {}, be, _MERGE_FIELDS, treat_zero_as_missing=True)
            tok = _normalize_after_merge(merged)
            if tok and not _needs_fields(tok, fields_needed):
                return _strip_non_t0_keys(tok)

    # ③ DexScreener como snapshot base/metadata
    try:
        ds = await dexscreener.get_pair(address)
        ds = _coerce_tick_numbers(ds)
    except Exception as exc:
        logger.debug("[price_service] DexScreener error: %s", exc)
        ds = None

    if ds:
        logger.debug("[price_service] Merge ← DexScreener (último) para %s…", address[:6])
        if tok:
            # Ya hay base: solo rellenamos los huecos pedidos
            merged = fill_missing_fields(tok, ds, _MERGE_FIELDS, treat_zero_as_missing=True)
        else:
            # DexScreener es la primera fuente válida → usa TODO su payload como base
            merged = dict(ds)

        tok = _normalize_after_merge(merged)
        if tok and not _needs_fields(tok, fields_needed):
            return _strip_non_t0_keys(tok)

    # ④ GeckoTerminal (opcional, para completar sin perder metadata previa)
    gt_skip_key = f"price:gt_skip:{address}"
    if use_gt and USE_GECKO_TERMINAL and cache_get(gt_skip_key) is None:
        gt = await _query_gecko_terminal(address)
        gt = _coerce_tick_numbers(gt)

        if gt:
            logger.debug("[price_service] Merge ← GeckoTerminal para %s…", address[:6])
            merged = fill_missing_fields(tok or {}, gt, _MERGE_FIELDS, treat_zero_as_missing=True)
            tok = _normalize_after_merge(merged)
            if tok and not _needs_fields(tok, fields_needed):
                return _strip_non_t0_keys(tok)
        else:
            cache_set(gt_skip_key, True, ttl=_GT_SKIP_TTL)
    elif use_gt and USE_GECKO_TERMINAL:
        logger.debug("[price_service] GeckoTerminal skip cache activo para %s…", address[:6])

    # ⑤ Conversión price_native→USD (segura)
    if _USE_BIRDEYE:
        pair_address = _extract_pair_address(tok)
        if pair_address:
            try:
                be_pool = await birdeye.get_pool_info(pair_address)
                be_pool = _coerce_tick_numbers(be_pool)
            except Exception as exc:
                logger.debug("[price_service] Birdeye pool error: %s", exc)
                be_pool = None

            if be_pool:
                logger.debug("[price_service] Merge â† Birdeye pool para %sâ€¦", pair_address[:6])
                merged = fill_missing_fields(tok or {}, be_pool, _MERGE_FIELDS, treat_zero_as_missing=True)
                tok = _normalize_after_merge(merged)
                if tok and not _needs_fields(tok, fields_needed):
                    return _strip_non_t0_keys(tok)

    tok = _normalize_after_merge(await _price_native_to_usd(tok))
    if tok and not _needs_fields(tok, fields_needed):
        logger.debug("[price_service] Fallback → native×SOL para %s…", address[:6])
        return _strip_non_t0_keys(tok)

    if use_gt and USE_GECKO_TERMINAL:
        cache_set(gt_skip_key, True, ttl=_GT_SKIP_TTL)

    # ⑥ Sin datos suficientes para los campos solicitados (puede ser dict incompleto)
    return _strip_non_t0_keys(tok)


# ───────────────────────── API principal ──────────────────────────
async def get_price(
    address: str,
    *,
    use_gt: bool = False,
    critical: bool = False,
    price_only: bool = False,
    allow_partial: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Devuelve un dict con métricas de precio/liquidez o ``None``.

    Params
    ------
    address : str
    use_gt : bool
        Permite llamar a GeckoTerminal como tercer fallback.
    critical : bool
        Si True, ignora cache negativa y no la escribe (modo cierre).
    price_only : bool
        Si True, exige SOLO `price_usd` (cierres/compras rápidas).
        Si False, exige `price_usd` + `liquidity_usd` (validaciones).
    allow_partial : bool
        Si True, puede devolver snapshots parciales cacheados para no volver a
        golpear todas las fuentes cuando faltan campos no críticos.
    """
    norm_address = normalize_mint(address)
    if not norm_address or not _is_solana_address(norm_address):
        # cache negativo corto para no martillear (salvo en crítico)
        if not critical:
            cache_set(f"price:{address}:bad", False, ttl=_TTL_ERR)
        logger.debug("[price_service] Address no-Solana bloqueada: %r", address)
        return None
    address = norm_address

    fields_needed = _REQUIRED_FOR_PRICE if price_only else _REQUIRED_FOR_FULL
    ck = f"price:{address}:{int(use_gt)}:{int(price_only)}"
    partial_ck = f"{ck}:partial"

    # ③(a) — Cache hit: refuerza tipos y garantiza `address`
    hit = cache_get(ck)
    if hit is not None:
        if hit is False:
            if allow_partial:
                partial_hit = cache_get(partial_ck)
                if partial_hit is not None:
                    partial_hit = _coerce_tick_numbers(partial_hit)
                    if isinstance(partial_hit, dict):
                        partial_hit.setdefault("address", address)
                    return _strip_non_t0_keys(partial_hit)
            if critical:
                logger.debug("[price_service] critical=True: ignorando cache negativa para %s", address[:6])
            else:
                return None  # respetamos caché negativa en modo normal
        else:
            hit = _coerce_tick_numbers(hit)
            if isinstance(hit, dict):
                hit.setdefault("address", address)  # ← garantía de address
            hit = _strip_non_t0_keys(hit)  # saneo anti claves futuras
            return hit
    elif allow_partial:
        partial_hit = cache_get(partial_ck)
        if partial_hit is not None:
            partial_hit = _coerce_tick_numbers(partial_hit)
            if isinstance(partial_hit, dict):
                partial_hit.setdefault("address", address)
            return _strip_non_t0_keys(partial_hit)

    # Primer intento de la cadena (Jupiter primero)
    tok = await _query_sources(address, use_gt=use_gt, fields_needed=fields_needed)

    # ② — Garantiza `address` antes de cachear/devolver
    if tok:
        tok.setdefault("address", address)

    tok = _strip_non_t0_keys(tok)  # saneo

    if tok and not _needs_fields(tok, fields_needed):
        cache_set(ck, tok, ttl=_TTL_OK)
        return tok

    # Reintento corto (fallos transitorios)
    if _RETRY_ON_FAIL > 0:
        try:
            import asyncio
            await asyncio.sleep(_RETRY_DELAY_S)
        except Exception:
            pass

        tok_retry = await _query_sources(address, use_gt=use_gt, fields_needed=fields_needed)
        if tok_retry:
            tok_retry.setdefault("address", address)
        tok_retry = _strip_non_t0_keys(tok_retry)

        if tok_retry and not _needs_fields(tok_retry, fields_needed):
            cache_set(ck, tok_retry, ttl=_TTL_OK)
            return tok_retry

        tok = tok_retry or tok

    # Último chequeo post-reintento
    if tok:
        tok.setdefault("address", address)
    tok = _strip_non_t0_keys(tok)

    if tok and not _needs_fields(tok, fields_needed):
        cache_set(ck, tok, ttl=_TTL_OK)
        return tok

    if allow_partial and _has_any_signal(tok):
        cache_set(partial_ck, tok, ttl=_TTL_PARTIAL)
        return tok

    # Sin datos válidos → sólo cache negativa si NO es crítico
    if not critical:
        cache_set(ck, False, ttl=_TTL_ERR)
    logger.debug(
        "[price_service] Sin datos (%s) para %s (fallback agotado; critical=%s)",
        "price_only" if price_only else "full",
        address[:6],
        critical,
    )
    return None


# ─────────────────── Helper simplificado ──────────────────────
async def get_price_usd(address: str, *, use_gt: bool = True, critical: bool = False) -> float | None:
    """
    Devuelve sólo ``price_usd`` (float) o ``None``.
    En cierres/compras rápidas no exigimos liquidez (price_only=True).
    En crítico ignoramos caché negativa y no la escribimos.
    """
    tok = await get_price(address, use_gt=use_gt, critical=critical, price_only=True)
    return float(tok["price_usd"]) if tok and not _is_missing(tok.get("price_usd")) else None


__all__ = ["get_price", "get_price_usd"]
