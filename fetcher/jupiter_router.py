# fetcher/jupiter_router.py
from __future__ import annotations

import aiohttp
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Mapping

log = logging.getLogger("jupiter_router")

# ───────────────────────── Config ─────────────────────────
JUP_QUOTE_URL = os.getenv("JUP_QUOTE_URL", "https://quote-api.jup.ag/v6/quote")
TIMEOUT_S = float(os.getenv("JUP_QUOTE_TIMEOUT", "6.0"))

# Slippage para la *cotización* (no ejecuta swap). 100 bps = 1%.
# Nota: esto NO limita la futura ejecución; solo afecta a la respuesta del quote.
try:
    DEFAULT_SLIPPAGE_BPS = int(os.getenv("JUP_QUOTE_SLIPPAGE_BPS", "100"))  # 1.00%
except Exception:
    DEFAULT_SLIPPAGE_BPS = 100

# Para conveniencia con SOL (wsSOL mint):
SOL_MINT = "So11111111111111111111111111111111111111112"


# ─────────────────────── Data Models ───────────────────────
@dataclass
class QuoteResult:
    ok: bool
    price_impact_bps: Optional[float]
    in_amount: Optional[int]       # cantidad de entrada (lamports del input token)
    out_amount: Optional[int]      # cantidad de salida (enteros del output token)
    other: Dict[str, Any]          # campos útiles (slippageBps efectivo, routePlan, etc.)
    raw: Dict[str, Any]            # payload completo de Jupiter para auditoría


# ─────────────────────── Helpers internos ───────────────────────
def _normalize_query_params(d: Mapping[str, Any]) -> dict[str, str]:
    """
    Convierte un dict arbitrario a un dict apto para query string:
    - None -> se omite
    - False -> se omite (Jupiter suele tratar ausencia == false)
    - True -> "true"
    - list/tuple/set -> "a,b,c"
    - int/float/str -> str(v)
    - otros -> str(v)
    """
    out: dict[str, str] = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):
            if v:
                out[k] = "true"
            else:
                continue  # omitimos False
        elif isinstance(v, (int, float, str)):
            out[k] = str(v)
        elif isinstance(v, (list, tuple, set)):
            out[k] = ",".join(str(x) for x in v)
        else:
            out[k] = str(v)
    return out


def _extract_price_impact_bps(data: Dict[str, Any]) -> Optional[float]:
    """
    Intenta extraer `priceImpactPct` de varias estructuras y lo convierte a bps.
    Retorna None si no se pudo determinar.
    """
    impact_pct = None

    # Top-level v6
    if isinstance(data.get("priceImpactPct"), (int, float)):
        impact_pct = float(data["priceImpactPct"])

    # routePlan[0].swapInfo.priceImpactPct (caso común en v6)
    if impact_pct is None:
        try:
            rp0 = (data.get("routePlan") or [])[0]
            si = rp0.get("swapInfo") or {}
            v = si.get("priceImpactPct")
            if isinstance(v, (int, float)):
                impact_pct = float(v)
        except Exception:
            pass

    # routes[0].priceImpactPct (algunas variantes)
    if impact_pct is None:
        try:
            routes = data.get("routes") or []
            if routes and isinstance(routes[0].get("priceImpactPct"), (int, float)):
                impact_pct = float(routes[0]["priceImpactPct"])
        except Exception:
            pass

    if impact_pct is None:
        return None
    # 1% = 100 bps
    return impact_pct * 100.0


