"""Temporary artifacts only; coherent ranks are not profitable execution."""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace

import joblib
import numpy as np
import pandas as pd
import pytest

from analytics import model_runtime_common as runtime
from analytics.inference_scope import ensure_inference_scope, inference_scope, scoped_value


class Ranker:
    hook = None

    def __init__(self, value):
        self.value = value

    def rank_score(self, frame):
        if Ranker.hook is not None:
            hook, Ranker.hook = Ranker.hook, None
            hook()
        return np.full(len(frame), self.value)


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    Ranker.hook = None
    yield
    Ranker.hook = None


def write_head(root, target, value, *, version=None, family="runner", metadata_changes=None):
    directory = root / "ml" / "models" / family
    if version:
        directory = directory / "versions" / version
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{target}.pkl"
    joblib.dump(Ranker(value), path)
    checksum = sha256(path.read_bytes()).hexdigest()
    metadata = {"family": family, "target": target, "features": ["price_pct_5m"],
                "model_sha256": checksum, "ranking_validation_ready": True,
                "rank_reference_quantiles": [0, .25, .5, .75, 1],
                "validation": {"mode": "purged_token_walk_forward", "temporal": {"out_of_sample_rows": 30}}}
    if version:
        metadata["activation_role"] = "scanner_ranking_only"
    metadata.update(metadata_changes or {})
    path.with_suffix(".meta.json").write_text(json.dumps(metadata))
    return {"path": f"versions/{version}/{target}.pkl", "version": version, "model_sha256": checksum,
            "metadata_sha256": sha256(path.with_suffix(".meta.json").read_bytes()).hexdigest()}


def publish(root, heads, **changes):
    path = root / "ml" / "models" / "runner" / "advisory_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "role": "scanner_ranking_only", "heads": heads, **changes}))
    return sha256(path.read_bytes()).hexdigest()


def rank(target="runner_100"):
    return runtime.predict_ranking_score("runner", target, {"price_pct_5m": 1000000})


def test_manifest_selection_is_common_to_previously_unread_targets(tmp_path):
    first = {name: write_head(tmp_path, name, .8, version="one") for name in ("runner_100", "runner_10000")}
    digest = publish(tmp_path, first)
    second = {name: write_head(tmp_path, name, .1, version="two") for name in first}
    with inference_scope():
        assert runtime._model_path("runner", "runner_100").parent.name == "one"
        publish(tmp_path, second)
        assert runtime._model_path("runner", "runner_10000").parent.name == "one"
        assert runtime.family_model_selection("runner")["manifest_sha256"] == digest
    assert runtime._model_path("runner", "runner_10000").parent.name == "two"


def test_all_heads_are_captured_before_first_model_prediction(tmp_path):
    first = {name: write_head(tmp_path, name, .8, version="one") for name in ("runner_100", "runner_10000")}
    digest = publish(tmp_path, first)
    second = {name: write_head(tmp_path, name, .1, version="two") for name in first}
    def replace():
        publish(tmp_path, second)
        write_head(tmp_path, "runner_10000", .1, version="one")
    Ranker.hook = replace
    with inference_scope():
        assert rank() == 80
        assert rank("runner_10000") == 80
        receipt = runtime.family_model_selection("runner")
        assert receipt["manifest_sha256"] == digest
        assert receipt["heads"]["runner_10000"]["model_sha256"] == first["runner_10000"]["model_sha256"]
    with inference_scope():
        assert rank() == 20
        assert rank("runner_10000") == 20


def test_flat_heads_pin_checked_models_before_prediction_without_claiming_common_training(tmp_path):
    write_head(tmp_path, "runner_100", .8)
    write_head(tmp_path, "runner_10000", .8)
    Ranker.hook = lambda: write_head(tmp_path, "runner_10000", .1)
    with inference_scope():
        assert rank() == rank("runner_10000") == 80
        receipt = runtime.family_model_selection("runner")
        assert receipt["mode"] == "legacy_flat" and receipt["capture_stable"]
        assert receipt["manifest_sha256"] is None and not receipt["same_training_cohort_asserted"]
    assert rank("runner_10000") == 20


