from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml import runner_advisory_learning as learning
from ml.temporal_validation import temporal_eligibility


def _frame(n=160):
    times = pd.date_range("2026-09-01", periods=n, freq="10min", tz="UTC")
    return pd.DataFrame({
        "address": [f"mint{i}" for i in range(n)], "timestamp": times,
        "ts": times + pd.Timedelta(minutes=2),
        "price_pct_5m": [80 if i % 2 else -5 for i in range(n)],
        "liquidity_usd": [20000] * n, "txns_last_5m": [200] * n,
        "market_cap_usd": [50000] * n,
        "max_pnl_pct_seen": [1600 if i % 2 else 3 for i in range(n)],
        "target_total_pnl_pct": [100 if i % 2 else -10 for i in range(n)],
    })


@pytest.fixture
def trained(tmp_path, monkeypatch):
    monkeypatch.setattr(learning, "CFG", SimpleNamespace(ML_RUNNER_ADVISORY_ENABLED=True,
                                                       ML_RUNNER_ADVISORY_MIN_ROWS=40,
                                                       ML_RUNNER_ADVISORY_MIN_LIFT_DELTA=0.05))
    result = learning.train_runner_advisory(root=tmp_path, frame=_frame())
    assert result["updated"]
    return tmp_path, result


def test_settled_eligibility_excludes_future_missing_and_backwards_labels():
    frame = _frame(8)
    frame.loc[0, "ts"] = pd.NaT
    frame.loc[1, "ts"] = frame.loc[1, "timestamp"] - pd.Timedelta(seconds=1)
    frame.loc[2, "address"] = " "
    valid, *_ = temporal_eligibility(frame, as_of="2026-09-01T00:42:00Z")
    assert valid.to_list() == [False, False, False, True, True, False, False, False]


def test_bootstrap_versions_have_disjoint_holdout_and_no_financial_permission(trained):
    root, result = trained
    assert result["role"] == "scanner_ranking_only"
    assert not result["buy_permission"] and not result["exit_policy_change"]
    directory = root / "ml" / "models" / "runner"
    manifest = json.loads((directory / "advisory_manifest.json").read_text())
    assert "runner_100" in manifest["heads"]
    assert "runner_5000" not in manifest["heads"]  # no invented rare positives
    path = directory / manifest["heads"]["runner_100"]["path"]
    metadata = json.loads(path.with_suffix(".meta.json").read_text())
    from hashlib import sha256
    assert sha256(path.with_suffix(".meta.json").read_bytes()).hexdigest() == manifest["heads"]["runner_100"]["metadata_sha256"]
    holdout_start = pd.Timestamp(result["temporal"]["folds"][-1]["test_start"])
    assert pd.Timestamp(metadata["training_label_latest"]) < holdout_start
    assert metadata["activation_role"] == "scanner_ranking_only"
    assert metadata["later_cohort_evaluation"]["positives"] >= 5
    assert len(metadata["training_token_hashes"]) == result["training_rows"]


def test_runtime_uses_manifest_rank_but_refuses_its_probabilities(trained, monkeypatch):
    import analytics.model_runtime_common as runtime
    root, _ = trained
    monkeypatch.setattr(runtime, "PROJECT_ROOT", root)
    assert runtime.predict_ranking_score("runner", "runner_100", {"price_pct_5m": 80}) is not None
    assert runtime.predict_model("runner", "runner_100", {"price_pct_5m": 80}) is None
    assert runtime.predict_ranking_score("runner", "runner_5000", {"price_pct_5m": 80}) is None


def test_unchanged_data_skips_fit(trained, monkeypatch):
    root, _ = trained
    monkeypatch.setattr(learning, "train_classifier_family", lambda **kwargs: pytest.fail("unexpected refit"))
    result = learning.train_runner_advisory(root=root, frame=_frame())
    assert result["status"] == "unchanged" and not result["updated"]


def test_equal_challenger_retains_incumbent_on_same_cohort(trained):
    root, _ = trained
    path = root / "ml" / "models" / "runner" / "advisory_manifest.json"
    before = path.read_bytes()
    result = learning.train_runner_advisory(root=root, frame=_frame(), force=True)
    assert not result["updated"]
    assert path.read_bytes() == before
    decision = result["decisions"]["runner_100"]
    assert decision["reason"] == "incumbent_not_outperformed_on_same_cohort"
    assert decision["challenger"]["cohort_sha256"] == decision["incumbent"]["cohort_sha256"]


def test_failed_training_preserves_manifest(trained, monkeypatch):
    root, _ = trained
    path = root / "ml" / "models" / "runner" / "advisory_manifest.json"
    before = path.read_bytes()
    def fail(**kwargs):
        raise RuntimeError("fixture failure")
    monkeypatch.setattr(learning, "train_classifier_family", fail)
    with pytest.raises(RuntimeError):
        learning.train_runner_advisory(root=root, frame=_frame(), force=True)
    assert path.read_bytes() == before
    status = json.loads((root / "data" / "metrics" / "runner_advisory_status.json").read_text())
    assert status["status"] == "failed" and status["error_type"] == "RuntimeError"


