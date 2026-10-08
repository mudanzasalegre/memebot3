# fetcher/jupiter_router.py
from __future__ import annotations

import aiohttp
import asyncio
import base64
import copy
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Mapping, Union
from urllib.parse import urlsplit
from utils.raw_units import U64_MAX as _U64_MAX, raw_uint as _raw_uint, sol_to_lamports

log = logging.getLogger("jupiter_router")

# ───────────────────────── Config ─────────────────────────
# Quote-only Metis v1 adapter. Legacy v6 settings resolve to v1. A checked
# response proves this HTTP quote's identity, not market freshness or a fill.
_API_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
_LITE_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
_API_SWAP_URL = "https://api.jup.ag/swap/v1/swap"
_LITE_SWAP_URL = "https://lite-api.jup.ag/swap/v1/swap"
_ORDER_URL = "https://api.jup.ag/ultra/v1/order"
_EXECUTE_URL = "https://api.jup.ag/ultra/v1/execute"

# API key opcional (para api.jup.ag)
JUP_API_KEY = os.getenv("JUP_API_KEY", "").strip()
JUP_MANAGED_ENABLED = os.getenv("JUP_MANAGED_ENABLED", "true").strip().lower() == "true"


def _is_legacy_quote_url(url: str | None) -> bool:
    q = (url or "").strip().lower()
    return "quote-api.jup.ag" in q or "/v6/quote" in q


def _is_legacy_swap_url(url: str | None) -> bool:
    q = (url or "").strip().lower()
    return "quote-api.jup.ag" in q or "/v6/swap" in q


def _preferred_quote_url() -> str:
    raw = (os.getenv("JUP_QUOTE_URL", "") or "").strip()
    if raw and not _is_legacy_quote_url(raw):
        return raw
    return _API_QUOTE_URL if JUP_API_KEY else _LITE_QUOTE_URL


def _preferred_swap_url() -> str:
    raw = (os.getenv("JUP_SWAP_URL", "") or "").strip()
    if raw and not _is_legacy_swap_url(raw):
        return raw
    return _API_SWAP_URL if JUP_API_KEY else _LITE_SWAP_URL


JUP_QUOTE_URL = _preferred_quote_url()
JUP_SWAP_URL = _preferred_swap_url()
JUP_ORDER_URL = (os.getenv("JUP_ORDER_URL", _ORDER_URL) or _ORDER_URL).strip()
JUP_EXECUTE_URL = (os.getenv("JUP_EXECUTE_URL", _EXECUTE_URL) or _EXECUTE_URL).strip()

TIMEOUT_S = float(os.getenv("JUP_QUOTE_TIMEOUT", "6.0"))
SWAP_TIMEOUT_S = float(os.getenv("JUP_SWAP_TIMEOUT", str(TIMEOUT_S)))

# Slippage para la *cotización* (no ejecuta swap). 100 bps = 1%.
try:
    DEFAULT_SLIPPAGE_BPS = int(os.getenv("JUP_QUOTE_SLIPPAGE_BPS", "100"))  # 1.00%
except Exception:
    DEFAULT_SLIPPAGE_BPS = 100

try:
    MANAGED_SLIPPAGE_BPS = int(os.getenv("JUP_MANAGED_SLIPPAGE_BPS", str(DEFAULT_SLIPPAGE_BPS)))
except Exception:
    MANAGED_SLIPPAGE_BPS = DEFAULT_SLIPPAGE_BPS

# Swap settings (ejecución)
# Por compat con tu trader/sol_signer (legacy Transaction), dejamos legacy por defecto.
# Si tu signer soporta VersionedTransaction, puedes ponerlo a false.
_SWAP_AS_LEGACY_DEFAULT = os.getenv("JUP_SWAP_AS_LEGACY", "true").lower() == "true"
_SWAP_WRAP_SOL_DEFAULT = os.getenv("JUP_SWAP_WRAP_SOL", "true").lower() == "true"
_SWAP_DYNAMIC_CU_DEFAULT = os.getenv("JUP_SWAP_DYNAMIC_CU_LIMIT", "true").lower() == "true"
_SWAP_SKIP_PREFLIGHT_DEFAULT = os.getenv("JUP_SWAP_SKIP_PREFLIGHT", "false").lower() == "true"
_SWAP_MAX_RETRIES = int(os.getenv("JUP_SWAP_MAX_RETRIES", "2"))

