"""Bind changed auxiliary meanings to original, causal T0 receipts.

The raw predictor vector stays at 77 columns. Its receipt is storage metadata,
never a predictor. Legacy rows remain useful for stable inputs, but cannot be
silently relabelled as normalized RugCheck risk or snapshot momentum.
"""
from __future__ import annotations

from hashlib import sha256
from copy import deepcopy
import json
from types import MappingProxyType
import numpy as np
import pandas as pd

from features.numeric_encoding import PREFIX, RULES, numeric_value
from features.strategy_context import (ENTRY_VERSION as STRATEGY_ENTRY_VERSION,
    SOURCE as STRATEGY_SOURCE, capture_strategy_context)

VERSION = "fresh_snapshot_momentum_and_normalised_risk_v1"
PROOF_COLUMN = "t0_auxiliary_semantics_proof"
ENTRY_VERSION = "frozen_entry_features_with_auxiliary_receipts_v3"
# v4 preserves these exact auxiliary meanings and adds original strategy context;
# accepting the extension does not fabricate a new meaning for any v1-v3 row.
MEANINGS = {
    "trend": "fresh_snapshot_m5_percentage_point_momentum_not_ema",
    "rug_score": "rugcheck_score_normalised_0_100_higher_is_more_risk",
    "cluster_bad": "confirmed_rpc_top10_token_account_concentration_not_wallet_cluster",
    "score_total": "coverage_aware_soft_score_no_unknown_health_or_absent_holder_bonus",
    "missing_rug_score": "absence_of_normalised_risk_observation",
    "missing_trend": "absence_of_fresh_snapshot_momentum_observation",
    "coverage_core_fields": "observed_core_fields_with_nullable_auxiliary_inputs",
    "snapshot_missing_fields": "unobserved_core_fields_with_nullable_auxiliary_inputs",
}
SCHEMA_SHA256 = sha256(json.dumps(MEANINGS, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
MEANINGS = MappingProxyType(MEANINGS)


class AuxiliarySemanticsError(ValueError):
    """A formerly usable artifact belongs to a different input generation."""


def semantic_sources(features):
    return sorted({name.removeprefix(PREFIX) for name in features
                   if name.removeprefix(PREFIX) in MEANINGS})


def semantics_schema(features):
    sources = semantic_sources(features)
    return ({"version": VERSION, "schema_sha256": SCHEMA_SHA256,
             "entry_receipt_version": ENTRY_VERSION,
             "sources": sources, "meanings": {name: MEANINGS[name] for name in sources}}
            if sources else None)


def checked_semantics_schema(metadata, features):
    expected = semantics_schema(features)
    if metadata.get("auxiliary_semantics") != expected:
        return False
    if expected is None:
        return True  # A legacy model using only unchanged inputs is compatible.
    proof = metadata.get("auxiliary_semantics_training") or {}
    rows, tokens = proof.get("rows"), proof.get("unique_tokens")
    digest = proof.get("population_sha256")
    return (proof.get("version") == VERSION and proof.get("mode") == "current_receipts_only"
            and type(rows) is int and rows >= 30 and type(tokens) is int and 30 <= tokens <= rows
            and proof.get("current_rows") == rows
            and metadata.get("target_rows", rows) == rows and metadata.get("rows", rows) == rows
            and isinstance(digest, str) and len(digest) == 64
            and all(value in "0123456789abcdef" for value in digest))


def input_frame(vector):
    """Preserve the receipt before Series.to_dict() can discard its attrs."""
    if isinstance(vector, pd.DataFrame):
        return vector.copy()
    row = vector.to_dict() if hasattr(vector, "to_dict") else dict(vector or {})
    proof = getattr(vector, "attrs", {}).get(PROOF_COLUMN)
    if proof is not None:
        row[PROOF_COLUMN] = proof
    return pd.DataFrame([row])


def bind_vector_receipt(vector, token):
    """No I/O, no new timestamps, no reuse of a previous vector's receipt."""
    from runtime.trade_learning import freeze_entry_features
    observations = token.get("auxiliary_observations")
    social = token.get("social_signal")
    if not isinstance(observations, dict) or not isinstance(social, dict):
        return vector
    try:
        proof = freeze_entry_features(vector, address=token.get("address"),
            captured_at=vector["timestamp"], auxiliary_observations={"social": social, **observations},
            strategy_context=capture_strategy_context(token))
        if proof["version"] == STRATEGY_ENTRY_VERSION:
            vector.attrs[PROOF_COLUMN] = json.dumps(proof, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, KeyError, RuntimeError):
        pass  # Unproved auxiliary inputs remain unavailable to a semantic model.
    return vector


def _same_value(name, value, original):
    from ml.data_contract import normalize_dex_id, normalize_entry_lane, normalize_entry_regime, normalize_price_source
    normalizer = {"dex_id": normalize_dex_id, "entry_lane": normalize_entry_lane,
                  "entry_regime": normalize_entry_regime, "price_source": normalize_price_source}.get(name)
    if normalizer is not None and value == normalizer(original):
        return True  # Exactly the existing store's categorical projection.
    if name == "timestamp":
        left, right = pd.to_datetime(value, utc=True, errors="coerce"), pd.to_datetime(original, utc=True, errors="coerce")
        return pd.notna(left) and pd.notna(right) and left == right
    if name in RULES:
        left, right = numeric_value(value, name), numeric_value(original, name)
        if left is None or right is None:
            return left is None and right is None
        # The fixed Parquet schema persists continuous observations as float32.
        # Accept exactly that projection, never a generic epsilon or new value.
        return left == right or RULES[name] == "finite_float32" and np.float32(left) == np.float32(right)
    if original is None or original == "":
        if value == original:
            return True
        return value is None or not isinstance(value, (list, dict)) and bool(pd.isna(value))
    return type(value) in (str, int, float, bool) and value == original


def checked_row_receipt(row):
    """Replay the original receipt and bind it to this row, not its label."""
    from runtime.trade_learning import validate_entry_features
    try:
        raw = row.get(PROOF_COLUMN)
        if raw is None or not isinstance(raw, (str, dict)) and bool(pd.isna(raw)):
            # Pre-column managed v3 exports already retained this same entry
            # receipt inside their checked financial source. Replay it rather
            # than rewriting history or manufacturing a generation marker.
            original = row.get("outcome_execution_proof")
            source = json.loads(original) if isinstance(original, str) else None
            entry = source.get("entry_features") if isinstance(source, dict) else None
            if isinstance(entry, dict) and entry.get("version") in {ENTRY_VERSION, STRATEGY_ENTRY_VERSION}:
                from ml.financial_targets import checked_net_return
                if checked_net_return(row) is not None:
                    raw = entry
        proof = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(proof, dict) or proof.get("version") not in {ENTRY_VERSION, STRATEGY_ENTRY_VERSION}:
            return None
        validate_entry_features(proof, address=row.get("address"))
        if any(not _same_value(name, row.get(name), value) for name, value in proof["vector"].items()):
            return None
        return proof
    except (ValueError, TypeError, KeyError, RuntimeError, OverflowError):
        return None


def checked_model_frame(frame, features):
    """A model with changed inputs only consumes matching causal receipts."""
    from features.context_encoding import FEATURE_SOURCES
    features = list(features)
    strategy = any(FEATURE_SOURCES.get(name) == STRATEGY_SOURCE for name in features)
    if not semantic_sources(features) and not strategy:
        return frame
    records = []
    for row in frame.to_dict(orient="records"):
        proof = checked_row_receipt(row)
        if proof is None or strategy and proof["version"] != STRATEGY_ENTRY_VERSION:
            raise ValueError("Unproved auxiliary input generation")
        restored = {**row, **proof["vector"]}
        if strategy:
            restored[STRATEGY_SOURCE] = proof["strategy_context"][STRATEGY_SOURCE]
        restored["timestamp"] = pd.to_datetime(restored["timestamp"], utc=True)
        records.append(restored)
    columns = list(dict.fromkeys([*frame.columns, *([STRATEGY_SOURCE] if strategy else [])]))
    out = pd.DataFrame(records, columns=columns, index=frame.index)
    out.attrs = deepcopy(frame.attrs)
    return out


def population_proof(frame, *, mode="current_receipts_only"):
    proofs = [checked_row_receipt(row) for row in frame.to_dict(orient="records")]
    current = [proof for proof in proofs if proof is not None]
    return {"version": VERSION, "mode": mode, "rows": len(frame), "current_rows": len(current),
            "unique_tokens": len({proof["vector"]["address"] for proof in current}),
            "population_sha256": sha256(json.dumps(sorted(proof["payload_sha256"] for proof in current),
                separators=(",", ":")).encode()).hexdigest()}


def prepare_training_frame(frame, *, min_current_rows=30):
    """Use a current-generation population, or unchanged historical predictors.

    This affects model learning, not scanner admission. No synthetic new source
    proofs, zero-imputed health observations, or relabelled legacy scores.
    """
    proofs = [checked_row_receipt(row) for row in frame.to_dict(orient="records")]
    positions = [i for i, proof in enumerate(proofs) if proof is not None]
    tokens = {proofs[i]["vector"]["address"] for i in positions}
    minimum = max(30, int(min_current_rows))
    strategy_positions = [i for i in positions if proofs[i]["version"] == STRATEGY_ENTRY_VERSION]
    strategy_tokens = {proofs[i]["vector"]["address"] for i in strategy_positions}
    # New predictors need their own original population, not later SQL labels
    # attached to old v3 rows. Stable v3 auxiliary meanings remain compatible.
    if len(strategy_positions) >= minimum and len(strategy_tokens) >= 30:
        out = checked_model_frame(frame.iloc[strategy_positions].copy(),
            [*MEANINGS, f"t0ctx_{STRATEGY_SOURCE}__unobserved"])
        report = population_proof(out)
    elif len(positions) >= minimum and len(tokens) >= 30:
        out = checked_model_frame(frame.iloc[positions].copy(), list(MEANINGS))
        report = population_proof(out)
    else:
        incompatible = [name for name in frame.columns if name.removeprefix(PREFIX) in MEANINGS]
        out = frame.drop(columns=incompatible).copy()
        report = {"version": VERSION, "mode": "unchanged_historical_inputs_only",
                  "rows": len(frame), "current_rows": len(positions), "unique_tokens": len(tokens),
                  "minimum_current_rows": minimum, "excluded_features": incompatible}
    if not (len(strategy_positions) >= minimum and len(strategy_tokens) >= 30):
        out = out.drop(columns=[name for name in out if name == STRATEGY_SOURCE
            or isinstance(name, str) and name.startswith(f"t0ctx_{STRATEGY_SOURCE}__")], errors="ignore")
    out.attrs["auxiliary_semantics_training"] = report
    return out.reset_index(drop=True), report
