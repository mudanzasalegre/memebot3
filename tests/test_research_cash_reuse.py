"""Synthetic primary-quote reuse; no provider, order or operator artifacts."""
import asyncio
import copy
import datetime as dt
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from analytics import exit_policy
from execution import paper_cash_mark as cash
from quote_fixtures import SOL, TOKEN, v2_quote
from research_loop import entry_gate_forward as bank, runner_forward as runner, paired_forward as paired
from research_loop.paper_exit_receipt import make_intent
from test_entry_gate_forward import T0, config, capture, fill, case, quote, fx, isolated
from test_runner_forward import entry as runner_entry, cfg as runner_config
from test_paper_cash_mark import entry as paper_entry, position


@pytest.fixture(autouse=True)
def no_providers(monkeypatch, isolated):
    from trader import papertrading as paper
    from fetcher import jupiter_price
    from utils import sol_price
    failure = AsyncMock(side_effect=AssertionError("Synthetic reuse attempted a provider"))
    monkeypatch.setattr(paper.jupiter_router, "get_routing_quote", failure)
    monkeypatch.setattr(jupiter_price, "get_many_usd_prices", failure)
    monkeypatch.setattr(sol_price, "get_sol_usd_observation", failure)
    monkeypatch.setattr(paper, "get_sol_usd", failure)
    runner._ACTIVE_INDEX.clear()


def prepared(kind, root):
    if kind == "entry":
        cfg = config()
        identity = capture(root, cfg)
        assert fill(root, cfg, identity)
        record = case(root, identity)
        path = bank.directory(root) / "active" / f"{identity}.json"
    else:
        cfg = runner_config()
        record = runner.prepare_partial_case(runner_entry(token_address=TOKEN), cfg=cfg,
                                             now=T0 + dt.timedelta(minutes=1))
        path = runner._directory(root) / "active" / f"{record['case_id']}.json"
        runner._write(path, record)
    return cfg, record, path


def arms(kind, record):
    return record["arms"] if kind == "runner" else bank.cash_case(record)[0]["arms"]


def observer(kind):
    return runner if kind == "runner" else bank


def incoming(kind, record, stamp, *, quantity=None, output=None):
    terminal = next(iter(arms(kind, record).values()))
    quantity = terminal["subject"]["qty_lamports"] if quantity is None else quantity
    return quote(quantity=quantity, output=quantity * 100000 if output is None else output,
                 source=record["token"], now=stamp)


@pytest.mark.parametrize("kind", ["entry", "runner"])
@pytest.mark.parametrize("family", ["metis", "jupiterz", "dflow", "okx"])
def test_reuse_checks_each_own_cash_basis_without_spending_a_quote_slot(tmp_path, kind, family):
    cfg, record, path = prepared(kind, tmp_path)
    stamp = T0 + dt.timedelta(minutes=2)
    quantity = next(iter(arms(kind, record).values()))["subject"]["qty_lamports"]
    q = v2_quote(record["token"], SOL, quantity, quantity * 100000, now=stamp, family=family)
    budget = tmp_path / "data/research/paired_forward_budget.json"
    before_budget = budget.read_bytes() if budget.exists() else None
    assert observer(kind).observe_cash_quote(record["token"], q, fx(stamp), root=tmp_path, cfg=cfg,
        now=stamp, quote_started_at=stamp) == len(arms(kind, record))
    updated = bank.storage.read(path)
    owners = set()
    for terminal in arms(kind, updated).values():
        owners.add(terminal["cash_last_mark"]["basis"]["owner"])
        assert terminal["cash_observation_count"] == 1 and not terminal["fills"]
        assert terminal["cash_last_mark"]["basis"]["entry_notional_usd"] == 10.
        if terminal.get("intent"):
            assert terminal["intent"]["cash_valuation"]["current"] == terminal["cash_last_mark"]
    assert len(owners) == len(arms(kind, record))
    assert (budget.read_bytes() if budget.exists() else None) == before_budget


@pytest.mark.parametrize("kind", ["entry", "runner"])
@pytest.mark.parametrize("fault", ["quantity", "fx_scalar", "fx_stale", "pre_request", "no_start", "version", "owner", "pending"])
def test_unknown_or_incompatible_reuse_never_mutates_financial_state(tmp_path, kind, fault):
    cfg, record, path = prepared(kind, tmp_path)
    stamp = T0 + dt.timedelta(minutes=2)
    q, rate, started = incoming(kind, record, stamp), fx(stamp), stamp
    if fault == "quantity":
        q = incoming(kind, record, stamp, quantity=q.in_amount - 1)
    elif fault == "fx_scalar": rate = 100.
    elif fault == "fx_stale": rate = fx(stamp, received_at=stamp.timestamp() - 61)
    elif fault == "pre_request": started += dt.timedelta(seconds=1)
    elif fault == "no_start": started = None
    elif fault == "version": record["financial_policy_version"] = "unknown"
    elif fault == "owner":
        for terminal in arms(kind, record).values(): terminal["subject"]["paper_cash_owner"] = "case:" + "f" * 64
    else:
        for terminal in arms(kind, record).values():
            terminal["intent"] = make_intent(terminal["subject"], quantity=q.in_amount, reason="synthetic_exit", now=stamp)
    bank.storage.write(path, record)
    original = path.read_bytes()
    assert observer(kind).observe_cash_quote(record["token"], q, rate, root=tmp_path, cfg=cfg,
        now=stamp, quote_started_at=started) == 0
    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", ["entry", "runner"])
