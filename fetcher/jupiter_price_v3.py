"""Price V3 evidence: omission is not a transport failure or a routing quote."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

from solders.pubkey import Pubkey

MAX_PRICE_BODY_BYTES = 1024 * 1024
MAX_PRICE_IDS = 50


@dataclass(frozen=True)
class PricePoint:
    status: str
    price_usd: float | None = None
    block_id: int | None = None
    decimals: int | None = None
    reason: str = "unknown"


class PriceBatch(dict):
    """Retain the legacy tuple mapping with original HTTP receipt and typed points."""
    def __init__(self, points: dict[str, PricePoint], *, received_at: float | None = None):
        super().__init__((mint, (point.status, point.price_usd)) for mint, point in points.items())
        self.points = points
        self.received_at = received_at


def unknown_batch(mints: Iterable[str], reason: str = "response_unavailable") -> PriceBatch:
    return PriceBatch({mint: PricePoint("ERR", reason=reason) for mint in mints})


def _mint(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(Pubkey.from_string(value)) == value
    except (ValueError, TypeError):
        return False


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def parse_price_payload(payload: Any, mints: Iterable[str], *, received_at: float | None = None) -> PriceBatch:
    """Canonical bare V3 map only; absent keys are the only authoritative NIL."""
    requested = list(mints)
    if (not requested or len(requested) > MAX_PRICE_IDS or not all(_mint(mint) for mint in requested)
            or len(set(requested)) != len(requested)):
        return unknown_batch(requested, "invalid_request")
    if (not isinstance(payload, dict)
            or any(key not in requested for key in payload)):
        return unknown_batch(requested, "invalid_response_map")
    if received_at is not None and _positive_number(received_at) is None:
        return unknown_batch(requested, "invalid_receipt_clock")

    points = {}
    for mint in requested:
        if mint not in payload:
            points[mint] = PricePoint("NIL", reason="provider_omitted_price")
            continue
        entry = payload[mint]
        price = _positive_number(entry.get("usdPrice")) if isinstance(entry, dict) else None
        if price is None:
            points[mint] = PricePoint("ERR", reason="invalid_price_point")
            continue
        block_id, decimals = entry.get("blockId"), entry.get("decimals")
        if (("blockId" in entry and (type(block_id) is not int or not 0 <= block_id < 2**64))
                or ("decimals" in entry and (type(decimals) is not int or not 0 <= decimals <= 255))
                or any(key in entry for key in ("error", "errorCode", "errorMessage"))):
            points[mint] = PricePoint("ERR", reason="invalid_price_metadata")
            continue
        points[mint] = PricePoint("OK", price, block_id, decimals, "provider_price")
    return PriceBatch(points, received_at=received_at)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("nonfinite_json_number")


def decode_price_body(body: bytes, mints: Iterable[str], *, received_at: float | None = None) -> PriceBatch:
    requested = list(mints)
    if not isinstance(body, bytes) or not body or len(body) > MAX_PRICE_BODY_BYTES:
        return unknown_batch(requested, "invalid_body_size")
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_invalid_constant)
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        return unknown_batch(requested, "invalid_json_body")
    return parse_price_payload(payload, requested, received_at=received_at)


async def read_price_body(response: Any) -> bytes:
    """Bound decompressed bytes; never fall back to an unbounded response.json()."""
    length = getattr(response, "headers", {}).get("Content-Length")
    if length is not None and (not str(length).isdigit() or int(length) > MAX_PRICE_BODY_BYTES):
        raise ValueError("invalid_body_size")
    content = getattr(response, "content", None)
    if content is None or not callable(getattr(content, "read", None)):
        raise ValueError("missing_bounded_body_reader")
    body = bytearray()
    while True:
        chunk = await content.read(min(65536, MAX_PRICE_BODY_BYTES + 1 - len(body)))
        if not isinstance(chunk, bytes):
            raise ValueError("invalid_body_chunk")
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_PRICE_BODY_BYTES:
            raise ValueError("invalid_body_size")
