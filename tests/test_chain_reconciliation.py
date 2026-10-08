"""Original SDK packets, synthetic RPC, durable journals; no operator or network."""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from execution import chain_reconciliation as chain
from execution import jupiter_managed_contract as managed_contract
from fetcher import jupiter_router as router
from runtime.buy_recovery import BuyRecoveryStore, BuyOutcomeUncertain
from runtime.sell_recovery import SellRecoveryStore, SellOutcomeUncertain
from runtime import execution_provenance, owned_dispatch
from trader import buyer, seller
from db.models import Position
from utils import solana_execution
from test_jupiter_managed_contract import (managed, signer, http, Response, order_payload,
    execution_payload, buyer_guards, TOKEN, AMOUNT)
from chain_fixtures import INPUT_ACCOUNT, OUTPUT_ACCOUNT, POOL, evidence


def prepared(signer, *, side="buy", sponsor=False, cap=None):
    fee_payer = Keypair.from_seed(bytes([7]) * 32) if sponsor else None
    raw = order_payload(str(signer.PUBLIC_KEY), sponsor=fee_payer,
        router_name="jupiterz" if sponsor else "metis", amount=AMOUNT if side == "buy" else 400)
    if side == "sell": raw.update(inputMint=TOKEN, outputMint=chain.SOL, feeMint=TOKEN)
    request = managed_contract.ManagedRequest(raw["inputMint"], raw["outputMint"], int(raw["inAmount"]), raw["taker"], 100)
    order = managed_contract.check_order(raw, request)
    signed = signer.sign_base64_transaction(raw["transaction"])
    capsule = chain.make_capsule(order, signed, managed_contract.check_signed_packet(order, signed),
        rpc_source_sha256=solana_execution.endpoint_fingerprint("https://synthetic.invalid"), max_wallet_fee_lamports=cap)
    execute = execution_payload(raw, signer, sponsor=fee_payer)
    tx, status = evidence(capsule, execute)
    return capsule, execute, tx, status


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("sponsor", [False, True])
@pytest.mark.parametrize("confirmation", ["confirmed", "finalized"])
def test_original_sdk_native_spl_and_sponsored_wallet_proofs(signer, side, sponsor, confirmation):
    capsule, execute, tx, status = prepared(signer, side=side, sponsor=sponsor)
    status["value"][0]["confirmationStatus"] = confirmation
    receipt = chain.reconcile(capsule, execute, tx, status)
    assert receipt["actual_input_units"] == int(execute["totalInputAmount"])
    assert receipt["actual_output_units"] == int(execute["totalOutputAmount"])
    assert receipt["wallet_network_fee_lamports"] == (0 if sponsor else 6000)
    assert receipt["financial_finality_verified"] is (confirmation == "finalized")
    assert receipt["requires_reconciliation"] is (confirmation != "finalized")
    assert receipt["unsigned_swap_instructions_verified"] is False
    assert receipt["rpc_block_inclusion_cryptographically_verified"] is False
    assert chain.validate_receipt(capsule, execute, receipt) == receipt
    tx["meta"]["fee"] = 0
    assert receipt["rpc_evidence"]["transaction"]["meta"]["fee"] == 6000


@pytest.mark.parametrize("corruption", ["missing", "version", "slot", "err", "missing_err", "fee_bool",
    "fee_float", "balances_missing", "balance_float", "balance_overflow", "conservation", "token_missing",
    "token_duplicate", "index_bool", "index_outside", "owner_missing", "owner_changed", "mint_changed",
    "decimals_bool", "decimals_changed", "raw_float", "program", "output_different", "input_different",
    "missing_record_with_balance", "loaded_missing", "loaded_count", "message", "signature",
    "processed", "status_error", "status_missing_err", "status_slot", "context_stale", "status_missing",
    "status_multiple", "encoding", "status_conflict", "meta_conflict", "wrapped_lamports"])
