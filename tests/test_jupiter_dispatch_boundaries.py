"""Actual Jupiter HTTP/SDK/consumer boundaries with synthetic inputs only."""
from __future__ import annotations

import asyncio
import base64
import copy
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from solders.transaction import VersionedTransaction

from fetcher import jupiter_router as router
from runtime import owned_dispatch as owned
from runtime.sell_recovery import SellOutcomeUncertain
from trader import seller
from test_jupiter_quote_contract import payload, checked
from test_live_signing_boundaries import signer, unsigned

REAL_SLEEP = asyncio.sleep


@pytest.fixture
def wallet(monkeypatch):
    import trader
    fake = SimpleNamespace(PUBLIC_KEY="synthetic-owner", sign_and_send=Mock(return_value="synthetic-signature"))
    monkeypatch.setitem(trader.__dict__, "sol_signer", fake)
    monkeypatch.setenv("SOL_PUBLIC_KEY", fake.PUBLIC_KEY)
    return fake


class Response:
    def __init__(self, body=None, *, status=200, error=None, enter=None):
        self.body, self.status, self.error, self.enter = body, status, error, enter
    async def __aenter__(self):
        if self.enter: self.enter()
        if self.error: raise self.error
        return self
    async def __aexit__(self, *args): pass
    async def json(self, **kwargs):
        if isinstance(self.body, BaseException): raise self.body
        return copy.deepcopy(self.body)


def packet(raw=b"synthetic-unsigned"):
    return {"swapTransaction": base64.b64encode(raw).decode("ascii"), "lastValidBlockHeight": 123}


@pytest.fixture
def http(monkeypatch):
    calls, responses, closed, delays = [], [], [], []
    class Session:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): closed.append(True)
        def post(self, url, **kwargs):
            calls.append((url, copy.deepcopy(kwargs)))
            assert responses, "Unexpected extra build request"
            response = responses.pop(0)
            if isinstance(response, BaseException): raise response
            return response
    async def sleep(delay):
        assert len(closed) == len(calls), "Close each HTTP session before backoff"
        delays.append(delay)
    monkeypatch.setattr(router.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(router.asyncio, "sleep", sleep)
    monkeypatch.setattr(router, "JUP_SWAP_URL", "https://synthetic.invalid/swap")
    monkeypatch.setattr(router, "JUP_API_KEY", "")
    monkeypatch.setattr(router, "JUP_LEGACY_SWAP_ENABLED", True)
    monkeypatch.setattr(router, "_PRIORITY_FEE_RAW", "")
    return SimpleNamespace(calls=calls, responses=responses, closed=closed, delays=delays)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["raw", "checked"])
async def test_unsigned_build_snapshot_then_one_shared_owned_dispatch(http, wallet, shape):
    body = payload(amount=2**53 + 1)
    original = copy.deepcopy(body)
    quote = body if shape == "raw" else checked(body, amount=2**53 + 1)
    http.responses.append(Response(packet()))
    result = await router.execute_swap(quote, user_public_key=wallet.PUBLIC_KEY, max_retries=0,
        skip_preflight=True, prioritization_fee_lamports=20000)
    assert result == "synthetic-signature" and len(http.calls) == 1
    assert http.calls[0][1]["json"]["quoteResponse"] == original
    assert http.calls[0][1]["json"]["prioritizationFeeLamports"] == 20000
    wallet.sign_and_send.assert_called_once_with(b"synthetic-unsigned", skip_preflight=True)
    assert body == original and owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("transient", [429, 500, 503, asyncio.TimeoutError("synthetic build"),
                                      aiohttp.ClientConnectionError("synthetic connect")])
async def test_only_unsigned_build_retries_with_closed_sessions_and_original_payload(http, wallet, transient):
    body = payload()
    original = copy.deepcopy(body)
    first = Response(status=transient) if isinstance(transient, int) else transient
    http.responses.extend([first, Response(packet(), enter=lambda: body.update(inAmount="7"))])
    assert await router.execute_swap(body, max_retries=1) == "synthetic-signature"
    assert [call[1]["json"]["quoteResponse"] for call in http.calls] == [original, original]
    assert http.delays == [.6] and len(http.closed) == 2
    wallet.sign_and_send.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError("synthetic acknowledgement loss"),
    TypeError("synthetic after send"), router.SwapPreparationError("synthetic error after dispatch")])
