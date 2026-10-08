"""Current gateway, local budgets and real adapter boundaries; synthetic only."""
from __future__ import annotations

import asyncio
import base64
import copy
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utils import jupiter_access as access
from fetcher import jupiter_price as price, jupiter_router as router
from runtime import execution_provenance
from jupiter_access_fixtures import isolate_budget, REAL_SLEEP
from test_jupiter_managed_contract import managed, http, order_payload, execution_payload, Response, TOKEN
from test_live_signing_boundaries import signer
from test_jupiter_quote_contract import SOL, AMOUNT, payload as quote_payload
from test_live_quote_admission import guarded


def payload():
    value = quote_payload()
    value["outputMint"] = TOKEN
    value["routePlan"][0]["swapInfo"]["outputMint"] = TOKEN
    return value


@pytest.mark.parametrize("operation,path", [("quote", "/swap/v1/quote"),
    ("swap", "/swap/v1/swap"), ("price", "/price/v3")])
@pytest.mark.parametrize("host", ["api.jup.ag", "lite-api.jup.ag", "quote-api.jup.ag"])
def test_known_host_aliases_migrate_before_http(operation, path, host):
    assert access.endpoint(f"https://{host}{path}", operation) == f"https://api.jup.ag{path}"


@pytest.mark.parametrize("operation", ["quote", "swap"])
def test_v6_aliases_migrate_without_a_second_request(operation):
    assert access.endpoint(f"https://quote-api.jup.ag/v6/{operation}", operation) == f"https://api.jup.ag/swap/v1/{operation}"


@pytest.mark.parametrize("url", ["http://api.jup.ag/swap/v1/quote", "https://api.jup.ag:444/swap/v1/quote",
    "https://api.jup.ag/swap/v1/quote?key=secret", "https://:@api.jup.ag/swap/v1/quote",
    "https://user:secret@synthetic.invalid/quote", "https://api.jup.ag/wrong", "http://[bad",
    "https://synthetic.invalid/quote#fragment", "https://synthetic.invalid:99999/quote"])
def test_invalid_endpoints_are_not_authorized(url):
    with pytest.raises(ValueError): access.endpoint(url, "quote")


@pytest.mark.parametrize("url,forward", [("https://api.jup.ag/price/v3?ids=synthetic", True),
    ("https://api.jup.ag:443/swap/v2/order", True), ("https://api.jup.ag.evil.invalid/quote", False),
    ("https://lite-api.jup.ag/price/v3", False), ("http://api.jup.ag/quote", False),
    ("https://:@api.jup.ag/quote", False), ("http://127.0.0.1/quote", False),
    ("https://synthetic.invalid/quote", False)])
def test_credentials_only_forward_to_exact_https_gateway(url, forward):
    headers = access.headers("synthetic-key", url)
    assert (headers.get("x-api-key") == "synthetic-key") is forward
    assert access.headers("", url) == {"accept": "application/json"}


@pytest.mark.parametrize("key", [None, 1, "a\r\nb", "a\x00b", "a\x7fb", "a" * 513])
def test_invalid_credentials_fail_before_transport(key):
    with pytest.raises(ValueError): access.headers(key, "https://api.jup.ag/swap/v2/order")


@pytest.mark.parametrize("raw", ["nan", "inf", "0", "-1", "151", "not-a-rate"])
def test_bad_configuration_cannot_expand_or_disable_budget(monkeypatch, raw):
    monkeypatch.setenv("JUP_API_RPS", raw)
    with pytest.raises(ValueError): access.rates(True)


@pytest.mark.parametrize("raw,keyed,expected", [("", False, (.5, 20)), ("", True, (1, 50)),
    ("10", True, (10, 100)), ("150", True, (150, 100)), ("150", False, (.5, 20)),
    (".25", True, (.25, 50))])
def test_default_and_explicit_plan_rates(monkeypatch, raw, keyed, expected):
    monkeypatch.setenv("JUP_API_RPS", raw)
    assert access.rates(keyed) == expected


