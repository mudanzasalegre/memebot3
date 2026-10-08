"""Actual Swap v2 adapters/SDK/consumers, synthetic keys and HTTP only."""
from __future__ import annotations

import asyncio
import base64
import copy
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from execution import jupiter_managed_contract as contract
from fetcher import jupiter_router as router
from runtime import owned_dispatch as owned
from runtime.buy_recovery import BuyRecoveryStore, BuyOutcomeUncertain
from runtime.sell_recovery import SellOutcomeUncertain
from db.models import Position
from trader import buyer, seller
from test_live_signing_boundaries import signer, unsigned
from chain_fixtures import INPUT_ACCOUNT, OUTPUT_ACCOUNT, POOL, evidence as rpc_evidence, simulation

SOL = router.SOL_MINT
TOKEN = str(Pubkey.new_unique())
AMOUNT = 100_000_000


def order_payload(owner, *, token=TOKEN, amount=AMOUNT, router_name="metis", sponsor=None):
    payer = owner
    if sponsor is not None: payer = str(sponsor.pubkey())
    ix = Instruction(Pubkey.new_unique(), b"synthetic", [AccountMeta(Pubkey.from_string(owner), True, True),
        AccountMeta(INPUT_ACCOUNT, False, True), AccountMeta(OUTPUT_ACCOUNT, False, True), AccountMeta(POOL, False, True)])
    message = MessageV0.try_compile(Pubkey.from_string(payer), [ix], [], Hash.new_unique())
    raw = bytes(VersionedTransaction.populate(message, [Signature.default()] * message.header.num_required_signatures))
    return {"inputMint": SOL, "outputMint": token, "inAmount": str(amount), "outAmount": "2000000",
        "otherAmountThreshold": "1980000", "swapMode": "ExactIn", "slippageBps": 100,
        "taker": owner, "mode": "manual", "router": router_name, "transactionVersion": 0,
        "transaction": base64.b64encode(raw).decode(), "requestId": "synthetic-request",
        "lastValidBlockHeight": "123", "priceImpact": .1, "priceImpactPct": ".001",
        "signatureFeeLamports": 5000, "signatureFeePayer": payer,
        "prioritizationFeeLamports": 1000, "prioritizationFeePayer": payer,
        "rentFeeLamports": 0, "rentFeePayer": owner,
        "feeMint": SOL, "feeBps": 5, "gasless": sponsor is not None,
        "routePlan": [], **({"expireAt": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
            if router_name == "jupiterz" else {})}


def execution_payload(order, signer, *, sponsor=None):
    signed = VersionedTransaction.from_bytes(base64.b64decode(signer.sign_base64_transaction(order["transaction"])))
    signature = (sponsor.sign_message(to_bytes_versioned(signed.message)) if sponsor is not None
        else signed.signatures[0])
    return {"status": "Success", "code": 0, "signature": str(signature), "slot": "456",
        "totalInputAmount": order["inAmount"], "inputAmountResult": str(int(order["inAmount"]) - min(50000, int(order["inAmount"]) // 2000)),
        "outputAmountResult": "1985000", "totalOutputAmount": "1985000"}


@pytest.fixture
def managed(signer, monkeypatch):
    import trader
    monkeypatch.setitem(trader.__dict__, "sol_signer", signer)
    monkeypatch.setenv("SOL_PUBLIC_KEY", str(signer.PUBLIC_KEY))
    monkeypatch.setattr(router, "JUP_API_KEY", "synthetic-key")
    monkeypatch.setattr(router, "JUP_MANAGED_ENABLED", True)
    monkeypatch.setattr(router, "JUP_LEGACY_SWAP_ENABLED", True)
    monkeypatch.setattr(router, "JUP_ORDER_URL", "https://api.jup.ag/ultra/v1/order")
    monkeypatch.setattr(router, "JUP_EXECUTE_URL", "https://api.jup.ag/ultra/v1/execute")
    monkeypatch.setattr(router.solana_execution, "configured_endpoint", lambda: "https://synthetic.invalid")
    async def project(order, *, endpoint, max_wallet_fee_lamports=None):
        from execution import unsigned_projection
        return unsigned_projection.check(order, simulation(order),
            rpc_source_sha256=router.solana_execution.endpoint_fingerprint(endpoint),
            observed_at=datetime.now(timezone.utc).isoformat(), current_node_slot=449,
            max_wallet_fee_lamports=max_wallet_fee_lamports), time.monotonic()
    monkeypatch.setattr(router.solana_execution, "project_unsigned", project)
    async def read(capsule, execution, *, endpoint):
        tx, status = rpc_evidence(capsule, execution)
        return router.chain_reconciliation.reconcile(capsule, execution, tx, status)
    monkeypatch.setattr(router.solana_execution, "reconcile_original", read)
    return signer


class Response:
    def __init__(self, body=None, *, status=200, enter=None):
        self.body, self.status, self.enter = body, status, enter
    async def __aenter__(self):
        if self.enter is not None: self.enter()
        return self
    async def __aexit__(self, *args): pass
    async def json(self, **kwargs):
        if isinstance(self.body, BaseException): raise self.body
        return copy.deepcopy(self.body)


@pytest.fixture
def http(monkeypatch):
    from jupiter_access_fixtures import isolate_budget
    isolate_budget(monkeypatch)
    calls, replies = [], []
    class Session:
        def __init__(self, *args, **kwargs): self.headers = kwargs.get("headers")
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def request(self, method, url, **kwargs):
            calls.append((method, url, copy.deepcopy(kwargs), copy.deepcopy(self.headers)))
            assert replies, "Unexpected extra managed request"
            response = replies.pop(0)
            if isinstance(response, BaseException): raise response
            return response
        def get(self, url, **kwargs): return self.request("GET", url, **kwargs)
        def post(self, url, **kwargs): return self.request("POST", url, **kwargs)
    monkeypatch.setattr(router.aiohttp, "ClientSession", Session)
    return SimpleNamespace(calls=calls, replies=replies)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["metis", "jupiterz", "dflow", "okx"])
async def test_current_v2_preserves_request_signed_message_and_actual_provider_units(managed, http, monkeypatch, route):
    order = order_payload(str(managed.PUBLIC_KEY), router_name=route)
    response = execution_payload(order, managed)
    http.replies.extend([Response(order), Response(response)])
    sign = Mock(wraps=managed.sign_base64_transaction)
    monkeypatch.setattr(managed, "sign_base64_transaction", sign)
    result = await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN,
        amount_lamports=AMOUNT, slippage_bps=100, max_price_impact_pct=8., max_wallet_fee_lamports=6000)
    assert result["qty_lamports"] == 1985000 != int(order["outAmount"])
    assert result["signature"] == response["signature"] and result["fill_verified"] is True
    assert [call[:2] for call in http.calls] == [("GET", "https://api.jup.ag/swap/v2/order"),
        ("POST", "https://api.jup.ag/swap/v2/execute")]
    params = http.calls[0][2]["params"]
    assert all(call[2]["allow_redirects"] is False for call in http.calls)
    assert params == {"inputMint": SOL, "outputMint": TOKEN, "amount": str(AMOUNT),
        "taker": str(managed.PUBLIC_KEY), "slippageBps": "100", "swapMode": "ExactIn",
        "maxSupportedTransactionVersion": "0"}
    posted = http.calls[1][2]["json"]
    assert posted["requestId"] == order["requestId"] and posted["lastValidBlockHeight"] == "123"
    actual = VersionedTransaction.from_bytes(base64.b64decode(posted["signedTransaction"]))
    assert actual.message == VersionedTransaction.from_bytes(base64.b64decode(order["transaction"])).message
    assert actual.verify_with_results() == [True] and sign.call_count == 1
    receipt = result["execution_receipt"]
    assert len(receipt["original_order_sha256"]) == len(receipt["original_request_sha256"]) == 64
    assert receipt["total_input_units"] == AMOUNT and receipt["input_fee_units"] == 50000
    assert receipt["wallet_fee_estimate_lamports"] == 6000 and receipt["first_signature_bound"] is True
    assert receipt["chain_wallet_verified"] is True and receipt["requires_reconciliation"] is False
    assert result["route"]["execution_receipt"] == receipt and owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("inputMint", TOKEN), ("outputMint", SOL), ("taker", TOKEN), ("inAmount", True),
    ("inAmount", "100000001"), ("inAmount", 100000000.), ("outAmount", "0"),
    ("otherAmountThreshold", "0"), ("otherAmountThreshold", "1979999"),
    ("otherAmountThreshold", "2000001"), ("swapMode", "ExactOut"), ("slippageBps", True),
    ("slippageBps", 101), ("priceImpact", None), ("priceImpact", float("nan")),
    ("priceImpact", float("inf")), ("priceImpact", True), ("priceImpact", 9.),
    ("router", "unknown"), ("mode", "unknown"), ("transactionVersion", True),
    ("transactionVersion", 1), ("transaction", "bad base64"), ("transaction", None),
    ("requestId", ""), ("requestId", None), ("requestId", " trimmed "),
    ("signatureFeeLamports", True), ("signatureFeeLamports", -1),
    ("signatureFeeLamports", 2**64), ("signatureFeePayer", None), ("signatureFeePayer", TOKEN),
    ("prioritizationFeeLamports", 1001), ("rentFeeLamports", None), ("gasless", None), ("gasless", True),
    ("feeBps", None), ("feeBps", float("nan")), ("feeBps", 10001), ("feeMint", TOKEN + "bad"),
    ("lastValidBlockHeight", None), ("lastValidBlockHeight", 0), ("lastValidBlockHeight", True),
    ("receiver", TOKEN), ("referralAccount", TOKEN), ("errorCode", 1), ("error", "synthetic")])