async def test_any_dispatch_error_is_uncertain_and_never_rebuilds_even_if_it_looks_prepared(http, wallet, error):
    http.responses.extend([Response(packet()), Response(packet(b"must-not-rebuild"))])
    wallet.sign_and_send.side_effect = error
    with pytest.raises(router.SwapSubmissionUncertain) as caught:
        await router.execute_swap(checked(payload()), max_retries=5)
    assert caught.value.__cause__ is error
    assert len(http.calls) == 1 and not http.delays
    wallet.sign_and_send.assert_called_once()
    assert owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_nontransient_unsigned_http_failure_never_retries_or_dispatches(http, wallet, status):
    http.responses.append(Response(status=status))
    with pytest.raises(router.SwapPreparationError, match="HTTP"):
        await router.execute_swap(checked(payload()), max_retries=5)
    assert len(http.calls) == len(http.closed) == 1 and not http.delays
    wallet.sign_and_send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("retries", [0, 1, 5])
async def test_unsigned_transport_exhaustion_is_bounded_and_pre_dispatch(http, wallet, retries):
    http.responses.extend(Response(status=503) for _ in range(retries + 1))
    with pytest.raises(router.SwapPreparationError, match="transport"):
        await router.execute_swap(checked(payload()), max_retries=retries)
    assert len(http.calls) == len(http.closed) == retries + 1
    assert http.delays == [.6 * (i + 1) for i in range(retries)]
    wallet.sign_and_send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, [], {}, ValueError("invalid synthetic JSON"),
    {"error": "synthetic"}, {"errorCode": 1}, {"simulationError": {}},
    {"swapTransaction": ""}, {"swapTransaction": True}, {"swapTransaction": "not base64"},
    {"swapTransaction": "YQ==\n"}, packet(b"a" * 1233),
    {**packet(), "transaction": "YQ=="}, {**packet(), "transaction": None},
    {**packet(), "lastValidBlockHeight": True}, {**packet(), "lastValidBlockHeight": "123"},
    {**packet(), "lastValidBlockHeight": -1}, {**packet(), "lastValidBlockHeight": 2**64},
    {**packet(), "prioritizationFeeLamports": 1.5}])
async def test_malformed_unsigned_response_is_not_a_retry_or_send(http, wallet, body):
    http.responses.append(Response(body))
    with pytest.raises(router.SwapPreparationError):
        await router.execute_swap(checked(payload()), max_retries=5)
    assert len(http.calls) == len(http.closed) == 1 and not http.delays
    wallet.sign_and_send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["swapTransaction", "swap_transaction", "transaction", "all"])
async def test_documented_packet_and_consistent_compatibility_aliases(http, wallet, alias):
    encoded = base64.b64encode(b"a" * 1232).decode("ascii")
    body = ({key: encoded for key in ("swapTransaction", "swap_transaction", "transaction")}
            if alias == "all" else {alias: encoded})
    http.responses.append(Response(body))
    assert await router.execute_swap(checked(payload()), max_retries=0) == "synthetic-signature"
    wallet.sign_and_send.assert_called_once_with(b"a" * 1232, skip_preflight=router._SWAP_SKIP_PREFLIGHT_DEFAULT)


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"max_retries": -1}, {"max_retries": 6}, {"max_retries": True},
    {"max_retries": 1.5}, {"wrap_and_unwrap_sol": 1}, {"as_legacy_transaction": "true"},
    {"dynamic_compute_unit_limit": None}, {"skip_preflight": 0},
    {"prioritization_fee_lamports": True}, {"prioritization_fee_lamports": -1},
    {"prioritization_fee_lamports": 2**64}, {"prioritization_fee_lamports": 1.5},
    {"prioritization_fee_lamports": {"invalid": float("nan")}},
    {"user_public_key": "another-owner"}, {"user_public_key": 123}])
async def test_bad_execution_options_fail_before_http_or_signing(http, wallet, options):
    with pytest.raises(router.SwapPreparationError):
        await router.execute_swap(checked(payload()), **options)
    assert not http.calls
    wallet.sign_and_send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("ok", False), ("in_amount", True), ("out_amount", 1),
    ("price_impact_bps", float("nan")), ("price_impact_bps", True), ("other", None),
    ("other", {"onlyDirectRoutes": 1}), ("other", {"requested_in_amount": 100_000_001}),
    ("other", {"inputMint": "foreign"}), ("other", {"slippageBps": True})])
