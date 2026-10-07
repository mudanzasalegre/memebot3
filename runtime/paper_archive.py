"""Immutable per-trade paper outcomes; legacy JSONL remains read-only input.

The primary portfolio keeps a close until this archive confirms persistence.
This module does not submit orders or infer missing financial evidence.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Mapping

from utils.atomic_json import read_json_strict, write_json_atomic

VERSION = 1
FIELDS = frozenset("""
entry_intent_id buy_signature buy_tx_sig source_position_key token_address address token_mint symbol
run_id run_started_at config_profile config_hash strategy_version experiment_id dry_run test_event
opened_at closed_at closed qty_lamports entry_qty buy_price_usd amount_sol buy_amount_sol
entry_notional_usd buy_liquidity_usd execution_cost_model spot_entry_price_usd estimated_fees_usd
estimated_fees_sol realized_proceeds_sol execution_fill_count quantity_basis entry_route_quote
entry_regime entry_lane gate_profile runner_exit_profile exit_profile discovered_via
partial_taken partial_count partial_fill_events highest_pnl_pct max_pnl_pct_seen max_adverse_pnl_pct
peak_price peak_price_usd exit_state partial_ladder_state runner_trailing_policy paper_entry_policy realized_qty
realized_proceeds_usd realized_cost_usd realized_pnl_usd net_realized_pnl_usd net_realized_pnl_pct
effective_exit_price_usd total_pnl_usd total_pnl_pct net_total_pnl_usd net_total_pnl_pct net_total_pnl_sol
total_proceeds_sol first_partial_at last_partial_at last_partial_qty last_partial_price_usd
exit_reason exit_reason_full close_price_usd price_source price_confidence price_source_close
price_confidence_close require_jupiter_for_buy exact_paper_trade_size_sol pnl_pct exit_fill_events
time_to_partial_sec time_to_peak_sec peak_after_partial_pct exit_from_peak_giveback_pct outcome
""".split())


class PaperArchiveError(RuntimeError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")).hexdigest()


def _time(value):
    result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=dt.timezone.utc) if result.tzinfo is None else result.astimezone(dt.timezone.utc)


def entry_identity(row) -> str | None:
    """A checked causal UUID, not a mint/time guess or a numeric SQL row id."""
    raw = row.get("entry_intent_id")
    source = str(row.get("source_position_key") or "")
    source_id = source[4:] if source.startswith("buy:") else None
    if raw is not None and source_id is not None and raw != source_id:
        raise PaperArchiveError("Paper entry identity conflicts with its SQL lineage")
    if raw is None:
        raw = source_id
    if raw is not None:
        if not isinstance(raw, str) or re.fullmatch(r"[0-9a-f]{32}", raw) is None:
            raise PaperArchiveError("Malformed paper entry identity")
        return raw
    return None


def _validate_trade(trade):
    if (not isinstance(trade, Mapping) or trade.get("closed") is not True
            or type(trade.get("qty_lamports")) is not int or trade["qty_lamports"] != 0
            or not isinstance(trade.get("token_address"), str) or not trade["token_address"].strip()
            or trade.get("dry_run") is False):
        raise PaperArchiveError("Not a terminal paper trade snapshot")
    if _time(trade.get("closed_at")) < _time(trade.get("opened_at")):
        raise PaperArchiveError("Paper trade close precedes its entry")
    for field in ("address", "token_mint"):
        if trade.get(field) and trade[field] != trade["token_address"]:
            raise PaperArchiveError("Paper trade address lineage conflicts")
    identity = entry_identity(trade)
    if identity and trade.get("buy_signature") not in (None, "SIM-" + identity):
        raise PaperArchiveError("Paper trade buy signature conflicts")
    return identity or "legacy_" + _digest([trade.get("run_id"), trade["token_address"],
        _time(trade["opened_at"]).isoformat(), trade.get("buy_signature") or trade.get("buy_tx_sig")])


def _snapshot(entry, token):
    if not isinstance(entry, Mapping):
        raise PaperArchiveError("Paper outcome must be an object")
    trade = {key: copy.deepcopy(value) for key, value in entry.items() if key in FIELDS}
    if trade.get("token_address") not in (None, token):
        raise PaperArchiveError("Paper archive mint conflicts with its portfolio key")
    trade["token_address"] = token
    # Keep public financial proof, never arbitrary provider payload/credentials.
    for name, allowed in (
        ("entry_route_quote", {"in_amount", "out_amount", "impact_bps", "route_count", "max_impact_pct"}),
        ("execution_cost_model", {"version", "observed_execution", "slippage_bps", "fee_sol_per_fill"}),
    ):
        if isinstance(trade.get(name), Mapping):
            trade[name] = {key: value for key, value in trade[name].items() if key in allowed}
    if "exit_fill_events" in trade:
        events = trade["exit_fill_events"]
        if (not isinstance(events, list) or any(not isinstance(event, Mapping)
                or not isinstance(event.get("response"), Mapping) for event in events)):
            raise PaperArchiveError("Malformed paper exit fill evidence")
        responses = {"ok", "signature", "venue", "price_used_usd", "price_source_close", "price_confidence_close",
                     "qty_sold", "qty_left", "partial", "filled_at", "exit_intent_id"}
        trade["exit_fill_events"] = [{"intent_id": event["intent_id"], "qty_before": event["qty_before"],
            "response": {key: value for key, value in event["response"].items() if key in responses}}
            for event in events]
    return trade


def _validate_record(row, filename=None):
    if (not isinstance(row, Mapping) or type(row.get("version")) is not int
            or row.get("version") != VERSION):
        raise PaperArchiveError("Unknown paper archive schema")
    key = _validate_trade(row.get("trade"))
    if row.get("trade_id") != key or row.get("payload_sha256") != _digest(row["trade"]):
        raise PaperArchiveError("Paper archive identity/checksum mismatch")
    if filename is not None and filename != key + ".json":
        raise PaperArchiveError("Paper archive filename mismatch")
    return key


def archive_closed_trade(data_directory: Path, entry: Mapping, *, token: str) -> str:
    """Confirm an immutable snapshot before the portfolio can replace this buy."""
    try:
        trade = _snapshot(entry, token)
        key = _validate_trade(trade)
        record = {"version": VERSION, "trade_id": key, "payload_sha256": _digest(trade), "trade": trade}
        path = Path(data_directory) / "paper_closed_trades" / (key + ".json")
        if path.exists():
            previous = read_json_strict(path)
            _validate_record(previous, path.name)
            if previous != record:
                raise PaperArchiveError("An archived paper outcome cannot be overwritten by a different snapshot")
        else:
            write_json_atomic(path, record)
        return key
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise PaperArchiveError("Paper outcome was not durably archived") from exc


def read_closed_evidence(data_directory: Path) -> tuple[list[dict], list[dict]]:
    """Retain valid rows and surface every unreadable cell/legacy row explicitly."""
    data_directory = Path(data_directory)
    rows, issues = [], []
    for path in sorted((data_directory / "paper_closed_trades").glob("*.json")):
        try:
            record = read_json_strict(path)
            _validate_record(record, path.name)
            rows.append(dict(record["trade"]))
        except (PaperArchiveError, OSError, ValueError, TypeError, KeyError) as exc:
            issues.append({"source": "paper_closed_trades/" + path.name, "error_type": type(exc).__name__})
    legacy = data_directory / "paper_closed_trades.jsonl"
    try:
        lines = legacy.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    except (OSError, UnicodeError) as exc:
        lines = []
        issues.append({"source": legacy.name, "error_type": type(exc).__name__})
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Non-object legacy closed record")
            rows.append(row)  # Legacy unknown costs/identities are not repaired or invented.
        except (ValueError, TypeError) as exc:
            issues.append({"source": legacy.name, "line": index + 1, "error_type": type(exc).__name__})
    return rows, issues
