"""Original-source generation checks; synthetic cohorts, never real profits."""
import asyncio
import copy
import datetime as dt
import os
from pathlib import Path

import pytest

from research_loop import entry_gate_forward as bank, entry_gate_policy as evaluator, forward_budget as store
from test_entry_gate_forward import T0, capture, case, config, fill, fx, isolated, quote, token  # noqa: F401
from test_paper_entry_policy import cohort, install, config as selection_config
from runtime import entry_gate_code as code, paper_entry_policy as binding


def source_change(monkeypatch, *, component="research_rank_canary.py", active=lambda: True):
    original = Path.read_bytes
    def read(path):
        value = original(path)
        if path.name == component and path.parent.name == "analytics" and active():
            return value + b"\n# SYNTHETIC_DIFFERENT_SOURCE_GENERATION\n"
        return value
    monkeypatch.setattr(Path, "read_bytes", read)


def test_original_registration_binds_plan_case_and_journal_to_gate_code(tmp_path):
    cfg = config()
    identity = capture(tmp_path, cfg)
    record = case(tmp_path, identity)
    base = bank.directory(tmp_path)
    plan = store.read(base / "plans" / f"{record['plan_id']}.json")
    journal = store.read(base / "journals" / f"{record['plan_id']}.json")
    original = plan.get("gate_code_identity")
    assert original and original["gate"] == "rank_canary"
    assert record.get("gate_code_identity") == original
    assert journal["events"][0].get("gate_code_reference") == code.reference(original, gate="rank_canary")


def test_original_cohort_cannot_be_approved_under_changed_gate_source(monkeypatch):
    cfg = selection_config()
    plan, cases, now = cohort(cfg)
    assert evaluator.compare_cohort(plan, cases, cfg, now=now)["accepted"]
    source_change(monkeypatch)
    assert not evaluator.compare_cohort(plan, cases, cfg, now=now)["accepted"]


def test_original_source_change_invalidates_a_warm_policy_cache(tmp_path, monkeypatch):
    cfg = selection_config()
    _, _, now = install(tmp_path, cfg)
    assert evaluator.load_selection(cfg, root=tmp_path, now=now, gate="rank_canary")
    source_change(monkeypatch)
    assert evaluator.load_selection(cfg, root=tmp_path, now=now+dt.timedelta(seconds=1), gate="rank_canary") is None


def test_cached_original_case_content_is_checked_even_with_same_size_and_mtime(tmp_path):
    cfg = selection_config()
    base, _, now = install(tmp_path, cfg)
    assert evaluator.load_selection(cfg, root=tmp_path, now=now, gate="rank_canary")
    path = next((base / "closed").glob("*.json"))
    metadata = path.stat()
    before = path.read_text(encoding="utf-8")
    after = before.replace('"observation_count": 1560', '"observation_count": 1561', 1)
    assert after != before and len(after.encode()) == len(before.encode())
    path.write_text(after, encoding="utf-8")
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    assert path.stat().st_size == metadata.st_size and path.stat().st_mtime_ns == metadata.st_mtime_ns
    assert evaluator.load_selection(cfg, root=tmp_path, now=now+dt.timedelta(seconds=1), gate="rank_canary") is None


def test_changed_entry_code_blocks_a_pending_virtual_fill_before_dispatch(tmp_path, monkeypatch):
    cfg = config()
    identity = capture(tmp_path, cfg)
    source_change(monkeypatch)
    assert not fill(tmp_path, cfg, identity)
    assert case(tmp_path, identity, "invalid").get("cash") is None


def test_entry_source_is_rechecked_after_the_actual_quote_await(tmp_path, monkeypatch):
    cfg = config()
    identity = capture(tmp_path, cfg)
    changed = [False]
    source_change(monkeypatch, active=lambda: changed[0])
    stamp = T0 + dt.timedelta(seconds=1)
    async def prices(tokens): return {mint: 1. for mint in tokens}
    async def sol(): return 100.
    async def rate(): return fx(stamp)
    async def quoted(**kwargs):
        changed[0] = True
        return quote(source=kwargs["input_mint"], target=kwargs["output_mint"], now=stamp)
    assert not asyncio.run(bank.fill_entry(identity, root=tmp_path, cfg=cfg, now=stamp,
        prices_func=prices, quote_func=quoted, sol_price_func=sol, fx_func=rate))
    assert case(tmp_path, identity, "invalid").get("cash") is None


