from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from config.config import PROJECT_ROOT
from ml.data_contract import normalize_candidate_event_row
from runtime.paper_archive import PaperArchiveError, entry_identity, read_closed_evidence

_INCLUDE_TEST_EVENTS = False


def set_include_test_events(value: bool) -> None:
    global _INCLUDE_TEST_EVENTS
    _INCLUDE_TEST_EVENTS = bool(value)


def include_test_events_enabled() -> bool:
    return bool(_INCLUDE_TEST_EVENTS)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _normalize_event_rows(rows: Iterable[dict[str, Any]], *, path: Path, candidate_only: bool = False) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        event = str(row.get("event_type") or row.get("event") or row.get("action") or "").strip().lower()
        should_normalize = (
            not candidate_only
            or event.startswith("candidate_")
            or any(key in row for key in ("decision_action", "stage", "reason", "entry_lane"))
        )
        if should_normalize:
            normalized.append(
                normalize_candidate_event_row(
                    row,
                    source_file=str(path),
                    row_index=index,
                )
            )
        else:
            item = dict(row)
            item.setdefault("row_lineage", {"source_file": str(path), "row_index": index})
            normalized.append(item)
    return normalized


def dedupe_candidate_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = normalize_candidate_event_row(row)
        key = str(item.get("decision_id") or "").strip()
        if not key:
            key = "|".join(
                str(item.get(part) or "").strip().lower()
                for part in ("address", "candidate_stage", "decision", "reason", "lane")
            )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def is_test_event(row: dict[str, Any]) -> bool:
    return boolish(row.get("test_event"), False) or str(row.get("run_id") or "").strip().upper() == "SMOKE"


