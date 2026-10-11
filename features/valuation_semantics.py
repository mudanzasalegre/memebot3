"""Original T0 cap generation, distinct from historical cap/FDV conflation.

Receipt metadata is not a predictor and does not authenticate a provider's
circulating supply. Stable historical predictors remain useful without it.
"""
from copy import deepcopy
from hashlib import sha256
import json

import numpy as np
import pandas as pd

from utils.market_observation import (OBSERVATION_VERSION, VALUATION_VERSION,
    VALUATION_MEANINGS, fresh_market_value, market_number)

VERSION = VALUATION_VERSION
PROOF_COLUMN = "t0_valuation_proof"
FEATURES = ("market_cap_usd", "mcap_bucket", "mcap_bucket_code")
BASIS = "original_normalised_T0_reported_cap_not_fdv_or_supply_derived"


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def sources(features):
    from features.numeric_encoding import PREFIX
    return sorted({name.removeprefix(PREFIX) for name in features if isinstance(name, str)
                   and name.removeprefix(PREFIX) in FEATURES})


def _time(value):
    if isinstance(value, (bool, np.bool_)) or value is None:
        raise ValueError("Invalid valuation T0")
    stamp = pd.to_datetime(value, utc=True, errors="raise")
    if not isinstance(stamp, pd.Timestamp) or pd.isna(stamp):
        raise ValueError("Invalid valuation T0")
    return stamp


def _same(value, original):
    if value is None or value is pd.NA or isinstance(value, (float, np.floating)) and np.isnan(value):
        return original is None
    if original is None or isinstance(value, (bool, np.bool_)):
        return False
    parsed = market_number(value, "market_cap_usd")
    return parsed is not None and (parsed == original or parsed == float(np.float32(original)))


def validate_proof(proof, row):
    keys = {"version", "basis", "address", "captured_at", "market_observation", "values", "payload_sha256"}
    if (not isinstance(proof, dict) or set(proof) != keys or proof["version"] != VERSION
            or proof["basis"] != BASIS or not isinstance(proof["address"], str) or not proof["address"]
            or row.get("address") != proof["address"] or _time(row.get("timestamp")) != _time(proof["captured_at"])
            or proof["captured_at"] != _time(proof["captured_at"]).isoformat()
            or proof["payload_sha256"] != _hash({k: v for k, v in proof.items() if k != "payload_sha256"})):
        raise ValueError("Invalid original valuation proof")
    values, market = proof["values"], proof["market_observation"]
    if (not isinstance(values, dict) or set(values) != set(FEATURES)
            or values["market_cap_usd"] is not None and type(values["market_cap_usd"]) is not float
            or not isinstance(market, dict) or market.get("version") != OBSERVATION_VERSION
            or market.get("address") != proof["address"] or market.get("valuation_semantics") != VERSION
            or market.get("basis") != "http_response_received_not_provider_market_asof"
            or not isinstance(market.get("fields"), dict) or set(market["fields"]) - {"market_cap_usd"}):
        raise ValueError("Unsupported original valuation meaning")
    record = market["fields"].get("market_cap_usd")
    cap = values["market_cap_usd"]
    if cap is None:
        if "market_cap_usd" in market["fields"]:
            raise ValueError("Unknown cap conflicts with a retained observation")
    elif (not isinstance(record, dict) or set(record) != {"source", "received_at", "value", "meaning"}
            or not isinstance(record["source"], str) or not record["source"].strip()
            or type(record["received_at"]) not in (int, float)
            or record["meaning"] != VALUATION_MEANINGS["market_cap_usd"]
            or fresh_market_value({"address": proof["address"], "market_cap_usd": cap,
                "market_observation": market}, "market_cap_usd", now=_time(proof["captured_at"]).timestamp()) != cap
            or record["received_at"] > _time(proof["captured_at"]).timestamp()):
        raise ValueError("Unobserved, stale or noncausal original cap")
    from features.builder import _mcap_bucket
    bucket, code = _mcap_bucket(cap)
    if (values["mcap_bucket"] != bucket or type(values["mcap_bucket_code"]) is not int
            or values["mcap_bucket_code"] != code or not _same(row.get("market_cap_usd"), cap)
            or row.get("mcap_bucket") != bucket or not _same(row.get("mcap_bucket_code"), float(code))):
        raise ValueError("Original cap/bucket/T0 conflicts")
    return proof


