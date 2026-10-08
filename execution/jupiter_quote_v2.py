"""Swap V2 observation-only quotes: no taker, packet, signer or execution."""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from execution.jupiter_managed_contract import address, finite_decimal, opaque_id, positive_units

ROUTERS = frozenset({"metis", "jupiterz", "dflow", "okx"})


@dataclass(frozen=True)
class QuoteRequest:
    input_mint: str
    output_mint: str
    amount: int
    slippage: int

    def __post_init__(self):
        address(self.input_mint)
        address(self.output_mint)
        if self.input_mint == self.output_mint or type(self.amount) is not int:
            raise ValueError("Invalid observation-only quote identity")
        positive_units(self.amount)
        if type(self.slippage) is not int or not 0 <= self.slippage <= 10000:
            raise ValueError("Invalid observation-only slippage")

    def params(self):
        return {"inputMint": self.input_mint, "outputMint": self.output_mint,
            "amount": str(self.amount), "slippageBps": self.slippage,
            "swapMode": "ExactIn", "maxSupportedTransactionVersion": "0"}


@dataclass(frozen=True)
class CheckedQuote:
    raw: dict
    output: int
    impact_bps: float
    router: str
    steps: int


def check_quote(data, request: QuoteRequest, *, now=None):
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.utcoffset() is None:
        raise ValueError("Invalid observation-only receipt clock")
    if not isinstance(data, dict):
        raise ValueError("Observation-only quote is not an object")
    raw = copy.deepcopy(data)
    if any(raw.get(key) for key in ("error", "errorCode", "errorMessage")):
        raise ValueError("Observation-only quote unavailable")
    if (raw.get("inputMint") != request.input_mint or raw.get("outputMint") != request.output_mint
            or positive_units(raw.get("inAmount")) != request.amount or raw.get("swapMode") != "ExactIn"
            or type(raw.get("slippageBps")) is not int or raw["slippageBps"] != request.slippage
            or raw.get("router") not in ROUTERS or raw.get("mode") not in {"ultra", "manual"}):
        raise ValueError("Observation-only quote differs from request")
    # A quote-only adapter must never receive or make available a signable order.
    if (raw.get("transaction") is not None or raw.get("taker") is not None
            or raw.get("receiver") not in (None, "") or raw.get("referralAccount") not in (None, "")):
        raise ValueError("Observation-only quote unexpectedly contains execution material")
    version = raw.get("transactionVersion")
    if version is not None and (type(version) is not int or version != 0):
        raise ValueError("Observation-only quote violates supported-version ceiling")
    output, threshold = positive_units(raw.get("outAmount")), positive_units(raw.get("otherAmountThreshold"))
    if not max(1, output * (10000 - request.slippage) // 10000) <= threshold <= output:
        raise ValueError("Invalid observation-only output floor")
    impact = finite_decimal(raw.get("priceImpact")) * 100  # signed percentage points -> bps
    if not math.isfinite(float(impact)):
        raise ValueError("Invalid observation-only signed impact")
    plan = raw.get("routePlan", [])
    if not isinstance(plan, list) or len(plan) > 128 or (raw["router"] == "metis" and not plan):
        raise ValueError("Invalid observation-only route plan")
    edges = []
    for step in plan:
        info = step.get("swapInfo") if isinstance(step, dict) else None
        if not isinstance(info, dict):
            raise ValueError("Invalid observation-only route step")
        for key in ("ammKey", "inputMint", "outputMint"):
            address(info.get(key))
        positive_units(info.get("inAmount"))
        positive_units(info.get("outAmount"))
        for key, maximum in (("percent", 100), ("bps", 10000)):
            if step.get(key) is not None and (type(step[key]) is not int or not 0 <= step[key] <= maximum):
                raise ValueError("Invalid observation-only route weight")
        edges.append((info["inputMint"], info["outputMint"]))
    if edges:
        def reachable(start, reverse=False):
            seen = {start}
            for _ in edges:
                expanded = seen | {a if reverse else b for a, b in edges if (b if reverse else a) in seen}
                if expanded == seen:
                    break
                seen = expanded
            return seen
        forward, backward = reachable(request.input_mint), reachable(request.output_mint, True)
        if request.output_mint not in forward or any(a not in forward or b not in backward for a, b in edges):
            raise ValueError("Disconnected observation-only route plan")
    if raw.get("feeBps") is not None:
        if not 0 <= finite_decimal(raw["feeBps"]) <= 10000 or raw.get("feeMint") not in (request.input_mint, request.output_mint):
            raise ValueError("Invalid observation-only token fee")
    for field in ("requestId", "quoteId"):
        if raw.get(field) is not None:
            opaque_id(raw[field])
    if raw.get("maker") is not None:
        address(raw["maker"])
    if raw.get("expireAt") is not None:
        value = raw["expireAt"]
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError("Invalid observation-only expiry")
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expiry.utcoffset() is None or current.utcoffset() is None or current >= expiry:
            raise ValueError("Observation-only quote expired")
    return CheckedQuote(raw, output, float(impact), raw["router"], len(plan))