# Prioritization fee:
# - Puede ser int (lamports) o JSON (dict) si tu endpoint lo acepta.
#   Ejemplos:
#     JUP_PRIORITY_FEE_LAMPORTS=20000
#     JUP_PRIORITY_FEE_LAMPORTS={"priorityLevel":"high","maxLamports":200000}
_PRIORITY_FEE_RAW = os.getenv("JUP_PRIORITY_FEE_LAMPORTS", "").strip()

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
                continue
        elif isinstance(v, (int, float, str)):
            out[k] = str(v)
        elif isinstance(v, (list, tuple, set)):
            out[k] = ",".join(str(x) for x in v)
        else:
            out[k] = str(v)
    return out


_MAX_QUOTE_ROUTE_STEPS = 128


def _extract_price_impact_bps(data: Dict[str, Any]) -> Optional[float]:
    """Official top-level decimal fraction [0, 1]; never guess percent units."""
    value = data.get("priceImpactPct")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str) and (not value or len(value) > 128 or value != value.strip()):
        return None
    try:
        impact = Decimal(str(value))
        if not impact.is_finite() or not 0 <= impact <= 1:
            return None
        return float(impact * 10000)
    except (InvalidOperation, ValueError):
        return None


def _extract_amounts(data: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    # A first-hop amount is not a whole-route amount in a split or multihop.
    return _raw_uint(data.get("inAmount")), _raw_uint(data.get("outAmount"))


def _checked_quote(data: Any, *, input_mint: str, output_mint: str,
                   amount: int, slippage: int, direct: bool) -> QuoteResult:
    """Check one complete response against one request; never merge fallbacks."""
    def invalid(reason: str) -> QuoteResult:
        return QuoteResult(False, None, None, None, {"quote_contract_error": reason},
                           copy.deepcopy(data) if isinstance(data, dict) else {"error": reason})

    if not isinstance(data, dict):
        return invalid("response_not_object")
    if data.get("error") or data.get("errorCode"):
        return invalid("provider_error")
    if data.get("inputMint") != input_mint or data.get("outputMint") != output_mint:
        return invalid("mint_mismatch")
    in_amt, out_amt = _extract_amounts(data)
    if in_amt != amount or out_amt is None or out_amt <= 0:
        return invalid("invalid_whole_route_amounts")
    if data.get("swapMode") != "ExactIn":
        return invalid("swap_mode_mismatch")
    if type(data.get("slippageBps")) is not int or data["slippageBps"] != slippage:
        return invalid("slippage_mismatch")
    threshold = _raw_uint(data.get("otherAmountThreshold"))
    if threshold is None or not 0 < threshold <= out_amt:
        return invalid("invalid_output_threshold")
    impact = _extract_price_impact_bps(data)
    if impact is None:
        return invalid("invalid_price_impact_fraction")
    route = data.get("routePlan")
    if not isinstance(route, list) or not 0 < len(route) <= _MAX_QUOTE_ROUTE_STEPS:
        return invalid("invalid_route_plan")
    edges = []
    for step in route:
        info = step.get("swapInfo") if isinstance(step, dict) else None
        if not isinstance(info, dict):
            return invalid("invalid_swap_info")
        if any(not isinstance(info.get(key), str) or not info[key].strip()
               for key in ("ammKey", "inputMint", "outputMint")):
            return invalid("invalid_swap_identity")
        if any((_raw_uint(info.get(key)) or 0) <= 0 for key in ("inAmount", "outAmount")):
            return invalid("invalid_swap_amounts")
        for key, limit in (("percent", 100), ("bps", 10000)):
            if step.get(key) is not None and (type(step[key]) is not int or not 0 <= step[key] <= limit):
                return invalid("invalid_route_weight")
        edge = (info["inputMint"], info["outputMint"])
        if direct and edge != (input_mint, output_mint):
            return invalid("non_direct_route")
        edges.append(edge)
    # Connectivity only, not invented fee conservation. Multihop percentages
    # need not sum to 100 across the whole plan, and split paths are supported.
    def reachable(start: str, reverse: bool = False) -> set[str]:
        seen = {start}
        for _ in range(len(edges)):
            next_seen = seen | {a if reverse else b for a, b in edges if (b if reverse else a) in seen}
            if next_seen == seen:
                break
            seen = next_seen
        return seen
    forward, backward = reachable(input_mint), reachable(output_mint, True)
    if output_mint not in forward or any(a not in forward or b not in backward for a, b in edges):
        return invalid("disconnected_route")
    slot = data.get("contextSlot")
    if slot is not None and (type(slot) is not int or _raw_uint(slot) is None):
        return invalid("invalid_context_slot")
    other = {"slippageBps": slippage, "onlyDirectRoutes": direct,
             "routePlan_len": len(route), "contextSlot": slot,
             "quote_contract_version": 1,
             "inputMint": input_mint, "outputMint": output_mint,
             "requested_in_amount": amount,
             "received_at_utc": datetime.now(timezone.utc).isoformat(),
             "market_asof_verified": False, "fill_verified": False}
    return QuoteResult(True, impact, in_amt, out_amt, other, copy.deepcopy(data))


def _derive_swap_url() -> str:
    """
    Deriva un swap URL coherente si no se define JUP_SWAP_URL.
    - .../quote -> .../swap
    """
    if JUP_SWAP_URL:
        return JUP_SWAP_URL
    q = (JUP_QUOTE_URL or "").strip().lower()
    if "/quote" in q:
        return q.replace("/quote", "/swap")
    return _API_SWAP_URL if JUP_API_KEY else _LITE_SWAP_URL


def _headers() -> Dict[str, str]:
    h = {
        "accept": "application/json",
        "User-Agent": os.getenv("JUPITER_UA", "MemeBot3/1.0 (+bot)"),
    }
    if JUP_API_KEY:
        h["x-api-key"] = JUP_API_KEY
    return h


def _parse_priority_fee(value: str) -> Optional[Union[int, Dict[str, Any]]]:
    if not value:
        return None
    # int simple
    if value.isdigit():
        try:
            return int(value)
        except Exception:
            return None
    # JSON dict
    try:
        obj = json.loads(value)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, int):
            return obj
    except Exception:
        return None
    return None