async def test_bad_order_never_reaches_signing_or_post(managed, http, monkeypatch, field, value):
    order = order_payload(str(managed.PUBLIC_KEY))
    order[field] = value
    http.replies.append(Response(order))
    sign = Mock(wraps=managed.sign_base64_transaction)
    monkeypatch.setattr(managed, "sign_base64_transaction", sign)
    with pytest.raises(router.SwapPreparationError):
        await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=AMOUNT,
            slippage_bps=100, max_price_impact_pct=8., max_wallet_fee_lamports=6000)
    assert [call[0] for call in http.calls] == ["GET"]
    sign.assert_not_called()
    assert owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"amount_lamports": True}, {"amount_lamports": 0},
    {"amount_lamports": 1.5}, {"amount_lamports": 2**64}, {"slippage_bps": True},
    {"slippage_bps": -1}, {"slippage_bps": 10001}, {"input_mint": "invalid"},
    {"output_mint": SOL}, {"user_public_key": TOKEN}, {"user_public_key": True},
    {"max_price_impact_pct": True}, {"max_price_impact_pct": float("nan")},
    {"max_price_impact_pct": -1}, {"max_wallet_fee_lamports": True},
    {"max_wallet_fee_lamports": -1}, {"max_wallet_fee_lamports": 2**64}])