def test_unproved_original_chain_observations_are_never_a_fill(signer, corruption):
    cap, ex, tx, status = prepared(signer)
    meta = tx["meta"]
    if corruption == "missing": tx = None
    elif corruption == "version": tx["version"] = True
    elif corruption == "slot": tx["slot"] += 1
    elif corruption == "err": meta["err"] = {"InstructionError": [0, "synthetic"]}
    elif corruption == "missing_err": del meta["err"]
    elif corruption == "fee_bool": meta["fee"] = True
    elif corruption == "fee_float": meta["fee"] = 6000.
    elif corruption == "balances_missing": del meta["preBalances"]
    elif corruption == "balance_float": meta["postBalances"][0] = float(meta["postBalances"][0])
    elif corruption == "balance_overflow": meta["postBalances"][0] = 2**64
    elif corruption == "conservation": meta["postBalances"][0] += 1
    elif corruption == "token_missing": del meta["postTokenBalances"]
    elif corruption == "token_duplicate": meta["postTokenBalances"].append(copy.deepcopy(meta["postTokenBalances"][0]))
    elif corruption == "index_bool": meta["postTokenBalances"][0]["accountIndex"] = True
    elif corruption == "index_outside": meta["postTokenBalances"][0]["accountIndex"] = 255
    elif corruption == "owner_missing": del meta["postTokenBalances"][0]["owner"]
    elif corruption == "owner_changed": meta["postTokenBalances"][0]["owner"] = TOKEN
    elif corruption == "mint_changed": meta["postTokenBalances"][0]["mint"] = TOKEN
    elif corruption == "decimals_bool": meta["postTokenBalances"][0]["uiTokenAmount"]["decimals"] = True
    elif corruption == "decimals_changed": meta["postTokenBalances"][0]["uiTokenAmount"]["decimals"] = 8
    elif corruption == "raw_float": meta["postTokenBalances"][1]["uiTokenAmount"]["amount"] = 1985000.
    elif corruption == "program": meta["postTokenBalances"][0]["programId"] = TOKEN
    elif corruption == "output_different": meta["postTokenBalances"][1]["uiTokenAmount"]["amount"] = "1986001"
    elif corruption == "input_different": ex["totalInputAmount"] = str(AMOUNT + 1)
    elif corruption == "missing_record_with_balance": meta["preTokenBalances"].pop()
    elif corruption == "loaded_missing": del meta["loadedAddresses"]
    elif corruption == "loaded_count": meta["loadedAddresses"]["writable"] = [TOKEN]
    elif corruption == "message": tx["transaction"][0] = prepared(signer)[2]["transaction"][0]
    elif corruption == "signature":
        actual = VersionedTransaction.from_bytes(base64.b64decode(tx["transaction"][0]))
        wrong = Keypair.from_seed(bytes([9]) * 32).sign_message(b"not original")
        tx["transaction"][0] = base64.b64encode(bytes(VersionedTransaction.populate(actual.message, [wrong]))).decode()
    elif corruption == "processed": status["value"][0]["confirmationStatus"] = "processed"
    elif corruption == "status_error": status["value"][0]["err"] = {"error": "synthetic"}
    elif corruption == "status_missing_err": del status["value"][0]["err"]
    elif corruption == "status_slot": status["value"][0]["slot"] += 1
    elif corruption == "context_stale": status["context"]["slot"] = 1
    elif corruption == "status_missing": status["value"] = [None]
    elif corruption == "status_multiple": status["value"] *= 2
    elif corruption == "encoding": tx["transaction"][1] = "json"
    elif corruption == "status_conflict": status["value"][0]["status"] = {"Err": "synthetic"}
    elif corruption == "meta_conflict": meta["status"] = {"Err": "synthetic"}
    elif corruption == "wrapped_lamports": meta["preTokenBalances"][0]["uiTokenAmount"]["amount"] = "2039281"
    with pytest.raises((ValueError, KeyError)): chain.reconcile(cap, ex, tx, status)


@pytest.mark.parametrize("closed", [False, True])
def test_owned_account_created_closed_native_deposits_are_separate_from_principal(signer, closed):
    cap, ex, tx, status = prepared(signer)
    meta = tx["meta"]
    owner = [str(x) for x in managed_contract.packet(cap["signed_transaction"]).message.account_keys].index(cap["request"]["taker"])
    index = meta["postTokenBalances"][1]["accountIndex"]
    rent = 2_039_280
    if closed:
        # Close the empty native-input token account and refund its deposit.
        index = meta["preTokenBalances"][0]["accountIndex"]
        meta["postTokenBalances"].pop(0)
        meta["postBalances"][index] = 0
        meta["postBalances"][owner] += rent
    else:
        # New output account had no native or token account before the swap.
        meta["preTokenBalances"].pop(1)
        meta["preBalances"][index] = 0
        meta["postBalances"][owner] -= rent
        meta["postTokenBalances"][1]["uiTokenAmount"]["amount"] = ex["totalOutputAmount"]
    result = chain.reconcile(cap, ex, tx, status)
    assert result["actual_input_units"] == AMOUNT
    assert result["owned_token_account_lamports_delta"] == (-rent if closed else rent)


