"""Bounded loaded Python agreement; no frame, whole-process or profit proof."""
from __future__ import annotations

import functools
import importlib
import copy
import hashlib
import json
from pathlib import Path
from types import FunctionType

import pytest

from runtime import entry_gate_code as code, loaded_gate_code as loaded
from research_loop import entry_gate_forward as bank, forward_budget as storage
from test_entry_gate_forward import T0, capture, config, token, isolated  # noqa: F401


ENTRY_POINTS = {
    "rank_canary": ("analytics.research_rank_canary", "evaluate_research_rank_canary"),
    "sniper_subprofile": ("analytics.sniper_research_subprofiles", "evaluate_sniper_research_subprofile"),
    "late_momentum": ("analytics.late_momentum_watch", "evaluate_late_momentum_watch"),
    "moonshot": ("analytics.moonshot_micro_lottery", "evaluate_moonshot_micro_lottery"),
}


@pytest.mark.parametrize("gate", ENTRY_POINTS)
def test_changed_loaded_entry_cannot_borrow_unchanged_declared_source(gate, monkeypatch):
    module_name, name = ENTRY_POINTS[gate]
    module = importlib.import_module(module_name)
    original_identity = code.snapshot(gate)
    original = getattr(module, name)
    @functools.wraps(original)
    def replaced(*args, **kwargs):
        return original(*args, **kwargs)
    monkeypatch.setattr(module, name, replaced)
    assert code.valid_identity(original_identity, gate=gate)
    assert not code.matches_current(original_identity, gate=gate)
    with pytest.raises(ValueError):
        code.snapshot(gate)


@pytest.mark.parametrize("gate", ENTRY_POINTS)
def test_same_code_with_a_foreign_globals_mapping_is_not_original_loaded_binding(gate, monkeypatch):
    module_name, name = ENTRY_POINTS[gate]
    module = importlib.import_module(module_name)
    original_identity = code.snapshot(gate)
    original = getattr(module, name)
    injected = FunctionType(original.__code__, dict(original.__globals__), original.__name__, original.__defaults__, original.__closure__)
    injected.__qualname__ = original.__qualname__
    injected.__kwdefaults__ = original.__kwdefaults__
    monkeypatch.setattr(module, name, injected)
    assert not code.matches_current(original_identity, gate=gate)


def test_unrelated_loaded_component_does_not_disable_rank_canary(monkeypatch):
    from analytics import moonshot_micro_lottery as moonshot
    original_identity = code.snapshot("rank_canary")
    monkeypatch.setattr(moonshot, "evaluate_moonshot_micro_lottery", lambda *args, **kwargs: None)
    assert code.matches_current(original_identity, gate="rank_canary")


def test_loaded_import_alias_cannot_hide_a_changed_helper(monkeypatch):
    from analytics import research_rank_canary as canary
    original_identity = code.snapshot("rank_canary")
    original = canary.fnum
    @functools.wraps(original)
    def changed(*args, **kwargs):
        return original(*args, **kwargs)
    monkeypatch.setattr(canary, "fnum", changed)
    assert not code.matches_current(original_identity, gate="rank_canary")


def test_stale_import_alias_cannot_hide_a_changed_owner_export(monkeypatch):
    from analytics import report_utils, research_rank_canary as canary
    identity = code.snapshot("rank_canary")
    original = report_utils.fnum
    @functools.wraps(original)
    def changed(*args, **kwargs):
        return original(*args, **kwargs)
    monkeypatch.setattr(report_utils, "fnum", changed)
    assert canary.fnum is original
    assert not code.matches_current(identity, gate="rank_canary")


@pytest.mark.parametrize("kind", ["import", "shared_global"])
def test_foreign_helper_cannot_disappear_from_a_fresh_loaded_graph(kind, monkeypatch):
    from analytics import research_rank_canary as canary
    from research_loop import entry_gate_forward as bank
    identity = code.snapshot("rank_canary")
    module, name = (canary, "fnum") if kind == "import" else (bank, "fill_entry")
    monkeypatch.setattr(module, name, lambda *args, **kwargs: None)
    assert not code.matches_current(identity, gate="rank_canary")
    with pytest.raises(ValueError):
        code.snapshot("rank_canary")


@pytest.mark.parametrize("gate", ENTRY_POINTS)
def test_same_function_identity_with_replaced_immutable_code_cannot_use_warm_cache(gate, monkeypatch):
    module_name, name = ENTRY_POINTS[gate]
    original = getattr(importlib.import_module(module_name), name)
    identity = code.snapshot(gate)
    assert code.matches_current(identity, gate=gate)
    def changed(*args, **kwargs):
        return None
    monkeypatch.setattr(original, "__code__", changed.__code__)
    assert not code.matches_current(identity, gate=gate)


