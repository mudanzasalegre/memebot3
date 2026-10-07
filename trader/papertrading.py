# trader/papertrading.py
"""
Motor de *paper-trading* (órdenes fantasma) cuando el bot se ejecuta con
`--dry-run` o `CFG.DRY_RUN = 1`.

Objetivo en esta revisión:
──────────────────────────
• Verificado que `buy()` devuelve: buy_price_usd, price_source.
• Verificado que `sell()` devuelve: price_used_usd, price_source_close.
• `check_exit_conditions()` sella correctamente: closed_at, pnl_pct y exit_reason.
• Añadido helper `safe_close_snapshot()` para obtener un snapshot de cierre
  (p. ej., para el orquestador), sin lógica de dataset aquí.
• Solo logs/trazas; **NO** persistimos dataset (eso se hace en run_bot.py al cierre).

Cambios
───────
2025-09-15
• Guard extra de seguridad (“belt & suspenders”): bloquear BUY si Jupiter no
  tiene ruta ejecutable **solo si** la policy lo exige. Si *no* se exige,
  se permite comprar en DRY-RUN aplicando **fallback de impacto** con
  `IMPACT_EST_K`/`IMPACT_MAX_PCT` (o divergencia DS↔JUP) para pares jóvenes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
import os
import pathlib
import time
from typing import Any, Dict, Optional, Tuple

from config.config import CFG, PROJECT_ROOT
from analytics import runner_ladder
import analytics.exit_policy as exit_policy
from utils.time import utc_now, is_in_trading_window, seconds_until_next_window
from utils import price_service
from utils.sol_price import amount_sol_to_usd, get_sol_usd
from utils.runtime_context import runtime_context_payload
from trade_pnl import apply_partial_fill, summarize_trade
from fetcher import jupiter_price, jupiter_router
from research_loop import runner_forward

log = logging.getLogger("papertrading")

SOL_MINT = "So11111111111111111111111111111111111111112"


def _positive_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _cost_model() -> dict[str, Any]:
    # Scenario assumptions, NOT observed fills/fees. Freeze per position.
    bps = float(os.getenv("PAPER_FILL_SLIPPAGE_BPS", "100"))
    fee = float(os.getenv("PAPER_FILL_FEE_SOL", "0.000025"))
    if not math.isfinite(bps) or not 0 <= bps < 10000 or not math.isfinite(fee) or fee < 0:
        raise ValueError("Invalid paper execution cost assumptions")
    return {"version": "estimated-v1", "slippage_bps": bps, "fee_sol_per_fill": fee,
            "observed_execution": False}


def _update_net_costs(entry: dict[str, Any], *, closing: bool) -> None:
    if not entry.get("execution_cost_model"):
        return  # Historical records have unknown costs; never invent net PnL.
    fees = float(entry.get("estimated_fees_usd") or 0)
    entry["net_realized_pnl_usd"] = float(entry.get("realized_pnl_usd") or 0) - fees
    if closing:
        entry["net_total_pnl_usd"] = float(entry["total_pnl_usd"]) - fees
        entry["net_total_pnl_pct"] = 100 * entry["net_total_pnl_usd"] / float(entry["entry_notional_usd"])

# Política de entrada: alinear con run_bot → usar el flag REQUIRE_JUPITER_FOR_BUY
_REQUIRE_JUP_PRICE: bool = bool(
    getattr(CFG, "REQUIRE_JUPITER_FOR_BUY", getattr(CFG, "USE_JUPITER_PRICE", False))
)

# ───── Impact fallback params (para DRY-RUN cuando no hay ruta) ─────
try:
    _IMPACT_MAX_PCT = float(os.getenv("IMPACT_MAX_PCT", "8"))
except Exception:
    _IMPACT_MAX_PCT = 8.0

try:
    _IMPACT_EST_K = float(os.getenv("IMPACT_EST_K", "2.0"))
except Exception:
    _IMPACT_EST_K = 2.0

try:
    _PRICE_DIVERGENCE_MAX_PCT = float(os.getenv("PRICE_DIVERGENCE_MAX_PCT", "15"))
except Exception:
    _PRICE_DIVERGENCE_MAX_PCT = 15.0

# ────────────── Parámetros de salida (alineados con seller.py) ───────────────
TAKE_PROFIT_PCT   = float(CFG.TAKE_PROFIT_PCT or 0.0)
STOP_LOSS_PCT     = float(CFG.STOP_LOSS_PCT or 0.0)
TRAILING_PCT      = float(CFG.TRAILING_PCT or 0.0)
MAX_HOLDING_H     = float(CFG.MAX_HOLDING_H or 24)

TAKE_PROFIT_FRAC  = TAKE_PROFIT_PCT / 100.0
STOP_LOSS_FRAC    = abs(STOP_LOSS_PCT) / 100.0
TRAILING_FRAC     = TRAILING_PCT / 100.0
TIMEOUT_SECONDS   = int(MAX_HOLDING_H * 3600)

# TP parcial (alineado con run_bot.py)
TP_PARTIAL_ENABLED = os.getenv("TP_PARTIAL_ENABLED", "true").lower() == "true"
try:
    TP_PARTIAL_FRACTION = float(os.getenv("TP_PARTIAL_FRACTION", "0.40"))
except Exception:
    TP_PARTIAL_FRACTION = 0.40
TP_PARTIAL_FRACTION = min(max(TP_PARTIAL_FRACTION, 0.05), 0.95)

# Extensión máxima dura (si va muy en verde)
try:
    MAX_HARD_HOLD_H = float(os.getenv("MAX_HARD_HOLD_H", "4"))
except Exception:
    MAX_HARD_HOLD_H = 4.0
HARD_TIMEOUT_SECONDS = int(MAX_HARD_HOLD_H * 3600)

# No-Expansion: cierre temprano a 1h si PnL ≤ umbral (por defecto 0%)
try:
    NO_EXPANSION_MAX_PCT = float(os.getenv("NO_EXPANSION_MAX_PCT", "0.0"))
except Exception:
    NO_EXPANSION_MAX_PCT = 0.0
NO_EXPANSION_MAX_FRAC = NO_EXPANSION_MAX_PCT / 100.0

# ───────────────────────── helpers de precio ─────────────────────────

async def _resolve_buy_price_usd(
    token_mint: str,
    amount_sol: float,
    tokens_received: Optional[float],
    ds_price_usd: Optional[float] = None,
) -> tuple[float, str]:
    # 1) Intenta precio directo en Jupiter
    p = await jupiter_price.get_usd_price(token_mint)
    if _positive_finite(p):
        return float(p), "jupiter"
    # 2) Estimar con SOL/USD si sabemos cuántos tokens recibimos
    sol_usd = await get_sol_usd()
    if sol_usd and sol_usd > 0 and tokens_received and tokens_received > 0:
        return float((amount_sol * sol_usd) / tokens_received), "sol_estimate"
    # 3) Hint (DexScreener) si venía del orquestador
    if _positive_finite(ds_price_usd):
        return float(ds_price_usd), "dexscreener"
    # 4) Último recurso
    log.warning("[buy] No pude resolver buy_price_usd para %s; guardo 0.0", token_mint[:6])
    return 0.0, "fallback0"


async def _resolve_close_price_usd(
    token_mint: str,
    *,
    price_hint: Optional[float] = None,
    price_source_hint: Optional[str] = None,
) -> Tuple[Optional[float], Optional[str]]:
    """
    Resuelve precio de cierre con prioridad:
      1) hint del orquestador (si válido)
      2) Jupiter unitario
      3) price_service crítico
      4) Dex/GT “full” (par), como último recurso
    Devuelve (precio | None, fuente | None)
    """
    # 1) hint del orquestador
    if _positive_finite(price_hint):
        return float(price_hint), (price_source_hint or "hint")

    # 2) Jupiter unitario
    try:
        jp = await jupiter_price.get_usd_price(token_mint)
        if _positive_finite(jp):
            return float(jp), "jupiter"
    except Exception:
        pass

    # 3) price_service crítico (forzando saltarse caches negativas si aplica)
    try:
        ps = await price_service.get_price_usd(token_mint, use_gt=True, critical=True)
        if _positive_finite(ps):
            return float(ps), "jup_critical"
    except TypeError:
        try:
            ps = await price_service.get_price_usd(token_mint)
            if _positive_finite(ps):
                return float(ps), "jup_single"
        except Exception:
            pass
    except Exception:
        pass

    # 4) Dex/GT “full”
    try:
        tok_full = await price_service.get_price(token_mint, use_gt=True)
        if tok_full and _positive_finite(tok_full.get("price_usd")):
            return float(tok_full["price_usd"]), "dex_full"
    except Exception:
        pass

    return None, None


async def _resolve_entry_notional_usd(amount_sol: float) -> float:
    notional = await amount_sol_to_usd(amount_sol)
    return float(notional or 0.0)


def _recompute_entry_totals(entry: Dict[str, Any]) -> None:
    entry = _ensure_entry_accounting(entry)
    entry_notional = float(entry.get("entry_notional_usd") or 0.0)
    if entry_notional <= 0.0:
        return

    if bool(entry.get("closed")):
        totals = summarize_trade(
            entry_qty=entry.get("entry_qty", 0),
            remaining_qty=entry.get("qty_lamports", 0),
            buy_price_usd=entry.get("buy_price_usd", 0.0),
            entry_notional_usd=entry_notional,
            realized_qty=entry.get("realized_qty", 0),
            realized_proceeds_usd=entry.get("realized_proceeds_usd", 0.0),
            close_price_usd=entry.get("close_price_usd"),
        )
        entry["realized_cost_usd"] = float(totals.realized_cost_usd)
        entry["realized_pnl_usd"] = float(totals.realized_pnl_usd)
        entry["effective_exit_price_usd"] = totals.effective_exit_price_usd
        entry["total_pnl_usd"] = float(totals.total_pnl_usd)
        entry["total_pnl_pct"] = float(totals.total_pnl_pct)
        entry["pnl_pct"] = float(totals.total_pnl_pct)
        return

    if int(entry.get("realized_qty") or 0) > 0:
        totals = summarize_trade(
            entry_qty=entry.get("entry_qty", 0),
            remaining_qty=entry.get("qty_lamports", 0),
            buy_price_usd=entry.get("buy_price_usd", 0.0),
            entry_notional_usd=entry_notional,
            realized_qty=entry.get("realized_qty", 0),
            realized_proceeds_usd=entry.get("realized_proceeds_usd", 0.0),
            close_price_usd=None,
        )
        entry["realized_cost_usd"] = float(totals.realized_cost_usd)
        entry["realized_pnl_usd"] = float(totals.realized_pnl_usd)


async def _ensure_entry_notional_async(entry: Dict[str, Any]) -> float:
    entry = _ensure_entry_accounting(entry)
    current = float(entry.get("entry_notional_usd") or 0.0)
    if current > 0.0:
        return current
    amount_sol = float(entry.get("amount_sol") or 0.0)
    if amount_sol <= 0.0:
        return 0.0
    notional = await _resolve_entry_notional_usd(amount_sol)
    if notional > 0.0:
        entry["entry_notional_usd"] = float(notional)
        _recompute_entry_totals(entry)
        _save()
    return float(notional or 0.0)


async def backfill_entry_notionals() -> int:
    entries = [
        entry
        for entry in _PORTFOLIO.values()
        if float(entry.get("amount_sol") or 0.0) > 0.0
    ]
    if not entries:
        return 0

    needs_sol_price = any(
        float(entry.get("entry_notional_usd") or 0.0) <= 0.0
        for entry in entries
    )
    sol_usd = await get_sol_usd() if needs_sol_price else None
    updated = 0
    for entry in entries:
        amount_sol = float(entry.get("amount_sol") or 0.0)
        before = (
            float(entry.get("entry_notional_usd") or 0.0),
            entry.get("total_pnl_usd"),
            entry.get("total_pnl_pct"),
            entry.get("realized_cost_usd"),
            entry.get("realized_pnl_usd"),
        )
        if (
            float(entry.get("entry_notional_usd") or 0.0) <= 0.0
            and sol_usd is not None
            and sol_usd > 0.0
        ):
            entry["entry_notional_usd"] = float(amount_sol * float(sol_usd))
        if float(entry.get("entry_notional_usd") or 0.0) > 0.0:
            _recompute_entry_totals(entry)
        after = (
            float(entry.get("entry_notional_usd") or 0.0),
            entry.get("total_pnl_usd"),
            entry.get("total_pnl_pct"),
            entry.get("realized_cost_usd"),
            entry.get("realized_pnl_usd"),
        )
        if after != before:
            updated += 1
    if updated:
        _save()
        log.info("[papertrading] resync entry_notional_usd/PnL aplicado a %d posiciones", updated)
    return updated


async def _has_jupiter_route(token_mint: str, amount_sol: float = 0.1, *, proof: dict | None = None) -> tuple[Optional[bool], str]:
    """
    Intenta averiguar si Jupiter tiene **ruta ejecutable**.
    Preferimos un método enriquecido si existe; fallback: derivar de get_usd_price().
    Devuelve (has_route | None si indeterminado, status_str).
    """
    # A Price API response proves a price, NOT an executable swap route.
    try:
        quote = await jupiter_router.get_quote(input_mint=SOL_MINT, output_mint=token_mint, amount_sol=amount_sol)
        if not quote.ok:
            return False, "NO_QUOTE"
        if quote.in_amount != int(amount_sol * 1e9) or not quote.out_amount or quote.out_amount <= 0:
            return False, "INVALID_QUOTE_AMOUNTS"
        impact = quote.price_impact_bps
        if impact is None or not math.isfinite(impact):
            return False, "IMPACT_UNKNOWN"
        limit = float(getattr(CFG, "PAPER_BOOTSTRAP_MAX_PRICE_IMPACT_PCT", _IMPACT_MAX_PCT)) if getattr(CFG, "PAPER_BOOTSTRAP_ENABLED", False) else _IMPACT_MAX_PCT
        if abs(impact) / 100 > limit:
            return False, "HIGH_QUOTE_IMPACT"
        if proof is not None:
            proof.update({"out_amount": quote.out_amount, "in_amount": quote.in_amount,
                          "impact_bps": impact, "max_impact_pct": limit,
                          "route_count": (getattr(quote, "other", None) or {}).get("routePlan_len", 0)})
        return True, "QUOTE_OK"
    except Exception:
        return None, "ERR"


# ───────────────────────── persistencia ─────────────────────────
_DATA_PATH = pathlib.Path(PROJECT_ROOT) / "data" / "paper_portfolio.json"


def _research_root() -> pathlib.Path:
    # Keep isolated paper stores isolated too; never enroll test portfolios in
    # the operator's runtime directory merely because PROJECT_ROOT is global.
    return _DATA_PATH.parent.parent if _DATA_PATH.parent.name == "data" else _DATA_PATH.parent


def record_market_observation(address: str, price: float, *, liq_now: float | None = None) -> None:
    entry = _PORTFOLIO.get(address)
    if not entry or entry.get("closed") or not _positive_finite(price):
        return
    buy_price = entry.get("buy_price_usd")
    if not _positive_finite(buy_price):
        return
    previous = (entry.get("highest_pnl_pct"), entry.get("max_adverse_pnl_pct"))
    exit_policy.update_exit_state(entry, pnl_pct=(float(price) / float(buy_price) - 1) * 100)
    if previous != (entry.get("highest_pnl_pct"), entry.get("max_adverse_pnl_pct")):
        _save()
    runner_forward.observe_market(address, price, root=_research_root(), cfg=CFG, liq_now=liq_now)
_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

try:
    _PORTFOLIO: Dict[str, Any] = json.loads(_DATA_PATH.read_text())
except Exception:  # noqa: BLE001
    _PORTFOLIO = {}


def _save() -> None:
    """Graba `_PORTFOLIO` en disco (best-effort)."""
    try:
        _DATA_PATH.write_text(json.dumps(_PORTFOLIO, indent=2, default=str))
    except Exception as exc:  # noqa: BLE001
        log.warning("[papertrading] no se pudo guardar portfolio: %s", exc)


# ───────────────────── utilidades locales ──────────────────────
def _is_solana_address(addr: str) -> bool:
    """Filtro defensivo: descarta EVM (0x…) y longitudes extrañas."""
    if not addr or addr.startswith("0x"):
        return False
    return 30 <= len(addr) <= 50  # rango típico base58 de mints SOL


def _pick_key_for_entry(address: str, token_mint: Optional[str]) -> str:
    """
    Determina la clave usada en el JSON para esta posición. Preferimos token_mint si existe en cartera,
    si no, usamos `address`.
    """
    if token_mint and token_mint in _PORTFOLIO:
        return token_mint
    return address


def _ensure_entry_accounting(entry: Dict[str, Any]) -> Dict[str, Any]:
    qty_now = int(entry.get("qty_lamports") or 0)
    realized_qty = int(entry.get("realized_qty") or entry.get("realized_qty_lamports") or 0)
    entry_qty = int(entry.get("entry_qty") or 0)
    if entry_qty <= 0:
        entry_qty = qty_now + realized_qty

    entry["entry_qty"] = max(entry_qty, qty_now + realized_qty)
    entry["realized_qty"] = realized_qty
    entry.setdefault("realized_proceeds_usd", 0.0)
    entry.setdefault("realized_cost_usd", 0.0)
    entry.setdefault("realized_pnl_usd", 0.0)
    entry.setdefault("partial_count", 0)
    entry.setdefault("first_partial_at", None)
    entry.setdefault("last_partial_at", None)
    entry.setdefault("last_partial_qty", None)
    entry.setdefault("last_partial_price_usd", None)
    entry.setdefault("effective_exit_price_usd", None)
    entry.setdefault("total_pnl_usd", None)
    entry.setdefault("total_pnl_pct", None)
    entry.setdefault("entry_notional_usd", 0.0)
    entry.setdefault("highest_pnl_pct", 0.0)
    entry.setdefault("max_pnl_pct_seen", 0.0)
    entry.setdefault("max_adverse_pnl_pct", 0.0)
    entry.setdefault("exit_state", "pre_partial" if not entry.get("partial_taken") else "post_partial")
    entry.setdefault("partial_ladder_state", runner_ladder.encode_ladder_state(runner_ladder.initial_ladder_state()))
    return entry


# ─── lógica de compra ──────────────────────────────────────────
async def buy(
    address: str,
    amount_sol: float,
    *,
    price_hint: float | None = None,
    token_mint: str | None = None,
    liquidity_usd: float | None = None,
    entry_regime: str | None = None,
    entry_lane: str | None = None,
    discovered_via: str | None = None,
    gate_profile: str | None = None,
    runner_exit_profile: str | None = None,
    exit_profile: str | None = None,
    strategy_version: str | None = None,
    experiment_id: str | None = None,
    config_hash: str | None = None,
    require_jupiter_for_buy: bool | None = None,
) -> dict:
    """
    Registra una posición simulada.
    Retorna dict con: qty_lamports, signature, route, buy_price_usd, peak_price, price_source.

    `liquidity_usd` permite activar el **fallback de impacto** cuando no hay ruta Jupiter:
      impact_est ≈ (amount_sol·SOL_USD / liquidity_usd) · IMPACT_EST_K
      Si impact_est > IMPACT_MAX_PCT → no compra (ni siquiera en DRY-RUN).
      Si no hay liquidez, se usa divergencia DexScreener↔Jupiter como salvaguarda.
    """
    # 0️⃣ Validación de red
    if not _is_solana_address(address):
        raise ValueError(f"[papertrading] Dirección no Solana bloqueada: {address!r}")

    mint_key = token_mint or address
    if not _positive_finite(amount_sol):
        return {"ok": False, "qty_lamports": 0, "signature": "INVALID_AMOUNT", "route": {}}
    if getattr(CFG, "PAPER_EXACT_TRADE_SIZE_ENABLED", False):
        required_amount = getattr(CFG, "PAPER_EXACT_TRADE_SIZE_SOL", 0.1)
        if not _positive_finite(required_amount) or not math.isclose(float(amount_sol), float(required_amount), abs_tol=1e-9, rel_tol=0):
            return {"ok": False, "qty_lamports": 0, "signature": "EXACT_PAPER_SIZE_REQUIRED", "route": {}}
    if mint_key in _PORTFOLIO and not _PORTFOLIO[mint_key].get("closed"):
        return {"ok": False, "qty_lamports": 0, "signature": "POSITION_ALREADY_OPEN", "route": {}}
    require_jup_price = _REQUIRE_JUP_PRICE if require_jupiter_for_buy is None else bool(require_jupiter_for_buy)
    exact_size_mode = bool(getattr(CFG, "PAPER_EXACT_TRADE_SIZE_ENABLED", False))
    require_exact_quote = require_jup_price or exact_size_mode

    # 0.5️⃣ Ventana horaria (SOLO si hay ventanas definidas por env)
    H = (os.getenv("TRADING_HOURS", "") or "").strip()
    E = (os.getenv("TRADING_HOURS_EXTRA", "") or "").strip()
    USE_EXTRA = os.getenv("USE_EXTRA_HOURS", "false").lower() == "true"
    if H or (USE_EXTRA and E):
        if not is_in_trading_window():
            delay = max(60, seconds_until_next_window())
            log.warning("[papertrading] Fuera de ventana horaria; no simulo compra. Próxima en %ss", delay)
            return {
                "qty_lamports": 0,
                "signature": "OUT_OF_WINDOW",
                "route": {},
                "buy_price_usd": 0.0,
                "peak_price": 0.0,
                "price_source": "fallback0",
            }

    # 0.6️⃣ Guard de ruta (belt & suspenders):
    #    - Si la policy EXIGE Jupiter → bloquear en ausencia de ruta.
    #    - Si NO lo exige → permitir, pero aplicando fallback de impacto (más abajo).
    route_proof: dict[str, Any] = {}
    try:
        has_route, status = await _has_jupiter_route(mint_key, amount_sol, proof=route_proof)
    except Exception:
        has_route, status = None, "ERR"

    if require_exact_quote and (has_route is not True or not route_proof):
        log.warning(
            "[trader] BUY bloqueado: sin ruta Jupiter (mint=%s, src=paper, reason=no_route)",
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
            "[papertrading] sin ruta Jupiter (mint=%s) pero REQUIRE_JUPITER_FOR_BUY=false → continuo (fallback).",
            mint_key[:6],
        )

    # 0.7️⃣ Política Jupiter (alineada con orquestador)
    if require_jup_price:
        try:
            jp = await jupiter_price.get_usd_price(mint_key)
        except Exception:
            jp = None
        if jp is None or jp <= 0:
            log.warning(
                "[papertrading] Jupiter NO devuelve precio para %s → NO simulo compra (policy).",
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

    # 0.8️⃣ Fallback de IMPACTO cuando **no hay ruta** y la policy NO exige Jupiter
    if has_route is False and not require_jup_price:
        impact_blocked = False
        try:
            sol_usd = await jupiter_price.get_usd_price(SOL_MINT)
        except Exception:
            sol_usd = None

        # 1) Heurística con liquidez (si la tenemos y hay SOL/USD)
        if sol_usd and sol_usd > 0 and liquidity_usd and liquidity_usd > 0:
            order_usd = amount_sol * float(sol_usd)
            try:
                impact_est_pct = 100.0 * (order_usd / float(liquidity_usd)) * _IMPACT_EST_K
                if impact_est_pct > _IMPACT_MAX_PCT:
                    log.info(
                        "[papertrading] impacto-estimado %.2f%% (liq %.0f USD, K=%.2f) > %.2f%% → skip",
                        impact_est_pct, liquidity_usd, _IMPACT_EST_K, _IMPACT_MAX_PCT
                    )
                    impact_blocked = True
                else:
                    log.debug(
                        "[papertrading] impacto-estimado OK: %.2f%% ≤ %.2f%% (liq %.0f, K=%.2f)",
                        impact_est_pct, _IMPACT_MAX_PCT, liquidity_usd, _IMPACT_EST_K
                    )
            except Exception as exc:
                log.debug("[papertrading] impacto-estimado: error cálculo con liquidez: %s", exc)

        # 2) Si no hay liquidez, usar divergencia DS↔JUP como sanity-check
        if not impact_blocked and (not liquidity_usd or liquidity_usd <= 0):
            tok_usd = None
            try:
                tok_usd = await jupiter_price.get_usd_price(mint_key)
            except Exception:
                tok_usd = None

            if tok_usd and tok_usd > 0 and price_hint and price_hint > 0:
                try:
                    ratio = float(price_hint) / float(tok_usd)
                    dev_pct = abs(100.0 * (1.0 - ratio))
                    if dev_pct > _PRICE_DIVERGENCE_MAX_PCT:
                        log.info(
                            "[papertrading] divergencia DS vs JUP (%.2f%%) > %.2f%% → skip",
                            dev_pct, _PRICE_DIVERGENCE_MAX_PCT
                        )
                        impact_blocked = True
                except Exception as exc:
                    log.debug("[papertrading] impacto-estimado: error divergencia DS↔JUP: %s", exc)

        if impact_blocked:
            return {
                "qty_lamports": 0,
                "signature": "HIGH_IMPACT_EST",
                "route": {},
                "buy_price_usd": 0.0,
                "peak_price": 0.0,
                "price_source": "fallback0",
            }

    # 1️⃣ Resolver precio de compra con trazabilidad
    tokens_received = None  # en paper no sabemos la cantidad exacta recibida
    buy_price_usd, price_src = await _resolve_buy_price_usd(
        token_mint=mint_key,
        amount_sol=amount_sol,
        tokens_received=tokens_received,
        ds_price_usd=price_hint,
    )
    entry_notional_usd = await _resolve_entry_notional_usd(amount_sol)
    if not _positive_finite(buy_price_usd) or not _positive_finite(entry_notional_usd):
        return {"ok": False, "qty_lamports": 0, "signature": "ENTRY_PRICE_OR_NOTIONAL_UNAVAILABLE", "route": {}}
    cost_model = _cost_model()
    spot_price_usd = buy_price_usd
    buy_price_usd *= 1 + cost_model["slippage_bps"] / 10000
    price_confidence = price_service.price_confidence_from_source(price_src, buy_price_usd)

    # 2️⃣ Alta de la posición en el JSON
    qty_lp = int(amount_sol * 1e9)  # simulamos "lamports" del token de salida
    if require_jup_price or route_proof.get("out_amount"):
        if not route_proof.get("out_amount"):
            return {"ok": False, "qty_lamports": 0, "signature": "QUOTE_PROOF_MISSING", "route": {}}
        # Raw SPL token units from the exact-size quote, reduced for adverse
        # paper slippage. These can be used for a real reverse-quote probe.
        qty_lp = int(route_proof["out_amount"] / (1 + cost_model["slippage_bps"] / 10000))
        if qty_lp <= 0:
            return {"ok": False, "qty_lamports": 0, "signature": "QUOTED_OUTPUT_TOO_SMALL", "route": {}}
    _PORTFOLIO[mint_key] = {
        **runtime_context_payload(),
        "qty_lamports": qty_lp,
        "entry_qty": qty_lp,
        "buy_price_usd": float(buy_price_usd),
        "peak_price": float(buy_price_usd),
        "amount_sol": amount_sol,
        "entry_notional_usd": float(entry_notional_usd),
        "buy_liquidity_usd": float(liquidity_usd) if _positive_finite(liquidity_usd) else None,
        "execution_cost_model": cost_model,
        "spot_entry_price_usd": spot_price_usd,
        "estimated_fees_usd": cost_model["fee_sol_per_fill"] * entry_notional_usd / amount_sol,
        "estimated_fees_sol": cost_model["fee_sol_per_fill"],
        "realized_proceeds_sol": 0.0,
        "execution_fill_count": 1,
        "opened_at": utc_now().isoformat(),
        "closed": False,
        "dry_run": True,
        "config_profile": os.getenv("CONFIG_PROFILE", ""),
        "token_address": mint_key,
        "price_source": price_src,
        "price_confidence": price_confidence,
        "require_jupiter_for_buy": bool(require_jup_price),
        "exact_paper_trade_size_sol": float(amount_sol) if exact_size_mode else None,
        "entry_route_quote": route_proof or None,
        "quantity_basis": "quoted_raw_spl_units" if route_proof else "synthetic_paper_units",
        "entry_regime": entry_regime,
        "entry_lane": entry_lane,
        "gate_profile": gate_profile,
        "runner_exit_profile": runner_exit_profile,
        "exit_profile": exit_profile or runner_exit_profile,
        "strategy_version": strategy_version,
        "experiment_id": experiment_id,
        "config_hash": config_hash,
        "discovered_via": discovered_via,
        "partial_taken": False,
        "partial_count": 0,
        "partial_fill_events": 0,
        "highest_pnl_pct": 0.0,
        "max_pnl_pct_seen": 0.0,
        "max_adverse_pnl_pct": 0.0,
        "exit_state": "pre_partial",
        "partial_ladder_state": runner_ladder.encode_ladder_state(runner_ladder.initial_ladder_state()),
        "runner_trailing_policy": runner_forward.entry_policy(CFG, root=_research_root()),
        "realized_qty": 0,
        "realized_proceeds_usd": 0.0,
        "realized_cost_usd": 0.0,
        "realized_pnl_usd": 0.0,
        "effective_exit_price_usd": None,
        "total_pnl_usd": None,
        "total_pnl_pct": None,
        "first_partial_at": None,
        "last_partial_at": None,
        "last_partial_qty": None,
        "last_partial_price_usd": None,
        "exit_reason": None,
    }
    _save()

    log.info(
        "[papertrading] 💰💰 BUY %s amount_sol=%.3f price_usd=%.8g src=%s",
        mint_key[:6], amount_sol, buy_price_usd, price_src,
    )
    return {
        "qty_lamports": qty_lp,
        "signature": f"SIM-{int(time.time()*1e3)}",
        "route": {},
        "buy_price_usd": float(buy_price_usd),
        "peak_price": float(buy_price_usd),
        "price_source": price_src,
        "price_confidence": price_confidence,
        "entry_notional_usd": float(entry_notional_usd),
        "runner_trailing_policy": _PORTFOLIO[mint_key]["runner_trailing_policy"],
    }


# ─── rellenar precio tras la compra ────────────────────────────
async def _retry_fill_buy_price(
    address: str,
    *,
    tries: int = 3,
    delay: int = 8,
) -> None:
    """Intenta rellenar `buy_price_usd`/`peak_price` si quedaron a 0."""
    for attempt in range(1, tries + 1):
        await asyncio.sleep(delay)
        price = await jupiter_price.get_usd_price(address)
        if price:
            entry = _PORTFOLIO.get(address)
            if entry and entry.get("buy_price_usd") in (0.0, None):
                entry["buy_price_usd"] = entry["peak_price"] = float(price)
                entry["price_confidence"] = "high"
                _save()
                log.info(
                    "[papertrading] buy_price_usd actualizado a %.6f USD (retry %d)",
                    price,
                    attempt,
                )
            break


# ─── venta (simulada): soporta parciales y cierre total ─────────────────────
async def sell(
    address: str,
    qty_lamports: int,
    *,
    token_mint: str | None = None,
    price_hint: float | None = None,
    price_source_hint: str | None = None,
    exit_reason: str | None = None,
) -> dict:
    """
    Vende (simulado) una cantidad del token. Si `qty_lamports` es menor que el tamaño
    restante, realiza una **venta parcial** (no cierra posición). Si es mayor o igual,
    cierra completamente.

    Retorna dict con: signature, price_used_usd, price_source_close, partial, qty_sold, qty_left.
    """
    key = _pick_key_for_entry(address, token_mint)
    entry = _PORTFOLIO.get(key)
    if not entry or entry.get("closed"):
        raise RuntimeError(f"No hay posición activa para {address[:4]}")
    entry = _ensure_entry_accounting(entry)
    await _ensure_entry_notional_async(entry)

    if not _is_solana_address(key):
        log.error("[papertrading] Venta bloqueada: address no Solana %r", key)
        sig = f"SIM-{int(time.time()*1e3)}"
        return {"signature": sig, "error": "INVALID_ADDRESS", "price_used_usd": None, "price_source_close": None}

    total_qty = int(entry.get("qty_lamports", 0))
    take_qty = max(0, min(int(qty_lamports), total_qty))
    if take_qty <= 0:
        sig = f"SIM-{int(time.time()*1e3)}"
        log.info("[papertrading] sell qty=0 — nada que hacer")
        return {"signature": sig, "price_used_usd": None, "price_source_close": None}

    # 1) Resolver precio de cierre (prioriza hint)
    if entry.get("entry_route_quote"):
        price_now, price_src = None, None
    else:
        price_now, price_src = await _resolve_close_price_usd(
            token_mint=key, price_hint=price_hint, price_source_hint=price_source_hint,
        )

    cost_model = entry.get("execution_cost_model")
    sol_usd = await get_sol_usd() if cost_model else None
    if entry.get("entry_route_quote"):
        # Price data alone does not prove exit liquidity. Quote the exact raw
        # token quantity being sold, never the original SOL input amount.
        try:
            quote = await jupiter_router.get_quote(input_mint=key, output_mint=SOL_MINT, amount_lamports=take_qty)
            valid = (quote.ok and quote.in_amount == take_qty and quote.out_amount is not None
                     and quote.out_amount > 0 and quote.price_impact_bps is not None
                     and math.isfinite(quote.price_impact_bps)
                     and abs(quote.price_impact_bps) / 100 <= entry["entry_route_quote"]["max_impact_pct"])
        except Exception:
            valid = False
        if not valid or not _positive_finite(sol_usd):
            return {"ok": False, "error": "EXIT_QUOTE_UNAVAILABLE", "signature": None,
                    "qty_sold": 0, "qty_left": total_qty}
        proceeds_usd = quote.out_amount / 1e9 * float(sol_usd)
        try:
            runner_forward.observe_quote(key, quote, float(sol_usd), root=_research_root(), cfg=CFG)
        except Exception as exc:
            log.warning("[runner_forward] quote reuse unavailable: %s", type(exc).__name__)
        reference_tokens = (take_qty / int(entry["entry_qty"])) * float(entry["entry_notional_usd"]) / float(entry["buy_price_usd"])
        price_now, price_src = proceeds_usd / reference_tokens, "jupiter_reverse_quote"
    # A missing exit cannot be fabricated as a break-even fill.
    if not _positive_finite(price_now):
        log.warning("[papertrading] EXIT_PRICE_UNAVAILABLE %s; position remains open", key[:6])
        return {"ok": False, "error": "EXIT_PRICE_UNAVAILABLE", "err": "EXIT_PRICE_UNAVAILABLE",
                "signature": None, "price_used_usd": None, "price_source_close": None,
                "qty_sold": 0, "qty_left": total_qty}
    if cost_model:
        price_now = float(price_now) * (1 - float(cost_model["slippage_bps"]) / 10000)
        if not _positive_finite(sol_usd):
            return {"ok": False, "error": "FEE_VALUATION_UNAVAILABLE", "signature": None, "qty_sold": 0, "qty_left": total_qty}
        entry["estimated_fees_usd"] = float(entry.get("estimated_fees_usd") or 0) + float(cost_model["fee_sol_per_fill"]) * float(sol_usd)
        entry["estimated_fees_sol"] = float(entry.get("estimated_fees_sol") or 0) + float(cost_model["fee_sol_per_fill"])
        entry["execution_fill_count"] = int(entry.get("execution_fill_count") or 1) + 1
    price_confidence_close = price_service.price_confidence_from_source(price_src, price_now)

    sig = f"SIM-{int(time.time()*1e3)}"

    # 3) Parcial vs cierre total
    if take_qty < total_qty:
        partial_ladder_plan = None
        try:
            buy_price_for_plan = float(entry.get("buy_price_usd") or 0.0)
            pnl_pct_for_plan = (
                ((float(price_now) - buy_price_for_plan) / buy_price_for_plan) * 100.0
                if buy_price_for_plan > 0 and price_now > 0
                else 0.0
            )
            exit_policy.update_exit_state(entry, pnl_pct=pnl_pct_for_plan)
            partial_ladder_plan = exit_policy.partial_ladder_plan(entry, pnl_pct_for_plan)
        except Exception:
            partial_ladder_plan = None
        totals = apply_partial_fill(
            entry_qty=entry.get("entry_qty", total_qty),
            remaining_qty=total_qty,
            buy_price_usd=entry.get("buy_price_usd", 0.0),
            entry_notional_usd=entry.get("entry_notional_usd", 0.0),
            realized_qty=entry.get("realized_qty", 0),
            realized_proceeds_usd=entry.get("realized_proceeds_usd", 0.0),
            qty_sold=take_qty,
            fill_price_usd=price_now,
        )
        if cost_model:
            entry["realized_proceeds_sol"] = float(entry.get("realized_proceeds_sol") or 0) + (totals.realized_proceeds_usd - float(entry.get("realized_proceeds_usd") or 0)) / float(sol_usd)
        entry["qty_lamports"] = int(totals.remaining_qty)
        entry["entry_qty"] = int(totals.entry_qty)
        entry["realized_qty"] = int(totals.realized_qty)
        entry["realized_proceeds_usd"] = float(totals.realized_proceeds_usd)
        entry["realized_cost_usd"] = float(totals.realized_cost_usd)
        entry["realized_pnl_usd"] = float(totals.realized_pnl_usd)
        entry["partial_fill_events"] = int(entry.get("partial_fill_events", 1 if entry.get("partial_taken") else 0)) + 1
        entry["partial_taken"] = True
        partial_increment = 1
        if isinstance(partial_ladder_plan, dict):
            partial_increment = max(1, int(partial_ladder_plan.get("pending_step_count") or 1))
        entry["partial_count"] = int(entry.get("partial_count") or 0) + partial_increment
        entry["exit_state"] = "post_partial"
        if isinstance(partial_ladder_plan, dict) and isinstance(partial_ladder_plan.get("next_state"), dict):
            entry["partial_ladder_state"] = runner_ladder.encode_ladder_state(partial_ladder_plan["next_state"])
        entry["first_partial_at"] = entry.get("first_partial_at") or utc_now().isoformat()
        entry["last_partial_at"] = utc_now().isoformat()
        entry["last_partial_qty"] = int(take_qty)
        entry["last_partial_price_usd"] = float(price_now)
        entry["price_source_close"] = price_src  # guardamos fuente de la última acción
        entry["price_confidence_close"] = price_confidence_close
        entry["exit_reason"] = exit_reason or "partial_tp"
        _update_net_costs(entry, closing=False)
        _save()
        try:
            runner_forward.register_partial(entry, root=_research_root(), cfg=CFG)
        except Exception as exc:
            # A research failure cannot turn a completed fill into a failed sell.
            log.warning("[runner_forward] enrollment unavailable: %s", type(exc).__name__)
        log.info(
            "📝 PAPER-PARTIAL %s…  qty=%d/%d  px=%.6f USD  src=%s  sig=%s  reason=%s",
            key[:4], take_qty, total_qty, price_now, price_src, sig, entry["exit_reason"]
        )
        return {
            "signature": sig,
            "price_used_usd": float(price_now),
            "price_source_close": price_src,
            "price_confidence_close": price_confidence_close,
            "partial": True,
            "qty_sold": take_qty,
            "qty_left": int(entry["qty_lamports"]),
        }

    # 4) Cierre total
    buy_price = float(entry.get("buy_price_usd") or 0.0)
    totals = summarize_trade(
        entry_qty=entry.get("entry_qty", total_qty),
        remaining_qty=total_qty,
        buy_price_usd=buy_price,
        entry_notional_usd=entry.get("entry_notional_usd", 0.0),
        realized_qty=entry.get("realized_qty", 0),
        realized_proceeds_usd=entry.get("realized_proceeds_usd", 0.0),
        close_price_usd=price_now,
    )

    entry.update(
        {
            "closed_at": utc_now().isoformat(),
            "close_price_usd": float(price_now),
            "pnl_pct": float(totals.total_pnl_pct),
            "closed": True,
            "price_source_close": price_src,
            "price_confidence_close": price_confidence_close,
            "qty_lamports": 0,
            "effective_exit_price_usd": totals.effective_exit_price_usd,
            "total_pnl_usd": float(totals.total_pnl_usd),
            "total_pnl_pct": float(totals.total_pnl_pct),
            "exit_reason": exit_reason or entry.get("exit_reason") or "manual/auto",
        }
    )
    _update_net_costs(entry, closing=True)
    if cost_model:
        total_proceeds_sol = float(entry.get("realized_proceeds_sol") or 0) + (totals.total_proceeds_usd - float(entry.get("realized_proceeds_usd") or 0)) / float(sol_usd)
        entry["net_total_pnl_sol"] = total_proceeds_sol - float(entry["amount_sol"]) - float(entry["estimated_fees_sol"])
        entry["total_proceeds_sol"] = total_proceeds_sol
    _save()
    # Append-only closed-trade evidence survives a later buy of the same mint.
    archive = _DATA_PATH.parent / "paper_closed_trades.jsonl"
    try:
        with archive.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, default=str, allow_nan=False) + "\n")
    except (OSError, ValueError) as exc:
        log.error("[papertrading] CLOSED_TRADE_ARCHIVE_FAILED: %s", exc)

    log.info(
        "📝 PAPER-SELL %s…  close=%.6f USD  PnL=%.2f%%  src=%s  sig=%s  reason=%s",
        key[:4], price_now, totals.total_pnl_pct, price_src, sig, entry["exit_reason"]
    )
    return {
        "signature": sig,
        "price_used_usd": float(price_now),
        "price_source_close": price_src,
        "price_confidence_close": price_confidence_close,
        "partial": False,
        "qty_sold": take_qty,
        "qty_left": 0,
    }


# ─── helpers de parciales ───────────────────────────────────────────────────
def _compute_partial_qty(entry: dict, fraction: float) -> int:
    qty_lp = int(entry.get("qty_lamports") or 0)
    take = int(max(1, round(qty_lp * float(fraction))))
    return min(take, qty_lp)


# ─── evaluación y EJECUCIÓN de salidas ──────────────────────────────────────
async def check_exit_conditions(address: str) -> bool:  # noqa: C901
    """
    Evalúa y **ejecuta** salidas según la lógica del modo real.
    Devuelve True si ejecuta una venta parcial o total; False si no hace nada.
    Sella: closed_at, pnl_pct y exit_reason cuando cierra.
    """
    entry = _PORTFOLIO.get(address)
    if not entry or entry.get("closed"):
        return False
    entry = _ensure_entry_accounting(entry)

    # Precio actual (usar crítico para saltar cachés NIL)
    price_val = await price_service.get_price_usd(address, use_gt=True, critical=True)
    price = float(price_val or 0.0)

    buy_price = float(entry.get("buy_price_usd") or 0.0)
    peak_price = float(entry.get("peak_price") or (price if price > 0 else buy_price))

    # Actualiza pico sólo si hay precio válido
    if price > 0.0 and price > peak_price:
        entry["peak_price"] = peak_price = price
        _save()

    pnl_pct = (((price - buy_price) / buy_price) * 100.0) if (buy_price > 0.0 and price > 0.0) else 0.0
    state_before = (
        entry.get("highest_pnl_pct"),
        entry.get("max_pnl_pct_seen"),
        entry.get("max_adverse_pnl_pct"),
        entry.get("exit_state"),
    )
    exit_policy.update_exit_state(entry, pnl_pct=float(pnl_pct))
    if (
        entry.get("highest_pnl_pct"),
        entry.get("max_pnl_pct_seen"),
        entry.get("max_adverse_pnl_pct"),
        entry.get("exit_state"),
    ) != state_before:
        _save()

    if exit_policy.should_take_partial(entry, pnl_pct):
        frac = exit_policy.partial_sell_fraction(entry, pnl_pct)
        qty = _compute_partial_qty(entry, frac)
        if qty > 0:
            result = await sell(address, qty, token_mint=address, exit_reason="tp_partial")
            if result.get("ok") is False:
                return False
            log.info("[papertrading] Partial TP @ %.2f%% → vendidas ~%.0f%%", pnl_pct, frac * 100.0)
            return True

    exit_reason = exit_policy.should_exit(
        entry,
        price,
        utc_now(),
        pnl_pct=pnl_pct,
    )
    if exit_reason is None:
        return False

    result = await sell(address, int(entry.get("qty_lamports", 0)), token_mint=address, exit_reason=str(exit_reason).lower())
    if result.get("ok") is False:
        return False
    log.info("[papertrading] %s @ %.2f%% → cierre total", exit_reason, pnl_pct)
    return True


# ─── snapshot de cierre seguro (para orquestador) ───────────────────────────
async def safe_close_snapshot(
    address: str,
    *,
    token_mint: str | None = None,
    price_hint: float | None = None,
    price_source_hint: str | None = None,
    reason: str | None = None,
) -> dict:
    """
    Ejecuta un **cierre total seguro** y devuelve un snapshot con:
    { close_price_usd, price_source_close, pnl_pct, closed_at, exit_reason }

    • Resuelve precio (hint → Jupiter → crítico → Dex full); sin precio no cierra.
    • Sella en cartera: closed_at, close_price_usd, pnl_pct, exit_reason.
    • **No** persiste dataset aquí (eso lo hace run_bot.py).
    """
    key = _pick_key_for_entry(address, token_mint)
    entry = _PORTFOLIO.get(key)
    if not entry or entry.get("closed"):
        return {
            "close_price_usd": None,
            "price_source_close": None,
            "pnl_pct": None,
            "total_pnl_pct": entry.get("total_pnl_pct") if entry else None,
            "closed_at": entry.get("closed_at") if entry else None,
            "exit_reason": entry.get("exit_reason") if entry else reason,
        }

    # vender todo para cerrar
    qty_all = int(entry.get("qty_lamports", 0))
    res = await sell(
        address,
        qty_all,
        token_mint=token_mint,
        price_hint=price_hint,
        price_source_hint=price_source_hint,
        exit_reason=reason or "snapshot_close",
    )
    if res.get("ok") is False:
        return {"ok": False, "error": res.get("error"), "closed_at": None, "pnl_pct": None}

    # Releer entry tras sell()
    entry = _PORTFOLIO.get(key, {})
    snap = {
        "close_price_usd": entry.get("close_price_usd"),
        "price_source_close": entry.get("price_source_close"),
        "price_confidence_close": entry.get("price_confidence_close"),
        "pnl_pct": entry.get("pnl_pct"),
        "total_pnl_pct": entry.get("total_pnl_pct"),
        "closed_at": entry.get("closed_at"),
        "exit_reason": entry.get("exit_reason"),
    }
    log.debug("[papertrading] safe_close_snapshot %s → %s", key[:4], snap)
    return snap


# ─── exportación mínima ────────────────────────────────────────
__all__ = ["buy", "sell", "check_exit_conditions", "safe_close_snapshot", "backfill_entry_notionals"]
