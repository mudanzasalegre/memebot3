"""Durable buy intent -> fill -> SQL snapshot -> acknowledgement.

Never retry or send a transaction. A confirmed paper store can reconstruct a
lost SQL entry. Unknown live execution remains quarantined; SQL acknowledgement
alone is not a wallet/finality proof. Recovery must run only after entry owners
have finished, or at startup before they are created.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import logging
import math
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import DateTime, select

from db.models import Position, Token
from utils.atomic_json import read_json_strict, write_json_atomic

log = logging.getLogger(__name__)
VERSION = 1
TERMINAL = {"persisted", "no_fill"}
STATES = TERMINAL | {"prepared", "fill_received", "position_prepared"}
NO_EXECUTION_SIGNATURES = {"SIMULATION", "OUT_OF_WINDOW", "LIMIT_REACHED", "INSUFFICIENT_FUNDS",
    "NO_ROUTE", "NO_JUP_PRICE", "NO_JUP_ROUTE", "HIGH_IMPACT", "HIGH_IMPACT_EST",
    "INVALID_AMOUNT", "INVALID_IMPACT_LIMIT", "EXACT_PAPER_SIZE_REQUIRED", "POSITION_ALREADY_OPEN",
    "ENTRY_PRICE_OR_NOTIONAL_UNAVAILABLE", "QUOTE_PROOF_MISSING", "QUOTED_OUTPUT_TOO_SMALL",
    "PAPER_ARCHIVE_UNAVAILABLE", "ENTRY_INTENT_ALREADY_USED"}
FILL_FIELDS = ("qty_lamports", "signature", "buy_price_usd", "price_source", "price_confidence",
               "entry_notional_usd", "runner_trailing_policy", "venue")


class BuyRecoveryError(RuntimeError):
    pass


class BuyOutcomeUncertain(BuyRecoveryError):
    """Submission may have taken effect; absence is not established."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _time(value: Any) -> dt.datetime:
    parsed = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)


def position_snapshot(position: Position) -> dict[str, Any]:
    snapshot = {}
    for column in Position.__table__.columns:
        if column.name == "id":
            continue
        value = getattr(position, column.name, None)
        if value is None and column.default is not None:
            continue  # Let genuine ORM defaults, not fabricated labels, apply.
        snapshot[column.name] = _time(value).isoformat() if isinstance(value, dt.datetime) else value
    return snapshot


def position_from_snapshot(snapshot: Mapping[str, Any]) -> Position:
    fields = {}
    for column in Position.__table__.columns:
        if column.name in snapshot and column.name != "id":
            value = snapshot[column.name]
            fields[column.name] = _time(value) if value is not None and isinstance(column.type, DateTime) else value
    return Position(**fields)


class BuyAttempt:
    def __init__(self, store: "BuyRecoveryStore", row: dict[str, Any]):
        self.store, self.row = store, row

    @property
    def intent_id(self) -> str:
        return self.row["intent_id"]

    def _save(self, **changes) -> None:
        row = {**self.row, **changes, "updated_at": _now()}
        self.store._write(row)
        self.row = row

    def receive(self, response: Mapping[str, Any]) -> None:
        if not isinstance(response, Mapping):
            raise BuyOutcomeUncertain("Buy returned no structured outcome")
        qty = response.get("qty_lamports")
        if isinstance(qty, int) and not isinstance(qty, bool) and qty == 0:
            if str(response.get("signature") or "") not in NO_EXECUTION_SIGNATURES:
                raise BuyOutcomeUncertain("Zero quantity has no proven pre-execution rejection")
            self._save(state="no_fill", rejection=str(response["signature"]))
            return
        if not isinstance(qty, int) or isinstance(qty, bool) or not 0 < qty <= 2**63 - 1:
            raise BuyOutcomeUncertain("Buy quantity is not confirmed")
        if (not _positive(response.get("buy_price_usd"))
                or not _positive(response.get("entry_notional_usd"))
                or not str(response.get("signature") or "").strip()):
            raise BuyOutcomeUncertain("Buy price, notional or signature is unconfirmed")
        fill = {key: response.get(key) for key in FILL_FIELDS}
        self._save(state="fill_received", fill=fill, fill_received_at=_now())

    def capture_position(self, position: Position) -> None:
        if self.row["state"] != "fill_received":
            raise BuyRecoveryError("No positive buy result to persist")
        position.source_position_key = "buy:" + self.intent_id
        snapshot = position_snapshot(position)
        self.store._validate_identity(self.row, snapshot)
        self._save(state="position_prepared", position=snapshot)

    def confirm(self, position: Position) -> None:
        if self.row["state"] != "position_prepared" or position.id is None:
            raise BuyRecoveryError("Buy SQL acknowledgement lacks a persisted identity")
        self._save(state="persisted", position_id=int(position.id))


class BuyRecoveryStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._records: dict[str, dict[str, Any]] = {}
        self._active: set[str] = set()
        self._scope = contextvars.ContextVar(f"buy-recovery-scope:{id(self)}", default=None)
        for path in sorted(self.directory.glob("*.json")):
            try:
                row = read_json_strict(path)
                self._validate_row(row)
                if path.stem != row["intent_id"]:
                    raise ValueError("Intent filename mismatch")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise BuyRecoveryError("Unreadable buy recovery journal; trading must stay blocked") from exc
            if row["state"] not in TERMINAL:
                self._records[row["intent_id"]] = row

    @staticmethod
    def _validate_row(row: Mapping[str, Any]) -> None:
        if (not isinstance(row, Mapping) or type(row.get("version")) is not int or row.get("version") != VERSION
                or re.fullmatch(r"[0-9a-f]{32}", str(row.get("intent_id") or "")) is None
                or row.get("state") not in STATES or type(row.get("paper")) is not bool
                or not str(row.get("address") or "").strip() or not _positive(row.get("amount_sol"))
                or not isinstance(row.get("base_position"), Mapping)):
            raise ValueError("Invalid buy recovery record")
        base = row["base_position"]
        if (base.get("address") != row["address"] or base.get("dry_run") is not row["paper"]
                or base.get("buy_amount_sol") != row["amount_sol"]):
            raise ValueError("Buy recovery base identity mismatch")
        if "entry_features" in row:
            from runtime.trade_learning import validate_entry_features, _time as feature_time
            try:
                validate_entry_features(row["entry_features"], address=row["address"])
                if feature_time(row["entry_features"]["captured_at"]) != feature_time(row["created_at"]):
                    raise ValueError("Frozen feature capture differs from pre-buy journal time")
            except (RuntimeError, TypeError, KeyError, ValueError) as exc:
                raise ValueError("Invalid pre-buy feature proof") from exc
        if "entry_decision" in row:
            from runtime.entry_decision import validate_entry_decision
            try:
                validate_entry_decision(row["entry_decision"], entry_features=row["entry_features"],
                    intent_id=row["intent_id"], run_id=base.get("run_id"), paper=row["paper"], amount_sol=row["amount_sol"])
            except (RuntimeError, TypeError, KeyError, ValueError) as exc:
                raise ValueError("Invalid pre-buy decision proof") from exc
        if row["state"] in {"fill_received", "position_prepared", "persisted"}:
            fill = row.get("fill")
            if (not isinstance(fill, Mapping) or type(fill.get("qty_lamports")) is not int
                    or not 0 < fill["qty_lamports"] <= 2**63 - 1
                    or not _positive(fill.get("buy_price_usd")) or not _positive(fill.get("entry_notional_usd"))
                    or not isinstance(fill.get("signature"), str) or not fill["signature"].strip()):
                raise ValueError("Invalid received buy fill")
        if row["state"] in {"position_prepared", "persisted"}:
            if not isinstance(row.get("position"), Mapping):
                raise ValueError("Missing buy position snapshot")
            BuyRecoveryStore._validate_identity(row, row["position"])

    def _write(self, row: dict[str, Any]) -> None:
        self._validate_row(row)
        try:
            write_json_atomic(self.directory / (row["intent_id"] + ".json"), row)
        except (OSError, ValueError, TypeError) as exc:
            raise BuyRecoveryError("Cannot durably persist buy recovery state") from exc
        if row["state"] in TERMINAL:
            self._records.pop(row["intent_id"], None)
            # Retain complete evidence; only the active directory is read at startup.
            try:
                archive = self.directory / "resolved"
                archive.mkdir(parents=True, exist_ok=True)
                (self.directory / (row["intent_id"] + ".json")).replace(archive / (row["intent_id"] + ".json"))
            except OSError:
                log.warning("Buy recovery acknowledgement retained in active directory")
        else:
            self._records[row["intent_id"]] = row

    @contextmanager
    def scope(self):
        # The list is intentionally shared only with this entry's wait_for child
        # task; other task-local scopes receive independent containers.
        owned = []
        token = self._scope.set(owned)
        try:
            yield
        finally:
            for intent_id in owned:
                self._active.discard(intent_id)
            self._scope.reset(token)

    @property
    def pending_addresses(self) -> set[str]:
        return {row["address"] for key, row in self._records.items() if key not in self._active}

    def begin(self, position: Position, *, paper: bool, amount_sol: float,
              feature_vector=None, positive_pnl_ratio=0., auxiliary_observations=None,
              strategy_context=None, entry_decision=None) -> BuyAttempt:
        owned = self._scope.get()
        if owned is None:
            raise BuyRecoveryError("Buy requires an owned entry scope")
        if self._active or self.pending_addresses:
            raise BuyRecoveryError("Earlier buy execution is active or unresolved")
        row = {"version": VERSION, "intent_id": uuid.uuid4().hex, "state": "prepared",
               "created_at": _now(), "paper": paper, "address": position.address,
               "amount_sol": amount_sol, "base_position": position_snapshot(position)}
        if feature_vector is not None:
            from runtime.trade_learning import freeze_entry_features
            row["entry_features"] = freeze_entry_features(feature_vector, address=position.address,
                captured_at=row["created_at"], positive_pnl_ratio=positive_pnl_ratio,
                auxiliary_observations=auxiliary_observations, strategy_context=strategy_context)
        if entry_decision is not None:
            from runtime.entry_decision import bind_entry_decision
            row["entry_decision"] = bind_entry_decision(entry_decision, row)
        if (self.directory / (row["intent_id"] + ".json")).exists() or (
                self.directory / "resolved" / (row["intent_id"] + ".json")).exists():
            raise BuyRecoveryError("Buy intent identity already exists")
        self._write(row)  # Must complete before the caller can invoke any buyer.
        owned.append(row["intent_id"])
        self._active.add(row["intent_id"])
        return BuyAttempt(self, row)

    @staticmethod
    def _validate_identity(row: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
        if (snapshot.get("address") != row["address"] or snapshot.get("dry_run") is not row["paper"]
                or snapshot.get("source_position_key") != "buy:" + row["intent_id"]
                or snapshot.get("entry_qty") != row["fill"]["qty_lamports"]
                or snapshot.get("buy_tx_sig") != row["fill"]["signature"]
                or snapshot.get("buy_amount_sol") != row["amount_sol"]
                or snapshot.get("buy_price_usd") != row["fill"]["buy_price_usd"]
                or snapshot.get("entry_notional_usd") != row["fill"]["entry_notional_usd"]):
            raise BuyRecoveryError("Buy position identity conflicts with the received fill")

    async def recover(self, session, *, paper_portfolio: Mapping[str, Any]) -> dict[str, Any]:
        resolved, failed = [], []
        for intent_id, original in list(self._records.items()):
            if intent_id in self._active:
                continue  # Never reconcile an in-flight call or SQL commit.
            attempt = BuyAttempt(self, original)
            try:
                row = attempt.row
                if not row["paper"]:
                    raise BuyOutcomeUncertain("Live buy requires separate wallet/finality reconciliation")
                entry = paper_portfolio.get(row["address"])
                if entry is None and row["state"] == "prepared":
                    attempt._save(state="no_fill", reconciliation="confirmed_paper_store_absent")
                    resolved.append(intent_id)
                    continue
                if not isinstance(entry, Mapping) or entry.get("entry_intent_id") != intent_id:
                    raise BuyOutcomeUncertain("Paper store has no matching durable buy identity")
                if row["state"] == "prepared":
                    attempt.receive({"qty_lamports": entry.get("entry_qty"),
                        "signature": entry.get("buy_signature"), "buy_price_usd": entry.get("buy_price_usd"),
                        "entry_notional_usd": entry.get("entry_notional_usd"),
                        "price_source": entry.get("price_source"), "price_confidence": entry.get("price_confidence"),
                        "runner_trailing_policy": entry.get("runner_trailing_policy"), "venue": "paper"})
                    row = attempt.row
                fill = row.get("fill") or {}
                if (entry.get("entry_qty") != fill.get("qty_lamports")
                        or entry.get("amount_sol") != row["amount_sol"]
                        or entry.get("buy_price_usd") != fill.get("buy_price_usd")
                        or entry.get("entry_notional_usd") != fill.get("entry_notional_usd")
                        or entry.get("buy_signature") != fill.get("signature")):
                    raise BuyOutcomeUncertain("Paper fill conflicts with its buy recovery record")
                if row["state"] == "fill_received":
                    snapshot = {**row["base_position"], "qty": fill["qty_lamports"],
                        "entry_qty": fill["qty_lamports"], "buy_price_usd": fill["buy_price_usd"],
                        "entry_notional_usd": fill["entry_notional_usd"], "buy_tx_sig": fill["signature"],
                        "price_source_at_buy": fill.get("price_source"),
                        "price_confidence_at_buy": fill.get("price_confidence"),
                        "runner_trailing_policy": fill.get("runner_trailing_policy"),
                        "opened_at": entry.get("opened_at"), "source_position_key": "buy:" + intent_id}
                    _time(snapshot["opened_at"])
                    attempt.capture_position(position_from_snapshot(snapshot))
                    row = attempt.row
                snapshot = row["position"]
                self._validate_identity(row, snapshot)
                result = await session.execute(select(Position).where(Position.source_position_key == "buy:" + intent_id))
                existing = result.scalar_one_or_none()
                if existing is not None:
                    self._validate_identity(row, position_snapshot(existing))
                    # Never overwrite later monitoring, partials, or a closed row.
                    position = existing
                else:
                    if entry.get("closed") or entry.get("qty_lamports") != entry.get("entry_qty"):
                        raise BuyOutcomeUncertain("Paper position changed before its SQL identity was recovered")
                    conflict = await session.execute(select(Position.id).where(
                        Position.address == row["address"], Position.closed.is_(False)))
                    if conflict.first() is not None:
                        raise BuyOutcomeUncertain("Another open position owns this address")
                    token = await session.get(Token, row["address"])
                    if token is None:
                        session.add(Token(address=row["address"], symbol=snapshot.get("symbol")))
                    position = position_from_snapshot(snapshot)
                    session.add(position)
                await session.commit()
                attempt.confirm(position)
                resolved.append(intent_id)
            except Exception as exc:
                await session.rollback()
                failed.append({"intent_id": intent_id, "address": original["address"], "error_type": type(exc).__name__})
        return {"resolved": resolved, "failed": failed, "pending_addresses": sorted(self.pending_addresses)}