@pytest.mark.asyncio
async def test_main_paces_sliding_window_and_execute_is_independent(monkeypatch):
    budget = isolate_budget(monkeypatch)
    stamps = []
    for _ in range(31):
        await budget.acquire(keyed=False, max_wait=3)
        stamps.append(budget.clock())
    assert stamps[0] == 0 and stamps[-1] >= 60
    assert all(b - a >= 2 - 1e-9 for a, b in zip(stamps, stamps[1:]))
    assert max(sum(t <= x < t + 60 for x in stamps) for t in stamps) <= 30
    start = budget.clock()
    await budget.acquire(keyed=False, execute=True)
    assert budget.clock() == start
    await budget.acquire(keyed=False, execute=True)
    assert .05 - 1e-9 <= budget.clock() - start <= .051
    assert budget.snapshot()["keyless_main"]["granted"] == 31
    assert budget.snapshot()["keyless_execute"]["granted"] == 2


@pytest.mark.asyncio
async def test_header_reset_is_epoch_one_slot_not_counter_reset(monkeypatch):
    budget = isolate_budget(monkeypatch)
    await budget.acquire(keyed=False)
    budget.observe(keyed=False, status=429, headers={"x-ratelimit-remaining": "-2",
        "x-ratelimit-reset": str(budget.wall() + 4)})
    await budget.acquire(keyed=False, max_wait=5)
    assert 4 <= budget.clock() < 4.01
    assert len(budget._buckets[(False, False)].admissions) == 2
    assert budget.snapshot()["keyless_main"]["statuses"] == {429: 1}
    before = budget.clock()
    await budget.acquire(keyed=False, max_wait=3)
    assert budget.clock() - before >= 2 - 1e-9


@pytest.mark.asyncio
@pytest.mark.parametrize("status,headers", [(429, {}), (429, {"x-ratelimit-reset": "nan"}),
    (200, {"x-ratelimit-remaining": "0"}), (200, {"x-ratelimit-remaining": "-1"})])
async def test_missing_or_corrupt_reset_has_bounded_conservative_backoff(monkeypatch, status, headers):
    budget = isolate_budget(monkeypatch)
    budget.observe(keyed=True, execute=True, status=status, headers=headers)
    await budget.acquire(keyed=True, execute=True, max_wait=2)
    assert 1 <= budget.clock() < 1.01


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 500])
async def test_permission_errors_do_not_reset_admissions_or_drop_key(monkeypatch, status):
    budget = isolate_budget(monkeypatch)
    await budget.acquire(keyed=True)
    budget.observe(keyed=True, status=status, headers={"x-ratelimit-reset": "9999999999"})
    with pytest.raises(access.BudgetUnavailable): await budget.acquire(keyed=True, max_wait=.2)
    assert len(budget._buckets[(True, False)].admissions) == 1


@pytest.mark.asyncio
async def test_zero_deadline_queue_refusal_is_not_admitted(monkeypatch):
    budget = isolate_budget(monkeypatch)
    await budget.acquire(keyed=False)
    with pytest.raises(access.BudgetUnavailable): await budget.acquire(keyed=False, max_wait=1)
    assert budget.snapshot()["keyless_main"] == {"queued": 0, "granted": 1,
        "refused": 1, "cancelled": 0, "statuses": {}}


@pytest.mark.asyncio
async def test_exit_priority_precedes_waiting_price_and_cancel_removes_ticket(monkeypatch):
    now, pending, admitted = [0.0], [], []
    async def sleep(delay):
        event = asyncio.Event()
        pending.append(event)
        await event.wait()
    monkeypatch.setenv("JUP_API_RPS", "")
    budget = access.AccessBudget(clock=lambda: now[0], sleep=sleep)
    await budget.acquire(keyed=False)
    async def request(name, priority):
        await budget.acquire(keyed=False, priority=priority)
        admitted.append(name)
    background = asyncio.create_task(request("price", 3))
    await REAL_SLEEP(0)
    exit_task = asyncio.create_task(request("exit", 0))
    await REAL_SLEEP(0)
    now[0] = 2
    for event in pending: event.set()
    await REAL_SLEEP(0)
    assert admitted == ["exit"]
    assert exit_task.done() and not background.done()
    background.cancel()
    with pytest.raises(asyncio.CancelledError): await background
    assert budget.snapshot()["keyless_main"]["cancelled"] == 1
    assert budget.snapshot()["keyless_main"]["queued"] == 0


