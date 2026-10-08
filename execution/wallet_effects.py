"""Strict native/SPL effects observed by a node, not instruction semantics.

Shared by unsigned simulation and confirmed-transaction reconciliation. Neither
caller may turn a projected effect into an actual fill or a profitability label.
"""
from __future__ import annotations

from execution import jupiter_managed_contract as managed
from utils.raw_units import raw_uint

SOL = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}


def units(value):
    if type(value) is not int or raw_uint(value) is None:
        raise ValueError("RPC raw balance is not an exact uint64")
    return value


def token_map(values, keys):
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


def inspect(order, meta, *, max_wallet_fee_lamports=None):
    """Read exact effects under the original message's account ordering.

    These observations do not prove delegate/close authority, refundability,
    future program behavior, loaded-address contents or block inclusion.
    """
    if not isinstance(meta, dict) or "err" not in meta or meta["err"] is not None:
        raise ValueError("Original transaction failed or RPC metadata is missing")
    if "status" in meta and meta["status"] != {"Ok": None}:
        raise ValueError("RPC transaction status fields conflict")
    fee = units(meta.get("fee"))
    loaded = meta.get("loadedAddresses")
    if not isinstance(loaded, dict) or not isinstance(loaded.get("writable"), list) or not isinstance(loaded.get("readonly"), list):
        raise ValueError("RPC loaded address recording is unavailable")
    lookups = order.transaction.message.address_table_lookups
    if (len(loaded["writable"]) != sum(len(x.writable_indexes) for x in lookups)
            or len(loaded["readonly"]) != sum(len(x.readonly_indexes) for x in lookups)):
        raise ValueError("RPC loaded account counts differ from original message")
    keys = [str(key) for key in order.transaction.message.account_keys]
    keys += [managed.address(key) for key in loaded["writable"] + loaded["readonly"]]
    if len(keys) > 256 or len(set(keys)) != len(keys):
        raise ValueError("RPC account identity is ambiguous")
    pre, post = meta.get("preBalances"), meta.get("postBalances")
    if not isinstance(pre, list) or not isinstance(post, list) or len(pre) != len(keys) or len(post) != len(keys):
        raise ValueError("RPC native balance vectors do not match the original message")
    pre, post = [units(x) for x in pre], [units(x) for x in post]
    if sum(pre) - sum(post) != fee or meta.get("rewards") not in (None, []):
        raise ValueError("RPC native balance conservation is not established")
    before, after = token_map(meta.get("preTokenBalances"), keys), token_map(meta.get("postTokenBalances"), keys)
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
    limit = max_wallet_fee_lamports
    if limit is not None and (type(limit) is not int or raw_uint(limit) is None):
        raise ValueError("Invalid original wallet reserve")
    if limit is not None and wallet_fee > limit:
        raise ValueError("Actual network fee exceeds original wallet reserve")
    native_delta = post[wallet_index] - pre[wallet_index]
    owned_lamports_delta = sum(post[i] - pre[i] for i in owned)
    locked_lamports_delta = owned_lamports_delta - deltas.get(SOL, 0)
    if limit is not None and wallet_fee + max(0, locked_lamports_delta) > limit:
        raise ValueError("Actual wallet fee/account deposits exceed original reserve")
    native_principal_delta = native_delta + owned_lamports_delta + wallet_fee
    if SOL not in {order.request.input_mint, order.request.output_mint} and native_principal_delta:
        raise ValueError("Unexplained wallet native transfer or sponsorship")
    def delta(mint):
        return native_principal_delta if mint == SOL else deltas.get(mint, 0)
    actual_input, actual_output = -delta(order.request.input_mint), delta(order.request.output_mint)
    if any(x <= 0 or x > 2**64 - 1 for x in (actual_input, actual_output)):
        raise ValueError("RPC aggregate wallet units are nonpositive or overflow")
    if SOL in decimals and decimals[SOL] != 9:
        raise ValueError("Wrapped SOL decimals conflict")
    if SOL in {order.request.input_mint, order.request.output_mint}:
        decimals[SOL] = 9
    if order.request.input_mint not in decimals or order.request.output_mint not in decimals:
        raise ValueError("RPC requested mint decimals are unavailable")
    return {"actual_input_units": actual_input, "actual_output_units": actual_output,
        "input_decimals": decimals[order.request.input_mint], "output_decimals": decimals[order.request.output_mint],
        "network_fee_lamports": fee, "wallet_network_fee_lamports": wallet_fee,
        "wallet_native_delta_lamports": native_delta, "owned_token_account_lamports_delta": owned_lamports_delta}
