"""Versioned automatic learning for scanner order, never buy/exit permission.

Challenger and incumbent are compared on the SAME later, token-disjoint,
settled cohort. A weak/failed candidate cannot overwrite a useful incumbent.
The outer holdout is not used to fit or calibrate the challenger.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import joblib
import numpy as np
import pandas as pd

from config.config import CFG, PROJECT_ROOT
from ml.family_training import load_training_frame, train_classifier_family
from ml.feature_matrix import coerce_feature_frame
from ml.feature_sets import feature_set_hash
from ml.label_builder import RUNNER_THRESHOLDS
from ml.model_validation_warnings import (ranking_at_k, RANKING_METRIC_VERSION,
    ranking_token_skill, ranking_token_skill_ready, RANKING_TOKEN_SKILL_VERSION)
from ml.temporal_validation import purged_temporal_windows, temporal_eligibility
from features.context_encoding import checked_context_schema, SCHEMA_SHA256, STRATEGY_SCHEMA_SHA256
from features.numeric_encoding import checked_numeric_schema, SCHEMA_SHA256 as NUMERIC_SCHEMA_SHA256
from features.auxiliary_semantics import (checked_semantics_schema, checked_model_frame,
    prepare_training_frame, SCHEMA_SHA256 as AUXILIARY_SCHEMA_SHA256)

ROLE = "scanner_ranking_only"
PIPELINE_VERSION = 9  # Token-balanced capture evidence cannot reuse row-only approvals.


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    return payload


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _cohort_digest(frame: pd.DataFrame) -> str:
    columns = sorted(frame.columns)
    normalized = frame[columns].astype("string").fillna("<missing>")
    hashed = pd.util.hash_pandas_object(normalized, index=False).to_numpy().tobytes()
    return sha256(json.dumps(columns).encode() + hashed).hexdigest()


def _identity_hashes(identities: Any) -> list[str]:
    return sorted({sha256(str(value).encode()).hexdigest() for value in identities.dropna()})


def _safe_model_path(family_dir: Path, relative: Any) -> Path:
    path = (family_dir / str(relative)).resolve()
    if not path.is_relative_to(family_dir.resolve()) or path.suffix != ".pkl":
        raise ValueError("Model path is outside its family directory")
    return path


def _evaluate(model: Any, features: list[str], cohort: pd.DataFrame, target: str,
              *, metadata: dict[str, Any] | None = None, score_sink: list[float] | None = None) -> dict[str, Any]:
    if (not checked_context_schema(metadata or {}, features) or not checked_numeric_schema(metadata or {}, features)
            or not checked_semantics_schema(metadata or {}, features)):
        raise ValueError("Unproved advisory context encoding")
    observed = pd.to_numeric(cohort[target], errors="coerce")
    mask = observed.isin([0, 1])
    y = observed[mask].astype(int).to_numpy()
    scores = np.asarray(model.rank_score(coerce_feature_frame(
        checked_model_frame(cohort.loc[mask], features), features)), dtype=float) if len(y) else np.asarray([])
    if len(scores) != len(y) or not np.isfinite(scores).all():
        raise ValueError("Invalid holdout ranking predictions")
    ranking = ranking_at_k(y, scores)
    precision = ranking["precision"]
    rate = float(np.mean(y)) if len(y) else None
    identities = temporal_eligibility(cohort)[3].loc[mask]
    token_skill = ranking_token_skill(y, scores, identities)
    if score_sink is not None:
        score_sink.extend(scores.tolist())
    return {
        "rows": len(y), "positives": int(y.sum()), "base_rate": rate,
        "unique_tokens": int(identities.nunique()),
        "positive_tokens": int(identities.iloc[np.flatnonzero(y == 1)].nunique()),
        "precision_at_k": precision,
        "ranking_metric_version": RANKING_METRIC_VERSION,
        "ranking_metrics": ranking,
        "ranking_token_skill": token_skill,
        "precision_lift_at_k": float(precision / rate) if precision is not None and rate else None,
        "cohort_sha256": _cohort_digest(cohort.loc[mask]),
        "metric": "observed_peak_ranking_not_costed_profit",
    }


def _candidate_decision(candidate: dict[str, Any], challenger: dict[str, Any],
                        incumbent: dict[str, Any] | None, *, min_lift_delta: float,
                        paired_comparison: dict[str, Any] | None = None) -> tuple[bool, str]:
    if not candidate.get("ranking_validation_ready"):
        return False, "internal_temporal_ranking_not_validated"
    if candidate.get("ranking_metric_version") != RANKING_METRIC_VERSION:
        return False, "unsupported_internal_ranking_metric"
    if (challenger.get("ranking_metric_version") != RANKING_METRIC_VERSION
            or incumbent is not None and incumbent.get("ranking_metric_version") != RANKING_METRIC_VERSION):
        return False, "unsupported_later_ranking_metric"
    if not ranking_token_skill_ready(candidate.get("ranking_token_skill")):
        return False, "internal_token_ranking_skill_not_validated"
    if not ranking_token_skill_ready(challenger.get("ranking_token_skill")):
        return False, "later_token_ranking_skill_not_validated"
    lift = challenger["ranking_token_skill"].get("capture_lift")
    if (challenger["rows"] < 30 or challenger["positives"] < 5
            or int(challenger.get("unique_tokens", 0)) < 30 or int(challenger.get("positive_tokens", 0)) < 5
            or lift is None or lift < 1.25):
        return False, "insufficient_later_cohort_ranking_evidence"
    if incumbent is None:
        return True, "bootstrap_validated_scanner_ranker"
    old_skill = incumbent.get("ranking_token_skill")
    if not isinstance(old_skill, dict) or old_skill.get("version") != RANKING_TOKEN_SKILL_VERSION:
        return False, "unsupported_incumbent_token_ranking_metric"
    old_lift = old_skill.get("capture_lift")
    if challenger["cohort_sha256"] != incumbent["cohort_sha256"]:
        return False, "incomparable_cohorts"
    if old_lift is None or lift < old_lift + min_lift_delta:
        return False, "incumbent_not_outperformed_on_same_cohort"
    if not ranking_token_skill_ready(paired_comparison, comparison="incumbent_topk"):
        return False, "incumbent_improvement_uncertain_by_token"
    if paired_comparison.get("cohort_sha256") != challenger["cohort_sha256"]:
        return False, "incomparable_paired_token_cohort"
    return True, "challenger_outperformed_same_later_cohort"


def train_runner_advisory(*, root: Path | None = None, frame: pd.DataFrame | None = None,
                          as_of: Any = None, force: bool = False) -> dict[str, Any]:
    """Called under the training lock; never starts services or sends orders."""
    root = Path(root or PROJECT_ROOT)
    if not bool(getattr(CFG, "ML_RUNNER_ADVISORY_ENABLED", True)):
        return {"status": "disabled", "updated": False, "role": ROLE}
    family_dir = root / "ml" / "models" / "runner"
    manifest_path = family_dir / "advisory_manifest.json"
    status_path = root / "data" / "metrics" / "runner_advisory_status.json"
    stamp = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "status": "pending", "updated": False, "role": ROLE, "updated_at_utc": stamp,
        "automatic_live_activation": False, "buy_permission": False,
        "position_size_change": False, "exit_policy_change": False, "decisions": {},
    }
    try:
        # Absent first-launch data is normal, not a daemon-killing exception.
        if frame is None:
            from ml.train import feature_dataset_snapshot
            if not feature_dataset_snapshot().get("usable"):
                result["status"] = "missing_dataset"
                _atomic_json(status_path, result)
                return result
        df = load_training_frame(frame)
        valid, times, available, identities = temporal_eligibility(df, as_of=as_of)
        result["source_rows"] = len(df)
        result["excluded_unsettled_or_invalid_rows"] = int((~valid).sum())
        df = df.loc[valid].copy().reset_index(drop=True)
        min_rows = max(20, int(getattr(CFG, "ML_RUNNER_ADVISORY_MIN_ROWS", 40)))
        df, semantics_filtering = prepare_training_frame(df, min_current_rows=min_rows)
        result["auxiliary_semantics_filtering"] = semantics_filtering
        delta = float(getattr(CFG, "ML_RUNNER_ADVISORY_MIN_LIFT_DELTA", 0.05))
        if not math.isfinite(delta) or delta < 0:
            raise ValueError("ML_RUNNER_ADVISORY_MIN_LIFT_DELTA must be finite and nonnegative")
        result["settled_rows"] = len(df)
        if len(df) < min_rows:
            result["status"] = "insufficient_settled_data"
            _atomic_json(status_path, result)
            return result
        # Includes targets/availability: late closes and backfilled peaks must
        # invalidate the fingerprint, even if entry features did not change.
        fingerprint = sha256(json.dumps({
            "pipeline_version": PIPELINE_VERSION, "cohort": _cohort_digest(df),
            "feature_set_hash": feature_set_hash("runner_features"),
            "context_encoding_sha256": SCHEMA_SHA256,
            "strategy_context_encoding_sha256": STRATEGY_SCHEMA_SHA256,
            "numeric_encoding_sha256": NUMERIC_SCHEMA_SHA256,
            "auxiliary_semantics_sha256": AUXILIARY_SCHEMA_SHA256,
            "min_rows": min_rows, "min_lift_delta": delta,
            "targets": list(RUNNER_THRESHOLDS),
            "ranking_metric_version": RANKING_METRIC_VERSION,
            "ranking_token_skill_version": RANKING_TOKEN_SKILL_VERSION,
            "precision_at_k_pct": float(getattr(CFG, "PRECISION_AT_K_PCT", .10)),
        }, sort_keys=True).encode()).hexdigest()
        result["dataset_sha256"] = fingerprint
        manifest = _read_json(manifest_path)
        if manifest and (manifest.get("role") != ROLE or not isinstance(manifest.get("heads"), dict)):
            raise ValueError("Invalid advisory manifest; preserve it for diagnosis")
        result["active_targets"] = sorted((manifest.get("heads") or {}).keys())
        previous = _read_json(status_path)
        if not force and previous.get("dataset_sha256") == fingerprint and previous.get("status") in {"completed", "unchanged"}:
            result.update({"status": "unchanged", "candidate_version": previous.get("candidate_version")})
            _atomic_json(status_path, result)
            return result
        windows, timing = purged_temporal_windows(df, min_train_rows=min_rows, as_of=as_of)
        result["temporal"] = timing
        if not windows:
            result["status"] = "insufficient_temporal_cohort"
            _atomic_json(status_path, result)
            return result
        train_idx, test_idx = windows[-1]
        train_frame, holdout = df.iloc[train_idx].copy(), df.iloc[test_idx].copy()
        _, train_times, train_available, train_ids = temporal_eligibility(train_frame, as_of=as_of)
        _, test_times, _, test_ids = temporal_eligibility(holdout, as_of=as_of)
        test_start = test_times.min()
        test_hashes = set(_identity_hashes(test_ids))
        version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:10]
        version_dir = family_dir / "versions" / version
        report = train_classifier_family(
            family="runner", targets=[f"runner_{threshold}" for threshold in RUNNER_THRESHOLDS],
            feature_set_name="runner_features", frame=train_frame.reset_index(drop=True),
            output_dir=version_dir, min_rows=min_rows,
        )
        result["candidate_version"] = version
        result["training_rows"] = len(train_frame)
        result["comparison_rows"] = len(holdout)
        heads = dict(manifest.get("heads") or {})
        old_heads = dict(heads)
        for target, candidate in report.get("targets", {}).items():
            decision: dict[str, Any] = {"selected": False, "reason": candidate.get("reason", "not_validated")}
            result["decisions"][target] = decision
            if candidate.get("status") != "trained":
                continue
            path = version_dir / f"{target}.pkl"
            metadata = _read_json(path.with_suffix(".meta.json"))
            if (not checked_context_schema(metadata, metadata.get("features") or [])
                    or not checked_numeric_schema(metadata, metadata.get("features") or [])
                    or not checked_semantics_schema(metadata, metadata.get("features") or [])):
                raise ValueError("Candidate auxiliary generation is unsupported")
            if sha256(path.read_bytes()).hexdigest() != metadata.get("model_sha256"):
                raise ValueError("Candidate artifact checksum mismatch")
            model = joblib.load(path)
            challenger_scores: list[float] = []
            evaluation = _evaluate(model, metadata["features"], holdout, target, metadata=metadata,
                                   score_sink=challenger_scores)
            decision["challenger"] = evaluation
            incumbent_evaluation = None
            paired_comparison = None
            incumbent = heads.get(target)
            if incumbent:
                old_path = _safe_model_path(family_dir, incumbent.get("path"))
                old_meta = _read_json(old_path.with_suffix(".meta.json"))
                checksum = sha256(old_path.read_bytes()).hexdigest()
                if checksum != old_meta.get("model_sha256") or checksum != incumbent.get("model_sha256"):
                    raise ValueError("Incumbent artifact checksum mismatch; preserve it for diagnosis")
                approved_metadata = incumbent.get("metadata_sha256")
                if approved_metadata is not None and sha256(old_path.with_suffix(".meta.json").read_bytes()).hexdigest() != approved_metadata:
                    raise ValueError("Incumbent metadata approval checksum mismatch; preserve it for diagnosis")
                if (old_meta.get("family") != "runner" or old_meta.get("target") != target
                        or old_meta.get("activation_role") != ROLE
                        or incumbent.get("version") != old_path.parent.name):
                    raise ValueError("Incumbent head identity mismatch; preserve it for diagnosis")
                if (not checked_context_schema(old_meta, old_meta.get("features") or [])
                        or not checked_numeric_schema(old_meta, old_meta.get("features") or [])):
                    raise ValueError("Incumbent input encoding is unsupported")
                if not checked_semantics_schema(old_meta, old_meta.get("features") or []):
                    # Retained on disk/previous_heads, but unavailable at runtime.
                    # Only the independently validated later-cohort candidate
                    # can replace this obsolete interpretation.
                    decision["obsolete_incumbent_generation"] = True
                    incumbent = None
                if approved_metadata is None:
                    # Legacy selectors never bound the original validation
                    # metadata. Do not manufacture an approval by hashing its
                    # current bytes; preserve it and require a checked successor.
                    decision["unproved_incumbent_metadata_approval"] = True
                    incumbent = None
                if old_meta.get("ranking_metric_version") != RANKING_METRIC_VERSION:
                    # Preserve the original artifact and approval. Old row-order
                    # metrics cannot become a new-generation ranking approval.
                    decision["obsolete_incumbent_ranking_metric"] = True
                    incumbent = None
                if not ranking_token_skill_ready(old_meta.get("ranking_token_skill")):
                    decision["obsolete_incumbent_token_ranking_skill"] = True
                    incumbent = None
            if incumbent:
                old_available = pd.to_datetime(old_meta.get("training_label_latest"), utc=True, errors="coerce")
                old_tokens = set(old_meta.get("training_token_hashes") or [])
                if pd.isna(old_available) or old_available >= test_start or not old_tokens or old_tokens & test_hashes:
                    decision["reason"] = "no_fresh_token_disjoint_incumbent_comparison"
                    continue
                incumbent_scores: list[float] = []
                incumbent_evaluation = _evaluate(joblib.load(old_path), old_meta["features"], holdout, target,
                                                 metadata=old_meta, score_sink=incumbent_scores)
                decision["incumbent"] = incumbent_evaluation
                observed = pd.to_numeric(holdout[target], errors="coerce")
                mask = observed.isin([0, 1])
                paired_comparison = ranking_token_skill(observed[mask].to_numpy(), challenger_scores,
                    temporal_eligibility(holdout)[3].loc[mask], baseline_scores=incumbent_scores)
                paired_comparison["cohort_sha256"] = _cohort_digest(holdout.loc[mask])
                decision["paired_token_comparison"] = paired_comparison
            selected, reason = _candidate_decision(candidate, evaluation, incumbent_evaluation,
                min_lift_delta=delta, paired_comparison=paired_comparison)
            decision.update({"selected": selected, "reason": reason})
            # The immutable selected version has an explicit role, so the
            # probability API cannot accidentally start consuming it for buys.
            metadata.update({
                "activation_role": ROLE, "dataset_sha256": fingerprint,
                "training_label_latest": train_available.max().isoformat(),
                "training_decision_latest": train_times.max().isoformat(),
                "training_token_hashes": _identity_hashes(train_ids),
                "later_cohort_evaluation": evaluation,
            })
            _atomic_json(path.with_suffix(".meta.json"), metadata)
            if selected:
                heads[target] = {"path": str(path.relative_to(family_dir)).replace("\\", "/"),
                                 "model_sha256": metadata["model_sha256"],
                                 "metadata_sha256": sha256(path.with_suffix(".meta.json").read_bytes()).hexdigest(),
                                 "version": version}
        _atomic_json(version_dir / "training_report.json", report)
        result["status"] = "completed"
        result["updated"] = heads != old_heads
        result["active_targets"] = sorted(heads.keys())
        if result["updated"]:
            _atomic_json(manifest_path, {
                "schema_version": 1, "role": ROLE, "updated_at_utc": stamp,
                "automatic_live_activation": False, "buy_permission": False,
                "heads": heads, "previous_heads": old_heads,
                "dataset_sha256": fingerprint,
            })
        _atomic_json(status_path, result)
        return result
    except Exception as exc:
        result.update({"status": "failed", "error_type": type(exc).__name__})
        _atomic_json(status_path, result)
        raise


def rollback_runner_advisory(*, root: Path | None = None) -> bool:
    """Restore the last manifest pointers, without deleting model artifacts."""
    root = Path(root or PROJECT_ROOT)
    path = root / "ml" / "models" / "runner" / "advisory_manifest.json"
    manifest = _read_json(path)
    if manifest.get("role") != ROLE or "previous_heads" not in manifest:
        return False
    try:
        for target, entry in manifest["previous_heads"].items():
            model_path = _safe_model_path(path.parent, entry["path"])
            metadata = _read_json(model_path.with_suffix(".meta.json"))
            if (model_path.stem != target or metadata.get("activation_role") != ROLE
                    or metadata.get("family") != "runner" or metadata.get("target") != target
                    or entry.get("version") != model_path.parent.name
                    or not entry.get("metadata_sha256")
                    or sha256(model_path.with_suffix(".meta.json").read_bytes()).hexdigest() != entry.get("metadata_sha256")
                    or not checked_context_schema(metadata, metadata.get("features") or [])
                    or not checked_numeric_schema(metadata, metadata.get("features") or [])
                    or not checked_semantics_schema(metadata, metadata.get("features") or [])
                    or metadata.get("ranking_metric_version") != RANKING_METRIC_VERSION
                    or not ranking_token_skill_ready(metadata.get("ranking_token_skill"))
                    or sha256(model_path.read_bytes()).hexdigest() != entry.get("model_sha256")
                    or metadata.get("model_sha256") != entry.get("model_sha256")):
                return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    current = manifest["heads"]
    manifest["heads"] = manifest["previous_heads"]
    manifest["previous_heads"] = current
    manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["rollback"] = True
    _atomic_json(path, manifest)
    # A rollback must not be immediately undone by an unchanged-data cycle.
    return True


__all__ = ["train_runner_advisory", "rollback_runner_advisory"]
