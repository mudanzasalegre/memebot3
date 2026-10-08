"""Durable recovery journal for sells whose database commit was not confirmed.

The exchange/wallet side effect happens before the SQLAlchemy commit.  If that
commit fails, retrying the sell is unsafe: it can liquidate the same quantity a
second time.  This module stores the *post execution* position and trade-event
state in a fsync'ed JSONL journal and can replay it idempotently into SQLite.

The journal is append-only.  A recovery is pending until a later row with the
same ``recovery_id`` has ``status=resolved``.  Keeping status transitions in the
same durable stream makes reconstruction after a process restart deterministic
and tolerates a truncated final JSONL row.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import add_trade_event
from db.models import Position, TradeEvent


SCHEMA_VERSION = 2

# Every field mutated by the close/partial-fill paths.  Capturing a fixed,
# explicit allow-list avoids serialising relationships or SQLAlchemy internals.
POSITION_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "qty",
    "entry_qty",
    "closed",
    "closed_at",
    "close_price_usd",
    "exit_tx_sig",
    "price_source_at_close",
    "price_confidence_at_close",
    "exit_reason",
    "exit_reason_full",
    "outcome",
    "highest_pnl_pct",
    "max_pnl_pct_seen",
    "max_adverse_pnl_pct",
    "exit_state",
    "realized_qty",
    "realized_proceeds_usd",
    "realized_cost_usd",
    "realized_pnl_usd",
    "effective_exit_price_usd",
    "total_pnl_usd",
    "total_pnl_pct",
    "runner_exit_profile",
    "time_to_partial_sec",
    "time_to_peak_sec",
    "peak_after_partial_pct",
    "exit_from_peak_giveback_pct",
    "partial_taken",
    "partial_count",
    "partial_ladder_state",
    "first_partial_at",
    "last_partial_at",
    "last_partial_qty",
    "last_partial_price_usd",
    "peak_price_usd",
    "peak_price",
)

_DATETIME_POSITION_FIELDS = {
    "closed_at",
    "first_partial_at",
    "last_partial_at",
}


class CloseRecoveryError(RuntimeError):
    """Raised when a recovery record cannot be made durable."""


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of one replay pass."""

    resolved: tuple[dict[str, Any], ...]
    failed: tuple[dict[str, Any], ...]
    pending_addresses: frozenset[str]


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json_value(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _parse_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return value
    if isinstance(value, dt.datetime):
        # SQLite returns stored UTC instants without a timezone. Never reinterpret
        # those ledger timestamps in the workstation's local timezone.
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def capture_position_snapshot(position: object) -> dict[str, Any]:
    """Capture all close-related state while the ORM object is still usable."""

    return {
        field: _json_value(getattr(position, field, None))
        for field in POSITION_SNAPSHOT_FIELDS
        if hasattr(position, field)
    }


def apply_position_snapshot(position: object, snapshot: Mapping[str, Any]) -> None:
    """Restore a validated close snapshot onto an ORM position."""

    for field in POSITION_SNAPSHOT_FIELDS:
        if field not in snapshot or not hasattr(position, field):
            continue
        value = snapshot[field]
        if field in _DATETIME_POSITION_FIELDS:
            value = _parse_datetime(value)
        setattr(position, field, value)


def build_recovery_record(
    position: object,
    *,
    event_type: str,
    reason: str,
    sell_response: Mapping[str, Any] | None,
    trade_event: Mapping[str, Any],
    telemetry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the immutable post-sell record before attempting the DB commit."""

    address = str(getattr(position, "address", "") or "").strip()
    position_id = getattr(position, "id", None)
    if not address or position_id is None:
        raise ValueError("close recovery requires position_id and address")

    response = dict(sell_response or {})
    intent_id = response.get("_sell_intent_id")
    if intent_id is not None and re.fullmatch(r"[0-9a-f]{32}", str(intent_id)) is None:
        raise ValueError("invalid sell intent identity")
    from execution.paper_execution_fx import validate_exit
    original_fx = None
    if validate_exit(response):
        if getattr(position, "dry_run", None) is not True:
            raise ValueError("Original PAPER FX cannot certify a LIVE close")
        original_fx = {name: _json_value(response.get(name)) for name in (
            "paper_execution_fx_version", "fill_fx_observation", "quote_sol_usd", "filled_at")}
    return {
        "schema_version": SCHEMA_VERSION,
        "ts_utc": _utc_now_iso(),
        "status": "pending",
        "recovery_id": intent_id or uuid.uuid4().hex,
        "address": address,
        "position_id": int(position_id),
        "run_id": getattr(position, "run_id", None),
        "source_position_key": getattr(position, "source_position_key", None),
        "expected_before_qty": response.get("_qty_before"),
        "event_type": str(event_type),
        "reason": str(reason),
        "sell_signature": str(response.get("signature") or "") or None,
        "sell_venue": str(response.get("venue") or "") or None,
        "sell_qty": response.get("qty_sold"),
        "execution_provenance": _json_value(response.get("execution_provenance")),
        "paper_fill_fx": original_fx,
        # Immutable execution mode is checked against SQL, never restored as a
        # mutable close field. Old records without FX need no new mode marker.
        "paper_fill_dry_run": True if original_fx is not None else None,
        "execution_token_mint": str(getattr(position, "token_mint", None) or address),
        "position_snapshot": capture_position_snapshot(position),
        "trade_event": _json_value(dict(trade_event)),
        "telemetry": _json_value(dict(telemetry or {})),
    }


def _legacy_recovery_id(row: Mapping[str, Any]) -> str:
    """Stable key for v1 rows, which did not carry a recovery id."""

    return "legacy:{position_id}:{address}:{signature}:{reason}".format(
        position_id=row.get("position_id") or "",
        address=str(row.get("address") or "").strip(),
        signature=str(row.get("sell_signature") or "").strip(),
        reason=str(row.get("reason") or "").strip(),
    )


def recovery_id(row: Mapping[str, Any]) -> str:
    value = str(row.get("recovery_id") or "").strip()
    return value or _legacy_recovery_id(row)


def append_journal_row(path: Path, row: Mapping[str, Any]) -> None:
    """Append one JSON row and force it through the OS page cache."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_json_value(dict(row)), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as exc:  # pragma: no cover - exact OS error is platform-specific
        raise CloseRecoveryError(f"cannot persist close recovery journal {path}: {exc}") from exc


def append_prepared(path: Path, record: Mapping[str, Any]) -> None:
    row = dict(record)
    row.update(
        {
            "status": "pending",
            "journaled_at_utc": _utc_now_iso(),
        }
    )
    append_journal_row(path, row)


def append_pending(path: Path, record: Mapping[str, Any], *, error: BaseException) -> None:
    row = dict(record)
    row["error"] = f"{type(error).__name__}:{error}"
    append_prepared(path, row)


def append_status(
    path: Path,
    record: Mapping[str, Any],
    *,
    status: str,
    error: BaseException | str | None = None,
) -> None:
    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ts_utc": _utc_now_iso(),
        "status": str(status),
        "recovery_id": recovery_id(record),
        "address": record.get("address"),
        "position_id": record.get("position_id"),
        "run_id": record.get("run_id"),
        "event_type": record.get("event_type"),
        "reason": record.get("reason"),
    }
    if error is not None:
        row["error"] = str(error) if isinstance(error, str) else f"{type(error).__name__}:{error}"
    append_journal_row(path, row)


def load_journal(path: Path) -> dict[str, dict[str, Any]]:
    """Fold journal rows into the latest merged state per recovery id."""

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise CloseRecoveryError(f"cannot read close recovery journal {path}: {exc}") from exc

    lines = raw.splitlines()
    state: dict[str, dict[str, Any]] = {}
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
        except (TypeError, ValueError) as exc:
            # A crash can leave the last append truncated.  Earlier fsync'ed
            # records remain valid and must still be recovered.
            if index == len(lines) - 1 and not raw.endswith("\n"):
                continue
            raise CloseRecoveryError(
                f"corrupt close recovery journal row {index + 1} in {path}: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise CloseRecoveryError(
                f"corrupt close recovery journal row {index + 1} in {path}: expected object"
            )
        rid = recovery_id(row)
        previous = state.get(rid, {})
        state[rid] = {**previous, **row, "recovery_id": rid}
    return state


def load_pending_records(path: Path) -> list[dict[str, Any]]:
    records = [
        row
        for row in load_journal(path).values()
        if str(row.get("status") or "pending").strip().lower() != "resolved"
    ]
    records.sort(key=lambda row: (str(row.get("ts_utc") or ""), recovery_id(row)))
    return records


def pending_addresses(path: Path) -> set[str]:
    return {
        str(row.get("address") or "").strip()
        for row in load_pending_records(path)
        if str(row.get("address") or "").strip()
    }


def _normalised_timestamp(value: Any) -> str | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    return parsed.astimezone(dt.timezone.utc).isoformat()


def _float_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return abs(float(left) - float(right)) <= 1e-12
    except (TypeError, ValueError):
        return left == right


def _event_has_recovery_id(event: TradeEvent, rid: str) -> bool:
    try:
        raw = json.loads(str(event.raw_json or ""))
    except (TypeError, ValueError):
        return False
    return isinstance(raw, dict) and str(raw.get("close_recovery_id") or "") == rid


def _event_matches_snapshot(event: TradeEvent, snapshot: Mapping[str, Any]) -> bool:
    return (
        str(event.event_type) == str(snapshot.get("event_type") or "")
        and _normalised_timestamp(event.ts_utc) == _normalised_timestamp(snapshot.get("ts_utc"))
        and event.qty == (None if snapshot.get("qty") is None else int(snapshot["qty"]))
        and _float_equal(event.price_usd, snapshot.get("price_usd"))
        and _float_equal(event.notional_usd, snapshot.get("notional_usd"))
        and _float_equal(event.pnl_usd, snapshot.get("pnl_usd"))
        and _float_equal(event.pnl_pct, snapshot.get("pnl_pct"))
        and str(event.reason or "") == str(snapshot.get("reason") or "")
        and str(event.price_source or "") == str(snapshot.get("price_source") or "")
        and str(event.price_confidence or "") == str(snapshot.get("price_confidence") or "")
    )


def _tag_event(event: TradeEvent, rid: str) -> None:
    try:
        raw = json.loads(str(event.raw_json or ""))
    except (TypeError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw["close_recovery_id"] = rid
    event.raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))


