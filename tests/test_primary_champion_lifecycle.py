"""Synthetic primary lifecycle faults; no providers, active artifacts or trading."""
from copy import deepcopy
from dataclasses import replace
import json

import pandas as pd
import pytest

from primary_champion_fixtures import champion_artifact, population
from ml.primary_activation import read_bundle, registry_lock, selected_reference
from ml.primary_champion import (authorize_candidate, current_incumbent, reserve_later_cohort,
    supported_approval, training_provenance)
from ml.financial_targets import checked_financial_frame
from analytics.inference_scope import inference_scope


def bind_runtime(root, monkeypatch, registry, alias):
    from analytics import ai_predict as runtime
    for name, value in {"_MODEL_PATH": alias, "_META_PATH": alias.with_suffix(".meta.json"),
        "_REGISTRY_PATH": registry.REGISTRY_PATH, "_MODELS_DIR": registry.MODELS_DIR,
        "_TRAIN_STATUS_PATH": root / "absent.json", "_model_signature": None}.items():
        monkeypatch.setattr(runtime, name, value)
    return runtime


def incumbent(registry, alias):
    return current_incumbent(registry_path=registry.REGISTRY_PATH, models_dir=registry.MODELS_DIR, model_alias=alias)


def test_same_later_comparison_is_required_before_any_activation(tmp_path, monkeypatch):
    reg, artifact, approval, _, alias = champion_artifact(tmp_path, monkeypatch)
    assert approval["accepted"] and not alias.exists() and not reg.REGISTRY_PATH.exists()
    with pytest.raises(RuntimeError, match="same-later-cohort"):
        reg.promote_candidate(artifact, active_model_path=alias)
    assert not alias.exists() and not reg.REGISTRY_PATH.exists()
    selected = reg.promote_candidate(artifact, active_model_path=alias, approval=approval)
    model, meta, docs, paths = read_bundle(selected["primary_activation"]["active"], reg.REGISTRY_PATH, reg.MODELS_DIR)
    assert set(paths) == {"model.pkl", "model.meta.json", "threshold.json", "thresholds.by_lane.json", "acceptance.json"}
    assert docs["acceptance.json"] == approval and meta["training_provenance"]["rows"] == 240
    assert model.predict_proba(pd.DataFrame({"price_pct_5m": [80.]}))[0, 1] > .9


@pytest.mark.parametrize("path,value", [
    ("accepted", 1), ("version", "old"), ("candidate_model_sha256", "a" * 64),
    ("candidate_meta_sha256", "b" * 64), ("expected_active_epoch", "fake"),
    ("cohort_sha256", "0" * 64), ("cohort_token_sha256", []), ("cohort_start", "2026-08-01"),
    ("cohort_label_latest", "bad"), ("unique_tokens", True), ("positive_tokens", 4),
    ("negative_tokens", 100), ("min_rows", True), ("min_selected", 100),
    ("precision_floor", float("nan")), ("min_delta_pct_points", -1),
    ("challenger.rows", 999), ("challenger.selected_rows", True), ("challenger.selected_tokens", 4),
    ("challenger.precision", 1.1), ("challenger.selected_avg_net_pct", float("inf")),
    ("challenger.brier_score", -1), ("challenger.probability_loss_skill.validation_ready", 1),
    ("challenger.probability_loss_skill.unique_tokens", 999),
    ("paired_net_proxy_improvement.rows", 1), ("paired_net_proxy_improvement.lower_loss_improvement", 0),
    ("paired_net_proxy_improvement.method", "row_bootstrap"), ("financial_cohort.return_basis", "gross")])
def test_approval_flags_cannot_override_bound_population_and_uncertainty(tmp_path, monkeypatch, path, value):
    _, artifact, approval, _, _ = champion_artifact(tmp_path, monkeypatch)
    assert supported_approval(approval, artifact.model_path.read_bytes(), artifact.meta_path.read_bytes())
    cell = approval
    keys = path.split(".")
    for key in keys[:-1]:
        cell = cell[key]
    cell[keys[-1]] = value
    assert not supported_approval(approval, artifact.model_path.read_bytes(), artifact.meta_path.read_bytes())


