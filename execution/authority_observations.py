"""Reject observed wallet delegation/authority changes, not a total IDL verifier.

Check original top-level binary instructions and recorded simulation CPIs.
An unknown router/program or unexamined extension is NOT certified safe here.
"""
from __future__ import annotations

import base58

from execution import jupiter_managed_contract as managed
from execution.wallet_effects import TOKEN_PROGRAMS, token_map


def check(order, meta):
    loaded = meta["loadedAddresses"]
    keys = [str(key) for key in order.transaction.message.account_keys] + loaded["writable"] + loaded["readonly"]
    before, after = token_map(meta["preTokenBalances"], keys), token_map(meta["postTokenBalances"], keys)
    owned = {keys[i] for i, item in {**before, **after}.items() if item[1] == order.request.taker}
    wallet = order.request.taker

    def binary(program, accounts, data):
        if program not in TOKEN_PROGRAMS:
            return
        if not data:
            raise ValueError("Observed token instruction has no discriminator")
        # SPL base opcodes: Approve=4, SetAuthority=6, ApproveChecked=13.
        authority_index = {4: 2, 6: 1, 13: 3}.get(data[0])
        if authority_index is not None:
            if len(accounts) <= authority_index:
                raise ValueError("Observed authority instruction lacks account identity")
            if accounts[0] in owned or wallet in accounts[authority_index:]:
                raise ValueError("Unrequested wallet token delegation or authority change")

    for ix in order.transaction.message.instructions:
        binary(keys[ix.program_id_index], [keys[i] for i in ix.accounts], bytes(ix.data))
    groups = meta.get("innerInstructions")
    if not isinstance(groups, list) or len(groups) > len(order.transaction.message.instructions):
        raise ValueError("Unsigned simulation instruction recording is unavailable")
    seen, total = set(), 0
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Invalid unsigned simulation instruction group")
        index, instructions = group.get("index"), group.get("instructions")
        if (type(index) is not int or not 0 <= index < len(order.transaction.message.instructions)
                or index in seen or not isinstance(instructions, list)):
            raise ValueError("Ambiguous unsigned simulation instruction group")
        seen.add(index)
        total += len(instructions)
        if total > 1000:
            raise ValueError("Unsigned simulation instruction recording exceeds bound")
        for ix in instructions:
            if not isinstance(ix, dict):
                raise ValueError("Invalid recorded unsigned instruction")
            program = managed.address(ix.get("programId"))
            if program not in keys:
                raise ValueError("Recorded unsigned instruction program is unresolved")
            if "parsed" in ix:
                if program not in TOKEN_PROGRAMS:
                    # Memo and other official parsers may expose strings or a
                    # different JSON schema. This is only a base token-authority
                    # guard, not approval of every router/program instruction.
                    continue
                parsed = ix["parsed"]
                if not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str) or not isinstance(parsed.get("info"), dict):
                    raise ValueError("Invalid parsed unsigned instruction")
                if program in TOKEN_PROGRAMS and parsed["type"] in {"approve", "approveChecked", "setAuthority"}:
                    info = parsed["info"]
                    subjects = [x for x in info.values() if isinstance(x, str)]
                    subjects += [x for values in info.values() if isinstance(values, list) for x in values if isinstance(x, str)]
                    if wallet in subjects or any(x in owned for x in subjects):
                        raise ValueError("Unrequested recorded wallet delegation or authority change")
            else:
                accounts, data = ix.get("accounts"), ix.get("data")
                if (not isinstance(accounts, list) or not all(isinstance(x, str) for x in accounts)
                        or not isinstance(data, str) or len(data) > 1644):
                    raise ValueError("Invalid partially decoded unsigned instruction")
                accounts = [managed.address(x) for x in accounts]
                if any(x not in keys for x in accounts) or program not in keys:
                    raise ValueError("Recorded unsigned instruction contains unresolved accounts")
                binary(program, accounts, base58.b58decode(data))
