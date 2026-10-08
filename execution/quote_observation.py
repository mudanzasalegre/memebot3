"""Fresh request-bound route observations, never execution/finality evidence."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from utils import jupiter_access
from utils.raw_units import U64_MAX

MAX_RECEIPT_AGE_SECONDS = 10
NO_ROUTE_CODES = frozenset({"NO_ROUTES_FOUND", "COULD_NOT_FIND_ANY_ROUTE", "TOKEN_NOT_TRADABLE",
    "ROUTE_PLAN_DOES_NOT_CONSUME_ALL_THE_AMOUNT"})


@dataclass(frozen=True)
class RouteObservation:
    has_route: bool | None
    reason: str
    in_amount: int | None = None
    out_amount: int | None = None
    price_impact_bps: float | None = None
    route_count: int | None = None
    protocol: str | None = None
    router: str | None = None


def rejection_metadata(data: Any, *, url: str, status: int, input_mint: str,
                       output_mint: str, amount: int, slippage: int, direct: bool) -> dict | None:
    """Only documented routing enums from the actual official HTTP response.

    A 400 alone, an arbitrary error string, or a mixed quote/error is unknown.
    Preserve only the enum, not free-form provider text or credentials.
    This is scoped to this Metis request/size/time, not every trading venue.
    """
    if (not jupiter_access.official(url) or type(status) is not int or status != 400
            or not isinstance(data, dict) or not set(data) <= {"error", "errorCode"}
            or not isinstance(data.get("errorCode"), str) or data["errorCode"] not in NO_ROUTE_CODES
            or ("error" in data and (not isinstance(data["error"], str) or len(data["error"]) > 4096))):
        return None
    return {"quote_rejection_version": 1, "quote_contract_error": "provider_no_route",
        "provider_url": url, "http_status": status, "errorCode": data["errorCode"],
        "inputMint": input_mint, "outputMint": output_mint, "requested_in_amount": amount,
        "slippageBps": slippage, "onlyDirectRoutes": direct,
        "received_at_utc": datetime.now(timezone.utc).isoformat(),
        "market_asof_verified": False, "fill_verified": False}


def observe_quote(quote: Any, *, input_mint: str, output_mint: str, amount: int,
                  slippage: int, direct: bool = False, now: datetime | None = None) -> RouteObservation:
    """Recheck the original raw payload without refreshing its original receipt.

    False means a fresh request-bound provider routing rejection. All malformed,
    stale, unauthenticated, quota/network and local-budget failures stay None.
    """
    unknown = RouteObservation(None, "quote_unverified")
    if (type(amount) is not int or not 0 < amount <= U64_MAX or type(slippage) is not int
            or not 0 <= slippage <= 65535 or type(direct) is not bool
            or any(not isinstance(m, str) or not m or m != m.strip() for m in (input_mint, output_mint))):
        return unknown
    other, raw = getattr(quote, "other", None), getattr(quote, "raw", None)
    if not isinstance(other, Mapping) or not isinstance(raw, dict):
        return unknown
    if (other.get("inputMint") != input_mint or other.get("outputMint") != output_mint
            or type(other.get("requested_in_amount")) is not int or other["requested_in_amount"] != amount
            or type(other.get("slippageBps")) is not int or other["slippageBps"] != slippage
            or other.get("onlyDirectRoutes") is not direct
            or other.get("market_asof_verified") is not False or other.get("fill_verified") is not False):
        return unknown
    try:
        stamp = other["received_at_utc"]
        if not isinstance(stamp, str) or len(stamp) > 64:
            return unknown
        received = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        current = now if now is not None else datetime.now(timezone.utc)
        if not isinstance(current, datetime) or received.utcoffset() is None or current.utcoffset() is None:
            return unknown
        age = (current - received).total_seconds()
        if not 0 <= age <= MAX_RECEIPT_AGE_SECONDS:
            return RouteObservation(None, "quote_receipt_expired")
    except (KeyError, TypeError, ValueError, OverflowError):
        return unknown
    if getattr(quote, "ok", None) is False:
        code = other.get("errorCode")
        if (type(other.get("quote_rejection_version")) is int and other["quote_rejection_version"] == 1
                and other.get("quote_contract_error") == "provider_no_route"
                and isinstance(code, str) and code in NO_ROUTE_CODES and raw == {"errorCode": code}
                and type(other.get("http_status")) is int and other["http_status"] == 400
                and other.get("provider_url") == "https://api.jup.ag/swap/v1/quote"
                and all(getattr(quote, field, None) is None for field in ("in_amount", "out_amount", "price_impact_bps"))):
            return RouteObservation(False, code.lower())
        return unknown
    if (getattr(quote, "ok", None) is not True or type(other.get("quote_contract_version")) is not int
            or other["quote_contract_version"] not in (1, 2)):
        return unknown
    # Local import avoids a router/observation cycle. This recomputes structural
    # identity only; its new timestamp is deliberately NOT used for freshness.
    from fetcher.jupiter_router import _checked_quote, _checked_v2_quote
    v2 = other["quote_contract_version"] == 2
    if v2:
        if (direct or other.get("quote_protocol") != "swap_v2"
                or other.get("provider_url") != "https://api.jup.ag/swap/v2/order"
                or other.get("transaction_available") is not False or other.get("taker_provided") is not False):
            return unknown
        checked = _checked_v2_quote(raw, input_mint=input_mint, output_mint=output_mint,
            amount=amount, slippage=slippage, now=current)
    else:
        checked = _checked_quote(raw, input_mint=input_mint, output_mint=output_mint,
            amount=amount, slippage=slippage, direct=direct)
    impact = getattr(quote, "price_impact_bps", None)
    if (not checked.ok or type(getattr(quote, "in_amount", None)) is not int
            or type(getattr(quote, "out_amount", None)) is not int
            or quote.in_amount != checked.in_amount or quote.out_amount != checked.out_amount
            or isinstance(impact, bool) or not isinstance(impact, (int, float))
            or not math.isfinite(impact) or impact != checked.price_impact_bps
            or type(other.get("routePlan_len")) is not int
            or other["routePlan_len"] != checked.other["routePlan_len"]
            or (v2 and other.get("router") != checked.other.get("router"))
            or type(other.get("contextSlot")) is not type(checked.other.get("contextSlot"))
            or other.get("contextSlot") != checked.other.get("contextSlot")):
        return unknown
    return RouteObservation(True, "quote_ok", checked.in_amount, checked.out_amount,
        checked.price_impact_bps, checked.other["routePlan_len"], "swap_v2" if v2 else "metis_v1",
        checked.other.get("router") if v2 else "metis")


def impact_within_limit(impact, limit, *, protocol="metis_v1") -> bool:
    """V2 impact is signed: improvement is not adverse slippage or profit."""
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (impact, limit)) or limit < 0:
        return False
    return (max(0, impact) if protocol == "swap_v2" else abs(impact)) / 100 <= limit