def _validate_record(record: Mapping[str, Any]) -> None:
    if int(record.get("schema_version") or 0) < SCHEMA_VERSION:
        raise ValueError("legacy recovery row has no replayable position snapshot")
    if record.get("position_id") is None:
        raise ValueError("missing position_id")
    if not isinstance(record.get("position_snapshot"), Mapping):
        raise ValueError("missing position_snapshot")
    event = record.get("trade_event")
    if not isinstance(event, Mapping) or not str(event.get("event_type") or "").strip():
        raise ValueError("missing trade_event snapshot")
    if not event.get("ts_utc"):
        raise ValueError("missing trade_event timestamp")
    for values in (record["position_snapshot"], event):
        if any(isinstance(value, float) and not math.isfinite(value) for value in values.values()):
            raise ValueError("non-finite close recovery accounting")
    before = record.get("expected_before_qty")
    if before is not None and (type(before) is not int or not 0 < before <= 2**63 - 1):
        raise ValueError("invalid pre-sell quantity")
    fx = record.get("paper_fill_fx")
    if fx is None and record.get("paper_fill_dry_run") is not None:
        raise ValueError("Close replay lost its declared original PAPER FX")
    if fx is not None:
        from execution.paper_execution_fx import validate_exit
        if (not validate_exit(fx) or record.get("paper_fill_dry_run") is not True
                or _parse_datetime(fx["filled_at"]) != _parse_datetime(event["ts_utc"])):
            raise ValueError("Close replay differs from its original PAPER FX event")
    proof = record.get("execution_provenance")
    if proof is not None:
        from execution import chain_reconciliation as chain
        if not isinstance(proof, dict):
            raise ValueError("Invalid close original execution provenance")
        receipt = chain.validate_receipt(proof["capsule"], proof["provider_response"], proof["chain_receipt"])
        request = proof["capsule"]["request"]
        sold = receipt["actual_input_units"]
        if (request["inputMint"] != record.get("execution_token_mint") or request["outputMint"] != chain.SOL
                or receipt["provider_reported_signature"] != record.get("sell_signature")
                or sold != record.get("sell_qty") or sold != event.get("qty")
                or before is None or before < sold or record["position_snapshot"].get("qty") != before - sold
                or event["event_type"] != ("partial_fill" if sold < before else "close")):
            raise ValueError("Close accounting differs from original chain wallet fill")


