"""Typed original auxiliary inputs; HTTP receipt time is not provider as-of.

Momentum is not an insider graph, and token-account concentration is not a
wallet-cluster analysis. These explicit proxies never certify trading profit.
"""
from __future__ import annotations

from copy import deepcopy
import datetime as dt
from decimal import Decimal
import hashlib
import json
import math
import time

from utils.market_observation import fresh_market_value, market_number
from utils.numeric_types import binary_value

VERSION = "auxiliary_t0_receipt_v1"
KINDS = frozenset({"trend", "early_accumulation", "rug", "cluster"})
MAX_AGES = {"trend": 30., "early_accumulation": 30., "rug": 120., "cluster": 60.}
BASES = {
    "trend": "fresh_snapshot_m5_percentage_point_momentum_not_ema",
    "early_accumulation": "fresh_snapshot_early_buying_proxy_not_confirmed_insider",
    "rug": "rugcheck_normalised_risk_http_receipt_not_report_asof",
    "cluster": "rpc_top10_token_account_concentration_not_wallet_cluster",
}
TREND_THRESHOLD_PCT = 15.
EARLY_WINDOW_MIN = 20.
EARLY_MIN_BUYS = 3
EARLY_MIN_PCT = 5.
EARLY_MIN_LIQ_USD = 3000.
TOP_N = 10
MAX_TOP_SHARE = .20
MAX_SLOT_GAP = 8


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _clock(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def observation_clock():
    # Same UTC clock/precision as the persisted feature-vector datetime. On
    # Windows time.time() can round one microsecond above datetime.now().
    return dt.datetime.now(dt.timezone.utc).timestamp()


def receipt_datetime(stamp):
    """Original float clock at the persisted vector's microsecond resolution.

    Floor instead of rounding upward across a microsecond boundary. This is
    precision normalization, not a seconds-wide causal tolerance.
    """
    seconds = int(stamp)
    micros = int((Decimal(str(stamp)) - seconds) * 1_000_000)
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc) + dt.timedelta(microseconds=micros)


def clock_after(left, right):
    """Allow only the one-microsecond mixed-clock serialization quantum.

    Original receipt timestamps remain unchanged. This is not the much larger
    provider freshness/skew window, and must never be widened to seconds.
    """
    return receipt_datetime(left) > receipt_datetime(right) + dt.timedelta(microseconds=1)


def whole_number(value, *, maximum=2**31 - 1):
    """No floats for raw chain quantities; callers can use int/string only."""
    if type(value) is int:
        return value if 0 <= value <= maximum else None
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 20:
        number = int(value)
        return number if number <= maximum else None
    return None


def trend_number(value):
    if isinstance(value, str):
        value = {"up": 1, "down": -1, "flat": 0}.get(value.strip().lower(), value)
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return int(number) if number in (-1., 0., 1.) else None
    except (TypeError, ValueError, OverflowError):
        return None


def observation(kind, address, value=None, *, source="unknown", observed_at=None,
                evaluated_at=None, inputs=None, reason="not_observed"):
    if kind not in KINDS:
        raise ValueError("Unsupported auxiliary kind")
    known = value is not None
    payload = {"version": VERSION, "kind": kind, "address": address,
        "source": source if known else "unknown", "basis": BASES[kind] if known else "not_observed",
        "value": value, "observed_at": observed_at if known else None,
        "evaluated_at": (observation_clock() if evaluated_at is None else evaluated_at) if known else None,
        "inputs": deepcopy(inputs or {}) if known else {}, "reason": "observed" if known else reason}
    payload["payload_sha256"] = _hash(payload)
    return payload


def _market_inputs(token, fields):
    records = {}
    for field in fields:
        value = fresh_market_value(token, field)
        if value is None:
            return None
        record = token["market_observation"]["fields"][field]
        records[field] = deepcopy(record)
    return records


def trend_observation(token):
    inputs = _market_inputs(token, ("price_pct_5m",))
    address = str(token.get("address") or "")
    if inputs is None:
        return observation("trend", address, reason="missing_fresh_momentum")
    value = inputs["price_pct_5m"]["value"]
    direction = 1 if value >= TREND_THRESHOLD_PCT else -1 if value <= -TREND_THRESHOLD_PCT else 0
    return observation("trend", address, direction, source="current_entry_snapshot",
        observed_at=inputs["price_pct_5m"]["received_at"],
        inputs={"market": inputs, "threshold_pct": TREND_THRESHOLD_PCT})


