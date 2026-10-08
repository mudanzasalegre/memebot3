# fetcher/jupiter_router.py
from __future__ import annotations

import aiohttp
import asyncio
import base64
import copy
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Mapping, Union
from urllib.parse import urlsplit
from utils.raw_units import U64_MAX as _U64_MAX, raw_uint as _raw_uint, sol_to_lamports
from runtime.owned_dispatch import run_owned_sync
from execution import jupiter_managed_contract as managed_contract
from execution import chain_reconciliation
from utils import solana_execution
from runtime import execution_provenance
from utils import jupiter_access
from execution.quote_observation import rejection_metadata

log = logging.getLogger("jupiter_router")

# ───────────────────────── Config ─────────────────────────
# Quote-only Metis v1 adapter. Legacy v6 settings resolve to v1. A checked
# response proves this HTTP quote's identity, not market freshness or a fill.
_API_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
_LITE_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
_API_SWAP_URL = "https://api.jup.ag/swap/v1/swap"
_LITE_SWAP_URL = "https://lite-api.jup.ag/swap/v1/swap"
_ORDER_URL = "https://api.jup.ag/swap/v2/order"
_EXECUTE_URL = "https://api.jup.ag/swap/v2/execute"

# API key opcional (para api.jup.ag)
JUP_API_KEY = os.getenv("JUP_API_KEY", "").strip()
JUP_MANAGED_ENABLED = os.getenv("JUP_MANAGED_ENABLED", "true").strip().lower() == "true"
JUP_LEGACY_SWAP_ENABLED = os.getenv("JUP_LEGACY_SWAP_ENABLED", "true").strip().lower() == "true"


def _is_legacy_quote_url(url: str | None) -> bool:
    q = (url or "").strip().lower()
    return "quote-api.jup.ag" in q or "/v6/quote" in q


def _is_legacy_swap_url(url: str | None) -> bool:
    q = (url or "").strip().lower()
    return "quote-api.jup.ag" in q or "/v6/swap" in q


def _preferred_quote_url() -> str:
    raw = (os.getenv("JUP_QUOTE_URL", "") or "").strip()
    try:
        return jupiter_access.endpoint(raw, "quote")
    except ValueError:
        return raw  # Invalid optional transport cannot crash paper imports.


def _preferred_swap_url() -> str:
    raw = (os.getenv("JUP_SWAP_URL", "") or "").strip()
    try:
        return jupiter_access.endpoint(raw, "swap")
    except ValueError:
        return raw  # Execution validates before any HTTP/signing.


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
#     JUP_PRIORITY_FEE_LAMPORTS={"priorityLevelWithMaxLamports":{"priorityLevel":"high","maxLamports":200000}}
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


class SwapPreparationError(RuntimeError):
    """This adapter has not invoked a signing/broadcast execution callable."""


class SwapSubmissionUncertain(RuntimeError):
    """Execution began; do not build/sign/send another order or fall back."""


class _TransientSwapBuild(RuntimeError):
    pass


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


async def _read_route_error(response) -> dict:
    """Bounded, duplicate-free negative envelope; never log response text."""
    chunks, size, maximum = [], 0, 65536
    while True:
        chunk = await response.content.read(min(8192, maximum + 1 - size))
        if not chunk:
            break
        size += len(chunk)
        if size > maximum:
            raise ValueError("Quote rejection exceeds bound")
        chunks.append(chunk)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Ambiguous quote rejection")
            result[key] = value
        return result

    return json.loads(b"".join(chunks), object_pairs_hook=unique)


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
        return jupiter_access.endpoint(JUP_SWAP_URL, "swap")
    q = (JUP_QUOTE_URL or "").strip().lower()
    if "/quote" in q:
        return jupiter_access.endpoint(q.replace("/quote", "/swap"), "swap")
    return _API_SWAP_URL


