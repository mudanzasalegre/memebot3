# trader/buyer.py
"""
Capa delgada sobre ``gmgn.buy`` que añade comprobaciones de saldo
y reserva de gas antes de lanzar la orden real. 100 % compatible
con el flujo original de MemeBot 3 (la firma de retorno NO cambia).

• Cuando *amount_sol* ≤ 0  →  modo simulación (paper-trading).
• Antes de comprar, verifica que el wallet dispone de saldo suficiente
  para cubrir la orden **y** deja un `GAS_RESERVE_SOL` para las ventas.
• Devuelve SIEMPRE un dict homogéneo:

    {
      "qty_lamports": int,     # cantidad comprada (lamports)
      "signature":    str,     # txid o flag especial
      "route":        dict     # JSON crudo de gmgn
      "buy_price_usd": float,  # precio unitario de entrada (USD)
      "peak_price":    float,  # precio máximo observado (USD)
      "price_source":  str,    # origen del precio de compra (nuevo)
    }
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, Final, Optional

from config.config import CFG
from utils.solana_rpc import get_balance_lamports
from db.database import SessionLocal
from db.models import Position
from sqlalchemy import select

# Precio: usaremos Jupiter Price v3 directamente
from fetcher import jupiter_price

# gmgn SDK local
from . import gmgn  # type: ignore

log = logging.getLogger("buyer")

SOL_MINT = "So11111111111111111111111111111111111111112"

# ─── Parámetros ──────────────────────────────────────────────
GAS_RESERVE_SOL: Final[float] = 0.002
_GAS_RESERVE_LAMPORTS: Final[int] = int(GAS_RESERVE_SOL * 1e9)

_RETRIES: Final[int] = 3
_RETRY_WAIT: Final[int] = 2  # s entre intentos

_WALLET_PUBKEY: Final[str] = os.getenv("SOL_PUBLIC_KEY", "")


# ─── Helpers ─────────────────────────────────────────────────
def _parse_route(resp: dict) -> tuple[int, float, dict]:
    """
    Normaliza la respuesta de gmgn:
    qty_lamports (outAmount), price_usd (unitario) y ruta bruta.

    Nota: 'price_usd' calculado aquí viene de inAmountUSD/qty. Para
    homogeneidad con el bot, preferimos calcular buy_price_usd con
    el helper Jupiter/SOL más abajo. Aun así lo devolvemos para
    compatibilidad si hiciera falta.
    """
    route = resp.get("route", {})
    quote = route.get("quote", {})

    qty_lp = int(quote.get("outAmount", "0"))
    total_usd = float(quote.get("inAmountUSD", "0.0"))  # coste TOTAL en USD
    price_unit = (total_usd / qty_lp * 1e9) if qty_lp else 0.0
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
        return True


async def _max_positions_reached() -> bool:
    """Comprueba si ya hay demasiadas posiciones abiertas."""
    async with SessionLocal() as session:
        stmt = select(Position).where(Position.closed.is_(False))
        count = (await session.execute(stmt)).scalars().unique().count()
        return count >= CFG.MAX_ACTIVE_POSITIONS


async def _resolve_buy_price_usd(
    token_mint: str,
    amount_sol: float,
    tokens_received: Optional[float],
    ds_price_usd: Optional[float] = None,
) -> tuple[float, str]:
    """
    Resuelve el precio de compra con prioridad:
    1) Jupiter Price directo del token
    2) Estimación vía SOL/USD si conocemos tokens_received
    3) Hint (DexScreener) si nos llega del orquestador
    4) Último recurso: 0.0
    """
    # 1) Jupiter directo
    p = await jupiter_price.get_usd_price(token_mint)
    if p is not None and p > 0:
        return float(p), "jupiter"

    # 2) Estimación por SOL/USD
    sol_usd = await jupiter_price.get_usd_price(SOL_MINT)
    if sol_usd and sol_usd > 0 and tokens_received and tokens_received > 0:
        est = (amount_sol * sol_usd) / tokens_received
        return float(est), "sol_estimate"

    # 3) Hint externo (DexScreener)
    if ds_price_usd and ds_price_usd > 0:
        return float(ds_price_usd), "dexscreener"

    # 4) Fallback
    log.warning("[buy] No pude resolver buy_price_usd para %s; guardo 0.0", token_mint[:6])
    return 0.0, "fallback0"


def _extract_decimals(route: dict) -> Optional[int]:
    """
    Intenta extraer los 'decimals' del token de salida de varias formas,
    porque distintos providers/SDKs usan keys diferentes.
    """
    quote = route.get("quote", {})

    # Candidatos directos
    for k in ("outDecimals", "decimals", "out_decimals"):
        v = quote.get(k)
        if isinstance(v, int) and 0 <= v <= 18:
            return v

    # Anidados habituales
    nested_candidates = [
        ("outToken", "decimals"),
        ("output", "decimals"),
        ("outputMintInfo", "decimals"),
    ]
    for a, b in nested_candidates:
        v = (quote.get(a) or {}).get(b) if isinstance(quote.get(a), dict) else None
        if isinstance(v, int) and 0 <= v <= 18:
            return v

    # A veces viene en 'route' a nivel superior
    for k in ("outDecimals", "decimals"):
        v = route.get(k)
        if isinstance(v, int) and 0 <= v <= 18:
            return v

    return None


def _extract_out_amount(route: dict) -> Optional[int]:
    """Devuelve el outAmount bruto (enteros) si está presente."""
    quote = route.get("quote", {})
    for k in ("outAmount", "out_amount", "toAmount"):
        v = quote.get(k)
        if isinstance(v, str) and v.isdigit():
            return int(v)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return None


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
    # ─────── Simulación directa (paper-trading) ────────────
    if amount_sol <= 0:
        log.info("[buyer] SIMULACIÓN · no se envía orden real (amount=0)")
        mint_key = token_mint or token_addr
        # en simulación no sabemos tokens_received
        buy_price_usd, price_src = await _resolve_buy_price_usd(
            token_mint=mint_key,
            amount_sol=amount_sol,
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
        log.error("[buyer] Fondos insuficientes · %.3f SOL pedido · reserva %.3f SOL",
                  amount_sol, GAS_RESERVE_SOL)
        return {
            "qty_lamports": 0,
            "signature": "INSUFFICIENT_FUNDS",
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
                # prueba directamente sobre 'route' por si el helper anterior falló
                out_raw = _extract_out_amount(route)
            if decimals is None:
                decimals = _extract_decimals(route)

            if out_raw is not None and isinstance(decimals, int):
                try:
                    tokens_received = out_raw / (10 ** decimals)
                except Exception:
                    tokens_received = None

            mint_key = token_mint or token_addr
            buy_price_usd, price_src = await _resolve_buy_price_usd(
                token_mint=mint_key,
                amount_sol=amount_sol,
                tokens_received=tokens_received,
                ds_price_usd=price_hint,
            )

            return {
                "qty_lamports": qty_lp,
                "signature": resp.get("signature", ""),
                "route": route,
                "buy_price_usd": buy_price_usd,
                "peak_price": buy_price_usd,
                "price_source": price_src,   # ← nuevo
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
