"""Isolated byte-coherence checks; no active artifacts or trading are touched."""
from hashlib import sha256
import json
import os

import pytest

from analytics.inference_scope import inference_scope
from ml.primary_activation import selected_reference
from primary_champion_fixtures import champion_artifact
from test_financial_model_acceptance import setup_entry
from test_primary_champion_lifecycle import bind_runtime


def replace_with_same_stat(path, payload):
    original = path.stat()
    assert len(payload) == original.st_size
    path.write_bytes(payload)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert path.stat().st_mtime_ns == original.st_mtime_ns


def test_same_stat_legacy_model_corruption_is_unknown_after_pinned_scope(tmp_path, monkeypatch):
    runtime, _, artifact = setup_entry(tmp_path, monkeypatch)
    with inference_scope():
        assert runtime.should_buy({}) == .5
        original = artifact.model_path.read_bytes()
        replace_with_same_stat(artifact.model_path, bytes([original[0] ^ 1]) + original[1:])
        assert runtime.should_buy({}) == .5
    with inference_scope():
        assert runtime.should_buy({}) is None
        assert not runtime.entry_prediction_state()["activation_ready"]


def test_same_stat_legacy_metadata_replacement_refreshes_threshold(tmp_path, monkeypatch):
    runtime, _, artifact = setup_entry(tmp_path, monkeypatch)
    metadata = json.loads(artifact.meta_path.read_bytes())
    metadata["ai_threshold_recommended"] = .4
    artifact.meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    with inference_scope():
        assert runtime.should_buy({}) == .5
        assert runtime.entry_prediction_state()["metadata"]["ai_threshold_recommended"] == .4
        replacement = artifact.meta_path.read_bytes().replace(b'"ai_threshold_recommended": 0.4', b'"ai_threshold_recommended": 0.9')
        assert sha256(replacement).digest() != sha256(artifact.meta_path.read_bytes()).digest()
        replace_with_same_stat(artifact.meta_path, replacement)
        assert runtime.entry_prediction_state()["metadata"]["ai_threshold_recommended"] == .4
    with inference_scope():
        assert runtime.entry_prediction_state()["metadata"]["ai_threshold_recommended"] == .9


@pytest.mark.parametrize("component", ["model.pkl", "model.meta.json", "acceptance.json",
    "threshold.json", "thresholds.by_lane.json"])
def test_same_stat_selected_component_corruption_cannot_reuse_cached_approval(tmp_path, monkeypatch, component):
    registry, artifact, approval, _, alias = champion_artifact(tmp_path, monkeypatch)
    selected = registry.promote_candidate(artifact, active_model_path=alias, approval=approval)
    runtime = bind_runtime(tmp_path, monkeypatch, registry, alias)
    reference = selected_reference(registry.REGISTRY_PATH, registry.MODELS_DIR, alias)
    path = reference["paths"][component]
    with inference_scope():
        assert runtime.should_buy({"price_pct_5m": 80.}) > .9
        original = path.read_bytes()
        replace_with_same_stat(path, bytes([original[0] ^ 1]) + original[1:])
        assert runtime.should_buy({"price_pct_5m": 80.}) > .9
        assert runtime.threshold_runtime_metadata()["revision"] == selected["primary_activation"]["revision"]
    with inference_scope():
        assert runtime.should_buy({"price_pct_5m": 80.}) is None
        assert not runtime.entry_prediction_state()["activation_ready"]
        assert runtime.threshold_runtime_metadata()["global"] == {}


def test_original_legacy_payload_is_deserialized_not_a_later_path(tmp_path, monkeypatch):
    runtime, _, artifact = setup_entry(tmp_path, monkeypatch)
    original_read = type(artifact.model_path).read_bytes
    reads = []
    def read(path):
        payload = original_read(path)
        if path == artifact.model_path:
            reads.append(payload)
            if len(reads) == 1:
                replace_with_same_stat(path, bytes([payload[0] ^ 1]) + payload[1:])
        return payload
    monkeypatch.setattr(type(artifact.model_path), "read_bytes", read)
    with inference_scope():
        assert runtime.should_buy({}) == .5
        assert len(reads) == 1
        assert runtime.primary_model_selection()["component_sha256"]["model.pkl"] == sha256(reads[0]).hexdigest()
    assert runtime.should_buy({}) is None


