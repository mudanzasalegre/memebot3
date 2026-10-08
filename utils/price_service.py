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
from copy import deepcopy
from typing import Any, Dict, Optional, Tuple

from utils.simple_cache import cache_get, cache_set, cache_delete
from utils.market_observation import (
    MARKET_FIELDS, OBSERVATION_VERSION,
    market_number, fresh_market_value, retain_fresh_market_fields,
    stamp_market_observation,
)
from utils.sol_price import get_sol_usd
from utils.solana_addr import normalize_mint
from analytics.social_signal import checked_social_receipt

# Adapters
from fetcher.geckoterminal import (
    get_token_data_async as get_gt_data_async,
    USE_GECKO_TERMINAL,
)
from fetcher import birdeye
from fetcher import dexscreener

try:
    from analytics.api_budget import provider_status as _provider_status
except Exception:  # pragma: no cover
    _provider_status = None  # type: ignore

# Jupiter Price (Lite)
try:
    from fetcher.jupiter_price import get_usd_price as _jup_get_usd_price  # type: ignore
    from fetcher.jupiter_price import get_price as _jup_get_price_info
except Exception:  # pragma: no cover
    _jup_get_usd_price = None
    _jup_get_price_info = None

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
_REQUIRED_FOR_LIQUIDITY: Tuple[str, ...] = ("liquidity_usd",)
# These are collection goals, not mandatory admission requirements. Optional
# gaps remain None and existing lane-specific gates decide their usability.
_ENTRY_COLLECTION_FIELDS: Tuple[str, ...] = (
    "price_usd", "liquidity_usd", "market_cap_usd", "volume_24h_usd",
    "txns_last_5m", "txns_last_5m_buys", "txns_last_5m_sells", "price_pct_5m",
)


# ─────────────────────────────────── utils ────────────────────────────────────
def _f(x):
    """Convierte a float o devuelve None si no es convertible."""
    try:
        if isinstance(x, bool):
            return None
        number = float(x)
        return number if math.isfinite(number) else None
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

    t = deepcopy(tick)

    # Precio USD (varía entre adapters)
    t["price_usd"] = _f(t.get("price_usd") if "price_usd" in t else t.get("priceUsd"))

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
    t["market_cap_usd"] = _f(t.get("market_cap_usd") if "market_cap_usd" in t else t.get("fdv", t.get("mcap")))

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
    if isinstance(val, bool):
        return True
    try:
        number = float(val)
        return not math.isfinite(number) or number <= 0
    except (TypeError, ValueError, OverflowError):
        return True


def _needs_fields(tok: Dict[str, Any] | None, fields: Tuple[str, ...]) -> bool:
    """True si faltan *cualesquiera* de los campos pedidos."""
    if not tok:
        return True
    return any(market_number(tok.get(k), k) is None for k in fields)


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
        if market_number(tok.get(key), key) is not None:
            return True
    return False


def _infer_price_source(tok: Dict[str, Any] | None) -> str | None:
    if not isinstance(tok, dict):
        return None
    source = tok.get("price_source") or tok.get("source")
    if isinstance(source, str) and source.strip():
        return source.strip().lower()
    if tok.get("dexId") or tok.get("dex_id") or tok.get("pair_address") or tok.get("pairAddress"):
        return "dexscreener"
    return None


def _source_degraded(source: str | None) -> bool:
    if _provider_status is None or not source:
        return False
    try:
        return bool(_provider_status(source).get("degraded"))
    except Exception:
        return False


def price_confidence_from_source(
    source: str | None,
    price_usd: Any,
    *,
    complete: bool = True,
    provider_degraded: bool = False,
) -> str:
    if _is_missing(price_usd):
        return "none"
    if provider_degraded:
        return "degraded"
    source_norm = str(source or "").strip().lower()
    if source_norm == "jupiter":
        return "high" if complete else "partial"
    if source_norm in {"birdeye", "dexscreener", "gecko", "geckoterminal"}:
        return "medium" if complete else "partial"
    return "low" if complete else "partial"