def test_unrelated_gate_source_does_not_discard_a_valid_component(tmp_path, monkeypatch):
    cfg = selection_config()
    _, _, now = install(tmp_path, cfg)
    source_change(monkeypatch, component="moonshot_micro_lottery.py")
    assert evaluator.load_selection(cfg, root=tmp_path, now=now, gate="rank_canary")


def test_original_funded_case_can_still_close_when_entry_code_changes(tmp_path, monkeypatch):
    from research_loop.paper_exit_receipt import make_intent
    cfg = config()
    identity = capture(tmp_path, cfg)
    assert fill(tmp_path, cfg, identity)
    record = case(tmp_path, identity)
    quantity = record["cash"]["prefix"]["entry_qty"]
    stamp = T0 + dt.timedelta(minutes=2)
    record["cash"]["terminal"]["intent"] = make_intent(record["cash"]["terminal"]["subject"],
        quantity=quantity, reason="test_stop", now=stamp-dt.timedelta(seconds=1))
    store.write(bank.directory(tmp_path) / "active" / f"{identity}.json", record)
    source_change(monkeypatch)
    assert bank.observe_quote(record["token"], quote(quantity=quantity, output=200000000, now=stamp), 100.,
        root=tmp_path, cfg=cfg, now=stamp, quote_started_at=stamp, fx_observation=fx(stamp)) == 1
    assert case(tmp_path, identity, "closed")["cash"]["terminal"]["closed"] is True


@pytest.mark.parametrize("gate", code.LEAVES)
def test_snapshot_has_the_exact_bounded_component_and_common_sources(gate):
    identity = code.snapshot(gate)
    assert code.valid_identity(identity, gate=gate)
    assert code.matches_current(identity, gate=gate)
    assert [item["path"] for item in identity["sources"]] == list(code.COMMON + code.LEAVES[gate])
    assert all(not item["path"].startswith(("data/", "models/", "logs/", ".env")) for item in identity["sources"])


@pytest.mark.parametrize("gate", code.LEAVES)
@pytest.mark.parametrize("shared", ["analytics/token_time.py", "runtime/paper_entry_policy.py", "config/config.py"])
def test_shared_admission_source_change_invalidates_each_original_component(monkeypatch, gate, shared):
    identity = code.snapshot(gate)
    original = Path.read_bytes
    def read(path):
        value = original(path)
        return value + b"\n# SYNTHETIC_SHARED_CHANGE\n" if path.as_posix().endswith(shared) else value
    monkeypatch.setattr(Path, "read_bytes", read)
    assert code.valid_identity(identity, gate=gate)  # Still a historical source receipt.
    assert not code.matches_current(identity, gate=gate)


@pytest.mark.parametrize("fault", ["missing", "version", "gate", "digest", "extra", "sources",
                                   "duplicate", "path_escape", "boolean_hash", "extra_source_key"])
def test_malformed_original_source_receipts_are_not_proof(fault):
    identity = code.snapshot("rank_canary")
    if fault == "missing": identity.pop("sha256")
    elif fault == "version": identity["version"] = "invented"
    elif fault == "gate": identity["gate"] = "moonshot"
    elif fault == "digest": identity["sha256"] = "0" * 64
    elif fault == "extra": identity["accepted"] = True
    elif fault == "sources": identity["sources"] = "wrong"
    elif fault == "duplicate": identity["sources"][-1] = identity["sources"][0]
    elif fault == "path_escape": identity["sources"][-1]["path"] = "../.env"
    elif fault == "boolean_hash": identity["sources"][-1]["sha256"] = True
    elif fault == "extra_source_key": identity["sources"][-1]["secret"] = "synthetic"
    # Even a recomputed digest cannot authorize a different source inventory.
    if fault not in {"missing", "digest"}:
        identity["sha256"] = binding.digest({k: v for k, v in identity.items() if k != "sha256"})
    assert not code.valid_identity(identity, gate="rank_canary")
    assert not code.matches_current(identity, gate="rank_canary")


def test_normalized_source_identity_is_stable_between_lf_and_crlf(monkeypatch):
    before = code.snapshot("rank_canary")
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: original(path).replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert code.snapshot("rank_canary") == before


def test_no_original_source_is_backfilled_into_legacy_cohorts():
    cfg = selection_config()
    plan, cases, now = cohort(cfg)
    del plan["gate_code_identity"]
    before = copy.deepcopy([plan, cases])
    assert not evaluator.compare_cohort(plan, cases, cfg, now=now)["accepted"]
    assert [plan, cases] == before