def test_original_selected_payloads_are_checked_not_reread_before_deserialization(tmp_path, monkeypatch):
    runtime_module = __import__("analytics.ai_predict", fromlist=["read_bundle"])
    registry, artifact, approval, _, alias = champion_artifact(tmp_path, monkeypatch)
    selected = registry.promote_candidate(artifact, active_model_path=alias, approval=approval)
    runtime = bind_runtime(tmp_path, monkeypatch, registry, alias)
    reference = selected_reference(registry.REGISTRY_PATH, registry.MODELS_DIR, alias)
    original_read_bundle = runtime_module.read_bundle
    captures = []
    def read(reference_arg, registry_path, models_dir, **kwargs):
        captures.append(kwargs["captured_payloads"])
        path = reference["paths"]["model.pkl"]
        if len(captures) == 1:
            payload = path.read_bytes()
            replace_with_same_stat(path, bytes([payload[0] ^ 1]) + payload[1:])
        return original_read_bundle(reference_arg, registry_path, models_dir, **kwargs)
    monkeypatch.setattr(runtime_module, "read_bundle", read)
    with inference_scope():
        assert runtime.should_buy({"price_pct_5m": 80.}) > .9
        assert len(captures) == 1
        assert runtime.primary_model_selection()["component_sha256"] == selected["primary_activation"]["active"]["sha256"]
    assert runtime.should_buy({"price_pct_5m": 80.}) is None


