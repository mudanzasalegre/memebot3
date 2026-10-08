"""Crash/disk/source-gap tests with isolated files and mocked paper fills only."""
from __future__ import annotations

import copy
import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

from analytics import runner_price_policy
from research_loop import runner_forward as rf
from runtime import runner_enrollment as intake
from runtime.paper_archive import read_closed_evidence
from utils.atomic_json import read_json_strict, write_json_atomic
from test_runner_forward import T0, MINT, cfg, entry, closed_cohort, isolated_exit_policy
from test_paper_archive import paper


@pytest.fixture(autouse=True)
def reset_intake(monkeypatch):
    monkeypatch.setattr(intake, "_REPAIR_STATE", {})


def source(**changes):
    return intake.capture_source(entry(**changes), captured_at=T0 + dt.timedelta(minutes=1))


def active(root):
    return read_json_strict(next((rf._directory(root) / "active").glob("*.json")))


@pytest.fixture
def funded(paper, monkeypatch):
    clock = [T0]
    monkeypatch.setattr(paper, "CFG", replace(paper.CFG, DRY_RUN=True,
        PAPER_RUNNER_RESEARCH_ENABLED=True, PAPER_RUNNER_RESEARCH_AUTO_APPLY=False,
        RUNNER_PRICE_TRAILING_PAPER_ENABLED=True))
    monkeypatch.setattr(paper, "utc_now", lambda: clock[0])
    monkeypatch.setattr(rf, "_now", lambda: clock[0])
    monkeypatch.setattr(paper, "runtime_context_payload", lambda: {
        "run_id": "isolated-paper", "run_started_at": T0.isoformat()})
    return paper, clock


def fail_write(*args, **kwargs):
    raise OSError("synthetic secondary disk failure")


def test_source_detaches_first_partial_and_filters_arbitrary_private_payload():
    original = entry(private_key="not-a-real-secret", first_partial_exit_intent_id="c" * 32)
    original["entry_route_quote"]["authorization"] = "not-a-real-secret"
    frozen = intake.capture_source(original, captured_at=T0 + dt.timedelta(minutes=1))
    original["qty_lamports"] = 1
    original["highest_pnl_pct"] = 50000
    assert frozen["prefix"]["qty_lamports"] == 800 and frozen["prefix"]["highest_pnl_pct"] == 100
    assert "not-a-real-secret" not in json.dumps(frozen)


@pytest.mark.parametrize("mutation", ["checksum", "clock", "partial", "live", "lineage", "signature"])
def test_corrupt_or_noncausal_source_is_not_written(tmp_path, mutation):
    record = source(entry_intent_id="a" * 32, buy_signature="SIM-" + "a" * 32)
    if mutation == "checksum": record["prefix"]["highest_pnl_pct"] = 999
    else:
        if mutation == "clock": record["captured_at"] = (T0 - dt.timedelta(seconds=1)).isoformat()
        elif mutation == "partial": record["prefix"]["partial_fill_events"] = 2
        elif mutation == "live": record["prefix"]["dry_run"] = False
        elif mutation == "lineage": record["prefix"]["source_position_key"] = "buy:" + "b" * 32
        elif mutation == "signature": record["prefix"]["buy_signature"] = "LIVE"
        payload = {"captured_at": record["captured_at"], "prefix": record["prefix"]}
        record["payload_sha256"] = rf._hash(payload)
    with pytest.raises(intake.RunnerEnrollmentError):
        intake.register_source(record, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("delay", [0, 300, 301, 600])
def test_recovery_keeps_original_cohort_prefix_and_marks_missing_observations(tmp_path, delay):
    frozen = source()
    captured = T0 + dt.timedelta(minutes=1)
    assert intake.register_source(frozen, root=tmp_path, cfg=cfg(), now=captured + dt.timedelta(seconds=delay))["created"]
    case = active(tmp_path)
    assert case["registered_at"] == captured.isoformat() and case["cohort_started_at"] == T0.isoformat()
    assert case["prefix"]["qty_lamports"] == 800
    assert case["observation_count"] == 0 and case["last_observed_at"] == captured.isoformat()
    assert case["observation_gap_limit_exceeded"] is (delay > 300)
    assert case["enrollment_delay_seconds"] == delay


def test_recovery_after_settlement_deadline_is_invalid_without_quote_or_fill(tmp_path):
    frozen = source()
    result = intake.register_source(frozen, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(days=4))
    assert result["status"] == "invalid"
    case = read_json_strict(next((rf._directory(tmp_path) / "invalid").glob("*.json")))
    assert case["invalid_reason"] == "first_partial_recovery_after_settlement_deadline"
    assert all(not arm["fills"] and not arm["closed"] for arm in case["arms"].values())
    assert not list((rf._directory(tmp_path) / "active").glob("*.json"))


def test_same_mint_time_distinct_entry_intents_create_distinct_cases(tmp_path):
    for identity in ("a" * 32, "b" * 32):
        record = source(entry_intent_id=identity, buy_signature="SIM-" + identity)
        assert intake.register_source(record, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))["created"]
    cases = [read_json_strict(path) for path in (rf._directory(tmp_path) / "active").glob("*.json")]
    assert len(cases) == 2 and len({case["case_id"] for case in cases}) == 2


