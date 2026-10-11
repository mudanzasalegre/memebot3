"""Typed provider mint-creation context, never independent chain authentication."""
from __future__ import annotations

from copy import deepcopy
import datetime as dt
import math

from analytics.token_time import BIRTH_CLOCK_FIELDS, _to_datetime

FIELD = "token_birth_observation"
VERSION = "birdeye_mint_creation_receipt_v1"
BASIS = "provider_reported_mint_creation_not_independent_chain_verification"
_KEYS = {"version", "source", "chain", "address", "created_at", "slot", "tx_hash", "received_at", "basis"}
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58_size(value, size):
    if not isinstance(value, str) or not value or len(value) > size * 2:
        return False
    number = 0
    for char in value:
        digit = _BASE58.find(char)
        if digit < 0:
            return False
        number = number * 58 + digit
    leading = len(value) - len(value.lstrip("1"))
    return leading + (number.bit_length() + 7) // 8 == size


def checked_token_birth(raw, address, *, now=None):
    """Immutable old creation may be cached; identity/causality never expire away."""
    stamp = _to_datetime(now) if now is not None else dt.datetime.now(dt.timezone.utc)
    born = _to_datetime(raw.get("created_at")) if isinstance(raw, dict) else None
    if (not isinstance(raw, dict) or set(raw) != _KEYS or raw.get("version") != VERSION
            or raw.get("basis") != BASIS or raw.get("source") != "birdeye"
            or raw.get("chain") != "solana" or raw.get("address") != address
            or not base58_size(address, 32) or not base58_size(raw.get("tx_hash"), 64)
            or type(raw.get("slot")) is not int or raw["slot"] < 0
            or born is None or stamp is None or type(raw.get("created_at")) is not str
            or raw["created_at"] != born.isoformat()
            or type(raw.get("received_at")) not in (int, float)):
        return None
    received = raw["received_at"]
    try:
        valid = math.isfinite(received) and 0 < received <= stamp.timestamp() and born.timestamp() <= received
    except (ValueError, TypeError, OverflowError):
        valid = False
    if not valid:
        return None
    return deepcopy(raw)


def birth_clock(token):
    """Same ordered original clock selection as the common age helper."""
    for name in BIRTH_CLOCK_FIELDS:
        if (value := _to_datetime(token.get(name))) is not None:
            return value
    return None


def merge_birth_context(primary, secondary, *, now=None):
    """Move a birth and its receipt atomically; never rejuvenate a known mint.

    Existing untyped original clocks remain legacy-normalised inputs. Only a
    checked typed receipt can add new Birdeye creation context. Venue aliases,
    discovery/queue clocks and generic event timestamps are not considered.
    """
    out = deepcopy(primary)
    if not isinstance(secondary, dict) or secondary.get("address") != out.get("address"):
        return out
    existing, incoming = birth_clock(out), birth_clock(secondary)
    if existing is not None:
        if (FIELD not in out and incoming == existing
                and (proof := checked_token_birth(secondary.get(FIELD), out.get("address"), now=now)) is not None):
            out[FIELD] = proof
        return out
    # An invalid/future supplied original clock must not be silently replaced.
    if any(out.get(name) is not None for name in BIRTH_CLOCK_FIELDS):
        return out
    limit = _to_datetime(now) if now is not None else dt.datetime.now(dt.timezone.utc)
    if incoming is None or limit is None or incoming > limit:
        return out
    proof = checked_token_birth(secondary.get(FIELD), out.get("address"), now=limit)
    if FIELD in secondary and (proof is None or _to_datetime(proof["created_at"]) != incoming):
        return out
    if any((value := _to_datetime(secondary.get(name))) is not None and value != incoming for name in BIRTH_CLOCK_FIELDS):
        return out
    for name in BIRTH_CLOCK_FIELDS:
        if secondary.get(name) is not None:
            out[name] = deepcopy(secondary[name])
    if proof is not None:
        out[FIELD] = proof
    return out


def token_birth_problem(token, *, now=None):
    if FIELD not in token:
        return None  # Legacy-normalised input is not newly certified provider proof.
    proof = checked_token_birth(token[FIELD], token.get("address"), now=now)
    if proof is None:
        return "invalid_token_birth_receipt"
    born = _to_datetime(proof["created_at"])
    if birth_clock(token) != born or any((value := _to_datetime(token.get(name))) is not None and value != born
                                        for name in BIRTH_CLOCK_FIELDS):
        return "changed_token_birth_clock"
    return None
