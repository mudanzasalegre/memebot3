"""Unsigned node projections are not fills; synthetic SDK/accounts/HTTP only."""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from datetime import datetime, timezone
from unittest.mock import Mock

import aiohttp
import base58
import base64
import pytest
from solders.hash import Hash
from solders.instruction import Instruction, AccountMeta
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from execution import chain_reconciliation as chain, unsigned_projection as projection
from execution import jupiter_managed_contract as contract
from fetcher import jupiter_router as router
from runtime import owned_dispatch, execution_provenance
from runtime.buy_recovery import BuyRecoveryStore
from db.models import Position
from utils import solana_execution as rpc
from chain_fixtures import simulation, evidence, INPUT_ACCOUNT, OUTPUT_ACCOUNT, POOL
from test_live_signing_boundaries import signer
from test_jupiter_managed_contract import managed, http, Response, order_payload, execution_payload, TOKEN, AMOUNT

ACTUAL_PROJECT = rpc.project_unsigned
ENDPOINT = "https://synthetic.invalid/?api-key=synthetic-node-key"
SOURCE = rpc.endpoint_fingerprint(ENDPOINT)
DEFAULT_SIMULATION = object()


def checked(signer, *, side="buy", route="metis", sponsor=None):
    raw = order_payload(str(signer.PUBLIC_KEY), amount=AMOUNT if side == "buy" else 400,
        router_name=route, sponsor=sponsor)
    if side == "sell": raw.update(inputMint=TOKEN, outputMint=chain.SOL, feeMint=TOKEN)
    request = contract.ManagedRequest(raw["inputMint"], raw["outputMint"], int(raw["inAmount"]), raw["taker"], 100)
    return contract.check_order(raw, request)


def proof(order, raw=DEFAULT_SIMULATION, *, cap=6000, source=SOURCE):
    return projection.check(order, simulation(order) if raw is DEFAULT_SIMULATION else raw,
        rpc_source_sha256=source, observed_at=datetime.now(timezone.utc).isoformat(),
        current_node_slot=449, max_wallet_fee_lamports=cap)


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("route", ["metis", "jupiterz", "dflow", "okx"])
def test_original_unsigned_projection_has_no_signature_or_financial_claim(signer, side, route):
    order = checked(signer, side=side, route=route)
    receipt = proof(order)
    assert not any(order.transaction.verify_with_results())
    assert receipt["effects"]["projected_input_units"] == order.request.amount
    assert receipt["effects"]["projected_output_units"] == int(order.raw["outAmount"])
    assert receipt["unsigned_wallet_projection_checked"] is True
    for flag in ("chain_wallet_verified", "financial_finality_verified", "fill_verified", "unsigned_swap_instructions_verified"):
        assert receipt[flag] is False
    assert projection.validate(order, receipt, rpc_source_sha256=SOURCE, max_wallet_fee_lamports=6000) == receipt


@pytest.mark.parametrize("fault", ["null", "no_context", "slot_bool", "slot_stale", "no_meta", "err", "no_err",
    "replaced", "accounts", "fee_bool", "fee_excess", "no_balances", "float_balance", "conservation",
    "no_tokens", "owner", "program", "decimals", "token_float", "token_duplicate", "input", "output_floor",
    "loaded", "native_tip", "wallet_token_authority"])