def test_changed_source_body_cannot_claim_the_old_loaded_body(monkeypatch):
    identity = code.snapshot("rank_canary")
    read = Path.read_bytes
    def changed(path):
        content = read(path)
        if path.name == "research_rank_canary.py":
            content = content.replace(b"def normalize_score(", b"def changed_score(", 1)
        return content
    monkeypatch.setattr(Path, "read_bytes", changed)
    assert not code.matches_current(identity, gate="rank_canary")
    with pytest.raises(ValueError):
        code.snapshot("rank_canary")


@pytest.mark.parametrize("relative,name", [
    (relative, name) for relative, names in loaded.CORE.items() for name in names
])
def test_checked_shared_loaded_adaptation_and_collector_bindings(relative, name, monkeypatch):
    identity = code.snapshot("rank_canary")
    module = importlib.import_module(relative[:-3].replace("/", "."))
    original = getattr(module, name)
    @functools.wraps(original)
    def changed(*args, **kwargs):
        return original(*args, **kwargs)
    monkeypatch.setattr(module, name, changed)
    assert not code.matches_current(identity, gate="rank_canary")


def test_best_effort_wrapper_cannot_hide_a_changed_capture_closure(monkeypatch):
    from research_loop import entry_gate_forward as bank
    identity = code.snapshot("rank_canary")
    wrapper = bank.active_tokens
    cells = dict(zip(wrapper.__code__.co_freevars, wrapper.__closure__))
    original = cells["function"].cell_contents
    def changed(*args, **kwargs):
        return set()
    monkeypatch.setattr(cells["function"], "cell_contents", changed)
    assert not code.matches_current(identity, gate="rank_canary")
    assert cells["function"].cell_contents is not original


def test_exact_stdlib_contextmanager_and_real_decorated_closures_are_valid():
    identity = code.snapshot("rank_canary")
    rows = identity["loaded"]["bindings"]
    assert any(row["wrapper"] and row["wrapper"]["kind"] == "stdlib_contextmanager" for row in rows)
    assert any(".__closure__." in row["name"] and row["qualname"] == "<lambda>" for row in rows)
    assert code.valid_identity(code.freeze(identity), gate="rank_canary")
    assert code.matches_current(code.freeze(identity), gate="rank_canary")


def test_forged_stdlib_wrapped_target_is_not_trusted(monkeypatch):
    from runtime import paper_entry_policy as policy
    identity = code.snapshot("rank_canary")
    monkeypatch.setattr(policy.baseline_scope, "__wrapped__", lambda: None)
    assert not code.matches_current(identity, gate="rank_canary")


@pytest.mark.parametrize("fault", ["missing", "runtime", "row_key", "owner", "name", "qualname", "hash", "wrapper", "duplicate", "order", "required", "empty"])
def test_rehashed_incomplete_or_malformed_loaded_receipts_are_not_original_proof(fault):
    identity = copy.deepcopy(code.snapshot("rank_canary"))
    value = identity["loaded"]
    rows = value["bindings"]
    if fault == "missing": value.pop("runtime")
    elif fault == "runtime": value["runtime"]["optimize"] = True
    elif fault == "row_key": rows[0]["secret"] = "synthetic"
    elif fault == "owner": rows[0]["owner"] = "../.env"
    elif fault == "name": rows[0]["name"] = "bad/name"
    elif fault == "qualname": rows[0]["qualname"] = "bad:name"
    elif fault == "hash": rows[0]["code_sha256"] = False
    elif fault == "wrapper": rows[0]["wrapper"] = {"kind": "unknown", "code_sha256": "0"*64, "source_sha256": "0"*64}
    elif fault == "duplicate": rows.insert(0, copy.deepcopy(rows[0]))
    elif fault == "order": rows.reverse()
    elif fault == "required": rows[:] = [row for row in rows if (row["path"], row["name"]) != loaded.ENTRY["rank_canary"]]
    elif fault == "empty": rows.clear()
    value["sha256"] = loaded._digest({key: item for key, item in value.items() if key != "sha256"})
    identity["sha256"] = code._digest({key: item for key, item in identity.items() if key != "sha256"})
    assert not code.valid_identity(identity, gate="rank_canary")


def test_legacy_source_only_receipt_remains_historical_but_cannot_bind_current():
    identity = code.snapshot("rank_canary")
    legacy = {"version": code.LEGACY_VERSION, "gate": "rank_canary",
        "sources": [row for row in identity["sources"] if row["path"] != "runtime/loaded_gate_code.py"]}
    legacy["sha256"] = code._digest(legacy)
    assert code.valid_identity(legacy, gate="rank_canary")
    assert not code.matches_current(legacy, gate="rank_canary")