# ─────────────────────── API pública ───────────────────────
async def get_quote(
    *,
    input_mint: str,
    output_mint: str,
    amount_sol: float | None = None,
    amount_lamports: int | None = None,
    # alias retro-compat (seller/buyer antiguos)
    amount_tokens: int | None = None,
    slippage_bps: Optional[int] = None,
    only_direct_routes: bool = False,
) -> QuoteResult:
    """
    Pide una *cotización* a Jupiter (NO ejecuta swap) para poder medir impacto/slippage.

    Parámetros:
      - input_mint / output_mint: mints SPL
      - amount_sol: si input es SOL, puedes dar la cantidad en SOL directamente
      - amount_lamports: cantidad exacta en unidades del token de entrada
      - amount_tokens: alias de amount_lamports (compat)
      - slippage_bps: slippage base para la cotización (por defecto 100 bps = 1%)
      - only_direct_routes: restringe a rutas directas (opcional)

    Retorna:
      QuoteResult con:
        ok, price_impact_bps, in_amount, out_amount, other{slippageBps, routePlan…}, raw
    """
    if any(not isinstance(mint, str) or not mint or mint != mint.strip() for mint in (input_mint, output_mint)):
        return QuoteResult(False, None, None, None, {}, {"error": "missing mints"})

    if amount_lamports is not None and amount_tokens is not None:
        if type(amount_lamports) is not int or type(amount_tokens) is not int or amount_lamports != amount_tokens:
            return QuoteResult(False, None, None, None, {}, {"error": "conflicting raw amounts"})
    if amount_sol is not None and (amount_lamports is not None or amount_tokens is not None):
        return QuoteResult(False, None, None, None, {}, {"error": "conflicting amount units"})
    if type(only_direct_routes) is not bool:
        return QuoteResult(False, None, None, None, {}, {"error": "invalid direct route flag"})

    if amount_lamports is None and amount_tokens is not None:
        amount_lamports = amount_tokens

    # Normaliza amount
    if amount_lamports is None:
        if amount_sol is None:
            return QuoteResult(False, None, None, None, {}, {"error": "missing amount"})
        # Convertimos SOL → lamports solo si el input es SOL
        if input_mint != SOL_MINT:
            return QuoteResult(False, None, None, None, {}, {"error": "amount_lamports required for non-SOL inputs"})
        amount_lamports = sol_to_lamports(amount_sol)
        if amount_lamports is None:
            return QuoteResult(False, None, None, None, {}, {"error": "invalid amount_sol"})

    if type(amount_lamports) is not int or not 0 < amount_lamports <= _U64_MAX:
        return QuoteResult(False, None, None, None, {}, {"error": "non-positive amount"})

    slippage = DEFAULT_SLIPPAGE_BPS if slippage_bps is None else slippage_bps
    if type(slippage) is not int or not 0 <= slippage <= 65535:
        return QuoteResult(False, None, None, None, {}, {"error": "invalid slippage_bps"})

    raw_params: Dict[str, Any] = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount_lamports,
        "slippageBps": slippage,
        "swapMode": "ExactIn",
        "onlyDirectRoutes": bool(only_direct_routes),
        # Mantener explícito: en quote v6 existe; en otros endpoints se ignora.
        "asLegacyTransaction": False,
    }

    params = _normalize_query_params(raw_params)

    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)

    async def _do(url: str) -> QuoteResult:
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=_headers()) as sess:
                async with sess.get(url, params=params) as resp:
                    if resp.status != 200:
                        body: Any = None
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            try:
                                body = await resp.text()
                            except Exception:
                                body = None
                        log.debug("[jupiter_router] quote non-200 (%s) url=%s body=%s", resp.status, url, body)
                        return QuoteResult(False, None, None, None, {"status": resp.status}, {"status": resp.status, "body": body})
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.debug("[jupiter_router] quote HTTP error url=%s: %s", url, e)
            return QuoteResult(False, None, None, None, {"error": str(e)}, {"error": str(e)})
        except Exception as e:
            log.exception("[jupiter_router] quote unexpected url=%s: %s", url, e)
            return QuoteResult(False, None, None, None, {"error": str(e)}, {"error": str(e)})

        return _checked_quote(data, input_mint=input_mint, output_mint=output_mint,
                              amount=amount_lamports, slippage=slippage, direct=only_direct_routes)

    # 1) intento principal
    qr = await _do(JUP_QUOTE_URL)

    # 2) Independent v1 fallback; an invalid response is never partial evidence.
    if qr.ok:
        return qr

    try:
        host = urlsplit(JUP_QUOTE_URL or "").hostname
    except ValueError:
        host = None  # Malformed custom URL is unavailable, not a caller crash.
    fallbacks: list[str] = []
    if host == "api.jup.ag":
        fallbacks.append(_LITE_QUOTE_URL)
    elif host == "lite-api.jup.ag":
        if JUP_API_KEY:
            fallbacks.append(_API_QUOTE_URL)
    else:
        fallbacks.append(_preferred_quote_url())
        fallbacks.append(_LITE_QUOTE_URL)
        if JUP_API_KEY:
            fallbacks.append(_API_QUOTE_URL)
    seen = {JUP_QUOTE_URL}
    for fb in fallbacks:
        if not fb or fb in seen:
            continue
        seen.add(fb)
        qr2 = await _do(fb)
        if qr2.ok:
            return qr2

    return qr


