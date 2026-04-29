from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.config import CFG


def _db_path() -> Path:
    raw = str(getattr(CFG, "SQLITE_DB", "data/memebotdatabase.db") or "data/memebotdatabase.db")
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _profit_lane_tier(row: dict[str, Any]) -> str | None:
    bucket = str(row.get("size_bucket") or "").strip()
    if bucket == "pumpswap_meteor":
        return "pump_early_meteor_prime"
    if bucket == "pumpswap_prime":
        return "pump_early_pumpswap_prime"
    if str(row.get("entry_lane") or "").strip() == "pump_early_pumpswap_profit":
        return "pump_early_pumpswap_profit"
    return None


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    return str(value).strip() == ""


def _load_position_metadata(db_path: Path) -> dict[str, dict[str, Any]]:
    if not db_path.exists():
        return {}
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT
                address,
                entry_lane,
                gate_profile,
                size_bucket,
                buy_dex_id,
                buy_liquidity_is_proxy,
                mcap_bucket,
                price5m_bucket
            FROM positions
            WHERE closed = 1
            """
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        row["profit_lane_tier"] = _profit_lane_tier(row)
        out[str(row["address"])] = row
    return out


def backfill() -> dict[str, int]:
    db_path = _db_path()
    metadata = _load_position_metadata(db_path)
    if not metadata:
        return {"files": 0, "rows_updated": 0}

    feature_dir = Path(CFG.FEATURES_DIR)
    files = sorted([*feature_dir.glob("features_*.parquet"), *feature_dir.glob("features_*.csv")])
    rows_updated = 0
    files_updated = 0

    for path in files:
        frame = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
        if frame.empty or "sample_type" not in frame.columns or "address" not in frame.columns:
            continue
        for col in (
            "entry_lane",
            "gate_profile",
            "profit_lane_tier",
            "dex_id",
            "liquidity_is_proxy",
            "mcap_bucket",
            "price5m_bucket",
            "venue_is_pumpswap",
        ):
            if col not in frame.columns:
                frame[col] = pd.NA
        for col in ("entry_lane", "gate_profile", "profit_lane_tier", "dex_id", "mcap_bucket", "price5m_bucket"):
            frame[col] = frame[col].astype("object")
        changed = False
        trade_mask = frame["sample_type"].astype("string").eq("trade_close")
        for idx in frame.index[trade_mask]:
            address = str(frame.at[idx, "address"] or "")
            row = metadata.get(address)
            if not row:
                continue
            row_changed = False
            if _missing(frame.at[idx, "entry_lane"]):
                frame.at[idx, "entry_lane"] = row.get("entry_lane")
                row_changed = True
            if _missing(frame.at[idx, "gate_profile"]):
                frame.at[idx, "gate_profile"] = row.get("gate_profile")
                row_changed = True
            if _missing(frame.at[idx, "profit_lane_tier"]):
                frame.at[idx, "profit_lane_tier"] = row.get("profit_lane_tier")
                row_changed = True
            if _missing(frame.at[idx, "dex_id"]):
                frame.at[idx, "dex_id"] = row.get("buy_dex_id")
                row_changed = True
            if pd.isna(frame.at[idx, "liquidity_is_proxy"]):
                frame.at[idx, "liquidity_is_proxy"] = int(bool(row.get("buy_liquidity_is_proxy")))
                row_changed = True
            if _missing(frame.at[idx, "mcap_bucket"]):
                frame.at[idx, "mcap_bucket"] = row.get("mcap_bucket")
                row_changed = True
            if _missing(frame.at[idx, "price5m_bucket"]):
                frame.at[idx, "price5m_bucket"] = row.get("price5m_bucket")
                row_changed = True
            if row.get("buy_dex_id") and str(row.get("buy_dex_id")).lower() == "pumpswap":
                current_venue = pd.to_numeric(pd.Series([frame.at[idx, "venue_is_pumpswap"]]), errors="coerce").iloc[0]
                if pd.isna(current_venue) or int(current_venue) != 1:
                    frame.at[idx, "venue_is_pumpswap"] = 1
                    row_changed = True
            if row_changed:
                changed = True
                rows_updated += 1
        if changed:
            if path.suffix == ".parquet":
                frame.to_parquet(path, index=False)
            else:
                frame.to_csv(path, index=False)
            files_updated += 1

    return {"files": files_updated, "rows_updated": rows_updated}


if __name__ == "__main__":
    result = backfill()
    print(f"feature_entry_metadata_backfill={result}")