def test_changed_collection_during_capture_is_neutral_for_whole_decision(tmp_path, monkeypatch):
    write_head(tmp_path, "runner_100", .8)
    write_head(tmp_path, "runner_10000", .8)
    original = runtime._load_unscoped
    changed = []
    def load(path, **kwargs):
        result = original(path, **kwargs)
        if not changed:
            changed.append(True)
            write_head(tmp_path, "runner_10000", .1)
        return result
    monkeypatch.setattr(runtime, "_load_unscoped", load)
    with inference_scope():
        assert rank() is None and rank("runner_10000") is None
        assert not runtime.family_model_selection("runner")["capture_stable"]
    with inference_scope():
        assert rank() == 80 and rank("runner_10000") == 20


def replace_flat_with_same_stat(root, value):
    path = root / "ml" / "models" / "runner" / "runner_10000.pkl"
    metadata = path.with_suffix(".meta.json")
    stamps = {p: p.stat() for p in (path, metadata)}
    write_head(root, "runner_10000", value)
    for p, original in stamps.items():
        assert p.stat().st_size == original.st_size
        os.utime(p, ns=(original.st_atime_ns, original.st_mtime_ns))


def test_same_stat_replacement_does_not_make_cache_reuse_old_model(tmp_path):
    write_head(tmp_path, "runner_100", .8)
    write_head(tmp_path, "runner_10000", .8)
    Ranker.hook = lambda: replace_flat_with_same_stat(tmp_path, .1)
    with inference_scope():
        assert rank() == rank("runner_10000") == 80
    assert rank("runner_10000") == 20


def test_same_stat_mutation_during_capture_invalidates_family(tmp_path, monkeypatch):
    write_head(tmp_path, "runner_100", .8)
    write_head(tmp_path, "runner_10000", .8)
    original = runtime._load_unscoped
    changed = []
    def load(path, **kwargs):
        result = original(path, **kwargs)
        if not changed:
            changed.append(True)
            replace_flat_with_same_stat(tmp_path, .1)
        return result
    monkeypatch.setattr(runtime, "_load_unscoped", load)
    with inference_scope():
        assert rank() is None and rank("runner_10000") is None
        assert not runtime.family_model_selection("runner")["capture_stable"]


@pytest.mark.parametrize("initial", ["absent", "invalid", "empty"])
def test_missing_or_invalid_selection_does_not_slide_to_later_manifest(tmp_path, initial):
    directory = tmp_path / "ml" / "models" / "runner"
    directory.mkdir(parents=True)
    if initial == "invalid":
        (directory / "advisory_manifest.json").write_text("not json")
    elif initial == "empty":
        publish(tmp_path, {})
    with inference_scope():
        assert rank() is None
        publish(tmp_path, {"runner_10000": write_head(tmp_path, "runner_10000", .8, version="new")})
        assert rank("runner_10000") is None
    assert rank("runner_10000") == 80


@pytest.mark.parametrize("change", ["checksum", "metadata_checksum", "missing_metadata_checksum", "version", "role", "target", "family"])
def test_selected_identity_is_checked_before_deserialization(tmp_path, monkeypatch, change):
    entry = write_head(tmp_path, "runner_100", .8, version="one")
    if change == "metadata_checksum":
        entry["metadata_sha256"] = "0" * 64
    elif change == "missing_metadata_checksum":
        entry.pop("metadata_sha256")
    elif change in {"checksum", "version"}:
        entry["model_sha256" if change == "checksum" else "version"] = "0" * 64 if change == "checksum" else "other"
    else:
        path = tmp_path / "ml" / "models" / "runner" / entry["path"]
        metadata = json.loads(path.with_suffix(".meta.json").read_text())
        metadata[{"role": "activation_role", "target": "target", "family": "family"}[change]] = "other"
        path.with_suffix(".meta.json").write_text(json.dumps(metadata))
        entry["metadata_sha256"] = sha256(path.with_suffix(".meta.json").read_bytes()).hexdigest()
    publish(tmp_path, {"runner_100": entry})
    monkeypatch.setattr(runtime.joblib, "load", lambda *args: pytest.fail("unapproved artifact deserialized"))
    assert rank() is None


def test_bad_peer_is_unknown_without_losing_valid_head(tmp_path):
    good = write_head(tmp_path, "runner_100", .8, version="one")
    publish(tmp_path, {"runner_100": good, "runner_10000": {"path": "../../outside.pkl"}})
    with inference_scope():
        assert rank() == 80 and rank("runner_10000") is None