async def test_invalid_original_request_never_queries_or_signs(managed, http, monkeypatch, options):
    sign = Mock(wraps=managed.sign_base64_transaction)
    monkeypatch.setattr(managed, "sign_base64_transaction", sign)
    kwargs = {"input_mint": SOL, "output_mint": TOKEN, "amount_lamports": AMOUNT, "slippage_bps": 100}
    kwargs.update(options)
    with pytest.raises(router.SwapPreparationError): await router.execute_managed_swap(**kwargs)
    assert not http.calls
    sign.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 400, 401, 403, 429, 500, 503])
async def test_unsigned_order_http_failure_does_not_sign_or_repeat(managed, http, monkeypatch, status):
    http.replies.append(Response(status=status))
    sign = Mock(wraps=managed.sign_base64_transaction)
    monkeypatch.setattr(managed, "sign_base64_transaction", sign)
    with pytest.raises(router.SwapPreparationError):
        await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=AMOUNT)
    assert len(http.calls) == 1 and http.calls[0][0] == "GET"
    sign.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("status", None), ("status", "success"), ("status", "Failed"),
    ("code", None), ("code", True), ("code", -1), ("code", "0"), ("error", "synthetic"),
    ("signature", "invalid"), ("signature", str(Signature.default())),
    ("signature", str(Keypair.from_seed(bytes([4]) * 32).sign_message(b"different synthetic packet"))),
    ("slot", None), ("slot", True), ("slot", 1.5), ("slot", "0"),
    ("totalInputAmount", "100000001"), ("totalInputAmount", True),
    ("inputAmountResult", "100000001"), ("inputAmountResult", "0"),
    ("totalOutputAmount", "1979999"), ("totalOutputAmount", "1985001"),
    ("totalOutputAmount", True), ("outputAmountResult", "1985001"),
    ("outputAmountResult", 1985000.), ("requestId", "foreign-request")])
