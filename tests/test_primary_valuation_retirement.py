"""Original generation replacement; synthetic, isolated, not model activation."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from primary_valuation_retirement_fixtures import artifact, retained_bundle
from ml.primary_champion import current_incumbent
from ml.primary_activation import read_bundle, reference_paths


def state(reg, alias):
    return current_incumbent(registry_path=reg.REGISTRY_PATH, models_dir=reg.MODELS_DIR, model_alias=alias)


@pytest.mark.parametrize("encoded", [False, True])
def test_native_verified_retirement_preserves_epoch_and_original_boundaries(tmp_path, monkeypatch, encoded):
    reg, alias, selected, metadata, approval, _ = retained_bundle(tmp_path, monkeypatch, encoded=encoded)
    before = reg.REGISTRY_PATH.read_bytes()
    old = state(reg, alias)
    assert old["model"] is None and old["unavailable_reason"] == "valuation_semantics_changed"
    assert old["epoch"] == selected["primary_activation"]["revision"]
    assert old["metadata"] == metadata and old["acceptance"] == approval
    assert reg.REGISTRY_PATH.read_bytes() == before and not alias.exists()


@pytest.mark.parametrize("encoded", [False, True])
def test_native_later_successor_promotes_but_retired_rollback_is_rejected(tmp_path, monkeypatch, encoded):
    reg, alias, selected, _, _, _ = retained_bundle(tmp_path, monkeypatch, encoded=encoded)
    old = state(reg, alias)
    _, candidate, approval, _, _ = artifact(tmp_path, monkeypatch, incumbent=old, encoded=encoded)
    assert approval["accepted"] and "incumbent" not in approval
    assert approval["expected_retired_bundle"] == old["retired_bundle"]
    updated = reg.promote_candidate(candidate, active_model_path=alias, approval=approval)
    assert updated["primary_activation"]["previous"] == selected["primary_activation"]["active"]
    model, metadata, _, _ = read_bundle(updated["primary_activation"]["active"], reg.REGISTRY_PATH, reg.MODELS_DIR)
    assert model is not None and metadata["valuation_training"]["current_rows"] == 240
    before = reg.REGISTRY_PATH.read_bytes()
    with pytest.raises(ValueError):
        reg.rollback_primary_candidate(active_model_path=alias)
    assert reg.REGISTRY_PATH.read_bytes() == before


@pytest.mark.parametrize("encoded", [False, True])
def test_native_retirement_rejects_reused_selection_and_fit_cohorts(tmp_path, monkeypatch, encoded):
    reg, alias, _, _, _, _ = retained_bundle(tmp_path, monkeypatch, encoded=encoded)
    old = state(reg, alias)
    _, _, approval, _, _ = artifact(tmp_path, monkeypatch, name="reused", start="2026-09-10",
        prefix="RetainedSelection", incumbent=old, encoded=encoded)
    assert not approval["accepted"] and "incumbent_cannot" in approval["reason"]


@pytest.mark.parametrize("component", ["model.pkl", "model.meta.json", "acceptance.json", "threshold.json", "thresholds.by_lane.json"])
def test_corrupt_retained_component_never_becomes_a_retirement(tmp_path, monkeypatch, component):
    reg, alias, selected, _, _, _ = retained_bundle(tmp_path, monkeypatch)
    ref = selected["primary_activation"]["active"]
    path = reference_paths(ref, reg.REGISTRY_PATH, reg.MODELS_DIR)[component]
    path.write_bytes(path.read_bytes()+b"corrupt")
    before = reg.REGISTRY_PATH.read_bytes()
    with pytest.raises(ValueError, match="checksum"):
        state(reg, alias)
    assert reg.REGISTRY_PATH.read_bytes() == before and not alias.exists()


def test_default_native_reader_rejects_legacy_cap_before_deserialization(tmp_path, monkeypatch):
    reg, _, selected, _, _, _ = retained_bundle(tmp_path, monkeypatch)
    from ml import primary_activation
    calls = []
    monkeypatch.setattr(primary_activation.joblib, "load", lambda *a: calls.append(True))
    with pytest.raises(ValueError):
        read_bundle(selected["primary_activation"]["active"], reg.REGISTRY_PATH, reg.MODELS_DIR)
    assert not calls


def rebind_retained(reg, selected, *, meta_change=None, approval_change=None, component=None):
    """Adversarial coherent hashes still cannot waive semantic/model checks."""
    from ml.primary_activation import write_bundle
    from utils.atomic_json import write_json_atomic
    old = selected["primary_activation"]["active"]
    paths = reference_paths(old, reg.REGISTRY_PATH, reg.MODELS_DIR)
    payloads = {name: path.read_bytes() for name, path in paths.items()}
    meta = json.loads(payloads["model.meta.json"])
    approval = json.loads(payloads["acceptance.json"])
    if component:
        name, value = component
        payloads[name] = value
    if meta_change:
        meta_change(meta)
    if component and component[0] == "model.pkl":
        meta["model_sha256"] = sha256(payloads["model.pkl"]).hexdigest()
    payloads["model.meta.json"] = json.dumps(meta, sort_keys=True, allow_nan=False).encode()
    approval["candidate_model_sha256"] = sha256(payloads["model.pkl"]).hexdigest()
    approval["candidate_meta_sha256"] = sha256(payloads["model.meta.json"]).hexdigest()
    if approval_change:
        approval_change(approval)
    payloads["acceptance.json"] = json.dumps(approval, sort_keys=True, allow_nan=False).encode()
    reference = write_bundle(payloads, registry_path=reg.REGISTRY_PATH, models_dir=reg.MODELS_DIR,
        model_id=old["model_id"])
    changed = deepcopy(selected)
    changed["primary_activation"]["active"] = reference
    write_json_atomic(reg.REGISTRY_PATH, changed)
    return changed


@pytest.mark.parametrize("problem", ["unknown_numeric", "broken_current_population", "reordered_features",
    "unapproved", "threshold", "calibrator", "financial", "future_training", "bad_model"])
def test_rehashed_invalid_bundle_is_not_a_verified_retirement(tmp_path, monkeypatch, problem):
    from features.numeric_encoding import numeric_encoding_schema
    from ml.primary_activation import VerifiedRetiredPrimaryBundleError
    reg, alias, selected, _, _, _ = retained_bundle(tmp_path, monkeypatch)
    changes = {}
    if problem == "unknown_numeric":
        changes["meta_change"] = lambda m: m.update(numeric_encoding={"version": "unknown"})
    elif problem == "broken_current_population":
        def current(m):
            m["numeric_encoding"] = numeric_encoding_schema(m["features"])
            m["valuation_training"] = {"version": "fake"}
        changes["meta_change"] = current
    elif problem == "reordered_features":
        changes["meta_change"] = lambda m: m.update(features=list(reversed(m["features"])))
    elif problem == "unapproved":
        changes["approval_change"] = lambda a: a.update(accepted=False)
    elif problem == "threshold":
        changes["component"] = ("threshold.json", b'{"picked":0.1}')
    elif problem == "calibrator":
        changes["meta_change"] = lambda m: m.update(calibration_sha256="0"*64)
    elif problem == "financial":
        changes["meta_change"] = lambda m: m["financial_training"].update(return_basis="gross")
    elif problem == "future_training":
        changes["meta_change"] = lambda m: m["training_provenance"].update(trained_label_latest="2099-01-01")
    else:
        changes["component"] = ("model.pkl", b"unpickleable-rebound-model")
    rebind_retained(reg, selected, **changes)
    before = reg.REGISTRY_PATH.read_bytes()
    with pytest.raises(Exception) as caught:
        state(reg, alias)
    assert not isinstance(caught.value, VerifiedRetiredPrimaryBundleError)
    assert reg.REGISTRY_PATH.read_bytes() == before and not alias.exists()


@pytest.mark.parametrize("problem", ["missing_binding", "changed_binding", "changed_epoch", "changed_bundle"])
def test_retirement_promotion_rechecks_binding_and_epoch_under_lock(tmp_path, monkeypatch, problem):
    from utils.atomic_json import write_json_atomic
    reg, alias, selected, _, _, _ = retained_bundle(tmp_path, monkeypatch)
    old = state(reg, alias)
    _, candidate, approval, _, _ = artifact(tmp_path, monkeypatch, incumbent=old)
    assert approval["accepted"]
    if problem == "missing_binding":
        approval.pop("expected_retired_bundle")
    elif problem == "changed_binding":
        approval["expected_retired_bundle"]["sha256"]["model.pkl"] = "0"*64
    elif problem == "changed_epoch":
        selected["primary_activation"]["revision"] = "f"*32
        write_json_atomic(reg.REGISTRY_PATH, selected)
    else:
        rebind_retained(reg, selected, meta_change=lambda m: m.update(feature_set_hash="same-epoch-new-bytes"))
    before = reg.REGISTRY_PATH.read_bytes()
    with pytest.raises(RuntimeError):
        reg.promote_candidate(candidate, active_model_path=alias, approval=approval)
    assert reg.REGISTRY_PATH.read_bytes() == before and not alias.exists()


def test_reserved_later_cohort_keeps_retired_selection_boundaries(tmp_path, monkeypatch):
    from ml.primary_champion import reserve_later_cohort
    from primary_valuation_retirement_fixtures import population
    import pandas as pd
    reg, alias, _, _, _, selected_cohort = retained_bundle(tmp_path, monkeypatch)
    old = state(reg, alias)
    mixed = pd.concat([population(), selected_cohort], ignore_index=True)
    _, reserved, details = reserve_later_cohort(mixed, min_train_rows=80,
        incumbent_metadata=old["metadata"], incumbent_acceptance=old["acceptance"])
    assert reserved.empty and details["excluded_prior_fit_or_selection_rows"] > 0


@pytest.mark.parametrize("flag", [1, "true", None])
def test_retirement_mode_is_not_a_truthy_bypass(tmp_path, flag):
    with pytest.raises(ValueError, match="explicitly boolean"):
        read_bundle({}, tmp_path/"registry.json", tmp_path, verify_retired_generation=flag)


@pytest.mark.parametrize("problem", ["stable_only", "clock_unproved", "clock_wrong_generation",
    "clock_current", "valuation_key_present", "numeric_rule_changed"])
def test_predecessor_recognition_keeps_all_other_generations_checked(problem):
    from features.numeric_encoding import checked_prevaluation_schema, numeric_encoding_schema, PREFIX
    from primary_valuation_retirement_fixtures import predecessor_schema
    from analytics.token_time import AGE_SEMANTICS_VERSION
    features = ["market_cap_usd"]
    metadata = {}
    if problem == "stable_only":
        features = ["price_pct_5m"]
        metadata["numeric_encoding"] = numeric_encoding_schema(features)
    else:
        if problem.startswith("clock"):
            features += ["age_minutes"]
            if problem == "clock_current":
                metadata["token_clock_training"] = {"version": AGE_SEMANTICS_VERSION,
                    "mode": "original_clock_receipts_only", "rows": 40, "current_rows": 40,
                    "unique_tokens": 40, "population_sha256": "a"*64}
        if problem == "numeric_rule_changed":
            features += [PREFIX+"market_cap_usd"]
        metadata["numeric_encoding"] = predecessor_schema(features)
        if problem == "clock_wrong_generation":
            metadata["numeric_encoding"]["token_clock_semantics"] = "old"
        if problem == "valuation_key_present":
            metadata["valuation_training"] = None
        if problem == "numeric_rule_changed":
            metadata["numeric_encoding"]["schema_sha256"] = "f"*64
    assert checked_prevaluation_schema(metadata, features) is (problem == "clock_current")