def test_missing_case_after_receipt_is_not_silently_acknowledged(tmp_path):
    frozen = source()
    intake.register_source(frozen, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    path = next((rf._directory(tmp_path) / "active").glob("*.json"))
    path.unlink()
    with pytest.raises(intake.RunnerEnrollmentError, match="missing"):
        intake.register_source(frozen, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=2))
    assert not path.exists()  # No invented reconstruction of already observed history.


def test_case_write_failure_keeps_intake_then_recovers_once(tmp_path, monkeypatch):
    frozen = source()
    writer = rf._write
    monkeypatch.setattr(rf, "_write", fail_write)
    with pytest.raises(intake.RunnerEnrollmentError):
        intake.register_source(frozen, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    assert read_json_strict(next((rf._directory(tmp_path) / "enrollment_sources").glob("*.json"))) == frozen
    monkeypatch.setattr(rf, "_write", writer)
    assert intake.register_source(frozen, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=2))["created"]
    case_path = next((rf._directory(tmp_path) / "active").glob("*.json"))
    before = case_path.read_bytes()
    assert not intake.register_source(frozen, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=3))["created"]
    assert case_path.read_bytes() == before and len(list(case_path.parent.glob("*.json"))) == 1


def test_receipt_failure_does_not_rewind_observed_case_when_repaired(tmp_path, monkeypatch):
    writer, frozen = intake.write_json_atomic, source()
    def receipt_failure(path, payload):
        if path.parent.name == "enrollment_receipts": fail_write()
        return writer(path, payload)
    monkeypatch.setattr(intake, "write_json_atomic", receipt_failure)
    with pytest.raises(intake.RunnerEnrollmentError):
        intake.register_source(frozen, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    rf.observe_market(MINT, 1.1, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=2))
    path = next((rf._directory(tmp_path) / "active").glob("*.json"))
    before = path.read_bytes()
    monkeypatch.setattr(intake, "write_json_atomic", writer)
    result = intake.register_source(frozen, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=3))
    assert not result["created"] and path.read_bytes() == before
    assert len(list((rf._directory(tmp_path) / "enrollment_receipts").glob("*.json"))) == 1


def test_capacity_is_pending_not_trade_veto_and_gap_remains_explicit(tmp_path, monkeypatch):
    frozen = source()
    monkeypatch.setattr(rf, "MAX_ACTIVE", 0)
    assert intake.register_source(frozen, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))["status"] == "pending"
    assert len(list((rf._directory(tmp_path) / "enrollment_sources").glob("*.json"))) == 1
    monkeypatch.setattr(rf, "MAX_ACTIVE", 128)
    assert intake.register_source(frozen, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=2))["created"]
    case = active(tmp_path)
    assert not intake.population_matches(tmp_path, case["cohort_id"], [case])


