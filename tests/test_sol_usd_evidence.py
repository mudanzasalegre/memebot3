"""Synthetic SOL/USD clocks/HTTP and isolated financial failure paths only."""
from __future__ import annotations

import asyncio
import ast
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import FrozenInstanceError, replace
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utils import sol_price as sol


@pytest.fixture(autouse=True)
def state(monkeypatch):
    clock = SimpleNamespace(wall=1800000000., mono=1000.)
    monkeypatch.setattr(sol, "time", SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono))
    for name, value in {"_CACHE": None, "_FAILURE": None, "_RETRY_AT": 0., "_GENERATION": 0,
            "_LOCK": sol._LoopNeutralLock(), "_TTL_OK": 60., "_MAX_MARKET_AGE_S": 120.,
            "_SOL_USD_OVERRIDE": 0., "_DEMO_API_KEY": ""}.items():
        monkeypatch.setattr(sol, name, value)
    def forbidden(*args, **kwargs):
        pytest.fail("Real provider access is forbidden")
    monkeypatch.setattr(sol, "aiohttp", SimpleNamespace(ClientSession=forbidden,
        ClientTimeout=sol.aiohttp.ClientTimeout))
    return clock


def body(clock, **changes):
    return json.dumps({"solana": {"usd": 100., "last_updated_at": clock.wall, **changes}}).encode()


def point(clock, **changes):
    return sol.SolUsdObservation("OK", 100., clock.wall, clock.wall, **changes)


def test_valid_point_has_detached_original_clocks_and_immutable_provenance(state):
    observed = sol.parse_sol_usd_body(body(state), received_at=state.wall)
    assert sol.fresh_sol_usd(observed, now=state.wall) == 100.
    assert observed.market_updated_at == observed.received_at == state.wall
    with pytest.raises(FrozenInstanceError): observed.price_usd = 999
    snapshot = observed.to_dict()
    snapshot["received_at"] += 60
    assert observed.received_at == state.wall


@pytest.mark.parametrize("price", [None, True, False, "100", 0, -1, float("nan"), float("inf"), [], {}])
def test_bad_prices_are_unknown_not_zero_or_valid_currency(state, price):
    observed = sol.parse_sol_usd_body(body(state, usd=price), received_at=state.wall)
    assert observed.status == "ERR" and observed.price_usd is None


@pytest.mark.parametrize("updated", [None, True, "1800000000", 0, -1,
    float("nan"), float("inf"), 1800000006., 1799999879.])
def test_missing_invalid_future_or_stale_provider_time_is_unknown(state, updated):
    assert sol.parse_sol_usd_body(body(state, last_updated_at=updated), received_at=state.wall).status == "ERR"


@pytest.mark.parametrize("receipt", [None, True, "1800000000", 0, -1, float("nan"), float("inf")])
def test_bad_client_clock_cannot_certify_a_price(state, receipt):
    assert sol.parse_sol_usd_body(body(state), received_at=receipt).status == "ERR"


@pytest.mark.parametrize("value", [b"", b"null", b"[]", b"{", b"\xff", b'{"solana":null}',
    b'{"solana":{"usd":100}}', b'{"bitcoin":{"usd":100,"last_updated_at":1800000000}}',
    b'{"solana":{"usd":100,"usd":200,"last_updated_at":1800000000}}',
    b'{"solana":{"usd":100,"last_updated_at":1800000000},"solana":{}}',
    b'{"solana":{"usd":NaN,"last_updated_at":1800000000}}',
    b'{"solana":{"usd":100,"last_updated_at":1800000000,"error":"quota"}}',
    b" " * (sol.MAX_BODY_BYTES + 1)],
    ids=["empty", "null", "list", "truncated", "utf8", "null_coin", "missing_time", "wrong_coin",
         "duplicate_price", "duplicate_coin", "nonfinite", "extra_error", "oversize"])
def test_malformed_ambiguous_or_wrong_population_body_is_unknown(state, value):
    assert sol.parse_sol_usd_body(value, received_at=state.wall).status == "ERR"


def test_independent_receipt_and_market_age_boundaries_and_clock_rollback(state):
    observed = sol.parse_sol_usd_body(body(state, last_updated_at=state.wall - 110), received_at=state.wall)
    assert sol.fresh_sol_usd(observed, now=state.wall + 10) == 100.
    assert sol.fresh_sol_usd(observed, now=state.wall + 10.001) is None
    assert sol.fresh_sol_usd(point(state), now=state.wall + 60) == 100.
    assert sol.fresh_sol_usd(point(state), now=state.wall + 60.001) is None
    assert sol.fresh_sol_usd(point(state), now=state.wall - .001) is None
    assert sol.fresh_sol_usd(replace(point(state), source="other"), now=state.wall) is None