def test_later_reserved_cohort_is_excluded_before_feature_selection_and_final_fit():
    data, _ = checked_financial_frame(population())
    fit, cohort, details = reserve_later_cohort(data, min_train_rows=80)
    assert len(fit) == 180 and len(cohort) == 60
    assert set(fit.mint).isdisjoint(cohort.mint)
    assert fit.outcome_closed_at.max() < cohort.timestamp.min() - pd.Timedelta(seconds=60)
    assert details["temporal"]["folds"][-1]["used"]
    assert training_provenance(fit)["rows"] == 180


def test_cohort_inputs_are_restored_from_frozen_trade_source(tmp_path, monkeypatch):
    reg, artifact, approval, cohort, alias = champion_artifact(tmp_path, monkeypatch)
    cohort["price_pct_5m"] = -999999
    checked = authorize_candidate(artifact, cohort, incumbent=incumbent(reg, alias))
    assert checked["accepted"] and checked["cohort_sha256"] == approval["cohort_sha256"]
    assert checked["challenger"] == approval["challenger"]


@pytest.mark.parametrize("problem", ["small", "in_sample", "shared_tokens", "future", "rare_positive", "weak"])
def test_unusable_comparison_cannot_replace_an_active_model(tmp_path, monkeypatch, problem):
    reg, artifact, _, cohort, alias = champion_artifact(tmp_path, monkeypatch)
    if problem == "small":
        cohort = cohort.iloc[:20]
    elif problem == "in_sample":
        cohort = population(60, start="2026-09-01", prefix="Other")
    elif problem == "shared_tokens":
        cohort = population(60, start="2026-09-10", prefix="ChampionFit")
    elif problem == "future":
        # Execution-proof validation already rejects future closes. Also test
        # the independent comparison maturity cutoff with otherwise valid data.
        from ml import primary_champion
        from ml.temporal_validation import temporal_eligibility
        monkeypatch.setattr(primary_champion, "temporal_eligibility",
            lambda frame: temporal_eligibility(frame, as_of="2026-09-05"))
    elif problem == "rare_positive":
        cohort = pd.concat([cohort.iloc[::2], cohort.iloc[1:8:2]], ignore_index=True)
    else:
        cohort = population(60, start="2026-09-10", prefix="Weak", positive_feature=10.)
    report = authorize_candidate(artifact, cohort, incumbent=incumbent(reg, alias))
    assert report["accepted"] is False
    with pytest.raises(RuntimeError, match="same-later-cohort"):
        reg.promote_candidate(artifact, active_model_path=alias, approval=report)
    assert not alias.exists() and not reg.REGISTRY_PATH.exists()


def test_old_reported_scores_are_not_a_same_cohort_improvement(tmp_path, monkeypatch):
    reg, first, approval, _, alias = champion_artifact(tmp_path, monkeypatch, threshold=.95)
    reg.promote_candidate(first, active_model_path=alias, approval=approval)
    # Same probabilities/admissions on a fresh cohort: no actual improvement.
    _, second, comparison, _, _ = champion_artifact(tmp_path, monkeypatch, name="equal",
        threshold=.95, start="2026-09-15", prefix="FreshEqual")
    assert comparison["accepted"] is False and comparison["incumbent"]["rows"] == comparison["challenger"]["rows"] == 60
    assert comparison["paired_net_proxy_improvement"]["mean_loss_improvement"] == 0
    before = reg.REGISTRY_PATH.read_bytes()
    with pytest.raises(RuntimeError, match="same-later-cohort"):
        reg.promote_candidate(second, active_model_path=alias, approval=comparison)
    assert reg.REGISTRY_PATH.read_bytes() == before


def test_same_cohort_promotion_and_checked_rollback_restore_all_components(tmp_path, monkeypatch):
    reg, first, approval, _, alias = champion_artifact(tmp_path, monkeypatch, name="first", threshold=.95)
    one = reg.promote_candidate(first, active_model_path=alias, approval=approval)
    _, second, approval2, _, _ = champion_artifact(tmp_path, monkeypatch, name="second", threshold=.73,
        start="2026-09-15", prefix="FreshSecond", positive_feature=60.)
    assert approval2["accepted"] and approval2["incumbent"]["selected_rows"] == 0
    two = reg.promote_candidate(second, active_model_path=alias, approval=approval2)
    assert two["primary_activation"]["previous"] == one["primary_activation"]["active"]
    restored = reg.rollback_primary_candidate(active_model_path=alias)
    assert restored["primary_activation"]["active"] == one["primary_activation"]["active"]
    assert restored["primary_activation"]["previous"] == two["primary_activation"]["active"]
    assert restored["primary_activation"]["revision"] != one["primary_activation"]["revision"]
    _, meta, docs, _ = read_bundle(restored["primary_activation"]["active"], reg.REGISTRY_PATH, reg.MODELS_DIR)
    assert meta["threshold_result"]["picked"] == .95 and docs["acceptance.json"] == approval
    assert json.loads((tmp_path / "data/metrics/recommended_threshold.json").read_text())["picked"] == .95