def test_missing_conflicting_or_unrequested_wallet_effects_cannot_authorize_signing(signer, fault):
    order = checked(signer)
    raw = simulation(order)
    meta = raw["value"]
    wallet = list(map(str, order.transaction.message.account_keys)).index(order.request.taker)
    if fault == "null": raw = None
    elif fault == "no_context": del raw["context"]
    elif fault == "slot_bool": raw["context"]["slot"] = True
    elif fault == "slot_stale": raw["context"]["slot"] = 448
    elif fault == "no_meta": del raw["value"]
    elif fault == "err": meta["err"] = {"InstructionError": [0, "synthetic"]}
    elif fault == "no_err": del meta["err"]
    elif fault == "replaced": meta["replacementBlockhash"] = {"blockhash": "different"}
    elif fault == "accounts": meta["accounts"] = []
    elif fault == "fee_bool": meta["fee"] = True
    elif fault == "fee_excess":
        meta["fee"] += 1
        meta["postBalances"][0] -= 1
    elif fault == "no_balances": del meta["preBalances"]
    elif fault == "float_balance": meta["preBalances"][0] = float(meta["preBalances"][0])
    elif fault == "conservation": meta["postBalances"][0] += 1
    elif fault == "no_tokens": del meta["postTokenBalances"]
    elif fault == "owner": meta["postTokenBalances"][1]["owner"] = TOKEN
    elif fault == "program": meta["postTokenBalances"][1]["programId"] = TOKEN
    elif fault == "decimals": meta["postTokenBalances"][1]["uiTokenAmount"]["decimals"] = True
    elif fault == "token_float": meta["postTokenBalances"][1]["uiTokenAmount"]["amount"] = 2001000.
    elif fault == "token_duplicate": meta["postTokenBalances"].append(copy.deepcopy(meta["postTokenBalances"][1]))
    elif fault in {"input", "native_tip"}:
        meta["postBalances"][wallet] -= 1
        other = next(i for i in range(len(meta["postBalances"])) if i != wallet)
        meta["postBalances"][other] += 1
    elif fault == "output_floor": meta["postTokenBalances"][1]["uiTokenAmount"]["amount"] = str(order.threshold + 999)
    elif fault == "loaded": meta["loadedAddresses"]["writable"] = [TOKEN]
    elif fault == "wallet_token_authority": meta["preTokenBalances"][0]["accountIndex"] = wallet
    with pytest.raises(ValueError): proof(order, raw)


@pytest.mark.parametrize("field,value", [("fill_verified", True), ("simulation_slot", 451),
    ("rpc_source_sha256", "0" * 64), ("original_message_sha256", "0" * 64),
    ("max_wallet_fee_lamports", 6001), ("current_node_slot", True), ("observed_at", "2026-10-08T12:00:00")])
def test_durable_flags_or_rechecksummed_mutation_do_not_replace_original_proof(signer, field, value):
    order = checked(signer)
    receipt = proof(order)
    receipt[field] = value
    receipt["sha256"] = projection.digest({k: v for k, v in receipt.items() if k != "sha256"})
    with pytest.raises(ValueError): projection.validate(order, receipt, rpc_source_sha256=SOURCE, max_wallet_fee_lamports=6000)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["metis", "jupiterz", "dflow", "okx"])
async def test_adapter_checks_unsigned_before_sign_and_preserves_distinct_actual_fill(managed, http, monkeypatch, route):
    raw = order_payload(str(managed.PUBLIC_KEY), router_name=route)
    execute = execution_payload(raw, managed)
    http.replies.extend([Response(raw), Response(execute)])
    events = []
    prior_project = rpc.project_unsigned
    prior_sign = managed.sign_base64_transaction
    async def project(*args, **kwargs):
        events.append("projection")
        return await prior_project(*args, **kwargs)
    def sign(*args):
        events.append("sign")
        return prior_sign(*args)
    monkeypatch.setattr(rpc, "project_unsigned", project)
    monkeypatch.setattr(managed, "sign_base64_transaction", sign)
    result = await router.execute_managed_swap(input_mint=chain.SOL, output_mint=TOKEN,
        amount_lamports=AMOUNT, slippage_bps=100, max_wallet_fee_lamports=6000)
    assert events == ["projection", "sign"]
    capsule = result["submission_capsule"]
    assert capsule["version"] == 2 and capsule["unsigned_projection"]["fill_verified"] is False
    assert capsule["unsigned_projection"]["effects"]["projected_output_units"] == 2_000_000
    assert result["qty_lamports"] == 1_985_000
    assert result["execution_receipt"]["unsigned_wallet_projection_checked"] is True
    assert result["execution_receipt"]["unsigned_swap_instructions_verified"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "fake_flags", "wrong_source", "aged", "error"])
