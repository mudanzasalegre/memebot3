"""Synthetic scanner ranking checks; these are not profitable market trades."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import json

from features.context_encoding import context_encoding_schema
from features.numeric_encoding import numeric_encoding_schema
from features.auxiliary_semantics import semantics_schema
from ml import runner_advisory_learning as learning
from ml.model_validation_warnings import ranking_token_skill, ranking_token_skill_ready, RANKING_TOKEN_SKILL_VERSION
from ranking_skill_fixtures import current_ranking_skill


def _concentrated_frame():
    tokens = ["winner"] * 20 + [f"positive{i}" for i in range(4)] + [f"negative{i}" for i in range(60)]
    times = pd.date_range("2026-09-01", periods=len(tokens), freq="10min", tz="UTC")
    return pd.DataFrame({"address": tokens, "timestamp": times,
                         "ts": times + pd.Timedelta(minutes=2),
                         "price_pct_5m": [1.] * 20 + [0.] * 64,
                         "runner_5000": [1] * 24 + [0] * 60})


class ScoreColumnRanker:
    def rank_score(self, frame):
        return frame["price_pct_5m"].to_numpy(dtype=float)


def _metadata():
    features = ["price_pct_5m"]
    return {"context_encoding": context_encoding_schema(features),
            "numeric_encoding": numeric_encoding_schema(features),
            "auxiliary_semantics": semantics_schema(features)}


def test_native_later_cohort_cannot_approve_one_repeated_winning_token():
    frame = _concentrated_frame()
    evaluation = learning._evaluate(ScoreColumnRanker(), ["price_pct_5m"], frame,
                                    "runner_5000", metadata=_metadata())
    assert evaluation["unique_tokens"] == 65 and evaluation["positive_tokens"] == 5
    assert evaluation["precision_at_k"] == 1 and evaluation["precision_lift_at_k"] == 3.5
    candidate = {"ranking_validation_ready": True,
                 "ranking_metric_version": learning.RANKING_METRIC_VERSION,
                 "ranking_token_skill": current_ranking_skill()}
    selected, reason = learning._candidate_decision(candidate, evaluation, None, min_lift_delta=.05)
    assert not selected, reason
    assert reason == "later_token_ranking_skill_not_validated"
    assert evaluation["ranking_token_skill"]["mean_loss_improvement"] < 0


@pytest.mark.parametrize("seed", range(5))
def test_token_capture_is_permutation_invariant_and_deterministic(seed):
    truth = np.tile([0, 1], 80)
    scores = truth.astype(float)
    tokens = np.array([f"mint{i}" for i in range(len(truth))])
    original = ranking_token_skill(truth, scores, tokens)
    order = np.random.default_rng(seed).permutation(len(truth))
    assert ranking_token_skill(truth[order], scores[order], tokens[order]) == original
    assert ranking_token_skill_ready(original)


def test_repeated_rows_are_one_token_mean_not_independent_bootstrap_samples():
    truth, tokens = [0, 1] * 30, [f"mint{i}" for i in range(60)]
    original = ranking_token_skill(truth, truth, tokens)
    repeated = ranking_token_skill(truth * 3, truth * 3, tokens * 3)
    assert repeated["rows"] == 180 and repeated["unique_tokens"] == 60
    for key in ("capture_lift", "mean_capture_per_token", "lower_loss_improvement"):
        assert repeated[key] == pytest.approx(original[key])


def test_mint_identity_is_trimmed_but_case_sensitive():
    tokens = [f"Mint{i}" for i in range(15)] + [f"mint{i}" for i in range(15)]
    truth = [0] * 15 + [1] * 15
    result = ranking_token_skill(truth * 2, truth * 2, tokens + [f" {t} " for t in tokens])
    assert result["unique_tokens"] == 30 and result["positive_tokens"] == 15
    assert ranking_token_skill_ready(result)


def test_constant_scores_have_no_token_capture_advantage():
    result = ranking_token_skill([0, 1] * 30, [.5] * 60, [f"mint{i}" for i in range(60)])
    assert result["capture_lift"] == 1 and result["lower_loss_improvement"] == 0
    assert not ranking_token_skill_ready(result)


@pytest.mark.parametrize("truth,scores,tokens,baseline,pct", [
    ([], [], [], None, .1), ([1], [1, 2], ["a"], None, .1),
    ([1], [np.nan], ["a"], None, .1), ([np.nan], [1], ["a"], None, .1),
    ([1], [1], [None], None, .1), ([1], [1], [" "], None, .1),
    ([1], [1], ["a", "b"], None, .1), ([1], [1], ["a"], [np.inf], .1),
    ([1], [1], ["a"], [1, 2], .1), ([1], [1], ["a"], None, np.inf),
    ([[1]], [[1]], ["a"], None, .1), ([.5], [1], ["a"], None, .1),
])
def test_missing_or_partial_cluster_evidence_never_certifies_ranking(truth, scores, tokens, baseline, pct):
    result = ranking_token_skill(truth, scores, tokens, baseline_scores=baseline, k_pct=pct)
    assert not result["validation_ready"] and not ranking_token_skill_ready(result)


@pytest.mark.parametrize("key,value", [
    ("version", None), ("version", True), ("version", {}),
    ("lower_loss_improvement", np.nan), ("lower_loss_improvement", True),
    ("lower_loss_improvement", 0), ("unique_tokens", True), ("unique_tokens", 29),
    ("positive_tokens", 4), ("positive_tokens", True), ("capture_lift", 1.24),
    ("capture_lift", np.inf), ("comparison", "incumbent_topk"),
    ("bootstrap_samples", 1), ("lower_quantile", .5), ("validation_ready", 1),
])
def test_runtime_token_skill_declarations_are_typed_and_current(key, value):
    payload = current_ranking_skill()
    payload[key] = value
    assert not ranking_token_skill_ready(payload)


@pytest.mark.parametrize("key,value", [("mean_capture_per_token", None),
    ("mean_baseline_capture_per_token", 0), ("capture_lift", 9.), ("method", "row_bootstrap")])
def test_inconsistent_utility_declarations_are_unknown(key, value):
    payload = current_ranking_skill()
    payload[key] = value
    assert not ranking_token_skill_ready(payload)


def _evaluation(truth, scores, tokens):
    return {"rows": len(truth), "positives": int(np.sum(truth)), "unique_tokens": len(set(tokens)),
            "positive_tokens": len({t for t, y in zip(tokens, truth) if y == 1}),
            "ranking_metric_version": learning.RANKING_METRIC_VERSION,
            "ranking_token_skill": ranking_token_skill(truth, scores, tokens), "cohort_sha256": "same"}


def _candidate():
    return {"ranking_validation_ready": True, "ranking_metric_version": learning.RANKING_METRIC_VERSION,
            "ranking_token_skill": current_ranking_skill()}


def test_one_lucky_improvement_cannot_replace_a_valid_incumbent():
    truth = np.r_[np.ones(20), np.zeros(80)]
    tokens = [f"mint{i}" for i in range(100)]
    old_scores = np.zeros(100)
    old_scores[:9], old_scores[20] = 3., 2.
    new_scores = old_scores.copy()
    new_scores[9] = 4.
    new, old = _evaluation(truth, new_scores, tokens), _evaluation(truth, old_scores, tokens)
    assert ranking_token_skill_ready(new["ranking_token_skill"])
    assert ranking_token_skill_ready(old["ranking_token_skill"])
    assert new["ranking_token_skill"]["capture_lift"] > old["ranking_token_skill"]["capture_lift"] + .05
    paired = ranking_token_skill(truth, new_scores, tokens, baseline_scores=old_scores)
    assert paired["mean_loss_improvement"] > 0 and paired["lower_loss_improvement"] == 0
    selected, reason = learning._candidate_decision(_candidate(), new, old, min_lift_delta=.05,
                                                   paired_comparison=paired)
    assert not selected and reason == "incumbent_improvement_uncertain_by_token"


def test_broader_token_capture_can_replace_row_perfect_concentrated_ranker():
    frame = _concentrated_frame()
    truth, scores, tokens = frame.runner_5000.to_numpy(), frame.price_pct_5m.to_numpy(), frame.address.tolist()
    new, old = _evaluation(truth, truth, tokens), _evaluation(truth, scores, tokens)
    paired = ranking_token_skill(truth, truth, tokens, baseline_scores=scores)
    paired["cohort_sha256"] = "same"
    assert ranking_token_skill_ready(paired, comparison="incumbent_topk")
    assert learning._candidate_decision(_candidate(), new, old, min_lift_delta=.05,
                                        paired_comparison=paired)[0]


@pytest.mark.parametrize("cohort", [None, "other"])
def test_paired_capture_improvement_must_belong_to_the_original_comparison(cohort):
    frame = _concentrated_frame()
    truth, scores, tokens = frame.runner_5000.to_numpy(), frame.price_pct_5m.to_numpy(), frame.address.tolist()
    new, old = _evaluation(truth, truth, tokens), _evaluation(truth, scores, tokens)
    paired = ranking_token_skill(truth, truth, tokens, baseline_scores=scores)
    paired["cohort_sha256"] = cohort
    assert learning._candidate_decision(_candidate(), new, old, min_lift_delta=.05,
        paired_comparison=paired)[1] == "incomparable_paired_token_cohort"


def test_native_comparison_reuses_the_evaluated_scores_without_rescoring():
    frame = _concentrated_frame()
    model = ScoreColumnRanker()
    calls = []
    original = model.rank_score
    model.rank_score = lambda data: (calls.append(len(data)), original(data))[1]
    scores = []
    evaluation = learning._evaluate(model, ["price_pct_5m"], frame, "runner_5000",
                                    metadata=_metadata(), score_sink=scores)
    assert calls == [len(frame)] and scores == frame.price_pct_5m.tolist()
    assert evaluation["ranking_token_skill"]["version"] == RANKING_TOKEN_SKILL_VERSION


def _native_frame(n=400, rare=False):
    times = pd.date_range("2026-09-01", periods=n, freq="10min", tz="UTC")
    truth = np.arange(n) % (20 if rare else 2) == 0
    return pd.DataFrame({"address": [f"native_mint{i}" for i in range(n)], "timestamp": times,
                         "ts": times + pd.Timedelta(minutes=2), "price_pct_5m": np.where(truth, 80., -5.),
                         "liquidity_usd": [20000.] * n, "txns_last_5m": [200.] * n,
                         "market_cap_usd": [50000.] * n, "max_pnl_pct_seen": np.where(truth, 25000., 2.)})


def test_native_rare_extreme_heads_remain_eligible_without_financial_permission(tmp_path):
    result = learning.train_runner_advisory(root=tmp_path, frame=_native_frame(800, rare=True))
    assert result["updated"] and not result["buy_permission"] and not result["position_size_change"]
    for target in ("runner_1000", "runner_2000", "runner_5000", "runner_10000"):
        decision = result["decisions"][target]
        assert decision["selected"]
        assert decision["challenger"]["base_rate"] == .05
        assert ranking_token_skill_ready(decision["challenger"]["ranking_token_skill"])


def test_native_legacy_rank_metadata_is_neutral_but_calibrated_probability_unchanged(tmp_path, monkeypatch):
    from ml.family_training import train_classifier_family
    from analytics import model_runtime_common as runtime

    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    runtime._cache.clear()
    report = train_classifier_family(family="runner", targets=["runner_1000"], feature_set_name="runner_features",
        frame=_native_frame(), output_dir=tmp_path / "ml/models/runner", min_rows=20)
    metadata_path = report["targets"]["runner_1000"]["model_path"]
    from pathlib import Path
    metadata_path = Path(metadata_path).with_suffix(".meta.json")
    vector = {"price_pct_5m": 80.}
    assert runtime.predict_ranking_score("runner", "runner_1000", vector) is not None
    assert runtime.predict_model("runner", "runner_1000", vector) is not None
    metadata = json.loads(metadata_path.read_text())
    metadata.pop("ranking_token_skill")
    metadata_path.write_text(json.dumps(metadata))
    assert runtime.predict_ranking_score("runner", "runner_1000", vector) is None
    assert runtime.predict_ranking_scores("runner", "runner_1000", [vector]) == [None]
    assert runtime.predict_model("runner", "runner_1000", vector) is not None


def test_native_checked_successor_preserves_legacy_token_skill_artifacts_and_refuses_rollback(tmp_path):
    from hashlib import sha256

    first = learning.train_runner_advisory(root=tmp_path, frame=_native_frame())
    assert first["updated"]
    directory = tmp_path / "ml/models/runner"
    manifest_path = directory / "advisory_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    originals = {}
    for head in manifest["heads"].values():
        path = directory / head["path"]
        meta = path.with_suffix(".meta.json")
        payload = json.loads(meta.read_text())
        payload.pop("ranking_token_skill")
        meta.write_text(json.dumps(payload))
        head["metadata_sha256"] = sha256(meta.read_bytes()).hexdigest()
        originals[path] = path.read_bytes()
        originals[meta] = meta.read_bytes()
    manifest_path.write_text(json.dumps(manifest))
    successor = learning.train_runner_advisory(root=tmp_path, frame=_native_frame(), force=True)
    assert successor["updated"]
    assert successor["decisions"]["runner_1000"]["obsolete_incumbent_token_ranking_skill"]
    for path, content in originals.items():
        assert path.read_bytes() == content
    selected = manifest_path.read_bytes()
    assert not learning.rollback_runner_advisory(root=tmp_path)
    assert manifest_path.read_bytes() == selected