async def test_conflicting_checked_quote_fields_cannot_authorize_build(http, wallet, field, value):
    quote = replace(checked(payload()), **{field: value})
    with pytest.raises(router.SwapPreparationError):
        await router.execute_swap(quote)
    assert not http.calls
    wallet.sign_and_send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("inAmount", "0"), ("inputMint", ""),
    ("slippageBps", True), ("swapMode", "ExactOut"), ("routePlan", []),
    ("outAmount", "0"), ("priceImpactPct", "nan")])
async def test_mutated_checked_raw_quote_is_revalidated_before_build(http, wallet, field, value):
    quote = checked(payload())
    quote.raw[field] = value
    with pytest.raises(router.SwapPreparationError):
        await router.execute_swap(quote)
    assert not http.calls
    wallet.sign_and_send.assert_not_called()


@pytest.mark.asyncio
async def test_declared_wallet_conflict_is_pre_http(http, wallet, monkeypatch):
    monkeypatch.setenv("SOL_PUBLIC_KEY", "different-declared-wallet")
    with pytest.raises(router.SwapPreparationError):
        await router.execute_swap(checked(payload()))
    assert not http.calls
    wallet.sign_and_send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["legacy", "v0"])
@pytest.mark.parametrize("ack", ["matching", "lost", "wrong"])
async def test_actual_installed_sdk_packet_is_signed_once_without_another_build(http, signer, monkeypatch, version, ack):
    import trader
    monkeypatch.setitem(trader.__dict__, "sol_signer", signer)
    monkeypatch.setenv("SOL_PUBLIC_KEY", str(signer.PUBLIC_KEY))
    raw = unsigned(signer.PUBLIC_KEY, version)
    http.responses.append(Response(packet(raw)))
    sent = []
    def broadcast(value, **kwargs):
        sent.append(value)
        if ack == "lost": raise TimeoutError("synthetic acknowledgement loss")
        return SimpleNamespace(value=(str(VersionedTransaction.from_bytes(value).signatures[0])
                                      if ack == "matching" else "wrong-signature"))
    client = SimpleNamespace(send_raw_transaction=broadcast)
    monkeypatch.setattr(signer, "_client_for_url", lambda url: client)
    sign = Mock(wraps=signer.sign_raw_transaction)
    monkeypatch.setattr(signer, "sign_raw_transaction", sign)
    if ack == "matching":
        signature = await router.execute_swap(checked(payload()), max_retries=5)
        assert signature == str(VersionedTransaction.from_bytes(sent[0]).signatures[0])
    else:
        with pytest.raises(router.SwapSubmissionUncertain):
            await router.execute_swap(checked(payload()), max_retries=5)
    assert sign.call_count == len(http.calls) == 1 and not http.delays
    assert len(sent) == (1 if ack == "matching" else 2)
    assert all(value == sent[0] for value in sent)
    actual = VersionedTransaction.from_bytes(sent[0])
    assert actual.message == VersionedTransaction.from_bytes(raw).message
    assert actual.verify_with_results() == [True] and owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, TimeoutError("synthetic after dispatch")])