async def get_order(
    *,
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    taker: str,
    slippage_bps: int | None = None,
) -> Dict[str, Any]:
    """
    Managed Jupiter order flow (Ultra order/execute).

    Requires `JUP_API_KEY`. Returns the raw order payload with an unsigned
    base64 transaction plus `requestId`.
    """
    if not JUP_MANAGED_ENABLED:
        raise RuntimeError("managed Jupiter execution disabled")
    if not JUP_API_KEY:
        raise RuntimeError("managed Jupiter execution requires JUP_API_KEY")
    if not input_mint or not output_mint or not taker:
        raise RuntimeError("managed Jupiter order missing required fields")
    if int(amount_lamports) <= 0:
        raise RuntimeError("managed Jupiter order requires positive amount_lamports")

    params = _normalize_query_params(
        {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": int(amount_lamports),
            "taker": taker,
            "slippageBps": int(MANAGED_SLIPPAGE_BPS if slippage_bps is None else slippage_bps),
        }
    )
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)

    async with aiohttp.ClientSession(timeout=timeout, headers=_headers()) as sess:
        async with sess.get(JUP_ORDER_URL, params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Jupiter order non-200 ({resp.status}) body={body}")
            data = await resp.json(content_type=None)

    tx_b64 = data.get("transaction")
    request_id = data.get("requestId")
    if not tx_b64 or not request_id:
        error_code = data.get("errorCode")
        error_message = data.get("errorMessage")
        raise RuntimeError(
            f"Jupiter order missing transaction/requestId code={error_code} message={error_message}"
        )
    return data


async def execute_order(*, signed_transaction: str, request_id: str) -> Dict[str, Any]:
    if not JUP_MANAGED_ENABLED:
        raise RuntimeError("managed Jupiter execution disabled")
    if not JUP_API_KEY:
        raise RuntimeError("managed Jupiter execution requires JUP_API_KEY")
    payload = {
        "signedTransaction": signed_transaction,
        "requestId": request_id,
    }
    timeout = aiohttp.ClientTimeout(total=SWAP_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout, headers=_headers()) as sess:
        async with sess.post(JUP_EXECUTE_URL, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Jupiter execute non-200 ({resp.status}) body={body}")
            return await resp.json(content_type=None)


async def execute_managed_swap(
    *,
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    user_public_key: str | None = None,
    slippage_bps: int | None = None,
) -> Dict[str, Any]:
    """
    Full managed order/execute path.

    1. GET order
    2. sign base64 transaction locally
    3. POST execute and return execution metadata
    """
    if not user_public_key:
        try:
            from trader import sol_signer  # type: ignore

            user_public_key = str(getattr(sol_signer, "PUBLIC_KEY", "") or "")
        except Exception:
            user_public_key = ""
    user_public_key = str(user_public_key or os.getenv("SOL_PUBLIC_KEY", "") or "").strip()
    if not user_public_key:
        raise RuntimeError("managed Jupiter execution missing user_public_key")

    order = await get_order(
        input_mint=input_mint,
        output_mint=output_mint,
        amount_lamports=int(amount_lamports),
        taker=user_public_key,
        slippage_bps=slippage_bps,
    )

    tx_b64 = str(order.get("transaction") or "")
    request_id = str(order.get("requestId") or "")
    if not tx_b64 or not request_id:
        raise RuntimeError("managed Jupiter order missing transaction/requestId")

    try:
        from trader import sol_signer  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"sol_signer not available for managed Jupiter execution: {exc}") from exc

    signed_transaction = await asyncio.to_thread(sol_signer.sign_base64_transaction, tx_b64)
    execute_response = await execute_order(
        signed_transaction=signed_transaction,
        request_id=request_id,
    )

    status = str(execute_response.get("status") or "").strip()
    signature = str(execute_response.get("signature") or "")
    if status and status.lower() in {"failed", "error", "expired"}:
        code = execute_response.get("code")
        raise RuntimeError(f"managed Jupiter execute failed status={status} code={code}")
    if not signature:
        raise RuntimeError(f"managed Jupiter execute returned no signature: {execute_response}")

    route_meta = {
        "router": f"jupiter_managed:{order.get('router') or 'managed'}",
        "requestId": request_id,
        "status": status or "unknown",
        "mode": order.get("mode") or order.get("swapMode"),
        "inAmount": order.get("inAmount"),
        "outAmount": order.get("outAmount"),
        "priceImpactPct": order.get("priceImpactPct"),
    }

    return {
        "signature": signature,
        "route": route_meta,
        "order": order,
        "execute": execute_response,
    }


async def execute_swap(
    quote: Union[QuoteResult, Dict[str, Any]],
    *,
    user_public_key: Optional[str] = None,
    wrap_and_unwrap_sol: bool = _SWAP_WRAP_SOL_DEFAULT,
    as_legacy_transaction: bool = _SWAP_AS_LEGACY_DEFAULT,
    dynamic_compute_unit_limit: bool = _SWAP_DYNAMIC_CU_DEFAULT,
    prioritization_fee_lamports: Optional[Union[int, Dict[str, Any]]] = None,
    skip_preflight: bool = _SWAP_SKIP_PREFLIGHT_DEFAULT,
    max_retries: int = _SWAP_MAX_RETRIES,
) -> str:
    """
    Ejecuta un swap real con Jupiter (/swap):
      1) POST /swap con quoteResponse + userPublicKey
      2) decodifica swapTransaction (base64)
      3) firma y envía la transacción
      4) retorna la signature (txid)

    Compatibilidad:
      - Acepta `quote` como QuoteResult o como dict (raw quoteResponse).
      - Firma con tu clave del proyecto (trader/sol_signer.py) si existe.
      - Soporta legacy tx y, si solders lo permite, VersionedTransaction (fallback).

    Requisitos:
      - trader/sol_signer.py correctamente configurado (SOL_PRIVATE_KEY, SOL_RPC_URL).
    """
    # normaliza quoteResponse (dict)
    if isinstance(quote, QuoteResult):
        quote_resp = quote.raw
    elif isinstance(quote, dict):
        quote_resp = quote
    else:
        raise TypeError("execute_swap: quote must be QuoteResult or dict")

    if not isinstance(quote_resp, dict) or not quote_resp:
        raise ValueError("execute_swap: empty quoteResponse")

    # user public key
    if not user_public_key:
        # Intento 1: trader.sol_signer.PUBLIC_KEY
        try:
            from trader import sol_signer  # type: ignore
            pk = getattr(sol_signer, "PUBLIC_KEY", None)
            user_public_key = str(pk) if pk is not None else None
        except Exception:
            user_public_key = None

    if not user_public_key:
        # Intento 2: env SOL_PUBLIC_KEY
        user_public_key = os.getenv("SOL_PUBLIC_KEY", "").strip() or None

    if not user_public_key:
        raise RuntimeError("execute_swap: missing user_public_key (define SOL_PUBLIC_KEY o configura trader/sol_signer)")

    swap_url = _derive_swap_url()

    if prioritization_fee_lamports is None:
        prioritization_fee_lamports = _parse_priority_fee(_PRIORITY_FEE_RAW)

    payload: Dict[str, Any] = {
        "quoteResponse": quote_resp,
        "userPublicKey": user_public_key,
        "wrapAndUnwrapSol": bool(wrap_and_unwrap_sol),
        "asLegacyTransaction": bool(as_legacy_transaction),
        "dynamicComputeUnitLimit": bool(dynamic_compute_unit_limit),
    }
    if prioritization_fee_lamports is not None:
        payload["prioritizationFeeLamports"] = prioritization_fee_lamports

    timeout = aiohttp.ClientTimeout(total=SWAP_TIMEOUT_S)

    last_err: Optional[str] = None
    for attempt in range(max(1, int(max_retries)) + 1):
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=_headers()) as sess:
                async with sess.post(swap_url, json=payload) as resp:
                    if resp.status != 200:
                        body: Any = None
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            try:
                                body = await resp.text()
                            except Exception:
                                body = None
                        last_err = f"swap non-200 ({resp.status}) body={body}"
                        log.debug("[jupiter_router] %s url=%s", last_err, swap_url)
                        # retry suave en 429/5xx
                        if resp.status in (429, 500, 502, 503, 504) and attempt <= max_retries:
                            await asyncio.sleep(0.6 * attempt)
                            continue
                        raise RuntimeError(last_err)

                    data = await resp.json(content_type=None)

            # Jupiter suele devolver swapTransaction en base64
            tx_b64 = (
                data.get("swapTransaction")
                or data.get("swap_transaction")
                or data.get("transaction")
            )
            if not tx_b64 or not isinstance(tx_b64, str):
                raise RuntimeError(f"swap response missing swapTransaction: keys={list(data.keys())}")

            raw_tx = base64.b64decode(tx_b64)

            # Firma y envío
            sig = await _sign_and_send_raw_transaction(raw_tx, skip_preflight=skip_preflight)
            return sig

        except Exception as e:
            last_err = str(e)
            if attempt <= max_retries:
                await asyncio.sleep(0.6 * attempt)
                continue
            break

    raise RuntimeError(f"execute_swap failed after retries: {last_err}")


