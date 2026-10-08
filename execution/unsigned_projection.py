"""Original unsigned packet's node-simulated wallet effects, not an execution.

No signer/HTTP/operator key. This cannot prove arbitrary instruction semantics,
delegate/close authority, future execution, block inclusion or profitability.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from datetime import datetime

from solders.message import to_bytes_versioned

from execution import wallet_effects
from execution import jupiter_managed_contract as managed
from execution import authority_observations


MAX_SIGNING_AGE_SECONDS = 5.0


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def check(order, simulation, *, rpc_source_sha256, observed_at, current_node_slot,
          max_wallet_fee_lamports=None):
    """Project original ExactIn request, output floor and wallet reserve.

    Both before/after vectors must come from one simulation-bank response. No
    invented pre-state, replaced blockhash, signed packet or provider receipt.
    Missing recording on older nodes is unknown and cannot authorize signing.
    """
    original = managed.check_order(order.raw, order.request,
        max_wallet_fee_lamports=max_wallet_fee_lamports, historical=True)
    if original.order_sha256 != order.order_sha256 or original.transaction != order.transaction:
        raise ValueError("Original checked order changed before unsigned projection")
    order = original
    if not isinstance(rpc_source_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", rpc_source_sha256) is None:
        raise ValueError("Unsigned projection observation route is missing")
    if not isinstance(observed_at, str):
        raise ValueError("Unsigned projection receipt time is missing")
    received = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if received.tzinfo is None:
        raise ValueError("Unsigned projection receipt time is unzoned")
    raw = copy.deepcopy(simulation)
    if not isinstance(raw, dict) or not isinstance(raw.get("context"), dict):
        raise ValueError("Unsigned simulation-bank context is missing")
    slot = wallet_effects.units(raw["context"].get("slot"))
    current = wallet_effects.units(current_node_slot)
    if current <= 0 or slot < current:
        raise ValueError("Unsigned simulation-bank slot is invalid")
    meta = raw.get("value")
    if not isinstance(meta, dict) or meta.get("replacementBlockhash") is not None:
        raise ValueError("Unsigned simulation did not retain the original blockhash")
    # These fields are not requested and cannot stand in for checked balance
    # vectors. A node that reports their use differs from the submitted options.
    if meta.get("accounts") is not None:
        raise ValueError("Unsigned simulation returned unrequested account data")
    effects = wallet_effects.inspect(order, meta, max_wallet_fee_lamports=max_wallet_fee_lamports)
    authority_observations.check(order, meta)
    if (effects["actual_input_units"] != order.request.amount
            or effects["actual_output_units"] < order.threshold):
        raise ValueError("Unsigned wallet projection differs from original amount/output floor")
    projected = {key.replace("actual_", "projected_", 1): value for key, value in effects.items()}
    body = {"version": 1, "original_order_sha256": order.order_sha256,
        "original_request_sha256": digest(order.request.params()),
        "original_packet_sha256": hashlib.sha256(base64.b64decode(order.raw["transaction"], validate=True)).hexdigest(),
        "original_message_sha256": hashlib.sha256(to_bytes_versioned(order.transaction.message)).hexdigest(),
        "rpc_source_sha256": rpc_source_sha256, "max_wallet_fee_lamports": max_wallet_fee_lamports,
        "observed_at": observed_at, "current_node_slot": current, "simulation_slot": slot, "effects": projected,
        "simulation": raw, "unsigned_wallet_projection_checked": True,
        "observed_wallet_authority_mutations_rejected": True,
        "unsigned_swap_instructions_verified": False, "chain_wallet_verified": False,
        "financial_finality_verified": False, "fill_verified": False}
    return {**body, "sha256": digest(body)}


def validate(order, projection, *, rpc_source_sha256, max_wallet_fee_lamports=None):
    """Recompute durable projection; booleans/checksum alone are never proof."""
    if not isinstance(projection, dict):
        raise ValueError("Original unsigned projection is missing")
    checked = check(order, projection.get("simulation"), rpc_source_sha256=rpc_source_sha256,
        observed_at=projection.get("observed_at"), current_node_slot=projection.get("current_node_slot"),
        max_wallet_fee_lamports=max_wallet_fee_lamports)
    if checked != projection:
        raise ValueError("Original unsigned projection changed")
    return checked
