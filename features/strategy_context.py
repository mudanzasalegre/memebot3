"""Original selected entry subprofile, bound by the pre-buy T0 receipt.

This is not an outcome label or permission to buy. Historical reasons, gates,
SQL positions and arbitrary dataset columns cannot reconstruct the selection.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

VERSION = "selected_entry_subprofile_v1"
ENTRY_VERSION = "frozen_entry_features_with_strategy_context_v4"
SOURCE = "entry_subprofile"
VALUES = (
    "sniper_research_momentum_ignition", "sniper_research_deep_reversal",
    "sniper_research_micro_fallback", "paper_exploration_quota", "paper_bootstrap",
)


def _value(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid original entry subprofile")
    return value.strip() or None


def capture_strategy_context(token):
    """Capture explicit producer aliases, never infer a choice from its reason."""
    selected = _value(token.get(SOURCE))
    alias = _value(token.get("sniper_research_subprofile"))
    if selected is not None and alias is not None and selected != alias:
        raise ValueError("Conflicting original entry subprofile aliases")
    return {"version": VERSION, SOURCE: selected if selected is not None else alias}


def validate_strategy_context(context):
    if (not isinstance(context, dict) or set(context) != {"version", SOURCE}
            or context.get("version") != VERSION
            or _value(context.get(SOURCE)) != context.get(SOURCE)):
        raise ValueError("Invalid original strategy context")
    return context


def original_context(row):
    from features.auxiliary_semantics import checked_row_receipt
    proof = checked_row_receipt(row)
    return proof if proof is not None and proof["version"] == ENTRY_VERSION else None


def context_values(frame):
    """Mutable physical columns and precomputed indicators are not evidence."""
    proofs = [original_context(row) for row in frame.to_dict(orient="records")]
    return [proof["strategy_context"][SOURCE] if proof else None for proof in proofs], proofs


def entry_strategy_context(vector, token):
    """Transport the inference vector's original choice into the durable journal."""
    from features.auxiliary_semantics import input_frame
    proof = original_context(input_frame(vector).iloc[0].to_dict())
    if proof is None:
        return None  # Old callers keep their original v1-v3 contract.
    context = capture_strategy_context(token)
    if context != proof["strategy_context"]:
        raise ValueError("Entry subprofile changed after its T0 vector was captured")
    return deepcopy(context)


def population_proof(frame):
    _, proofs = context_values(frame)
    current = [proof for proof in proofs if proof is not None]
    return {"version": VERSION, "mode": "original_v4_receipts_only",
        "entry_receipt_version": ENTRY_VERSION, "rows": len(frame), "current_rows": len(current),
        "unique_tokens": len({proof["vector"]["address"] for proof in current}),
        "population_sha256": sha256(json.dumps(sorted(proof["payload_sha256"] for proof in current),
            separators=(",", ":")).encode()).hexdigest()}


def checked_population(metadata):
    proof = metadata.get("strategy_context_training") or {}
    rows, tokens, digest = proof.get("rows"), proof.get("unique_tokens"), proof.get("population_sha256")
    return (set(proof) == {"version", "mode", "entry_receipt_version", "rows", "current_rows",
                          "unique_tokens", "population_sha256"}
        and proof.get("version") == VERSION and proof.get("mode") == "original_v4_receipts_only"
        and proof.get("entry_receipt_version") == ENTRY_VERSION
        and type(rows) is int and rows >= 30 and proof.get("current_rows") == rows
        and type(tokens) is int and 30 <= tokens <= rows
        and metadata.get("target_rows", rows) == rows and metadata.get("rows", rows) == rows
        and isinstance(digest, str) and len(digest) == 64
        and all(value in "0123456789abcdef" for value in digest))
