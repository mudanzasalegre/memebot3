"""Pre-execution sell intents bridge submission/response loss to close replay.

No transaction is sent here. Paper fill identities allow deterministic recovery;
ambiguous live submissions require separate execution reconciliation. The existing
close outbox owns idempotent SQL position/trade-event replay.
"""
from __future__ import annotations

import copy
import datetime as dt
import math
import re
import uuid
from pathlib import Path
from typing import Any, Mapping

from runtime.buy_recovery import position_snapshot
from utils.atomic_json import read_json_strict, write_json_atomic

TERMINAL = {"resolved", "no_fill"}
STATES = TERMINAL | {"prepared", "fill_received", "sql_prepared"}
NO_FILL = {"INVALID_ADDRESS", "INVALID_MINT", "NO_QTY", "SKIP_LOW_LIQ", "INVALID_QUANTITY",
           "EXIT_QUOTE_UNAVAILABLE", "EXIT_PRICE_UNAVAILABLE", "FEE_VALUATION_UNAVAILABLE"}
FILL_FIELDS = ("signature", "price_used_usd", "price_source_close", "price_confidence_close",
               "qty_sold", "qty_left", "partial", "filled_at", "venue")
LINEAGE = ("entry_intent_id", "buy_signature", "entry_qty", "buy_price_usd", "amount_sol",
           "entry_notional_usd", "opened_at", "run_id")


class SellRecoveryError(RuntimeError):
    pass