async def test_cancelled_actual_jupiter_dispatch_retains_worker_until_settlement(http, wallet, monkeypatch, error):
    monkeypatch.setattr(router.asyncio, "sleep", REAL_SLEEP)
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()
    def submit(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "Synthetic worker was not released"
        if error: raise error
        return "synthetic-signature"
    wallet.sign_and_send.side_effect = submit
    http.responses.append(Response(packet()))
    task = asyncio.create_task(router.execute_swap(checked(payload()), max_retries=5))
    try:
        await asyncio.wait_for(started.wait(), 2)
        for _ in range(3):
            task.cancel()
            await REAL_SLEEP(0)
            assert not task.done() and owned.pending_dispatch_count() == 1
        assert len(http.calls) == 1
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    wallet.sign_and_send.assert_called_once()
    assert owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
async def test_cancelled_unsigned_build_never_invokes_signer(http, wallet):
    http.responses.append(Response(error=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await router.execute_swap(checked(payload()), max_retries=5)
    wallet.sign_and_send.assert_not_called()
    assert len(http.calls) == 1 and owned.pending_dispatch_count() == 0 and not http.delays


@pytest.mark.asyncio
async def test_managed_signing_is_owned_and_cancel_prevents_execute_post(wallet, signer, monkeypatch):
    from test_jupiter_managed_contract import order_payload, TOKEN
    wallet.PUBLIC_KEY = str(signer.PUBLIC_KEY)
    monkeypatch.setenv("SOL_PUBLIC_KEY", wallet.PUBLIC_KEY)
    monkeypatch.setattr(router, "JUP_API_KEY", "synthetic-key")
    monkeypatch.setattr(router, "JUP_MANAGED_ENABLED", True)
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()
    def sign(*args):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "Synthetic signing worker was not released"
        return "synthetic-signed"
    wallet.sign_base64_transaction = Mock(side_effect=sign)
    body = order_payload(wallet.PUBLIC_KEY)
    order = AsyncMock(return_value=body)
    execute = AsyncMock(return_value={"signature": "must-not-be-sent"})
    monkeypatch.setattr(router, "get_order", order)
    monkeypatch.setattr(router, "execute_order", execute)
    task = asyncio.create_task(router.execute_managed_swap(input_mint=router.SOL_MINT,
        output_mint=TOKEN, amount_lamports=100_000_000, slippage_bps=100))
    try:
        await asyncio.wait_for(started.wait(), 2)
        for _ in range(3):
            task.cancel()
            await REAL_SLEEP(0)
            assert not task.done() and owned.pending_dispatch_count() == 1
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    wallet.sign_base64_transaction.assert_called_once_with(body["transaction"])
    execute.assert_not_awaited()
    assert owned.pending_dispatch_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["preparation", "uncertain", "spoofed-preparation"])
async def test_actual_seller_falls_back_only_when_adapter_proves_no_dispatch(http, wallet, monkeypatch, failure):
    body = payload(amount=400, output=200)
    body.update(inputMint="CanonicalSyntheticMint", outputMint=router.SOL_MINT)
    body["routePlan"][0]["swapInfo"].update(inputMint=body["inputMint"], outputMint=body["outputMint"])
    quote = router._checked_quote(body, input_mint=body["inputMint"], output_mint=body["outputMint"],
        amount=400, slippage=100, direct=False)
    assert quote.ok
    get_quote = AsyncMock(return_value=quote)
    monkeypatch.setattr(router, "get_quote", get_quote)
    original = router.execute_swap
    seen = []
    async def execute_swap(*, quote):
        seen.append(quote)
        return await original(quote, max_retries=5)
    monkeypatch.setattr(router, "execute_swap", execute_swap)
    monkeypatch.setattr(seller, "jupiter", router)
    monkeypatch.setattr(seller, "_JUP_ROUTER_AVAILABLE", True)
    gmgn = AsyncMock(return_value={"signature": "synthetic-fallback", "route": {}})
    monkeypatch.setattr(seller.gmgn, "sell", gmgn)
    if failure == "preparation":
        http.responses.append(Response(status=403))
        ok, result = await seller._sell_execute_prefer_jupiter("AddressAlias", 400,
            token_mint="CanonicalSyntheticMint", liquidity_usd=1_000_000_000.)
        assert ok and result["venue"] == "gmgn"
        gmgn.assert_awaited_once_with("CanonicalSyntheticMint", 400)
        wallet.sign_and_send.assert_not_called()
    else:
        http.responses.append(Response(packet()))
        wallet.sign_and_send.side_effect = (router.SwapPreparationError("synthetic after send")
            if failure == "spoofed-preparation" else TimeoutError("synthetic after send"))
        with pytest.raises(SellOutcomeUncertain):
            await seller._sell_execute_prefer_jupiter("AddressAlias", 400,
                token_mint="CanonicalSyntheticMint", liquidity_usd=1_000_000_000.)
        gmgn.assert_not_awaited()
        wallet.sign_and_send.assert_called_once()
    assert seen == [quote] and seen[0] is quote
    assert len(http.calls) == 1 and owned.pending_dispatch_count() == 0
