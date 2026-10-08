"""Current decision inputs; HTTP receipts are not market-as-of or fill proof."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
import json
import time
from typing import Any, Mapping

from utils.market_observation import (
    DEFAULT_MAX_AGE_S, MARKET_FIELDS, fresh_market_value, market_number,
    retain_fresh_market_fields,
)
from analytics.social_signal import (
    SOCIAL_STATUS_UNKNOWN, SOCIAL_MAX_AGE_S, checked_social_receipt,
    social_cache_ttl_s, social_feature_values, social_signal_from_dict,
)

# Queue items carry discovery identity, not reusable decisions/model inputs.
_DISCOVERY_FIELDS = (
    "address", "symbol", "name", "creator", "source", "discovered_via",
    "discovered_at", "first_seen", "first_seen_at", "created_at", "createdAt",
    "created", "createdAtUtc", "pairCreatedAt", "pair_created_at",
    "pairCreatedAtMs", "listedAt", "dex_id", "dexId", "pair_address",
    "pairAddress", "pool_address", "poolAddress", "website", "twitter",
    "telegram", "discord", "websites", "socials",
)
_SNAPSHOT_FIELDS = (
    "price_source", "price_confidence", "price_confidence_reason",
    "price_provider_degraded", "price_snapshot_partial",
)
_PROXY_KEY = "paper_liquidity_observation"
_PROXY_BASIS = "configured_paper_proxy_not_observed_liquidity_v1"


def discovery_candidate(queued: Mapping[str, Any]) -> dict:
    """Detach stable discovery context and discard prior lanes/scores/features."""
    out = {key: deepcopy(queued[key]) for key in _DISCOVERY_FIELDS if key in queued}
    # Only _evaluate_and_buy_guarded supplies this checked immutable snapshot.
    if "paper_entry_policy" in queued:
        out["paper_entry_policy"] = deepcopy(queued["paper_entry_policy"])
    return out


def prepare_entry_candidate(queued: Mapping[str, Any], snapshot: dict | None) -> dict | None:
    """Missing fresh fields remain unknown; no fall-through to queued values."""
    out = discovery_candidate(queued)
    if not isinstance(snapshot, dict) or snapshot.get("address") != out.get("address"):
        return None
    clean = retain_fresh_market_fields(snapshot)
    if clean is None or fresh_market_value(clean, "price_usd") is None:
        return None
    for key in _DISCOVERY_FIELDS + _SNAPSHOT_FIELDS + MARKET_FIELDS + ("market_observation", "social_signal"):
        if key in clean and clean[key] is not None:
            out[key] = deepcopy(clean[key])
    # Explicit absence prevents default/alias resurrection in later enrichers.
    for field in MARKET_FIELDS:
        out[field] = clean.get(field)
    # Venue migration must not leave an old canonical alias ahead of a fresh
    # provider alias in downstream classifiers (dex_id is read before dexId).
    if dex := clean.get("dex_id") or clean.get("dexId"):
        out["dex_id"] = out["dexId"] = deepcopy(dex)
    provider_proxy = bool(clean.get("liquidity_usd_is_proxy") or clean.get("liquidity_is_proxy"))
    out["liquidity_usd_is_proxy"] = out["liquidity_is_proxy"] = int(provider_proxy)
    return out


def apply_paper_liquidity_proxy(token: dict, value: Any, kind: str, *, paper: bool) -> bool:
    """A paper-only estimate may fill an unknown, never an observed zero."""
    amount = market_number(value, "liquidity_usd")
    if not paper or amount is None or amount <= 0 or token.get("liquidity_usd") is not None:
        return False
    if fresh_market_value(token, "price_usd") is None:
        return False
    token["liquidity_usd"] = amount
    token["liquidity_usd_is_proxy"] = token["liquidity_is_proxy"] = 1
    token["sniper_liquidity_proxy"] = 1
    token[_PROXY_KEY] = {"basis": _PROXY_BASIS, "kind": kind,
                         "value": amount, "created_at": time.time()}
    return True


@dataclass(frozen=True)
class EntryObservation:
    address: str
    values: tuple[tuple[str, float | None], ...]
    receipts: tuple[tuple[str, str, float], ...]
    proxy: tuple[str, float, float] | None = None
    provider_proxy: bool = False
    social: str | None = None


def freeze_entry_social_observation(token: dict, observation: EntryObservation) -> EntryObservation:
    from dataclasses import replace
    return replace(observation, social=json.dumps(token["social_signal"], sort_keys=True, allow_nan=False))


def entry_auxiliary_observations(observation: EntryObservation) -> dict | None:
    return {"social": json.loads(observation.social)} if observation.social is not None else None


def social_observation_problem(payload, *, address: str, vector=None, now=None, max_age_s=SOCIAL_MAX_AGE_S):
    """Also usable on original pre-buy proofs, at their historical capture time."""
    if not isinstance(payload, dict):
        return "missing_social_receipt"
    signal = social_signal_from_dict(payload)
    if set(payload) != set(signal.to_dict()) or payload != signal.to_dict():
        return "changed_social_receipt"
    if signal.status != SOCIAL_STATUS_UNKNOWN or signal.received_at is not None:
        if checked_social_receipt(payload, address, now=now, max_age_s=max_age_s) is None:
            return "expired_or_invalid_social_receipt"
    if vector is not None:
        for field, value in social_feature_values(signal).items():
            actual = vector.get(field)
            if value is None and (actual is None or type(actual) is float and math.isnan(actual)):
                continue
            if actual != value:
                return "changed_model_social_inputs"
    return None


def freeze_entry_observation(token: dict, *, paper: bool) -> EntryObservation | None:
    """Freeze exactly the current market inputs before any lane/model decision."""
    values = tuple((field, market_number(token.get(field), field)) for field in MARKET_FIELDS)
    receipts = []
    proxy = None
    record = token.get(_PROXY_KEY)
    if record is not None:
        if not paper or not isinstance(record, dict) or record.get("basis") != _PROXY_BASIS:
            return None
        try:
            created = float(record["created_at"])
            amount = market_number(record.get("value"), "liquidity_usd")
            kind = str(record.get("kind") or "")
            if (isinstance(record["created_at"], bool) or not math.isfinite(created)
                    or created <= 0 or amount is None or amount <= 0 or not kind
                    or market_number(token.get("liquidity_usd"), "liquidity_usd") != amount):
                return None
            proxy = (kind, amount, created)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        # A provider receipt and a synthetic liquidity value cannot coexist.
        proof = token.get("market_observation")
        fields = proof.get("fields", {}) if isinstance(proof, dict) else {}
        if not isinstance(fields, dict) or "liquidity_usd" in fields:
            return None
    for field, value in values:
        if value is None or (field == "liquidity_usd" and proxy is not None):
            continue
        if fresh_market_value(token, field) is None:
            return None
        receipt = token["market_observation"]["fields"][field]
        receipts.append((field, str(receipt["source"]), float(receipt["received_at"])))
    provider_proxy = proxy is None and bool(token.get("liquidity_usd_is_proxy") or token.get("liquidity_is_proxy"))
    observation = EntryObservation(str(token.get("address") or ""), values, tuple(receipts), proxy, provider_proxy)
    return observation if entry_observation_problem(token, observation, paper=paper) is None else None


def entry_observation_problem(
    token: dict, observation: EntryObservation | None, *, paper: bool,
    vector: Mapping[str, Any] | None = None,
) -> str | None:
    """Return a transient reevaluation reason; never refresh just a price here."""
    if observation is None or token.get("address") != observation.address:
        return "missing_or_changed_identity"
    if observation.social is not None:
        payload = json.loads(observation.social)
        if token.get("social_signal") != payload:
            return "changed_social_inputs"
        problem = social_observation_problem(payload, address=observation.address, vector=vector,
                                              max_age_s=social_cache_ttl_s())
        if problem is not None:
            return problem
    expected = dict(observation.values)
    if expected.get("price_usd") is None:
        return "missing_price"
    for field, value in observation.values:
        if market_number(token.get(field), field) != value:
            return "changed_inputs"
        if vector is not None and field in vector and market_number(vector[field], field) != value:
            return "changed_model_inputs"
    proof = token.get("market_observation")
    fields = proof.get("fields", {}) if isinstance(proof, dict) else {}
    for field, source, received in observation.receipts:
        record = fields.get(field) if isinstance(fields, dict) else None
        if not isinstance(record, dict) or record.get("source") != source or record.get("received_at") != received:
            return "changed_receipts"
        if fresh_market_value(token, field) is None:
            return "expired_inputs"
    expected_proxy = bool(observation.proxy or observation.provider_proxy)
    if (bool(token.get("liquidity_usd_is_proxy")) != expected_proxy
            or bool(token.get("liquidity_is_proxy")) != expected_proxy):
        return "changed_liquidity_basis"
    if (vector is not None and "liquidity_is_proxy" in vector
            and market_number(vector["liquidity_is_proxy"], "liquidity_is_proxy") != int(expected_proxy)):
        return "changed_model_liquidity_basis"
    if observation.proxy is not None:
        record = token.get(_PROXY_KEY)
        kind, amount, created = observation.proxy
        if (not paper or not isinstance(record, dict) or record.get("basis") != _PROXY_BASIS
                or (record.get("kind"), record.get("value"), record.get("created_at")) != observation.proxy
                or not token.get("liquidity_usd_is_proxy") or not token.get("liquidity_is_proxy")
                or not -2 <= time.time() - created <= DEFAULT_MAX_AGE_S):
            return "changed_or_expired_paper_proxy"
    return None
