"""Task-local handoff of public execution evidence to the existing durable owner."""
from __future__ import annotations

import contextvars
import copy
from contextlib import contextmanager

from execution import chain_reconciliation as chain
from utils.raw_units import sol_to_lamports

_observer = contextvars.ContextVar("original-execution-evidence-owner", default=None)


@contextmanager
def execution_scope(observer):
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def record(stage, evidence):
    observer = _observer.get()
    if observer is not None:
        observer(stage, evidence)


def validate_journal(row, *, side):
    evidence = row.get("execution")
    if evidence is None:
        return None  # Existing historical journals have unknown chain provenance.
    if not isinstance(evidence, dict) or "capsule" not in evidence:
        raise ValueError("Original execution journal is incomplete")
    order, binding = chain.validate_capsule(evidence["capsule"])
    request = order.request
    if row["paper"]:
        raise ValueError("Paper intent cannot own a live submission")
    before = row["base_position"] if side == "buy" else row["before"]
    mint = before.get("token_mint") or row["address"]
    if side == "buy":
        if request.input_mint != chain.SOL or request.output_mint != mint or request.amount != sol_to_lamports(row["amount_sol"]):
            raise ValueError("Original execution differs from buy intent")
    elif side == "sell":
        if request.input_mint != mint or request.output_mint != chain.SOL or request.amount != row["quantity"]:
            raise ValueError("Original execution differs from sell intent")
    else:
        raise ValueError("Unknown execution owner side")
    if "dispatch_started" in evidence and evidence["dispatch_started"] != {"capsule_sha256": evidence["capsule"]["sha256"]}:
        raise ValueError("Original dispatch boundary changed")
    if "provider_response" in evidence:
        if "dispatch_started" not in evidence:
            raise ValueError("Provider response has no original dispatch boundary")
        from execution.jupiter_managed_contract import check_execution
        check_execution(evidence["provider_response"], order, binding)
    if "chain_receipt" in evidence:
        chain.validate_receipt(evidence["capsule"], evidence["provider_response"], evidence["chain_receipt"])
    return evidence


def append_evidence(attempt, stage, evidence, *, side):
    if attempt.row["state"] != "prepared":
        raise ValueError("Original submission cannot rewind a filled/terminal intent")
    current = copy.deepcopy(attempt.row.get("execution") or {})
    if stage == "prepared_submission" and not current:
        current["capsule"] = copy.deepcopy(evidence)
    elif stage == "dispatch_started" and "capsule" in current and "dispatch_started" not in current:
        current["dispatch_started"] = copy.deepcopy(evidence)
    elif stage == "provider_response" and "dispatch_started" in current and "provider_response" not in current:
        current["provider_response"] = copy.deepcopy(evidence)
    elif stage == "chain_receipt" and "provider_response" in current and "chain_receipt" not in current:
        current["chain_receipt"] = copy.deepcopy(evidence)
    else:
        raise ValueError("Original execution evidence cannot repeat or change its stage")
    validate_journal({**attempt.row, "execution": current}, side=side)
    if side == "buy":
        attempt._save(execution=current)
    else:
        attempt.save(execution=current)


def checked_live_fill(row, response, *, side):
    evidence = validate_journal(row, side=side)
    if evidence is None or "chain_receipt" not in evidence:
        raise ValueError("Live fill lacks owned original chain/wallet evidence")
    receipt = evidence["chain_receipt"]
    if response.get("execution_receipt") != receipt or response.get("signature") != receipt["provider_reported_signature"]:
        raise ValueError("Live response differs from owned original chain receipt")
    return copy.deepcopy(receipt)