def test_previous_selection_cohort_cannot_be_reused_as_fresh_approval(tmp_path, monkeypatch):
    reg, first, approval, cohort, alias = champion_artifact(tmp_path, monkeypatch)
    reg.promote_candidate(first, active_model_path=alias, approval=approval)
    checked = authorize_candidate(first, cohort, incumbent=incumbent(reg, alias))
    assert not checked["accepted"] and "incumbent_cannot" in checked["reason"]
    fit = checked_financial_frame(population())[0]
    combined = pd.concat([fit, checked_financial_frame(cohort)[0]], ignore_index=True)
    _, reserved, details = reserve_later_cohort(combined, min_train_rows=80,
        incumbent_metadata=incumbent(reg, alias)["metadata"], incumbent_acceptance=approval)
    assert reserved.empty and details["excluded_prior_fit_or_selection_rows"] > 0


def test_cas_prevents_stale_comparison_from_overwriting_new_selection(tmp_path, monkeypatch):
    reg, first, approval, _, alias = champion_artifact(tmp_path, monkeypatch, name="first")
    _, second, stale, _, _ = champion_artifact(tmp_path, monkeypatch, name="stale", start="2026-09-15", prefix="Stale")
    reg.promote_candidate(first, active_model_path=alias, approval=approval)
    before = reg.REGISTRY_PATH.read_bytes()
    with pytest.raises(RuntimeError, match="incumbent changed"):
        reg.promote_candidate(second, active_model_path=alias, approval=stale)
    assert reg.REGISTRY_PATH.read_bytes() == before


def test_os_lock_cannot_be_stolen_by_another_writer(tmp_path, monkeypatch):
    reg, artifact, approval, _, alias = champion_artifact(tmp_path, monkeypatch)
    with registry_lock(reg.REGISTRY_PATH):
        with pytest.raises(RuntimeError, match="mutation already active"):
            reg.promote_candidate(artifact, active_model_path=alias, approval=approval)
    assert not alias.exists() and not reg.REGISTRY_PATH.exists()


def test_precommit_failure_preserves_legacy_bytes_and_selector_absence(tmp_path, monkeypatch):
    from ml import primary_activation
    reg, artifact, _, cohort, alias = champion_artifact(tmp_path, monkeypatch)
    alias.parent.mkdir(exist_ok=True)
    alias.write_bytes(b"legacy-preserved")
    alias.with_suffix(".meta.json").write_text("{}")
    approval = authorize_candidate(artifact, cohort, incumbent=incumbent(reg, alias))
    def fail(*args, **kwargs):
        raise OSError("synthetic selector replace failure")
    monkeypatch.setattr(primary_activation, "write_json_atomic", fail)
    with pytest.raises(OSError):
        reg.promote_candidate(artifact, active_model_path=alias, approval=approval)
    assert alias.read_bytes() == b"legacy-preserved" and not reg.REGISTRY_PATH.exists()


