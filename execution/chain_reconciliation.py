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

from solders.message import MessageV0
from solders.signature import Signature

from execution import jupiter_managed_contract as managed
from utils.raw_units import raw_uint

SOL = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def make_capsule(order, signed_transaction, binding, *, rpc_source_sha256, max_wallet_fee_lamports=None):
    body = {"version": 1, "request": order.request.params(), "order": copy.deepcopy(order.raw),
        "signed_transaction": signed_transaction, "binding": copy.deepcopy(binding),
        "max_wallet_fee_lamports": max_wallet_fee_lamports, "rpc_source_sha256": rpc_source_sha256}
    capsule = {**body, "sha256": digest(body)}
    validate_capsule(capsule)
    return capsule


def validate_capsule(value):
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
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
    return order, binding


def _units(value):
    if type(value) is not int or raw_uint(value) is None:
        raise ValueError("RPC raw balance is not an exact uint64")
    return value


def _token_map(values, keys):
    if not isinstance(values, list):
        raise ValueError("RPC token balance recording is unavailable")
    result = {}
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("Invalid RPC token balance")
        index = item.get("accountIndex")
        if type(index) is not int or not 0 <= index < len(keys) or index in result:
            raise ValueError("Duplicate or out-of-range RPC token account")
        mint, owner = managed.address(item.get("mint")), managed.address(item.get("owner"))
        program = item.get("programId")
        if program not in TOKEN_PROGRAMS:
            raise ValueError("Unknown RPC token ownership program")
        ui = item.get("uiTokenAmount")
        if not isinstance(ui, dict) or type(ui.get("decimals")) is not int or not 0 <= ui["decimals"] <= 255:
            raise ValueError("Unknown RPC token decimals")
        amount = ui.get("amount")
        if not isinstance(amount, str) or raw_uint(amount) is None:
            raise ValueError("RPC token units require exact integer text")
        result[index] = (mint, owner, program, ui["decimals"], int(amount))
    return result


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
    meta = tx.get("meta")
    if not isinstance(meta, dict) or "err" not in meta or meta["err"] is not None:
        raise ValueError("Original transaction failed or RPC metadata is missing")
    if "status" in meta and meta["status"] != {"Ok": None}:
        raise ValueError("RPC transaction status fields conflict")
    fee = _units(meta.get("fee"))
    keys = list(actual.message.account_keys)
    loaded = meta.get("loadedAddresses")
    if not isinstance(loaded, dict) or not isinstance(loaded.get("writable"), list) or not isinstance(loaded.get("readonly"), list):
        raise ValueError("RPC loaded address recording is unavailable")
    if isinstance(actual.message, MessageV0):
        lookups = actual.message.address_table_lookups
        if (len(loaded["writable"]) != sum(len(x.writable_indexes) for x in lookups)
                or len(loaded["readonly"]) != sum(len(x.readonly_indexes) for x in lookups)):
            raise ValueError("RPC loaded account counts differ from original message")
    keys = [str(key) for key in keys] + [managed.address(key) for key in loaded["writable"] + loaded["readonly"]]
    if len(keys) > 256 or len(set(keys)) != len(keys):
        raise ValueError("RPC account identity is ambiguous")
    pre, post = meta.get("preBalances"), meta.get("postBalances")
    if not isinstance(pre, list) or not isinstance(post, list) or len(pre) != len(keys) or len(post) != len(keys):
        raise ValueError("RPC native balance vectors do not match the original message")
    pre, post = [_units(x) for x in pre], [_units(x) for x in post]
    if sum(pre) - sum(post) != fee or meta.get("rewards") not in (None, []):
        raise ValueError("RPC native balance conservation is not established")
    before, after = _token_map(meta.get("preTokenBalances"), keys), _token_map(meta.get("postTokenBalances"), keys)
    wallet = order.request.taker
    wallet_index = keys.index(wallet)
    owned, deltas, decimals = set(), {}, {}
    for index in before.keys() | after.keys():
        a, b = before.get(index), after.get(index)
        if a is not None and b is not None and a[:4] != b[:4]:
            raise ValueError("RPC token ownership/mint/program/decimals changed")
        if a is None and pre[index] != 0 or b is None and post[index] != 0:
            raise ValueError("Missing token balance is not a proved created/closed account")
        identity = a or b
        if identity[0] == SOL and ((a is not None and a[4] > pre[index]) or (b is not None and b[4] > post[index])):
            raise ValueError("Wrapped SOL token units exceed observed account lamports")
        if identity[1] != wallet:
            continue
        if index == wallet_index:
            raise ValueError("Wallet native account cannot also be a token account")
        mint, _, _, places, _ = identity
        if mint in decimals and decimals[mint] != places:
            raise ValueError("RPC decimals conflict across owned token accounts")
        decimals[mint] = places
        owned.add(index)
        deltas[mint] = deltas.get(mint, 0) + (b[4] if b else 0) - (a[4] if a else 0)
    if any(delta for mint, delta in deltas.items() if mint not in {SOL, order.request.input_mint, order.request.output_mint}):
        raise ValueError("Unrequested wallet token movement")
    wallet_fee = fee if keys[0] == wallet else 0
    limit = capsule["max_wallet_fee_lamports"]
    if limit is not None and wallet_fee > limit:
        raise ValueError("Actual network fee exceeds original wallet reserve")
    native_delta = post[wallet_index] - pre[wallet_index]
    owned_lamports_delta = sum(post[i] - pre[i] for i in owned)
    locked_lamports_delta = owned_lamports_delta - deltas.get(SOL, 0)
    if limit is not None and wallet_fee + max(0, locked_lamports_delta) > limit:
        raise ValueError("Actual wallet fee/account deposits exceed original reserve")
    # These are observed owned-account lamports, not a certification of their
    # rent-exemption/close-authority or of future refundability.
    native_principal_delta = native_delta + owned_lamports_delta + wallet_fee
    if SOL not in {order.request.input_mint, order.request.output_mint} and native_principal_delta:
        raise ValueError("Unexplained wallet native transfer or sponsorship")
    def delta(mint):
        return native_principal_delta if mint == SOL else deltas.get(mint, 0)
    actual_input, actual_output = -delta(order.request.input_mint), delta(order.request.output_mint)
    if actual_input != receipt["total_input_units"] or actual_output != receipt["total_output_units"]:
        raise ValueError("RPC wallet quantities differ from original execution receipt")
    if any(x < 0 or x > 2**64 - 1 for x in (actual_input, actual_output)):
        raise ValueError("RPC aggregate wallet units overflow")
    if SOL in decimals and decimals[SOL] != 9:
        raise ValueError("Wrapped SOL decimals conflict")
    if SOL == order.request.input_mint:
        decimals[SOL] = 9
    if SOL == order.request.output_mint:
        decimals[SOL] = 9
    return {**receipt, "chain_wallet_verified": True,
        "requires_reconciliation": confirmation != "finalized", "confirmation_status": confirmation,
        "financial_finality_verified": confirmation == "finalized", "chain_slot": slot,
        "actual_input_units": actual_input, "actual_output_units": actual_output,
        "input_decimals": decimals[order.request.input_mint], "output_decimals": decimals[order.request.output_mint],
        "network_fee_lamports": fee, "wallet_network_fee_lamports": wallet_fee,
        "wallet_native_delta_lamports": native_delta, "owned_token_account_lamports_delta": owned_lamports_delta,
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
