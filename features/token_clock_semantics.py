"""Original normalised token/queue clocks at T0, never provider authentication.

Receipt metadata is not a predictor. Legacy ages are not relabelled; unchanged
historical inputs remain trainable when this generation has too little support.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

import numpy as np
import pandas as pd

from analytics.token_time import (AGE_SEMANTICS_VERSION, BIRTH_CLOCK_FIELDS,
    _to_datetime, _to_float, compute_age_minutes, compute_queue_age_minutes)

VERSION = AGE_SEMANTICS_VERSION
PROOF_COLUMN = "t0_token_clock_proof"
AGE_FEATURES = ("age_minutes", "queue_age_minutes")
CLOCK_FIELDS = (*BIRTH_CLOCK_FIELDS, "first_seen_epoch_s", "first_seen_at")
MEASURED_FIELDS = ("age_minutes", "age_min", "token_age_min", "queue_age_minutes", "minutes_since_first_seen")
INPUT_ONLY_FIELDS = (*CLOCK_FIELDS, "age_min", "token_age_min", "minutes_since_first_seen",
                     "age_at_seen", "shadow_age_min", "age_since_seen_min")
BASIS = "original_normalised_T0_inputs_not_provider_birth_authentication"


def _hash(payload):
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def clock_sources(features):
    from features.numeric_encoding import PREFIX
    return sorted({name.removeprefix(PREFIX) for name in features if isinstance(name, str)
                   and name.removeprefix(PREFIX) in AGE_FEATURES + INPUT_ONLY_FIELDS})


def _inputs(token):
    clocks = {name: parsed.isoformat() if (parsed := _to_datetime(token.get(name))) is not None else None
              for name in CLOCK_FIELDS}
    return {**clocks, **{name: _to_float(token.get(name)) for name in MEASURED_FIELDS}}


def _ages(inputs, stamp):
    return {"age_minutes": compute_age_minutes(inputs, now=stamp),
            "queue_age_minutes": compute_queue_age_minutes(inputs, now=stamp)}


def _same_age(value, original):
    from features.numeric_encoding import numeric_value
    if value is None:
        return original is None
    if isinstance(value, (float, np.floating)) and np.isnan(value):
        return original is None
    if value is pd.NA:
        return original is None
    if original is None:
        return False
    parsed = numeric_value(value, "age_minutes")
    return parsed is not None and (parsed == original or parsed == float(np.float32(original)))


def validate_clock_proof(proof, row):
    keys = {"version", "basis", "address", "captured_at", "inputs", "ages", "payload_sha256"}
    stamp = _to_datetime(proof.get("captured_at")) if isinstance(proof, dict) else None
    row_stamp = _to_datetime(row.get("timestamp"))
    if (not isinstance(proof, dict) or set(proof) != keys or proof.get("version") != VERSION
            or proof.get("basis") != BASIS or stamp is None or row_stamp != stamp
            or not isinstance(proof.get("address"), str) or not proof["address"].strip()
            or proof["address"] != row.get("address") or proof["captured_at"] != stamp.isoformat()
            or not isinstance(proof.get("inputs"), dict)
            or set(proof["inputs"]) != set(CLOCK_FIELDS + MEASURED_FIELDS)
            or any(value is not None and type(value) is not str for name, value in proof["inputs"].items() if name in CLOCK_FIELDS)
            or any(value is not None and type(value) is not float for name, value in proof["inputs"].items() if name in MEASURED_FIELDS)
            or proof["inputs"] != _inputs(proof["inputs"])
            or not isinstance(proof.get("ages"), dict) or set(proof["ages"]) != set(AGE_FEATURES)
            or proof["payload_sha256"] != _hash({key: value for key, value in proof.items() if key != "payload_sha256"})):
        raise ValueError("Invalid original token clock proof")
    expected = _ages(proof["inputs"], stamp)
    for name in AGE_FEATURES:
        value = proof["ages"][name]
        if (value is not None and type(value) is not float or value != expected[name]
                or not _same_age(row.get(name), expected[name])):
            raise ValueError("Original token clock age/T0 conflicts")
    return proof


def capture_clock_proof(vector, token):
    row = vector.to_dict() if hasattr(vector, "to_dict") else dict(vector)
    stamp = _to_datetime(row.get("timestamp"))
    if stamp is None:
        raise ValueError("Missing original token clock T0")
    inputs = _inputs(token)
    proof = {"version": VERSION, "basis": BASIS, "address": row.get("address"),
             "captured_at": stamp.isoformat(), "inputs": inputs, "ages": _ages(inputs, stamp)}
    proof["payload_sha256"] = _hash(proof)
    return validate_clock_proof(proof, row)


def bind_vector_clock(vector, token):
    try:
        proof = capture_clock_proof(vector, token)
        vector.attrs[PROOF_COLUMN] = json.dumps(proof, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, KeyError, OverflowError):
        # Missing identity or invalid original inputs cannot certify a vector.
        vector.attrs.pop(PROOF_COLUMN, None)
    return vector


def _receipt(raw):
    if raw is None or raw is pd.NA or isinstance(raw, (float, np.floating)) and np.isnan(raw):
        return None
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(parsed, dict):
        raise ValueError("Malformed declared original receipt")
    return parsed


def checked_row_clock(row):
    try:
        proof = _receipt(row.get(PROOF_COLUMN))
        from features.auxiliary_semantics import PROOF_COLUMN as ENTRY_PROOF_COLUMN
        entry = _receipt(row.get(ENTRY_PROOF_COLUMN))
        source = _receipt(row.get("outcome_execution_proof"))
        if source is not None:
            from ml.financial_targets import checked_net_return
            if checked_net_return(row) is None:
                return None
            original_entry = source.get("entry_features")
            if entry is not None and entry != original_entry:
                return None
            entry = original_entry
        if entry is not None:
            from runtime.trade_learning import validate_entry_features
            validate_entry_features(entry, address=row.get("address"))
            original_clock = entry.get("token_clock")
            # A later standalone marker cannot upgrade a retained legacy entry
            # or contradict its original pre-buy/financial clock receipt.
            if original_clock is None or proof is not None and proof != original_clock:
                return None
            proof = original_clock
        return deepcopy(validate_clock_proof(proof, row))
    except (ValueError, TypeError, KeyError, RuntimeError, OverflowError):
        return None


def checked_model_frame(frame, features):
    sources = clock_sources(features)
    if not sources:
        return frame
    if any(name not in AGE_FEATURES for name in sources):
        raise ValueError("Unsupported token clock predictor alias")
    records = []
    for row in frame.to_dict(orient="records"):
        proof = checked_row_clock(row)
        if proof is None:
            raise ValueError("Unproved original token clock input generation")
        records.append({**row, **proof["ages"]})
    out = pd.DataFrame(records, columns=frame.columns, index=frame.index)
    out.attrs = deepcopy(frame.attrs)
    return out


def population_proof(frame):
    current = [proof for row in frame.to_dict(orient="records") if (proof := checked_row_clock(row)) is not None]
    return {"version": VERSION, "mode": "original_clock_receipts_only", "rows": len(frame),
            "current_rows": len(current), "unique_tokens": len({proof["address"] for proof in current}),
            "population_sha256": _hash(sorted(proof["payload_sha256"] for proof in current))}


def checked_population(metadata, features):
    sources = clock_sources(features)
    if not sources:
        return True
    if any(name not in AGE_FEATURES for name in sources):
        return False
    proof = metadata.get("token_clock_training")
    keys = {"version", "mode", "rows", "current_rows", "unique_tokens", "population_sha256"}
    if not isinstance(proof, dict) or set(proof) != keys:
        return False
    rows, tokens, digest = proof.get("rows"), proof.get("unique_tokens"), proof.get("population_sha256")
    return (proof.get("version") == VERSION and proof.get("mode") == "original_clock_receipts_only"
            and type(rows) is int and rows >= 30 and type(proof.get("current_rows")) is int
            and proof["current_rows"] == rows and type(tokens) is int and 30 <= tokens <= rows
            and metadata.get("rows", rows) == rows and metadata.get("target_rows", rows) == rows
            and isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest))


def prepare_training_frame(frame, *, min_current_rows=30):
    from features.numeric_encoding import PREFIX
    proofs = [checked_row_clock(row) for row in frame.to_dict(orient="records")]
    positions = [i for i, proof in enumerate(proofs) if proof is not None]
    tokens = {proofs[i]["address"] for i in positions}
    minimum = max(30, int(min_current_rows))
    aliases = [name for name in frame if isinstance(name, str) and name.removeprefix(PREFIX) in INPUT_ONLY_FIELDS]
    frame = frame.drop(columns=aliases).copy()
    relevant = [name for name in frame if isinstance(name, str) and name.removeprefix(PREFIX) in AGE_FEATURES]
    if relevant and len(positions) >= minimum and len(tokens) >= 30:
        out = checked_model_frame(frame.iloc[positions].copy(), relevant)
        report = population_proof(out)
    else:
        out = frame.drop(columns=relevant).copy()
        report = {"version": VERSION, "mode": "unchanged_nonclock_inputs_only", "rows": len(out),
                  "current_rows": len(positions), "unique_tokens": len(tokens),
                  "minimum_current_rows": minimum, "excluded_features": relevant}
    out.attrs["token_clock_training"] = report
    report["excluded_clock_aliases"] = aliases
    return out.reset_index(drop=True), report