def filter_test_events(rows: Iterable[dict[str, Any]], *, include_test_events: bool | None = None) -> list[dict[str, Any]]:
    include = _INCLUDE_TEST_EVENTS if include_test_events is None else bool(include_test_events)
    if include:
        return list(rows)
    return [row for row in rows if not is_test_event(row)]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def write_markdown(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if out != out:
            return default
        return out
    except Exception:
        return default


def inum(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def boolish(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def address_of(row: dict[str, Any]) -> str:
    return str(row.get("address") or row.get("mint") or row.get("token_address") or "").strip()


def first_nonempty(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def parse_event_timestamp(value: Any) -> dt.datetime | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(raw)
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def row_event_timestamp(row: dict[str, Any], *keys: str) -> dt.datetime | None:
    search_keys = keys or ("ts_utc", "timestamp", "created_at", "updated_at_utc", "first_seen_at", "opened_at", "closed_at")
    for key in search_keys:
        parsed = parse_event_timestamp(row.get(key))
        if parsed is not None:
            return parsed
    return None


def sort_event_rows(rows: Iterable[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    fallback = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda item: (row_event_timestamp(item[1], *keys) or fallback, item[0]))
    return [row for _index, row in indexed]


def position_key(row: dict[str, Any]) -> str:
    address = address_of(row)
    lane = str(row.get("entry_lane") or row.get("lane") or row.get("profit_lane_tier") or row.get("size_bucket") or "").strip().lower()
    stamp = first_nonempty(row, "entry_intent_id", "source_position_key", "id", "buy_tx_sig", "opened_at", "created_at", "closed_at", "run_id")
    identity = address or lane or "position"
    return f"{identity}:{stamp if stamp is not None else id(row)}"


def metrics_dir(root: Path | None = None) -> Path:
    return (root or PROJECT_ROOT) / "data" / "metrics"


def is_closed_trade(row: dict[str, Any]) -> bool:
    labels = {str(row.get(key) or "").strip().lower() for key in ("event_type", "event", "sample_type", "candidate_stage")}
    if labels & {"candidate_partial", "candidate_decision", "candidate_stage", "policy_reject"}:
        return False
    if row.get("closed") is not None:
        return boolish(row["closed"], False)
    if labels & {"candidate_outcome", "shadow_close", "trade_close", "close", "closed"}:
        return True
    # Legacy terminal records lack an event label but retain close evidence.
    return any(row.get(key) is not None for key in ("closed_at", "exit_reason", "total_pnl_pct", "realized_pnl_pct", "target_total_pnl_pct"))


def load_runtime_events(root: Path | None = None, *, include_test_events: bool | None = None) -> list[dict[str, Any]]:
    path = metrics_dir(root) / "runtime_events.jsonl"
    rows = _normalize_event_rows(read_jsonl(path), path=path, candidate_only=True)
    return filter_test_events(rows, include_test_events=include_test_events)


def load_candidate_outcomes(
    root: Path | None = None,
    *,
    include_test_events: bool | None = None,
    dedupe: bool = False,
) -> list[dict[str, Any]]:
    path = metrics_dir(root) / "candidate_outcomes.jsonl"
    rows = filter_test_events(
        _normalize_event_rows(read_jsonl(path), path=path),
        include_test_events=include_test_events,
    )
    return dedupe_candidate_rows(rows) if dedupe else rows


def load_paper_positions(root: Path | None = None) -> list[dict[str, Any]]:
    path = (root or PROJECT_ROOT) / "data" / "paper_portfolio.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    rows = payload.get("positions") if isinstance(payload, dict) else payload
    if rows is None and isinstance(payload, dict):
        rows = []
        for token_address, row in payload.items():
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item.setdefault("token_address", token_address)
            rows.append(item)
    if isinstance(rows, dict):
        rows = [
            {**row, "token_address": str(token_address)}
            for token_address, row in rows.items()
            if isinstance(row, dict)
        ]
    return [row for row in rows or [] if isinstance(row, dict)]


def load_sqlite_positions(root: Path | None = None) -> list[dict[str, Any]]:
    db_path = (root or PROJECT_ROOT) / "data" / "memebotdatabase.db"
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            if not _sqlite_object_exists(conn, "positions", "table"):
                return []
            return [_normalize_sqlite_position_row(dict(row)) for row in conn.execute("select * from positions")]
    except Exception:
        return []


def load_sqlite_tokens(root: Path | None = None) -> list[dict[str, Any]]:
    db_path = (root or PROJECT_ROOT) / "data" / "memebotdatabase.db"
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            if not _sqlite_object_exists(conn, "tokens", "table"):
                return []
            return [dict(row) for row in conn.execute("select * from tokens")]
    except Exception:
        return []


def _sqlite_object_exists(conn: sqlite3.Connection, name: str, object_type: str | None = None) -> bool:
    if object_type:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? AND type = ? LIMIT 1",
            (name, object_type),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1",
            (name,),
        ).fetchone()
    return row is not None


def _normalize_sqlite_position_row(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    full_reason = item.get("exit_reason_full")
    if full_reason is not None and not (isinstance(full_reason, str) and not full_reason.strip()):
        item["exit_reason"] = full_reason
    if not str(item.get("source_position_key") or "").strip():
        fallback = item.get("id")
        if fallback is not None and str(fallback).strip():
            item["source_position_key"] = str(fallback)
    return item


def load_sqlite_closed_trades(root: Path | None = None) -> list[dict[str, Any]]:
    db_path = (root or PROJECT_ROOT) / "data" / "memebotdatabase.db"
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            if _sqlite_object_exists(conn, "closed_trade_view", "view"):
                return [_normalize_sqlite_position_row(dict(row)) for row in conn.execute("select * from closed_trade_view")]
            if not _sqlite_object_exists(conn, "positions", "table"):
                return []
            return [
                row
                for row in (_normalize_sqlite_position_row(dict(raw)) for raw in conn.execute("select * from positions"))
                if boolish(row.get("closed"), False)
            ]
    except Exception:
        return []


def dedupe_position_rows(json_rows: list[dict[str, Any]], sqlite_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str | tuple[str, ...]] = set()
    known: dict[tuple[str, str], set[str]] = {}
    for row in json_rows + sqlite_rows:
        stamp = parse_event_timestamp(row.get("opened_at"))
        identity = entry_identity(row)
        if identity and stamp:
            known.setdefault((address_of(row), stamp.isoformat()), set()).add(identity)
    def trade_identity(row: dict[str, Any]) -> tuple[str, str, str]:
        stamp = parse_event_timestamp(row.get("opened_at"))
        base = (address_of(row), stamp.isoformat() if stamp else "")
        identity = entry_identity(row)
        matches = known.get(base, set())
        if identity is None and len(matches) > 1:
            raise PaperArchiveError("Legacy position cannot be assigned to multiple causal entries")
        return (*base, identity or next(iter(matches), ""))
    sqlite_addresses = {address_of(row) for row in sqlite_rows if address_of(row)}
    json_by_trade = {trade_identity(row): row for row in json_rows}
    sqlite_trade_ids = {trade_identity(row) for row in sqlite_rows}
    cost_fields = ("execution_cost_model", "estimated_fees_usd", "execution_fill_count",
                   "net_total_pnl_usd", "net_total_pnl_pct", "net_total_pnl_sol", "estimated_fees_sol",
                   "net_realized_pnl_usd", "config_profile", "runner_trailing_policy")
    source_order = (
        (("sqlite_positions", sqlite_rows), ("paper_portfolio", json_rows))
        if sqlite_rows
        else (("paper_portfolio", json_rows),)
    )
    for source, source_rows in source_order:
        for row in source_rows:
            item = dict(row)
            item["_source"] = source
            address = address_of(item)
            identity = trade_identity(item)
            if source == "sqlite_positions":
                companion = json_by_trade.get(identity)
                if companion and identity[1] and boolish(item.get("closed"), False) == boolish(companion.get("closed"), False):
                    for field in cost_fields:
                        if field in companion:
                            item[field] = companion[field]
            if source == "paper_portfolio" and address and address in sqlite_addresses:
                if identity in sqlite_trade_ids or not identity[1] or (address, "", "") in sqlite_trade_ids:
                    continue
            # A legacy alias and its one proven UUID refer to the same trade.
            # Unmatched legacy SQL ids retain their original distinct grain.
            key = ("causal_trade", *identity) if identity[2] else position_key(item)
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
    return rows


def load_deduped_positions(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or PROJECT_ROOT
    closed, issues = read_closed_evidence(root / "data")
    if issues:
        raise PaperArchiveError("Closed-trade evidence is unreadable; do not silently omit it")
    return dedupe_position_rows(load_paper_positions(root) + closed, load_sqlite_positions(root))


def bought_addresses(root: Path | None = None) -> set[str]:
    bought: set[str] = set()
    for row in load_runtime_events(root):
        event = str(row.get("event_type") or row.get("event") or row.get("action") or "").strip().lower()
        if event in {"buy", "bought", "buy_ok", "paper_buy"}:
            addr = address_of(row)
            if addr:
                bought.add(addr)
    for row in load_deduped_positions(root):
        addr = address_of(row)
        if addr:
            bought.add(addr)
    return bought


def rank_bucket(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return "rank_missing"
    score = fnum(value, -1.0)
    if score < 0:
        return "rank_missing"
    if score < 35:
        return "rank_<35"
    if score < 50:
        return "rank_35_50"
    if score < 61:
        return "rank_50_61"
    if score < 75:
        return "rank_61_75"
    return "rank_75+"


def price5m_bucket(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return "price5m_missing"
    score = fnum(value, 0.0)
    if score < 0:
        return "price5m_<0"
    if score < 25:
        return "price5m_0_25"
    if score < 50:
        return "price5m_25_50"
    if score < 100:
        return "price5m_50_100"
    if score < 180:
        return "price5m_100_180"
    if score < 300:
        return "price5m_180_300"
    return "price5m_300+"


def mcap_bucket(value: Any) -> str:
    mcap = fnum(value, 0.0)
    if mcap <= 0:
        return "mcap_missing"
    if mcap < 10_000:
        return "mcap_<10k"
    if mcap < 25_000:
        return "mcap_10k_25k"
    if mcap < 50_000:
        return "mcap_25k_50k"
    if mcap < 100_000:
        return "mcap_50k_100k"
    return "mcap_100k+"


SEVERE_EXITS = {"LIQUIDITY_CRUSH", "STOP_LOSS", "EARLY_DROP", "ADVERSE_TICK", "EARLY_DUMP_CUT"}


def is_severe_exit(row: dict[str, Any]) -> bool:
    reason = str(row.get("exit_reason") or row.get("reason") or "").upper()
    pnl_value = first_nonempty(row, "total_pnl_pct", "realized_pnl_pct", "pnl_pct")
    return reason in SEVERE_EXITS or fnum(pnl_value, 0.0) <= -25.0


__all__ = [
    "SEVERE_EXITS",
    "address_of",
    "boolish",
    "bought_addresses",
    "dedupe_position_rows",
    "dedupe_candidate_rows",
    "filter_test_events",
    "first_nonempty",
    "fnum",
    "inum",
    "include_test_events_enabled",
    "is_test_event",
    "is_severe_exit",
    "load_candidate_outcomes",
    "load_deduped_positions",
    "load_paper_positions",
    "load_runtime_events",
    "load_sqlite_closed_trades",
    "load_sqlite_positions",
    "load_sqlite_tokens",
    "mcap_bucket",
    "metrics_dir",
    "position_key",
    "price5m_bucket",
    "parse_event_timestamp",
    "rank_bucket",
    "read_jsonl",
    "row_event_timestamp",
    "set_include_test_events",
    "sort_event_rows",
    "write_json",
    "write_markdown",
]