def _headers(url=_API_QUOTE_URL) -> Dict[str, str]:
    h = jupiter_access.headers(JUP_API_KEY, url)
    h["User-Agent"] = os.getenv("JUPITER_UA", "MemeBot3/1.0 (+bot)")
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
            url = jupiter_access.endpoint(url, "quote")
            headers = _headers(url)
            await jupiter_access.acquire(url, api_key=JUP_API_KEY,
                priority=0 if output_mint == SOL_MINT else 2, max_wait=TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
                async with sess.get(url, params=params, allow_redirects=False) as resp:
                    jupiter_access.observe(url, api_key=JUP_API_KEY, response=resp)
                    if resp.status != 200:
                        log.debug("[jupiter_router] quote unavailable (HTTP %s)", resp.status)
                        if resp.status == 400 and url == _API_QUOTE_URL:
                            data = await _read_route_error(resp)
                            metadata = rejection_metadata(data, url=url, status=resp.status,
                                input_mint=input_mint, output_mint=output_mint, amount=amount_lamports,
                                slippage=slippage, direct=only_direct_routes)
                            if metadata is not None:
                                return QuoteResult(False, None, None, None, metadata,
                                    {"errorCode": metadata["errorCode"]})
                        return QuoteResult(False, None, None, None, {"status": resp.status}, {"status": resp.status})
                    data = await resp.json(content_type=None)
        except Exception as exc:
            reason = "provider_budget_unavailable" if isinstance(exc, jupiter_access.BudgetUnavailable) else "provider_observation_unavailable"
            log.debug("[jupiter_router] quote unavailable (%s)", type(exc).__name__)
            return QuoteResult(False, None, None, None, {"quote_contract_error": reason}, {"error": reason})

        return _checked_quote(data, input_mint=input_mint, output_mint=output_mint,
                              amount=amount_lamports, slippage=slippage, direct=only_direct_routes)

    # 1) intento principal
    qr = await _do(JUP_QUOTE_URL)

    # Only an explicitly configured nonofficial quote adapter may fall back to
    # the current gateway. Lite/v6 aliases migrate before the first request;
    # neither quota/auth failure nor invalid evidence retries the same gateway.
    if qr.ok:
        return qr

    try:
        primary_url = jupiter_access.endpoint(JUP_QUOTE_URL, "quote")
    except ValueError:
        primary_url = None
    return qr if primary_url == _API_QUOTE_URL else await _do(_API_QUOTE_URL)


async def get_order(
    *,
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    taker: str,
    slippage_bps: int | None = None,
) -> Dict[str, Any]:
    """Get a checked request-bound Swap v2 order; never sign or submit here."""
    if not JUP_MANAGED_ENABLED:
        raise RuntimeError("managed Jupiter execution disabled")
    request = managed_contract.ManagedRequest(input_mint, output_mint, amount_lamports, taker,
        MANAGED_SLIPPAGE_BPS if slippage_bps is None else slippage_bps)
    url = managed_contract.endpoint(JUP_ORDER_URL, "order")
    params = _normalize_query_params(request.params())
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)

    await jupiter_access.acquire(url, api_key=JUP_API_KEY,
        priority=0 if request.output_mint == SOL_MINT else 1, max_wait=TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout, headers=_headers(url)) as sess:
        async with sess.get(url, params=params, allow_redirects=False) as resp:
            jupiter_access.observe(url, api_key=JUP_API_KEY, response=resp)
            if resp.status != 200:
                raise RuntimeError(f"Jupiter unsigned order unavailable (HTTP {resp.status})")
            data = await resp.json(content_type=None)
    return managed_contract.check_order(data, request).raw