async def test_bad_execute_receipt_never_rebuilds_resigns_or_posts_again(managed, http, monkeypatch, field, value):
    order = order_payload(str(managed.PUBLIC_KEY))
    response = execution_payload(order, managed)
    response[field] = value
    http.replies.extend([Response(order), Response(response)])
    sign = Mock(wraps=managed.sign_base64_transaction)
    monkeypatch.setattr(managed, "sign_base64_transaction", sign)
    with pytest.raises(router.SwapSubmissionUncertain):
        await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=AMOUNT,
            slippage_bps=100)
    assert [call[0] for call in http.calls] == ["GET", "POST"] and sign.call_count == 1
    assert owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,value", [("JUP_ORDER_URL", "https://foreign.invalid/order"),
    ("JUP_EXECUTE_URL", "http://api.jup.ag/swap/v2/execute"),
    ("JUP_EXECUTE_URL", "https://api.jup.ag/swap/v2/execute?redirect=1"),
    ("JUP_EXECUTE_URL", "https://user:secret@api.jup.ag/swap/v2/execute"),
    ("JUP_ORDER_URL", "https://api.jup.ag/unknown/order")])
async def test_no_api_credentials_or_order_reach_unapproved_endpoints(managed, http, monkeypatch, endpoint, value):
    monkeypatch.setattr(router, endpoint, value)
    with pytest.raises(router.SwapPreparationError):
        await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=AMOUNT)
    assert not http.calls


@pytest.mark.asyncio
async def test_explicitly_disabled_managed_mode_is_pre_http(managed, http, monkeypatch):
    monkeypatch.setattr(router, "JUP_MANAGED_ENABLED", False)
    with pytest.raises(router.SwapPreparationError):
        await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=AMOUNT)
    assert not http.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, [], ValueError("synthetic malformed JSON"),
    TimeoutError("synthetic POST acknowledgement loss"), router.SwapPreparationError("synthetic after POST")])
async def test_execute_failures_cannot_spoof_a_preparation_failure(managed, http, body):
    order = order_payload(str(managed.PUBLIC_KEY))
    http.replies.extend([Response(order), Response(body)])
    with pytest.raises(router.SwapSubmissionUncertain):
        await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=AMOUNT,
            slippage_bps=100)
    assert [call[0] for call in http.calls] == ["GET", "POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 400, 429, 500, 503])
async def test_post_http_failure_never_retries_or_gets_another_order(managed, http, status):
    order = order_payload(str(managed.PUBLIC_KEY))
    http.replies.extend([Response(order), Response(status=status)])
    with pytest.raises(router.SwapSubmissionUncertain):
        await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=AMOUNT,
            slippage_bps=100)
    assert [call[0] for call in http.calls] == ["GET", "POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["message", "wallet_signature", "base64"])
async def test_changed_or_unverified_local_signing_cannot_reach_execute(managed, http, monkeypatch, corruption):
    order = order_payload(str(managed.PUBLIC_KEY))
    http.replies.append(Response(order))
    if corruption == "message":
        encoded = managed.sign_base64_transaction(base64.b64encode(unsigned(managed.PUBLIC_KEY, "v0")).decode())
    elif corruption == "wallet_signature":
        encoded = order["transaction"]
    else:
        encoded = "invalid base64"
    sign = Mock(return_value=encoded)
    monkeypatch.setattr(managed, "sign_base64_transaction", sign)
    with pytest.raises(router.SwapPreparationError):
        await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=AMOUNT,
            slippage_bps=100)
    sign.assert_called_once()
    assert [call[0] for call in http.calls] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("presigned", [False, True])