@pytest.mark.parametrize("cap", [5999, 6000])
def test_original_actual_network_budget_is_checked_not_estimated(signer, cap):
    capsule, execute, tx, status = prepared(signer)
    capsule["max_wallet_fee_lamports"] = cap
    capsule["sha256"] = chain.digest({k: v for k, v in capsule.items() if k != "sha256"})
    if cap == 5999:
        with pytest.raises(ValueError): chain.reconcile(capsule, execute, tx, status)
    else:
        assert chain.reconcile(capsule, execute, tx, status)["wallet_network_fee_lamports"] == cap


def test_capsule_historical_rfq_expiry_does_not_block_already_executed_receipt(signer):
    cap, ex, tx, statuses = prepared(signer, sponsor=True)
    cap["order"]["expireAt"] = "2020-01-01T00:00:00+00:00"
    cap["sha256"] = chain.digest({k: v for k, v in cap.items() if k != "sha256"})
    assert chain.reconcile(cap, ex, tx, statuses)["actual_output_units"] == 1985000


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_actual_consumers_durable_original_chain_and_quantities(managed, http, monkeypatch, tmp_path, side):
    owner = str(managed.PUBLIC_KEY)
    raw = order_payload(owner, amount=AMOUNT if side == "buy" else 400)
    if side == "sell": raw.update(inputMint=TOKEN, outputMint=chain.SOL, feeMint=TOKEN)
    execute = execution_payload(raw, managed)
    http.replies.extend([Response(raw), Response(execute)])
    if side == "buy":
        buyer_guards(monkeypatch)
        store = BuyRecoveryStore(tmp_path / "buy")
        with store.scope():
            attempt = store.begin(Position(address=TOKEN, token_mint=TOKEN, dry_run=False, buy_amount_sol=.1), paper=False, amount_sol=.1)
            with attempt.execution_scope(): result = await buyer.buy(TOKEN, .1)
            attempt.receive(result)
        assert result["buy_price_usd"] == pytest.approx(10 * 10**6 / 1985000)
        restarted = BuyRecoveryStore(store.directory)._records[attempt.intent_id]
    else:
        monkeypatch.setattr(seller, "jupiter", router)
        monkeypatch.setattr(seller, "_JUP_ROUTER_AVAILABLE", True)
        monkeypatch.setattr(seller, "JUP_SELL_SLIPPAGE_BPS", 100)
        monkeypatch.setattr(seller, "get_sol_usd", AsyncMock(return_value=100.))
        store = SellRecoveryStore(tmp_path / "sell")
        pos = Position(id=1, address=TOKEN, token_mint=TOKEN, dry_run=False, qty=1000, entry_qty=1000,
            buy_price_usd=1., entry_notional_usd=10., buy_amount_sol=.1, closed=False)
        attempt = store.begin(pos, 400, paper=False, reason="synthetic")
        with attempt.execution_scope(): result = await seller.sell(TOKEN, 400)
        checked = attempt.receive(result)
        store.finish(attempt)
        assert (checked["qty_sold"], checked["qty_left"], checked["partial"]) == (400, 600, True)
        assert result["price_used_usd"] == pytest.approx(1985000 / 10**9 * 100 * 10**6 / 400)
        restarted = SellRecoveryStore(store.directory).records[attempt.intent_id]
    assert attempt.row["state"] == "fill_received"
    assert restarted["execution"]["capsule"] == attempt.row["execution"]["capsule"]
    assert restarted["fill"]["execution_receipt"]["chain_wallet_verified"] is True
    assert [call[0] for call in http.calls] == ["GET", "POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "wrong_balance", "transport"])
async def test_one_post_unavailable_chain_preserves_original_pending_without_fallback(managed, http, monkeypatch, tmp_path, failure):
    fallback = buyer_guards(monkeypatch)
    order = order_payload(str(managed.PUBLIC_KEY))
    execute = execution_payload(order, managed)
    http.replies.extend([Response(order), Response(execute)])
    async def read(cap, ex, *, endpoint):
        if failure == "transport": raise TimeoutError("synthetic node timeout")
        tx, status = evidence(cap, ex)
        if failure == "missing": tx = None
        else: tx["meta"]["postTokenBalances"][1]["uiTokenAmount"]["amount"] = "1"
        return chain.reconcile(cap, ex, tx, status)
    monkeypatch.setattr(solana_execution, "reconcile_original", read)
    store = BuyRecoveryStore(tmp_path)
    with store.scope():
        attempt = store.begin(Position(address=TOKEN, token_mint=TOKEN, dry_run=False, buy_amount_sol=.1), paper=False, amount_sol=.1)
        with attempt.execution_scope(), pytest.raises(BuyOutcomeUncertain): await buyer.buy(TOKEN, .1)
    restarted = BuyRecoveryStore(tmp_path)._records[attempt.intent_id]
    assert restarted["state"] == "prepared" and "fill" not in restarted
    assert restarted["execution"]["provider_response"]["signature"] == execute["signature"]
    assert "chain_receipt" not in restarted["execution"]
    fallback.assert_not_awaited()
    assert [x[0] for x in http.calls] == ["GET", "POST"]


@pytest.mark.asyncio
async def test_repeated_cancel_keeps_actual_chain_worker_and_durable_result(managed, http, monkeypatch, tmp_path):
    buyer_guards(monkeypatch)
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()
    async def read(cap, execute, *, endpoint):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return chain.reconcile(cap, execute, *evidence(cap, execute))
    monkeypatch.setattr(solana_execution, "reconcile_original", read)
    raw = order_payload(str(managed.PUBLIC_KEY))
    http.replies.extend([Response(raw), Response(execution_payload(raw, managed))])
    store = BuyRecoveryStore(tmp_path)
    async def buy():
        with store.scope():
            attempt = store.begin(Position(address=TOKEN, token_mint=TOKEN, dry_run=False, buy_amount_sol=.1), paper=False, amount_sol=.1)
            with attempt.execution_scope(): return await buyer.buy(TOKEN, .1)
    task = asyncio.create_task(buy())
    try:
        await asyncio.wait_for(started.wait(), 2)
        intent = next(iter(store._active))
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and store._active == {intent} and owned_dispatch.pending_dispatch_count() == 1
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    restarted = BuyRecoveryStore(tmp_path)._records[intent]
    assert restarted["state"] == "prepared" and "fill" not in restarted
    assert restarted["execution"]["chain_receipt"]["chain_wallet_verified"] is True
    assert store.pending_addresses == {TOKEN} and owned_dispatch.pending_dispatch_count() == 0


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_boolean_verified_flags_without_owned_proof_never_admit_live_fill(tmp_path, side):
    response = {"signature": "synthetic", "qty_lamports": 1000, "buy_price_usd": 1.,
        "entry_notional_usd": 10., "ok": True, "price_used_usd": 1., "qty_sold": 400,
        "qty_left": 600, "partial": True, "fill_verified": True,
        "execution_receipt": {"chain_wallet_verified": True}}
    if side == "buy":
        store = BuyRecoveryStore(tmp_path)
        with store.scope():
            attempt = store.begin(Position(address=TOKEN, dry_run=False, buy_amount_sol=.1), paper=False, amount_sol=.1)
            with pytest.raises(BuyOutcomeUncertain): attempt.receive(response)
    else:
        store = SellRecoveryStore(tmp_path)
        p = Position(id=1, address=TOKEN, dry_run=False, qty=1000, entry_qty=1000, closed=False,
            buy_price_usd=1., entry_notional_usd=10.)
        attempt = store.begin(p, 400, paper=False, reason="synthetic")
        with pytest.raises(SellOutcomeUncertain): attempt.receive(response)
    assert attempt.row["state"] == "prepared"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "http", "wrong_id", "wrong_version", "error", "missing_result", "oversized", "json", "null"])
