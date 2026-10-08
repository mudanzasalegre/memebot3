"""Original PAPER research intent/generation receipts, not execution proof.

An exact-size quote alone cannot establish that it followed an exit decision.
Keep the original intent and financial generation, then recheck them at fill
and retrospective cohort consumption. Never turn an unknown legacy fill into
prospective evidence by assigning it the current clock.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
from typing import Any

from execution.quote_receipt import public_summary, valid_summary
from utils.raw_units import U64_MAX

VERSION = "paper_research_exit_intent_v1"
_MONEY = ("amount_sol", "entry_notional_usd", "buy_price_usd", "realized_proceeds_sol",
          "realized_proceeds_usd", "estimated_fees_sol", "estimated_fees_usd")
_IDENTITY = ("run_id", "run_started_at", "opened_at", "token_address", "entry_intent_id",
             "source_position_key", "buy_signature", "runner_trailing_policy")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _time(value: Any) -> dt.datetime:
    value = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.utcoffset() is None:
        raise ValueError("Unknown original clock")
    return value.astimezone(dt.timezone.utc)


def financial_basis(subject: dict) -> dict:
    if subject.get("dry_run") is not True or subject.get("quantity_basis") != "quoted_raw_spl_units":
        raise ValueError("Not an exact-quantity PAPER subject")
    quantities = {key: subject.get(key) for key in ("entry_qty", "qty_lamports", "realized_qty", "execution_fill_count")}
    if (any(type(value) is not int or not 0 <= value <= U64_MAX for value in quantities.values())
            or quantities["qty_lamports"] <= 0 or quantities["execution_fill_count"] <= 0
            or quantities["entry_qty"] != quantities["qty_lamports"] + quantities["realized_qty"]):
        raise ValueError("Unknown financial quantity generation")
    money = {key: subject.get(key) for key in _MONEY}
    if (any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in money.values())
            or any(money[key] <= 0 for key in _MONEY[:3])):
        raise ValueError("Unknown financial cash generation")
    model = subject.get("execution_cost_model") or {}
    model = {key: model.get(key) for key in ("version", "observed_execution", "slippage_bps", "fee_sol_per_fill")}
    if (model["version"] != "estimated-v1" or model["observed_execution"] is not False
            or type(model["slippage_bps"]) not in (int, float) or not math.isfinite(model["slippage_bps"])
            or not 0 <= model["slippage_bps"] < 10000
            or type(model["fee_sol_per_fill"]) not in (int, float) or not math.isfinite(model["fee_sol_per_fill"])
            or model["fee_sol_per_fill"] < 0):
        raise ValueError("Unknown frozen estimated cost model")
    route = subject.get("entry_route_quote")
    # This receipt certifies intent chronology/generation only. Legacy entry
    # evidence is not promoted into a public quote or a cash valuation receipt.
    if not valid_summary(route, amount=100000000, allow_legacy=True):
        raise ValueError("Unknown original entry route")
    return copy.deepcopy({"dry_run": True, "quantity_basis": "quoted_raw_spl_units", **quantities, **money,
        **{key: subject[key] for key in _IDENTITY if key in subject},
        "execution_cost_model": model, "entry_route_quote": public_summary(route)})


def make_intent(subject: dict, *, quantity: int, reason: str, now: dt.datetime,
                ladder_plan: dict | None = None) -> dict:
    basis = financial_basis(subject)
    stamp = _time(now)
    if (type(quantity) is not int or not 0 < quantity <= basis["qty_lamports"]
            or not isinstance(reason, str) or not reason.strip()
            or stamp < _time(subject["opened_at"])):
        raise ValueError("Invalid original PAPER exit intent")
    intent = {"quantity": quantity, "reason": reason, "requested_at": stamp.isoformat()}
    if ladder_plan is not None:
        intent["ladder_plan"] = copy.deepcopy(ladder_plan)
    receipt = {"version": VERSION, "role": "estimated_paper_intent_only", "observed_execution": False,
               "decision": copy.deepcopy(intent), "financial_basis": basis}
    receipt["sha256"] = _hash(receipt)
    intent["receipt"] = receipt
    return intent


def valid_intent(intent: Any, subject: dict) -> bool:
    try:
        receipt = intent["receipt"]
        return (isinstance(intent, dict) and isinstance(receipt, dict)
            and set(receipt) == {"version", "role", "observed_execution", "decision", "financial_basis", "sha256"}
            and receipt["version"] == VERSION and receipt["role"] == "estimated_paper_intent_only"
            and receipt["observed_execution"] is False
            and receipt["sha256"] == _hash({key: value for key, value in receipt.items() if key != "sha256"})
            and receipt["decision"] == {key: value for key, value in intent.items() if key != "receipt"}
            and financial_basis(receipt["financial_basis"]) == receipt["financial_basis"]
            and receipt["financial_basis"] == financial_basis(subject)
            and type(intent["quantity"]) is int and 0 < intent["quantity"] <= subject["qty_lamports"]
            and _time(intent["requested_at"]) >= _time(subject["opened_at"]))
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return False


def causal_quote(intent: dict, route: dict, *, quote_started_at: Any, filled_at: Any) -> bool:
    try:
        received = route["observation_receipt"]["other"]["received_at_utc"]
        return _time(intent["requested_at"]) <= _time(quote_started_at) <= _time(received) <= _time(filled_at)
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return False