async def test_sponsored_rfq_preserves_wallet_index_and_verifies_provider_added_payer_signature(managed, http, presigned):
    sponsor = Keypair.from_seed(bytes([7]) * 32)
    order = order_payload(str(managed.PUBLIC_KEY), router_name="jupiterz", sponsor=sponsor)
    if presigned:
        tx = VersionedTransaction.from_bytes(base64.b64decode(order["transaction"]))
        order["transaction"] = base64.b64encode(bytes(VersionedTransaction.populate(tx.message,
            [sponsor.sign_message(to_bytes_versioned(tx.message)), Signature.default()]))).decode()
    response = execution_payload(order, managed, sponsor=sponsor)
    http.replies.extend([Response(order), Response(response)])
    result = await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=AMOUNT,
        slippage_bps=100, max_wallet_fee_lamports=0)
    receipt = result["execution_receipt"]
    assert receipt["wallet_signer_index"] == 1 and receipt["first_signature_bound"] is True
    assert receipt["provider_first_signature_verified"] is True
    assert receipt["first_signature_present_before_execute"] is presigned
    assert (receipt["expected_first_signature"] is not None) is presigned
    assert receipt["chain_wallet_verified"] is True and receipt["requires_reconciliation"] is False
    posted = VersionedTransaction.from_bytes(base64.b64decode(http.calls[1][2]["json"]["signedTransaction"]))
    assert posted.verify_with_results() == [presigned, True]
    assert receipt["wallet_fee_estimate_lamports"] == 0


@pytest.mark.asyncio
async def test_expiry_is_rechecked_after_local_signing_without_execute(managed, http, monkeypatch):
    now = datetime.now(timezone.utc)
    order = order_payload(str(managed.PUBLIC_KEY), router_name="jupiterz")
    order["expireAt"] = (now + timedelta(seconds=2)).isoformat()
    current = [now]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return current[0]
    monkeypatch.setattr(contract, "datetime", Clock)
    original = managed.sign_base64_transaction
    def sign(encoded):
        result = original(encoded)
        current[0] = now + timedelta(seconds=3)
        return result
    monkeypatch.setattr(managed, "sign_base64_transaction", sign)
    http.replies.append(Response(order))
    with pytest.raises(router.SwapPreparationError):
        await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=AMOUNT,
            slippage_bps=100)
    assert [call[0] for call in http.calls] == ["GET"]


@pytest.mark.asyncio
async def test_uint64_input_output_never_pass_through_float(managed, http):
    amount, output = 2**53 + 1, 2**53 + 123
    order = order_payload(str(managed.PUBLIC_KEY), amount=amount)
    order.update(outAmount=str(output), otherAmountThreshold=str(output * 99 // 100))
    response = execution_payload(order, managed)
    response.update(totalOutputAmount=str(output - 1), outputAmountResult=str(output - 1))
    http.replies.extend([Response(order), Response(response)])
    result = await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=amount,
        slippage_bps=100)
    assert result["qty_lamports"] == output - 1
    assert result["execution_receipt"]["total_input_units"] == amount
    assert http.calls[0][2]["params"]["amount"] == str(amount)


def buyer_guards(monkeypatch):
    monkeypatch.setattr(buyer, "jupiter", router)
    monkeypatch.setattr(buyer, "_JUP_ROUTER_AVAILABLE", True)
    monkeypatch.setattr(buyer, "_JUP_BUY_SLIPPAGE_BPS", 100)
    monkeypatch.setattr(buyer, "_IMPACT_MAX_PCT_DEFAULT", 8.)
    monkeypatch.setattr(buyer, "_GAS_RESERVE_LAMPORTS", 6000)
    monkeypatch.setattr(buyer, "_REQUIRE_JUP_PRICE", False)
    monkeypatch.setattr(buyer, "is_in_trading_window", lambda: True)
    monkeypatch.setattr(buyer, "_max_positions_reached", AsyncMock(return_value=False))
    monkeypatch.setattr(buyer, "_has_enough_funds", AsyncMock(return_value=True))
    monkeypatch.setattr(buyer, "_resolve_buy_price_usd", AsyncMock(return_value=(1., "synthetic")))
    monkeypatch.setattr(buyer, "_resolve_entry_notional_usd", AsyncMock(return_value=10.))
    fallback = AsyncMock()
    monkeypatch.setattr(buyer.gmgn, "buy", fallback)
    return fallback


@pytest.mark.asyncio
async def test_actual_buyer_consumes_execute_quantity_not_quote_or_another_metis_gate(managed, http, monkeypatch):
    fallback = buyer_guards(monkeypatch)
    precheck = AsyncMock(side_effect=AssertionError("A different Metis route cannot veto this managed winner"))
    monkeypatch.setattr(buyer, "_jupiter_precheck_quote", precheck)
    order = order_payload(str(managed.PUBLIC_KEY), router_name="jupiterz")
    response = execution_payload(order, managed)
    http.replies.extend([Response(order), Response(response)])
    result = await buyer.buy("DisplayAlias", .1, token_mint=TOKEN)
    assert result["qty_lamports"] == 1985000 != int(order["outAmount"])
    assert result["route"]["execution_receipt"]["requested_input_units"] == AMOUNT
    assert result["fill_verified"] is True and result["venue"] == "jupiter_managed"
    assert [call[0] for call in http.calls] == ["GET", "POST"]
    precheck.assert_not_awaited()
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_prepared_no_order_is_durable_known_no_fill(managed, http, monkeypatch, tmp_path):
    fallback = buyer_guards(monkeypatch)
    http.replies.append(Response(status=403))
    response = await buyer.buy(TOKEN, .1)
    assert response["signature"] == "NO_JUP_ORDER" and response["qty_lamports"] == 0
    store = BuyRecoveryStore(tmp_path)
    with store.scope():
        attempt = store.begin(Position(address=TOKEN, dry_run=False, buy_amount_sol=.1,
            run_id="synthetic-managed"), amount_sol=.1, paper=False)
        attempt.receive(response)
    assert attempt.row["state"] == "no_fill" and not store.pending_addresses
    fallback.assert_not_awaited()
    assert [call[0] for call in http.calls] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, TimeoutError("synthetic POST acknowledgement loss")])