async def test_actual_read_only_rpc_uses_uncached_original_signature_and_bounded_envelope(monkeypatch, fault):
    calls, reads = [], []
    monkeypatch.setattr(solana_execution, "_rpc_urls", lambda: ["https://synthetic.invalid/?api-key=synthetic-node-key"])
    class Stream:
        def __init__(self, content): self.remaining = content
        async def read(self, bound):
            # Exercise fragmented bodies, not just a conveniently whole JSON.
            chunk, self.remaining = self.remaining[:min(13, bound)], self.remaining[min(13, bound):]
            return chunk
    class Reply:
        def __init__(self, request, count):
            self.status = 429 if fault == "http" else 200
            data = {"jsonrpc": "2.0", "id": request["id"], "result": {"synthetic": count}}
            if fault == "wrong_id": data["id"] = "unrelated"
            if fault == "wrong_version": data["jsonrpc"] = "1.0"
            if fault == "error": data["error"] = {"code": -32000, "message": "synthetic"}
            if fault == "missing_result": del data["result"]
            if fault == "null": data["result"] = None
            raw = json.dumps(data).encode()
            if fault == "oversized": raw = b"x" * (2 * 1024 * 1024 + 1)
            if fault == "json": raw = b"not JSON"
            self.content = Stream(raw)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, **kwargs):
            assert kwargs["timeout"].total == 8 and "headers" not in kwargs
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            assert kwargs["allow_redirects"] is False
            return Reply(kwargs["json"], len(calls))
    monkeypatch.setattr(solana_execution.aiohttp, "ClientSession", Session)
    if fault not in (None, "null"):
        with pytest.raises((ValueError, RuntimeError)): await solana_execution.fetch_transaction_evidence("original-signature")
        assert len(calls) == 1
    else:
        tx, status = await solana_execution.fetch_transaction_evidence("original-signature")
        assert (tx, status) == ((None, None) if fault == "null" else ({"synthetic": 1}, {"synthetic": 2}))
        assert len(calls) == 2
        requests = [x[1]["json"] for x in calls]
        assert requests[0]["method"] == "getTransaction"
        assert requests[0]["params"] == ["original-signature", {"encoding": "base64", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}]
        assert requests[1]["method"] == "getSignatureStatuses"
        assert requests[1]["params"] == [["original-signature"], {"searchTransactionHistory": True}]
        assert requests[0]["id"] != requests[1]["id"]