@pytest.mark.parametrize("place", ["case", "journal", "prefix"])
def test_original_cohort_requires_the_same_source_at_every_registration_layer(place):
    cfg = selection_config()
    plan, cases, now = cohort(cfg)
    if place == "case": cases[0].pop("gate_code_identity")
    elif place == "journal": plan["enrollment_journal"][0].pop("gate_code_reference")
    else: cases[0]["cash"]["prefix"].pop("gate_code_identity")
    assert not evaluator.compare_cohort(plan, cases, cfg, now=now)["accepted"]


def test_changed_entry_source_cannot_extend_an_original_population(tmp_path, monkeypatch):
    cfg = config()
    identity = capture(tmp_path, cfg)
    plan_id = case(tmp_path, identity)["plan_id"]
    journal_path = bank.directory(tmp_path) / "journals" / f"{plan_id}.json"
    before = journal_path.read_bytes()
    source_change(monkeypatch)
    assert capture(tmp_path, cfg, token(2), now=T0 + dt.timedelta(minutes=16)) is None
    assert journal_path.read_bytes() == before


def test_source_changed_during_actual_gate_evaluation_never_registers_a_case(tmp_path, monkeypatch):
    changed = [False]
    source_change(monkeypatch, active=lambda: changed[0])
    original = evaluator.profile_decision
    def decision(*args, **kwargs):
        result = original(*args, **kwargs)
        changed[0] = True
        return result
    monkeypatch.setattr(evaluator, "profile_decision", decision)
    assert capture(tmp_path, config()) is None
    assert not list((bank.directory(tmp_path) / "journals").glob("*.json"))
    assert not list((bank.directory(tmp_path) / "active").glob("*.json"))


def test_source_changed_during_complete_cohort_replay_cannot_be_approved(monkeypatch):
    cfg = selection_config()
    plan, cases, now = cohort(cfg)
    changed = [False]
    source_change(monkeypatch, active=lambda: changed[0])
    original = evaluator.profile_decision
    def decision(*args, **kwargs):
        result = original(*args, **kwargs)
        changed[0] = True
        return result
    monkeypatch.setattr(evaluator, "profile_decision", decision)
    assert not evaluator.compare_cohort(plan, cases, cfg, now=now)["accepted"]


def test_changed_pending_source_is_rejected_without_provider_dispatch(tmp_path, monkeypatch):
    cfg = config()
    identity = capture(tmp_path, cfg)
    source_change(monkeypatch)
    async def forbidden(*args, **kwargs): pytest.fail("a changed original source dispatched a provider")
    assert not asyncio.run(bank.fill_entry(identity, root=tmp_path, cfg=cfg, now=T0 + dt.timedelta(seconds=1),
        prices_func=forbidden, quote_func=forbidden, sol_price_func=forbidden, fx_func=forbidden))


def test_production_selection_transports_original_source_in_v2_binding(tmp_path):
    cfg = selection_config()
    _, manifest, _ = install(tmp_path, cfg)
    from runtime.entry_decision import _validate_binding
    with evaluator.selected_scope(cfg, root=tmp_path):
        original = binding.snapshot()
        assert original["version"] == "paper_entry_thresholds_v2"
        assert original["gate_code_identity"] == manifest["gate_code_identity"]
        _validate_binding(original, paper=True)
        original["gate_code_identity"]["sources"][0]["sha256"] = "0" * 64
        assert binding.snapshot()["gate_code_identity"] == manifest["gate_code_identity"]
    assert binding.snapshot() is None


def test_historical_v2_receipt_survives_code_change_but_cannot_bind_anew(monkeypatch):
    cfg = selection_config()
    params = {"RESEARCH_RANK_CANARY_MIN_SCORE": 60}
    identity = code.snapshot("rank_canary")
    with binding.parameter_scope(cfg, params, revision="synthetic", gate_code_identity=identity):
        original = binding.snapshot()
    source_change(monkeypatch)
    from runtime.entry_decision import _validate_binding
    _validate_binding(original, paper=True)  # Original provenance, not a new approval.
    with pytest.raises(ValueError):
        with binding.parameter_scope(cfg, params, revision="synthetic", gate_code_identity=identity): pass
    with pytest.raises(ValueError): _validate_binding(original, paper=False)
    original["gate_code_identity"]["sha256"] = "0" * 64
    with pytest.raises(ValueError): _validate_binding(original, paper=True)