async def test_pre_sign_adapter_failures_never_sign_or_execute_post(managed, http, monkeypatch, fault):
    raw = order_payload(str(managed.PUBLIC_KEY))
    http.replies.append(Response(raw))
    prior_project = rpc.project_unsigned
    async def project(*args, **kwargs):
        if fault == "error": raise RuntimeError("synthetic unavailable node")
        receipt, stamp = await prior_project(*args, **kwargs)
        if fault == "missing": receipt = None
        elif fault == "fake_flags": receipt = {"unsigned_wallet_projection_checked": True}
        elif fault == "wrong_source": receipt["rpc_source_sha256"] = "0" * 64
        elif fault == "aged": stamp -= 6
        return receipt, stamp
    monkeypatch.setattr(rpc, "project_unsigned", project)
    sign = Mock(wraps=managed.sign_base64_transaction)
    monkeypatch.setattr(managed, "sign_base64_transaction", sign)
    with pytest.raises(router.SwapPreparationError):
        await router.execute_managed_swap(input_mint=chain.SOL, output_mint=TOKEN, amount_lamports=AMOUNT, slippage_bps=100)
    sign.assert_not_called()
    assert [x[0] for x in http.calls] == ["GET"] and owned_dispatch.pending_dispatch_count() == 0


@pytest.mark.asyncio
async def test_age_after_signing_is_still_proved_pre_post_without_new_packet(managed, http, monkeypatch):
    raw = order_payload(str(managed.PUBLIC_KEY))
    http.replies.append(Response(raw))
    clock = [100.0]
    monkeypatch.setattr(rpc.time, "monotonic", lambda: clock[0])
    prior_sign = managed.sign_base64_transaction
    def sign(encoded):
        clock[0] += 6
        return prior_sign(encoded)
    sign_mock = Mock(side_effect=sign)
    monkeypatch.setattr(managed, "sign_base64_transaction", sign_mock)
    with pytest.raises(router.SwapPreparationError):
        await router.execute_managed_swap(input_mint=chain.SOL, output_mint=TOKEN, amount_lamports=AMOUNT, slippage_bps=100)
    assert sign_mock.call_count == 1 and [x[0] for x in http.calls] == ["GET"]


@pytest.mark.asyncio
async def test_owned_execute_queue_age_does_not_persist_dispatch_started(managed, http, monkeypatch, tmp_path):
    raw = order_payload(str(managed.PUBLIC_KEY))
    http.replies.append(Response(raw))
    clock = [100.0]
    monkeypatch.setattr(rpc.time, "monotonic", lambda: clock[0])
    prior_owned = router.run_owned_sync
    async def owned_call(fn, *args, **kwargs):
        if fn is router._execute_managed_once: clock[0] += 6
        return await prior_owned(fn, *args, **kwargs)
    monkeypatch.setattr(router, "run_owned_sync", owned_call)
    store = BuyRecoveryStore(tmp_path)
    with store.scope():
        attempt = store.begin(Position(address=TOKEN, token_mint=TOKEN, dry_run=False, buy_amount_sol=.1), paper=False, amount_sol=.1)
        with attempt.execution_scope(), pytest.raises(router.SwapPreparationError):
            await router.execute_managed_swap(input_mint=chain.SOL, output_mint=TOKEN, amount_lamports=AMOUNT, slippage_bps=100)
        assert "capsule" in attempt.row["execution"] and "dispatch_started" not in attempt.row["execution"]
        attempt.receive({"qty_lamports": 0, "signature": "NO_JUP_ORDER"})
        assert attempt.row["state"] == "no_fill" and [x[0] for x in http.calls] == ["GET"]