def _stamp_price_confidence(
    tok: Dict[str, Any] | None,
    *,
    address: str,
    fields_needed: Tuple[str, ...],
    reason: str | None = None,
) -> Dict[str, Any] | None:
    if not isinstance(tok, dict):
        return tok
    tok = _coerce_tick_numbers(dict(tok))
    tok.setdefault("address", address)
    source = _infer_price_source(tok)
    if source and not tok.get("price_source"):
        tok["price_source"] = source
    complete = not _needs_fields(tok, fields_needed)
    degraded = _source_degraded(source)
    tok["price_confidence"] = price_confidence_from_source(
        source,
        tok.get("price_usd"),
        complete=complete,
        provider_degraded=degraded,
    )
    if reason:
        tok["price_confidence_reason"] = reason
    elif _is_missing(tok.get("price_usd")):
        tok["price_confidence_reason"] = "no_price"
    elif not complete:
        tok["price_confidence_reason"] = "partial_snapshot"
    elif degraded:
        tok["price_confidence_reason"] = "provider_degraded"
    else:
        tok.setdefault("price_confidence_reason", "ok")
    tok["price_provider_degraded"] = bool(degraded)
    return tok


def build_no_price_snapshot(address: str, *, reason: str = "no_price") -> Dict[str, Any]:
    return {
        "address": address,
        "price_usd": None,
        "price_source": None,
        "price_confidence": "none",
        "price_confidence_reason": reason,
        "price_provider_degraded": False,
    }


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
    if not _is_missing(sol_usd):
        try:
            pn = float(price_native)
            su = float(sol_usd)
            tok["price_usd"] = pn * su
            tok["price_source"] = "sol_estimate"
            # No receipt proof for this derived price: the SOL leg may be cached.
            if isinstance(tok.get("market_observation"), dict):
                tok["market_observation"].get("fields", {}).pop("price_usd", None)
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