def test_original_reused_receipt_is_idempotent_and_not_renewed(tmp_path, kind):
    cfg, record, path = prepared(kind, tmp_path)
    stamp = T0 + dt.timedelta(minutes=2)
    q = incoming(kind, record, stamp)
    args = dict(root=tmp_path, cfg=cfg, now=stamp, quote_started_at=stamp)
    count = observer(kind).observe_cash_quote(record["token"], q, fx(stamp), **args)
    assert count == len(arms(kind, record))
    original = bank.storage.read(path)
    eligible = sum(not arm.get("intent") for arm in arms(kind, original).values())
    assert observer(kind).observe_cash_quote(record["token"], q, fx(stamp), **args) == eligible
    assert bank.storage.read(path) == original
    changed = incoming(kind, record, stamp, output=q.out_amount + 1)
    assert observer(kind).observe_cash_quote(record["token"], changed, fx(stamp), **args) == 0
    assert bank.storage.read(path) == original


def test_reused_primary_quote_creates_but_cannot_fill_a_new_entry_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(exit_policy, "CFG", replace(exit_policy.CFG, TP_PARTIAL_ENABLED=True,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=True, BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED=True))
    cfg, record, path = prepared("entry", tmp_path)
    stamp = T0 + dt.timedelta(minutes=2)
    q = incoming("entry", record, stamp, output=1000000000)
    assert bank.observe_cash_quote(record["token"], q, fx(stamp), root=tmp_path, cfg=cfg,
        now=stamp, quote_started_at=stamp) == 1
    terminal = bank.storage.read(path)["cash"]["terminal"]
    assert terminal["intent"]["reason"] == "partial_tp" and not terminal["fills"]
    quantity = terminal["intent"]["quantity"]
    assert 0 < quantity < q.in_amount
    assert bank.observe_cash_quote(record["token"], q, fx(stamp), root=tmp_path, cfg=cfg,
        now=stamp, quote_started_at=stamp) == 0
    assert bank.storage.read(path)["cash"]["terminal"] == terminal


def test_entry_reuse_cannot_use_a_changed_original_plan(tmp_path):
    cfg, record, path = prepared("entry", tmp_path)
    stamp = T0 + dt.timedelta(minutes=2)
    plan_path = bank.directory(tmp_path) / "plans" / f"{record['plan_id']}.json"
    planned = bank.storage.read(plan_path)
    planned["runner_exit_policy"] = "changed"
    bank.storage.write(plan_path, planned)
    original = path.read_bytes()
    assert bank.observe_cash_quote(record["token"], incoming("entry", record, stamp), fx(stamp),
        root=tmp_path, cfg=cfg, now=stamp, quote_started_at=stamp) == 0
    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", ["entry", "runner"])
def test_malformed_sibling_cannot_block_an_independent_valid_case(tmp_path, kind):
    cfg, record, path = prepared(kind, tmp_path)
    malformed = copy.deepcopy(record)
    malformed["case_id"] = "0" * 64
    if kind == "runner":
        malformed["arms"] = []
    else:
        malformed["plan_id"] = "../outside-the-plan-scope"
    bad_path = path.with_name(malformed["case_id"] + ".json")
    bank.storage.write(bad_path, malformed)
    original = bad_path.read_bytes()
    runner._ACTIVE_INDEX.clear()
    stamp = T0 + dt.timedelta(minutes=2)
    assert observer(kind).observe_cash_quote(record["token"], incoming(kind, record, stamp), fx(stamp),
        root=tmp_path, cfg=cfg, now=stamp, quote_started_at=stamp) == len(arms(kind, record))
    assert bad_path.read_bytes() == original
    assert all(arm["cash_observation_count"] == 1 for arm in arms(kind, bank.storage.read(path)).values())