@pytest.mark.parametrize("bad", ["broken", "nan", "inf", "-1", "999999"])
def test_malformed_optional_settings_are_cold_import_safe_without_network(tmp_path, bad):
    env = dict(os.environ)
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), COINGECKO_SOL_TTL=bad,
        COINGECKO_SOL_MAX_MARKET_AGE_S=bad, COINGECKO_TIMEOUT=bad, SOL_USD_OVERRIDE=bad)
    code = "import socket; socket.socket.connect=lambda *a: (_ for _ in ()).throw(AssertionError('network forbidden')); from utils import sol_price as s; assert (s._TTL_OK,s._MAX_MARKET_AGE_S,s._TIMEOUT)==(60,120,6); print('cold_import_pass')"
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "cold_import_pass"


class Response:
    def __init__(self, data, *, status=200, headers=None, exit_hook=None):
        self.body, self.status, self.headers = data, status, headers or {}
        self.content, self.offset, self.read_sizes = self, 0, []
        self.exit_hook = exit_hook
    async def __aenter__(self): return self
    async def __aexit__(self, *args):
        if self.exit_hook: self.exit_hook()
    async def read(self, size):
        self.read_sizes.append(size)
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk
    async def json(self, **kwargs): pytest.fail("Unbounded JSON decode is forbidden")


@pytest.fixture
def http(state, monkeypatch):
    replies, calls = [], []
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            item = replies.pop(0)
            if isinstance(item, BaseException): raise item
            return item
    monkeypatch.setattr(sol.aiohttp, "ClientSession", Session)
    return replies, calls


@pytest.mark.asyncio
async def test_real_adapter_uses_documented_request_no_redirect_and_original_receipt(state, http, monkeypatch):
    replies, calls = http
    monkeypatch.setattr(sol, "_DEMO_API_KEY", "synthetic-fixture-key")
    replies.append(Response(body(state), exit_hook=lambda: setattr(state, "wall", state.wall + 3)))
    observed = await sol.get_sol_usd_observation()
    assert observed.received_at == state.wall - 3 and observed.market_updated_at == state.wall - 3
    url, args = calls[0]
    assert url.startswith("https://api.coingecko.com/api/v3/simple/price?")
    assert "ids=solana&vs_currencies=usd&include_last_updated_at=true&precision=full" in url
    assert args["allow_redirects"] is False and args["timeout"].total == 6
    assert args["headers"] == {"x-cg-demo-api-key": "synthetic-fixture-key"}
    assert "synthetic-fixture-key" not in json.dumps(observed.to_dict())
    assert await sol.get_sol_usd() == 100. and len(calls) == 1
    assert (await sol.get_sol_usd_observation()).received_at == observed.received_at


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 400, 401, 403, 429, 500])
async def test_all_non_success_responses_are_unknown_without_parse_or_redirect(state, http, status):
    replies, calls = http
    response = Response(body(state), status=status)
    replies.append(response)
    assert await sol.get_sol_usd() is None
    assert response.read_sizes == [] and len(calls) == 1
    assert await sol.get_sol_usd(force_refresh=True) is None and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [str(sol.MAX_BODY_BYTES + 1), "-1", "not-a-size"])
async def test_invalid_length_is_rejected_before_reading(state, http, header):
    response = Response(body(state), headers={"Content-Length": header})
    http[0].append(response)
    assert await sol.get_sol_usd() is None and response.read_sizes == []


@pytest.mark.asyncio
async def test_decompressed_body_is_bounded_and_context_delay_cannot_refresh_its_age(state, http):
    large = Response(b" " * (sol.MAX_BODY_BYTES + 100))
    http[0].append(large)
    assert (await sol._fetch_observation()).status == "ERR"
    assert large.offset == sol.MAX_BODY_BYTES + 1 and max(large.read_sizes) <= 16384
    http[0].append(Response(body(state), exit_hook=lambda: setattr(state, "wall", state.wall + 61)))
    assert await sol.get_sol_usd() is None and sol._CACHE is None


@pytest.mark.asyncio
async def test_failed_refresh_never_resurrects_last_good_or_slides_clock(state, http):
    replies, calls = http
    replies.append(Response(body(state)))
    old = await sol.get_sol_usd_observation()
    state.wall += 61
    state.mono += 61
    replies.append(Response(b"{}", status=429))
    assert await sol.get_sol_usd() is None and sol._CACHE is None
    assert old.received_at == state.wall - 61
    assert await sol.get_sol_usd(force_refresh=True) is None and len(calls) == 2
    state.wall += 11
    state.mono += 11
    replies.append(Response(body(state, usd=120)))
    assert await sol.get_sol_usd() == 120 and len(calls) == 3