async def _query_gecko_terminal(address: str, *, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
    try:
        return await asyncio.wait_for(
            (get_gt_data_async(_CHAIN, address, timeout=_GT_TIMEOUT_S, force_refresh=True)
             if force_refresh else get_gt_data_async(_CHAIN, address, timeout=_GT_TIMEOUT_S)),
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
def _provider_tick(payload: dict | None, address: str, *, fresh: bool) -> dict | None:
    if not isinstance(payload, dict) or payload.get("address") != address:
        return None
    tick = _coerce_tick_numbers(payload)
    return retain_fresh_market_fields(tick) if fresh else tick


def _merge_market_fields(primary: dict | None, secondary: dict, source: str) -> dict:
    """Merge chosen values and their original receipt together, preserving real zero."""
    out = deepcopy(primary if primary else secondary)
    if not primary and market_number(out.get("price_usd"), "price_usd") is not None:
        out["price_source"] = source
    out.setdefault("address", secondary.get("address"))
    proof = out.get("market_observation")
    if not isinstance(proof, dict) or not isinstance(proof.get("fields"), dict):
        proof = {
            "version": OBSERVATION_VERSION, "address": out.get("address"),
            "basis": "http_response_received_not_provider_market_asof", "fields": {},
        }
        out["market_observation"] = proof
    incoming = secondary.get("market_observation", {})
    incoming_fields = incoming.get("fields", {}) if isinstance(incoming, dict) else {}
    for field in _MERGE_FIELDS + ["price_native"]:
        missing = (market_number(out.get(field), field) is None if field in MARKET_FIELDS
                   else out.get(field) is None or out.get(field) == "")
        if not missing:
            continue
        value = secondary.get(field)
        if field in MARKET_FIELDS and market_number(value, field) is None:
            continue
        if value is None:
            continue
        out[field] = deepcopy(value)
        if field == "liquidity_usd":
            # These flags describe the chosen liquidity, not a later fallback
            # whose liquidity was ignored. Move them with that field's receipt.
            for key in ("liquidity_usd_is_proxy", "liquidity_is_proxy"):
                out[key] = deepcopy(secondary.get(key))
        if field in MARKET_FIELDS:
            proof["fields"].pop(field, None)
            record = incoming_fields.get(field) if isinstance(incoming_fields, dict) else None
            if isinstance(record, dict) and incoming.get("address") == out.get("address"):
                proof["fields"][field] = deepcopy(record)
        if field == "price_usd":
            out["price_source"] = source
    address = str(out.get("address") or "")
    social = [checked_social_receipt(item.get("social_signal"), address) for item in (out, secondary)]
    social = [item for item in social if item is not None]
    if social:
        out["social_signal"] = max(social, key=lambda item: item.received_at).to_dict()
    else:
        out.pop("social_signal", None)
    return out


async def get_jupiter_price_snapshot(address: str) -> dict | None:
    """Fresh Jupiter-only price; a fallback is not evidence of a Jupiter price."""
    address = normalize_mint(address)
    if not address or not _USE_JUPITER_PRICE or _jup_get_price_info is None:
        return None
    try:
        info = await _jup_get_price_info(address, force_refresh=True)
        price = market_number(getattr(info, "price_usd", None), "price_usd")
        received = getattr(info, "received_at", None)
        if getattr(info, "status", None) != "OK" or price is None or received is None:
            return None
        tick = stamp_market_observation(
            {"address": address, "price_usd": price, "price_source": "jupiter"},
            "jupiter", received_at=received,
        )
        if fresh_market_value(tick, "price_usd", source="jupiter") is None:
            return None
        return _stamp_price_confidence(tick, address=address, fields_needed=_REQUIRED_FOR_PRICE)
    except Exception as exc:
        logger.debug("[price_service] fresh Jupiter price unavailable: %s", type(exc).__name__)
        return None


async def _query_sources(address: str, *, use_gt: bool, fields_needed: Tuple[str, ...],
                         force_refresh: bool = False, entry_snapshot: bool = False) -> Optional[Dict[str, Any]]:
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
    if "price_usd" in fields_needed and _USE_JUPITER_PRICE and _jup_get_usd_price is not None:
        try:
            fresh_jup = await get_jupiter_price_snapshot(address) if force_refresh else None
            jup_price = fresh_jup.get("price_usd") if fresh_jup else (
                None if force_refresh else await _jup_get_usd_price(address))
        except Exception as exc:
            logger.debug("[price_service] Jupiter price error: %s", exc)
            jup_price = None

        if jup_price and not _is_missing(jup_price):
            tok = fresh_jup if force_refresh else {"address": address, "price_usd": float(jup_price), "price_source": "jupiter"}
            # Intentar impacto (no bloqueante)
            if not entry_snapshot:
                tok = await _attach_jupiter_impact(tok, address)
            tok = _coerce_tick_numbers(tok)
            if not _needs_fields(tok, fields_needed):
                return _strip_non_t0_keys(tok)
        # Si Jupiter no dio precio, continuamos con las demás fuentes

    # ② Birdeye (liquidez/volumen/mcap) + relleno de price_usd si faltara
    if _USE_BIRDEYE:
        be: Dict[str, Any] | None = None
        try:
            be = (await birdeye.get_token_info(address, force_refresh=True) if force_refresh
                  else await birdeye.get_token_info(address))
            be = _provider_tick(be, address, fresh=force_refresh)
        except Exception as exc:
            logger.debug("[price_service] Birdeye error: %s", exc)
            be = None

        if be:
            logger.debug("[price_service] Merge ← Birdeye para %s…", address[:6])
            merged = _merge_market_fields(tok, be, "birdeye")
            tok = _normalize_after_merge(merged)
            if tok and not _needs_fields(tok, fields_needed):
                return _strip_non_t0_keys(tok)

    # ③ DexScreener como snapshot base/metadata
    try:
        ds = (await dexscreener.get_pair(address, force_refresh=True) if force_refresh
              else await dexscreener.get_pair(address))
        ds = _provider_tick(ds, address, fresh=force_refresh)
    except Exception as exc:
        logger.debug("[price_service] DexScreener error: %s", exc)
        ds = None

    if ds:
        logger.debug("[price_service] Merge ← DexScreener (último) para %s…", address[:6])
        merged = _merge_market_fields(tok, ds, "dexscreener")

        tok = _normalize_after_merge(merged)
        if tok and not _needs_fields(tok, fields_needed):
            return _strip_non_t0_keys(tok)

    # ④ GeckoTerminal (opcional, para completar sin perder metadata previa)
    gt_skip_key = f"price:gt_skip:{address}"
    if use_gt and USE_GECKO_TERMINAL and (force_refresh or cache_get(gt_skip_key) is None):
        gt = (await _query_gecko_terminal(address, force_refresh=True) if force_refresh
              else await _query_gecko_terminal(address))
        gt = _provider_tick(gt, address, fresh=force_refresh)

        if gt:
            logger.debug("[price_service] Merge ← GeckoTerminal para %s…", address[:6])
            merged = _merge_market_fields(tok, gt, "geckoterminal")
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
                be_pool = (await birdeye.get_pool_info(pair_address, force_refresh=True) if force_refresh
                           else await birdeye.get_pool_info(pair_address))
                be_pool = _provider_tick(be_pool, address, fresh=force_refresh)
            except Exception as exc:
                logger.debug("[price_service] Birdeye pool error: %s", exc)
                be_pool = None

            if be_pool:
                logger.debug("[price_service] Merge â† Birdeye pool para %sâ€¦", pair_address[:6])
                merged = _merge_market_fields(tok, be_pool, "birdeye")
                tok = _normalize_after_merge(merged)
                if tok and not _needs_fields(tok, fields_needed):
                    return _strip_non_t0_keys(tok)

    tok = _normalize_after_merge(tok if force_refresh else await _price_native_to_usd(tok))
    if tok and not _needs_fields(tok, fields_needed):
        logger.debug("[price_service] Fallback → native×SOL para %s…", address[:6])
        return _strip_non_t0_keys(tok)

    if use_gt and USE_GECKO_TERMINAL:
        cache_set(gt_skip_key, True, ttl=_GT_SKIP_TTL)

    # ⑥ Sin datos suficientes para los campos solicitados (puede ser dict incompleto)
    return _strip_non_t0_keys(tok)


# ───────────────────────── API principal ──────────────────────────
async def get_entry_snapshot(address: str, *, use_gt: bool = False) -> dict | None:
    """One fresh collection pass for decisions, without exploratory cache gates.

    Collect fast activity even if Jupiter/Birdeye already supplied price+liq.
    Do not repeat the entire chain merely because optional metrics are absent,
    or quote a diagnostic amount different from the eventual entry amount.
    Provider cooldowns, budget controls and hard timeouts remain in effect.
    """
    address = normalize_mint(address)
    if not address or not _is_solana_address(address):
        return None
    tick = await _query_sources(address, use_gt=use_gt, fields_needed=_ENTRY_COLLECTION_FIELDS,
                                force_refresh=True, entry_snapshot=True)
    tick = retain_fresh_market_fields(tick)
    return _strip_non_t0_keys(_stamp_price_confidence(
        tick, address=address, fields_needed=_REQUIRED_FOR_FULL,
        reason="partial_entry_snapshot" if _needs_fields(tick, _ENTRY_COLLECTION_FIELDS) else None,
    ))


async def get_price(
    address: str,
    *,
    use_gt: bool = False,
    critical: bool = False,
    price_only: bool = False,
    allow_partial: bool = False,
    force_refresh: bool = False,
    liquidity_only: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Devuelve un dict con métricas de precio/liquidez o ``None``.

    Params
    ------
    address : str
    use_gt : bool
        Permite llamar a GeckoTerminal como tercer fallback.
    critical : bool
        Si True, consulta fresca ignorando las cachés positivas y negativas.
    price_only : bool
        Si True, exige SOLO `price_usd` (cierres/compras rápidas).
        Si False, exige `price_usd` + `liquidity_usd` (validaciones).
    allow_partial : bool
        Si True, puede devolver snapshots parciales cacheados para no volver a
        golpear todas las fuentes cuando faltan campos no críticos.
    force_refresh : bool
        Ignora todas las capas de caché y exige recibos HTTP por campo de hasta
        30 segundos. No desactiva throttling ni cooldowns de los proveedores.
    liquidity_only : bool
        Exige sólo liquidez observada (cero es válido), sin consultar Jupiter.
    """
    if price_only and liquidity_only:
        raise ValueError("price_only and liquidity_only are mutually exclusive")
    force_refresh = bool(force_refresh or critical)
    norm_address = normalize_mint(address)
    if not norm_address or not _is_solana_address(norm_address):
        # cache negativo corto para no martillear (salvo en crítico)
        if not critical:
            cache_set(f"price:{address}:bad", False, ttl=_TTL_ERR)
        logger.debug("[price_service] Address no-Solana bloqueada: %r", address)
        return None
    address = norm_address

    fields_needed = (_REQUIRED_FOR_LIQUIDITY if liquidity_only else
                     _REQUIRED_FOR_PRICE if price_only else _REQUIRED_FOR_FULL)
    ck = f"price:{address}:{int(use_gt)}:{2 if liquidity_only else int(price_only)}"
    partial_ck = f"{ck}:partial"
    if force_refresh:
        cache_delete(ck)
        cache_delete(partial_ck)

    # ③(a) — Cache hit: refuerza tipos y garantiza `address`
    hit = None if force_refresh else cache_get(ck)
    if hit is not None:
        if hit is False:
            if allow_partial:
                partial_hit = cache_get(partial_ck)
                if partial_hit is not None:
                    partial_hit = _coerce_tick_numbers(partial_hit)
                    if isinstance(partial_hit, dict):
                        partial_hit.setdefault("address", address)
                    partial_hit = _stamp_price_confidence(
                        partial_hit,
                        address=address,
                        fields_needed=fields_needed,
                    )
                    return _strip_non_t0_keys(partial_hit)
            if critical:
                logger.debug("[price_service] critical=True: ignorando cache negativa para %s", address[:6])
            else:
                return None  # respetamos caché negativa en modo normal
        else:
            hit = _coerce_tick_numbers(hit)
            if isinstance(hit, dict):
                hit.setdefault("address", address)  # ← garantía de address
            hit = _stamp_price_confidence(hit, address=address, fields_needed=fields_needed)
            hit = _strip_non_t0_keys(hit)  # saneo anti claves futuras
            return hit
    elif allow_partial and not force_refresh:
        partial_hit = cache_get(partial_ck)
        if partial_hit is not None:
            partial_hit = _coerce_tick_numbers(partial_hit)
            if isinstance(partial_hit, dict):
                partial_hit.setdefault("address", address)
            partial_hit = _stamp_price_confidence(
                partial_hit,
                address=address,
                fields_needed=fields_needed,
            )
            return _strip_non_t0_keys(partial_hit)

    # Primer intento de la cadena (Jupiter primero)
    tok = (await _query_sources(address, use_gt=use_gt, fields_needed=fields_needed, force_refresh=True)
           if force_refresh else await _query_sources(address, use_gt=use_gt, fields_needed=fields_needed))
    if force_refresh:
        tok = retain_fresh_market_fields(tok)

    # ② — Garantiza `address` antes de cachear/devolver
    if tok:
        tok.setdefault("address", address)

    tok = _stamp_price_confidence(tok, address=address, fields_needed=fields_needed)
    tok = _strip_non_t0_keys(tok)  # saneo

    if tok and not _needs_fields(tok, fields_needed):
        cache_set(ck, deepcopy(tok), ttl=_TTL_OK)
        return tok

    # Reintento corto (fallos transitorios)
    if _RETRY_ON_FAIL > 0:
        try:
            import asyncio
            await asyncio.sleep(_RETRY_DELAY_S)
        except Exception:
            pass

        tok_retry = (await _query_sources(address, use_gt=use_gt, fields_needed=fields_needed, force_refresh=True)
                     if force_refresh else await _query_sources(address, use_gt=use_gt, fields_needed=fields_needed))
        if force_refresh:
            tok_retry = retain_fresh_market_fields(tok_retry)
        if tok_retry:
            tok_retry.setdefault("address", address)
        tok_retry = _stamp_price_confidence(tok_retry, address=address, fields_needed=fields_needed)
        tok_retry = _strip_non_t0_keys(tok_retry)

        if tok_retry and not _needs_fields(tok_retry, fields_needed):
            cache_set(ck, deepcopy(tok_retry), ttl=_TTL_OK)
            return tok_retry

        tok = tok_retry or tok

    # Último chequeo post-reintento
    if force_refresh:
        tok = retain_fresh_market_fields(tok)
    if tok:
        tok.setdefault("address", address)
    tok = _stamp_price_confidence(tok, address=address, fields_needed=fields_needed)
    tok = _strip_non_t0_keys(tok)

    if tok and not _needs_fields(tok, fields_needed):
        cache_set(ck, deepcopy(tok), ttl=_TTL_OK)
        return tok

    if allow_partial and _has_any_signal(tok):
        tok = _stamp_price_confidence(
            tok,
            address=address,
            fields_needed=fields_needed,
            reason="partial_snapshot",
        )
        cache_set(partial_ck, deepcopy(tok), ttl=_TTL_PARTIAL)
        return tok

    # Sin datos válidos → sólo cache negativa si NO es crítico
    if not critical:
        cache_set(ck, False, ttl=_TTL_ERR)
    if allow_partial:
        snapshot = build_no_price_snapshot(address)
        cache_set(partial_ck, snapshot, ttl=_TTL_ERR)
        return snapshot
    logger.debug(
        "[price_service] Sin datos (%s) para %s (fallback agotado; critical=%s)",
        "price_only" if price_only else "full",
        address[:6],
        critical,
    )
    return None


# ─────────────────── Helper simplificado ──────────────────────
async def get_price_usd(address: str, *, use_gt: bool = True, critical: bool = False,
                        force_refresh: bool = False) -> float | None:
    """
    Devuelve sólo ``price_usd`` (float) o ``None``.
    En cierres/compras rápidas no exigimos liquidez (price_only=True).
    En crítico se exige recepción fresca y no se escribe caché negativa.
    """
    tok = await get_price(address, use_gt=use_gt, critical=critical, price_only=True, force_refresh=force_refresh)
    return float(tok["price_usd"]) if tok and not _is_missing(tok.get("price_usd")) else None


__all__ = [
    "build_no_price_snapshot",
    "get_price",
    "get_price_usd",
    "get_jupiter_price_snapshot",
    "price_confidence_from_source",
]
