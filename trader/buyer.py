# trader/buyer.py
"""
Capa delgada sobre ``gmgn.buy`` que añade comprobaciones de saldo,
ventana horaria y guardas de precio antes de lanzar la orden real.
100% compatible con el flujo original de MemeBot 3: la **firma** y
las **claves del retorno** NO cambian.

• Cuando *amount_sol* ≤ 0  →  modo simulación (paper-trading).
• Verifica saldo + reserva de gas antes de comprar.
• En compra real, si está activo USE_JUPITER_PRICE, **exige** precio
  de Jupiter para el mint: si no hay, NO compra.
• Devuelve SIEMPRE un dict homogéneo:

    {
      "qty_lamports": int,     # cantidad comprada (enteros del token)
      "signature":    str,     # txid o flag especial
      "route":        dict,    # JSON crudo de gmgn
      "buy_price_usd": float,  # precio unitario de entrada (USD)
      "peak_price":    float,  # precio máximo observado (USD)
      "price_source":  str,    # origen del precio de compra
    }
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, Final, Optional, Tuple

from config.config import CFG
from utils.solana_rpc import get_balance_lamports
from utils.time import is_in_trading_window, seconds_until_next_window
from db.database import SessionLocal
from db.models import Position
from sqlalchemy import select

# Precio: Jupiter Price v3 (Lite)
from fetcher import jupiter_price

# gmgn SDK local
from . import gmgn  # type: ignore

log = logging.getLogger("buyer")

SOL_MINT = "So11111111111111111111111111111111111111112"

# ─── Parámetros ──────────────────────────────────────────────
GAS_RESERVE_SOL: Final[float] = CFG.GAS_RESERVE_SOL
_GAS_RESERVE_LAMPORTS: Final[int] = int(GAS_RESERVE_SOL * 1e9)

_REQUIRE_JUP_PRICE: Final[bool] = CFG.USE_JUPITER_PRICE  # exige precio Jupiter para entrar
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

    price_unit = (total_usd_f / qty_lp * 1e9) if qty_lp else 0.0  # aprox.
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
        return len(open_positions) >= CFG.MAX_ACTIVE_POSITIONS


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
    3) Hint (DexScreener) si nos llega del orquestador
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
        sol_usd = await jupiter_price.get_usd_price(SOL_MINT)
    except Exception as exc:  # noqa: BLE001
        log.debug("[buyer] Jupiter SOL price error: %s", exc)
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


# ─── API pública ─────────────────────────────────────────────
async def buy(
    token_addr: str,
    amount_sol: float,
    price_hint: float | None = None,
    token_mint: str | None = None,
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
    """
    mint_key = token_mint or token_addr

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
        log.warning("[buyer] Límite de posiciones abiertas alcanzado (%d)", CFG.MAX_ACTIVE_POSITIONS)
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

    # ─────── Guard de precio Jupiter previo (exigido) ─────
    jup_price_prefetch: Optional[float] = None
    if _REQUIRE_JUP_PRICE:
        try:
            jup_price_prefetch = await jupiter_price.get_usd_price(mint_key)
        except Exception as exc:  # noqa: BLE001
            log.debug("[buyer] Jupiter prefetch error: %s", exc)
            jup_price_prefetch = None

        if jup_price_prefetch is None or jup_price_prefetch <= 0:
            log.warning(
                "[buyer] Jupiter NO devuelve precio para %s → NO compro (policy).",
                mint_key[:6],
            )
            return {
                "qty_lamports": 0,
                "signature": "NO_JUP_PRICE",
                "route": {},
                "buy_price_usd": 0.0,
                "peak_price": 0.0,
                "price_source": "fallback0",
            }

    # ─────── Intentos de compra real ───────────────────────
    last_exc: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = await gmgn.buy(token_addr, amount_sol)
            qty_lp, _price_unit_from_quote, route = _parse_route(resp)

            # tokens_received (si disponemos de outAmount y decimals)
            tokens_received: Optional[float] = None
            out_raw = _extract_out_amount({"quote": route.get("quote", {})})
            decimals = _extract_decimals({"quote": route.get("quote", {}), **route})
            if out_raw is None:
                out_raw = _extract_out_amount(route)
            if decimals is None:
                decimals = _extract_decimals(route)

            if out_raw is not None and isinstance(decimals, int):
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

            return {
                "qty_lamports": qty_lp,
                "signature": resp.get("signature", ""),
                "route": route,
                "buy_price_usd": buy_price_usd,
                "peak_price": buy_price_usd,
                "price_source": price_src,
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
    }
