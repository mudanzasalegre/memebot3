# trader/buyer.py
"""
Capa delgada sobre ``gmgn.buy`` que añade comprobaciones de saldo,
ventana horaria y guardas de precio antes de lanzar la orden real.
Conserva la firma pública y las claves del retorno; las guardas previas
deben probar la ruta y nunca convertir fallos en autorización.

• Cuando *amount_sol* ≤ 0  →  modo simulación (paper-trading).
• Verifica saldo + reserva de gas antes de comprar.
• En compra real, si está activo REQUIRE_JUPITER_FOR_BUY (o USE_JUPITER_PRICE heredado),
  **exige** precio/cotización de Jupiter para el mint: si no hay, NO compra.
• Valida una cotización Jupiter exacta cuando la política o la ejecución lo requieren y aborta si supera
  `IMPACT_MAX_PCT` (por defecto 8%, configurable en .env).
• Un router ausente o un precio aislado no prueban una ruta. No se sustituye una
  cotización exigida por un heurístico. El modo GMGN sin requisito Jupiter sigue separado.
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

2026-01 (parche de integración histórico):
• FIX: jupiter_router.get_quote usa amount_lamports (no amount_sol) cuando input es SOL.
• Se usa q.price_impact_bps directamente (bps) → % = bps/100.
• Mantiene el contrato de retorno y flags simbólicos del buyer.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Dict, Final, Optional, Tuple

from config.config import CFG
from utils.solana_rpc import get_balance_lamports
from utils.sol_price import amount_sol_to_usd, get_sol_usd
from utils.time import is_in_trading_window, seconds_until_next_window
from db.database import SessionLocal
from db.models import Position
from sqlalchemy import select
from runtime.buy_recovery import BuyOutcomeUncertain
from utils.raw_units import sol_to_lamports

# Precio: Jupiter Price v3 (Lite)
from fetcher import jupiter_price
from utils import price_service

# Router Jupiter (opcional): cotizaciones con price_impact (si existe)
try:
    from fetcher import jupiter_router as jupiter  # type: ignore
    from fetcher.jupiter_router import _checked_quote as _check_jupiter_quote
    from fetcher.jupiter_router import SwapPreparationError
    _JUP_ROUTER_AVAILABLE = True
except Exception:
    jupiter = None  # type: ignore
    _check_jupiter_quote = None
    class SwapPreparationError(RuntimeError):
        pass
    _JUP_ROUTER_AVAILABLE = False

# gmgn SDK local
from . import gmgn  # type: ignore

log = logging.getLogger("buyer")

SOL_MINT = "So11111111111111111111111111111111111111112"

# ─── Parámetros ──────────────────────────────────────────────
GAS_RESERVE_SOL: Final[float] = float(getattr(CFG, "GAS_RESERVE_SOL", 0.0) or 0.0)
_GAS_RESERVE_LAMPORTS: Final[int | None] = sol_to_lamports(GAS_RESERVE_SOL, allow_zero=True)

# Retrocompat: si no existe REQUIRE_JUPITER_FOR_BUY en CFG, usar USE_JUPITER_PRICE
_REQUIRE_JUP_PRICE: Final[bool] = bool(
    getattr(CFG, "REQUIRE_JUPITER_FOR_BUY", getattr(CFG, "USE_JUPITER_PRICE", False))
)

# Umbral de impacto permitido (porcentaje). .env: IMPACT_MAX_PCT=8
try:
    _IMPACT_MAX_PCT_DEFAULT = float(os.getenv("IMPACT_MAX_PCT", "8"))
except Exception:
    _IMPACT_MAX_PCT_DEFAULT = 8.0

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

_WALLET_PUBKEY: Final[str] = os.getenv("SOL_PUBLIC_KEY", "")


# ─── Helpers ─────────────────────────────────────────────────
def _raw_token_units(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return 0


def _validate_submission(qty: int, signature: object) -> None:
    if qty <= 0 or not str(signature or "").strip():
        raise BuyOutcomeUncertain("Submitted order has no confirmed quantity/signature")


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
    qty_lp = _raw_token_units(out_amount)

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
    if (not isinstance(amount_sol, (int, float)) or isinstance(amount_sol, bool)
            or not math.isfinite(amount_sol) or amount_sol <= 0):
        return False
    if not _WALLET_PUBKEY:
        log.warning("[buyer] live funds unknown: public wallet identity is missing")
        return False
    try:
        balance_lp = await get_balance_lamports(_WALLET_PUBKEY)
        units = sol_to_lamports(amount_sol)
        if units is None or type(_GAS_RESERVE_LAMPORTS) is not int or _GAS_RESERVE_LAMPORTS < 0:
            return False
        needed_lp = units + _GAS_RESERVE_LAMPORTS
        if not isinstance(balance_lp, int) or isinstance(balance_lp, bool) or balance_lp < 0:
            return False
        return balance_lp >= needed_lp
    except Exception as exc:  # noqa: BLE001
        log.warning("[buyer] balance check error: %s", type(exc).__name__)
        return False  # Unknown capital is not evidence that funds exist.


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
        amount = _raw_token_units(v)
        if amount > 0:
            return amount
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
    """One exact-size quote. Unknown/malformed routing is never permission."""
    units = sol_to_lamports(amount_sol)
    if (not _JUP_ROUTER_AVAILABLE or jupiter is None or _check_jupiter_quote is None
            or units is None or type(_JUP_BUY_SLIPPAGE_BPS) is not int
            or not 0 <= _JUP_BUY_SLIPPAGE_BPS <= 65535):
        return False, None

    try:
        q = await jupiter.get_quote(
            input_mint=SOL_MINT,
            output_mint=token_mint,
            amount_lamports=units,
            slippage_bps=_JUP_BUY_SLIPPAGE_BPS,
            only_direct_routes=False,
        )
        if getattr(q, "ok", False) is not True:
            return False, None
        checked = _check_jupiter_quote(getattr(q, "raw", None), input_mint=SOL_MINT,
            output_mint=token_mint, amount=units, slippage=_JUP_BUY_SLIPPAGE_BPS, direct=False)
        impact_bps = getattr(q, "price_impact_bps", None)
        if (not checked.ok or type(getattr(q, "in_amount", None)) is not int
                or type(getattr(q, "out_amount", None)) is not int
                or q.in_amount != checked.in_amount or q.out_amount != checked.out_amount
                or isinstance(impact_bps, bool) or not isinstance(impact_bps, (int, float))
                or not math.isfinite(impact_bps) or impact_bps != checked.price_impact_bps):
            return False, None
        return True, float(impact_bps) / 100.0
    except Exception as exc:  # noqa: BLE001
        log.debug("[buyer] Jupiter router unavailable: %s", type(exc).__name__)
        return False, None


async def _has_jupiter_route(token_mint: str, amount_sol: float = 0.1) -> tuple[Optional[bool], str]:
    """Compatibility probe backed by a quote, never a Price API response."""
    ok, _ = await _jupiter_precheck_quote(token_mint, amount_sol)
    return ok, "OK" if ok else "NIL"


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

    if (not isinstance(amount_sol, (int, float)) or isinstance(amount_sol, bool)
            or not math.isfinite(amount_sol)):
        return {"qty_lamports": 0, "signature": "INVALID_AMOUNT", "route": {}}
    if amount_sol > 0 and sol_to_lamports(amount_sol) is None:
        return {"qty_lamports": 0, "signature": "INVALID_AMOUNT", "route": {}}

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
            "price_confidence": price_service.price_confidence_from_source(price_src, buy_price_usd),
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

    units = sol_to_lamports(amount_sol)
    if units is None:
        return {"qty_lamports": 0, "signature": "INVALID_AMOUNT", "route": {}}
    use_managed = bool(_JUP_ROUTER_AVAILABLE and jupiter is not None
        and hasattr(jupiter, "execute_managed_swap") and getattr(jupiter, "JUP_API_KEY", "")
        and getattr(jupiter, "JUP_MANAGED_ENABLED", False) is True)
    if (_REQUIRE_JUP_PRICE or use_managed) and (
            isinstance(_IMPACT_MAX_PCT_DEFAULT, bool) or not isinstance(_IMPACT_MAX_PCT_DEFAULT, (int, float))
            or not math.isfinite(_IMPACT_MAX_PCT_DEFAULT) or _IMPACT_MAX_PCT_DEFAULT < 0):
        return {"qty_lamports": 0, "signature": "INVALID_IMPACT_LIMIT", "route": {}}
    if use_managed and (type(_GAS_RESERVE_LAMPORTS) is not int or _GAS_RESERVE_LAMPORTS < 0):
        return {"qty_lamports": 0, "signature": "INVALID_GAS_RESERVE", "route": {}}

    # ─────── Guard de Jupiter previo (precio/cotización exigidos) ─────
    jup_price_prefetch: Optional[float] = None
    if _REQUIRE_JUP_PRICE:
        try:
            jup_price_prefetch = await jupiter_price.get_usd_price(mint_key)
        except Exception as exc:  # noqa: BLE001
            log.debug("[buyer] Jupiter prefetch error: %s", exc)
            jup_price_prefetch = None

        if (isinstance(jup_price_prefetch, bool) or not isinstance(jup_price_prefetch, (int, float))
                or not math.isfinite(jup_price_prefetch) or jup_price_prefetch <= 0):
            log.warning("[buyer] Jupiter NO devuelve precio para %s → NO compro (policy).", mint_key[:6])
            return {
                "qty_lamports": 0,
                "signature": "NO_JUP_PRICE",
                "route": {},
                "buy_price_usd": 0.0,
                "peak_price": 0.0,
                "price_source": "fallback0",
            }

    # The actual managed Swap v2 order validates its own amount/impact/fees
    # before signing. Do not impose a different Metis-only route on an RFQ,
    # Dflow or OKX winner. Required legacy/GMGN Jupiter policy stays separate.
    if _REQUIRE_JUP_PRICE and not use_managed:
        ok_route, impact_pct = await _jupiter_precheck_quote(mint_key, amount_sol)
        if not ok_route or impact_pct is None:
            log.info("[buyer] BUY bloqueado: sin ruta Jupiter (router quote)")
            return {
                "qty_lamports": 0,
                "signature": "NO_JUP_ROUTE",
                "route": {},
                "buy_price_usd": 0.0,
                "peak_price": 0.0,
                "price_source": "fallback0",
            }

        if impact_pct > _IMPACT_MAX_PCT_DEFAULT:
            log.info("[buyer] High price impact %.2f%% (>%s%%) → skip", impact_pct, _IMPACT_MAX_PCT_DEFAULT)
            return {
                "qty_lamports": 0,
                "signature": "HIGH_IMPACT",
                "route": {},
                "buy_price_usd": 0.0,
                "peak_price": 0.0,
                "price_source": "fallback0",
            }

    # ─────── Intentos de compra real ───────────────────────
    if use_managed:
        try:
            managed_resp = await jupiter.execute_managed_swap(
                input_mint=SOL_MINT,
                output_mint=mint_key,
                amount_lamports=units,
                slippage_bps=_JUP_BUY_SLIPPAGE_BPS,
                max_price_impact_pct=_IMPACT_MAX_PCT_DEFAULT,
                max_wallet_fee_lamports=_GAS_RESERVE_LAMPORTS,
            )
            order = dict(managed_resp.get("order") or {})
            route = dict(managed_resp.get("route") or {})
            # The quote is not the received balance: use only checked execute
            # wallet-output units, retaining their provider-only provenance.
            qty_lp = _raw_token_units(managed_resp.get("qty_lamports"))
            _validate_submission(qty_lp, managed_resp.get("signature"))

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
                "price_confidence": price_service.price_confidence_from_source(price_src, buy_price_usd),
                "entry_notional_usd": float(entry_notional_usd),
                "venue": "jupiter_managed",
                "fill_verified": False,
            }
        except SwapPreparationError:
            return {"qty_lamports": 0, "signature": "NO_JUP_ORDER", "route": {},
                "buy_price_usd": 0.0, "peak_price": 0.0, "price_source": "fallback0"}
        except Exception as exc:  # noqa: BLE001
            # A response/transport/enrichment failure does not prove that the
            # wallet side effect was absent. Never execute another venue here.
            raise BuyOutcomeUncertain("Managed buy outcome needs reconciliation; no fallback sent") from exc

    # One submission only. Retrying an ambiguous side effect can double-buy.
    try:
        resp = await gmgn.buy(mint_key, amount_sol)
        qty_lp, _price_unit_from_quote, route = _parse_route(resp)
        _validate_submission(qty_lp, resp.get("signature"))

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
            "price_confidence": price_service.price_confidence_from_source(price_src, buy_price_usd),
            "entry_notional_usd": float(entry_notional_usd),
            "venue": "gmgn",
        }

    except Exception as exc:  # noqa: BLE001
        raise BuyOutcomeUncertain("Legacy buy outcome needs reconciliation; no retry sent") from exc