async def execute_order(*, signed_transaction: str, request_id: str,
                        last_valid_block_height: str | None = None, before_post=None) -> Dict[str, Any]:
    if not JUP_MANAGED_ENABLED:
        raise RuntimeError("managed Jupiter execution disabled")
    managed_contract.packet(signed_transaction)
    payload = {"signedTransaction": signed_transaction, "requestId": managed_contract.opaque_id(request_id)}
    if last_valid_block_height is not None:
        if _raw_uint(last_valid_block_height) is None:
            raise ValueError("Invalid original managed validity height")
        payload["lastValidBlockHeight"] = str(last_valid_block_height)
    url = managed_contract.endpoint(JUP_EXECUTE_URL, "execute")
    timeout = aiohttp.ClientTimeout(total=SWAP_TIMEOUT_S)
    try:
        await jupiter_access.acquire(url, api_key=JUP_API_KEY, priority=0, max_wait=SWAP_TIMEOUT_S)
    except jupiter_access.BudgetUnavailable as exc:
        raise _ManagedBudgetBeforeDispatch("Local execute budget unavailable before HTTP") from exc
    async with aiohttp.ClientSession(timeout=timeout, headers=_headers(url)) as sess:
        if before_post is not None: before_post()
        async with sess.post(url, json=payload, allow_redirects=False) as resp:
            jupiter_access.observe(url, api_key=JUP_API_KEY, response=resp)
            if resp.status != 200:
                raise RuntimeError(f"Jupiter managed submission uncertain (HTTP {resp.status})")
            return await resp.json(content_type=None)


def _sign_managed_projected(signer, order, projection_started, projection, source_sha256, fee_limit):
    # Queued owned workers must not sign a projection that aged while waiting.
    from execution import unsigned_projection
    unsigned_projection.validate(order, projection, rpc_source_sha256=source_sha256,
        max_wallet_fee_lamports=fee_limit)
    solana_execution.check_projection_age(projection_started)
    order.check_expiry()
    return signer.sign_base64_transaction(order.raw["transaction"])


class _ManagedExpiredBeforeDispatch(ValueError):
    """Only the owned worker's pre-POST expiry/age check may produce this."""


class _ManagedBudgetBeforeDispatch(ValueError):
    """Only the actual execute adapter's pre-HTTP budget may produce this."""


def _execute_managed_once(signed_transaction, order, binding, capsule, observation_url,
                          projection_started):
    # Its own HTTP loop lives entirely in an owned executor invocation. Parent
    # cancellation cannot abandon a POST or publish stopped while it settles.
    async def execute_and_check():
        dispatched = False
        def before_post():
            nonlocal dispatched
            try:
                solana_execution.check_projection_age(projection_started)
                order.check_expiry()
            except ValueError as exc:
                raise _ManagedExpiredBeforeDispatch("Original managed order aged before dispatch") from exc
            execution_provenance.record("dispatch_started", {"capsule_sha256": capsule["sha256"]})
            dispatched = True
        try:
            response = await execute_order(signed_transaction=signed_transaction,
                request_id=order.request_id, last_valid_block_height=order.last_valid_block_height, before_post=before_post)
        except (_ManagedExpiredBeforeDispatch, _ManagedBudgetBeforeDispatch) as exc:
            if dispatched:
                raise SwapSubmissionUncertain("A pre-dispatch error cannot undo an actual POST") from exc
            raise
        response, _ = managed_contract.check_execution(response, order, binding)
        execution_provenance.record("provider_response", response)
        receipt = await solana_execution.reconcile_original(capsule, response, endpoint=observation_url)
        execution_provenance.record("chain_receipt", receipt)
        return response, receipt
    return asyncio.run(execute_and_check())