@pytest.mark.asyncio
async def test_force_failure_invalidates_still_young_cached_value(state, http):
    http[0].append(Response(body(state)))
    assert await sol.get_sol_usd() == 100
    http[0].append(RuntimeError("fixture credential must never appear in logs"))
    assert await sol.get_sol_usd(force_refresh=True) is None
    assert await sol.get_sol_usd() is None and len(http[1]) == 2


@pytest.mark.asyncio
async def test_concurrent_refresh_is_single_flight_and_cancel_drains_owned_lock(state, monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()
    async def fetch():
        started.set()
        await finish.wait()
        return point(state)
    mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(sol, "_fetch_observation", mock)
    first = asyncio.create_task(sol.get_sol_usd_observation(force_refresh=True))
    await started.wait()
    second = asyncio.create_task(sol.get_sol_usd_observation(force_refresh=True))
    await asyncio.sleep(0)
    finish.set()
    a, b = await asyncio.gather(first, second)
    assert a is b and mock.await_count == 1
    started.clear()
    finish.clear()
    cancelled = asyncio.create_task(sol.get_sol_usd_observation(force_refresh=True))
    await started.wait()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError): await cancelled
    assert sol._CACHE is None and sol._FAILURE is None and not sol._LOCK.locked()


def test_shared_refresh_survives_contended_sequential_event_loops(state, monkeypatch):
    async def fetch():
        await asyncio.sleep(.01)
        return point(state)
    mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(sol, "_fetch_observation", mock)
    async def pair():
        return await asyncio.gather(sol.get_sol_usd(force_refresh=True), sol.get_sol_usd(force_refresh=True))
    assert asyncio.run(pair()) == [100., 100.]
    assert asyncio.run(pair()) == [100., 100.]
    assert mock.await_count == 2 and not sol._LOCK.locked()


def test_shared_refresh_handles_simultaneous_thread_owned_loops(state, monkeypatch):
    async def fetch():
        await asyncio.sleep(.05)
        return point(state)
    mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(sol, "_fetch_observation", mock)
    async def pair():
        return await asyncio.gather(sol.get_sol_usd(force_refresh=True), sol.get_sol_usd(force_refresh=True))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(asyncio.run, pair()) for _ in range(4)]
        assert [future.result(timeout=3) for future in futures] == [[100., 100.]] * 4
    assert 1 <= mock.await_count <= 4 and not sol._LOCK.locked()


@pytest.mark.asyncio
async def test_manual_override_is_assumption_not_financial_market_evidence(state, monkeypatch):
    monkeypatch.setattr(sol, "_SOL_USD_OVERRIDE", 123.)
    observed = await sol.get_sol_usd_observation(force_refresh=True)
    assert observed.assumed is True and observed.received_at is observed.market_updated_at is None
    assert sol.fresh_sol_usd(observed) is None and await sol.get_sol_usd() is None
    assert await sol.get_sol_usd(allow_assumed=True) == 123.
    assert await sol.amount_sol_to_usd(.1) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [None, True, "0.1", -1., float("nan"), float("inf"), 1e308])
async def test_currency_conversion_rejects_invalid_amount_or_overflow(state, http, amount):
    http[0].append(Response(body(state)))
    assert await sol.amount_sol_to_usd(amount) is None


@pytest.mark.asyncio
async def test_exact_size_conversion_uses_original_checked_point(state, http):
    http[0].append(Response(body(state)))
    assert await sol.amount_sol_to_usd(.1) == 10.
    assert await sol.amount_sol_to_usd(0) == 0.


@pytest.mark.asyncio
async def test_actual_paper_notional_and_existing_exit_do_not_use_unavailable_fx(state, monkeypatch, tmp_path):
    from trader import papertrading as paper
    monkeypatch.setattr(paper, "get_sol_usd", sol.get_sol_usd)
    monkeypatch.setattr(paper, "amount_sol_to_usd", sol.amount_sol_to_usd)
    monkeypatch.setattr(sol, "_fetch_observation", AsyncMock(return_value=sol.SolUsdObservation("ERR")))
    assert await paper._resolve_entry_notional_usd(.1) == 0.
    entry = {"entry_qty": 1000, "qty_lamports": 1000, "amount_sol": .1, "buy_price_usd": 1.,
        "entry_notional_usd": 10., "closed": False, "execution_cost_model": {
            "version": "estimated-v1", "slippage_bps": 100., "fee_sol_per_fill": .000025}}
    monkeypatch.setattr(paper, "_PORTFOLIO", {"A" * 44: copy.deepcopy(entry)})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "paper_portfolio.json")
    monkeypatch.setattr(paper, "_resolve_close_price_usd", AsyncMock(return_value=(2., "hint")))
    before = copy.deepcopy(paper._ensure_entry_accounting(paper._PORTFOLIO["A" * 44]))
    result = await paper.sell("A" * 44, 1000)
    assert result["ok"] is False and result["qty_sold"] == 0
    assert paper._PORTFOLIO["A" * 44] == before and not tmp_path.joinpath("paper_portfolio.json").exists()


