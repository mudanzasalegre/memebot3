"""Synthetic burst-dependence regressions, not market-profit acceptance."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from copy import deepcopy

from features.context_encoding import context_encoding_schema
from features.numeric_encoding import numeric_encoding_schema
from features.auxiliary_semantics import semantics_schema
from ml import runner_advisory_learning as learning
from ranking_skill_fixtures import current_ranking_skill
from ml.model_validation_warnings import ranking_token_skill, ranking_token_skill_ready
from ml.prediction_validation import paired_token_time_block_check, token_time_block_skill_ready


class FeatureRanker:
    def rank_score(self, frame):
        return frame["price_pct_5m"].to_numpy(dtype=float)


def test_native_later_cohort_cannot_approve_one_burst_as_many_independent_tokens():
    truth = np.r_[np.ones(20), np.zeros(180)]
    times = pd.date_range("2026-09-01T00:00:00Z", periods=200, freq="15s")
    frame = pd.DataFrame({"address": [f"burst_mint{i}" for i in range(200)],
        "timestamp": times, "ts": times + pd.Timedelta(seconds=2),
        "runner_5000": truth, "price_pct_5m": truth})
    features = ["price_pct_5m"]
    metadata = {"context_encoding": context_encoding_schema(features),
        "numeric_encoding": numeric_encoding_schema(features),
        "auxiliary_semantics": semantics_schema(features)}
    evaluation = learning._evaluate(FeatureRanker(), features, frame, "runner_5000", metadata=metadata)
    candidate = {"ranking_validation_ready": True,
        "ranking_metric_version": learning.RANKING_METRIC_VERSION,
        "ranking_token_skill": current_ranking_skill()}
    selected, reason = learning._candidate_decision(candidate, evaluation, None, min_lift_delta=.05)
    assert evaluation["ranking_token_skill"]["capture_lift"] == 10
    assert not selected, (reason, evaluation["ranking_token_skill"])
    assert reason == "later_token_ranking_skill_not_validated"
    assert evaluation["ranking_token_skill"]["token_cluster_validation_ready"]
    assert evaluation["ranking_token_skill"]["temporal_support"]["schemes"][0]["blocks"] == 1


def _vectors():
    truth = np.tile([0, 1], 60)
    tokens = np.array([f"independent_mint{i}" for i in range(len(truth))])
    times = pd.date_range("2026-09-01", periods=len(truth), freq="10min", tz="UTC")
    return truth, tokens, times


@pytest.mark.parametrize("seed", range(5))
def test_time_support_and_ranking_are_row_permutation_invariant(seed):
    truth, tokens, times = _vectors()
    original = ranking_token_skill(truth, truth, tokens, decision_times=times)
    order = np.random.default_rng(seed).permutation(len(truth))
    assert ranking_token_skill(truth[order], truth[order], tokens[order], decision_times=times[order]) == original
    assert ranking_token_skill_ready(original)


def test_one_positive_burst_cannot_borrow_temporal_support_from_many_negative_hours():
    truth = np.r_[np.ones(20), np.zeros(180)]
    times = list(pd.date_range("2026-09-01", periods=20, freq="15s", tz="UTC"))
    times += list(pd.date_range("2026-09-02", periods=180, freq="1h", tz="UTC"))
    result = ranking_token_skill(truth, truth, [f"mint{i}" for i in range(200)], decision_times=times)
    assert result["token_cluster_validation_ready"] and result["capture_lift"] == 10
    assert not ranking_token_skill_ready(result)
    assert all(record["positive_blocks"] == 1 for record in result["temporal_support"]["schemes"])


def test_repeated_mint_rows_do_not_create_new_time_units_or_relocate_the_anchor():
    truth, tokens, times = _vectors()
    one = ranking_token_skill(truth, truth, tokens, decision_times=times)
    repeated = ranking_token_skill(np.tile(truth, 2), np.tile(truth, 2), np.tile(tokens, 2),
                                   decision_times=list(times) + list(times + pd.Timedelta(days=7)))
    assert repeated["temporal_support"]["rows"] == 240
    for key in ("schemes", "lower_loss_improvement", "unique_tokens", "positive_tokens"):
        assert repeated["temporal_support"][key] == one["temporal_support"][key]


@pytest.mark.parametrize("resolution", ["ns", "us", "ms", "s"])
def test_datetime_resolution_and_timezone_do_not_change_hour_buckets(resolution):
    truth, tokens, times = _vectors()
    expected = ranking_token_skill(truth, truth, tokens, decision_times=times)
    other = times.tz_convert("Europe/Madrid").as_unit(resolution)
    assert ranking_token_skill(truth, truth, tokens, decision_times=other) == expected


@pytest.mark.parametrize("times", [None, [], [None] * 120, [True] * 120, [1790000000] * 120,
    ["bad"] * 120, ["2026-09-01T00:00:00Z"] * 119,
    ["2026-09-01T00:00:00Z"] * 119 + [None]])
def test_missing_partial_or_numeric_times_do_not_create_temporal_approval(times):
    truth, tokens, _ = _vectors()
    result = ranking_token_skill(truth, truth, tokens, decision_times=times)
    assert result["token_cluster_validation_ready"]
    assert not result["temporal_support"]["validation_ready"] and not ranking_token_skill_ready(result)


def test_zero_gain_and_regression_are_not_temporally_supported():
    truth, tokens, times = _vectors()
    for actual, baseline in ((np.ones(120), np.ones(120)), (np.ones(120), np.zeros(120))):
        check = paired_token_time_block_check(tokens, times, actual, baseline, truth)
        assert check["lower_loss_improvement"] <= 0 and not token_time_block_skill_ready(check)


def test_temporal_ready_flag_cannot_disagree_with_minimum_token_support():
    times = pd.date_range("2026-09-01", periods=6, freq="2h", tz="UTC")
    result = paired_token_time_block_check([f"mint{i}" for i in range(6)], times,
                                         np.zeros(6), np.ones(6), np.ones(6))
    assert not result["validation_ready"] and result["reason"] == "insufficient_token_support"


def test_native_later_cohort_uses_decision_times_not_spread_out_closing_times():
    truth = np.r_[np.ones(20), np.zeros(180)]
    decision = pd.date_range("2026-09-01", periods=200, freq="15s", tz="UTC")
    closes = pd.date_range("2026-09-02", periods=200, freq="2h", tz="UTC")
    features = ["price_pct_5m"]
    frame = pd.DataFrame({"address": [f"mint{i}" for i in range(200)], "timestamp": decision,
                         "ts": closes, "runner_5000": truth, "price_pct_5m": truth})
    metadata = {"context_encoding": context_encoding_schema(features),
        "numeric_encoding": numeric_encoding_schema(features), "auxiliary_semantics": semantics_schema(features)}
    result = learning._evaluate(FeatureRanker(), features, frame, "runner_5000", metadata=metadata)
    assert result["ranking_token_skill"]["temporal_support"]["schemes"][0]["blocks"] == 1
    assert not ranking_token_skill_ready(result["ranking_token_skill"])


def test_native_family_forwards_original_oos_decision_times(tmp_path, monkeypatch):
    from ml import family_training
    truth = np.tile([0, 1], 200)
    times = pd.date_range("2026-09-01", periods=400, freq="10min", tz="UTC")
    frame = pd.DataFrame({"address": [f"mint{i}" for i in range(400)], "timestamp": times,
                         "ts": times + pd.Timedelta(minutes=2), "price_pct_5m": truth,
                         "max_pnl_pct_seen": np.where(truth, 5000, 0)})
    monkeypatch.setattr(family_training, "fit_calibrated_ranker",
                        lambda *args, **kwargs: (FeatureRanker(), {"calibrated": False}))
    labels, scores, positions, details = family_training._forward_predictions(frame,
        frame[["price_pct_5m"]], pd.Series(truth), None, min_rows=20, classifier=True)
    expected = ranking_token_skill(labels, scores, frame.address.iloc[positions],
        score_cohorts=np.repeat([1, 2, 3], 100), decision_times=frame.timestamp.iloc[positions])
    assert details["ranking_evaluation"]["token_skill"] == expected
    assert ranking_token_skill_ready(expected)


def test_native_v2_ranking_is_neutral_without_disabling_independent_probability(tmp_path, monkeypatch):
    import json
    from ml.family_training import train_classifier_family
    from analytics import model_runtime_common as runtime
    truth = np.tile([0, 1], 200)
    times = pd.date_range("2026-09-01", periods=400, freq="10min", tz="UTC")
    frame = pd.DataFrame({"address": [f"mint{i}" for i in range(400)], "timestamp": times,
        "ts": times + pd.Timedelta(minutes=2), "price_pct_5m": np.where(truth, 80., -5.),
        "max_pnl_pct_seen": np.where(truth, 5000., 0.)})
    path = tmp_path / "ml/models/runner"
    report = train_classifier_family(family="runner", targets=["runner_1000"], feature_set_name="runner_features",
                                    frame=frame, output_dir=path, min_rows=20)
    assert report["targets"]["runner_1000"]["ranking_validation_ready"]
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    runtime._cache.clear()
    vector = {"price_pct_5m": 80.}
    assert runtime.predict_ranking_score("runner", "runner_1000", vector) is not None
    assert runtime.predict_model("runner", "runner_1000", vector) is not None
    metadata_path = path / "runner_1000.meta.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["ranking_token_skill"]["version"] = "fit_cohort_topk_paired_token_capture_v2"
    metadata["ranking_token_skill"].pop("temporal_support")
    metadata_path.write_text(json.dumps(metadata))
    assert runtime.predict_ranking_score("runner", "runner_1000", vector) is None
    assert runtime.predict_ranking_scores("runner", "runner_1000", [vector]) == [None]
    assert runtime.predict_model("runner", "runner_1000", vector) is not None


@pytest.mark.parametrize("key,value", [("version", "old"), ("method", "iid_rows"),
    ("anchor", None), ("block_seconds", True), ("block_seconds", 1800), ("offsets_seconds", [0]),
    ("minimum_blocks", 5.), ("minimum_positive_blocks", True), ("bootstrap_samples", 1000.),
    ("lower_quantile", .5), ("rows", True), ("unique_tokens", 29), ("positive_tokens", True),
    ("mean_loss_improvement", np.nan), ("lower_loss_improvement", 0), ("validation_ready", 1)])
def test_temporal_declarations_are_typed_current_and_complete(key, value):
    proof = deepcopy(current_ranking_skill()["temporal_support"])
    proof[key] = value
    assert not token_time_block_skill_ready(proof)


@pytest.mark.parametrize("key,value", [("bucket_id", True), ("tokens", 0), ("tokens", 2),
    ("positive_tokens", 3), ("delta_sum", np.nan), ("delta_sum", 5.)])
def test_time_unit_counts_and_utility_are_conserved(key, value):
    proof = deepcopy(current_ranking_skill()["temporal_support"])
    proof["schemes"][0]["block_support"][0][key] = value
    assert not token_time_block_skill_ready(proof)


def test_duplicate_units_and_cross_scheme_lower_bound_mismatch_are_unknown():
    original = current_ranking_skill()["temporal_support"]
    duplicate = deepcopy(original)
    duplicate["schemes"][0]["block_support"][1]["bucket_id"] = duplicate["schemes"][0]["block_support"][0]["bucket_id"]
    assert not token_time_block_skill_ready(duplicate)
    wrong_lower = deepcopy(original)
    wrong_lower["lower_loss_improvement"] += .1
    assert not token_time_block_skill_ready(wrong_lower)


@pytest.mark.parametrize("key,value", [("temporal_support", None), ("token_cluster_validation_ready", False),
    ("version", "fit_cohort_topk_paired_token_capture_v2")])
def test_token_only_generation_is_not_a_current_ranking_approval(key, value):
    payload = current_ranking_skill()
    payload[key] = value
    assert not ranking_token_skill_ready(payload)


def test_typed_temporal_proof_must_belong_to_the_same_token_utility():
    payload = current_ranking_skill()
    different = deepcopy(payload["temporal_support"])
    for scheme in different["schemes"]:
        for record in scheme["block_support"]:
            record["delta_sum"] *= 2
        scheme["lower_loss_improvement"] *= 2
    different["mean_loss_improvement"] *= 2
    different["lower_loss_improvement"] *= 2
    assert token_time_block_skill_ready(different)
    payload["temporal_support"] = different
    assert not ranking_token_skill_ready(payload)