@pytest.mark.asyncio
async def test_original_order_cannot_mutate_in_owned_signing_queue(managed, http, monkeypatch):
    raw = order_payload(str(managed.PUBLIC_KEY))
    http.replies.append(Response(raw))
    prior_owned = router.run_owned_sync
    async def owned_call(fn, *args, **kwargs):
        if fn is router._sign_managed_projected:
            args[1].raw["requestId"] = "different-request"
        return await prior_owned(fn, *args, **kwargs)
    monkeypatch.setattr(router, "run_owned_sync", owned_call)
    sign = Mock(wraps=managed.sign_base64_transaction)
    monkeypatch.setattr(managed, "sign_base64_transaction", sign)
    with pytest.raises(router.SwapPreparationError):
        await router.execute_managed_swap(input_mint=chain.SOL, output_mint=TOKEN, amount_lamports=AMOUNT, slippage_bps=100)
    sign.assert_not_called()
    assert [x[0] for x in http.calls] == ["GET"]


@pytest.mark.asyncio
async def test_cancelling_unsigned_rpc_cannot_invoke_owned_signer(managed, http, monkeypatch):
    raw = order_payload(str(managed.PUBLIC_KEY))
    http.replies.append(Response(raw))
    entered = asyncio.Event()
    async def project(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(rpc, "project_unsigned", project)
    sign = Mock(wraps=managed.sign_base64_transaction)
    monkeypatch.setattr(managed, "sign_base64_transaction", sign)
    task = asyncio.create_task(router.execute_managed_swap(input_mint=chain.SOL, output_mint=TOKEN, amount_lamports=AMOUNT, slippage_bps=100))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    sign.assert_not_called()
    assert [x[0] for x in http.calls] == ["GET"] and owned_dispatch.pending_dispatch_count() == 0


def test_original_pre_sign_projection_is_rechecked_on_actual_chain_and_restart(signer):
    order = checked(signer)
    signed = signer.sign_base64_transaction(order.raw["transaction"])
    cap = chain.make_capsule(order, signed, contract.check_signed_packet(order, signed),
        rpc_source_sha256=SOURCE, max_wallet_fee_lamports=6000, unsigned_projection=proof(order))
    execute = execution_payload(order.raw, signer)
    tx, status = evidence(cap, execute)
    receipt = chain.reconcile(cap, execute, tx, status)
    assert chain.validate_receipt(cap, execute, receipt) == receipt
    assert receipt["unsigned_projection_sha256"] == cap["unsigned_projection"]["sha256"]
    tx["slot"] = 449
    execute["slot"] = "449"
    status["value"][0]["slot"] = 449
    with pytest.raises(ValueError): chain.reconcile(cap, execute, tx, status)
    corrupt = copy.deepcopy(cap)
    corrupt["unsigned_projection"]["effects"]["projected_output_units"] += 1
    corrupt["sha256"] = chain.digest({k: v for k, v in corrupt.items() if k != "sha256"})
    with pytest.raises(ValueError): chain.validate_capsule(corrupt)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "http", "wrong_id", "duplicate", "null", "slot_bool", "stale",
    "missing_balances", "replaced", "timeout", "connection", "oversized", "malformed", "minslot"])