@pytest.mark.parametrize("url", ["http://foreign.invalid", "https://u:p@rpc.invalid", "file:///tmp/rpc", "https://rpc.invalid/#bad"])
def test_bad_read_only_endpoint_is_rejected_without_http(monkeypatch, url):
    monkeypatch.setattr(solana_execution, "_rpc_urls", lambda: [url])
    with pytest.raises(ValueError): solana_execution.configured_endpoint()


@pytest.mark.asyncio
async def test_actual_primary_sell_guard_preserves_chain_proof_in_sql_outbox(managed, http, monkeypatch, tmp_path):
    from test_sell_recovery import namespace, position
    store = SellRecoveryStore(tmp_path / "sell")
    pos = position(paper_mode=False)
    pos.address = pos.token_mint = TOKEN
    ns = namespace(tmp_path, store, seller=seller, paper_mode=False)
    monkeypatch.setattr(seller, "jupiter", router)
    monkeypatch.setattr(seller, "_JUP_ROUTER_AVAILABLE", True)
    monkeypatch.setattr(seller, "JUP_SELL_SLIPPAGE_BPS", 100)
    monkeypatch.setattr(seller, "get_sol_usd", AsyncMock(return_value=100.))
    raw = order_payload(str(managed.PUBLIC_KEY), amount=400)
    raw.update(inputMint=TOKEN, outputMint=chain.SOL, feeMint=TOKEN)
    http.replies.extend([Response(raw), Response(execution_payload(raw, managed))])
    result = await ns["_sell_position_guarded"](pos, 400, reason="partial_fill")
    row = store.records[result["_sell_intent_id"]]
    assert row["state"] == "sql_prepared" and row["sql_record"]["trade_event"]["qty"] == 400
    assert row["sql_record"]["execution_provenance"]["chain_receipt"]["chain_wallet_verified"] is True
    assert row["sql_record"]["position_snapshot"]["qty"] == 600
    from runtime.close_recovery import _validate_record
    _validate_record(row["sql_record"])
    corrupted = copy.deepcopy(row["sql_record"])
    corrupted["execution_provenance"]["chain_receipt"]["actual_input_units"] = 401
    with pytest.raises(ValueError): _validate_record(corrupted)