def bind_vector(vector, token):
    vector.attrs.pop(PROOF_COLUMN, None)
    try:
        market = deepcopy(token.get("market_observation"))
        if not isinstance(market, dict):
            return vector
        fields = market.get("fields")
        market["fields"] = ({"market_cap_usd": deepcopy(fields["market_cap_usd"])}
            if isinstance(fields, dict) and "market_cap_usd" in fields else {})
        from features.builder import _mcap_bucket
        cap = market_number(token.get("market_cap_usd"), "market_cap_usd")
        bucket, code = _mcap_bucket(cap)
        proof = {"version": VERSION, "basis": BASIS, "address": vector.get("address"),
                 "captured_at": _time(vector.get("timestamp")).isoformat(), "market_observation": market,
                 "values": {"market_cap_usd": cap, "mcap_bucket": bucket, "mcap_bucket_code": code}}
        proof["payload_sha256"] = _hash(proof)
        validate_proof(proof, vector.to_dict())
        vector.attrs[PROOF_COLUMN] = json.dumps(proof, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, KeyError, OverflowError):
        vector.attrs.pop(PROOF_COLUMN, None)
    return vector


def _receipt(value):
    if value is None or value is pd.NA or isinstance(value, (float, np.floating)) and np.isnan(value):
        return None
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError("Malformed valuation source")
    return parsed


def checked_row(row):
    try:
        standalone = _receipt(row.get(PROOF_COLUMN))
        entry = _receipt(row.get("t0_auxiliary_semantics_proof"))
        financial = _receipt(row.get("outcome_execution_proof"))
        financial_entry = financial.get("entry_features") if isinstance(financial, dict) else None
        if (standalone is None and (not isinstance(entry, dict) or "valuation" not in entry)
                and (not isinstance(financial_entry, dict) or "valuation" not in financial_entry)):
            return None  # Legacy inputs cannot supply this generation; no financial relabelling.
        if financial is not None:
            from ml.financial_targets import checked_net_return
            if checked_net_return(row) is None:
                return None
            original = financial.get("entry_features")
            if entry is not None and entry != original:
                return None
            entry = original
        if entry is not None:
            from runtime.trade_learning import validate_entry_features
            validate_entry_features(entry, address=row.get("address"))
            original = entry.get("valuation")
            if original is None or standalone is not None and standalone != original:
                return None  # A later standalone proof cannot upgrade a legacy entry.
            standalone = original
        return deepcopy(validate_proof(standalone, row))
    except (ValueError, TypeError, KeyError, RuntimeError, OverflowError):
        return None


def checked_model_frame(frame, features):
    if sources(features) and any(checked_row(row) is None for row in frame.to_dict(orient="records")):
        raise ValueError("Unproved original cap input generation")
    return frame


def population_proof(frame):
    current = [proof for row in frame.to_dict(orient="records") if (proof := checked_row(row)) is not None]
    return {"version": VERSION, "mode": "original_valuation_receipts_only", "rows": len(frame),
            "current_rows": len(current), "unique_tokens": len({proof["address"] for proof in current}),
            "population_sha256": _hash(sorted(proof["payload_sha256"] for proof in current))}


def checked_population(metadata, features):
    if not sources(features):
        return True
    proof = metadata.get("valuation_training")
    if not isinstance(proof, dict) or set(proof) != {"version", "mode", "rows", "current_rows", "unique_tokens", "population_sha256"}:
        return False
    rows, tokens, digest = proof.get("rows"), proof.get("unique_tokens"), proof.get("population_sha256")
    return (proof.get("version") == VERSION and proof.get("mode") == "original_valuation_receipts_only"
            and type(rows) is int and rows >= 30 and type(proof.get("current_rows")) is int and proof["current_rows"] == rows
            and type(tokens) is int and 30 <= tokens <= rows and metadata.get("rows", rows) == rows
            and metadata.get("target_rows", rows) == rows and isinstance(digest, str) and len(digest) == 64
            and all(c in "0123456789abcdef" for c in digest))


def prepare_training_frame(frame, *, min_current_rows=30):
    relevant = [name for name in frame if sources([name])]
    proofs = [checked_row(row) for row in frame.to_dict(orient="records")]
    positions = [i for i, proof in enumerate(proofs) if proof is not None]
    tokens = {proofs[i]["address"] for i in positions}
    minimum = max(30, int(min_current_rows))
    if relevant and len(positions) >= minimum and len(tokens) >= 30:
        out = checked_model_frame(frame.iloc[positions].copy(), relevant)
        report = population_proof(out)
    else:
        out = frame.drop(columns=relevant).copy()
        report = {"version": VERSION, "mode": "unchanged_nonvaluation_inputs_only", "rows": len(out),
                  "current_rows": len(positions), "unique_tokens": len(tokens),
                  "minimum_current_rows": minimum, "excluded_features": relevant}
    out.attrs["valuation_training"] = report
    return out.reset_index(drop=True), report