def test_legacy_provenance_is_detached_and_does_not_claim_atomic_approval(tmp_path, monkeypatch):
    runtime, _, artifact = setup_entry(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_THRESHOLDS_BY_LANE_PATH", tmp_path / "missing-lanes.json")
    monkeypatch.setattr(runtime, "_LEGACY_THRESHOLD_PATH", tmp_path / "missing-threshold.json")
    with inference_scope():
        assert runtime.should_buy({}) == .5
        receipt = runtime.primary_model_selection()
        assert receipt["status"] == "checked_artifact" and receipt["mode"] == "checked_legacy_artifact"
        assert receipt["revision"] is None and receipt["buy_permission"] is False
        assert receipt["full_strategy_profitability_established"] is False
        assert receipt["component_sha256"] == {"model.pkl": sha256(artifact.model_path.read_bytes()).hexdigest(),
            "model.meta.json": sha256(artifact.meta_path.read_bytes()).hexdigest()}
        receipt["component_sha256"]["model.pkl"] = "changed"
        assert runtime.primary_model_selection()["component_sha256"]["model.pkl"] != "changed"
        assert runtime.threshold_runtime_metadata()["source"] == "legacy"
        assert not any("path" in key for key in receipt)


@pytest.mark.parametrize("missing", ["model", "metadata"])
def test_unknown_primary_receipt_does_not_invent_model_identity(tmp_path, monkeypatch, missing):
    runtime, _, artifact = setup_entry(tmp_path, monkeypatch)
    (artifact.model_path if missing == "model" else artifact.meta_path).unlink()
    receipt = runtime.primary_model_selection()
    assert receipt["status"] == "unknown" and receipt["component_sha256"] == {}
    assert receipt["model_id"] is receipt["revision"] is receipt["feature_schema_sha256"] is None
    assert receipt["buy_permission"] is False


def test_actual_entry_records_fixed_t0_without_feature_or_policy_change(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from analytics import ai_predict, exit_model_runtime
    from test_entry_inference_coherence import load_helpers
    namespace, _ = load_helpers()
    captures = []
    monkeypatch.setattr(ai_predict, "primary_model_selection", lambda: captures.append(1) or {
        "status": "checked_artifact", "component_sha256": {"model.pkl": "a" * 64},
        "buy_permission": False})
    monkeypatch.setattr(exit_model_runtime, "predict_exit_profile", lambda vec: {"status": "unknown"})
    namespace.update(_runner_profile_for_subject=lambda token: "balanced", _config_hash=lambda: "config",
        build_feature_vector=lambda token, now: {"liquidity_usd": token["liquidity_usd"]},
        CFG=SimpleNamespace(ML_GATE_MODE="off"), DRY_RUN=True,
        predict_risk=lambda vector: None, predict_ev=lambda vector: None,
        entry_prediction_state=lambda: {"activation_ready": False, "metadata": {}},
        decide_ml_action=lambda **kwargs: SimpleNamespace(threshold=.5),
        research_runtime=SimpleNamespace(score_candidate=lambda *a, **k: {}))
    token, captured = {"liquidity_usd": 40000}, datetime(2026, 7, 1, tzinfo=timezone.utc)
    namespace["_score_entry_inputs"](token, captured_at=captured)
    assert not captures and token["entry_model_selection"]["status"] == "disabled"
    namespace.update(CFG=SimpleNamespace(ML_GATE_MODE="advisory"), should_buy=lambda vector: None)
    result = namespace["_score_entry_inputs"](token, captured_at=captured)
    assert captures == [1] and token["entry_model_selection"]["input_captured_at_utc"] == captured.isoformat()
    assert "entry_model_selection" not in result[1] and token["exit_profile"] == "balanced"


def test_primary_receipt_is_detached_in_logs_and_is_not_a_predictor():
    from analytics.research_runtime import _common_payload
    from features.builder import build_feature_vector, ALLOWED_FEATURES, COLUMNS
    from ml.train import _META_COLS, _select_feature_columns
    from datetime import datetime, timezone
    import pandas as pd
    token = {"address": "synthetic", "liquidity_usd": 40000, "entry_model_selection": {
        "status": "checked_artifact", "component_sha256": {"model.pkl": "a" * 64}}}
    payload = _common_payload(token)
    captured = datetime(2026, 7, 1, tzinfo=timezone.utc)
    before = build_feature_vector(token, now=captured)
    token["entry_model_selection"]["component_sha256"]["model.pkl"] = "changed"
    pd.testing.assert_series_equal(before, build_feature_vector(token, now=captured))
    assert payload["entry_model_selection"]["component_sha256"]["model.pkl"] == "a" * 64
    assert "entry_model_selection" not in ALLOWED_FEATURES and "entry_model_selection" not in COLUMNS
    assert "entry_model_selection" in _META_COLS
    _, predictors, excluded = _select_feature_columns(pd.DataFrame({
        "liquidity_usd": [20000., 40000.], "entry_model_selection": [.1, .9]}))
    assert "entry_model_selection" not in predictors and "entry_model_selection" in excluded


@pytest.mark.parametrize("problem", ["missing", "extra", "bytearray", "mapping_list", "wrong_bytes"])
def test_captured_bundle_api_cannot_bypass_shape_or_original_approval(tmp_path, monkeypatch, problem):
    from ml import primary_activation
    registry, artifact, approval, _, alias = champion_artifact(tmp_path, monkeypatch)
    selected = registry.promote_candidate(artifact, active_model_path=alias, approval=approval)
    reference = selected_reference(registry.REGISTRY_PATH, registry.MODELS_DIR, alias)
    captured = {name: path.read_bytes() for name, path in reference["paths"].items()}
    if problem == "missing":
        captured.pop("acceptance.json")
    elif problem == "extra":
        captured["extra"] = b"extra"
    elif problem == "bytearray":
        captured["model.pkl"] = bytearray(captured["model.pkl"])
    elif problem == "mapping_list":
        captured = list(captured.items())
    else:
        captured["model.pkl"] = b"unapproved"
    deserialized = []
    monkeypatch.setattr(primary_activation.joblib, "load", lambda *args, **kwargs: deserialized.append(1))
    with pytest.raises(ValueError, match="captured|checksum"):
        primary_activation.read_bundle(selected["primary_activation"]["active"], registry.REGISTRY_PATH,
            registry.MODELS_DIR, captured_payloads=captured)
    assert not deserialized
