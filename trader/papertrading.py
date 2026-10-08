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
import copy
import datetime as dt
import json
import logging
import math
import os
import pathlib
import time
import uuid
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
from execution.quote_observation import observe_quote
from execution import paper_cash_mark
from utils.raw_units import sol_to_lamports
from research_loop import runner_forward
from runtime.paper_entry_policy import snapshot as entry_policy_snapshot
from utils.atomic_json import read_json_strict, write_json_atomic
from runtime.paper_archive import PaperArchiveError, archive_closed_trade, entry_identity

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
    current = entry.get("entry_notional_usd")
    if (not isinstance(current, bool) and isinstance(current, (int, float))
            and _positive_finite(current)):
        return float(current)
    # Today's FX cannot reconstruct an entry that occurred in the past.
    # Recovery of an original buy receipt is separate from valuation.
    return 0.0


async def backfill_entry_notionals() -> int:
    entries = [
        entry
        for entry in _PORTFOLIO.values()
        if float(entry.get("amount_sol") or 0.0) > 0.0
    ]
    if not entries:
        return 0

    updated = 0
    for entry in entries:
        before = (
            float(entry.get("entry_notional_usd") or 0.0),
            entry.get("total_pnl_usd"),
            entry.get("total_pnl_pct"),
            entry.get("realized_cost_usd"),
            entry.get("realized_pnl_usd"),
        )
        original = entry.get("entry_notional_usd")
        if (not isinstance(original, bool) and isinstance(original, (int, float))
                and _positive_finite(original)):
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


def quote_impact_limit_pct(cfg: Any = None) -> float | None:
    cfg = CFG if cfg is None else cfg
    value = getattr(cfg, "PAPER_BOOTSTRAP_MAX_PRICE_IMPACT_PCT", _IMPACT_MAX_PCT) if getattr(cfg, "PAPER_BOOTSTRAP_ENABLED", False) else _IMPACT_MAX_PCT
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


async def _has_jupiter_route(token_mint: str, amount_sol: float = 0.1, *, proof: dict | None = None) -> tuple[Optional[bool], str]:
    """
    Cotización reciente para el importe exacto; nunca permiso por un precio.
    None significa observación desconocida, no ausencia de ruta.
    """
    # A Price API response proves a price, NOT an executable swap route.
    try:
        slippage = jupiter_router.routing_quote_slippage_bps()
        quote = await jupiter_router.get_routing_quote(input_mint=SOL_MINT, output_mint=token_mint,
            amount_sol=amount_sol, slippage_bps=slippage)
        observed = observe_quote(quote, input_mint=SOL_MINT, output_mint=token_mint,
            amount=sol_to_lamports(amount_sol), slippage=slippage, now=utc_now())
        if observed.has_route is not True:
            return observed.has_route, "NO_ROUTE" if observed.has_route is False else "QUOTE_UNVERIFIED"
        impact = observed.price_impact_bps
        limit = quote_impact_limit_pct()
        if limit is None:
            return False, "IMPACT_LIMIT_UNKNOWN"
        from execution.quote_observation import impact_within_limit
        from execution.quote_receipt import capture_summary
        if not impact_within_limit(impact, limit, protocol=observed.protocol):
            return False, "HIGH_QUOTE_IMPACT"
        if proof is not None:
            proof.update(capture_summary(quote, input_mint=SOL_MINT, output_mint=token_mint,
                amount=sol_to_lamports(amount_sol), slippage=slippage, limit=limit, now=utc_now()))
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
    from research_loop import entry_gate_forward
    entry_gate_forward.observe_market(address, price, root=_research_root(), cfg=CFG, liq_now=liq_now)
    entry = _PORTFOLIO.get(address)
    if not entry or entry.get("closed") or not _positive_finite(price):
        return
    if entry.get("entry_route_quote") or entry.get("quantity_basis") == "quoted_raw_spl_units":
        return  # Physical spot is not an exact-quantity PAPER cash valuation.
    buy_price = entry.get("buy_price_usd")
    if not _positive_finite(buy_price):
        return
    previous = (entry.get("highest_pnl_pct"), entry.get("max_adverse_pnl_pct"))
    exit_policy.update_exit_state(entry, pnl_pct=(float(price) / float(buy_price) - 1) * 100)
    if previous != (entry.get("highest_pnl_pct"), entry.get("max_adverse_pnl_pct")):
        _save()
    runner_forward.observe_market(address, price, root=_research_root(), cfg=CFG, liq_now=liq_now)