async def test_actual_keyless_rpc_uses_original_unsigned_packet_and_strict_current_bank(monkeypatch, signer, fault):
    order = checked(signer)
    calls, closed = [], []
    raw_sim = simulation(order)
    class Stream:
        def __init__(self, content): self.remaining = content
        async def read(self, bound):
            chunk, self.remaining = self.remaining[:min(31, bound)], self.remaining[min(31, bound):]
            return chunk
    class Reply:
        def __init__(self, request):
            self.status = 403 if fault == "http" else 200
            current = request["method"] == "getSlot"
            result = 449 if current else copy.deepcopy(raw_sim)
            if fault == "slot_bool" and current: result = True
            if not current:
                if fault == "null": result = None
                elif fault == "stale": result["context"]["slot"] = 448
                elif fault == "missing_balances": del result["value"]["preBalances"]
                elif fault == "replaced": result["value"]["replacementBlockhash"] = {}
            if fault == "minslot" and current: result = 451
            data = {"jsonrpc": "2.0", "id": request["id"], "result": result}
            if fault == "wrong_id": data["id"] = "unrelated"
            raw = json.dumps(data).encode()
            if fault == "duplicate": raw = raw.replace(b'"jsonrpc": "2.0"', b'"jsonrpc": "2.0", "jsonrpc": "2.0"')
            if fault == "oversized": raw = b"x" * (2 * 1024 * 1024 + 1)
            if fault == "malformed": raw = b"not JSON"
            self.content = Stream(raw)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): closed.append("response")
    class Session:
        def __init__(self, **kwargs):
            assert kwargs["timeout"].total == 8 and "headers" not in kwargs
        async def __aenter__(self): return self
        async def __aexit__(self, *args): closed.append("session")
        def post(self, url, **kwargs):
            calls.append((url, copy.deepcopy(kwargs)))
            assert url == ENDPOINT and kwargs["allow_redirects"] is False
            if fault == "timeout": raise asyncio.TimeoutError("must-not-leak-url-or-credentials")
            if fault == "connection": raise aiohttp.ClientConnectionError("must-not-leak-url-or-credentials")
            return Reply(kwargs["json"])
    monkeypatch.setattr(rpc.aiohttp, "ClientSession", Session)
    if fault is not None:
        with pytest.raises((ValueError, RuntimeError)) as exc:
            await ACTUAL_PROJECT(order, endpoint=ENDPOINT, max_wallet_fee_lamports=6000)
        assert "must-not-leak" not in str(exc.value) and len(calls) <= 2
    else:
        receipt, stamp = await ACTUAL_PROJECT(order, endpoint=ENDPOINT, max_wallet_fee_lamports=6000)
        assert receipt["simulation_slot"] == 450 and receipt["current_node_slot"] == 449
        assert receipt["rpc_source_sha256"] == SOURCE and time.monotonic() - stamp < 5
        assert [x[1]["json"]["method"] for x in calls] == ["getSlot", "simulateTransaction"]
        assert calls[1][1]["json"]["params"] == [order.raw["transaction"], {"encoding": "base64", "commitment": "confirmed",
            "sigVerify": False, "replaceRecentBlockhash": False, "minContextSlot": 449, "innerInstructions": True}]
        assert calls[0][1]["json"]["id"] != calls[1][1]["json"]["id"]
    assert "session" in closed


def authority_order(signer, program, *, top=None):
    owner = signer.PUBLIC_KEY
    accounts = [AccountMeta(owner, True, True), AccountMeta(INPUT_ACCOUNT, False, True),
        AccountMeta(OUTPUT_ACCOUNT, False, True), AccountMeta(POOL, False, True),
        AccountMeta(Pubkey.from_string(program), False, False)]
    instructions = [Instruction(Pubkey.new_unique(), b"synthetic-swap", accounts)]
    if top is not None:
        auth = [AccountMeta(INPUT_ACCOUNT, False, True), AccountMeta(POOL, False, False), AccountMeta(owner, True, False)]
        data = bytes([top]) + (1000).to_bytes(8, "little")
        if top == 6:
            auth = [AccountMeta(INPUT_ACCOUNT, False, True), AccountMeta(owner, True, False)]
            data = bytes([6, 3, 1]) + bytes(POOL)  # Set close authority, no token balance change.
        elif top == 13:
            auth.insert(1, AccountMeta(Pubkey.from_string(chain.SOL), False, False))
            data += bytes([9])
        instructions.append(Instruction(Pubkey.from_string(program), data, auth))
    message = MessageV0.try_compile(owner, instructions, [], Hash.new_unique())
    raw = order_payload(str(owner))
    raw["transaction"] = base64.b64encode(bytes(VersionedTransaction.populate(message, [Signature.default()]))).decode()
    return contract.check_order(raw, contract.ManagedRequest(chain.SOL, TOKEN, AMOUNT, str(owner), 100))


@pytest.mark.parametrize("program", sorted(chain.TOKEN_PROGRAMS))
@pytest.mark.parametrize("opcode", [4, 6, 13])
def test_original_binary_approval_or_close_authority_change_rejected_despite_valid_balances(signer, program, opcode):
    order = authority_order(signer, program, top=opcode)
    with pytest.raises(ValueError, match="delegation or authority"):
        proof(order)