def test_mirror_failure_after_commit_is_a_warning_not_a_partial_rollback(tmp_path, monkeypatch):
    reg, artifact, approval, _, alias = champion_artifact(tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise OSError("synthetic mirror export failure")
    monkeypatch.setattr(reg, "refresh_legacy_mirrors", fail)
    result = reg.promote_candidate(artifact, active_model_path=alias, approval=approval)
    assert result["mirror_errors"] == ["mirror_export:OSError"] and not alias.exists()
    runtime = bind_runtime(tmp_path, monkeypatch, reg, alias)
    assert runtime.should_buy({"price_pct_5m": 80.}) > .9
    assert runtime.threshold_runtime_metadata()["source"] == "atomic_primary_bundle"


@pytest.mark.parametrize("name", ["model.pkl", "model.meta.json", "threshold.json", "thresholds.by_lane.json", "acceptance.json"])
def test_damaged_selected_component_never_falls_back_to_good_old_mirrors(tmp_path, monkeypatch, name):
    reg, artifact, approval, _, alias = champion_artifact(tmp_path, monkeypatch)
    reg.promote_candidate(artifact, active_model_path=alias, approval=approval)
    runtime = bind_runtime(tmp_path, monkeypatch, reg, alias)
    assert runtime.should_buy({"price_pct_5m": 80.}) > .9
    chosen = selected_reference(reg.REGISTRY_PATH, reg.MODELS_DIR, alias)
    chosen["paths"][name].write_bytes(b"damaged")
    assert alias.exists() and runtime.should_buy({"price_pct_5m": 80.}) is None
    assert not runtime.model_runtime_status()["activation_ready"]
    assert runtime.threshold_runtime_metadata()["global"] == {}


def test_corrupt_registry_is_preserved_and_runtime_is_unknown(tmp_path, monkeypatch):
    reg, artifact, approval, _, alias = champion_artifact(tmp_path, monkeypatch)
    reg.promote_candidate(artifact, active_model_path=alias, approval=approval)
    runtime = bind_runtime(tmp_path, monkeypatch, reg, alias)
    reg.REGISTRY_PATH.write_text('{"primary_activation": NaN}')
    before = reg.REGISTRY_PATH.read_bytes()
    assert runtime.should_buy({"price_pct_5m": 80.}) is None
    with pytest.raises(ValueError):
        reg.promote_candidate(artifact, active_model_path=alias, approval=approval)
    assert reg.REGISTRY_PATH.read_bytes() == before


def test_entry_scope_pins_model_and_threshold_across_promotion_and_rollback(tmp_path, monkeypatch):
    reg, first, approval, _, alias = champion_artifact(tmp_path, monkeypatch, name="first", threshold=.95)
    one = reg.promote_candidate(first, active_model_path=alias, approval=approval)
    runtime = bind_runtime(tmp_path, monkeypatch, reg, alias)
    with inference_scope():
        before = runtime.entry_prediction_state()["metadata"]
        _, second, approval2, _, _ = champion_artifact(tmp_path, monkeypatch, name="second", threshold=.73,
            start="2026-09-15", prefix="ScopeFresh", positive_feature=60.)
        assert approval2["accepted"]
        reg.promote_candidate(second, active_model_path=alias, approval=approval2)
        assert runtime.entry_prediction_state()["metadata"] == before
        assert runtime.threshold_runtime_metadata()["revision"] == one["primary_activation"]["revision"]
    assert runtime.entry_prediction_state()["metadata"]["threshold_result"]["picked"] == .73
    with inference_scope():
        second_meta = runtime.entry_prediction_state()["metadata"]
        reg.rollback_primary_candidate(active_model_path=alias)
        assert runtime.entry_prediction_state()["metadata"] == second_meta
    assert runtime.entry_prediction_state()["metadata"]["threshold_result"]["picked"] == .95


@pytest.mark.parametrize("name", ["../escape", "..", "a/b", "a\\b", "C:outside", "", "x" * 129])
def test_explicit_candidate_identity_is_a_safe_immutable_component(tmp_path, monkeypatch, name):
    reg, artifact, _, _, _ = champion_artifact(tmp_path, monkeypatch)
    from primary_champion_fixtures import _strong_parts
    model, meta = _strong_parts()
    if name == "":
        # Empty is the existing request for an auto-generated unique ID.
        first = reg.write_candidate(model=model, meta=meta)
        second = reg.write_candidate(model=model, meta=meta)
        assert first.model_id != second.model_id
    else:
        with pytest.raises(ValueError):
            reg.write_candidate(model=model, meta=meta, model_id=name)
    with pytest.raises(FileExistsError):
        reg.write_candidate(model=model, meta=meta, model_id=artifact.model_id)


def test_candidate_identity_cannot_change_between_approval_and_activation(tmp_path, monkeypatch):
    reg, artifact, approval, _, alias = champion_artifact(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="identity/metadata"):
        reg.promote_candidate(replace(artifact, model_id="other"), active_model_path=alias, approval=approval)
    assert not reg.REGISTRY_PATH.exists()


def test_primary_promotion_and_rollback_preserve_other_family_registry_headers(tmp_path, monkeypatch):
    reg, first, approval, _, alias = champion_artifact(tmp_path, monkeypatch, name="first", threshold=.95)
    families = {"runner": {"active_model_id": "runner-independent", "status": "active"}}
    reg.atomic_write_json(reg.REGISTRY_PATH, {"families": families, "custom_header": "preserved"})
    reg.promote_candidate(first, active_model_path=alias, approval=approval)
    _, second, approval2, _, _ = champion_artifact(tmp_path, monkeypatch, name="second",
        start="2026-09-15", prefix="HeadersFresh", positive_feature=60.)
    reg.promote_candidate(second, active_model_path=alias, approval=approval2)
    restored = reg.rollback_primary_candidate(active_model_path=alias)
    assert restored["families"] == families and restored["custom_header"] == "preserved"


def test_corrupt_previous_bundle_blocks_rollback_without_touching_active_selection(tmp_path, monkeypatch):
    reg, first, approval, _, alias = champion_artifact(tmp_path, monkeypatch, name="first", threshold=.95)
    one = reg.promote_candidate(first, active_model_path=alias, approval=approval)
    _, second, approval2, _, _ = champion_artifact(tmp_path, monkeypatch, name="second",
        start="2026-09-15", prefix="RollbackFresh", positive_feature=60.)
    reg.promote_candidate(second, active_model_path=alias, approval=approval2)
    from ml.primary_activation import reference_paths
    reference_paths(one["primary_activation"]["active"], reg.REGISTRY_PATH, reg.MODELS_DIR)["acceptance.json"].write_bytes(b"bad")
    before = reg.REGISTRY_PATH.read_bytes()
    with pytest.raises(ValueError, match="checksum"):
        reg.rollback_primary_candidate(active_model_path=alias)
    assert reg.REGISTRY_PATH.read_bytes() == before
    assert bind_runtime(tmp_path, monkeypatch, reg, alias).should_buy({"price_pct_5m": 80.}) > .9


def test_absent_first_launch_cannot_load_an_unapproved_candidate(tmp_path, monkeypatch):
    reg, _, _, _, alias = champion_artifact(tmp_path, monkeypatch)
    runtime = bind_runtime(tmp_path, monkeypatch, reg, alias)
    assert runtime.should_buy({"price_pct_5m": 80.}) is None
    assert runtime.entry_prediction_state() == {"activation_ready": False, "metadata": {}}
    assert runtime.model_runtime_status()["candidate_fallback_used"] is False
    with pytest.raises(RuntimeError, match="No checked previous"):
        reg.rollback_primary_candidate(active_model_path=alias)


def test_api_cannot_mix_current_atomic_bundle_with_old_worker_gate_diagnostics(monkeypatch):
    from api.services.ml import _merge_runtime_payload, _effective_gate
    runtime = {"primary_selection_revision": "a" * 32, "model_loaded": True, "activation_ready": True}
    stale = {"model_loaded": False, "activation_ready": False, "threshold": .1, "enforced": False}
    assert _merge_runtime_payload(runtime, stale) == runtime
    gate = _effective_gate(runtime, {"picked": .73}, stale)
    assert gate["threshold"] == .73 and gate["activation_ready"] is True


@pytest.mark.parametrize("mode,enforced", [("off", False), ("shadow", False), ("enforce", True),
    ("legacy", True), ("lane_aware", False), ("sizing_only", False), ("risk_veto_only", False)])
def test_api_reports_atomic_primary_mode_without_fabricating_uniform_lane_enforcement(monkeypatch, mode, enforced):
    from types import SimpleNamespace
    from api.services import ml as service
    monkeypatch.setattr(service, "CFG", SimpleNamespace(ML_GATE_MODE=mode, AI_THRESHOLD=.5))
    current = {"primary_selection_revision": "a" * 32, "activation_ready": True}
    gate = service._effective_gate(current, {"picked": .73}, {"mode": "legacy", "threshold": .1, "enforced": True})
    assert gate["mode"] == mode and gate["enforced"] is enforced and gate["threshold"] == .73
    assert gate["enforcement_scope"] == ("per_lane" if mode == "lane_aware" else "global_primary_probability")