@pytest.mark.asyncio
async def test_queue_capacity_and_invalid_arguments_are_bounded(monkeypatch):
    budget = isolate_budget(monkeypatch)
    bucket = budget._bucket(False, False)
    bucket.tickets.update({i: (3, i) for i in range(128)})
    with pytest.raises(access.BudgetUnavailable): await budget.acquire(keyed=False)
    for kwargs in ({"priority": True}, {"priority": 4}, {"max_wait": True},
                   {"max_wait": 0}, {"max_wait": float("inf")}, {"max_wait": 61}):
        with pytest.raises(ValueError): await budget.acquire(keyed=False, **kwargs)
    assert bucket.granted == 0


def test_budget_shared_across_real_threads_and_event_loops(monkeypatch):
    monkeypatch.setenv("JUP_API_RPS", "150")
    budget = access.AccessBudget(sleep=REAL_SLEEP)
    def call(): asyncio.run(budget.acquire(keyed=True, execute=True, max_wait=2))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: call(), range(12)))
    admissions = list(budget._buckets[(True, True)].admissions)
    assert len(admissions) == 12
    assert all(b - a >= .01 - 1e-6 for a, b in zip(admissions, admissions[1:]))
    assert budget.snapshot()["keyed_execute"]["queued"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["metis", "jupiterz", "dflow", "okx"])
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_keyless_actual_managed_flow_keeps_original_proofs_and_one_post(managed, http, monkeypatch, route, side):
    monkeypatch.setattr(router, "JUP_API_KEY", "")
    order = order_payload(str(managed.PUBLIC_KEY), router_name=route)
    if side == "sell": order.update(inputMint=TOKEN, outputMint=SOL, feeMint=TOKEN)
    http.replies.extend([Response(order), Response(execution_payload(order, managed))])
    stages = []
    with execution_provenance.execution_scope(lambda stage, data: stages.append(stage)):
        result = await router.execute_managed_swap(input_mint=order["inputMint"], output_mint=order["outputMint"],
            amount_lamports=AMOUNT, slippage_bps=100)
    assert result["fill_verified"] is True and result["qty_lamports"] == 1985000
    assert result["submission_capsule"]["version"] == 2
    assert stages == ["prepared_submission", "dispatch_started", "provider_response", "chain_receipt"]
    assert [c[0] for c in http.calls] == ["GET", "POST"]
    assert all("x-api-key" not in c[3] and c[2]["allow_redirects"] is False for c in http.calls)
    assert access._BUDGET.snapshot()["keyless_main"]["granted"] == 1
    assert access._BUDGET.snapshot()["keyless_execute"]["granted"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429])
async def test_invalid_key_or_quota_never_retries_anonymously(managed, http, status):
    http.replies.append(Response(status=status))
    with pytest.raises(router.SwapPreparationError):
        await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=AMOUNT)
    assert len(http.calls) == 1 and http.calls[0][3]["x-api-key"] == "synthetic-key"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["budget", "aged"])
async def test_execute_budget_wait_rechecks_age_before_post_and_dispatch_marker(managed, http, monkeypatch, fault):
    order = order_payload(str(managed.PUBLIC_KEY))
    http.replies.append(Response(order))
    original = access.acquire
    async def acquire(url, **kwargs):
        if url.endswith("/execute"):
            if fault == "budget": raise access.BudgetUnavailable("synthetic saturated queue")
            def aged(*args): raise ValueError("synthetic aged projection")
            monkeypatch.setattr(router.solana_execution, "check_projection_age", aged)
        await original(url, **kwargs)
    monkeypatch.setattr(access, "acquire", acquire)
    stages = []
    with execution_provenance.execution_scope(lambda stage, data: stages.append(stage)):
        with pytest.raises(router.SwapPreparationError):
            await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=AMOUNT)
    assert stages == ["prepared_submission"] and [c[0] for c in http.calls] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [router._ManagedBudgetBeforeDispatch("synthetic after POST"),
    router._ManagedExpiredBeforeDispatch("synthetic after POST")])
async def test_pre_dispatch_exception_type_after_post_cannot_claim_no_submission(managed, http, error):
    order = order_payload(str(managed.PUBLIC_KEY))
    http.replies.extend([Response(order), Response(error)])
    stages = []
    with execution_provenance.execution_scope(lambda stage, data: stages.append(stage)):
        with pytest.raises(router.SwapSubmissionUncertain):
            await router.execute_managed_swap(input_mint=SOL, output_mint=TOKEN, amount_lamports=AMOUNT)
    assert stages == ["prepared_submission", "dispatch_started"]
    assert [c[0] for c in http.calls] == ["GET", "POST"]