def test_valid_peer_keeps_v2_provenance_when_another_leaf_changes(tmp_path, monkeypatch):
    from test_paper_entry_policy import _install_rank_and_moonshot
    cfg, _, _, _, _ = _install_rank_and_moonshot(tmp_path)
    source_change(monkeypatch, component="moonshot_micro_lottery.py")
    selections = evaluator.load_selections(cfg, root=tmp_path)
    assert set(selections) == {"rank_canary"}
    with evaluator.selected_scope(cfg, root=tmp_path):
        assert binding.snapshot()["gate"] == "rank_canary"
        assert binding.snapshot()["version"] == "paper_entry_thresholds_v2"


def test_complete_composition_v2_keeps_independent_original_sources(tmp_path):
    from test_paper_entry_policy import _install_rank_and_moonshot
    from runtime.entry_decision import _validate_binding
    cfg, _, _, _, _ = _install_rank_and_moonshot(tmp_path)
    with evaluator.selected_scope(cfg, root=tmp_path):
        original = binding.snapshot()
        assert original["version"] == "paper_entry_composition_v2"
        assert set(original["components"]) == {"rank_canary", "moonshot"}
        _validate_binding(original, paper=True)
        for gate, component in original["components"].items():
            assert code.matches_current(component["gate_code_identity"], gate=gate)


def test_simulation_only_and_original_bound_components_cannot_be_mixed():
    cfg = selection_config()
    selections = {
        "rank_canary": {"parameters": {"RESEARCH_RANK_CANARY_MIN_SCORE": 60}, "revision": "synthetic",
                        "evidence_sha256": "", "gate_code_identity": code.snapshot("rank_canary")},
        "moonshot": {"parameters": {"MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M": cfg.MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M - 50},
                     "revision": "synthetic", "evidence_sha256": ""},
    }
    with pytest.raises(ValueError):
        with binding.composition_scope(cfg, selections): pass


def test_secondary_scope_never_swallows_an_error_from_the_primary_caller(tmp_path):
    cfg = selection_config()
    install(tmp_path, cfg)
    with pytest.raises(ValueError, match="synthetic primary error"):
        with evaluator.selected_scope(cfg, root=tmp_path):
            raise ValueError("synthetic primary error")
    assert binding.snapshot() is None


@pytest.mark.parametrize("target", ["case", "manifest", "gate_source"])
def test_sources_are_rechecked_after_complete_original_replay(tmp_path, monkeypatch, target):
    cfg = selection_config()
    base, manifest, now = install(tmp_path, cfg)
    evaluator._VERIFIED_CACHE.clear()
    changed = [False]
    if target == "gate_source": source_change(monkeypatch, active=lambda: changed[0])
    original = evaluator._replay_manifest
    def replay(*args, **kwargs):
        result = original(*args, **kwargs)
        if target == "case":
            path = next((base / "closed").glob("*.json"))
            record = store.read(path)
            record["observation_count"] += 1
            store.write(path, record)
        elif target == "manifest":
            path = evaluator.selection_path(tmp_path, "rank_canary")
            store.write(path, {**manifest, "revision": "0" * 20})
        else: changed[0] = True
        return result
    monkeypatch.setattr(evaluator, "_replay_manifest", replay)
    assert evaluator.load_selection(cfg, root=tmp_path, now=now, gate="rank_canary") is None


def test_source_read_failure_stays_unknown_without_reading_operator_artifacts(monkeypatch):
    identity = code.snapshot("rank_canary")
    original = Path.read_bytes
    reads = []
    def read(path):
        reads.append(path.relative_to(code.ROOT).as_posix())
        if path.name == "token_time.py": raise OSError("synthetic missing source")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", read)
    assert not code.matches_current(identity, gate="rank_canary")
    assert set(reads).issubset(set(code.COMMON + code.LEAVES["rank_canary"]))


def test_selector_rechecks_source_between_loading_and_scope_binding(tmp_path, monkeypatch):
    cfg = selection_config()
    _, _, now = install(tmp_path, cfg)
    selected = evaluator.load_selections(cfg, root=tmp_path, now=now)
    assert "rank_canary" in selected
    source_change(monkeypatch)
    monkeypatch.setattr(evaluator, "load_selections", lambda *args, **kwargs: selected)
    with evaluator.selected_scope(cfg, root=tmp_path):
        assert binding.entry_config(cfg) is cfg and binding.snapshot() is None