async def execute_managed_swap(
    *,
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    user_public_key: str | None = None,
    slippage_bps: int | None = None,
    max_price_impact_pct: float | None = None,
    max_wallet_fee_lamports: int | None = None,
) -> Dict[str, Any]:
    """One checked Swap v2 order, pure owned signing and one owned execute.

    An unsigned configured-node projection must pass before wallet signing;
    an independent RPC must confirm the original message and wallet deltas.
    Projection is not a full instruction-semantics or future-fill proof.
    Confirmed fills are manageable positions, not finalized learning evidence.
    No automatic order rebuild, POST retry or response-as-financial-proof.
    """
    try:
        if not JUP_MANAGED_ENABLED:
            raise ValueError("Managed Jupiter is disabled or unavailable")
        user_public_key = _configured_swap_wallet(user_public_key)
        request = managed_contract.ManagedRequest(input_mint, output_mint, amount_lamports,
            user_public_key, MANAGED_SLIPPAGE_BPS if slippage_bps is None else slippage_bps)
        if max_price_impact_pct is not None and managed_contract.finite_decimal(max_price_impact_pct) < 0:
            raise ValueError("Invalid managed price impact bound")
        if max_wallet_fee_lamports is not None and (type(max_wallet_fee_lamports) is not int
                or _raw_uint(max_wallet_fee_lamports) is None):
            raise ValueError("Invalid original wallet fee bound")
        managed_contract.endpoint(JUP_ORDER_URL, "order")
        managed_contract.endpoint(JUP_EXECUTE_URL, "execute")
        observation_url = solana_execution.configured_endpoint()  # Freeze before signing/sending.
        raw_order = await get_order(input_mint=request.input_mint, output_mint=request.output_mint,
            amount_lamports=request.amount, taker=request.taker, slippage_bps=request.slippage)
        order = managed_contract.check_order(raw_order, request,
            max_price_impact_pct=max_price_impact_pct, max_wallet_fee_lamports=max_wallet_fee_lamports)
        projection, projection_started = await solana_execution.project_unsigned(order,
            endpoint=observation_url, max_wallet_fee_lamports=max_wallet_fee_lamports)
        # Recheck the actual proof at the consumer; receipt flags alone cannot
        # authorize signing, including a compromised/malformed observation hook.
        from execution import unsigned_projection
        unsigned_projection.validate(order, projection,
            rpc_source_sha256=solana_execution.endpoint_fingerprint(observation_url),
            max_wallet_fee_lamports=max_wallet_fee_lamports)
        from trader import sol_signer
        signed_transaction = await run_owned_sync(_sign_managed_projected, sol_signer, order, projection_started,
            projection, solana_execution.endpoint_fingerprint(observation_url), max_wallet_fee_lamports)
        binding = managed_contract.check_signed_packet(order, signed_transaction)
        order.check_expiry()
        solana_execution.check_projection_age(projection_started)
        capsule = chain_reconciliation.make_capsule(order, signed_transaction, binding,
            rpc_source_sha256=solana_execution.endpoint_fingerprint(observation_url),
            max_wallet_fee_lamports=max_wallet_fee_lamports, unsigned_projection=projection)
        execution_provenance.record("prepared_submission", capsule)
    except Exception as exc:
        raise SwapPreparationError("Managed order preparation failed before execute POST") from exc
    try:
        execute_response, receipt = await run_owned_sync(_execute_managed_once, signed_transaction,
            order, binding, capsule, observation_url, projection_started)
    except (_ManagedExpiredBeforeDispatch, _ManagedBudgetBeforeDispatch) as exc:
        raise SwapPreparationError("Original managed order unavailable before execute POST") from exc
    except Exception as exc:
        raise SwapSubmissionUncertain("Managed execution needs reconciliation; no new order sent") from exc
    route_meta = {"router": f"jupiter_managed:{order.raw['router']}", "requestId": order.request_id,
        "status": "Success", "mode": order.raw["mode"], "inAmount": receipt["total_input_units"],
        "outAmount": receipt["total_output_units"], "priceImpactPct": order.impact_pct / 100,
        "execution_receipt": receipt}
    return {"signature": execute_response["signature"], "qty_lamports": receipt["total_output_units"],
        "route": route_meta, "order": order.raw, "execute": execute_response,
        "execution_receipt": receipt, "submission_capsule": capsule,
        "fill_verified": True, "financial_finality_verified": receipt["financial_finality_verified"]}


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
    """Build an unsigned transaction with bounded retries, then dispatch once.

    Only the unsigned build request may retry. After entering the signing/send
    callable, every ordinary error is uncertain; never request a new packet.
    This validates transport, not swap instruction content or chain fills.
    """
    try:
        if not JUP_LEGACY_SWAP_ENABLED:
            raise ValueError("Legacy Jupiter swap execution is disabled")
        quote_resp = _swap_quote_snapshot(quote)
        if type(max_retries) is not int or not 0 <= max_retries <= 5:
            raise ValueError("max_retries must be an integer from 0 through 5")
        if any(type(value) is not bool for value in (wrap_and_unwrap_sol,
                as_legacy_transaction, dynamic_compute_unit_limit, skip_preflight)):
            raise ValueError("Swap options must be booleans")
        user_public_key = _configured_swap_wallet(user_public_key)
        if prioritization_fee_lamports is None:
            prioritization_fee_lamports = _parse_priority_fee(_PRIORITY_FEE_RAW)
        if (prioritization_fee_lamports is not None and not isinstance(prioritization_fee_lamports, dict)
                and (type(prioritization_fee_lamports) is not int
                     or not 0 <= prioritization_fee_lamports <= _U64_MAX)):
            raise ValueError("Invalid prioritization fee input")
        payload = {"quoteResponse": quote_resp, "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": wrap_and_unwrap_sol, "asLegacyTransaction": as_legacy_transaction,
            "dynamicComputeUnitLimit": dynamic_compute_unit_limit}
        if prioritization_fee_lamports is not None:
            payload["prioritizationFeeLamports"] = copy.deepcopy(prioritization_fee_lamports)
        json.dumps(payload, allow_nan=False)  # Unknown/nonfinite options cannot authorize execution.
        raw_tx = await _build_swap_transaction(payload, max_retries=max_retries)
    except SwapPreparationError:
        raise
    except Exception as exc:
        raise SwapPreparationError("Jupiter swap preparation failed before dispatch") from exc

    # This boundary is deliberately outside the build retry loop. Even an
    # execution callable raising SwapPreparationError here is not a no-send proof.
    try:
        return await _sign_and_send_raw_transaction(raw_tx, skip_preflight=skip_preflight)
    except Exception as exc:
        raise SwapSubmissionUncertain("Jupiter dispatch requires reconciliation; no new order sent") from exc


