# fetcher/jupiter_router.py
from __future__ import annotations

import aiohttp
import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Mapping, Union

log = logging.getLogger("jupiter_router")

# ───────────────────────── Config ─────────────────────────
# Nota:
# - Tu .env actual usa v6 quote: https://quote-api.jup.ag/v6/quote (sin API key).
# - Para ejecutar swaps, si no defines JUP_SWAP_URL, derivamos automáticamente:
#     .../v6/quote  -> .../v6/swap
# - Si quieres usar el endpoint moderno, puedes setear:
#     JUP_QUOTE_URL=https://api.jup.ag/swap/v1/quote
#     JUP_SWAP_URL=https://api.jup.ag/swap/v1/swap
#   y opcionalmente JUP_API_KEY (header x-api-key).
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


def _to_float(x: Any) -> Optional[float]:
    try:
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, str) and x.strip() != "":
            return float(x)
    except Exception:
        return None
    return None


def _extract_price_impact_bps(data: Dict[str, Any]) -> Optional[float]:
    """
    Intenta extraer `priceImpactPct` de varias estructuras y lo convierte a bps.
    Robusto ante:
      - strings ("0.000128")
      - float/int
    Heurística:
      - si value <= 1.0 → se interpreta como fracción (1.0=100%) → bps = v * 10000
      - si value  > 1.0 → se interpreta como "porcentaje" (p.ej. 3.2=3.2%) → bps = v * 100
    """
    impact = None

    # Top-level
    impact = _to_float(data.get("priceImpactPct"))

    # routePlan[0].swapInfo.priceImpactPct
    if impact is None:
        try:
            rp0 = (data.get("routePlan") or [])[0]
            si = rp0.get("swapInfo") or {}
            impact = _to_float(si.get("priceImpactPct"))
        except Exception:
            pass

    # routes[0].priceImpactPct
    if impact is None:
        try:
            routes = data.get("routes") or []
            if routes:
                impact = _to_float(routes[0].get("priceImpactPct"))
        except Exception:
            pass

    if impact is None:
        return None

    # Conversión a bps
    if impact <= 1.0:
        return float(impact) * 10000.0
    return float(impact) * 100.0


def _extract_amounts(data: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """
    Devuelve (inAmount, outAmount) como enteros si están presentes.
    Jupiter suele devolver `inAmount`/`outAmount` como strings.
    """
    def _to_int(x) -> Optional[int]:
        try:
            if isinstance(x, str):
                # puede venir "123" o "123.0" en algunos casos raros
                if x.isdigit():
                    return int(x)
                return int(float(x))
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


def _route_len(data: Dict[str, Any]) -> int:
    try:
        rp = data.get("routePlan")
        if isinstance(rp, list):
            return len(rp)
    except Exception:
        pass
    # algunas variantes usan "routes"
    try:
        r = data.get("routes")
        if isinstance(r, list):
            return len(r)
    except Exception:
        pass
    return 0


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
    if not input_mint or not output_mint:
        return QuoteResult(False, None, None, None, {}, {"error": "missing mints"})

    if amount_lamports is None and amount_tokens is not None:
        amount_lamports = amount_tokens

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

    if not isinstance(amount_lamports, int) or amount_lamports <= 0:
        return QuoteResult(False, None, None, None, {}, {"error": "non-positive amount"})

    try:
        slippage = int(DEFAULT_SLIPPAGE_BPS if slippage_bps is None else slippage_bps)
    except Exception:
        slippage = DEFAULT_SLIPPAGE_BPS

    raw_params: Dict[str, Any] = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount_lamports,
        "slippageBps": slippage,
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

        impact_bps = _extract_price_impact_bps(data)
        in_amt, out_amt = _extract_amounts(data)
        rlen = _route_len(data)

        other = {
            "slippageBps": slippage,
            "onlyDirectRoutes": bool(only_direct_routes),
            "routePlan_len": rlen,
            "contextSlot": data.get("contextSlot"),
        }

        # ok: hay ruta/cotización (aunque no tengamos impact)
        ok = bool(rlen > 0 or (out_amt is not None and out_amt > 0))
        return QuoteResult(ok, impact_bps, in_amt, out_amt, other, data)

    # 1) intento principal
    qr = await _do(JUP_QUOTE_URL)

    # 2) fallback automático cruzado (v6 <-> v1) si el principal no devuelve ruta
    if qr.ok:
        return qr

    q = (JUP_QUOTE_URL or "").strip().lower()
    fallbacks: list[str] = []
    if "api.jup.ag" in q:
        fallbacks.append(_LITE_QUOTE_URL)
    elif "lite-api.jup.ag" in q:
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