def _created_epoch(value):
    try:
        stamp = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None  # Do not invent timezone/creation evidence.
        return stamp.timestamp() if _clock(stamp.timestamp()) else None
    except (ValueError, TypeError, OverflowError):
        return None


def accumulation_observation(token):
    address = str(token.get("address") or "")
    fields = ("txns_last_5m_buys", "price_pct_5m", "liquidity_usd")
    inputs = _market_inputs(token, fields)
    created, now = _created_epoch(token.get("created_at")), observation_clock()
    if (inputs is None or created is None or created > now
            or binary_value(token.get("liquidity_is_proxy")) == 1
            or binary_value(token.get("liquidity_usd_is_proxy")) == 1):
        return observation("early_accumulation", address, reason="missing_fresh_real_proxy_inputs")
    buys = inputs["txns_last_5m_buys"]["value"]
    if not float(buys).is_integer() or buys > 2**31 - 1:
        return observation("early_accumulation", address, reason="invalid_buy_count")
    positive = ((now - created) / 60 <= EARLY_WINDOW_MIN and buys >= EARLY_MIN_BUYS
                and inputs["price_pct_5m"]["value"] >= EARLY_MIN_PCT
                and inputs["liquidity_usd"]["value"] >= EARLY_MIN_LIQ_USD)
    return observation("early_accumulation", address, positive, source="current_entry_snapshot",
        observed_at=min(record["received_at"] for record in inputs.values()), evaluated_at=now,
        inputs={"market": inputs, "created_at": created,
                "creation_basis": "pair_or_discovery_creation_not_verified_mint"})


def _checked_market(inputs, fields, *, now, limit):
    if not isinstance(inputs, dict) or set(inputs) != set(fields):
        return False
    for field, record in inputs.items():
        if (not isinstance(record, dict) or set(record) != {"source", "received_at", "value"}
                or not isinstance(record["source"], str) or not record["source"]
                or not _clock(record["received_at"]) or not -2 <= now - record["received_at"] <= limit
                or market_number(record["value"], field) is None):
            return False
    return True


def cluster_inputs_value(inputs):
    """Recompute a bounded concentration proxy from original raw RPC inputs."""
    keys = {"accounts", "total_supply", "decimals", "largest_slot", "supply_slot",
            "largest_received_at", "supply_received_at", "commitment"}
    if not isinstance(inputs, dict) or set(inputs) != keys or inputs["commitment"] != "confirmed":
        return None
    supply = whole_number(inputs["total_supply"], maximum=2**64 - 1)
    decimals = whole_number(inputs["decimals"], maximum=255)
    slots = [whole_number(inputs[key], maximum=2**63 - 1) for key in ("largest_slot", "supply_slot")]
    accounts = inputs["accounts"]
    if (supply is None or supply == 0 or decimals is None or type(inputs["decimals"]) is not int
            or any(slot is None for slot in slots)
            or any(type(inputs[key]) is not int for key in ("largest_slot", "supply_slot"))
            or abs(slots[0] - slots[1]) > MAX_SLOT_GAP
            or not isinstance(accounts, list) or not 1 <= len(accounts) <= 20):
        return None
    amounts, seen = [], set()
    for account in accounts:
        if (not isinstance(account, dict) or set(account) != {"address", "amount", "decimals"}
                or not isinstance(account["address"], str) or not account["address"] or account["address"] in seen
                or type(account["decimals"]) is not int or account["decimals"] != decimals):
            return None
        amount = whole_number(account["amount"], maximum=2**64 - 1)
        if amount is None:
            return None
        seen.add(account["address"])
        amounts.append(amount)
    if (amounts != sorted(amounts, reverse=True) or sum(amounts) > supply
            or len(accounts) < 20 and sum(amounts) != supply):
        return None
    # Exact raw-integer comparison, including uint64 quantities near 20%.
    return sum(amounts[:TOP_N]) * 5 > supply