def _extract_amounts(data: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """
    Devuelve (inAmount, outAmount) como enteros si están presentes.
    Jupiter v6 suele devolver `inAmount`/`outAmount` como strings.
    """
    def _to_int(x) -> Optional[int]:
        try:
            if isinstance(x, str):
                return int(x)
            if isinstance(x, (int, float)):
                return int(x)
        except Exception:
            return None
        return None

    in_amount = _to_int(data.get("inAmount"))
    out_amount = _to_int(data.get("outAmount"))

    # Fallbacks por si vienen dentro de routePlan[0].swapInfo
    if in_amount is None or out_amount is None:
        try:
            rp0 = (data.get("routePlan") or [])[0]
            si = rp0.get("swapInfo") or {}
            in_amount = in_amount if in_amount is not None else _to_int(si.get("inAmount"))
            out_amount = out_amount if out_amount is not None else _to_int(si.get("outAmount"))
        except Exception:
            pass

    return in_amount, out_amount


# ─────────────────────── API pública ───────────────────────
async def get_quote(
    *,
    input_mint: str,
    output_mint: str,
    amount_sol: float | None = None,
    amount_lamports: int | None = None,
    slippage_bps: Optional[int] = None,
    only_direct_routes: bool = False,
) -> QuoteResult:
    """
    Pide una *cotización* a Jupiter (NO ejecuta swap) para poder medir impacto/slippage.

    Parámetros:
      - input_mint / output_mint: mints SPL
      - amount_sol: si input es SOL, puedes dar la cantidad en SOL directamente
      - amount_lamports: cantidad exacta en unidades del token de entrada
      - slippage_bps: slippage base para la cotización (por defecto 100 bps = 1%)
      - only_direct_routes: restringe a rutas directas (opcional)

    Retorna:
      QuoteResult con:
        ok, price_impact_bps, in_amount, out_amount, other{slippageBps, routePlan…}, raw
    """
    if not input_mint or not output_mint:
        return QuoteResult(False, None, None, None, {}, {"error": "missing mints"})

    # Normaliza amount
    if amount_lamports is None:
        if amount_sol is None:
            return QuoteResult(False, None, None, None, {}, {"error": "missing amount"})
        # Convertimos SOL → lamports solo si el input es SOL
        if input_mint != SOL_MINT:
            return QuoteResult(False, None, None, None, {}, {"error": "amount_lamports required for non-SOL inputs"})
        try:
            amount_lamports = int(max(0.0, float(amount_sol)) * 1_000_000_000)
        except Exception:
            return QuoteResult(False, None, None, None, {}, {"error": "invalid amount_sol"})

    if amount_lamports <= 0:
        return QuoteResult(False, None, None, None, {}, {"error": "non-positive amount"})

    try:
        slippage = int(DEFAULT_SLIPPAGE_BPS if slippage_bps is None else slippage_bps)
    except Exception:
        slippage = DEFAULT_SLIPPAGE_BPS

    # Construcción de parámetros (con booleans "crudos")
    raw_params: Dict[str, Any] = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount_lamports,
        "slippageBps": slippage,
        "onlyDirectRoutes": bool(only_direct_routes),
        # Mejor omitir flags falsos que enviar "false"
        "asLegacyTransaction": False,
    }

    # Normaliza a tipos admitidos por yarl (sin bools/None)
    params = _normalize_query_params(raw_params)

    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(JUP_QUOTE_URL, params=params) as resp:
                if resp.status != 200:
                    body = {}
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        pass
                    log.debug("[jupiter_router] non-200 (%s): %s", resp.status, body or "<no-body>")
                    return QuoteResult(False, None, None, None, {"status": resp.status}, body or {"status": resp.status})
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.debug("[jupiter_router] HTTP error: %s", e)
        return QuoteResult(False, None, None, None, {"error": str(e)}, {"error": str(e)})
    except Exception as e:
        log.exception("[jupiter_router] unexpected: %s", e)
        return QuoteResult(False, None, None, None, {"error": str(e)}, {"error": str(e)})

    impact_bps = _extract_price_impact_bps(data)
    in_amt, out_amt = _extract_amounts(data)

    other = {
        "slippageBps": slippage,
        "onlyDirectRoutes": bool(only_direct_routes),
        "routePlan_len": len(data.get("routePlan", []) or []),
        "contextSlot": data.get("contextSlot"),
    }

    # Consideramos sonda útil si tenemos impacto
    ok = impact_bps is not None
    return QuoteResult(ok, impact_bps, in_amt, out_amt, other, data)


# (Opcional) stub para ejecución futura — NO implementado en este stub.
async def execute_swap(*args, **kwargs):
    """
    Placeholder de ejecución real.
    Este stub no ejecuta swaps. Si en el futuro quieres soportarlo, aquí
    implementamos la llamada a /swap o integras tu ejecutor (gmgn, etc.).
    """
    raise NotImplementedError("execute_swap no implementado en jupiter_router stub")


__all__ = ["get_quote", "QuoteResult", "SOL_MINT", "execute_swap"]
