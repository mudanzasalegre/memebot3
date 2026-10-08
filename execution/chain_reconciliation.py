"""Original-message and wallet-delta proofs from a separately queried Solana RPC.

No signer, key, HTTP, resend or market-price initialization. RPC metadata is a
trusted-node observation, not a cryptographic proof of block inclusion. Unknown
ownership, missing balances and unexplained native transfers stay unresolved.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from solders.signature import Signature

from execution import jupiter_managed_contract as managed
from execution import wallet_effects
from execution.wallet_effects import SOL, TOKEN_PROGRAMS, units as _units


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def make_capsule(order, signed_transaction, binding, *, rpc_source_sha256, max_wallet_fee_lamports=None,
                 unsigned_projection=None):
    body = {"version": 2 if unsigned_projection is not None else 1,
        "request": order.request.params(), "order": copy.deepcopy(order.raw),
        "signed_transaction": signed_transaction, "binding": copy.deepcopy(binding),
        "max_wallet_fee_lamports": max_wallet_fee_lamports, "rpc_source_sha256": rpc_source_sha256}
    if unsigned_projection is not None:
        body["unsigned_projection"] = copy.deepcopy(unsigned_projection)
    capsule = {**body, "sha256": digest(body)}
    validate_capsule(capsule)
    return capsule


def validate_capsule(value):
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] not in {1, 2}:
        raise ValueError("Invalid original submission capsule")
    body = {key: copy.deepcopy(item) for key, item in value.items() if key != "sha256"}
    if value.get("sha256") != digest(body):
        raise ValueError("Original submission capsule changed")
    if not isinstance(value.get("rpc_source_sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", value["rpc_source_sha256"]) is None:
        raise ValueError("Original observation endpoint provenance is missing")
    p = value["request"]
    request = managed.ManagedRequest(p["inputMint"], p["outputMint"],
        managed.positive_units(p["amount"]), p["taker"], p["slippageBps"])
    if p != request.params():
        raise ValueError("Original submission request changed")
    order = managed.check_order(value["order"], request,
        max_wallet_fee_lamports=value["max_wallet_fee_lamports"], historical=True)
    binding = managed.check_signed_packet(order, value["signed_transaction"])
    if binding != value["binding"]:
        raise ValueError("Original submission signing provenance changed")
    if value["version"] == 2:
        from execution import unsigned_projection
        unsigned_projection.validate(order, value.get("unsigned_projection"),
            rpc_source_sha256=value["rpc_source_sha256"], max_wallet_fee_lamports=value["max_wallet_fee_lamports"])
    elif "unsigned_projection" in value:
        raise ValueError("Historical capsule cannot declare unchecked unsigned projection")
    return order, binding


def reconcile(capsule, execution, transaction_result, status_result):
    """Recheck original packet, all signatures, success, slot and wallet balances.

    Native principal is measured separately from the payer's network fee and
    native balances held in owned token accounts. External rent sponsorship or
    extra tips/transfers that cannot be reconciled exactly are not guessed.
    """
    order, binding = validate_capsule(capsule)
    execution, receipt = managed.check_execution(execution, order, binding)
    tx = copy.deepcopy(transaction_result)
    statuses = copy.deepcopy(status_result)
    if not isinstance(tx, dict) or not isinstance(statuses, dict):
        raise ValueError("Original transaction is not yet observed by RPC")
    slot = _units(tx.get("slot"))
    if not slot or slot != receipt["provider_reported_slot"]:
        raise ValueError("RPC slot differs from original provider receipt")
    if capsule["version"] == 2 and slot < capsule["unsigned_projection"]["simulation_slot"]:
        raise ValueError("Executed transaction predates original unsigned simulation")
    if type(tx.get("version")) is not int or tx["version"] != 0:
        raise ValueError("Unexpected RPC transaction version")
    encoded = tx.get("transaction")
    if not isinstance(encoded, list) or len(encoded) != 2 or encoded[1] != "base64":
        raise ValueError("RPC did not return the original binary transaction")
    actual = managed.packet(encoded[0])
    original = managed.packet(capsule["signed_transaction"])
    if actual.message != original.message or not all(actual.verify_with_results()):
        raise ValueError("RPC original message/signatures do not verify")
    if str(actual.signatures[0]) != receipt["provider_reported_signature"]:
        raise ValueError("RPC first signature is not the queried original signature")
    for i, signature in enumerate(original.signatures):
        if signature != Signature.default() and actual.signatures[i] != signature:
            raise ValueError("RPC changed an original non-placeholder signature")
    context, values = statuses.get("context"), statuses.get("value")
    if (not isinstance(context, dict) or _units(context.get("slot")) < slot
            or not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict)):
        raise ValueError("RPC signature-status context is missing or stale")
    status = values[0]
    confirmation = status.get("confirmationStatus")
    if (_units(status.get("slot")) != slot or "err" not in status or status["err"] is not None
            or confirmation not in {"confirmed", "finalized"}):
        raise ValueError("Original transaction lacks checked RPC confirmation")
    if "status" in status and status["status"] != {"Ok": None}:
        raise ValueError("RPC signature status fields conflict")
    effects = wallet_effects.inspect(order, tx.get("meta"), max_wallet_fee_lamports=capsule["max_wallet_fee_lamports"])
    if effects["actual_input_units"] != receipt["total_input_units"] or effects["actual_output_units"] != receipt["total_output_units"]:
        raise ValueError("RPC wallet quantities differ from original execution receipt")
    if capsule["version"] == 2:
        projected = capsule["unsigned_projection"]
        if (tx["meta"]["loadedAddresses"] != projected["simulation"]["value"]["loadedAddresses"]
                or effects["input_decimals"] != projected["effects"]["input_decimals"]
                or effects["output_decimals"] != projected["effects"]["output_decimals"]):
            raise ValueError("Executed account resolution/decimals differ from original simulation")
    return {**receipt, "chain_wallet_verified": True,
        "requires_reconciliation": confirmation != "finalized", "confirmation_status": confirmation,
        "financial_finality_verified": confirmation == "finalized", "chain_slot": slot,
        **effects,
        **({"unsigned_wallet_projection_checked": True,
            "unsigned_projection_sha256": capsule["unsigned_projection"]["sha256"],
            "unsigned_projection_slot": capsule["unsigned_projection"]["simulation_slot"]}
            if capsule["version"] == 2 else {}),
        "submission_capsule_sha256": capsule["sha256"],
        "rpc_source_sha256": capsule["rpc_source_sha256"],
        "rpc_evidence_sha256": digest({"transaction": tx, "statuses": statuses}),
        "rpc_evidence": {"transaction": tx, "statuses": statuses},
        "rpc_block_inclusion_cryptographically_verified": False,
        "rent_refundability_verified": False}


def validate_receipt(capsule, execution, receipt):
    if not isinstance(receipt, dict) or not isinstance(receipt.get("rpc_evidence"), dict):
        raise ValueError("Financial response lacks independent RPC evidence")
    proof = receipt["rpc_evidence"]
    checked = reconcile(capsule, execution, proof.get("transaction"), proof.get("statuses"))
    if checked != receipt:
        raise ValueError("Independent execution receipt changed")
    return checked