@pytest.mark.parametrize("basis", [None, True, 0, -1, float("nan"), float("inf"), "10"])
@pytest.mark.asyncio
async def test_paper_missing_historical_entry_basis_is_not_backfilled_from_today(state, monkeypatch, tmp_path, basis):
    from trader import papertrading as paper
    entry = {"entry_qty": 1000, "qty_lamports": 1000, "amount_sol": .1, "buy_price_usd": 1.,
        "entry_notional_usd": basis, "closed": False}
    monkeypatch.setattr(paper, "_PORTFOLIO", {"A" * 44: entry})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "paper_portfolio.json")
    fx = AsyncMock(return_value=100.)
    monkeypatch.setattr(paper, "get_sol_usd", fx)
    assert await paper.backfill_entry_notionals() == 0 and entry["entry_notional_usd"] is basis
    result = await paper.sell("A" * 44, 1000)
    assert result["error"] == "ENTRY_BASIS_UNAVAILABLE" and entry["qty_lamports"] == 1000
    fx.assert_not_awaited()


@pytest.mark.parametrize("pnl", [None, True, "1", float("nan"), float("inf")])
def test_unknown_live_canary_valuation_does_not_reset_losses_or_reopen_risk_budget(monkeypatch, pnl):
    from runtime import live_canary as canary
    monkeypatch.setattr(canary, "STATE", canary.LiveCanaryState(consecutive_losses=1))
    monkeypatch.setattr(canary, "CFG", SimpleNamespace(STRATEGY_OPTIMIZATION_LOCK=False,
        GREEN_SNIPER_LIVE_ENABLED=True, GREEN_SNIPER_LIVE_DISABLE_ON_LIQ_CRUSH=True))
    canary.record_green_live_close(pnl_sol=pnl)
    assert canary.STATE.consecutive_losses == 1 and canary.STATE.daily_loss_sol == {}
    assert canary.STATE.unvalued_closes == 1 and canary.snapshot()["disabled"]
    canary.STATE.disabled_until = "2000-01-01T00:00:00+00:00"
    assert canary.evaluate_green_live_canary({}) == (False, "pnl_valuation_unavailable")
    canary.record_green_live_close(pnl_sol=1.)
    assert canary.STATE.unvalued_closes == 1


@pytest.mark.parametrize("basis", [None, True, 0, -1, float("nan"), float("inf"), "10"])
@pytest.mark.asyncio
async def test_actual_sql_missing_basis_is_not_current_fx_backfill(monkeypatch, basis):
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "_ensure_position_entry_notional")
    fx = AsyncMock(return_value=100.)
    ns = {"Position": object, "SessionLocal": object, "math": math, "get_sol_usd": fx}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "run_bot.py", "exec"), ns)
    pos = SimpleNamespace(entry_notional_usd=basis, buy_amount_sol=.1, closed=False)
    assert await ns[function.name](pos, object()) is False
    fx.assert_not_awaited()
    assert pos.entry_notional_usd is basis


@pytest.mark.parametrize("fx,pnl,expected", [(None, -10., None), (True, -10., None),
    (0., -10., None), (float("nan"), -10., None), (100., None, None),
    (100., True, None), (100., -10., -.1), (.5, -10., -20.), (100., 0., 0.)])
@pytest.mark.asyncio
async def test_actual_live_monitor_canary_binding_never_invents_one_dollar_sol(fx, pnl, expected):
    tree = ast.parse(Path("run_bot.py").read_text(encoding="utf-8"))
    block = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
        and "not DRY_RUN" in ast.unparse(n.test)
        and "pump_early_green_candle_sniper" in ast.unparse(n.test))
    records = []
    wrapper = ast.AsyncFunctionDef(name="evaluate", args=ast.arguments(posonlyargs=[], args=[],
        kwonlyargs=[], kw_defaults=[], defaults=[]), body=[block], decorator_list=[])
    ns = {"math": math, "DRY_RUN": False, "exit_reason": "STOP_LOSS",
        "pos": SimpleNamespace(entry_lane="pump_early_green_candle_sniper", total_pnl_usd=pnl),
        "get_sol_usd": AsyncMock(return_value=fx),
        "live_canary": SimpleNamespace(record_green_live_close=lambda **kw: records.append(kw))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "run_bot.py", "exec"), ns)
    await ns["evaluate"]()
    assert records == [{"pnl_sol": expected, "exit_reason": "STOP_LOSS"}]