async def test_repeated_post_cancel_retains_actual_buy_journal_owner_until_http_worker_settles(managed, http,
        monkeypatch, tmp_path, failure):
    buyer_guards(monkeypatch)
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()
    def enter():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "Synthetic managed POST was not released"
    order = order_payload(str(managed.PUBLIC_KEY))
    response = execution_payload(order, managed) if failure is None else failure
    http.replies.extend([Response(order), Response(response, enter=enter)])
    store = BuyRecoveryStore(tmp_path)
    async def execute():
        with store.scope():
            store.begin(Position(address=TOKEN, dry_run=False, buy_amount_sol=.1,
                run_id="synthetic-managed"), amount_sol=.1, paper=False)
            return await buyer.buy(TOKEN, .1)
    task = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(started.wait(), 2)
        original = next(iter(store._active))
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and store._active == {original} and owned.pending_dispatch_count() == 1
        assert BuyRecoveryStore(tmp_path).pending_addresses == {TOKEN}
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert not store._active and store.pending_addresses == {TOKEN}
    assert store._records[original]["state"] == "prepared" and "fill" not in store._records[original]
    assert [call[0] for call in http.calls] == ["GET", "POST"] and owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
async def test_disabled_managed_buyer_with_key_keeps_optional_gmgn_policy_separate(managed, http, monkeypatch):
    fallback = buyer_guards(monkeypatch)
    monkeypatch.setattr(router, "JUP_MANAGED_ENABLED", False)
    fallback.return_value = {"signature": "synthetic", "route": {"quote": {"outAmount": "1000"}}}
    assert (await buyer.buy(TOKEN, .1))["qty_lamports"] == 1000
    fallback.assert_awaited_once_with(TOKEN, .1)
    assert not http.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["preparation", "post"])
