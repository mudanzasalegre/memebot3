from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from config.config import PROJECT_ROOT


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


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


def metrics_dir(root: Path | None = None) -> Path:
    return (root or PROJECT_ROOT) / "data" / "metrics"


def load_runtime_events(root: Path | None = None) -> list[dict[str, Any]]:
    return read_jsonl(metrics_dir(root) / "runtime_events.jsonl")


def load_candidate_outcomes(root: Path | None = None) -> list[dict[str, Any]]:
    return read_jsonl(metrics_dir(root) / "candidate_outcomes.jsonl")


def load_paper_positions(root: Path | None = None) -> list[dict[str, Any]]:
    path = (root or PROJECT_ROOT) / "data" / "paper_portfolio.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    rows = payload.get("positions") if isinstance(payload, dict) else payload
    if isinstance(rows, dict):
        rows = list(rows.values())
    return [row for row in rows or [] if isinstance(row, dict)]


def load_sqlite_positions(root: Path | None = None) -> list[dict[str, Any]]:
    db_path = (root or PROJECT_ROOT) / "data" / "memebotdatabase.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute("select * from positions")]
        conn.close()
        return rows
    except Exception:
        return []


def bought_addresses(root: Path | None = None) -> set[str]:
    bought: set[str] = set()
    for row in load_runtime_events(root):
        event = str(row.get("event_type") or row.get("event") or row.get("action") or "").strip().lower()
        if event in {"buy", "bought", "buy_ok", "paper_buy"}:
            addr = address_of(row)
            if addr:
                bought.add(addr)
    for row in load_paper_positions(root) + load_sqlite_positions(root):
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
    return reason in SEVERE_EXITS or fnum(row.get("realized_pnl_pct") or row.get("pnl_pct"), 0.0) <= -25.0


__all__ = [
    "SEVERE_EXITS",
    "address_of",
    "boolish",
    "bought_addresses",
    "fnum",
    "inum",
    "is_severe_exit",
    "load_candidate_outcomes",
    "load_paper_positions",
    "load_runtime_events",
    "load_sqlite_positions",
    "mcap_bucket",
    "metrics_dir",
    "price5m_bucket",
    "rank_bucket",
    "read_jsonl",
    "write_json",
    "write_markdown",
]