_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

class PaperPortfolioError(RuntimeError):
    pass


def load_portfolio() -> Dict[str, Any]:
    """A missing first-launch store is empty; a corrupt store is never empty."""
    try:
        portfolio = read_json_strict(_DATA_PATH)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError) as exc:
        raise PaperPortfolioError("Paper portfolio is unreadable; preserve it and keep trading stopped") from exc
    if not isinstance(portfolio, dict) or any(not isinstance(value, dict) for value in portfolio.values()):
        raise PaperPortfolioError("Paper portfolio must contain address-keyed entries")
    return portfolio


_PORTFOLIO: Dict[str, Any] = load_portfolio()


def _save(*, strict: bool = False) -> None:
    """Atomically replace the portfolio; a BUY requires successful persistence."""
    try:
        write_json_atomic(_DATA_PATH, _PORTFOLIO)
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise PaperPortfolioError("Paper fill was not durably persisted") from exc
        log.warning("[papertrading] no se pudo guardar portfolio: %s", type(exc).__name__)


def _archive_closed(key: str) -> str:
    """Archive first; acknowledgement metadata cannot invalidate a filled sell."""
    original = _PORTFOLIO[key]
    archive_id = archive_closed_trade(_DATA_PATH.parent, original, token=key)
    if original.get("closed_archive_pending") is True:
        updated = {**original, "closed_archive_pending": False}
        _PORTFOLIO[key] = updated
        try:
            _save(strict=True)
        except PaperPortfolioError:
            _PORTFOLIO[key] = original
            log.warning("[papertrading] archive acknowledged but portfolio marker remains pending")
        else:
            original.clear()
            original.update(updated)
            _PORTFOLIO[key] = original
    return archive_id


_ARCHIVE_REPAIR_STATE: dict[str, tuple[float, int]] = {}


async def repair_paper_archives(*, force: bool = False, limit: int = 8) -> dict:
    """Bound secondary retries; never execute a trade or modify its cash state."""
    key = str(_DATA_PATH.resolve())
    previous, cursor = _ARCHIVE_REPAIR_STATE.get(key, (0., 0))
    stamp = time.monotonic()
    if not force and stamp - previous < 30:
        return {"status": "throttled", "attempted": 0, "failed": 0}
    if len(_ARCHIVE_REPAIR_STATE) >= 32 and key not in _ARCHIVE_REPAIR_STATE:
        _ARCHIVE_REPAIR_STATE.pop(next(iter(_ARCHIVE_REPAIR_STATE)))
    keys = list(_PORTFOLIO)
    attempted = failed = examined = 0
    limit = max(1, min(8, int(limit)))
    while keys and examined < min(128, len(keys)) and attempted < limit:
        token = keys[cursor % len(keys)]
        cursor += 1
        examined += 1
        current = _PORTFOLIO.get(token, {})
        if current.get("closed") is True and current.get("closed_archive_pending") is True:
            attempted += 1
            async def repair_owned():
                fresh = _PORTFOLIO.get(token, {})
                if fresh.get("closed") is True and fresh.get("closed_archive_pending") is True:
                    _archive_closed(token)
            try:
                await _serialized_paper_order(token, repair_owned)
            except PaperArchiveError as exc:
                failed += 1
                log.warning("[papertrading] closed archive retry pending: %s", type(exc).__name__)
    _ARCHIVE_REPAIR_STATE[key] = (stamp, cursor % len(keys) if keys else 0)
    return {"status": "pending" if failed else "ok", "attempted": attempted, "failed": failed}


async def repair_runner_research(*, force: bool = False) -> dict:
    from runtime.runner_enrollment import repair_sources
    return repair_sources(root=_research_root(), portfolio=_PORTFOLIO, cfg=CFG, force=force)


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
async def buy(address: str, amount_sol: float, **kwargs) -> dict:
    kwargs = copy.deepcopy(kwargs)
    return await _serialized_paper_order(kwargs.get("token_mint") or address,
        _buy_owned, address, amount_sol, **kwargs)