def _swap_quote_snapshot(quote: Union[QuoteResult, Dict[str, Any]]) -> dict:
    result = quote if isinstance(quote, QuoteResult) else None
    raw = result.raw if result is not None else quote
    if not isinstance(raw, dict) or not raw or result is not None and result.ok is not True:
        raise ValueError("Swap requires an available complete quote")
    raw = copy.deepcopy(raw)
    input_mint, output_mint = raw.get("inputMint"), raw.get("outputMint")
    amount, slippage = _raw_uint(raw.get("inAmount")), raw.get("slippageBps")
    if (not isinstance(input_mint, str) or not input_mint.strip()
            or not isinstance(output_mint, str) or not output_mint.strip()
            or amount is None or amount <= 0 or type(slippage) is not int or not 0 <= slippage <= 65535):
        raise ValueError("Invalid swap quote request identity")
    direct = False
    if result is not None:
        if not isinstance(result.other, dict):
            raise ValueError("Invalid quote metadata")
        direct = result.other.get("onlyDirectRoutes", False)
        if type(direct) is not bool:
            raise ValueError("Invalid direct-route metadata")
        for key, value in (("inputMint", input_mint), ("outputMint", output_mint),
                           ("requested_in_amount", amount), ("slippageBps", slippage)):
            if key in result.other and (type(result.other[key]) is not type(value) or result.other[key] != value):
                raise ValueError("Conflicting quote request metadata")
    checked = _checked_quote(raw, input_mint=input_mint, output_mint=output_mint,
        amount=amount, slippage=slippage, direct=direct)
    if not checked.ok:
        raise ValueError("Incomplete or malformed swap quote")
    if result is not None and (type(result.in_amount) is not int or type(result.out_amount) is not int
            or result.in_amount != checked.in_amount or result.out_amount != checked.out_amount
            or isinstance(result.price_impact_bps, bool) or not isinstance(result.price_impact_bps, (int, float))
            or not math.isfinite(result.price_impact_bps) or result.price_impact_bps != checked.price_impact_bps):
        raise ValueError("Conflicting quote result fields")
    return raw