async def test_actual_managed_seller_only_falls_back_on_proven_pre_post_failure(managed, http, monkeypatch, failure):
    monkeypatch.setattr(seller, "jupiter", router)
    monkeypatch.setattr(seller, "_JUP_ROUTER_AVAILABLE", True)
    monkeypatch.setattr(seller, "JUP_SELL_SLIPPAGE_BPS", 100)
    fallback = AsyncMock(return_value={"signature": "synthetic-fallback", "route": {}})
    monkeypatch.setattr(seller.gmgn, "sell", fallback)
    if failure == "preparation":
        http.replies.append(Response(status=403))
        ok, result = await seller._sell_execute_prefer_jupiter("DisplayAlias", 400,
            token_mint=TOKEN, liquidity_usd=1_000_000_000.)
        assert ok and result["venue"] == "gmgn"
        fallback.assert_awaited_once_with(TOKEN, 400)
        assert [call[0] for call in http.calls] == ["GET"]
    else:
        order = order_payload(str(managed.PUBLIC_KEY), amount=400)
        order.update(inputMint=TOKEN, outputMint=SOL)
        http.replies.extend([Response(order), Response(TimeoutError("synthetic after sell POST"))])
        with pytest.raises(SellOutcomeUncertain):
            await seller._sell_execute_prefer_jupiter("DisplayAlias", 400,
                token_mint=TOKEN, liquidity_usd=1_000_000_000.)
        fallback.assert_not_awaited()
        assert [call[0] for call in http.calls] == ["GET", "POST"]


@pytest.mark.asyncio
async def test_signed_negative_v2_impact_is_not_treated_as_unknown_or_fraction(managed, http):
    order = order_payload(str(managed.PUBLIC_KEY))
    order["priceImpact"] = -.1
    http.replies.extend([Response(order), Response(execution_payload(order, managed))])
    response = await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN,
        amount_lamports=AMOUNT, slippage_bps=100, max_price_impact_pct=0.)
    assert response["route"]["priceImpactPct"] == pytest.approx(-.001)


@pytest.mark.asyncio
async def test_both_disabled_jupiter_sell_modes_with_key_do_not_query_before_canonical_fallback(managed, http, monkeypatch):
    monkeypatch.setattr(router, "JUP_MANAGED_ENABLED", False)
    monkeypatch.setattr(router, "JUP_LEGACY_SWAP_ENABLED", False)
    monkeypatch.setattr(seller, "jupiter", router)
    monkeypatch.setattr(seller, "_JUP_ROUTER_AVAILABLE", True)
    fallback = AsyncMock(return_value={"signature": "synthetic", "route": {}})
    monkeypatch.setattr(seller.gmgn, "sell", fallback)
    ok, result = await seller._sell_execute_prefer_jupiter("DisplayAlias", 400,
        token_mint=TOKEN, liquidity_usd=1_000_000_000.)
    assert ok and result["venue"] == "gmgn" and not http.calls
    fallback.assert_awaited_once_with(TOKEN, 400)


@pytest.mark.asyncio
@pytest.mark.parametrize("height", [0, "0"])
async def test_rfq_zero_height_is_not_sent_as_an_expired_aggregator_nonce(managed, http, height):
    order = order_payload(str(managed.PUBLIC_KEY), router_name="jupiterz")
    order["lastValidBlockHeight"] = height
    http.replies.extend([Response(order), Response(execution_payload(order, managed))])
    result = await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN,
        amount_lamports=AMOUNT, slippage_bps=100)
    assert result["qty_lamports"] == 1985000
    assert "lastValidBlockHeight" not in http.calls[1][2]["json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["other_message", "other_payer", "wallet_instead_of_payer"])
async def test_missing_initial_sponsor_signature_never_accepts_unrelated_provider_signature(managed, http, corruption):
    sponsor = Keypair.from_seed(bytes([7]) * 32)
    order = order_payload(str(managed.PUBLIC_KEY), router_name="jupiterz", sponsor=sponsor)
    response = execution_payload(order, managed, sponsor=sponsor)
    message = VersionedTransaction.from_bytes(base64.b64decode(order["transaction"])).message
    if corruption == "other_message":
        signature = sponsor.sign_message(b"different original message")
    elif corruption == "other_payer":
        signature = Keypair.from_seed(bytes([9]) * 32).sign_message(to_bytes_versioned(message))
    else:
        signature = managed.KEYPAIR.sign_message(to_bytes_versioned(message))
    response["signature"] = str(signature)
    http.replies.extend([Response(order), Response(response)])
    with pytest.raises(router.SwapSubmissionUncertain):
        await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN,
            amount_lamports=AMOUNT, slippage_bps=100)
    assert [call[0] for call in http.calls] == ["GET", "POST"]