@pytest.mark.asyncio
async def test_actual_buyer_selects_keyless_managed_route_without_legacy_probe(guarded, monkeypatch):
    from trader import buyer
    monkeypatch.setattr(router, "JUP_MANAGED_ENABLED", True)
    assert router.JUP_API_KEY == ""
    result = await buyer.buy(TOKEN, .1)
    assert result["qty_lamports"] == 1000
    guarded[0].assert_awaited_once()
    guarded[1].assert_not_awaited()
    assert guarded[0].await_args.kwargs["amount_lamports"] == AMOUNT


@pytest.mark.asyncio
@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("status_reader", [False, True])
async def test_price_and_quote_share_actual_budget_and_scoped_uncached_headers(monkeypatch, custom, status_reader):
    budget = isolate_budget(monkeypatch)
    requests = []
    mint = "A" * 44
    class Reply:
        status, headers = 200, {}
        def __init__(self, value): self.value = value
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self, **kwargs): return copy.deepcopy(self.value)
    class Session:
        def __init__(self, **kwargs): self.headers = kwargs.get("headers", {})
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            requests.append((url, copy.deepcopy(kwargs), copy.deepcopy(self.headers)))
            assert kwargs["allow_redirects"] is False
            return Reply(payload() if url.endswith("/quote") else {mint: {"usdPrice": 2}})
    session = Session()
    monkeypatch.setattr(router.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(price, "_ensure_session", AsyncMock(return_value=session))
    monkeypatch.setattr(price, "_throttle", AsyncMock())
    monkeypatch.setattr(router, "JUP_API_KEY", "synthetic-key")
    monkeypatch.setattr(price, "_JUP_API_KEY", "synthetic-key")
    monkeypatch.setattr(router, "JUP_QUOTE_URL", router._LITE_QUOTE_URL)
    monkeypatch.setattr(price, "JUPITER_PRICE_URL",
        "https://synthetic.invalid/price" if custom else "https://lite-api.jup.ag/price/v3")
    result = await (price._fetch_batch_with_status if status_reader else price._fetch_batch)([mint])
    assert result[mint] == (("OK", 2.) if status_reader else 2.)
    assert "x-api-key" not in session.headers
    assert (requests[0][1]["headers"].get("x-api-key") == "synthetic-key") is (not custom)
    assert (await router.get_quote(input_mint=SOL, output_mint=TOKEN, amount_sol=.1)).ok
    assert budget.snapshot()["keyed_main"]["granted"] == (1 if custom else 2)
    assert budget.clock() >= (0 if custom else 1)


@pytest.mark.asyncio
async def test_persistent_price_session_never_retains_jupiter_secret(monkeypatch):
    kwargs = []
    monkeypatch.setattr(price, "_SESSION", None)
    monkeypatch.setattr(price, "_JUP_API_KEY", "synthetic-secret")
    monkeypatch.setattr(price.aiohttp, "ClientSession", lambda **kw: kwargs.append(kw) or SimpleNamespace(closed=False))
    await price._ensure_session()
    assert len(kwargs) == 1 and "x-api-key" not in kwargs[0]["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("output,priority", [(SOL, 0), (TOKEN, 1)])
async def test_actual_unsigned_build_prioritizes_exit_not_entry(http, monkeypatch, output, priority):
    monkeypatch.setattr(router, "JUP_API_KEY", "")
    monkeypatch.setattr(router, "JUP_SWAP_URL", router._LITE_SWAP_URL)
    seen = []
    original = access.acquire
    async def acquire(url, **kwargs):
        seen.append((url, kwargs["priority"]))
        await original(url, **kwargs)
    monkeypatch.setattr(access, "acquire", acquire)
    http.replies.append(Response({"swapTransaction": base64.b64encode(b"synthetic-unsigned").decode(),
        "lastValidBlockHeight": 123}))
    assert await router._build_swap_transaction({"quoteResponse": {"outputMint": output}}, max_retries=0) == b"synthetic-unsigned"
    assert seen == [(router._API_SWAP_URL, priority)]
    assert len(http.calls) == 1 and http.calls[0][2]["allow_redirects"] is False
    assert "x-api-key" not in http.calls[0][3]
