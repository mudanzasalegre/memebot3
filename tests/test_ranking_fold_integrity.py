"""Synthetic model-fit cohort checks; no financial or market acceptance."""
from __future__ import annotations

import json
import numpy as np
import pandas as pd
import pytest

from ml import family_training
from ml.model_validation_warnings import (ranking_at_k, ranking_across_score_cohorts,
    ranking_token_skill, ranking_token_skill_ready)
from ranking_skill_fixtures import current_ranking_skill


class FixedFeatureRanker:
    def rank_score(self, frame):
        return frame["price_pct_5m"].to_numpy(dtype=float)


def _frame():
    truth = np.r_[np.tile([0, 1], 50), np.r_[np.ones(90), np.zeros(10)],
                  np.r_[np.ones(50), np.zeros(50)], np.r_[np.ones(10), np.zeros(90)]]
    times = pd.date_range("2026-09-01", periods=len(truth), freq="10min", tz="UTC")
    return pd.DataFrame({"address": [f"mint{i}" for i in range(len(truth))],
                         "timestamp": times, "ts": times + pd.Timedelta(minutes=2),
                         "price_pct_5m": np.repeat([.7, .9, .5, .1], 100),
                         "max_pnl_pct_seen": np.where(truth, 5000., 1.)})


def test_native_family_cannot_approve_only_between_fit_cohort_separation(tmp_path, monkeypatch):
    monkeypatch.setattr(family_training, "fit_calibrated_ranker",
                        lambda *args, **kwargs: (FixedFeatureRanker(), {"calibrated": False}))
    report = family_training.train_classifier_family(family="runner", targets=["runner_1000"],
        feature_set_name="runner_features", frame=_frame(), output_dir=tmp_path, min_rows=20)
    target = report["targets"]["runner_1000"]
    assert target["validation"]["temporal"]["out_of_sample_rows"] == 300
    assert not target["ranking_validation_ready"], target["ranking_token_skill"]
    assert target["ranking_token_skill"]["capture_lift"] == pytest.approx(1.)


def _cohorts():
    truth = np.r_[np.r_[np.ones(90), np.zeros(10)], np.r_[np.ones(50), np.zeros(50)],
                  np.r_[np.ones(10), np.zeros(90)]]
    return truth, np.repeat([.9, .5, .1], 100), [f"mint{i}" for i in range(300)], np.repeat([1, 2, 3], 100)


def test_between_cohort_prevalence_is_not_within_cohort_ranking_skill():
    truth, scores, tokens, cohorts = _cohorts()
    # Pooling one-fit-cohort scores is meaningful only when they really came
    # from one scorer. The same raw arrays cannot represent three separate fits.
    assert ranking_token_skill(truth, scores, tokens)["capture_lift"] == pytest.approx(1.8)
    metrics = ranking_across_score_cohorts(truth, scores, cohorts)
    skill = ranking_token_skill(truth, scores, tokens, score_cohorts=cohorts)
    assert metrics["precision"] == .5 and metrics["recall"] == .1 and metrics["k"] == 30
    assert skill["capture_lift"] == 1 and skill["mean_loss_improvement"] == 0
    assert not ranking_token_skill_ready(skill)


@pytest.mark.parametrize("seed", range(5))
def test_cohort_capture_is_permutation_invariant(seed):
    truth, _, tokens, cohorts = _cohorts()
    scores = truth * .6 + cohorts * .1
    original = ranking_token_skill(truth, scores, tokens, score_cohorts=cohorts)
    order = np.random.default_rng(seed).permutation(len(truth))
    shuffled = ranking_token_skill(truth[order], scores[order], np.asarray(tokens)[order],
                                    score_cohorts=cohorts[order])
    assert shuffled == original and ranking_token_skill_ready(original)


def test_independent_increasing_score_transforms_keep_selections_and_paired_gain():
    truth, _, tokens, cohorts = _cohorts()
    scores = truth.astype(float)
    baseline = scores.copy()
    for group in (1, 2, 3):
        positions = np.flatnonzero(cohorts == group)
        baseline[positions[:5]] = 0
        baseline[positions[-5:]] = 2
    original = ranking_token_skill(truth, scores, tokens, score_cohorts=cohorts,
                                  baseline_scores=baseline)
    transformed = scores.copy()
    transformed_baseline = baseline.copy()
    for group, scale, offset in ((1, .01, 10), (2, 100, -20), (3, 7, 1000)):
        mask = cohorts == group
        transformed[mask] = scores[mask] * scale + offset
        transformed_baseline[mask] = baseline[mask] * (scale * 3) - offset
    changed = ranking_token_skill(truth, transformed, tokens, score_cohorts=cohorts,
                                  baseline_scores=transformed_baseline)
    assert changed == original
    assert ranking_token_skill_ready(original, comparison="incumbent_topk")


def test_rounded_capacity_and_baseline_are_local_to_each_original_cohort():
    sizes = (7, 11, 82)
    cohorts = np.repeat([1, 2, 3], sizes)
    truth = np.tile([0, 1], 50)
    skill = ranking_token_skill(truth, truth, [f"mint{i}" for i in range(100)], score_cohorts=cohorts)
    metrics = ranking_across_score_cohorts(truth, truth, cohorts)
    assert [record["k"] for record in metrics["score_cohorts"]] == [1, 1, 8]
    baseline = sum(truth[cohorts == group].sum() * k / size
                   for group, size, k in zip((1, 2, 3), sizes, (1, 1, 8))) / 100
    assert skill["capacity_fraction"] == .1
    assert skill["mean_baseline_capture_per_token"] == pytest.approx(baseline)
    assert ranking_token_skill_ready(skill)


