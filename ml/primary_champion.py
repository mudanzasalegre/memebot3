"""Same later cohort selection, before activation; historical label proxies only."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import io
import json
import math
import re
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from ml.entry_probability import supported_entry_model
from ml.feature_matrix import coerce_feature_frame
from ml.financial_targets import checked_financial_frame, supported_financial_training
from ml.prediction_validation import paired_token_loss_check
from ml.temporal_validation import purged_temporal_windows, temporal_eligibility
from features.builder import ALLOWED_FEATURES
from features.context_encoding import checked_context_schema
from features.numeric_encoding import checked_numeric_schema
from features.auxiliary_semantics import checked_semantics_schema, checked_model_frame, AuxiliarySemanticsError

VERSION = "same_later_primary_champion_v1"
PROVENANCE_VERSION = "primary_training_population_v1"


def identity_hashes(values):
    return sorted({sha256(str(value).encode()).hexdigest() for value in values})


def training_provenance(frame):
    valid, times, available, identities = temporal_eligibility(frame)
    if frame.empty or not valid.all():
        raise ValueError("Primary provenance requires settled identified observations")
    return {"version": PROVENANCE_VERSION, "rows": len(frame),
            "trained_decision_latest": times.max().isoformat(), "trained_label_latest": available.max().isoformat(),
            "token_sha256": identity_hashes(identities), "positive_rate": float(frame.label.mean())}


def _provenance(meta):
    proof = meta.get("training_provenance") or {}
    rate, hashes = proof.get("positive_rate"), proof.get("token_sha256")
    latest = pd.to_datetime(proof.get("trained_label_latest"), utc=True, errors="coerce")
    decision = pd.to_datetime(proof.get("trained_decision_latest"), utc=True, errors="coerce")
    if (proof.get("version") != PROVENANCE_VERSION or type(proof.get("rows")) is not int or proof["rows"] < 20
            or not isinstance(rate, (int, float)) or isinstance(rate, bool) or not math.isfinite(rate) or not 0 < rate < 1
            or not isinstance(hashes, list) or not hashes or hashes != sorted(set(hashes))
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes)
            or len(hashes) > proof["rows"] or pd.isna(latest) or pd.isna(decision) or decision > latest
            or proof["rows"] != (meta.get("financial_training") or {}).get("rows")):
        raise ValueError("Unknown primary training/selection provenance")
    return proof, latest, set(hashes)


def reserve_later_cohort(frame, *, min_train_rows=20, incumbent_metadata=None, incumbent_acceptance=None):
    valid, times, available, identities = temporal_eligibility(frame)
    eligible = frame.loc[valid].reset_index(drop=True)
    windows, timing = purged_temporal_windows(eligible, splits=3, min_train_rows=max(20, min_train_rows))
    details = {"version": VERSION, "excluded_unsettled_rows": int((~valid).sum()), "temporal": timing}
    if not windows:
        return eligible.copy(), pd.DataFrame(), {**details, "reason": "insufficient_reserved_later_cohort"}
    train, test = windows[-1]
    cohort = eligible.iloc[test].copy()
    if incumbent_metadata is not None:
        _, latest, used = _provenance(incumbent_metadata)
        acceptance = incumbent_acceptance or {}
        if acceptance:
            selection_latest = pd.to_datetime(acceptance.get("cohort_label_latest"), utc=True, errors="coerce")
            if pd.isna(selection_latest):
                raise ValueError("Unknown previous primary selection boundary")
            latest = max(latest, selection_latest)
            used |= set(acceptance.get("cohort_token_sha256") or [])
        _, test_times, _, test_ids = temporal_eligibility(cohort)
        fresh = test_times > latest + pd.Timedelta(seconds=60)
        fresh &= ~test_ids.map(lambda value: sha256(str(value).encode()).hexdigest()).isin(used)
        details["excluded_prior_fit_or_selection_rows"] = int((~fresh).sum())
        cohort = cohort.loc[fresh].copy()
    return eligible.iloc[train].copy(), cohort.reset_index(drop=True), details


def current_incumbent(*, registry_path: Path, models_dir: Path, model_alias: Path):
    from ml.primary_activation import read_registry, active_epoch, selected_reference, read_bundle
    registry = read_registry(registry_path)
    epoch = active_epoch(registry, model_alias)
    selected = selected_reference(registry_path, models_dir, model_alias)
    if selected is not None:
        try:
            model, meta, documents, _ = read_bundle(selected["reference"], registry_path, models_dir)
        except AuxiliarySemanticsError:
            # Preserve its bytes and the actual CAS epoch. An incompatible
            # incumbent is not a financial comparison or a reason to prevent
            # a new independently validated bootstrap candidate forever.
            return {"epoch": epoch, "model": None, "metadata": None, "acceptance": None,
                    "unavailable_reason": "auxiliary_semantics_changed"}
        _provenance(meta)
        return {"epoch": epoch, "model": model, "metadata": meta, "acceptance": documents["acceptance.json"]}
    # Legacy bytes are archived on migration. Missing provenance is not an
    # invitation to fabricate an incumbent comparison or use its old metric.
    return {"epoch": epoch, "model": None, "metadata": None, "acceptance": None}


def _ensure_input_encoding(meta):
    features = meta.get("features")
    if (not isinstance(features, list) or not features or len(set(features)) != len(features)
            or any(name not in ALLOWED_FEATURES for name in features)
            or not checked_context_schema(meta, features) or not checked_numeric_schema(meta, features)
            or not checked_semantics_schema(meta, features)):
        raise ValueError("Unproved primary comparison input encoding")


def _predictions(model, meta, cohort):
    _ensure_input_encoding(meta)
    if not supported_entry_model(model, meta) or not supported_financial_training(meta, entry=True):
        raise ValueError("Unsupported primary model for same-cohort evaluation")
    from analytics.ml_policy import _snapshot_threshold_payload
    from ml.lane_taxonomy import normalize_entry_lane
    values = np.asarray(model.predict_proba(coerce_feature_frame(
        checked_model_frame(cohort, meta["features"]), meta["features"]))[:, 1], dtype=float)
    if values.shape != (len(cohort),) or not np.isfinite(values).all() or np.any((values < 0) | (values > 1)):
        raise ValueError("Invalid primary comparison probability")
    decisions = []
    for lane in cohort.get("entry_lane", pd.Series(None, index=cohort.index)):
        threshold, ready, _, _ = _snapshot_threshold_payload(meta, normalize_entry_lane(lane))
        decisions.append(threshold if ready and threshold is not None else np.nan)
    thresholds = np.asarray(decisions, dtype=float)
    selected = np.isfinite(thresholds) & (values >= thresholds)
    net = cohort.target_total_pnl_pct.to_numpy(dtype=float)
    y = cohort.label.to_numpy(dtype=int)
    proxy = np.where(selected, net, 0.)
    proof, _, _ = _provenance(meta)
    losses, baseline = (y - values) ** 2, (y - proof["positive_rate"]) ** 2
    _, _, _, identities = temporal_eligibility(cohort)
    skill = paired_token_loss_check(identities, losses, baseline)
    selected_tokens = identities.loc[selected].nunique()
    return {"rows": len(cohort), "selected_rows": int(selected.sum()), "selected_tokens": int(selected_tokens),
        "precision": float(y[selected].mean()) if selected.any() else None,
        "selected_avg_net_pct": float(net[selected].mean()) if selected.any() else None,
        "mean_fixed_unit_label_proxy_pct": float(proxy.mean()), "probability_loss_skill": skill,
        "brier_score": float(losses.mean())}, proxy


def authorize_candidate(artifact, cohort, *, incumbent: dict, min_rows=30, min_selected=10,
                        precision_floor=.6, min_delta=.25):
    if not math.isfinite(min_delta) or min_delta < 0 or not math.isfinite(precision_floor) or not 0 <= precision_floor <= 1:
        raise ValueError("Invalid primary champion limits")
    model_bytes, meta_bytes = artifact.model_path.read_bytes(), artifact.meta_path.read_bytes()
    meta = json.loads(meta_bytes)
    if sha256(model_bytes).hexdigest() != meta.get("model_sha256") or meta.get("activation_ready") is not True:
        raise ValueError("Primary candidate is not internally ready or has changed")
    _ensure_input_encoding(meta)
    model = joblib.load(io.BytesIO(model_bytes))
    checked, financial = checked_financial_frame(cohort)
    result = {"version": VERSION, "accepted": False, "reason": "insufficient_later_cohort",
        "candidate_model_sha256": sha256(model_bytes).hexdigest(), "candidate_meta_sha256": sha256(meta_bytes).hexdigest(),
        "expected_active_epoch": incumbent["epoch"], "financial_cohort": financial,
        "min_rows": max(30, min_rows), "min_selected": max(5, min_selected), "precision_floor": precision_floor,
        "min_delta_pct_points": min_delta, "scope": "historical_executed_population_fixed_unit_net_label_proxy_not_full_strategy_profit"}
    if (len(checked) != len(cohort) or not financial.get("ready") or financial.get("conflicting_trade_ids")
            or len(checked) < result["min_rows"]):
        return result
    valid, times, available, identities = temporal_eligibility(checked)
    if not valid.all():
        result["reason"] = "unsettled_comparison_labels"
        return result
    hashes = identity_hashes(identities)
    proof, latest, trained = _provenance(meta)
    start = times.min()
    if (latest >= start - pd.Timedelta(seconds=60) or trained & set(hashes)
            or meta["financial_training"]["positive_pnl_ratios"] != financial["positive_pnl_ratios"]):
        result["reason"] = "candidate_comparison_not_later_disjoint_same_target"
        return result
    positive_tokens = int(identities[checked.label == 1].nunique())
    negative_tokens = int(identities[checked.label == 0].nunique())
    result.update(cohort_sha256=financial["population_sha256"], cohort_token_sha256=hashes,
        cohort_start=start.isoformat(), cohort_label_latest=available.max().isoformat(),
        unique_tokens=int(identities.nunique()), positive_tokens=positive_tokens, negative_tokens=negative_tokens)
    challenger, proxy = _predictions(model, meta, checked)
    result["challenger"] = challenger
    if (result["unique_tokens"] < 30 or positive_tokens < 5 or negative_tokens < 5
            or challenger["selected_rows"] < result["min_selected"] or challenger["selected_tokens"] < 5
            or challenger["precision"] is None or challenger["precision"] < precision_floor
            or challenger["selected_avg_net_pct"] is None or challenger["selected_avg_net_pct"] <= 0
            or not challenger["probability_loss_skill"]["validation_ready"]):
        result["reason"] = "challenger_later_support_precision_net_or_probability_skill_failed"
        return result
    reference = np.zeros(len(checked))
    if incumbent.get("model") is not None:
        old_meta = incumbent["metadata"]
        _, old_latest, old_tokens = _provenance(old_meta)
        previous = incumbent.get("acceptance") or {}
        if previous:
            old_latest = max(old_latest, pd.to_datetime(previous["cohort_label_latest"], utc=True))
            old_tokens |= set(previous["cohort_token_sha256"])
        if (old_latest >= start - pd.Timedelta(seconds=60) or old_tokens & set(hashes)
                or old_meta["financial_training"]["positive_pnl_ratios"] != financial["positive_pnl_ratios"]):
            result["reason"] = "incumbent_cannot_be_compared_on_same_later_target_population"
            return result
        evaluation, reference = _predictions(incumbent["model"], old_meta, checked)
        result["incumbent"] = evaluation
    # paired_token_loss_check requires nonnegative losses. Shift both losses by
    # the same row-wise constant: their difference is exactly new - old proxy.
    floor = np.maximum(proxy, reference)
    improvement = paired_token_loss_check(identities, floor - proxy, floor - reference)
    result["paired_net_proxy_improvement"] = improvement
    lower = improvement.get("lower_loss_improvement")
    if (not improvement["validation_ready"] or lower is None or lower <= min_delta):
        result["reason"] = "same_cohort_net_proxy_improvement_uncertain_or_insufficient"
        return result
    result.update(accepted=True, reason="same_later_cohort_challenger_supported" if incumbent.get("model") is not None
                  else "first_later_cohort_primary_supported")
    return result


def supported_approval(approval, model_bytes: bytes, meta_bytes: bytes) -> bool:
    try:
        meta = json.loads(meta_bytes)
        proof, trained_latest, trained_tokens = _provenance(meta)
        hashes = approval["cohort_token_sha256"]
        start = pd.to_datetime(approval["cohort_start"], utc=True, errors="coerce")
        latest = pd.to_datetime(approval["cohort_label_latest"], utc=True, errors="coerce")
        def finite(value):
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        def cluster(value, rows, tokens):
            return (isinstance(value, dict) and value.get("method") == "paired_token_cluster_bootstrap"
                and value.get("validation_ready") is True and type(value.get("rows")) is int
                and value["rows"] == rows and type(value.get("unique_tokens")) is int
                and value["unique_tokens"] == tokens and value.get("bootstrap_samples") == 1000
                and value.get("lower_quantile") == .05 and finite(value.get("lower_loss_improvement"))
                and value["lower_loss_improvement"] > 0 and finite(value.get("mean_loss_improvement")))
        if (approval.get("version") != VERSION or approval.get("accepted") is not True
                or approval.get("candidate_model_sha256") != sha256(model_bytes).hexdigest()
                or approval.get("candidate_meta_sha256") != sha256(meta_bytes).hexdigest()
                or not supported_financial_training({"financial_training": approval["financial_cohort"]}, entry=True)
                or approval["cohort_sha256"] != approval["financial_cohort"]["population_sha256"]
                or approval.get("scope") != "historical_executed_population_fixed_unit_net_label_proxy_not_full_strategy_profit"
                or not isinstance(approval.get("expected_active_epoch"), str)
                or not re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}", approval["expected_active_epoch"])
                or not isinstance(hashes, list) or hashes != sorted(set(hashes))
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes)
                or any(type(approval[key]) is not int for key in ("unique_tokens", "positive_tokens", "negative_tokens", "min_rows", "min_selected"))
                or not 30 <= approval["unique_tokens"] == len(hashes) <= approval["financial_cohort"]["rows"]
                or not 5 <= approval["positive_tokens"] <= approval["unique_tokens"]
                or not 5 <= approval["negative_tokens"] <= approval["unique_tokens"]
                or approval["positive_tokens"] + approval["negative_tokens"] < approval["unique_tokens"]
                or pd.isna(start) or pd.isna(latest) or latest < start
                or trained_latest >= start - pd.Timedelta(seconds=60) or trained_tokens & set(hashes)
                or meta["financial_training"]["positive_pnl_ratios"] != approval["financial_cohort"]["positive_pnl_ratios"]
                or not finite(approval["precision_floor"]) or not 0 <= approval["precision_floor"] <= 1
                or not finite(approval["min_delta_pct_points"]) or approval["min_delta_pct_points"] < 0):
            return False
        challenger, improvement = approval["challenger"], approval["paired_net_proxy_improvement"]
        return bool(all(type(challenger[key]) is int for key in ("rows", "selected_rows", "selected_tokens"))
            and challenger["rows"] == approval["financial_cohort"]["rows"] >= max(30, approval["min_rows"])
            and challenger["rows"] >= challenger["selected_rows"] >= max(5, approval["min_selected"])
            and 5 <= challenger["selected_tokens"] <= min(challenger["selected_rows"], approval["unique_tokens"])
            and finite(challenger["precision"]) and approval["precision_floor"] <= challenger["precision"] <= 1
            and finite(challenger["selected_avg_net_pct"]) and challenger["selected_avg_net_pct"] > 0
            and finite(challenger["mean_fixed_unit_label_proxy_pct"])
            and finite(challenger["brier_score"]) and 0 <= challenger["brier_score"] <= 1
            and cluster(challenger["probability_loss_skill"], challenger["rows"], approval["unique_tokens"])
            and cluster(improvement, challenger["rows"], approval["unique_tokens"])
            and improvement["lower_loss_improvement"] > approval["min_delta_pct_points"] >= 0)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False