class SellOutcomeUncertain(SellRecoveryError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _quantity(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def _time(value):
    result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=dt.timezone.utc) if result.tzinfo is None else result


class SellAttempt:
    def __init__(self, store, row):
        self.store, self.row = store, row

    @property
    def intent_id(self):
        return self.row["intent_id"]

    def save(self, **changes):
        row = {**self.row, **copy.deepcopy(changes), "updated_at": now_iso()}
        self.store._write(row)
        self.row = row

    def receive(self, response):
        if self.row["state"] not in {"prepared", "fill_received"}:
            raise SellRecoveryError("Sell receipt cannot rewind a prepared SQL/terminal state")
        if not isinstance(response, Mapping):
            raise SellOutcomeUncertain("Sell response is missing")
        if response.get("ok") is False:
            if self.row["state"] == "fill_received":
                raise SellOutcomeUncertain("A received sell fill cannot become a rejection")
            code = str(response.get("error") or response.get("err") or response.get("signature") or "")
            signature = response.get("signature")
            if (code not in NO_FILL or response.get("qty_sold") not in (None, 0)
                    or (signature and signature != code)):
                raise SellOutcomeUncertain("Sell rejection does not prove submission absent")
            self.save(state="no_fill", rejection=code)
            return None
        fill = {key: response.get(key) for key in FILL_FIELDS}
        fill["filled_at"] = fill.get("filled_at") or now_iso()
        self.store.validate_fill(self.row, fill)
        if self.row["state"] == "fill_received" and self.row.get("fill") != fill:
            raise SellOutcomeUncertain("Sell receipt conflicts with the persisted fill")
        self.save(state="fill_received", fill=fill)
        return {**dict(response), **fill, "_sell_intent_id": self.intent_id,
                "_qty_before": self.row["before"]["qty"]}


class SellRecoveryStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.records = {}
        self.active = set()
        for path in sorted(self.directory.glob("*.json")):
            try:
                row = read_json_strict(path)
                self.validate_row(row)
                if path.stem != row["intent_id"]:
                    raise ValueError("Sell intent filename mismatch")
            except (OSError, ValueError, TypeError, KeyError, SellRecoveryError) as exc:
                raise SellRecoveryError("Sell recovery journal is unreadable; keep trading blocked") from exc
            if row["state"] not in TERMINAL:
                self.records[row["intent_id"]] = row

    @staticmethod
    def validate_fill(row, fill):
        before_qty, requested = row["before"]["qty"], row["quantity"]
        if (not isinstance(fill, Mapping) or not isinstance(fill.get("signature"), str)
                or not fill["signature"].strip() or not _positive(fill.get("price_used_usd"))
                or not _quantity(fill.get("qty_sold")) or fill["qty_sold"] != requested
                or not _quantity(fill.get("qty_left")) or fill["qty_left"] != before_qty - requested
                or type(fill.get("partial")) is not bool or fill["partial"] is not (requested < before_qty)):
            raise SellOutcomeUncertain("Sell quantity/price/identity is not a checked fill")
        _time(fill.get("filled_at"))

    @staticmethod
    def validate_row(row):
        if (not isinstance(row, Mapping) or type(row.get("version")) is not int or row["version"] != 1
                or re.fullmatch(r"[0-9a-f]{32}", str(row.get("intent_id") or "")) is None
                or row.get("state") not in STATES or type(row.get("paper")) is not bool
                or not isinstance(row.get("before"), Mapping) or not row.get("address")
                or not _quantity(row.get("quantity")) or row["quantity"] <= 0
                or not _quantity(row["before"].get("qty")) or row["quantity"] > row["before"]["qty"]
                or row["before"].get("closed") is not False
                or type(row.get("position_id")) is not int or row["position_id"] <= 0
                or not _quantity(row["before"].get("entry_qty")) or row["before"]["entry_qty"] < row["before"]["qty"]
                or not _positive(row["before"].get("buy_price_usd")) or not _positive(row["before"].get("entry_notional_usd"))
                or row["before"].get("address") != row["address"]
                or row["before"].get("dry_run") is not row["paper"]):
            raise ValueError("Invalid sell intent")
        if row["state"] in {"fill_received", "sql_prepared", "resolved"}:
            SellRecoveryStore.validate_fill(row, row.get("fill"))
        if row["state"] in {"sql_prepared", "resolved"}:
            record = row.get("sql_record")
            if (not isinstance(record, Mapping) or record.get("recovery_id") != row["intent_id"]
                    or record.get("position_id") != row["position_id"] or record.get("address") != row["address"]):
                raise ValueError("Sell SQL recovery identity mismatch")
            fill, event, snapshot = row["fill"], record.get("trade_event"), record.get("position_snapshot")
            if (not isinstance(event, Mapping) or not isinstance(snapshot, Mapping)
                    or record.get("expected_before_qty") != row["before"]["qty"]
                    or event.get("qty") != fill["qty_sold"] or event.get("price_usd") != fill["price_used_usd"]
                    or event.get("event_type") != ("partial_fill" if fill["partial"] else "close")
                    or _time(event.get("ts_utc")) != _time(fill["filled_at"])
                    or snapshot.get("qty") != fill["qty_left"] or snapshot.get("closed") is not (not fill["partial"])):
                raise ValueError("Sell SQL snapshot conflicts with the received fill")

    def _write(self, row):
        self.validate_row(row)
        path = self.directory / (row["intent_id"] + ".json")
        previous = self.records.get(row["intent_id"])
        if previous is None and path.exists():
            previous = read_json_strict(path)
        transitions = {"prepared": {"prepared", "fill_received", "no_fill"},
                       "fill_received": {"fill_received", "sql_prepared"},
                       "sql_prepared": {"sql_prepared", "resolved"},
                       "resolved": {"resolved"}, "no_fill": {"no_fill"}}
        if ((previous is not None and row["state"] not in transitions[previous["state"]])
                or (previous is None and (self.directory / "resolved" / path.name).exists())):
            raise SellRecoveryError("A stale sell attempt cannot rewind or resurrect its journal")
        try:
            write_json_atomic(path, row)
        except (OSError, ValueError, TypeError) as exc:
            raise SellRecoveryError("Cannot durably persist sell intent") from exc
        if row["state"] in TERMINAL:
            self.records.pop(row["intent_id"], None)
            try:
                archive = self.directory / "resolved"
                archive.mkdir(parents=True, exist_ok=True)
                (self.directory / (row["intent_id"] + ".json")).replace(archive / (row["intent_id"] + ".json"))
            except OSError:
                pass  # Terminal acknowledgement remains durable in its original location.
        else:
            self.records[row["intent_id"]] = row

    @property
    def pending_addresses(self):
        return {row["address"] for key, row in self.records.items() if key not in self.active}

    def begin(self, position, quantity, *, paper, reason, partial_plan=None, paper_before=None):
        if any(row["address"] == position.address for row in self.records.values()):
            raise SellRecoveryError("Earlier sell of this position is active or unresolved")
        before = position_snapshot(position)
        if paper:
            if (not isinstance(paper_before, Mapping) or paper_before.get("closed")
                    or paper_before.get("qty_lamports") != before.get("qty")
                    or paper_before.get("entry_qty") != before.get("entry_qty")
                    or paper_before.get("buy_price_usd") != before.get("buy_price_usd")
                    or paper_before.get("entry_notional_usd") != before.get("entry_notional_usd")):
                raise SellRecoveryError("Paper and SQL entry ownership/quantities conflict")
            source = str(before.get("source_position_key") or "")
            if ((source.startswith("buy:") and source != "buy:" + str(paper_before.get("entry_intent_id") or ""))
                    or (before.get("buy_tx_sig") is not None and before["buy_tx_sig"] != paper_before.get("buy_signature"))):
                raise SellRecoveryError("Paper and SQL buy lineage conflict")
        row = {"version": 1, "intent_id": uuid.uuid4().hex, "created_at": now_iso(), "state": "prepared",
               "address": position.address, "position_id": position.id, "paper": paper,
               "quantity": quantity, "reason": str(reason), "before": before,
               "partial_plan": copy.deepcopy(partial_plan if partial_plan is not None else {}),
               "paper_before": copy.deepcopy(paper_before)}
        if (self.directory / (row["intent_id"] + ".json")).exists() or (
                self.directory / "resolved" / (row["intent_id"] + ".json")).exists():
            raise SellRecoveryError("Sell intent identity already exists")
        self._write(row)
        self.active.add(row["intent_id"])
        return SellAttempt(self, row)

    def finish(self, attempt):
        self.active.discard(attempt.intent_id)

    def prepare_sql(self, record):
        row = self.records.get(record.get("recovery_id"))
        if row is None:
            return  # Existing v2 close outbox compatibility.
        if row["state"] not in {"fill_received", "sql_prepared"}:
            raise SellRecoveryError("Sell has no received fill to persist")
        if row["state"] == "sql_prepared":
            if (row["sql_record"]["trade_event"] != record["trade_event"]
                    or row["sql_record"]["position_snapshot"] != record["position_snapshot"]):
                raise SellRecoveryError("Sell SQL payload conflicts with the checked fill")
            return  # Reusing a checked snapshot needs no duplicate fsync.
        SellAttempt(self, row).save(state="sql_prepared", sql_record=record)

    def acknowledge(self, record):
        row = self.records.get(record.get("recovery_id"))
        if row is not None:
            if row["state"] != "sql_prepared":
                raise SellRecoveryError("Sell has no SQL snapshot to acknowledge")
            if row["sql_record"]["trade_event"] != record["trade_event"]:
                raise SellRecoveryError("Sell SQL acknowledgement payload conflicts")
            SellAttempt(self, row).save(state="resolved", sql_record=record)

    def recover_to_outbox(self, *, paper_portfolio, build_record):
        records, failures = [], []
        for key, original in list(self.records.items()):
            if key in self.active:
                continue
            attempt = SellAttempt(self, original)
            try:
                row = attempt.row
                if row["state"] == "sql_prepared":
                    records.append(copy.deepcopy(row["sql_record"]))
                    continue
                if not row["paper"]:
                    raise SellOutcomeUncertain("Live sell requires wallet/execution reconciliation")
                entry = paper_portfolio.get(row["address"])
                before = row["paper_before"]
                if (not isinstance(entry, Mapping) or any(entry.get(field) != before.get(field) for field in LINEAGE)):
                    raise SellOutcomeUncertain("Paper sell lineage is missing or conflicting")
                events = [event for event in entry.get("exit_fill_events", [])
                          if isinstance(event, Mapping) and event.get("intent_id") == key]
                if not events:
                    if row["state"] == "prepared" and entry.get("qty_lamports") == before.get("qty_lamports") and not entry.get("closed"):
                        attempt.save(state="no_fill", reconciliation="confirmed_paper_fill_absent")
                        continue
                    raise SellOutcomeUncertain("Paper fill identity is absent")
                if len(events) != 1 or events[0].get("qty_before") != row["before"]["qty"]:
                    raise SellOutcomeUncertain("Paper sell event is conflicting")
                response = attempt.receive(events[0]["response"])
                if response is None:
                    raise SellOutcomeUncertain("A persisted paper event cannot be a rejection")
                record = build_record(attempt.row, response)
                self.prepare_sql(record)
                records.append(record)
            except Exception as exc:
                failures.append({"intent_id": key, "address": original["address"], "error_type": type(exc).__name__})
        return records, failures
