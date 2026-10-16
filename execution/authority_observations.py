"""Reject observed wallet authority/account mutations, not a total IDL verifier.

Check original top-level binary instructions and recorded simulation CPIs.
An unknown router/program or unexamined extension is NOT certified safe here.
"""
from __future__ import annotations

import base58

from execution import jupiter_managed_contract as managed
from execution.wallet_effects import TOKEN_PROGRAMS, token_map


SYSTEM_PROGRAM = "11111111111111111111111111111111"
# Original SystemInstruction u32 discriminants and Agave jsonParsed targets.
# Protect the taker itself, not a payer merely funding a separate new account.
# CreateAccountAllowPrefund (13) targets account0, unlike CreateAccount (0/3).
_SYSTEM_MUTATIONS = {
    0: (1, "createAccount", "newAccount"),
    1: (0, "assign", "account"),
    3: (1, "createAccountWithSeed", "newAccount"),
    4: (0, "advanceNonce", "nonceAccount"),
    5: (0, "withdrawFromNonce", "nonceAccount"),
    6: (0, "initializeNonce", "nonceAccount"),
    7: (0, "authorizeNonce", "nonceAccount"),
    8: (0, "allocate", "account"),
    9: (0, "allocateWithSeed", "account"),
    10: (0, "assignWithSeed", "account"),
    12: (0, "upgradeNonce", "nonceAccount"),
    13: (0, "createAccountAllowPrefund", "newAccount"),
}


def _system_binary(accounts, data, wallet):
    if len(data) < 4:
        raise ValueError("Observed system instruction lacks its discriminator")
    opcode = int.from_bytes(data[:4], "little")
    spec = _SYSTEM_MUTATIONS.get(opcode)
    if spec is None:
        return  # Transfers/unknown opcodes are not certified semantically here.
    target = spec[0]
    if len(accounts) <= max(target, 1 if opcode == 7 else target):
        raise ValueError("Observed system mutation lacks account identity")
    if accounts[target] == wallet or opcode == 7 and accounts[1] == wallet:
        raise ValueError("Unrequested wallet system-account mutation")


def _system_parsed(parsed, keys, wallet):
    if (not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str)
            or not isinstance(parsed.get("info"), dict)):
        raise ValueError("Invalid parsed system instruction")
    spec = next((row for row in _SYSTEM_MUTATIONS.values() if row[1] == parsed["type"]), None)
    if spec is None:
        return  # Not a complete current/future System Program decoder.
    fields = [spec[2]] + (["nonceAuthority"] if parsed["type"] == "authorizeNonce" else [])
    subjects = [managed.address(parsed["info"].get(field)) for field in fields]
    if any(subject not in keys for subject in subjects):
        raise ValueError("Recorded system mutation account is unresolved")
    if wallet in subjects:
        raise ValueError("Unrequested wallet system-account mutation")


def check(order, meta):
    loaded = meta["loadedAddresses"]
    keys = [str(key) for key in order.transaction.message.account_keys] + loaded["writable"] + loaded["readonly"]
    before, after = token_map(meta["preTokenBalances"], keys), token_map(meta["postTokenBalances"], keys)
    owned = {keys[i] for i, item in {**before, **after}.items() if item[1] == order.request.taker}
    wallet = order.request.taker

    def binary(program, accounts, data):
        if program == SYSTEM_PROGRAM:
            _system_binary(accounts, data, wallet)
            return
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
                if program == SYSTEM_PROGRAM:
                    _system_parsed(ix["parsed"], keys, wallet)
                    continue
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