def checked_auxiliary_observation(raw, address, kind, *, now=None):
    """Original source age and derived value are checked on every cache read."""
    now = time.time() if now is None else now
    keys = {"version", "kind", "address", "source", "basis", "value", "observed_at",
            "evaluated_at", "inputs", "reason", "payload_sha256"}
    try:
        if (kind not in KINDS or not isinstance(raw, dict) or set(raw) != keys
                or raw["version"] != VERSION or raw["kind"] != kind or raw["address"] != address
                or not isinstance(address, str) or not address
                or raw["payload_sha256"] != _hash({key: value for key, value in raw.items() if key != "payload_sha256"})):
            return None
        value, inputs = raw["value"], raw["inputs"]
        if value is None:
            return deepcopy(raw) if (raw["basis"] == "not_observed" and raw["source"] == "unknown"
                and raw["observed_at"] is None and raw["evaluated_at"] is None and inputs == {}
                and isinstance(raw["reason"], str) and raw["reason"]) else None
        limit = MAX_AGES[kind]
        if (raw["basis"] != BASES[kind] or raw["reason"] != "observed"
                or not isinstance(raw["source"], str) or not raw["source"]
                or not _clock(raw["observed_at"]) or not _clock(raw["evaluated_at"])
                or clock_after(raw["observed_at"], raw["evaluated_at"])
                or not -2 <= now - raw["observed_at"] <= limit
                or not -2 <= now - raw["evaluated_at"] <= limit):
            return None
        expected_source = {"trend": "current_entry_snapshot", "early_accumulation": "current_entry_snapshot",
                           "rug": "rugcheck_report_summary", "cluster": "solana_rpc_confirmed"}[kind]
        if raw["source"] != expected_source:
            return None
        if kind in {"trend", "early_accumulation"}:
            fields = ("price_pct_5m",) if kind == "trend" else ("txns_last_5m_buys", "price_pct_5m", "liquidity_usd")
            if not _checked_market(inputs.get("market"), fields, now=now, limit=limit):
                return None
            if any(clock_after(record["received_at"], raw["evaluated_at"]) for record in inputs["market"].values()):
                return None
            if raw["observed_at"] != min(record["received_at"] for record in inputs["market"].values()):
                return None
            if kind == "trend":
                if set(inputs) != {"market", "threshold_pct"} or inputs["threshold_pct"] != TREND_THRESHOLD_PCT:
                    return None
                pct = inputs["market"]["price_pct_5m"]["value"]
                expected = 1 if pct >= TREND_THRESHOLD_PCT else -1 if pct <= -TREND_THRESHOLD_PCT else 0
                if type(value) is not int or value != expected:
                    return None
            else:
                if (set(inputs) != {"market", "created_at", "creation_basis"} or not _clock(inputs["created_at"])
                        or inputs["created_at"] > raw["evaluated_at"]
                        or inputs["creation_basis"] != "pair_or_discovery_creation_not_verified_mint"):
                    return None
                market = inputs["market"]
                buys = market["txns_last_5m_buys"]["value"]
                if not float(buys).is_integer() or buys > 2**31 - 1:
                    return None
                expected = ((raw["evaluated_at"] - inputs["created_at"]) / 60 <= EARLY_WINDOW_MIN
                    and buys >= EARLY_MIN_BUYS and market["price_pct_5m"]["value"] >= EARLY_MIN_PCT
                    and market["liquidity_usd"]["value"] >= EARLY_MIN_LIQ_USD)
                if type(value) is not bool or value is not expected:
                    return None
        elif kind == "rug":
            if (not isinstance(inputs, dict) or set(inputs) != {"score", "score_normalised"}
                    or type(inputs["score"]) is not int or type(inputs["score_normalised"]) is not int
                    or whole_number(inputs["score"], maximum=2**63 - 1) is None
                    or type(value) is not int or whole_number(value, maximum=100) is None
                    or inputs["score_normalised"] != value):
                return None
        else:
            expected = cluster_inputs_value(inputs)
            if expected is None or type(value) is not bool or value is not expected:
                return None
            receipts = [inputs[key] for key in ("largest_received_at", "supply_received_at")]
            if (any(not _clock(stamp) or not -2 <= now - stamp <= limit
                    or clock_after(stamp, raw["evaluated_at"]) for stamp in receipts)
                    or raw["observed_at"] != min(receipts)):
                return None
        return deepcopy(raw)
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return None


def auxiliary_scalar(token, kind):
    field = {"trend": "trend", "early_accumulation": "early_accumulation_sig",
             "rug": "rug_score", "cluster": "cluster_bad"}[kind]
    value = token.get(field)
    if kind == "trend":
        return trend_number(value)
    if kind in {"early_accumulation", "cluster"}:
        binary = binary_value(value)
        return None if binary is None else bool(binary)
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None and float(value).is_integer() and 0 <= float(value) <= 100 else None
    except (ValueError, TypeError, OverflowError):
        return None
