"""Restart-safe green LIVE risk projection, never an execution/retry mechanism.

Original buy/sell journals and committed SQL identities remain authoritative.
The bot's single-instance lock owns this store; the RLock also serializes local
reservations. No import-time I/O, provider, FX, signer or wallet access.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

from runtime.buy_recovery import BuyRecoveryStore, position_snapshot
from runtime.sell_recovery import SellRecoveryStore
from runtime.execution_provenance import checked_live_fill
from utils.atomic_json import read_json_strict, write_json_atomic
from utils.raw_units import sol_to_lamports

LANE = "pump_early_green_candle_sniper"
VERSION = 1


class GreenCanaryRiskError(RuntimeError):
    pass


def _time(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("Risk event lacks original UTC time")
    # SQLAlchemy's SQLite UTC columns come back without a timezone.
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _journals(directory: Path, validator) -> dict[str, dict]:
    rows = {}
    for path in sorted([*directory.glob("*.json"), *(directory / "resolved").glob("*.json")]):
        row = read_json_strict(path)
        validator(row)
        if path.stem != row["intent_id"] or row["intent_id"] in rows and rows[row["intent_id"]] != row:
            raise ValueError("Duplicate/conflicting original journal identity")
        rows[row["intent_id"]] = row
    return rows


def _cash_effect(receipt: dict, *, side: str) -> int:
    """Conservative native-wallet cash budget, including observed account locks.

    Do not credit rent refunds or wrapped balances above checked principal less
    wallet network fees. This is a risk-budget measure, not a profit claim.
    """
    principal = -receipt["actual_input_units"] if side == "buy" else receipt["actual_output_units"]
    return min(receipt["wallet_native_delta_lamports"], principal - receipt["wallet_network_fee_lamports"])


class GreenCanaryRiskStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "data" / "metrics" / "green_live_risk.json"
        self._lock = threading.RLock()
        self._seen = self.path.exists()
        self.ready = False
        self.error: str | None = None

    def _read(self) -> dict:
        if not self.path.exists():
            if self._seen:
                raise ValueError("Durable risk state disappeared")
            return {"version": VERSION, "records": {}, "disabled_until": None, "disable_reason": None}
        doc = read_json_strict(self.path)
        if (not isinstance(doc, dict) or type(doc.get("version")) is not int or doc["version"] != VERSION
                or not isinstance(doc.get("records"), dict)
                or doc.get("sha256") != _digest({k: v for k, v in doc.items() if k != "sha256"})):
            raise ValueError("Invalid durable risk document")
        if doc.get("disabled_until") is not None:
            _time(doc["disabled_until"])
            if not isinstance(doc.get("disable_reason"), str) or not doc["disable_reason"]:
                raise ValueError("Risk disable deadline lacks a reason")
        for key, row in doc["records"].items():
            if (not isinstance(row, dict) or key != row.get("id") or row.get("state") not in {"pending", "open", "closed", "no_fill"}
                    or not isinstance(row.get("address"), str) or not row["address"]
                    or type(row.get("reserved")) is not bool or type(row.get("unproved")) is not bool
                    or not isinstance(row.get("buy_fingerprint"), str) or len(row["buy_fingerprint"]) != 64
                    or not isinstance(row.get("sell_fingerprints"), dict)
                    or row.get("pnl_lamports") is not None and type(row["pnl_lamports"]) is not int):
                raise ValueError("Invalid durable risk record")
            _time(row["buy_at"])
            if _time(row["buy_at"]) > dt.datetime.now(dt.timezone.utc):
                raise ValueError("Risk buy is future-dated")
            if row["state"] == "closed":
                if not _time(row["buy_at"]) <= _time(row["closed_at"]) <= dt.datetime.now(dt.timezone.utc):
                    raise ValueError("Risk close predates original buy")
            elif row.get("closed_at") is not None or row.get("pnl_lamports") is not None:
                raise ValueError("Unclosed risk record declares a financial outcome")
        return doc

    def _save(self, doc: dict) -> None:
        body = {k: v for k, v in doc.items() if k != "sha256"}
        write_json_atomic(self.path, {**body, "sha256": _digest(body)})
        self._seen = True

    def _fail(self, exc: Exception):
        self.ready, self.error = False, type(exc).__name__
        raise GreenCanaryRiskError("Original risk evidence unavailable; new green LIVE buys blocked") from exc

    def reconcile(self, positions: Iterable[Any]) -> dict:
        """Rebuild original event grain, retaining every earlier source identity."""
        with self._lock:
            try:
                previous = self._read()
                buys = _journals(self.root / "data" / "metrics" / "buy_recovery", BuyRecoveryStore._validate_row)
                sells = _journals(self.root / "data" / "metrics" / "sell_recovery", SellRecoveryStore.validate_row)
                sql = {}
                for pos in positions:
                    p = dict(pos) if isinstance(pos, Mapping) else {"id": pos.id, **position_snapshot(pos)}
                    if p.get("dry_run") is not False or p.get("entry_lane") != LANE:
                        continue
                    key = p.get("source_position_key") or "sql:" + str(p.get("id"))
                    if key in sql or type(p.get("id")) is not int or p["id"] <= 0 or type(p.get("closed")) is not bool:
                        raise ValueError("Ambiguous original SQL position")
                    sql[key] = p
                records = {}
                for intent, buy in buys.items():
                    base = buy["base_position"]
                    if buy["paper"] or base.get("entry_lane") != LANE:
                        continue
                    created = _time(buy["created_at"])
                    filled = _time(buy.get("fill_received_at") or buy["created_at"])
                    if not created <= filled <= dt.datetime.now(dt.timezone.utc):
                        raise ValueError("Original buy event time is invalid")
                    key = "buy:" + intent
                    p = sql.pop(key, None)
                    record = {"id": key, "address": buy["address"], "buy_at": _time(buy.get("fill_received_at") or buy["created_at"]).isoformat(),
                        "buy_fingerprint": _digest({"intent_id": intent, "created_at": buy["created_at"], "base": base}),
                        "sell_fingerprints": {}, "state": "no_fill" if buy["state"] == "no_fill" else "pending",
                        "reserved": False, "unproved": False, "closed_at": None, "pnl_lamports": None, "exit_reason": None}
                    if p is not None:
                        if buy["state"] not in {"position_prepared", "persisted"}:
                            raise ValueError("SQL position has no original prepared buy fill")
                        BuyRecoveryStore._validate_identity(buy, p)
                        if any(p.get(k) != base.get(k) for k in ("token_mint", "run_id", "entry_lane")):
                            raise ValueError("Original buy/SQL causal identity differs")
                        if buy.get("position_id", p["id"]) != p["id"]:
                            raise ValueError("Original buy/SQL identity differs")
                        record.update(state="closed" if p["closed"] else "open",
                            closed_at=_time(p["closed_at"]).isoformat() if p["closed"] else None,
                            exit_reason=p.get("exit_reason") or p.get("exit_reason_full"))
                    elif buy["state"] == "persisted":
                        raise ValueError("Original persisted buy lost its SQL identity")
                    if buy["state"] not in {"prepared", "no_fill"}:
                        if buy.get("execution") is None:
                            record["unproved"] = True
                        else:
                            receipt = checked_live_fill(buy, buy["fill"], side="buy")
                            record["unproved"] = not receipt["financial_finality_verified"]
                    related = [s for s in sells.values() if not s["paper"] and s["before"].get("source_position_key") == key]
                    received = [s for s in related if s["state"] in {"fill_received", "sql_prepared", "resolved"}]
                    pending = any(s["state"] in {"prepared", "fill_received", "sql_prepared"} for s in related)
                    if pending:
                        record["unproved"] = True
                    for s in received:
                        if p is None or s["position_id"] != p["id"] or s["address"] != record["address"]:
                            raise ValueError("Sell belongs to another original position")
                        record["sell_fingerprints"][s["intent_id"]] = _digest(s["fill"])
                    if record["state"] == "closed" and not record["unproved"]:
                        qty = buy["fill"]["qty_lamports"]
                        ordered = sorted(received, key=lambda s: (_time(s["fill"]["filled_at"]), s["intent_id"]))
                        cash = _cash_effect(receipt, side="buy")
                        for s in ordered:
                            before = s["before"]
                            if (before["qty"] != qty or before.get("buy_tx_sig") != buy["fill"]["signature"]
                                    or any(before.get(k) != p.get(k) for k in ("token_mint", "run_id", "entry_lane", "entry_qty", "buy_amount_sol", "entry_notional_usd"))
                                    or _time(s["fill"]["filled_at"]) < _time(record["buy_at"])):
                                raise ValueError("Original sell coverage is discontinuous")
                            qty -= s["fill"]["qty_sold"]
                            r = checked_live_fill(s, s["fill"], side="sell") if s.get("execution") is not None else None
                            if r is None or not r["financial_finality_verified"]:
                                record["unproved"] = True
                                break
                            if (s["execution"]["capsule"]["request"]["taker"] != buy["execution"]["capsule"]["request"]["taker"]
                                    or r["input_decimals"] != receipt["output_decimals"]):
                                raise ValueError("Original sell wallet/decimals differ from buy")
                            cash += _cash_effect(r, side="sell")
                        if not record["unproved"] and qty == 0 and ordered and _time(ordered[-1]["fill"]["filled_at"]) == _time(record["closed_at"]):
                            if p.get("exit_tx_sig") != ordered[-1]["fill"]["signature"]:
                                raise ValueError("SQL close differs from original final sell signature")
                            record["pnl_lamports"] = cash
                    records[key] = record
                # Historical SQL without its original journal stays unknown.
                for key, p in sql.items():
                    record = {"id": key, "address": p["address"], "buy_at": _time(p["opened_at"]).isoformat(),
                        "buy_fingerprint": _digest({**{k: p.get(k) for k in ("id", "address", "source_position_key", "buy_tx_sig", "entry_qty", "buy_amount_sol")}, "opened_at": _time(p["opened_at"]).isoformat()}),
                        "sell_fingerprints": {}, "state": "closed" if p["closed"] else "open", "reserved": False,
                        "unproved": True, "closed_at": _time(p["closed_at"]).isoformat() if p["closed"] else None,
                        "pnl_lamports": None, "exit_reason": p.get("exit_reason") or p.get("exit_reason_full")}
                    records[key] = record
                for key, old in previous["records"].items():
                    row = records.get(key)
                    if (row is None or row["buy_fingerprint"] != old["buy_fingerprint"]
                            or any(row["sell_fingerprints"].get(k) != v for k, v in old["sell_fingerprints"].items())
                            or old["state"] in {"closed", "no_fill"} and row["state"] != old["state"]
                            or old["state"] == "open" and row["state"] == "pending"
                            or old["state"] == "closed" and (row["closed_at"] != old["closed_at"] or row["exit_reason"] != old["exit_reason"])
                            or old["pnl_lamports"] is not None and row["pnl_lamports"] != old["pnl_lamports"]):
                        raise ValueError("Original risk history disappeared, changed or rewound")
                    row["reserved"] = old["reserved"]
                doc = {**previous, "records": records}
                # Validate all generated records before replacing durable state.
                for row in records.values():
                    if _time(row["buy_at"]) > dt.datetime.now(dt.timezone.utc) or row["state"] == "closed" and not _time(row["buy_at"]) <= _time(row["closed_at"]) <= dt.datetime.now(dt.timezone.utc):
                        raise ValueError("Original risk event clock is invalid")
                self._save(doc)
                self.ready, self.error = True, None
                return self.snapshot()
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                self._fail(exc)

    def snapshot(self, *, now: dt.datetime | None = None, excluding: str | None = None) -> dict:
        with self._lock:
            try:
                doc = self._read()
                now = _time(now or dt.datetime.now(dt.timezone.utc))
                rows = [r for k, r in doc["records"].items() if k != excluding and r["state"] != "no_fill"]
                daily_buys, daily_loss, consecutive = {}, {}, 0
                for row in rows:
                    day = _time(row["buy_at"]).date().isoformat()
                    daily_buys[day] = daily_buys.get(day, 0) + 1
                closed = sorted((r for r in rows if r["state"] == "closed"), key=lambda r: (_time(r["closed_at"]), r["id"]))
                until, reason = doc["disabled_until"], doc["disable_reason"]
                for row in closed:
                    pnl = row["pnl_lamports"]
                    if pnl is not None:
                        if pnl < 0:
                            day = _time(row["closed_at"]).date().isoformat()
                            daily_loss[day] = daily_loss.get(day, 0) + -pnl
                            consecutive += 1
                        else:
                            consecutive = 0
                    if str(row["exit_reason"] or "").upper() == "LIQUIDITY_CRUSH":
                        deadline = _time(row["closed_at"]) + dt.timedelta(minutes=240)
                        if until is None or deadline > _time(until):
                            until, reason = deadline.isoformat(), "liquidity_crush"
                return {"daily_buys": daily_buys, "daily_loss_sol": {d: u / 1e9 for d, u in daily_loss.items()},
                    "daily_loss_lamports": daily_loss, "consecutive_losses": consecutive,
                    "unvalued_closes": sum(r["pnl_lamports"] is None for r in closed),
                    "pending_buys": sum(r["state"] == "pending" for r in rows),
                    "unproved_positions": sum(r["unproved"] for r in rows),
                    "open_positions": sum(r["state"] in {"open", "pending"} for r in rows),
                    "disabled_until": until, "last_disable_reason": reason,
                    "disabled": until is not None and now < _time(until),
                    "ready": self.ready, "ledger_error": self.error, "source_records": len(rows)}
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self._fail(exc)

    def reserve(self, row: dict, token: dict, evaluate) -> tuple[bool, str]:
        with self._lock:
            try:
                BuyRecoveryStore._validate_row(row)
                if row["paper"] or row["base_position"].get("entry_lane") != LANE or row["state"] != "prepared" or row.get("execution") is not None:
                    raise ValueError("Reservation is not an original green pre-execution intent")
                if _time(row["created_at"]) > dt.datetime.now(dt.timezone.utc):
                    raise ValueError("Original reservation is future-dated")
                path = self.root / "data" / "metrics" / "buy_recovery" / (row["intent_id"] + ".json")
                if read_json_strict(path) != row:
                    raise ValueError("Reservation differs from durable original buy intent")
                key, doc = "buy:" + row["intent_id"], self._read()
                if doc["records"].get(key, {}).get("reserved"):
                    return False, "buy_intent_already_reserved"
                ok, reason = evaluate(token, risk=self.snapshot(excluding=key))
                if not ok:
                    return ok, reason
                doc["records"][key] = {"id": key, "address": row["address"], "buy_at": _time(row["created_at"]).isoformat(),
                    "buy_fingerprint": _digest({"intent_id": row["intent_id"], "created_at": row["created_at"], "base": row["base_position"]}),
                    "sell_fingerprints": {}, "state": "pending", "reserved": True, "unproved": False,
                    "closed_at": None, "pnl_lamports": None, "exit_reason": None}
                self._save(doc)  # Must succeed before any buyer/signing/dispatch.
                return True, "ok"
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                self._fail(exc)

    def disable(self, reason: str, *, minutes: int = 240) -> None:
        with self._lock:
            try:
                if not isinstance(reason, str) or not reason or type(minutes) is not int or minutes <= 0:
                    raise ValueError("Invalid durable disable event")
                doc = self._read()
                until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)
                if doc["disabled_until"] is None or until > _time(doc["disabled_until"]):
                    doc.update(disabled_until=until.isoformat(), disable_reason=reason)
                    self._save(doc)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self._fail(exc)