def test_native_json_digest_matches_detached_legacy_encoding_for_frozen_nested_values():
    frozen = code.freeze({"nested": [{"string": "synthetic", "integer": 0, "flag": True, "none": None,
        "number": 0.1, "values": (1, 2, 3)}]})
    expected = hashlib.sha256(json.dumps(code._plain(frozen), sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert code._digest(frozen) == expected
    with pytest.raises(ValueError):
        code._digest({"value": float("nan")})


def test_immutable_code_cache_is_bounded_and_not_a_function_identity_cache():
    original = compile("1 + 1", "synthetic.py", "eval")
    before = loaded.code_digest(original)
    assert loaded.code_digest(original) == before
    assert loaded.code_digest(original.replace(co_consts=(3,))) != before
    for index in range(4110):
        loaded.code_digest(original.replace(co_consts=(index,)))
    assert len(loaded._CODE_CACHE) == 4096
    assert id(original) not in loaded._CODE_CACHE
    assert loaded.code_digest(original) == before
    assert len(loaded._CODE_CACHE) == 4096


@pytest.mark.parametrize("module", [None, True, "../.env"])
def test_malformed_loaded_owner_does_not_raise_an_unclassified_error(module, monkeypatch):
    from analytics import research_rank_canary as canary
    identity = code.snapshot("rank_canary")
    monkeypatch.setattr(canary.evaluate_research_rank_canary, "__module__", module)
    assert not code.matches_current(identity, gate="rank_canary")
    with pytest.raises(ValueError):
        code.snapshot("rank_canary")


def test_complete_native_maximum_cohort_journal_remains_readable(tmp_path):
    import datetime as dt
    cfg = config(PAPER_ENTRY_RESEARCH_SAMPLE_S=600)
    for index in range(128):
        identity = capture(tmp_path, cfg, token(index + 1), now=T0 + dt.timedelta(seconds=600 * index))
        assert identity, index
    base = bank.directory(tmp_path)
    pointer = storage.read(base / "open_plan.json")
    plan = storage.read(base / "plans" / f"{pointer['plan_id']}.json")
    path = base / "journals" / f"{pointer['plan_id']}.json"
    journal = storage.read(path)
    assert journal is not None and len(journal["events"]) == 128
    assert path.stat().st_size < 2 * 1024 * 1024
    expected = code.reference(plan["gate_code_identity"], gate="rank_canary")
    assert all(event["gate_code_reference"] == expected for event in journal["events"])
    assert all("gate_code_identity" not in event for event in journal["events"])
    assert len(list((base / "active").glob("*.json"))) == 128
    print(json.dumps({"native_registered_cases": 128, "journal_bytes": path.stat().st_size,
        "reader_limit_bytes": 2 * 1024 * 1024, "readable": True, "providers_called": 0}))


@pytest.mark.parametrize("fault", ["version", "gate", "hash", "extra"])
def test_rehashed_journal_reference_cannot_substitute_original_plan_source(fault):
    from test_paper_entry_policy import cohort, config as selection_config
    from research_loop import entry_gate_policy as policy
    cfg = selection_config()
    plan, cases, now = cohort(cfg)
    reference = plan["enrollment_journal"][0]["gate_code_reference"]
    if fault == "version": reference["version"] = "wrong"
    elif fault == "gate": reference["gate"] = "moonshot"
    elif fault == "hash": reference["sha256"] = "0" * 64
    elif fault == "extra": reference["secret"] = "synthetic"
    previous = policy.plan_identity(plan)
    for event in plan["enrollment_journal"]:
        event["previous_sha256"] = previous
        event["sha256"] = bank.policy.digest({key: value for key, value in event.items() if key != "sha256"})
        previous = event["sha256"]
    assert not policy.compare_cohort(plan, cases, cfg, now=now)["accepted"]


def test_reference_does_not_upgrade_legacy_or_missing_sources():
    identity = code.snapshot("rank_canary")
    legacy = {"version": code.LEGACY_VERSION, "gate": "rank_canary",
        "sources": [row for row in identity["sources"] if row["path"] != "runtime/loaded_gate_code.py"]}
    legacy["sha256"] = code._digest(legacy)
    assert code.reference(legacy, gate="rank_canary")["sha256"] == legacy["sha256"]
    assert not code.matches_current(legacy, gate="rank_canary")
    with pytest.raises(ValueError):
        code.reference({}, gate="rank_canary")
    with pytest.raises(ValueError):
        code.reference(identity, gate="moonshot")