async def _candidate_events(session: AsyncSession, position_id: int, event_type: str) -> Iterable[TradeEvent]:
    result = await session.execute(
        select(TradeEvent).where(
            TradeEvent.position_id == int(position_id),
            TradeEvent.event_type == str(event_type),
        )
    )
    return tuple(result.scalars())


async def _apply_record(session: AsyncSession, record: Mapping[str, Any]) -> Position:
    _validate_record(record)
    position_id = int(record["position_id"])
    position = (
        await session.execute(select(Position).where(Position.id == position_id))
    ).scalar_one_or_none()
    if position is None:
        raise LookupError(f"position_id={position_id} not found")

    if position.address != record.get("address") or position.run_id != record.get("run_id"):
        raise ValueError("close recovery position ownership mismatch")
    if record.get("source_position_key") is not None and position.source_position_key != record["source_position_key"]:
        raise ValueError("close recovery entry lineage mismatch")
    if record.get("paper_fill_fx") is not None and position.dry_run is not True:
        raise ValueError("Original PAPER FX cannot replay over a LIVE SQL position")
    event_snapshot = dict(record["trade_event"])
    rid = recovery_id(record)
    candidates = await _candidate_events(session, position_id, str(event_snapshot["event_type"]))
    existing = next((event for event in candidates if _event_has_recovery_id(event, rid)), None)
    if existing is None:
        existing = next((event for event in candidates if _event_matches_snapshot(event, event_snapshot)), None)
    if existing is not None:
        if not _event_matches_snapshot(existing, event_snapshot):
            raise ValueError("close recovery event identity conflicts with payload")
        _tag_event(existing, rid)
        # SQL already contains this fill. Applying an old partial snapshot here
        # would reopen a later close or rewind another committed partial.
        return position
    else:
        before = record.get("expected_before_qty")
        if before is not None and position.qty != before:
            raise ValueError("close recovery pre-sell quantity mismatch")
        if position.closed:
            raise ValueError("cannot replay an unproven fill over a closed position")
        apply_position_snapshot(position, record["position_snapshot"])
        marker = json.dumps({"close_recovery_id": rid}, sort_keys=True, separators=(",", ":"))
        add_trade_event(
            session,
            position,
            event_type=str(event_snapshot["event_type"]),
            ts_utc=_parse_datetime(event_snapshot.get("ts_utc")),
            qty=event_snapshot.get("qty"),
            price_usd=event_snapshot.get("price_usd"),
            notional_usd=event_snapshot.get("notional_usd"),
            pnl_usd=event_snapshot.get("pnl_usd"),
            pnl_pct=event_snapshot.get("pnl_pct"),
            reason=event_snapshot.get("reason"),
            price_source=event_snapshot.get("price_source"),
            price_confidence=event_snapshot.get("price_confidence"),
            feature_snapshot_json=event_snapshot.get("feature_snapshot_json"),
            raw_json=marker,
        )
    return position