async def _buy_owned(
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
    entry_intent_id: str | None = None,
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
    intent_id = uuid.uuid4().hex if entry_intent_id is None else entry_intent_id
    if not isinstance(intent_id, str) or len(intent_id) != 32 or any(char not in "0123456789abcdef" for char in intent_id):
        raise ValueError("Invalid paper entry intent identity")
    if ((_DATA_PATH.parent / "paper_closed_trades" / (intent_id + ".json")).exists()
            or any(row.get("entry_intent_id") == intent_id
                   or row.get("buy_signature") == "SIM-" + intent_id
                   for row in _PORTFOLIO.values())):
        return {"ok": False, "qty_lamports": 0, "signature": "ENTRY_INTENT_ALREADY_USED", "route": {}}
    if getattr(CFG, "PAPER_EXACT_TRADE_SIZE_ENABLED", False):
        required_amount = getattr(CFG, "PAPER_EXACT_TRADE_SIZE_SOL", 0.1)
        if not _positive_finite(required_amount) or not math.isclose(float(amount_sol), float(required_amount), abs_tol=1e-9, rel_tol=0):
            return {"ok": False, "qty_lamports": 0, "signature": "EXACT_PAPER_SIZE_REQUIRED", "route": {}}
    if mint_key in _PORTFOLIO and not _PORTFOLIO[mint_key].get("closed"):
        return {"ok": False, "qty_lamports": 0, "signature": "POSITION_ALREADY_OPEN", "route": {}}
    if mint_key in _PORTFOLIO:
        try:
            _archive_closed(mint_key)
        except PaperArchiveError as exc:
            log.warning("[papertrading] replacement blocked until prior close is archived: %s", type(exc).__name__)
            return {"ok": False, "qty_lamports": 0, "signature": "PAPER_ARCHIVE_UNAVAILABLE", "route": {}}
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
        rejection = {"HIGH_QUOTE_IMPACT": "HIGH_IMPACT", "IMPACT_LIMIT_UNKNOWN": "INVALID_IMPACT_LIMIT"}.get(status, "NO_ROUTE")
        log.warning(
            "[trader] BUY bloqueado: sin ruta Jupiter (mint=%s, src=paper, reason=no_route)",
            mint_key[:6],
        )
        return {
            "qty_lamports": 0,
            "signature": rejection,
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
    buy_signature = "SIM-" + intent_id
    previous = _PORTFOLIO.get(mint_key)
    _PORTFOLIO[mint_key] = {
        **runtime_context_payload(),
        "entry_intent_id": intent_id,
        "buy_signature": buy_signature,
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
        "paper_entry_policy": entry_policy_snapshot(),
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
    try:
        _save(strict=True)
    except BaseException:
        if previous is None:
            _PORTFOLIO.pop(mint_key, None)
        else:
            _PORTFOLIO[mint_key] = previous
        raise

    log.info(
        "[papertrading] 💰💰 BUY %s amount_sol=%.3f price_usd=%.8g src=%s",
        mint_key[:6], amount_sol, buy_price_usd, price_src,
    )
    return {
        "qty_lamports": qty_lp,
        "signature": buy_signature,
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
_SELL_LOCKS: dict[str, tuple[asyncio.Lock, int]] = {}


async def sell(address: str, qty_lamports: int, **kwargs) -> dict:
    """Serialize fills per mint, including quote awaits; release idle locks."""
    key = _pick_key_for_entry(address, kwargs.get("token_mint"))
    kwargs = copy.deepcopy(kwargs)
    return await _serialized_paper_order(key, _sell_owned, address, qty_lamports, **kwargs)


async def _serialized_paper_order(key, action, *args, **kwargs):
    """Buy, sell and archive repair share the same per-mint ownership."""
    lock, users = _SELL_LOCKS.get(key, (asyncio.Lock(), 0))
    _SELL_LOCKS[key] = (lock, users + 1)
    try:
        async with lock:
            return await action(*args, **kwargs)
    finally:
        remaining = _SELL_LOCKS[key][1] - 1
        if remaining:
            _SELL_LOCKS[key] = (lock, remaining)
        else:
            del _SELL_LOCKS[key]


def checked_cash_price(address: str, mark, *, expected_position=None) -> float | None:
    """A private policy reference, never a physical token price receipt."""
    entry = _PORTFOLIO.get(address)
    if not isinstance(entry, dict):
        return None
    try:
        from runtime.paper_archive import entry_identity
        owner = "buy:" + (entry_identity(entry) or "")
        if expected_position is not None and not paper_cash_mark.matches_sql(entry, expected_position, token=address):
            return None
        return paper_cash_mark.checked_price(mark, entry, token=address, owner=owner, now=utc_now())
    except (ValueError, TypeError, PaperArchiveError):
        return None


async def get_exit_cash_mark(address: str, *, expected_position=None, quote_func=None, fx_func=None):
    """Observe the exact current remaining quantity under per-mint ownership."""
    async def owned():
        from runtime.paper_archive import entry_identity
        from utils.sol_price import get_sol_usd_observation
        try:
            entry = _PORTFOLIO.get(address)
            owner = "buy:" + (entry_identity(entry) or "") if isinstance(entry, dict) else ""
            frozen = paper_cash_mark.basis(entry, token=address, owner=owner)
            if expected_position is not None and not paper_cash_mark.matches_sql(entry, expected_position, token=address):
                return None
            slippage = jupiter_router.routing_quote_slippage_bps()
            quote = await (quote_func or jupiter_router.get_routing_quote)(
                input_mint=address, output_mint=SOL_MINT, amount_lamports=frozen["remaining_qty"],
                slippage_bps=slippage)
            fx = await (fx_func or get_sol_usd_observation)()
            current = _PORTFOLIO.get(address)
            if paper_cash_mark.basis(current, token=address, owner=owner) != frozen:
                return None
            if expected_position is not None and not paper_cash_mark.matches_sql(current, expected_position, token=address):
                return None
            mark = paper_cash_mark.capture(current, quote, fx, token=address, owner=owner,
                                           now=utc_now(), slippage_bps=slippage)
            return mark if checked_cash_price(address, mark, expected_position=expected_position) is not None else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("[papertrading] cash mark unavailable (%s)", type(exc).__name__)
            return None
    return await _serialized_paper_order(address, owned)


def record_cash_observation(address: str, mark, *, expected_position=None) -> bool:
    """Persist original quote/FX clocks with quote-based peaks, not spot peaks."""
    price = checked_cash_price(address, mark, expected_position=expected_position)
    if price is None:
        return False
    original = _PORTFOLIO[address]
    entry = copy.deepcopy(original)
    # A spot/mixed peak cannot acquire quote provenance by relabelling it.
    if entry.get("peak_valuation_basis") != paper_cash_mark.VERSION:
        fields = ("highest_pnl_pct", "max_pnl_pct_seen", "peak_pnl_pct", "max_adverse_pnl_pct",
                  "peak_price", "peak_price_usd", "time_to_peak_sec", "peak_after_partial_pct")
        entry["legacy_market_peak_diagnostic"] = {"role": "unverified_valuation_only",
            "values": {name: value for name in fields
                if isinstance((value := entry.get(name)), (int, float))
                and not isinstance(value, bool) and math.isfinite(value)}}
        for name in ("highest_pnl_pct", "max_pnl_pct_seen", "peak_pnl_pct", "max_adverse_pnl_pct"):
            entry[name] = 0.0
        entry["peak_price"] = entry["peak_price_usd"] = float(entry["buy_price_usd"])
        entry["time_to_peak_sec"] = entry["peak_after_partial_pct"] = None
        for name in ("cash_peak_mark", "cash_max_adverse_mark", "cash_peak_observed_at", "cash_total_peak_mark"):
            entry.pop(name, None)
    peak_before = float(entry.get("highest_pnl_pct") or 0.0)
    adverse_before = float(entry.get("max_adverse_pnl_pct") or 0.0)
    entry["last_cash_mark"] = mark.to_dict() if isinstance(mark, paper_cash_mark.PaperCashMark) else copy.deepcopy(mark)
    entry["peak_valuation_basis"] = paper_cash_mark.VERSION
    previous_total_peak = entry.get("cash_total_peak_mark")
    if previous_total_peak is not None:
        previous_total_peak = paper_cash_mark.public_historical_mark(previous_total_peak, entry,
            token=address, owner="buy:" + (entry_identity(entry) or ""))
        if (dt.datetime.fromisoformat(previous_total_peak["valued_at"])
                > dt.datetime.fromisoformat(entry["last_cash_mark"]["valued_at"])):
            return False  # Do not rewind a durably observed total peak.
    if (previous_total_peak is None or entry["last_cash_mark"]["values"]["estimated_total_liquidation_net_pnl_usd"]
            > previous_total_peak["values"]["estimated_total_liquidation_net_pnl_usd"]):
        # Start at the first checked net observation, including a negative one.
        # Neither an old spot peak nor a partial fill can manufacture this peak.
        entry["cash_total_peak_mark"] = copy.deepcopy(entry["last_cash_mark"])
    exit_policy.update_exit_state(entry, pnl_pct=(price / float(entry["buy_price_usd"]) - 1) * 100)
    entry["peak_price"] = max(float(entry.get("peak_price") or entry["buy_price_usd"]), price)
    entry["peak_price_usd"] = entry["peak_price"]
    if entry["highest_pnl_pct"] > peak_before:
        entry["cash_peak_mark"] = copy.deepcopy(entry["last_cash_mark"])
        valued = dt.datetime.fromisoformat(entry["last_cash_mark"]["valued_at"])
        opened = dt.datetime.fromisoformat(entry["last_cash_mark"]["basis"]["opened_at"])
        entry["cash_peak_observed_at"] = valued.isoformat()
        entry["time_to_peak_sec"] = max(0, int((valued - opened).total_seconds()))
        if entry.get("partial_taken"):
            entry["peak_after_partial_pct"] = max(float(entry.get("peak_after_partial_pct") or 0.0),
                                                   entry["highest_pnl_pct"])
    if entry["max_adverse_pnl_pct"] < adverse_before:
        entry["cash_max_adverse_mark"] = copy.deepcopy(entry["last_cash_mark"])
    _PORTFOLIO[address] = entry
    try:
        _save(strict=True)
    except BaseException:
        _PORTFOLIO[address] = original
        raise
    original.clear()
    original.update(entry)
    _PORTFOLIO[address] = original
    return True


def sync_cash_peak_metrics(address: str, mark, position) -> bool:
    """Copy durably owned cash peaks; never merge an old SQL spot peak."""
    if checked_cash_price(address, mark, expected_position=position) is None:
        return False
    entry = _PORTFOLIO[address]
    receipt = mark.to_dict() if isinstance(mark, paper_cash_mark.PaperCashMark) else mark
    if (entry.get("peak_valuation_basis") != paper_cash_mark.VERSION
            or entry.get("last_cash_mark") != receipt):
        return False
    changed = False
    for name in ("highest_pnl_pct", "max_pnl_pct_seen", "max_adverse_pnl_pct", "exit_state",
                 "peak_price", "peak_price_usd", "time_to_peak_sec", "peak_after_partial_pct"):
        if hasattr(position, name) and getattr(position, name) != entry.get(name):
            setattr(position, name, entry.get(name))
            changed = True
    return changed


def cash_exit_context(address: str, *, expected_position=None):
    """Checked current/total-peak cash for the real exit consumer, or unknown."""
    entry = _PORTFOLIO.get(address)
    mark = entry.get("last_cash_mark") if isinstance(entry, dict) else None
    if checked_cash_price(address, mark, expected_position=expected_position) is None:
        return paper_cash_mark.PaperCashProtection()
    return paper_cash_mark.protection_context(entry, mark, entry.get("cash_total_peak_mark"),
        token=address, owner="buy:" + (entry_identity(entry) or ""), now=utc_now())


def _persist_sell_fill(key, original, entry, response, total_qty):
    entry.setdefault("exit_fill_events", []).append({
        "intent_id": response["exit_intent_id"], "qty_before": total_qty,
        "response": copy.deepcopy(response),
    })
    if (response["partial"] and entry.get("partial_fill_events") == 1
            and entry.get("dry_run") is True and getattr(CFG, "PAPER_RUNNER_RESEARCH_ENABLED", False) is True
            and "runner_research_source" not in entry):
        entry["first_partial_exit_intent_id"] = response["exit_intent_id"]
        try:
            from runtime.runner_enrollment import capture_source
            entry["runner_research_source"] = capture_source(entry, captured_at=dt.datetime.fromisoformat(response["filled_at"]))
        except Exception as exc:
            entry["runner_research_capture_failed"] = {"captured_at": response["filled_at"],
                "runner_trailing_policy": entry.get("runner_trailing_policy")}
            log.error("[runner_forward] original first-partial capture failed: %s", type(exc).__name__)
    _PORTFOLIO[key] = entry
    try:
        _save(strict=True)
    except BaseException:
        _PORTFOLIO[key] = original
        raise
    # Preserve references used by callers, but only after the durable write.
    original.clear()
    original.update(entry)
    _PORTFOLIO[key] = original


async def _sell_owned(
    address: str,
    qty_lamports: int,
    *,
    token_mint: str | None = None,
    price_hint: float | None = None,
    price_source_hint: str | None = None,
    exit_reason: str | None = None,
    exit_intent_id: str | None = None,
    partial_ladder_plan: dict | None = None,
) -> dict:
    """
    Vende (simulado) una cantidad del token. Si `qty_lamports` es menor que el tamaño
    restante, realiza una **venta parcial** (no cierra posición). Si es mayor o igual,
    cierra completamente.

    Retorna dict con: signature, price_used_usd, price_source_close, partial, qty_sold, qty_left.
    """
    key = _pick_key_for_entry(address, token_mint)
    entry = _PORTFOLIO.get(key)
    if type(qty_lamports) is not int or not 0 < qty_lamports <= 2**63 - 1:
        return {"ok": False, "error": "INVALID_QUANTITY", "signature": None}
    if entry and exit_intent_id:
        previous = [event for event in entry.get("exit_fill_events", [])
                    if event.get("intent_id") == exit_intent_id]
        if previous:
            if len(previous) != 1 or previous[0]["response"]["qty_sold"] != qty_lamports:
                raise PaperPortfolioError("Paper sell intent conflicts with an earlier fill")
            if entry.get("closed_archive_pending") is True:
                try:
                    _archive_closed(key)
                except PaperArchiveError:
                    log.warning("[papertrading] completed fill remains pending archival")
            return copy.deepcopy(previous[0]["response"])
    if not entry or entry.get("closed"):
        raise RuntimeError(f"No hay posición activa para {address[:4]}")
    entry = _ensure_entry_accounting(entry)
    if await _ensure_entry_notional_async(entry) <= 0:
        return {"ok": False, "error": "ENTRY_BASIS_UNAVAILABLE", "signature": None,
                "qty_sold": 0, "qty_left": int(entry.get("qty_lamports") or 0)}

    if not _is_solana_address(key):
        log.error("[papertrading] Venta bloqueada: address no Solana %r", key)
        return {"ok": False, "signature": None, "error": "INVALID_ADDRESS", "price_used_usd": None, "price_source_close": None}

    total_qty = int(entry.get("qty_lamports", 0))
    if qty_lamports > total_qty:
        return {"ok": False, "error": "INVALID_QUANTITY", "signature": None}
    take_qty = qty_lamports
    if take_qty <= 0:
        sig = f"SIM-{int(time.time()*1e3)}"
        log.info("[papertrading] sell qty=0 — nada que hacer")
        return {"ok": False, "error": "NO_QTY", "signature": None, "price_used_usd": None, "price_source_close": None}

    # 1) Resolver precio de cierre (prioriza hint)
    if entry.get("entry_route_quote"):
        price_now, price_src = None, None
    else:
        price_now, price_src = await _resolve_close_price_usd(
            token_mint=key, price_hint=price_hint, price_source_hint=price_source_hint,
        )

    cost_model = entry.get("execution_cost_model")
    sol_usd = await get_sol_usd() if cost_model else None
    exit_route_quote = None
    if entry.get("entry_route_quote"):
        # Price data alone does not prove exit liquidity. Quote the exact raw
        # token quantity being sold, never the original SOL input amount.
        try:
            from execution.quote_receipt import capture_summary
            slippage = jupiter_router.routing_quote_slippage_bps()
            research_quote_started_at = utc_now()
            quote = await jupiter_router.get_routing_quote(input_mint=key, output_mint=SOL_MINT,
                amount_lamports=take_qty, slippage_bps=slippage)
            exit_route_quote = capture_summary(quote, input_mint=key, output_mint=SOL_MINT,
                amount=take_qty, slippage=slippage, limit=entry["entry_route_quote"]["max_impact_pct"], now=utc_now())
            valid = True
        except Exception:
            valid = False
        if not valid or not _positive_finite(sol_usd):
            return {"ok": False, "error": "EXIT_QUOTE_UNAVAILABLE", "signature": None,
                    "qty_sold": 0, "qty_left": total_qty}
        # Re-read the shared FX contract after the quote await. A cache hit
        # preserves its original clocks; an expired/failed refresh is unknown.
        sol_usd = await get_sol_usd()
        try:
            exit_route_quote = capture_summary(quote, input_mint=key, output_mint=SOL_MINT,
                amount=take_qty, slippage=slippage, limit=entry["entry_route_quote"]["max_impact_pct"], now=utc_now())
        except Exception:
            valid = False
        if not valid or not _positive_finite(sol_usd):
            return {"ok": False, "error": "EXIT_QUOTE_UNAVAILABLE", "signature": None,
                    "qty_sold": 0, "qty_left": total_qty}
        proceeds_usd = quote.out_amount / 1e9 * float(sol_usd)
        try:
            runner_forward.observe_quote(key, quote, float(sol_usd), root=_research_root(), cfg=CFG,
                quote_started_at=research_quote_started_at)
            from research_loop import entry_gate_forward
            entry_gate_forward.observe_quote(key, quote, float(sol_usd), root=_research_root(), cfg=CFG,
                quote_started_at=research_quote_started_at)
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
    # Financial changes are isolated until the atomic portfolio write succeeds.
    original_entry = entry
    entry = copy.deepcopy(entry)
    if cost_model:
        price_now = float(price_now) * (1 - float(cost_model["slippage_bps"]) / 10000)
        if not _positive_finite(sol_usd):
            return {"ok": False, "error": "FEE_VALUATION_UNAVAILABLE", "signature": None, "qty_sold": 0, "qty_left": total_qty}
        entry["estimated_fees_usd"] = float(entry.get("estimated_fees_usd") or 0) + float(cost_model["fee_sol_per_fill"]) * float(sol_usd)
        entry["estimated_fees_sol"] = float(entry.get("estimated_fees_sol") or 0) + float(cost_model["fee_sol_per_fill"])
        entry["execution_fill_count"] = int(entry.get("execution_fill_count") or 1) + 1
    price_confidence_close = price_service.price_confidence_from_source(price_src, price_now)

    intent_id = exit_intent_id or uuid.uuid4().hex
    sig = f"SIM-EXIT-{intent_id}"
    filled_at = utc_now().isoformat()
    response = {"ok": True, "signature": sig, "venue": "paper",
                "price_used_usd": float(price_now), "price_source_close": price_src,
                "price_confidence_close": price_confidence_close, "qty_sold": take_qty,
                "qty_left": total_qty - take_qty, "partial": take_qty < total_qty,
                "filled_at": filled_at, "exit_intent_id": intent_id}
    if exit_route_quote is not None:
        response["exit_route_quote"] = exit_route_quote
        response["quote_sol_usd"] = float(sol_usd)

    # 3) Parcial vs cierre total
    if take_qty < total_qty:
        supplied_plan = partial_ladder_plan
        try:
            buy_price_for_plan = float(entry.get("buy_price_usd") or 0.0)
            pnl_pct_for_plan = (
                ((float(price_now) - buy_price_for_plan) / buy_price_for_plan) * 100.0
                if buy_price_for_plan > 0 and price_now > 0
                else 0.0
            )
            if entry.get("peak_valuation_basis") != paper_cash_mark.VERSION:
                exit_policy.update_exit_state(entry, pnl_pct=pnl_pct_for_plan)
            partial_ladder_plan = supplied_plan if supplied_plan is not None else exit_policy.partial_ladder_plan(entry, pnl_pct_for_plan)
        except Exception:
            partial_ladder_plan = supplied_plan
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
        entry["first_partial_at"] = entry.get("first_partial_at") or filled_at
        entry["last_partial_at"] = filled_at
        entry["last_partial_qty"] = int(take_qty)
        entry["last_partial_price_usd"] = float(price_now)
        entry["price_source_close"] = price_src  # guardamos fuente de la última acción
        entry["price_confidence_close"] = price_confidence_close
        entry["exit_reason"] = exit_reason or "partial_tp"
        _update_net_costs(entry, closing=False)
        _persist_sell_fill(key, original_entry, entry, response, total_qty)
        try:
            if entry.get("runner_research_source"):
                from runtime.runner_enrollment import register_source
                register_source(entry["runner_research_source"], root=_research_root(), cfg=CFG)
        except Exception as exc:
            # A research failure cannot turn a completed fill into a failed sell.
            log.warning("[runner_forward] enrollment unavailable: %s", type(exc).__name__)
        log.info(
            "📝 PAPER-PARTIAL %s…  qty=%d/%d  px=%.6f USD  src=%s  sig=%s  reason=%s",
            key[:4], take_qty, total_qty, price_now, price_src, sig, entry["exit_reason"]
        )
        return response

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
            "closed_at": filled_at,
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
            "closed_archive_pending": True,
        }
    )
    _update_net_costs(entry, closing=True)
    if cost_model:
        total_proceeds_sol = float(entry.get("realized_proceeds_sol") or 0) + (totals.total_proceeds_usd - float(entry.get("realized_proceeds_usd") or 0)) / float(sol_usd)
        entry["net_total_pnl_sol"] = total_proceeds_sol - float(entry["amount_sol"]) - float(entry["estimated_fees_sol"])
        entry["total_proceeds_sol"] = total_proceeds_sol
    _persist_sell_fill(key, original_entry, entry, response, total_qty)
    # A durable per-entry cell replaces fragile append-only writes. Existing
    # JSONL history stays untouched and is still read by the evidence readers.
    try:
        _archive_closed(key)
    except PaperArchiveError as exc:
        log.error("[papertrading] CLOSED_TRADE_ARCHIVE_PENDING: %s", type(exc).__name__)

    log.info(
        "📝 PAPER-SELL %s…  close=%.6f USD  PnL=%.2f%%  src=%s  sig=%s  reason=%s",
        key[:4], price_now, totals.total_pnl_pct, price_src, sig, entry["exit_reason"]
    )
    return response


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
    quoted = bool(entry.get("entry_route_quote")) or entry.get("quantity_basis") == "quoted_raw_spl_units"
    if quoted:
        mark = await get_exit_cash_mark(address)
        if mark is not None and record_cash_observation(address, mark):
            price = checked_cash_price(address, mark)
        else:
            price = None
        # A new buy or close during an awaited probe must not inherit an exit.
        if _PORTFOLIO.get(address) is not entry or entry.get("closed"):
            return False
    else:
        entry = _ensure_entry_accounting(entry)
        price_val = await price_service.get_price_usd(address, use_gt=True, critical=True)
        price = float(price_val) if _positive_finite(price_val) else None
        if _PORTFOLIO.get(address) is not entry or entry.get("closed"):
            return False

    buy_price = float(entry.get("buy_price_usd") or 0.0)
    peak_price = float(entry.get("peak_price") or buy_price)

    # Actualiza pico sólo si hay precio válido
    if not quoted and price is not None and price > peak_price:
        entry["peak_price"] = peak_price = price
        _save()

    pnl_pct = (((price - buy_price) / buy_price) * 100.0) if (buy_price > 0.0 and price is not None) else None
    state_before = (
        entry.get("highest_pnl_pct"),
        entry.get("max_pnl_pct_seen"),
        entry.get("max_adverse_pnl_pct"),
        entry.get("exit_state"),
    )
    if not quoted and pnl_pct is not None:
        exit_policy.update_exit_state(entry, pnl_pct=float(pnl_pct))
    if (
        entry.get("highest_pnl_pct"),
        entry.get("max_pnl_pct_seen"),
        entry.get("max_adverse_pnl_pct"),
        entry.get("exit_state"),
    ) != state_before:
        _save()

    if pnl_pct is not None and exit_policy.should_take_partial(entry, pnl_pct):
        frac = exit_policy.partial_sell_fraction(entry, pnl_pct)
        qty = _compute_partial_qty(entry, frac)
        if qty > 0:
            plan = exit_policy.partial_ladder_plan(entry, pnl_pct)
            result = await sell(address, qty, token_mint=address, exit_reason="tp_partial", partial_ladder_plan=plan)
            if result.get("ok") is False:
                return False
            log.info("[papertrading] Partial TP @ %.2f%% → vendidas ~%.0f%%", pnl_pct, frac * 100.0)
            return True

    exit_reason = exit_policy.should_exit(
        entry,
        price,
        utc_now(),
        pnl_pct=pnl_pct,
        cash_context=cash_exit_context(address) if entry.get("quantity_basis") == "quoted_raw_spl_units" else None,
    )
    if exit_reason is None:
        return False

    result = await sell(address, int(entry.get("qty_lamports", 0)), token_mint=address, exit_reason=str(exit_reason).lower())
    if result.get("ok") is False:
        return False
    log.info("[papertrading] %s @ %s%% → cierre total", exit_reason,
             f"{pnl_pct:.2f}" if pnl_pct is not None else "unknown")
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