@pytest.mark.parametrize("enabled,dry", [(False, True), (True, False)])
def test_disabled_or_live_maintenance_never_writes(tmp_path, enabled, dry):
    result = intake.repair_sources(root=tmp_path, portfolio={MINT: {"runner_research_source": source()}},
        cfg=cfg(PAPER_RUNNER_RESEARCH_ENABLED=enabled, DRY_RUN=dry), force=True)
    assert result["status"] == "disabled" and not list(tmp_path.rglob("*.json"))


def test_empty_first_launch_is_read_only_and_failed_retry_is_bounded_rotating(tmp_path, monkeypatch):
    assert intake.repair_sources(root=tmp_path, portfolio={}, cfg=cfg(), force=True)["attempted"] == 0
    assert not list(tmp_path.iterdir())
    portfolio = {str(i): {"runner_research_source": source(entry_intent_id=f"{i:032x}",
        buy_signature="SIM-" + f"{i:032x}")} for i in range(10)}
    attempted = []
    def fail(record, **kwargs):
        attempted.append(record["prefix"]["entry_intent_id"])
        raise intake.RunnerEnrollmentError("synthetic")
    monkeypatch.setattr(intake, "register_source", fail)
    first = intake.repair_sources(root=tmp_path, portfolio=portfolio, cfg=cfg(), force=True, limit=2)
    assert first == {"status": "pending", "attempted": 2, "failed": 2}
    assert intake.repair_sources(root=tmp_path, portfolio=portfolio, cfg=cfg())["status"] == "throttled"
    intake.repair_sources(root=tmp_path, portfolio=portfolio, cfg=cfg(), force=True, limit=2)
    assert attempted == [f"{i:032x}" for i in range(4)]


@pytest.mark.asyncio
async def test_primary_disk_failure_never_creates_research_for_an_unfilled_partial(funded, monkeypatch):
    paper, clock = funded
    await paper.buy(MINT, .1, entry_intent_id="a" * 32)
    before = paper._DATA_PATH.read_bytes()
    clock[0] += dt.timedelta(minutes=1)
    monkeypatch.setattr(paper, "write_json_atomic", fail_write)
    with pytest.raises(paper.PaperPortfolioError): await paper.sell(MINT, 200)
    assert paper._DATA_PATH.read_bytes() == before and paper._PORTFOLIO[MINT]["qty_lamports"] == 1000
    assert "runner_research_source" not in paper._PORTFOLIO[MINT]
    assert not list((rf._directory(paper._research_root()) / "enrollment_sources").glob("*.json"))


@pytest.mark.asyncio
async def test_original_source_survives_later_partials_close_restart_and_same_mint_reentry(funded, monkeypatch):
    paper, clock = funded
    await paper.buy(MINT, .1, entry_intent_id="a" * 32)
    clock[0] += dt.timedelta(minutes=1)
    writer = intake.write_json_atomic
    monkeypatch.setattr(intake, "write_json_atomic", fail_write)
    first = await paper.sell(MINT, 200, exit_intent_id="c" * 32)
    assert first["ok"] and first["qty_left"] == 800
    frozen = copy.deepcopy(paper._PORTFOLIO[MINT]["runner_research_source"])
    durable = read_json_strict(paper._DATA_PATH)[MINT]
    assert durable["runner_research_source"] == frozen
    clock[0] += dt.timedelta(minutes=1)
    paper._PORTFOLIO[MINT]["highest_pnl_pct"] = 50000
    await paper.sell(MINT, 200)
    assert paper._PORTFOLIO[MINT]["runner_research_source"] == frozen
    await paper.sell(MINT, 600)
    archived, issues = read_closed_evidence(paper._DATA_PATH.parent)
    assert not issues and archived[0]["runner_research_source"] == frozen
    await paper.buy(MINT, .1, entry_intent_id="b" * 32)
    assert "runner_research_source" not in paper._PORTFOLIO[MINT]
    # Simulated restart: reload the actual isolated portfolio; only the archive retains the old source.
    paper._PORTFOLIO = paper.load_portfolio()
    rf._ACTIVE_INDEX.clear()
    clock[0] += dt.timedelta(minutes=10)
    monkeypatch.setattr(intake, "write_json_atomic", writer)
    quote_calls = paper.jupiter_router.get_routing_quote.await_count
    result = await paper.repair_runner_research(force=True)
    assert result["attempted"] == 1 and result["failed"] == 0
    case = active(paper._research_root())
    assert case["registered_at"] == frozen["captured_at"] and case["prefix"]["qty_lamports"] == 800
    assert case["prefix"]["highest_pnl_pct"] != 50000 and case["observation_gap_limit_exceeded"]
    assert all(arm["subject"]["qty_lamports"] == 800 and not arm["fills"] for arm in case["arms"].values())
    assert paper.jupiter_router.get_routing_quote.await_count == quote_calls == 3
    assert (await paper.repair_runner_research(force=True))["attempted"] == 0


