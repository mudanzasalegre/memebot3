"""Synthetic ranking evidence only; no claim about market returns."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import json
from pathlib import Path

from ml.family_training import _recall_at_k
from ml.model_validation_warnings import precision_at_k, ranking_at_k, RANKING_METRIC_VERSION
from ranking_skill_fixtures import current_ranking_skill


@pytest.mark.parametrize("reverse", [False, True])
def test_constant_ranking_has_only_the_population_base_rate(reverse):
    truth = np.r_[np.zeros(50), np.ones(50)].astype(int)
    if reverse:
        truth = truth[::-1]
    scores = np.full(100, .5)
    assert precision_at_k(truth, scores, k_pct=.1) == pytest.approx(.5)
    assert _recall_at_k(truth, scores, k_pct=.1) == pytest.approx(.1)


@pytest.mark.parametrize("seed", range(5))
def test_boundary_ties_do_not_borrow_row_order(seed):
    # Three sure selections, then two expected selections out of four tied
    # rows. The tied group has two positive labels: expected TP is 2 + 1.
    truth = np.array([1, 0, 1, 1, 0, 1, 0, 0, 1, 0])
    scores = np.array([.9, .8, .7, .6, .6, .6, .6, .3, .2, .1])
    order = np.random.default_rng(seed).permutation(len(truth))
    assert precision_at_k(truth[order], scores[order], k_pct=.5) == pytest.approx(.6)
    assert _recall_at_k(truth[order], scores[order], k_pct=.5) == pytest.approx(.6)


def test_native_advisory_cannot_promote_an_uninformative_constant_ranker(tmp_path):
    from ml.runner_advisory_learning import train_runner_advisory

    n = 160
    times = pd.date_range("2026-09-01", periods=n, freq="10min", tz="UTC")
    # Each chronological forty-token block has identical measured inputs.
    # Positive rows happen to follow negative rows; this is not model skill.
    frame = pd.DataFrame({
        "address": [f"mint{i}" for i in range(n)], "timestamp": times,
        "ts": times + pd.Timedelta(minutes=2), "price_pct_5m": [1.] * n,
        "liquidity_usd": [20000.] * n, "txns_last_5m": [200.] * n,
        "market_cap_usd": [50000.] * n,
        "max_pnl_pct_seen": ([3.] * 20 + [1500.] * 20) * 4,
    })
    result = train_runner_advisory(root=tmp_path, frame=frame)
    assert result["status"] == "completed"
    assert result["updated"] is False
    assert not (tmp_path / "ml/models/runner/advisory_manifest.json").exists()
    for decision in result["decisions"].values():
        if "challenger" in decision:
            assert decision["challenger"]["precision_lift_at_k"] == pytest.approx(1.)
            assert not decision["selected"]


@pytest.mark.parametrize("pct", [0., .01, .1, .5, 1., 2.])
def test_strictly_ordered_scores_keep_the_original_rounded_selection(pct):
    truth = np.array([0, 1, 1, 0, 1, 0, 0, 1, 1, 1])
    scores = np.arange(10, dtype=float)
    k = max(1, round(len(truth) * min(pct, 1.)))
    actual = ranking_at_k(truth, scores, k_pct=pct)
    assert actual["precision"] == pytest.approx(truth[-k:].mean())
    assert actual["recall"] == pytest.approx(truth[-k:].sum() / truth.sum())
    assert actual["k"] == k
    assert actual["boundary_tied_rows"] == 1
    assert actual["boundary_selected_weight"] == 1


def test_fractional_boundary_has_the_exact_capacity_without_selecting_all_ties():
    actual = ranking_at_k([1, 0, 1, 0, 1, 0], [.9, .8, .8, .8, .8, .1], k_pct=.5)
    assert actual["version"] == RANKING_METRIC_VERSION
    assert actual["k"] == 3
    assert actual["strictly_above_rows"] == 1
    assert actual["boundary_tied_rows"] == 4
    assert actual["boundary_selected_weight"] == .5
    assert actual["expected_true_positives"] == 2
    assert actual["precision"] == pytest.approx(2 / 3)
    assert actual["recall"] == pytest.approx(2 / 3)


@pytest.mark.parametrize("truth,scores,pct", [
    ([], [], .1), ([1], [1, 2], .1), ([[1]], [[1]], .1),
    ([.5], [1], .1), ([np.nan], [1], .1), ([np.inf], [1], .1),
    ([1], [np.nan], .1), ([1], [np.inf], .1), ([1], [1], np.nan),
    ([1], [1], np.inf), ([None], [1], .1), ([1], ["bad"], .1),
])
def test_invalid_or_empty_ranking_evidence_is_unknown(truth, scores, pct):
    actual = ranking_at_k(truth, scores, k_pct=pct)
    assert actual["precision"] is None and actual["recall"] is None


def test_nonfinite_predictions_are_excluded_and_missing_winners_are_not_zero_filled():
    actual = ranking_at_k([1, 0, 1, 0], [np.nan, .5, np.inf, .5], k_pct=.5)
    assert actual["rows"] == 2 and actual["positives"] == 0
    assert actual["precision"] == 0 and actual["recall"] is None


def test_native_later_cohort_evaluation_cannot_claim_skill_from_constant_scores():
    from ml import runner_advisory_learning as learning
    from features.context_encoding import context_encoding_schema
    from features.numeric_encoding import numeric_encoding_schema
    from features.auxiliary_semantics import semantics_schema

    class ConstantRanker:
        def rank_score(self, frame):
            return np.full(len(frame), .5)

    features = ["price_pct_5m"]
    frame = pd.DataFrame({"address": [f"mint{i}" for i in range(100)],
                          "price_pct_5m": [1.] * 100,
                          "runner_1000": [1] * 50 + [0] * 50})
    metadata = {"context_encoding": context_encoding_schema(features),
                "numeric_encoding": numeric_encoding_schema(features),
                "auxiliary_semantics": semantics_schema(features)}
    evaluation = learning._evaluate(ConstantRanker(), features, frame, "runner_1000", metadata=metadata)
    assert evaluation["precision_lift_at_k"] == pytest.approx(1.)
    assert evaluation["ranking_metrics"]["boundary_tied_rows"] == 100
    candidate = {"ranking_validation_ready": True, "ranking_metric_version": RANKING_METRIC_VERSION,
                 "ranking_token_skill": current_ranking_skill()}
    assert not learning._candidate_decision(candidate, evaluation, None, min_lift_delta=.05)[0]


@pytest.mark.parametrize("old_version", [None, "row_order_v0", True, {}])
def test_legacy_metric_cannot_borrow_current_internal_or_later_approval(old_version):
    from ml import runner_advisory_learning as learning

    candidate = {"ranking_validation_ready": True, "ranking_metric_version": RANKING_METRIC_VERSION}
    evaluation = {"rows": 100, "positives": 10, "unique_tokens": 100, "positive_tokens": 10,
                  "precision_lift_at_k": 3., "cohort_sha256": "a",
                  "ranking_metric_version": RANKING_METRIC_VERSION}
    assert not learning._candidate_decision({**candidate, "ranking_metric_version": old_version}, evaluation,
                                            None, min_lift_delta=.05)[0]
    assert not learning._candidate_decision(candidate, {**evaluation, "ranking_metric_version": old_version},
                                            None, min_lift_delta=.05)[0]
    assert not learning._candidate_decision(candidate, evaluation,
                                            {**evaluation, "ranking_metric_version": old_version}, min_lift_delta=.05)[0]


def _signal_frame(n=160):
    times = pd.date_range("2026-09-01", periods=n, freq="10min", tz="UTC")
    return pd.DataFrame({"address": [f"mint{i}" for i in range(n)], "timestamp": times,
                         "ts": times + pd.Timedelta(minutes=2),
                         "price_pct_5m": np.tile([1., 80.], n // 2),
                         "max_pnl_pct_seen": np.tile([2., 50000.], n // 2)})


@pytest.mark.parametrize("old_version", [None, "row_order_v0", True, {}])
def test_actual_scalar_and_batch_runtime_refuse_legacy_rank_metrics_only(tmp_path, monkeypatch, old_version):
    from ml.family_training import train_classifier_family
    from analytics import model_runtime_common as runtime

    report = train_classifier_family(family="runner", targets=["runner_1000"],
        feature_set_name="runner_features", frame=_signal_frame(),
        output_dir=tmp_path / "ml/models/runner")
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    vector = {"price_pct_5m": 80.}
    assert runtime.predict_ranking_score("runner", "runner_1000", vector) is not None
    assert runtime.predict_ranking_scores("runner", "runner_1000", [vector])[0] is not None
    assert runtime.predict_model("runner", "runner_1000", vector) is not None
    metadata_path = Path(report["targets"]["runner_1000"]["model_path"]).with_suffix(".meta.json")
    metadata = json.loads(metadata_path.read_text())
    if old_version is None:
        metadata.pop("ranking_metric_version")
    else:
        metadata["ranking_metric_version"] = old_version
    metadata_path.write_text(json.dumps(metadata))
    assert runtime.predict_ranking_score("runner", "runner_1000", vector) is None
    assert runtime.predict_ranking_scores("runner", "runner_1000", [vector]) == [None]
    # Independent calibrated probability evidence is not rewritten or
    # invalidated by a ranking-only definition change.
    assert runtime.predict_model("runner", "runner_1000", vector) is not None


def test_informative_extreme_runner_heads_remain_rankable_not_buy_permission(tmp_path, monkeypatch):
    from ml.family_training import train_classifier_family
    from analytics import model_runtime_common as runtime
    from runtime.runner_priority import learned_runner_priority

    targets = [f"runner_{threshold}" for threshold in (1000, 2000, 5000, 10000)]
    report = train_classifier_family(family="runner", targets=targets,
        feature_set_name="runner_features", frame=_signal_frame(),
        output_dir=tmp_path / "ml/models/runner")
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    for target in targets:
        item = report["targets"][target]
        assert item["ranking_validation_ready"] is True
        assert item["ranking_metric_version"] == RANKING_METRIC_VERSION
        assert item["ranking_metrics"]["boundary_tied_rows"] > 1
        assert item["precision_lift_at_k"] == pytest.approx(2.)
        assert runtime.predict_ranking_score("runner", target, {"price_pct_5m": 80.}) is not None
    priority = learned_runner_priority({"price_pct_5m": 80., "txns_last_5m": 200,
                                       "liquidity_usd": 20000., "market_cap_usd": 50000.})
    assert set(priority["rank_percentiles"]) == set(targets)
    assert priority["bonus"] > 0 and priority["buy_permission"] is False


def test_ranking_fraction_change_invalidates_unchanged_advisory_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ml import runner_advisory_learning as learning, model_validation_warnings as metrics

    cfg = SimpleNamespace(ML_RUNNER_ADVISORY_ENABLED=True, ML_RUNNER_ADVISORY_MIN_ROWS=40,
                          ML_RUNNER_ADVISORY_MIN_LIFT_DELTA=.05, PRECISION_AT_K_PCT=.1)
    monkeypatch.setattr(learning, "CFG", cfg)
    monkeypatch.setattr(metrics, "CFG", cfg)
    frame = _signal_frame()
    first = learning.train_runner_advisory(root=tmp_path, frame=frame)
    assert first["updated"]
    assert learning.train_runner_advisory(root=tmp_path, frame=frame)["status"] == "unchanged"
    cfg.PRECISION_AT_K_PCT = .2
    second = learning.train_runner_advisory(root=tmp_path, frame=frame)
    assert second["status"] == "completed"
    assert second["dataset_sha256"] != first["dataset_sha256"]
    assert second["decisions"]["runner_1000"]["challenger"]["ranking_metrics"]["k_pct"] == .2


def test_checked_successor_replaces_legacy_metric_without_backfilling_its_artifact(tmp_path, monkeypatch):
    from hashlib import sha256
    from ml import runner_advisory_learning as learning
    from analytics import model_runtime_common as runtime

    frame = _signal_frame()
    assert learning.train_runner_advisory(root=tmp_path, frame=frame)["updated"]
    directory = tmp_path / "ml/models/runner"
    selector = directory / "advisory_manifest.json"
    manifest = json.loads(selector.read_text())
    originals = {}
    for entry in manifest["heads"].values():
        metadata_path = (directory / entry["path"]).with_suffix(".meta.json")
        metadata = json.loads(metadata_path.read_text())
        metadata.pop("ranking_metric_version")
        metadata_path.write_text(json.dumps(metadata))
        originals[metadata_path] = metadata_path.read_bytes()
        entry["metadata_sha256"] = sha256(originals[metadata_path]).hexdigest()
    manifest["previous_heads"] = manifest["heads"]
    selector.write_text(json.dumps(manifest))
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    before = selector.read_bytes()
    assert runtime.predict_ranking_score("runner", "runner_1000", {"price_pct_5m": 80.}) is None
    assert not learning.rollback_runner_advisory(root=tmp_path)
    assert selector.read_bytes() == before
    successor = learning.train_runner_advisory(root=tmp_path, frame=frame, force=True)
    assert successor["updated"]
    assert successor["decisions"]["runner_1000"]["obsolete_incumbent_ranking_metric"]
    assert runtime.predict_ranking_score("runner", "runner_1000", {"price_pct_5m": 80.}) is not None
    assert all(path.read_bytes() == original for path, original in originals.items())
    before = selector.read_bytes()
    assert not learning.rollback_runner_advisory(root=tmp_path)
    assert selector.read_bytes() == before
