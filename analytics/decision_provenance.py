"""Record only actual model queries, bound to original inputs and loaded bytes."""
from __future__ import annotations

from hashlib import sha256
import json
import math
from numbers import Real

from analytics.inference_scope import record_observation, observation_snapshot, observations_enabled


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def receipt_input_digest(proof):
    """Bind original input meanings, not later journal time or outcome labels."""
    return digest({key: proof[key] for key in
        ("version", "vector", "auxiliary_observations", "strategy_context") if key in proof})


def input_identity(vector):
    from features.auxiliary_semantics import input_frame, checked_row_receipt, PROOF_COLUMN
    from runtime.trade_learning import freeze_entry_features
    frame = input_frame(vector)
    if len(frame) != 1:
        raise ValueError("Decision provenance requires one original input vector")
    row = frame.iloc[0].to_dict()
    frozen = freeze_entry_features(row, address=row.get("address"), captured_at=row.get("timestamp"))
    proof = None
    if row.get(PROOF_COLUMN) is not None:
        proof = checked_row_receipt(row)
        if proof is None:
            raise ValueError("Unproved original decision input receipt")
    return {"input_vector_sha256": digest(frozen["vector"]),
            "input_receipt_sha256": receipt_input_digest(proof) if proof is not None else None,
            "input_captured_at_utc": frozen["vector"]["timestamp"]}


def model_source(model, features, metadata, *, primary_reader=False):
    # The actual reader owns the provenance namespace. Fields persisted inside
    # an unrelated model's metadata cannot select another reader's identity.
    primary = (metadata.get("_primary_runtime") or {}) if primary_reader else {}
    artifact = {} if primary_reader else (metadata.get("_artifact_runtime") or {})
    aliases = {"model.pkl": "model", "model.meta.json": "meta", "threshold.json": "thresholds",
               "thresholds.by_lane.json": "lane_thresholds", "acceptance.json": "acceptance"}
    components = ({aliases[name]: value for name, value in (primary.get("component_sha256") or {}).items()
                   if name in aliases} if primary else
                  {"model": artifact.get("model_sha256"), "meta": artifact.get("metadata_sha256")})
    expected = set(aliases.values()) if primary.get("mode") == "atomic_primary_bundle" else {"model", "meta"}
    checked = model is not None and set(components) == expected and all(isinstance(value, str) and len(value) == 64
        and all(c in "0123456789abcdef" for c in value) for value in components.values())
    source = {"status": "checked_artifact" if checked else "unknown",
        "mode": (primary.get("mode") or artifact.get("mode", "checked_compatibility_artifact")) if checked else "unavailable",
        "component_sha256": components if checked else {},
        "feature_schema_sha256": digest(list(features)) if checked else None,
        "selector_sha256": artifact.get("manifest_sha256") if checked else None,
        "revision": (primary.get("revision") if primary else artifact.get("version")) if checked else None,
        "same_training_cohort_asserted": False}
    source["identity_sha256"] = digest(source)
    return source


def record_model_query(vector, *, family, target, operation, value, model, features, metadata, primary_reader=False):
    """Telemetry failures cannot change the prediction or admission decision."""
    if not observations_enabled():
        return
    try:
        # NumPy scalars returned by a model are measurements, not JSON-native
        # values. Preserve the finite value without weakening persisted proof
        # validation or admitting booleans, arrays and numeric strings.
        if value is not None:
            if operation == "diagnostic_label" and isinstance(value, str):
                value = str(value)
            elif isinstance(value, Real) and not isinstance(value, bool):
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError("Nonfinite model observation")
            else:
                raise ValueError("Unsupported model observation")
        identity = input_identity(vector)
        record_observation({**identity, "family": family, "target": target, "operation": operation,
                            "value": value, "source": model_source(model, features, metadata, primary_reader=primary_reader)})
    except (ValueError, TypeError, KeyError, RuntimeError, AttributeError, OverflowError):
        return


def model_query_snapshot(vector):
    identity = input_identity(vector)
    return {**identity, **observation_snapshot(identity["input_vector_sha256"], identity["input_receipt_sha256"])}