@pytest.mark.asyncio
async def test_capture_failure_is_durable_without_invalidating_fill_or_poisoning_future_days(funded, monkeypatch):
    paper, clock = funded
    await paper.buy(MINT, .1, entry_intent_id="a" * 32)
    clock[0] += dt.timedelta(minutes=1)
    def failure(*args, **kwargs): raise RuntimeError("synthetic pure capture error")
    monkeypatch.setattr(intake, "capture_source", failure)
    result = await paper.sell(MINT, 200)
    assert result["ok"] and result["qty_left"] == 800
    persisted = read_json_strict(paper._DATA_PATH)[MINT]
    gap = persisted["runner_research_capture_failed"]
    policy = runner_price_policy.parse_policy(gap["runner_trailing_policy"])
    cohort = T0.strftime("%Y%m%d") + "_" + rf._policy_id(policy)
    assert intake._capture_gap_applies(persisted, cohort)
    later_cohort = (T0 + dt.timedelta(days=1)).strftime("%Y%m%d") + "_" + rf._policy_id(policy)
    assert not intake._capture_gap_applies(persisted, later_cohort)


def test_known_capture_gap_blocks_its_day_but_allows_a_new_complete_population(tmp_path):
    next_day = T0 + dt.timedelta(days=1)
    cases = closed_cohort(tmp_path, start=next_day)
    gap = {"captured_at": (T0 + dt.timedelta(minutes=1)).isoformat(),
        "runner_trailing_policy": runner_price_policy.freeze_policy(cfg(), dry_run=True)}
    write_json_atomic(tmp_path / "data" / "paper_portfolio.json", {MINT: {"runner_research_capture_failed": gap}})
    assert intake.population_matches(tmp_path, cases[0]["cohort_id"], cases)
    assert rf.evaluate_completed_cohorts(root=tmp_path, cfg=cfg(), now=next_day + dt.timedelta(hours=27))["status"] == "selected"


def test_disabled_research_does_not_consume_a_previously_checked_selection(tmp_path):
    closed_cohort(tmp_path)
    stamp = T0 + dt.timedelta(hours=27)
    assert rf.evaluate_completed_cohorts(root=tmp_path, cfg=cfg(), now=stamp)["status"] == "selected"
    assert runner_price_policy.parse_policy(rf.entry_policy(cfg(), root=tmp_path, now=stamp))["max_price_drawdown_pct"] == 15
    disabled = cfg(PAPER_RUNNER_RESEARCH_ENABLED=False)
    assert runner_price_policy.parse_policy(rf.entry_policy(disabled, root=tmp_path, now=stamp))["max_price_drawdown_pct"] == 20
    manifest = rf._directory(tmp_path) / "active_policy.json"
    before = manifest.read_bytes()
    assert rf.evaluate_completed_cohorts(root=tmp_path, cfg=disabled, now=stamp)["status"] == "disabled"
    assert manifest.read_bytes() == before


