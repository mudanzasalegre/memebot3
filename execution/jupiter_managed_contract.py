"""Swap v2 transport contracts, not independent instruction/wallet acceptance.

Pure validation: no wallet secret, signer initialization, HTTP or RPC here.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from utils.raw_units import raw_uint


def endpoint(value: str, operation: str) -> str:
    """Migrate known Ultra defaults; never send API credentials elsewhere."""
    canonical = f"https://api.jup.ag/swap/v2/{operation}"
    parts = urlsplit(value or canonical)
    if (operation not in {"order", "execute"} or parts.scheme != "https"
            or parts.netloc != "api.jup.ag" or parts.query or parts.fragment
            or parts.path not in {f"/swap/v2/{operation}", f"/ultra/v1/{operation}"}):
        raise ValueError("Unsupported managed Jupiter endpoint")
    return canonical


def address(value) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Invalid original address")
    if str(Pubkey.from_string(value)) != value:
        raise ValueError("Noncanonical original address")
    return value


def positive_units(value) -> int:
    result = raw_uint(value)
    if result is None or result <= 0:
        raise ValueError("Invalid raw positive units")
    return result


def opaque_id(value) -> str:
    if (not isinstance(value, str) or not 0 < len(value) <= 256
            or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("Invalid managed request identity")
    return value


def finite_decimal(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("Invalid finite numeric field")
    if isinstance(value, str) and (len(value) > 128 or value != value.strip()):
        raise ValueError("Invalid finite numeric transport")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid finite numeric field") from exc
    if not result.is_finite():
        raise ValueError("Invalid finite numeric field")
    return result


@dataclass(frozen=True)
class ManagedRequest:
    input_mint: str
    output_mint: str
    amount: int
    taker: str
    slippage: int

    def __post_init__(self):
        for value in (self.input_mint, self.output_mint, self.taker):
            address(value)
        if self.input_mint == self.output_mint:
            raise ValueError("Identical input/output mint")
        if type(self.amount) is not int:
            raise ValueError("Managed amount requires exact integer units")
        positive_units(self.amount)
        if type(self.slippage) is not int or not 0 <= self.slippage <= 10000:
            raise ValueError("Invalid managed slippage bound")

    def params(self) -> dict:
        return {"inputMint": self.input_mint, "outputMint": self.output_mint,
            "amount": str(self.amount), "taker": self.taker, "slippageBps": self.slippage,
            "swapMode": "ExactIn", "maxSupportedTransactionVersion": "0"}


@dataclass(frozen=True)
class CheckedOrder:
    request: ManagedRequest
    raw: dict
    transaction: VersionedTransaction
    request_id: str
    threshold: int
    impact_pct: float
    wallet_fee_lamports: int
    fee_mint: str | None
    last_valid_block_height: str | None
    expiry: datetime | None
    order_sha256: str

    def check_expiry(self):
        if self.expiry is not None and datetime.now(timezone.utc) >= self.expiry:
            raise ValueError("Managed RFQ order expired before submission")


def packet(encoded) -> VersionedTransaction:
    if not isinstance(encoded, str) or not 0 < len(encoded) <= 1644:
        raise ValueError("Invalid managed transaction transport")
    raw = base64.b64decode(encoded, validate=True)
    if not 0 < len(raw) <= 1232:
        raise ValueError("Unsupported managed transaction packet size")
    transaction = VersionedTransaction.from_bytes(raw)
    transaction.sanitize()
    if not isinstance(transaction.message, MessageV0):
        raise ValueError("Swap v2 requested only supported v0 messages")
    return transaction


def check_order(data, request: ManagedRequest, *, max_price_impact_pct=None,
                max_wallet_fee_lamports=None) -> CheckedOrder:
    if not isinstance(data, dict):
        raise ValueError("Managed order is not an object")
    raw = copy.deepcopy(data)
    if raw.get("error") or raw.get("errorCode") or raw.get("errorMessage"):
        raise ValueError("Managed provider could not prepare the order")
    if (raw.get("inputMint") != request.input_mint or raw.get("outputMint") != request.output_mint
            or raw.get("taker") != request.taker or raw.get("swapMode") != "ExactIn"
            or positive_units(raw.get("inAmount")) != request.amount
            or type(raw.get("slippageBps")) is not int or raw["slippageBps"] != request.slippage):
        raise ValueError("Managed order differs from original request")
    output = positive_units(raw.get("outAmount"))
    threshold = positive_units(raw.get("otherAmountThreshold"))
    if not max(1, output * (10000 - request.slippage) // 10000) <= threshold <= output:
        raise ValueError("Invalid original output threshold")
    impact = finite_decimal(raw.get("priceImpact"))  # v2 signed percentage points, not a fraction.
    if not math.isfinite(float(impact)):
        raise ValueError("Nonfinite managed impact representation")
    if max_price_impact_pct is not None:
        limit = finite_decimal(max_price_impact_pct)
        if limit < 0 or impact > limit:
            raise ValueError("Managed order exceeds original impact bound")
    if raw.get("router") not in {"metis", "jupiterz", "dflow", "okx"} or raw.get("mode") not in {"ultra", "manual"}:
        raise ValueError("Unknown managed routing contract")
    if type(raw.get("transactionVersion")) is not int or raw["transactionVersion"] != 0:
        raise ValueError("Managed provider returned an unsupported transaction version")
    transaction = packet(raw.get("transaction"))
    required = transaction.message.header.num_required_signatures
    keys = transaction.message.account_keys
    wallet = Pubkey.from_string(request.taker)
    if wallet not in keys[:required] or len(transaction.signatures) != required:
        raise ValueError("Original taker is not a required transaction signer")
    valid = transaction.verify_with_results()
    if any(signature != Signature.default() and not valid[i]
            for i, signature in enumerate(transaction.signatures)):
        raise ValueError("Original non-placeholder signature is invalid")
    if raw.get("receiver") not in (None, "", request.taker) or raw.get("referralAccount") not in (None, ""):
        raise ValueError("Unrequested receiver or referral account")
    if type(raw.get("gasless")) is not bool:
        raise ValueError("Unknown managed fee sponsorship")
    wallet_fee = 0
    for fee in ("signature", "prioritization", "rent"):
        amount, payer = raw.get(f"{fee}FeeLamports"), raw.get(f"{fee}FeePayer")
        if type(amount) is not int or raw_uint(amount) is None:
            raise ValueError("Unknown managed network/rent fee estimate")
        if payer is not None:
            address(payer)
        elif amount:
            raise ValueError("Unknown nonzero fee payer")
        if payer == request.taker:
            wallet_fee += amount
        if fee == "signature" and payer is not None and payer != str(keys[0]):
            raise ValueError("Declared signature fee payer differs from original message")
        if fee == "signature" and payer is not None and raw["gasless"] != (payer != request.taker):
            raise ValueError("Declared fee sponsorship conflicts with original payer")
    if max_wallet_fee_lamports is not None:
        if type(max_wallet_fee_lamports) is not int or raw_uint(max_wallet_fee_lamports) is None:
            raise ValueError("Invalid original wallet fee bound")
        if wallet_fee > max_wallet_fee_lamports:
            raise ValueError("Managed fees exceed the original wallet reserve")
    if raw_uint(wallet_fee) is None:
        raise ValueError("Managed aggregate wallet fee overflows raw units")
    fee_bps = finite_decimal(raw.get("feeBps"))
    fee_mint = raw.get("feeMint")
    if not 0 <= fee_bps <= 10000 or fee_mint not in (request.input_mint, request.output_mint):
        raise ValueError("Unknown original token fee contract")
    height = raw_uint(raw.get("lastValidBlockHeight"))
    if raw["router"] != "jupiterz" and (height is None or height <= 0):
        raise ValueError("Missing managed aggregator validity height")
    if "lastValidBlockHeight" in raw and height is None:
        raise ValueError("Invalid managed validity height")
    expiry = None
    if raw.get("expireAt") is not None:
        if not isinstance(raw["expireAt"], str):
            raise ValueError("Invalid RFQ expiry")
        expiry = datetime.fromisoformat(raw["expireAt"].replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise ValueError("Unzoned RFQ expiry")
    if raw["router"] == "jupiterz" and expiry is None:
        raise ValueError("Missing RFQ expiry")
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")).hexdigest()
    order = CheckedOrder(request, raw, transaction, opaque_id(raw.get("requestId")), threshold,
        float(impact), wallet_fee, fee_mint, str(height) if height else None, expiry, digest)
    order.check_expiry()
    return order


def check_signed_packet(order: CheckedOrder, encoded) -> dict:
    signed = packet(encoded)
    original = order.transaction
    if signed.message != original.message or len(signed.signatures) != len(original.signatures):
        raise ValueError("Signing changed the original managed message")
    wallet = Pubkey.from_string(order.request.taker)
    index = list(original.message.account_keys[:original.message.header.num_required_signatures]).index(wallet)
    for i, signature in enumerate(original.signatures):
        if i != index and signature != signed.signatures[i]:
            raise ValueError("Signing changed an original co-signature")
    if not signed.verify_with_results()[index]:
        raise ValueError("Original wallet signature did not verify")
    first = signed.signatures[0]
    return {"message_sha256": hashlib.sha256(to_bytes_versioned(original.message)).hexdigest(),
        "original_fee_payer": str(original.message.account_keys[0]),
        "wallet_signature": str(signed.signatures[index]),
        "expected_first_signature": str(first) if first != Signature.default() else None,
        "wallet_signer_index": index}


def check_execution(data, order: CheckedOrder, binding: dict) -> tuple[dict, dict]:
    if (not isinstance(data, dict) or data.get("status") != "Success" or type(data.get("code")) is not int
            or data["code"] != 0 or data.get("error")):
        raise ValueError("Managed execution has no checked success receipt")
    raw = copy.deepcopy(data)
    signature = raw.get("signature")
    if not isinstance(signature, str) or Signature.from_string(signature) == Signature.default():
        raise ValueError("Invalid managed result signature")
    expected = binding["expected_first_signature"]
    if expected is not None and signature != expected:
        raise ValueError("Managed execution returned a different transaction signature")
    # A provider may add the sponsor's first signature after our partial sign.
    # Its public key and exact original message still let us bind that result
    # cryptographically, without inventing a signature before execute.
    if not Signature.from_string(signature).verify(order.transaction.message.account_keys[0],
            to_bytes_versioned(order.transaction.message)):
        raise ValueError("Managed result signature does not sign the original payer/message")
    slot = positive_units(raw.get("slot"))
    total_input = positive_units(raw.get("totalInputAmount"))
    route_input = positive_units(raw.get("inputAmountResult"))
    route_output = positive_units(raw.get("outputAmountResult"))
    total_output = positive_units(raw.get("totalOutputAmount"))
    if (total_input != order.request.amount or route_input > total_input
            or total_output > route_output or total_output < order.threshold):
        raise ValueError("Managed execution amounts conflict with the original order")
    input_fee, output_fee = total_input - route_input, route_output - total_output
    if ((order.fee_mint == order.request.input_mint and output_fee)
            or (order.fee_mint == order.request.output_mint and input_fee)):
        raise ValueError("Managed result fee mint conflicts with amount deductions")
    if "requestId" in raw and raw["requestId"] != order.request_id:
        raise ValueError("Managed result request identity conflicts")
    receipt = {"contract_version": 1, "provider_protocol": "jupiter_swap_v2",
        "original_order_sha256": order.order_sha256,
        "original_request_sha256": hashlib.sha256(json.dumps(order.request.params(), sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest(),
        "request_id": order.request_id, "input_mint": order.request.input_mint,
        "output_mint": order.request.output_mint, "wallet": order.request.taker,
        "requested_input_units": order.request.amount, "quoted_output_units": positive_units(order.raw["outAmount"]),
        "total_input_units": total_input, "total_output_units": total_output,
        "route_input_units": route_input, "route_output_units": route_output,
        "input_fee_units": input_fee, "output_fee_units": output_fee,
        "wallet_fee_estimate_lamports": order.wallet_fee_lamports,
        "provider_reported_slot": slot, "provider_reported_signature": signature,
        "first_signature_bound": True, "provider_first_signature_verified": True,
        "first_signature_present_before_execute": expected is not None, **binding,
        "chain_wallet_verified": False, "requires_reconciliation": True,
        "unsigned_swap_instructions_verified": False}
    return raw, receipt