def _configured_swap_wallet(requested: str | None) -> str:
    if requested is not None and not isinstance(requested, str):
        raise ValueError("Invalid swap wallet identity")
    from trader import sol_signer
    configured = str(getattr(sol_signer, "PUBLIC_KEY", "") or "")
    if not configured or not callable(getattr(sol_signer, "sign_and_send", None)):
        raise ValueError("Configured swap signer unavailable")
    declared = os.getenv("SOL_PUBLIC_KEY", "").strip()
    if declared and declared != configured:
        raise ValueError("Declared wallet differs from configured swap signer")
    requested = (requested or configured).strip()
    if requested != configured:
        raise ValueError("Swap wallet differs from configured signer")
    return configured


def _swap_packet(data: Any) -> bytes:
    if (not isinstance(data, dict) or data.get("error") or data.get("errorCode")
            or data.get("simulationError") is not None):
        raise SwapPreparationError("Jupiter build returned no usable unsigned transaction")
    packets = [data[key] for key in ("swapTransaction", "swap_transaction", "transaction") if key in data]
    if (not packets or any(not isinstance(value, str) or not value for value in packets)
            or any(value != packets[0] for value in packets) or len(packets[0]) > 1644):
        raise SwapPreparationError("Jupiter build returned missing/conflicting transaction bytes")
    try:
        raw = base64.b64decode(packets[0], validate=True)
    except (ValueError, TypeError) as exc:
        raise SwapPreparationError("Jupiter build returned invalid transaction base64") from exc
    if not 0 < len(raw) <= 1232:
        raise SwapPreparationError("Jupiter transaction exceeds supported legacy/v0 packet bounds")
    for key in ("lastValidBlockHeight", "prioritizationFeeLamports"):
        if key in data and (type(data[key]) is not int or _raw_uint(data[key]) is None):
            raise SwapPreparationError("Jupiter build returned invalid numeric metadata")
    return raw


async def _build_swap_transaction(payload: dict, *, max_retries: int) -> bytes:
    timeout = aiohttp.ClientTimeout(total=SWAP_TIMEOUT_S)
    url = _derive_swap_url()
    for attempt in range(max_retries + 1):
        try:
            await jupiter_access.acquire(url, api_key=JUP_API_KEY,
                priority=0 if payload["quoteResponse"].get("outputMint") == SOL_MINT else 1,
                max_wait=SWAP_TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout, headers=_headers(url)) as sess:
                async with sess.post(url, json=copy.deepcopy(payload), allow_redirects=False) as resp:
                    jupiter_access.observe(url, api_key=JUP_API_KEY, response=resp)
                    if resp.status != 200:
                        if resp.status == 429 or 500 <= resp.status <= 599:
                            raise _TransientSwapBuild(f"Jupiter unsigned build HTTP {resp.status}")
                        raise SwapPreparationError(f"Jupiter unsigned build unavailable (HTTP {resp.status})")
                    data = await resp.json(content_type=None)
        except (_TransientSwapBuild, aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
            if attempt < max_retries:
                await asyncio.sleep(.6 * (attempt + 1))
                continue
            raise SwapPreparationError("Jupiter unsigned build transport unavailable") from exc
        return _swap_packet(data)  # Malformed responses do not retry or dispatch.
    raise SwapPreparationError("Jupiter unsigned build exhausted")


async def _sign_and_send_raw_transaction(raw_tx: bytes, *, skip_preflight: bool = False) -> str:
    """Use the checked common signer and retain off-loop execution ownership."""
    if not isinstance(raw_tx, bytes) or type(skip_preflight) is not bool:
        raise ValueError("Invalid original transaction transport")
    from trader import sol_signer
    return await run_owned_sync(sol_signer.sign_and_send, raw_tx, skip_preflight=skip_preflight)


__all__ = [
    "QuoteResult",
    "SwapPreparationError",
    "SwapSubmissionUncertain",
    "SOL_MINT",
    "execute_managed_swap",
    "execute_order",
    "execute_swap",
    "get_order",
    "get_quote",
]
