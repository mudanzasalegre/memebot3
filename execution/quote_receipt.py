"""Public quote receipts for estimated PAPER evidence, never order/fill proof.

Historical validation rechecks the recorded payload at its original receipt
time. It cannot establish a current route or authenticate a provider signature.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from types import SimpleNamespace

from execution.quote_observation import observe_quote, impact_within_limit

SUMMARY_FIELDS = frozenset({"in_amount", "out_amount", "impact_bps", "route_count", "max_impact_pct",
    "protocol", "router", "observation_receipt"})
_RAW_FIELDS = frozenset({"inputMint", "outputMint", "inAmount", "outAmount", "otherAmountThreshold",
    "swapMode", "slippageBps", "priceImpactPct", "priceImpact", "contextSlot", "mode", "router",
    "transactionVersion", "expireAt", "feeBps", "feeMint"})
_OTHER_FIELDS = frozenset({"quote_contract_version", "quote_protocol", "router", "inputMint", "outputMint",
    "requested_in_amount", "slippageBps", "onlyDirectRoutes", "routePlan_len", "contextSlot",
    "received_at_utc", "provider_url", "market_asof_verified", "fill_verified",
    "transaction_available", "taker_provided"})
_RECEIPT_FIELDS = frozenset({"version", "raw", "other", "in_amount", "out_amount", "price_impact_bps", "sha256"})
_V1_RAW = frozenset({"inputMint", "outputMint", "inAmount", "outAmount", "otherAmountThreshold",
    "swapMode", "slippageBps", "priceImpactPct", "contextSlot"})
_V1_OTHER = _OTHER_FIELDS - {"quote_protocol", "router", "provider_url", "transaction_available", "taker_provided"}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def _public_raw(raw, *, v2=False):
    allowed = _RAW_FIELDS - {"priceImpactPct", "contextSlot"} if v2 else _V1_RAW
    result = {k: copy.deepcopy(v) for k, v in raw.items() if k in allowed}
    if "routePlan" in raw:
        result["routePlan"] = [{**{k: step[k] for k in ("percent", "bps") if k in step},
            "swapInfo": {k: step["swapInfo"][k] for k in
                ("ammKey", "inputMint", "outputMint", "inAmount", "outAmount")}}
            for step in raw["routePlan"]]
    return result


def capture_summary(quote, *, input_mint, output_mint, amount, slippage, limit, now=None):
    observed = observe_quote(quote, input_mint=input_mint, output_mint=output_mint,
        amount=amount, slippage=slippage, now=now)
    if observed.has_route is not True or not impact_within_limit(
            observed.price_impact_bps, limit, protocol=observed.protocol):
        raise ValueError("No fresh admissible observation-only quote")
    v2 = observed.protocol == "swap_v2"
    receipt = {"version": 1, "raw": _public_raw(quote.raw, v2=v2),
        "other": {k: copy.deepcopy(v) for k, v in quote.other.items() if k in (_OTHER_FIELDS if v2 else _V1_OTHER)},
        "in_amount": observed.in_amount, "out_amount": observed.out_amount,
        "price_impact_bps": observed.price_impact_bps}
    receipt["sha256"] = _hash(receipt)
    summary = {"in_amount": observed.in_amount, "out_amount": observed.out_amount,
        "impact_bps": observed.price_impact_bps, "route_count": observed.route_count,
        "max_impact_pct": limit, "protocol": observed.protocol, "router": observed.router,
        "observation_receipt": receipt}
    # Whitelisting must not change the contract that was just checked.
    if not valid_summary(summary, input_mint=input_mint, output_mint=output_mint, amount=amount):
        raise ValueError("Public observation-only receipt differs from its quote")
    return summary


def valid_summary(summary, *, input_mint=None, output_mint=None, amount=None,
                  not_after=None, allow_legacy=False):
    """No receipt freshness renewal. Legacy summaries remain explicitly legacy."""
    try:
        if not isinstance(summary, dict):
            return False
        receipt = summary.get("observation_receipt")
        if receipt is None:
            return (allow_legacy and summary.get("protocol", "metis_v1") == "metis_v1"
                and summary.get("router", "metis") == "metis"
                and type(summary.get("route_count")) is int and summary["route_count"] > 0
                and type(summary.get("in_amount")) is int and summary["in_amount"] > 0
                and type(summary.get("out_amount")) is int and summary["out_amount"] > 0
                and (amount is None or summary["in_amount"] == amount))
        if (not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS
                or type(receipt["version"]) is not int or receipt["version"] != 1
                or not isinstance(receipt["raw"], dict) or not isinstance(receipt["other"], dict)
                or not set(receipt["raw"]) <= _RAW_FIELDS | {"routePlan"}
                or not set(receipt["other"]) <= _OTHER_FIELDS
                or receipt["raw"] != _public_raw(receipt["raw"], v2=summary.get("protocol") == "swap_v2")
                or receipt["sha256"] != _hash({k: v for k, v in receipt.items() if k != "sha256"})):
            return False
        other = receipt["other"]
        if summary.get("protocol") != "swap_v2" and not set(other) <= _V1_OTHER:
            return False
        received = datetime.fromisoformat(other["received_at_utc"].replace("Z", "+00:00"))
        if received.utcoffset() is None or (not_after is not None and received > not_after):
            return False
        q = SimpleNamespace(ok=True, raw=receipt["raw"], other=other, in_amount=receipt["in_amount"],
            out_amount=receipt["out_amount"], price_impact_bps=receipt["price_impact_bps"])
        observed = observe_quote(q, input_mint=input_mint or other["inputMint"],
            output_mint=output_mint or other["outputMint"], amount=amount if amount is not None else other["requested_in_amount"],
            slippage=other["slippageBps"], direct=other["onlyDirectRoutes"], now=received)
        return (observed.has_route is True and type(summary.get("in_amount")) is int
            and type(summary.get("out_amount")) is int and type(summary.get("route_count")) is int
            and summary["in_amount"] == observed.in_amount and summary["out_amount"] == observed.out_amount
            and summary["route_count"] == observed.route_count and summary.get("impact_bps") == observed.price_impact_bps
            and summary.get("protocol") == observed.protocol and summary.get("router") == observed.router
            and impact_within_limit(summary.get("impact_bps"), summary.get("max_impact_pct"), protocol=observed.protocol))
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return False


def public_summary(summary):
    """Archive only a checked public receipt; never copy arbitrary nested payloads."""
    result = {k: copy.deepcopy(v) for k, v in summary.items() if k in SUMMARY_FIELDS}
    if "observation_receipt" in result and not valid_summary(result):
        raise ValueError("Malformed public observation-only receipt")
    return result