@pytest.mark.parametrize("cohorts", [[], [1], [None] * 60, [""] * 60, [" "] * 60,
    [[1]] * 60, [1] * 59 + [None]])
def test_partial_or_missing_fit_cohort_identity_never_certifies(cohorts):
    skill = ranking_token_skill([0, 1] * 30, [0, 1] * 30,
                                [f"mint{i}" for i in range(60)], score_cohorts=cohorts)
    assert not ranking_token_skill_ready(skill)
    assert ranking_across_score_cohorts([0, 1] * 30, [0, 1] * 30, cohorts)["precision"] is None


def test_single_cohort_keeps_original_tie_and_capacity_rules():
    truth = [0, 1] * 30
    scores = [0, 1] * 30
    old = ranking_at_k(truth, scores)
    one = ranking_across_score_cohorts(truth, scores, [0] * 60)
    for key in ("rows", "positives", "k", "k_pct", "precision", "recall", "expected_true_positives"):
        assert one[key] == old[key]
    tokens = [f"mint{i}" for i in range(60)]
    assert ranking_token_skill(truth, scores, tokens) == ranking_token_skill(
        truth, scores, tokens, score_cohorts=[0] * 60)


@pytest.mark.parametrize("key,value", [("version", "fixed_topk_paired_token_capture_v1"),
    ("selection_scope", None), ("score_cohort_count", True), ("score_cohort_count", 2),
    ("score_cohort_capacity", []), ("rows", 61)])
def test_legacy_or_inconsistent_cohort_declaration_is_unknown(key, value):
    payload = current_ranking_skill()
    payload[key] = value
    assert not ranking_token_skill_ready(payload)


@pytest.mark.parametrize("key,value", [("rows", True), ("rows", 59), ("k", .6),
    ("k", 61), ("cohort_id", None), ("cohort_id", " 0 ")])
def test_typed_capacity_contract_rejects_forged_cohort_parts(key, value):
    payload = current_ranking_skill()
    payload["score_cohort_capacity"][0][key] = value
    assert not ranking_token_skill_ready(payload)


def test_forward_records_keep_actual_fold_ids_after_skipping_one_class_train(monkeypatch):
    frame = _frame()
    frame.loc[:99, "max_pnl_pct_seen"] = 1
    labels = frame.max_pnl_pct_seen.ge(1000).astype(int)
    monkeypatch.setattr(family_training, "fit_calibrated_ranker",
                        lambda *args, **kwargs: (FixedFeatureRanker(), {"calibrated": False}))
    truth, scores, positions, details = family_training._forward_predictions(frame,
        frame[["price_pct_5m"]], labels, None, min_rows=20, classifier=True)
    assert len(truth) == len(scores) == len(positions) == 200
    assert [fold["fold_id"] for fold in details["evaluated_folds"]] == [2, 3]
    assert [fold["cohort_id"] for fold in details["ranking_evaluation"]["metrics"]["score_cohorts"]] == ["2", "3"]


def test_native_family_cohort_metrics_do_not_depend_on_input_row_order(tmp_path, monkeypatch):
    monkeypatch.setattr(family_training, "fit_calibrated_ranker",
                        lambda *args, **kwargs: (FixedFeatureRanker(), {"calibrated": False}))
    reports = [family_training.train_classifier_family(family="runner", targets=["runner_1000"],
        feature_set_name="runner_features", frame=frame, output_dir=tmp_path / str(index), min_rows=20)
        for index, frame in enumerate((_frame(), _frame().sample(frac=1, random_state=721)))]
    first, second = (report["targets"]["runner_1000"] for report in reports)
    assert first["ranking_metrics"] == second["ranking_metrics"]
    assert first["ranking_token_skill"] == second["ranking_token_skill"]
    assert first["probability_validation_ready"] is False


def test_native_legacy_pooled_skill_is_neutral_but_calibrated_probability_is_independent(tmp_path, monkeypatch):
    from analytics import model_runtime_common as runtime
    frame = _frame()
    frame["price_pct_5m"] = np.where(frame.max_pnl_pct_seen.ge(1000), 80., -5.)
    output = tmp_path / "ml/models/runner"
    report = family_training.train_classifier_family(family="runner", targets=["runner_1000"],
        feature_set_name="runner_features", frame=frame, output_dir=output, min_rows=20)
    assert report["targets"]["runner_1000"]["ranking_validation_ready"]
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    vector = {"price_pct_5m": 80.}
    assert runtime.predict_ranking_score("runner", "runner_1000", vector) is not None
    assert runtime.predict_model("runner", "runner_1000", vector) is not None
    path = output / "runner_1000.meta.json"
    metadata = json.loads(path.read_text())
    metadata["ranking_token_skill"]["version"] = "fixed_topk_paired_token_capture_v1"
    path.write_text(json.dumps(metadata))
    assert runtime.predict_ranking_score("runner", "runner_1000", vector) is None
    assert runtime.predict_ranking_scores("runner", "runner_1000", [vector]) == [None]
    assert runtime.predict_model("runner", "runner_1000", vector) is not None
