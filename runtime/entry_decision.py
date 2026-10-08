"""Durable original decision provenance, explicitly not strategy acceptance.

Models/policy/selected entry controls are captured before submission. Routes,
capital ownership, provider depth, live finality and prospective profitability
still require their independent proofs; this receipt cannot certify them.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re

from analytics.decision_provenance import digest, receipt_input_digest, model_query_snapshot

VERSION = "original_entry_decision_provenance_v1"
ROLE = "decision_provenance_only"
SHA = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
COVERAGE = ("model_queries_for_final_vector", "ml_admission_decision", "paper_entry_binding",
            "entry_size_and_exit_profile")
ML_FIELDS = frozenset({"mode", "lane", "proba", "threshold", "allow_buy", "enforce", "sizing_multiplier",
    "risk_veto", "reason", "activation_ready", "source", "risk_proba", "ev_pred_pct", "edge_score",
    "risk_veto_enforced"})
DRAFT_KEYS = frozenset({"version", "role", "buy_permission", "full_strategy_profitability_established",
    "coverage", "coverage_complete", "input_vector_sha256", "input_receipt_sha256", "input_captured_at_utc", "model_queries",
    "ml_policy", "paper_entry_policy", "entry"})
BINDING_KEYS = frozenset({"intent_id", "run_id", "journaled_at_utc", "entry_features_sha256", "payload_sha256"})


def _text(value, *, nullable=False, maximum=256):
    return (nullable and value is None) or (isinstance(value, str) and 0 < len(value) <= maximum
                                          and not any(ord(c) < 32 for c in value))


def _number(value, *, nullable=False):
    return nullable and value is None or type(value) in (int, float) and math.isfinite(value)


def _hash(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def _validate_policy(policy):
    if policy is None:
        return
    if not isinstance(policy, dict) or set(policy) != ML_FIELDS:
        raise ValueError("Unsupported original ML policy")
    for name in ("allow_buy", "enforce", "risk_veto", "activation_ready", "risk_veto_enforced"):
        if type(policy[name]) is not bool:
            raise ValueError("Invalid original ML flag")
    for name in ("mode", "lane", "reason", "source"):
        if not _text(policy[name]):
            raise ValueError("Invalid original ML descriptor")
    for name in ("proba", "threshold", "risk_proba"):
        value = policy[name]
        if not _number(value, nullable=True) or value is not None and not 0 <= value <= 1:
            raise ValueError("Invalid original ML probability")
    for name in ("sizing_multiplier", "ev_pred_pct", "edge_score"):
        if not _number(policy[name], nullable=name != "sizing_multiplier"):
            raise ValueError("Invalid original ML numeric decision")
    if policy["sizing_multiplier"] < 0:
        raise ValueError("Invalid original ML size multiplier")


def _validate_binding(binding, *, paper):
    if binding is None:
        return
    from runtime.paper_entry_policy import THRESHOLDS, PREFIXES
    if not paper or not isinstance(binding, dict) or binding.get("full_strategy_profitability_established") is not False:
        raise ValueError("Invalid original paper binding")
    version = binding.get("version")
    if version == "paper_entry_composition_v1":
        if set(binding) != {"version", "role", "components", "parameters", "full_strategy_profitability_established"} or binding["role"] != "paper_entry_components_only":
            raise ValueError("Invalid original paper composition")
        components = binding["components"]
    elif version == "paper_entry_thresholds_v1":
        if set(binding) != {"version", "role", "gate", "revision", "configured_hash", "parameters", "evidence_sha256", "full_strategy_profitability_established"} or binding["role"] != "paper_entry_gate_only":
            raise ValueError("Invalid original paper component")
        components = {binding["gate"]: {k: binding[k] for k in ("revision", "configured_hash", "parameters", "evidence_sha256")}}
    else:
        raise ValueError("Unsupported original paper binding")
    if not isinstance(components, dict) or not 1 <= len(components) <= len(PREFIXES):
        raise ValueError("Invalid original paper components")
    combined = {}
    for gate, component in components.items():
        if (gate not in PREFIXES or not isinstance(component, dict)
                or set(component) != {"revision", "configured_hash", "parameters", "evidence_sha256"}
                or not _text(component["revision"], maximum=128) or not _hash(component["configured_hash"])
                or component["evidence_sha256"] != "" and not _hash(component["evidence_sha256"])):
            raise ValueError("Invalid original paper component identity")
        parameters = component["parameters"]
        if not isinstance(parameters, dict) or not 1 <= len(parameters) <= 2:
            raise ValueError("Invalid original paper parameters")
        for name, value in parameters.items():
            rule = THRESHOLDS.get(name)
            if rule is None or rule.gate != gate or not _number(value) or not rule.minimum <= value <= rule.maximum:
                raise ValueError("Invalid original paper parameter envelope")
            combined[name] = value
    if binding["parameters"] != combined:
        raise ValueError("Original paper parameters conflict")


def _validate_queries(queries, identity):
    if (not isinstance(queries, dict) or set(queries) != {"status", "observations", "dropped"}
            or queries["status"] not in {"captured", "scope_missing"}
            or type(queries["dropped"]) is not int or queries["dropped"] < 0
            or not isinstance(queries["observations"], list) or len(queries["observations"]) > 256
            or queries["status"] == "scope_missing" and (queries["observations"] or queries["dropped"])):
        raise ValueError("Invalid original model-query population")
    seen = set()
    for record in queries["observations"]:
        if (not isinstance(record, dict) or set(record) != {"family", "target", "operation", "value", "source", *identity}
                or any(record.get(key) != value for key, value in identity.items())
                or any(not isinstance(record.get(key), str) or not NAME.fullmatch(record[key]) for key in ("family", "target"))
                or record["operation"] not in {"probability", "regression", "ranking_percentile", "diagnostic_label", "prediction"}):
            raise ValueError("Invalid original model query")
        source = record["source"]
        if (not isinstance(source, dict) or set(source) != {"status", "mode", "component_sha256", "feature_schema_sha256",
                "selector_sha256", "revision", "same_training_cohort_asserted", "identity_sha256"}
                or source["status"] not in {"checked_artifact", "unknown"}
                or source["mode"] not in {"atomic_primary_bundle", "checked_legacy_artifact", "legacy_flat", "manifest",
                                           "checked_compatibility_artifact", "unavailable"}
                or source["same_training_cohort_asserted"] is not False
                or source["identity_sha256"] != digest({k: v for k, v in source.items() if k != "identity_sha256"})):
            raise ValueError("Invalid original model source")
        components = source["component_sha256"]
        if source["status"] == "checked_artifact":
            expected = {"model", "meta", "thresholds", "lane_thresholds", "acceptance"} if source["mode"] == "atomic_primary_bundle" else {"model", "meta"}
            if (source["mode"] == "unavailable" or not isinstance(components, dict) or set(components) != expected
                    or not all(_hash(value) for value in components.values()) or not _hash(source["feature_schema_sha256"])
                    or source["selector_sha256"] is not None and not _hash(source["selector_sha256"])
                    or source["revision"] is not None and not (_text(source["revision"], maximum=128) or type(source["revision"]) is int and source["revision"] >= 0)):
                raise ValueError("Invalid checked model identity")
            if source["mode"] == "manifest":
                if not _hash(source["selector_sha256"]) or not _text(source["revision"], maximum=128):
                    raise ValueError("Unproved original head selector")
            elif source["selector_sha256"] is not None:
                raise ValueError("Invented original head selector")
            if source["mode"] == "atomic_primary_bundle":
                if type(source["revision"]) is not int or source["revision"] < 0:
                    raise ValueError("Unproved original primary selector")
            elif source["mode"] != "manifest" and source["revision"] is not None:
                raise ValueError("Invented original approval revision")
        elif (components != {} or source["mode"] != "unavailable" or source["feature_schema_sha256"] is not None
              or source["selector_sha256"] is not None or source["revision"] is not None or record["value"] is not None):
            raise ValueError("Unknown model cannot claim a prediction or identity")
        value, operation = record["value"], record["operation"]
        if value is not None:
            if operation == "diagnostic_label":
                if not _text(value, maximum=128): raise ValueError("Invalid original diagnostic label")
            elif not _number(value): raise ValueError("Invalid original numeric prediction")
            elif operation == "probability" and not 0 <= value <= 1: raise ValueError("Invalid original probability")
            elif operation == "ranking_percentile" and not 0 <= value <= 100: raise ValueError("Invalid original rank percentile")
        key = (record["family"], record["target"], operation, source["identity_sha256"])
        if key in seen: raise ValueError("Duplicate original model query")
        seen.add(key)


def capture_entry_decision(vector, token, *, paper, amount_sol, ml_policy=None, paper_bypass=False):
    from runtime.paper_entry_policy import snapshot
    from features.strategy_context import capture_strategy_context
    captured = model_query_snapshot(vector)
    identity = {key: captured.pop(key) for key in ("input_vector_sha256", "input_receipt_sha256", "input_captured_at_utc")}
    policy = ml_policy.to_dict() if ml_policy is not None else None
    draft = {"version": VERSION, "role": ROLE, "buy_permission": False,
        "full_strategy_profitability_established": False, "coverage_complete": False,
        "coverage": list(COVERAGE), **identity, "model_queries": captured, "ml_policy": deepcopy(policy),
        "paper_entry_policy": snapshot(), "entry": {"paper": paper, "amount_sol": amount_sol,
            "entry_lane": token.get("entry_lane"), "entry_subprofile": capture_strategy_context(token)["entry_subprofile"],
            "runner_exit_profile": token.get("runner_exit_profile"), "config_hash": token.get("config_hash"),
            "paper_admission_bypass": bool(paper and paper_bypass)}}
    validate_draft(draft)
    return draft


def validate_draft(draft):
    if (not isinstance(draft, dict) or set(draft) != DRAFT_KEYS or draft["version"] != VERSION or draft["role"] != ROLE
            or draft["buy_permission"] is not False or draft["full_strategy_profitability_established"] is not False
            or draft["coverage_complete"] is not False or draft["coverage"] != list(COVERAGE)
            or draft["input_receipt_sha256"] is not None and not _hash(draft["input_receipt_sha256"])
            or not _hash(draft["input_vector_sha256"]) or not _text(draft["input_captured_at_utc"], maximum=80)):
        raise ValueError("Invalid original decision provenance")
    entry = draft["entry"]
    if (not isinstance(entry, dict) or set(entry) != {"paper", "amount_sol", "entry_lane", "entry_subprofile",
                "runner_exit_profile", "config_hash", "paper_admission_bypass"}
            or type(entry["paper"]) is not bool or type(entry["paper_admission_bypass"]) is not bool
            or not _number(entry["amount_sol"]) or entry["amount_sol"] <= 0
            or entry["paper"] and entry["amount_sol"] != .1
            or not entry["paper"] and entry["paper_admission_bypass"]
            or any(not _text(entry[name], nullable=True, maximum=128) for name in
                   ("entry_lane", "entry_subprofile", "runner_exit_profile", "config_hash"))):
        raise ValueError("Invalid original entry controls")
    _validate_policy(draft["ml_policy"])
    _validate_binding(draft["paper_entry_policy"], paper=entry["paper"])
    _validate_queries(draft["model_queries"], {key: draft[key] for key in
        ("input_vector_sha256", "input_receipt_sha256", "input_captured_at_utc")})


def bind_entry_decision(draft, row):
    receipt = {**deepcopy(draft), "intent_id": row["intent_id"], "run_id": row["base_position"].get("run_id"),
        "journaled_at_utc": row["created_at"], "entry_features_sha256": row["entry_features"]["payload_sha256"]}
    receipt["payload_sha256"] = digest(receipt)
    validate_entry_decision(receipt, entry_features=row["entry_features"], intent_id=row["intent_id"],
        run_id=row["base_position"].get("run_id"), paper=row["paper"], amount_sol=row["amount_sol"])
    return receipt


def validate_entry_decision(receipt, *, entry_features, intent_id, run_id, paper, amount_sol):
    from runtime.trade_learning import validate_entry_features, _time
    if not isinstance(receipt, dict) or set(receipt) != DRAFT_KEYS | BINDING_KEYS:
        raise ValueError("Invalid bound entry decision")
    validate_draft({key: receipt[key] for key in DRAFT_KEYS})
    validate_entry_features(entry_features, address=entry_features["vector"].get("address"))
    if (receipt["intent_id"] != intent_id or receipt["run_id"] != run_id
            or receipt["journaled_at_utc"] != entry_features["captured_at"]
            or receipt["entry_features_sha256"] != entry_features["payload_sha256"]
            or receipt["input_vector_sha256"] != digest(entry_features["vector"])
            or receipt["input_receipt_sha256"] is not None and receipt["input_receipt_sha256"] != receipt_input_digest(entry_features)
            or _time(receipt["input_captured_at_utc"]) != _time(entry_features["vector"]["timestamp"])
            or _time(receipt["input_captured_at_utc"]) > _time(receipt["journaled_at_utc"])
            or receipt["entry"]["paper"] is not paper or receipt["entry"]["amount_sol"] != amount_sol
            or receipt["payload_sha256"] != digest({k: v for k, v in receipt.items() if k != "payload_sha256"})):
        raise ValueError("Entry decision identity/input/journal conflicts")
    original_context = entry_features.get("strategy_context")
    if original_context is not None and receipt["entry"]["entry_subprofile"] != original_context["entry_subprofile"]:
        raise ValueError("Entry decision original subprofile conflicts")


def checked_entry_decision(row):
    """Replay the checked original financial source, never a mutable column."""
    from ml.financial_targets import checked_net_return
    if checked_net_return(row) is None:
        return None
    source = json.loads(row["outcome_execution_proof"])
    return deepcopy(source.get("entry_decision"))