def test_later_metadata_change_cannot_inflate_rank_reference(tmp_path, monkeypatch):
    entry = write_head(tmp_path, "runner_100", .8, version="one")
    publish(tmp_path, {"runner_100": entry})
    path = tmp_path / "ml" / "models" / "runner" / entry["path"]
    metadata = json.loads(path.with_suffix(".meta.json").read_text())
    metadata["rank_reference_quantiles"] = [0, .1, .2, .3, 1]
    path.with_suffix(".meta.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(runtime.joblib, "load", lambda *args: pytest.fail("modified validation metadata accepted"))
    assert rank() is None


def test_retained_heads_from_different_fits_have_one_explicit_selector(tmp_path):
    first = write_head(tmp_path, "runner_100", .8, version="one")
    rare = write_head(tmp_path, "runner_10000", .1, version="two")
    digest = publish(tmp_path, {"runner_100": first, "runner_10000": rare})
    with inference_scope():
        assert rank() == 80 and rank("runner_10000") == 20
        receipt = runtime.family_model_selection("runner")
        assert receipt["manifest_sha256"] == digest and not receipt["same_training_cohort_asserted"]
        assert {head["version"] for head in receipt["heads"].values()} == {"one", "two"}
        receipt["heads"]["runner_100"]["model_sha256"] = "changed"
        assert runtime.family_model_selection("runner")["heads"]["runner_100"]["model_sha256"] == first["model_sha256"]


def test_ensure_scope_reuses_owner_and_standalone_scopes_do_not_leak():
    with inference_scope() as owner:
        with ensure_inference_scope() as nested:
            assert nested is owner
            assert scoped_value("value", lambda: 1) == 1
        assert not owner.closed and scoped_value("value", lambda: 2) == 1
    with ensure_inference_scope() as standalone:
        assert scoped_value("value", lambda: 3) == 3
    assert standalone.closed and not standalone.values


def test_standalone_queue_priority_uses_common_selector_and_extreme_head(tmp_path, monkeypatch):
    from runtime import runner_priority as priority
    monkeypatch.setattr(priority, "CFG", SimpleNamespace(SNIPER_LEARNING_PRIORITY_ENABLED=True))
    old = {name: write_head(tmp_path, name, .8, version="one") for name in ("runner_50", "runner_10000")}
    digest = publish(tmp_path, old)
    new = {name: write_head(tmp_path, name, .1, version="two") for name in old}
    Ranker.hook = lambda: publish(tmp_path, new)
    token = {"address": "synthetic", "price_pct_5m": 1000000, "txns_last_5m": 200,
             "liquidity_usd": 20000, "market_cap_usd": 50000}
    result = priority.learned_runner_priority(token)
    assert result["rank_percentiles"] == {"runner_50": 80, "runner_10000": 80}
    assert result["model_selection"]["manifest_sha256"] == digest
    assert 0 < result["bonus"] <= 20 and not result["buy_permission"]
    assert priority.learned_runner_priority(token)["rank_percentiles"] == {"runner_50": 20, "runner_10000": 20}


@pytest.mark.parametrize("module,function", [
    ("runner_model_runtime", "predict_runner_probabilities"),
    ("risk_model_runtime", "predict_severe_loss_risk"),
    ("continuation_model_runtime", "predict_continuation"),
    ("ev_model_runtime", "predict_ev_scores"),
])
def test_standalone_multi_head_wrappers_share_scope(monkeypatch, module, function):
    import importlib
    wrapper = importlib.import_module("analytics." + module)
    seen = []
    def prediction(*args, **kwargs):
        seen.append(scoped_value("wrapper", lambda: len(seen) + 1))
        return None
    monkeypatch.setattr(wrapper, "predict_model", prediction)
    if hasattr(wrapper, "predict_regression_estimate"):
        monkeypatch.setattr(wrapper, "predict_regression_estimate", lambda *a: {"value": prediction()})
    if hasattr(wrapper, "predict_risk"):
        monkeypatch.setattr(wrapper, "predict_risk", prediction)
    if hasattr(wrapper, "predict_ev"):
        monkeypatch.setattr(wrapper, "predict_ev", prediction)
    getattr(wrapper, function)({})
    assert len(seen) > 1 and set(seen) == {1}
    assert scoped_value("wrapper", lambda: "closed") == "closed"


def test_metadata_receipt_is_never_numeric_diagnostic_predictor():
    from features.auxiliary_semantics import PROOF_COLUMN
    from ml.train import _select_feature_columns
    _, features, excluded = _select_feature_columns(pd.DataFrame({
        "price_pct_5m": [10, 20, 30], PROOF_COLUMN: [1, 2, 3]}))
    assert PROOF_COLUMN not in features and PROOF_COLUMN in excluded