async def recover_pending(session: AsyncSession, path: Path) -> RecoveryResult:
    """Replay every unresolved record, one database transaction per sell."""

    resolved: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for record in load_pending_records(path):
        try:
            await _apply_record(session, record)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            append_status(path, record, status="retry_failed", error=exc)
            failed.append(
                {
                    "recovery_id": recovery_id(record),
                    "address": record.get("address"),
                    "position_id": record.get("position_id"),
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
            continue

        # If this append fails, do not pretend the quarantine is clear.  The DB
        # replay is idempotent, so the next process can safely retry and finish
        # the status transition without duplicating the trade event.
        append_status(path, record, status="resolved")
        resolved.append(dict(record))

    return RecoveryResult(
        resolved=tuple(resolved),
        failed=tuple(failed),
        pending_addresses=frozenset(pending_addresses(path)),
    )


__all__ = [
    "CloseRecoveryError",
    "POSITION_SNAPSHOT_FIELDS",
    "RecoveryResult",
    "append_journal_row",
    "append_pending",
    "append_prepared",
    "append_status",
    "apply_position_snapshot",
    "build_recovery_record",
    "capture_position_snapshot",
    "load_journal",
    "load_pending_records",
    "pending_addresses",
    "recover_pending",
    "recovery_id",
]