@pytest.mark.parametrize("program", sorted(chain.TOKEN_PROGRAMS))
@pytest.mark.parametrize("kind", ["approve", "approveChecked", "setAuthority", "partially_decoded"])
def test_recorded_router_cpi_cannot_grant_wallet_permissions_with_unchanged_balances(signer, program, kind):
    order = authority_order(signer, program)
    raw = simulation(order)
    if kind == "partially_decoded":
        ix = {"programId": program, "accounts": [str(INPUT_ACCOUNT), str(POOL), str(signer.PUBLIC_KEY)],
            "data": base58.b58encode(bytes([4]) + (1000).to_bytes(8, "little")).decode()}
    else:
        ix = {"programId": program, "parsed": {"type": kind, "info": {"source": str(INPUT_ACCOUNT),
            "owner": str(signer.PUBLIC_KEY), "delegate": str(POOL)}}}
    raw["value"]["innerInstructions"] = [{"index": 0, "instructions": [ix]}]
    with pytest.raises(ValueError, match="delegation or authority"):
        proof(order, raw)


@pytest.mark.parametrize("fault", ["absent", "null", "duplicate", "bad_index", "bool_index", "missing_instructions",
    "missing_program", "foreign_program", "bad_parsed", "missing_accounts", "bad_base58", "foreign_accounts"])
def test_missing_or_ambiguous_instruction_recording_cannot_be_a_pre_sign_proof(signer, fault):
    program = sorted(chain.TOKEN_PROGRAMS)[0]
    order = authority_order(signer, program)
    raw = simulation(order)
    ix = {"programId": program, "parsed": {"type": "transfer", "info": {"source": str(INPUT_ACCOUNT)}}}
    group = {"index": 0, "instructions": [ix]}
    raw["value"]["innerInstructions"] = [group]
    if fault == "absent": del raw["value"]["innerInstructions"]
    elif fault == "null": raw["value"]["innerInstructions"] = None
    elif fault == "duplicate": raw["value"]["innerInstructions"].append(copy.deepcopy(group))
    elif fault == "bad_index": group["index"] = 4
    elif fault == "bool_index": group["index"] = True
    elif fault == "missing_instructions": del group["instructions"]
    elif fault == "missing_program": del ix["programId"]
    elif fault == "foreign_program": ix["programId"] = TOKEN
    elif fault == "bad_parsed": ix["parsed"] = {"type": "approve"}
    else:
        ix.clear()
        ix.update(programId=program, accounts=[str(INPUT_ACCOUNT), str(POOL), str(signer.PUBLIC_KEY)],
            data=base58.b58encode(bytes([3]) + (1).to_bytes(8, "little")).decode())
        if fault == "missing_accounts": del ix["accounts"]
        elif fault == "bad_base58": ix["data"] = "not-base58-!"
        elif fault == "foreign_accounts": ix["accounts"][0] = TOKEN
    with pytest.raises(ValueError): proof(order, raw)


def test_known_pool_delegation_without_wallet_scope_is_not_an_arbitrary_router_veto(signer):
    program = sorted(chain.TOKEN_PROGRAMS)[0]
    order = authority_order(signer, program)
    raw = simulation(order)
    raw["value"]["innerInstructions"] = [{"index": 0, "instructions": [{"programId": program,
        "parsed": {"type": "approve", "info": {"source": str(POOL), "owner": str(POOL), "delegate": str(POOL)}}}]}]
    assert proof(order, raw)["unsigned_swap_instructions_verified"] is False


def test_unknown_auxiliary_parsed_schema_is_not_mistaken_for_token_authority(signer):
    order = checked(signer)
    raw = simulation(order)
    auxiliary = str(order.transaction.message.account_keys[order.transaction.message.instructions[0].program_id_index])
    raw["value"]["innerInstructions"] = [{"index": 0, "instructions": [{"programId": auxiliary,
        "parsed": "synthetic memo string, not a token instruction"}]}]
    assert proof(order, raw)["unsigned_swap_instructions_verified"] is False
