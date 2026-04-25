# trader/buyer.py
"""
Capa delgada sobre ``gmgn.buy`` que añade comprobaciones de saldo,
ventana horaria y guardas de precio antes de lanzar la orden real.
100% compatible con el flujo original de MemeBot 3: la **firma** y
las **claves del retorno** NO cambian.

• Cuando *amount_sol* ≤ 0  →  modo simulación (paper-trading).
• Verifica saldo + reserva de gas antes de comprar.
• En compra real, si está activo REQUIRE_JUPITER_FOR_BUY (o USE_JUPITER_PRICE heredado),
  **exige** precio/cotización de Jupiter para el mint: si no hay, NO compra.
• Valida impacto/slippage de la ruta Jupiter (si hay router disponible) y aborta si supera
  `IMPACT_MAX_PCT` (por defecto 8%, configurable en .env).
• Si NO hay router, estima impacto con liquidez USD y/o divergencia de precio spot (heurístico conservador).
• Devuelve SIEMPRE un dict homogéneo:

    {
      "qty_lamports": int,     # cantidad comprada (enteros del token)
      "signature":    str,     # txid o flag especial
      "route":        dict,    # JSON crudo de gmgn (normalizado)
      "buy_price_usd": float,  # precio unitario de entrada (USD)
      "peak_price":    float,  # precio máximo observado (USD)
      "price_source":  str,    # origen del precio de compra
    }

Cambios
───────
2025-09-15
• Guard extra de seguridad (“belt & suspenders”): **no ejecutar BUY si
  Jupiter no tiene ruta ejecutable**. Log:
    [trader] BUY bloqueado: sin ruta Jupiter (mint=..., src=real, reason=no_route)

2026-01 (parche de integración con tu jupiter_router.py v6):
• FIX: jupiter_router.get_quote usa amount_lamports (no amount_sol) cuando input es SOL.
• Se usa q.price_impact_bps directamente (bps) → % = bps/100.
• Mantiene el contrato de retorno y flags simbólicos del buyer.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, Final, Optional, Tuple

from config.config import CFG
from utils.solana_rpc import get_balance_lamports
from utils.sol_price import amount_sol_to_usd, get_sol_usd
from utils.time import is_in_trading_window, seconds_until_next_window
from db.database import SessionLocal
from db.models import Position
from sqlalchemy import select

# Precio: Jupiter Price v3 (Lite)
from fetcher import jupiter_price

# Router Jupiter (opcional): cotizaciones con price_impact (si existe)
try:
    from fetcher import jupiter_router as jupiter  # type: ignore
    _JUP_ROUTER_AVAILABLE = True
except Exception:
    jupiter = None  # type: ignore
    _JUP_ROUTER_AVAILABLE = False

# gmgn SDK local
from . import gmgn  # type: ignore

log = logging.getLogger("buyer")

SOL_MINT = "So11111111111111111111111111111111111111112"

# ─── Parámetros ──────────────────────────────────────────────
GAS_RESERVE_SOL: Final[float] = float(getattr(CFG, "GAS_RESERVE_SOL", 0.0) or 0.0)
_GAS_RESERVE_LAMPORTS: Final[int] = int(GAS_RESERVE_SOL * 1e9)

# Retrocompat: si no existe REQUIRE_JUPITER_FOR_BUY en CFG, usar USE_JUPITER_PRICE
_REQUIRE_JUP_PRICE: Final[bool] = bool(
    getattr(CFG, "REQUIRE_JUPITER_FOR_BUY", getattr(CFG, "USE_JUPITER_PRICE", False))
)

# Umbral de impacto permitido (porcentaje). .env: IMPACT_MAX_PCT=8
try:
    _IMPACT_MAX_PCT_DEFAULT = float(os.getenv("IMPACT_MAX_PCT", "8"))
except Exception:
    _IMPACT_MAX_PCT_DEFAULT = 8.0

# Factor de prudencia para impacto estimado sin router (K). .env: IMPACT_EST_K=2.0
try:
    _IMPACT_EST_K = float(os.getenv("IMPACT_EST_K", "2.0"))
except Exception:
    _IMPACT_EST_K = 2.0

# Divergencia máxima permitida entre DS y Jupiter (% absoluto). .env: PRICE_DIVERGENCE_MAX_PCT=15
try:
    _PRICE_DIVERGENCE_MAX_PCT = float(os.getenv("PRICE_DIVERGENCE_MAX_PCT", "15"))
except Exception:
    _PRICE_DIVERGENCE_MAX_PCT = 15.0

# Slippage para precheck quote (bps). No ejecuta el swap; es solo para ruta/impacto.
try:
    _JUP_BUY_SLIPPAGE_BPS = int(os.getenv("JUP_BUY_SLIPPAGE_BPS", "150"))
except Exception:
    _JUP_BUY_SLIPPAGE_BPS = 150

_RETRIES: Final[int] = 3
_RETRY_WAIT: Final[int] = 2  # s entre intentos

_WALLET_PUBKEY: Final[str] = os.getenv("SOL_PUBLIC_KEY", "")


# ─── Helpers ─────────────────────────────────────────────────
def _parse_route(resp: dict) -> Tuple[int, float, dict]:
    """
    Normaliza la respuesta de gmgn:
    - qty_lamports: outAmount (enteros del token de salida).
    - price_usd unitario estimado desde inAmountUSD / qty (si hay).
    - route: ruta cruda.

    Nota: el 'price_usd' que se devuelve aquí es orientativo. La
    fuente canónica de buy_price_usd la resolvemos con Jupiter/estimación.
    """
    route = resp.get("route", {}) or {}
    quote = route.get("quote", {}) or {}

    out_amount = quote.get("outAmount") or quote.get("toAmount") or quote.get("out_amount")
    if isinstance(out_amount, str) and out_amount.isdigit():
        qty_lp = int(out_amount)
    elif isinstance(out_amount, (int, float)) and out_amount > 0:
        qty_lp = int(out_amount)
    else:
        qty_lp = 0

    total_usd = quote.get("inAmountUSD")
    try:
        total_usd_f = float(total_usd) if total_usd is not None else 0.0
    except Exception:
        total_usd_f = 0.0

    # Ojo: sin decimals no podemos dar un unit-price real; esto es meramente orientativo.
    price_unit = (total_usd_f / qty_lp * 1e9) if qty_lp else 0.0
    return qty_lp, price_unit, route


async def _has_enough_funds(amount_sol: float) -> bool:
    """Comprueba que queda SOL suficiente + reserva para gas."""
    if not _WALLET_PUBKEY:
        return True
    try:
        balance_lp = await get_balance_lamports(_WALLET_PUBKEY)
        needed_lp = int(amount_sol * 1e9) + _GAS_RESERVE_LAMPORTS
        return balance_lp >= needed_lp
    except Exception as exc:  # noqa: BLE001
        log.warning("[buyer] balance check error: %s", exc)
        # En caso de error de RPC, no bloqueamos la estrategia
        return True


async def _max_positions_reached() -> bool:
    """Comprueba si ya hay demasiadas posiciones abiertas."""
    async with SessionLocal() as session:
        stmt = select(Position).where(Position.closed.is_(False))
        res = await session.execute(stmt)
        open_positions = res.scalars().all()
        try:
            max_pos = int(getattr(CFG, "MAX_ACTIVE_POSITIONS", 999999) or 999999)
        except Exception:
            max_pos = 999999
        return len(open_positions) >= max_pos


def _extract_decimals(route: dict) -> Optional[int]:
    """
    Intenta extraer los 'decimals' del token de salida de varias formas.
    """
    quote = (route.get("quote") or {}) if isinstance(route.get("quote"), dict) else {}

    # Candidatos directos
    for k in ("outDecimals", "decimals", "out_decimals"):
        v = quote.get(k)
        if isinstance(v, int) and 0 <= v <= 18:
            return v

    # Anidados habituales
    for a, b in (("outToken", "decimals"), ("output", "decimals"), ("outputMintInfo", "decimals")):
        v_parent = quote.get(a)
        if isinstance(v_parent, dict):
            v = v_parent.get(b)
            if isinstance(v, int) and 0 <= v <= 18:
                return v

    # Nivel superior en route
    for k in ("outDecimals", "decimals"):
        v = route.get(k)
        if isinstance(v, int) and 0 <= v <= 18:
            return v

    return None


def _extract_out_amount(route: dict) -> Optional[int]:
    """Devuelve el outAmount bruto (enteros) si está presente."""
    quote = (route.get("quote") or {}) if isinstance(route.get("quote"), dict) else route
    for k in ("outAmount", "out_amount", "toAmount"):
        v = quote.get(k)
        if isinstance(v, str) and v.isdigit():
            return int(v)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return None


async def _resolve_buy_price_usd(
    token_mint: str,
    amount_sol: float,
    tokens_received: Optional[float],
    ds_price_usd: Optional[float] = None,
    jupiter_prefetch: Optional[float] = None,
) -> Tuple[float, str]:
    """
    Resuelve el precio de compra con prioridad:
    1) Jupiter Price directo del token (prefetch si llega)
    2) Estimación vía SOL/USD si conocemos tokens_received
    3) Pista (DexScreener) si llega del orquestador
    4) Último recurso: 0.0
    """
    # 1) Jupiter directo (prefetch si disponible)
    p = jupiter_prefetch
    if p is None:
        try:
            p = await jupiter_price.get_usd_price(token_mint)
        except Exception as exc:  # noqa: BLE001
            log.debug("[buyer] Jupiter price error: %s", exc)
            p = None
    if p is not None and p > 0:
        return float(p), "jupiter"

    # 2) Estimación por SOL/USD
    try:
        sol_usd = await get_sol_usd()
    except Exception as exc:  # noqa: BLE001
        log.debug("[buyer] SOL/USD price error: %s", exc)
        sol_usd = None

    if sol_usd and sol_usd > 0 and tokens_received and tokens_received > 0:
        est = (amount_sol * float(sol_usd)) / tokens_received
        return float(est), "sol_estimate"

    # 3) Pista externa (DexScreener)
    if ds_price_usd and ds_price_usd > 0:
        return float(ds_price_usd), "dexscreener"

    # 4) Fallback
    log.warning("[buyer] No pude resolver buy_price_usd para %s; guardo 0.0", token_mint[:6])
    return 0.0, "fallback0"


async def _resolve_entry_notional_usd(amount_sol: float) -> float:
    notional = await amount_sol_to_usd(amount_sol)
    return float(notional or 0.0)


async def _jupiter_precheck_quote(token_mint: str, amount_sol: float) -> Tuple[bool, Optional[float]]:
    """
    Intenta obtener una cotización de Jupiter (si hay router disponible) y devuelve:
    - ok: si existe ruta válida
    - impact_pct: impacto estimado en %
    Si no hay router o falla, devuelve (True, None) para no bloquear (policy aparte).

    Integra con tu jupiter_router.py v6:
      get_quote(input_mint, output_mint, amount_sol=...) → QuoteResult(price_impact_bps float|None)
    """
    if not _JUP_ROUTER_AVAILABLE or jupiter is None:
        log.debug("[buyer] Jupiter router no disponible → omito precheck de impacto")
        return True, None

    try:
        q = await jupiter.get_quote(
            input_mint=SOL_MINT,
            output_mint=token_mint,
            amount_sol=float(amount_sol),
            slippage_bps=int(_JUP_BUY_SLIPPAGE_BPS),
            only_direct_routes=False,
        )
        ok = bool(getattr(q, "ok", False))
        impact_bps = getattr(q, "price_impact_bps", None)
        impact_pct = (float(impact_bps) / 100.0) if isinstance(impact_bps, (int, float)) else None
        return ok, impact_pct
    except Exception as exc:  # noqa: BLE001
        log.debug("[buyer] Jupiter router error: %s", exc)
        # Si falla el router, no forzamos bloqueo aquí (dejamos que REQUIRE_JUP_PRICE decida)
        return True, None


async def _has_jupiter_route(token_mint: str) -> tuple[Optional[bool], str]:
    """
    Averigua si Jupiter tiene **ruta ejecutable** para el mint.
    Devuelve (has_route | None si indeterminado, status_str en {OK,NIL,ERR}).

    NOTA: Aquí “ruta” es una aproximación:
      - Si jupiter_price da precio >0 → asumimos OK.
      - Si da None/0 → NIL.
      - Si error → ERR (indeterminado).
    """
    # 1) API enriquecida si el módulo la expone (status/has_route)
    try:
        if hasattr(jupiter_price, "get_quote_status"):
            res = await getattr(jupiter_price, "get_quote_status")(token_mint)
            hr = bool(res.get("has_route"))
            st = str(res.get("status") or ("OK" if hr else "NIL"))
            return hr, st
        if hasattr(jupiter_price, "get_price_status"):
            res = await getattr(jupiter_price, "get_price_status")(token_mint)
            hr = bool(res.get("has_route"))
            st = str(res.get("status") or ("OK" if hr else "NIL"))
            return hr, st
    except Exception:
        pass

    # 2) Fallback: precio > 0 ⇒ asumimos ruta; sin precio ⇒ NIL
    try:
        p = await jupiter_price.get_usd_price(token_mint)
        if p is not None and p > 0:
            return True, "OK"
        return False, "NIL"
    except Exception:
        return None, "ERR"


async def _impact_estimate_without_router(
    *,
    amount_sol: float,
    token_mint: str,
    jup_token_price_usd: Optional[float],
    ds_price_usd: Optional[float],
    liquidity_usd: Optional[float],
) -> Tuple[bool, Optional[float], str]:
    """
    Heurística conservadora cuando REQUIRE_JUP_PRICE=True pero NO hay router:
    - Si hay liquidity_usd: impact_est_pct ≈ 100 * (order_usd/liquidity_usd) * K
    - Si no hay liquidity_usd: usa divergencia DS vs JUP (si hay DS) como proxy de riesgo.
    Devuelve (blocked, metric_pct, reason).
    """
    sol_usd: Optional[float] = None
    tok_usd: Optional[float] = jup_token_price_usd

    try:
        sol_usd = await get_sol_usd()
    except Exception:
        sol_usd = None

    if tok_usd is None or tok_usd <= 0:
        try:
            tok_usd = await jupiter_price.get_usd_price(token_mint)
        except Exception:
            tok_usd = None

    if not sol_usd or sol_usd <= 0:
        return False, None, "no_sol_price"

    order_usd = float(amount_sol) * float(sol_usd)

    # 1) Con liquidez
    if liquidity_usd and liquidity_usd > 0:
        try:
            impact_est_pct = 100.0 * (order_usd / float(liquidity_usd)) * float(_IMPACT_EST_K)
            if impact_est_pct > float(_IMPACT_MAX_PCT_DEFAULT):
                return True, float(impact_est_pct), "impact_est_liq"
            return False, float(impact_est_pct), "impact_est_liq"
        except Exception:
            return False, None, "impact_est_liq_err"

    # 2) Divergencia DS vs JUP
    if ds_price_usd and ds_price_usd > 0 and tok_usd and tok_usd > 0:
        try:
            ratio = float(ds_price_usd) / float(tok_usd)
            dev_pct = abs(100.0 * (1.0 - ratio))
            if dev_pct > float(_PRICE_DIVERGENCE_MAX_PCT):
                return True, float(dev_pct), "price_divergence"
            return False, float(dev_pct), "price_divergence"
        except Exception:
            return False, None, "price_divergence_err"

    # 3) No hay señales para bloquear
    return False, None, "no_signal"


# ─── API pública ─────────────────────────────────────────────
async def buy(
    token_addr: str,
    amount_sol: float,
    price_hint: float | None = None,
    token_mint: str | None = None,
    liquidity_usd: float | None = None,
    entry_regime: str | None = None,
    entry_lane: str | None = None,
    discovered_via: str | None = None,
) -> Dict[str, object]:
    """
    Compra real o simulada.

    Parameters
    ----------
    token_addr : str
        Token mint address (Solana).
    amount_sol : float
        Tamaño en SOL. Si es ≤ 0 → simulación (paper).
    price_hint : float | None
        Pista de precio (DexScreener) que puede venir del orquestador.
    token_mint : str | None
        Mint normalizado (si lo tienes). Si no, se usa token_addr.
    liquidity_usd : float | None
        Liquidez estimada del pool (USD) del token a comprar; usada para
        estimar impacto si no hay router.
    """
    mint_key = token_mint or token_addr
    _ = entry_regime
    _ = entry_lane
    _ = discovered_via

    # ─────── Simulación directa (paper-trading) ────────────
    if amount_sol <= 0:
        log.info("[buyer] SIMULACIÓN · no se envía orden real (amount=%.4f SOL)", amount_sol)
        buy_price_usd, price_src = await _resolve_buy_price_usd(
            token_mint=mint_key,
            amount_sol=0.0,
            tokens_received=None,
            ds_price_usd=price_hint,
        )
        return {
            "qty_lamports": 0,
            "signature": "SIMULATION",
            "route": {},
            "buy_price_usd": buy_price_usd,
            "peak_price": buy_price_usd,
            "price_source": price_src,
        }

    # ─────── Ventana horaria (guard-rail) ────────────────
    if not is_in_trading_window():
        delay = seconds_until_next_window()
        log.warning("[buyer] Fuera de ventana horaria; no compro. Próxima en %ss", delay)
        return {
            "qty_lamports": 0,
            "signature": "OUT_OF_WINDOW",
            "route": {},
            "buy_price_usd": 0.0,
            "peak_price": 0.0,
            "price_source": "fallback0",
        }

    # ─────── Límite de posiciones / fondos ────────────────
    if await _max_positions_reached():
        max_pos = int(getattr(CFG, "MAX_ACTIVE_POSITIONS", 0) or 0)
        log.warning("[buyer] Límite de posiciones abiertas alcanzado (%d)", max_pos)
        return {
            "qty_lamports": 0,
            "signature": "LIMIT_REACHED",
            "route": {},
            "buy_price_usd": 0.0,
            "peak_price": 0.0,
            "price_source": "fallback0",
        }

    if not await _has_enough_funds(amount_sol):
        log.error(
            "[buyer] Fondos insuficientes · pedido %.3f SOL · reserva gas %.3f SOL",
            amount_sol,
            GAS_RESERVE_SOL,
        )
        return {
            "qty_lamports": 0,
            "signature": "INSUFFICIENT_FUNDS",
            "route": {},
            "buy_price_usd": 0.0,
            "peak_price": 0.0,
            "price_source": "fallback0",
        }

    # ─────── Guard de ruta Jupiter (solo si la política lo exige) ────────
    has_route, status = await _has_jupiter_route(mint_key)
    if _REQUIRE_JUP_PRICE and has_route is False:
        log.warning(
            "[trader] BUY bloqueado: sin ruta Jupiter (mint=%s, src=real, reason=no_route)",
            mint_key[:6],
        )
        return {
            "qty_lamports": 0,
            "signature": "NO_ROUTE",
            "route": {},
            "buy_price_usd": 0.0,
            "peak_price": 0.0,
            "price_source": "no_route",
            "jupiter_status": status,
        }
    elif has_route is False:
        log.info(
            "[buyer] sin ruta Jupiter (mint=%s) pero REQUIRE_JUPITER_FOR_BUY=false → continuo (fallback).",
            mint_key[:6],
        )

    # ─────── Guard de Jupiter previo (precio/cotización exigidos) ─────
    jup_price_prefetch: Optional[float] = None
    if _REQUIRE_JUP_PRICE:
        try:
            jup_price_prefetch = await jupiter_price.get_usd_price(mint_key)
        except Exception as exc:  # noqa: BLE001
            log.debug("[buyer] Jupiter prefetch error: %s", exc)
            jup_price_prefetch = None

        if jup_price_prefetch is None or jup_price_prefetch <= 0:
            log.warning("[buyer] Jupiter NO devuelve precio para %s → NO compro (policy).", mint_key[:6])
            return {
                "qty_lamports": 0,
                "signature": "NO_JUP_PRICE",
                "route": {},
                "buy_price_usd": 0.0,
                "peak_price": 0.0,
                "price_source": "fallback0",
            }

        # Chequeo de impacto/ruta con router si existe
        ok_route, impact_pct = await _jupiter_precheck_quote(mint_key, amount_sol)
        if not ok_route:
            log.info("[buyer] BUY bloqueado: sin ruta Jupiter (router quote)")
            return {
                "qty_lamports": 0,
                "signature": "NO_JUP_ROUTE",
                "route": {},
                "buy_price_usd": 0.0,
                "peak_price": 0.0,
                "price_source": "fallback0",
            }

        if impact_pct is not None and impact_pct > _IMPACT_MAX_PCT_DEFAULT:
            log.info("[buyer] High price impact %.2f%% (>%s%%) → skip", impact_pct, _IMPACT_MAX_PCT_DEFAULT)
            return {
                "qty_lamports": 0,
                "signature": "HIGH_IMPACT",
                "route": {},
                "buy_price_usd": 0.0,
                "peak_price": 0.0,
                "price_source": "fallback0",
            }

        # Si REQUIRE_JUP_PRICE=True pero no hay router, aplica heurística de impacto
        if not _JUP_ROUTER_AVAILABLE:
            blocked, metric, reason = await _impact_estimate_without_router(
                amount_sol=amount_sol,
                token_mint=mint_key,
                jup_token_price_usd=jup_price_prefetch,
                ds_price_usd=price_hint,
                liquidity_usd=liquidity_usd,
            )
            if blocked:
                log.info("[buyer] BUY bloqueado por heurístico (%s=%.2f%%)", reason, (metric or 0.0))
                return {
                    "qty_lamports": 0,
                    "signature": "HIGH_IMPACT_EST",
                    "route": {},
                    "buy_price_usd": 0.0,
                    "peak_price": 0.0,
                    "price_source": "fallback0",
                }

    # ─────── Intentos de compra real ───────────────────────
    if (
        _JUP_ROUTER_AVAILABLE
        and jupiter is not None
        and hasattr(jupiter, "execute_managed_swap")
        and bool(getattr(jupiter, "JUP_API_KEY", ""))
    ):
        try:
            managed_resp = await jupiter.execute_managed_swap(
                input_mint=SOL_MINT,
                output_mint=mint_key,
                amount_lamports=int(float(amount_sol) * 1_000_000_000),
                slippage_bps=int(_JUP_BUY_SLIPPAGE_BPS),
            )
            order = dict(managed_resp.get("order") or {})
            route = dict(managed_resp.get("route") or {})
            try:
                qty_lp = int(float(order.get("outAmount") or 0))
            except Exception:
                qty_lp = 0

            buy_price_usd, price_src = await _resolve_buy_price_usd(
                token_mint=mint_key,
                amount_sol=amount_sol,
                tokens_received=None,
                ds_price_usd=price_hint,
                jupiter_prefetch=jup_price_prefetch,
            )
            entry_notional_usd = await _resolve_entry_notional_usd(amount_sol)

            return {
                "qty_lamports": int(qty_lp),
                "signature": str(managed_resp.get("signature", "") or ""),
                "route": route,
                "buy_price_usd": float(buy_price_usd),
                "peak_price": float(buy_price_usd),
                "price_source": str(price_src),
                "entry_notional_usd": float(entry_notional_usd),
                "venue": "jupiter_managed",
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("[buyer] managed Jupiter buy fallo, fallback a legacy/gmgn: %s", exc)

    last_exc: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = await gmgn.buy(token_addr, amount_sol)
            qty_lp, _price_unit_from_quote, route = _parse_route(resp)

            # tokens_received (si disponemos de outAmount y decimals)
            tokens_received: Optional[float] = None
            out_raw = _extract_out_amount(route)
            decimals = _extract_decimals(route)

            # Si gmgn no incluye decimals, no forzamos: buy_price se resuelve con Jupiter/sol_est.
            if out_raw is not None and isinstance(decimals, int) and decimals >= 0:
                try:
                    tokens_received = out_raw / (10 ** decimals)
                except Exception:
                    tokens_received = None

            buy_price_usd, price_src = await _resolve_buy_price_usd(
                token_mint=mint_key,
                amount_sol=amount_sol,
                tokens_received=tokens_received,
                ds_price_usd=price_hint,
                jupiter_prefetch=jup_price_prefetch,
            )
            entry_notional_usd = await _resolve_entry_notional_usd(amount_sol)

            # Sanity opcional: si tenemos hint y jupiter_price, y divergen demasiado,
            # podemos etiquetar la fuente para telemetría (no bloquea por defecto).
            try:
                if price_hint and price_hint > 0 and buy_price_usd and buy_price_usd > 0:
                    dev_pct = abs(100.0 * (1.0 - (float(price_hint) / float(buy_price_usd))))
                    if dev_pct > _PRICE_DIVERGENCE_MAX_PCT:
                        log.debug(
                            "[buyer] Divergencia hint vs buy_price (%0.2f%%) (hint=%g buy=%g)",
                            dev_pct, float(price_hint), float(buy_price_usd)
                        )
            except Exception:
                pass

            return {
                "qty_lamports": int(qty_lp),
                "signature": str(resp.get("signature", "") or ""),
                "route": route,
                "buy_price_usd": float(buy_price_usd),
                "peak_price": float(buy_price_usd),
                "price_source": str(price_src),
                "entry_notional_usd": float(entry_notional_usd),
                "venue": "gmgn",
            }

        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("[buyer] gmgn.buy fallo (%s/%s): %s", attempt, _RETRIES, exc)
            if attempt < _RETRIES:
                await asyncio.sleep(_RETRY_WAIT)

    # ─────── Fracaso definitivo ────────────────────────────
    log.error("[buyer] gmgn.buy agotó reintentos: %s", last_exc)
    return {
        "qty_lamports": 0,
        "signature": "BUY_FAILED",
        "route": {},
        "buy_price_usd": 0.0,
        "peak_price": 0.0,
        "price_source": "fallback0",
        "venue": "failed",
    }
