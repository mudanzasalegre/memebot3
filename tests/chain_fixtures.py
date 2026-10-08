"""Public synthetic RPC observations; never a network or operator key."""
import base64
import copy

from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from execution.chain_reconciliation import SOL

INPUT_ACCOUNT = Pubkey.from_bytes(bytes([31]) * 32)
OUTPUT_ACCOUNT = Pubkey.from_bytes(bytes([32]) * 32)
POOL = Pubkey.from_bytes(bytes([33]) * 32)


def evidence(capsule, execution, *, confirmation="finalized"):
    original = VersionedTransaction.from_bytes(base64.b64decode(capsule["signed_transaction"]))
    signatures = list(original.signatures)
    if signatures[0] == Signature.default():
        sponsor = Keypair.from_seed(bytes([7]) * 32)
        assert original.message.account_keys[0] == sponsor.pubkey()
        signatures[0] = sponsor.sign_message(to_bytes_versioned(original.message))
    actual = VersionedTransaction.populate(original.message, signatures)
    keys = [str(key) for key in actual.message.account_keys]
    req = capsule["request"]
    wallet, pool = keys.index(req["taker"]), keys.index(str(POOL))
    a, b = keys.index(str(INPUT_ACCOUNT)), keys.index(str(OUTPUT_ACCOUNT))
    pre = [1000] * len(keys)
    pre[0] = 1_000_000
    pre[wallet] = 2**63
    pre[a] = pre[b] = 2_039_280
    amount, output, fee = int(req["amount"]), int(execution["totalOutputAmount"]), 6000
    post = pre.copy()
    post[0] -= fee
    def token(index, mint, amount):
        return {"accountIndex": index, "mint": mint, "owner": req["taker"],
            "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "uiTokenAmount": {"amount": str(amount), "decimals": 9 if mint == SOL else 6,
                "uiAmount": None, "uiAmountString": "irrelevant"}}
    if req["inputMint"] == SOL:
        post[wallet] -= amount
        post[pool] += amount
        inputs = (0, 0)
    else:
        inputs = (amount + 1000, 1000)
    if req["outputMint"] == SOL:
        pre[pool] += output
        post[pool] = pre[pool] - output
        post[wallet] += output
        outputs = (0, 0)
    else:
        outputs = (1000, output + 1000)
    tx = {"slot": int(execution["slot"]), "version": 0,
        "transaction": [base64.b64encode(bytes(actual)).decode(), "base64"],
        "meta": {"err": None, "fee": fee, "preBalances": pre, "postBalances": post,
            "loadedAddresses": {"writable": [], "readonly": []}, "rewards": [],
            "preTokenBalances": [token(a, req["inputMint"], inputs[0]), token(b, req["outputMint"], outputs[0])],
            "postTokenBalances": [token(a, req["inputMint"], inputs[1]), token(b, req["outputMint"], outputs[1])]}}
    status = {"context": {"slot": tx["slot"] + 1}, "value": [{"slot": tx["slot"], "err": None,
        "confirmationStatus": confirmation, "confirmations": None if confirmation == "finalized" else 1}]}
    return copy.deepcopy(tx), copy.deepcopy(status)


def simulation(order):
    """A node-bank projection fixture independent of wallet signing/fill status."""
    req = order.request.params()
    unsigned = order.raw["transaction"]
    # evidence only constructs public SDK bytes; no signature is needed for the
    # simulated account vectors. RFQ's placeholder sponsor is not filled here.
    tx = VersionedTransaction.from_bytes(base64.b64decode(unsigned))
    keys = [str(key) for key in tx.message.account_keys]
    wallet, pool = keys.index(req["taker"]), keys.index(str(POOL))
    a, b = keys.index(str(INPUT_ACCOUNT)), keys.index(str(OUTPUT_ACCOUNT))
    pre = [1000] * len(keys)
    pre[0], pre[wallet], pre[a], pre[b] = 1_000_000, 2**63, 2_039_280, 2_039_280
    post = pre.copy()
    post[0] -= 6000
    amount, output = int(req["amount"]), int(order.raw["outAmount"])
    def token(index, mint, amount):
        return {"accountIndex": index, "mint": mint, "owner": req["taker"],
            "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "uiTokenAmount": {"amount": str(amount), "decimals": 9 if mint == SOL else 6}}
    if req["inputMint"] == SOL:
        post[wallet] -= amount
        post[pool] += amount
        inputs = (0, 0)
    else:
        inputs = (amount + 1000, 1000)
    if req["outputMint"] == SOL:
        pre[pool] += output
        post[pool] = pre[pool] - output
        post[wallet] += output
        outputs = (0, 0)
    else:
        outputs = (1000, output + 1000)
    return {"context": {"slot": 450}, "value": {"err": None, "fee": 6000,
        "replacementBlockhash": None, "accounts": None, "innerInstructions": [], "preBalances": pre, "postBalances": post,
        "loadedAddresses": {"writable": [], "readonly": []},
        "preTokenBalances": [token(a, req["inputMint"], inputs[0]), token(b, req["outputMint"], outputs[0])],
        "postTokenBalances": [token(a, req["inputMint"], inputs[1]), token(b, req["outputMint"], outputs[1])]}}