def test_changed_incumbent_metadata_preserves_selector_and_refuses_rollback(trained):
    from hashlib import sha256
    root, _ = trained
    path = root / "ml" / "models" / "runner" / "advisory_manifest.json"
    manifest = json.loads(path.read_text())
    entry = manifest["heads"]["runner_100"]
    meta_path = (path.parent / entry["path"]).with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text())
    meta["rank_reference_quantiles"] = [0.0, 0.1, 0.2, 0.3, 1.0]
    meta_path.write_text(json.dumps(meta))
    assert sha256(meta_path.read_bytes()).hexdigest() != entry["metadata_sha256"]
    before = path.read_bytes()
    with pytest.raises(ValueError, match="metadata approval checksum"):
        learning.train_runner_advisory(root=root, frame=_frame(), force=True)
    assert path.read_bytes() == before
    manifest["previous_heads"] = manifest["heads"]
    path.write_text(json.dumps(manifest))
    before = path.read_bytes()
    assert not learning.rollback_runner_advisory(root=root)
    assert path.read_bytes() == before


def test_old_model_only_approval_stays_unknown_until_checked_successor(trained, monkeypatch):
    import analytics.model_runtime_common as runtime
    root, _ = trained
    path = root / "ml" / "models" / "runner" / "advisory_manifest.json"
    manifest = json.loads(path.read_text())
    for entry in manifest["heads"].values():
        entry.pop("metadata_sha256")
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(runtime, "PROJECT_ROOT", root)
    assert runtime.predict_ranking_score("runner", "runner_100", {"price_pct_5m": 80}) is None
    replacement = learning.train_runner_advisory(root=root, frame=_frame(), force=True)
    assert replacement["updated"]
    assert replacement["decisions"]["runner_100"]["unproved_incumbent_metadata_approval"]
    assert runtime.predict_ranking_score("runner", "runner_100", {"price_pct_5m": 80}) is not None
    before = path.read_bytes()
    assert not learning.rollback_runner_advisory(root=root)
    assert path.read_bytes() == before


def test_decision_requires_same_cohort_and_later_positives():
    candidate = {"ranking_validation_ready": True, "ranking_metric_version": learning.RANKING_METRIC_VERSION}
    new = {"rows": 100, "positives": 10, "unique_tokens": 100, "positive_tokens": 10,
           "precision_lift_at_k": 3.0, "cohort_sha256": "a",
           "ranking_metric_version": learning.RANKING_METRIC_VERSION}
    old = {**new, "precision_lift_at_k": 2.0, "cohort_sha256": "b"}
    assert learning._candidate_decision(candidate, new, old, min_lift_delta=0.05)[1] == "incomparable_cohorts"
    assert not learning._candidate_decision(candidate, {**new, "positives": 4}, None, min_lift_delta=0.05)[0]
    assert not learning._candidate_decision(candidate, {**new, "unique_tokens": 1}, None, min_lift_delta=0.05)[0]
    assert not learning._candidate_decision(candidate, {**new, "positive_tokens": 1}, None, min_lift_delta=0.05)[0]


def test_manifest_path_traversal_and_invalid_roles_fail_closed(tmp_path, monkeypatch):
    import analytics.model_runtime_common as runtime
    directory = tmp_path / "ml" / "models" / "runner"
    directory.mkdir(parents=True)
    path = directory / "advisory_manifest.json"
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    for manifest in ({"role": "scanner_ranking_only", "heads": {"runner_100": {"path": "../../outside.pkl"}}},
                     {"role": "buy_permission", "heads": {"runner_100": {"path": "versions/v/runner_100.pkl"}}}):
        path.write_text(json.dumps(manifest))
        assert "_unavailable_" in str(runtime._model_path("runner", "runner_100"))
        assert runtime.predict_ranking_score("runner", "runner_100", {}) is None


def test_rollback_preserves_versioned_artifacts_and_unchanged_cycle(trained):
    root, _ = trained
    paths = list((root / "ml" / "models" / "runner" / "versions").rglob("*.pkl"))
    assert learning.rollback_runner_advisory(root=root)
    manifest = json.loads((root / "ml" / "models" / "runner" / "advisory_manifest.json").read_text())
    assert not manifest["heads"]
    assert all(path.exists() for path in paths)
    assert learning.train_runner_advisory(root=root, frame=_frame())["status"] == "unchanged"


def test_daemon_runs_advisory_after_entry_training_failure(tmp_path, monkeypatch):
    from ml import training_daemon as daemon
    released = []
    monkeypatch.setattr(daemon, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(daemon, "acquire_lock", lambda **kwargs: True)
    monkeypatch.setattr(daemon, "release_lock", lambda: released.append(True))
    monkeypatch.setattr(daemon, "feature_dataset_snapshot", lambda: {"usable": True})
    def fail():
        raise ValueError("entry failure")
    monkeypatch.setattr(daemon, "retrain_if_better", fail)
    monkeypatch.setattr(daemon, "train_runner_advisory", lambda: {"status": "completed", "updated": True})
    assert daemon.train_once()
    assert released == [True]
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["runner_advisory"]["updated"]
    assert status["training_errors"] == {"entry_training": "ValueError"}


def test_daemon_failed_cycle_does_not_kill_future_cycles(monkeypatch):
    from ml import training_daemon as daemon
    calls = []
    def cycle():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("temporary filesystem error")
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(daemon, "train_once", cycle)
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        daemon.run_daemon(interval_s=0)
    assert len(calls) == 2 and sleeps == [1, 1]


def test_no_eligible_data_is_not_a_service_failure(tmp_path):
    frame = _frame(10)
    frame["ts"] = pd.NaT
    result = learning.train_runner_advisory(root=tmp_path, frame=frame)
    assert result["status"] == "insufficient_settled_data"
    assert not (tmp_path / "ml" / "models" / "runner" / "advisory_manifest.json").exists()