@pytest.mark.asyncio
async def test_original_endpoint_is_pinned_before_post_and_read_route_change_is_rejected(managed, http, monkeypatch):
    import utils.solana_execution as actual_rpc
    raw = order_payload(str(managed.PUBLIC_KEY))
    execute = execution_payload(raw, managed)
    http.replies.extend([Response(raw), Response(execute)])
    observed = []
    async def read(cap, ex, *, endpoint):
        observed.append(endpoint)
        assert cap["rpc_source_sha256"] == actual_rpc.endpoint_fingerprint(endpoint)
        return chain.reconcile(cap, ex, *evidence(cap, ex))
    monkeypatch.setattr(solana_execution, "reconcile_original", read)
    original_sign = managed.sign_base64_transaction
    def sign(encoded):
        monkeypatch.setattr(solana_execution, "configured_endpoint", lambda: "https://changed.invalid")
        return original_sign(encoded)
    monkeypatch.setattr(managed, "sign_base64_transaction", sign)
    await router.execute_managed_swap(input_mint=chain.SOL, output_mint=TOKEN, amount_lamports=AMOUNT, slippage_bps=100)
    assert observed == ["https://synthetic.invalid"]


def test_owned_capsule_conflicts_and_journal_corruption_do_not_become_live_fills(signer, tmp_path):
    cap, ex, tx, statuses = prepared(signer)
    receipt = chain.reconcile(cap, ex, tx, statuses)
    store = BuyRecoveryStore(tmp_path)
    with store.scope():
        attempt = store.begin(Position(address=TOKEN, token_mint=TOKEN, dry_run=False, buy_amount_sol=.1), paper=False, amount_sol=.1)
        with attempt.execution_scope():
            execution_provenance.record("prepared_submission", cap)
            with pytest.raises(ValueError): execution_provenance.record("prepared_submission", cap)
            execution_provenance.record("dispatch_started", {"capsule_sha256": cap["sha256"]})
            execution_provenance.record("provider_response", ex)
            execution_provenance.record("chain_receipt", receipt)
        response = {"qty_lamports": receipt["actual_output_units"], "signature": ex["signature"],
            "buy_price_usd": 1., "entry_notional_usd": 10., "execution_receipt": receipt}
        attempt.receive(response)
        with pytest.raises(BuyOutcomeUncertain): attempt.receive({**response, "qty_lamports": 1})
        with pytest.raises(BuyOutcomeUncertain): attempt.receive({"qty_lamports": 0, "signature": "NO_ROUTE"})
    # Corruption is isolated to the synthetic temporary journal.
    from utils.atomic_json import write_json_atomic
    mutated = copy.deepcopy(attempt.row)
    mutated["fill"]["qty_lamports"] = 1
    write_json_atomic(tmp_path / (attempt.intent_id + ".json"), mutated)
    from runtime.buy_recovery import BuyRecoveryError
    with pytest.raises(BuyRecoveryError): BuyRecoveryStore(tmp_path)


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_later_preparation_rejection_never_erases_an_earlier_owned_dispatch(signer, tmp_path, side):
    cap, ex, tx, statuses = prepared(signer, side=side)
    if side == "buy":
        store = BuyRecoveryStore(tmp_path)
        scope = store.scope()
        scope.__enter__()
        attempt = store.begin(Position(address=TOKEN, token_mint=TOKEN, dry_run=False, buy_amount_sol=.1), paper=False, amount_sol=.1)
    else:
        store = SellRecoveryStore(tmp_path)
        p = Position(id=1, address=TOKEN, token_mint=TOKEN, dry_run=False, qty=1000, entry_qty=1000,
            closed=False, buy_price_usd=1., entry_notional_usd=10.)
        attempt = store.begin(p, 400, paper=False, reason="synthetic")
    try:
        with attempt.execution_scope():
            execution_provenance.record("prepared_submission", cap)
            execution_provenance.record("dispatch_started", {"capsule_sha256": cap["sha256"]})
        if side == "buy":
            with pytest.raises(BuyOutcomeUncertain): attempt.receive({"qty_lamports": 0, "signature": "NO_JUP_ORDER"})
        else:
            with pytest.raises(SellOutcomeUncertain): attempt.receive({"ok": False, "error": "NO_QTY"})
        assert attempt.row["state"] == "prepared" and "dispatch_started" in attempt.row["execution"]
    finally:
        if side == "buy": scope.__exit__(None, None, None)
        else: store.finish(attempt)