async def _sign_and_send_raw_transaction(raw_tx: bytes, *, skip_preflight: bool = False) -> str:
    """
    Firma (si hace falta) y envía una transacción raw (bytes) usando:
      - trader/sol_signer.KEYPAIR + trader/sol_signer.client si existe
    Soporta legacy y versioned (si solders expone VersionedTransaction).
    """
    # Carga signer + client del proyecto
    try:
        from trader import sol_signer  # type: ignore
    except Exception as e:
        raise RuntimeError(f"sol_signer not available: {e}")

    keypair = getattr(sol_signer, "KEYPAIR", None)
    client = getattr(sol_signer, "client", None)

    if keypair is None or client is None:
        raise RuntimeError("sol_signer missing KEYPAIR/client (revisa SOL_PRIVATE_KEY / SOL_RPC_URL)")

    # Imports lazy para no romper import-time si faltan deps en ciertos entornos
    try:
        from solana.rpc.types import TxOpts  # type: ignore
    except Exception:
        TxOpts = None  # type: ignore

    # 1) Intentar VersionedTransaction primero (si existe)
    signed_bytes: Optional[bytes] = None
    versioned_used = False

    try:
        from solders.versioned_transaction import VersionedTransaction  # type: ignore

        try:
            vtx = VersionedTransaction.from_bytes(raw_tx)
            # firmar el mensaje
            msg_bytes = bytes(vtx.message)
            sig_obj = keypair.sign_message(msg_bytes)

            # mantener firmas existentes si ya hay placeholders
            try:
                sigs = list(vtx.signatures)
            except Exception:
                sigs = []
            if sigs:
                sigs[0] = sig_obj
            else:
                sigs = [sig_obj]

            vtx_signed = VersionedTransaction.populate(vtx.message, sigs)
            signed_bytes = bytes(vtx_signed)
            versioned_used = True
        except Exception:
            signed_bytes = None
            versioned_used = False
    except Exception:
        signed_bytes = None
        versioned_used = False

    # 2) Fallback legacy Transaction
    if signed_bytes is None:
        try:
            from solders.transaction import Transaction  # type: ignore

            tx = Transaction.from_bytes(raw_tx)
            # firmar (legacy)
            tx.sign([keypair], tx.recent_blockhash)
            signed_bytes = bytes(tx)
        except Exception as e:
            raise RuntimeError(f"failed to decode/sign transaction (versioned={versioned_used}): {e}")

    # 3) Enviar
    try:
        if TxOpts is not None:
            resp = client.send_raw_transaction(signed_bytes, opts=TxOpts(skip_preflight=bool(skip_preflight)))
        else:
            resp = client.send_raw_transaction(signed_bytes)
        sig = getattr(resp, "value", None)
        if sig is None:
            # algunos clients devuelven dict
            if isinstance(resp, dict) and resp.get("result"):
                return str(resp["result"])
            raise RuntimeError(f"send_raw_transaction returned no signature: {resp}")
        return str(sig)
    except Exception as e:
        raise RuntimeError(f"send_raw_transaction failed: {e}")


__all__ = [
    "QuoteResult",
    "SOL_MINT",
    "execute_managed_swap",
    "execute_order",
    "execute_swap",
    "get_order",
    "get_quote",
]