@pytest.mark.parametrize("kind", ["entry", "runner"])
@pytest.mark.parametrize("return_pct", [300., 1000., 10000., 1000000.])
def test_reused_extreme_cash_is_uncapped_and_keeps_the_declared_tail(tmp_path, monkeypatch, kind, return_pct):
    monkeypatch.setattr(exit_policy, "CFG", replace(exit_policy.CFG, TP_PARTIAL_ENABLED=True,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=True, BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED=True,
        BIRD_MOONBAG_FRACTION=.15))
    cfg, record, path = prepared(kind, tmp_path)
    stamp = T0 + dt.timedelta(minutes=2)
    terminal = next(iter(arms(kind, record).values()))
    quantity = terminal["subject"]["qty_lamports"]
    q = incoming(kind, record, stamp, output=round(quantity * 100000 * (1 + return_pct / 100)))
    assert observer(kind).observe_cash_quote(record["token"], q, fx(stamp), root=tmp_path, cfg=cfg,
        now=stamp, quote_started_at=stamp) == len(arms(kind, record))
    updated = bank.storage.read(path)
    for terminal in arms(kind, updated).values():
        assert terminal["cash_last_mark"]["values"]["gross_remaining_return_pct"] == pytest.approx(return_pct)
        assert terminal["subject"]["highest_pnl_pct"] == pytest.approx(return_pct)
        requested = terminal["intent"]
        assert requested["reason"] == "partial_tp" and not terminal["fills"]
        tail = max(1, round(terminal["subject"]["entry_qty"] * .15))
        assert 0 < requested["quantity"] <= quantity - tail
    original = path.read_bytes()
    assert observer(kind).observe_cash_quote(record["token"], q, fx(stamp), root=tmp_path, cfg=cfg,
        now=stamp, quote_started_at=stamp) == 0
    assert path.read_bytes() == original  # A valuation never executes its new intent.


@pytest.mark.parametrize("fault", ["none", "secondary", "sql_generation", "await_generation"])
def test_actual_primary_cash_consumer_reuses_only_its_checked_original_quote(tmp_path, monkeypatch, fault):
    from trader import papertrading as paper
    cfg, record, path = prepared("entry", tmp_path)
    stamp = T0 + dt.timedelta(minutes=2)
    prefix = record["cash"]["prefix"]
    donor = paper_entry(token_address=record["token"], opened_at=prefix["opened_at"],
                        entry_route_quote=copy.deepcopy(prefix["entry_route_quote"]))
    monkeypatch.setattr(paper, "_PORTFOLIO", {record["token"]: donor})
    monkeypatch.setattr(paper, "_DATA_PATH", tmp_path / "data" / "paper_portfolio.json")
    monkeypatch.setattr(paper, "CFG", cfg)
    monkeypatch.setattr(paper, "utc_now", lambda: stamp)
    if fault == "secondary":
        def unavailable(*args, **kwargs): raise OSError("synthetic secondary storage failure")
        monkeypatch.setattr(runner, "observe_cash_quote", unavailable)
    expected = position(donor)
    expected.token_mint = expected.address = record["token"]
    if fault == "sql_generation": expected.qty -= 1
    original, original_bytes, calls = copy.deepcopy(donor), path.read_bytes(), []
    async def quoted(**kwargs):
        calls.append(kwargs)
        if fault == "await_generation": donor["qty_lamports"] -= 1
        return incoming("entry", record, stamp)
    async def rate(): return fx(stamp)
    mark = asyncio.run(paper.get_exit_cash_mark(record["token"], expected_position=expected,
        quote_func=quoted, fx_func=rate))
    if fault in {"sql_generation", "await_generation"}:
        assert mark is None and path.read_bytes() == original_bytes
        assert len(calls) == int(fault == "await_generation")
    else:
        assert mark is not None and len(calls) == 1
        terminal = bank.storage.read(path)["cash"]["terminal"]
        assert terminal["cash_observation_count"] == 1 and not terminal["fills"]
        assert terminal["cash_last_mark"]["basis"]["owner"] != mark.to_dict()["basis"]["owner"]
        assert paper._PORTFOLIO[record["token"]] == original
    assert not paper._DATA_PATH.exists() and not paper._SELL_LOCKS


def test_actual_paired_dispatch_has_no_unneeded_spot_call_and_carries_original_fx(tmp_path, monkeypatch):
    cfg = config(PAPER_RUNNER_RESEARCH_ENABLED=True)
    monkeypatch.setattr(paired.runner_forward, "active_tokens", lambda _: {"synthetic-mint"})
    monkeypatch.setattr(paired.entry_gate_forward, "active_tokens", lambda _: {"synthetic-mint"})
    original_fx = AsyncMock()
    seen = []
    async def component(**kwargs):
        seen.append(kwargs)
        assert await kwargs["prices_func"](["synthetic-mint"]) == {}
        assert kwargs["fx_func"] is original_fx
        return {"quote_calls": 0}
    monkeypatch.setattr(paired.runner_forward, "tick", component)
    monkeypatch.setattr(paired.entry_gate_forward, "tick", component)
    result = asyncio.run(paired.tick(root=tmp_path, cfg=cfg, now=T0, fx_func=original_fx))
    assert result["extra_quote_calls"] == 0 and len(seen) == 2
    original_fx.assert_not_awaited()