@pytest.mark.parametrize("field,value", [("qty_lamports", 800.5), ("realized_qty", True),
    ("entry_qty", "1000"), ("execution_fill_count", True), ("execution_fill_count", 3)])
def test_unchecked_raw_quantity_or_extra_fill_count_is_not_a_first_partial_case(field, value):
    assert rf.prepare_partial_case(entry(**{field: value}), cfg=cfg(), now=T0 + dt.timedelta(minutes=1)) is None


@pytest.mark.parametrize("mutation", ["source", "receipt", "case"])
def test_acknowledged_source_corruption_is_preserved_and_reported_pending(tmp_path, monkeypatch, mutation):
    frozen = source()
    monkeypatch.setattr(rf, "_now", lambda: T0 + dt.timedelta(minutes=2))
    intake.register_source(frozen, root=tmp_path, cfg=cfg(), now=T0 + dt.timedelta(minutes=1))
    directory = rf._directory(tmp_path)
    folder = {"source": "enrollment_sources", "receipt": "enrollment_receipts", "case": "active"}[mutation]
    path = next((directory / folder).glob("*.json"))
    path.write_text('{"torn":')
    result = intake.repair_sources(root=tmp_path, portfolio={MINT: {"runner_research_source": frozen}}, cfg=cfg(), force=True)
    assert result["status"] == "pending" and result["failed"] >= 1
    assert path.read_text() == '{"torn":'


def test_unreadable_source_directory_is_not_an_empty_complete_population(tmp_path, monkeypatch):
    cases = closed_cohort(tmp_path)
    write_json_atomic(tmp_path / "data" / "paper_portfolio.json", [])
    assert not intake.population_matches(tmp_path, cases[0]["cohort_id"], cases)
    write_json_atomic(tmp_path / "data" / "paper_portfolio.json", {})
    original = Path.iterdir
    blocked = tmp_path / "data" / "paper_closed_trades"
    def failure(path):
        if path == blocked: raise PermissionError("synthetic unreadable source directory")
        return original(path)
    monkeypatch.setattr(Path, "iterdir", failure)
    assert not intake.population_matches(tmp_path, cases[0]["cohort_id"], cases)
    result = intake.repair_sources(root=tmp_path, portfolio={}, cfg=cfg(), force=True)
    assert result == {"status": "pending", "attempted": 0, "failed": 1, "source_scan_failed": True}
    assert intake.repair_sources(root=tmp_path, portfolio={}, cfg=cfg())["status"] == "throttled"


@pytest.mark.parametrize("after_selection", [False, True])
def test_missing_captured_trade_blocks_selection_and_invalidates_cached_manifest(tmp_path, after_selection):
    cases = closed_cohort(tmp_path)
    stamp = T0 + dt.timedelta(hours=27)
    assert rf.compare_cohort(cases, now=stamp)["accepted"]
    if after_selection:
        assert rf.evaluate_completed_cohorts(root=tmp_path, cfg=cfg(), now=stamp)["status"] == "selected"
        assert runner_price_policy.parse_policy(rf.entry_policy(cfg(), root=tmp_path, now=stamp))["max_price_drawdown_pct"] == 15
    import base58
    mint = base58.b58encode((1001).to_bytes(32, "big")).decode()
    missing = source(token_address=mint)
    write_json_atomic(tmp_path / "data" / "paper_portfolio.json", {mint: {"runner_research_source": missing}})
    assert not intake.population_matches(tmp_path, cases[0]["cohort_id"], cases)
    assert rf.evaluate_completed_cohorts(root=tmp_path, cfg=cfg(), now=stamp)["status"] == "awaiting_comparable_evidence"
    assert runner_price_policy.parse_policy(rf.entry_policy(cfg(), root=tmp_path, now=stamp))["max_price_drawdown_pct"] == 20
    report = read_json_strict(rf._directory(tmp_path) / "evaluations" / (cases[0]["cohort_id"] + ".json"))
    assert "incomplete_first_partial_population" in report["reasons"]
