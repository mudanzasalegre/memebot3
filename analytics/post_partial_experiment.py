from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from analytics.exit_policy import resolve_runner_exit_profile
from config.config import CFG, PROJECT_ROOT
from trade_pnl import summarize_trade
from utils.time import utc_now


METRICS_DIR = PROJECT_ROOT / "data" / "metrics"
PAPER_PORTFOLIO_PATH = PROJECT_ROOT / "data" / "paper_portfolio.json"
DB_PATH = (PROJECT_ROOT / str(getattr(CFG, "SQLITE_DB", "data/memebotdatabase.db"))).resolve()
STATE_PATH = METRICS_DIR / "post_partial_experiment.state.json"
SNAPSHOT_PATH = METRICS_DIR / "post_partial_experiment.json"

_LOCK = threading.Lock()
_EPSILON = 1e-9


def _normalize_regime(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"pump_early", "pumpfun", "pump", "pump_fun"}:
        return "pump_early"
    if raw in {"revival", "revive", "revived"}:
        return "revival"
    return "dex_mature"


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(payload), indent=2), encoding="utf-8")


def _load_portfolio(portfolio: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(portfolio, dict):
        return portfolio
    loaded = _read_json(PAPER_PORTFOLIO_PATH)
    if isinstance(loaded, dict):
        return loaded
    return {}


def _locked_threshold() -> float:
    recommended = _read_json(Path(CFG.AI_THRESHOLD_FILE))
    if isinstance(recommended, dict):
        picked = recommended.get("picked")
        try:
            if picked is not None:
                return float(picked)
        except Exception:
            pass
    return float(getattr(CFG, "POST_PARTIAL_EXPERIMENT_LOCKED_ML_THRESHOLD", 0.3972866423002348) or 0.3972866423002348)


def _candidate_config() -> dict[str, Any]:
    return {
        "enabled": bool(getattr(CFG, "POST_PARTIAL_EXPERIMENT_ENABLED", True)),
        "mode": str(getattr(CFG, "POST_PARTIAL_EXPERIMENT_MODE", "paper_shadow") or "paper_shadow").strip().lower(),
        "entry_regime": _normalize_regime(getattr(CFG, "POST_PARTIAL_EXPERIMENT_REGIME", "pump_early")),
        "lock_floor_pct": float(getattr(CFG, "POST_PARTIAL_EXPERIMENT_LOCK_FLOOR_PCT", 20.0) or 20.0),
        "max_giveback_after_partial_pct": float(
            getattr(CFG, "POST_PARTIAL_EXPERIMENT_MAX_GIVEBACK_PCT", 5.0) or 5.0
        ),
        "min_new_closes_for_gate": max(int(getattr(CFG, "POST_PARTIAL_EXPERIMENT_MIN_NEW_CLOSES", 50) or 50), 1),
        "ml_threshold_locked": float(_locked_threshold()),
    }


def _closed_rows_from_sqlite() -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(DB_PATH))
        connection.row_factory = sqlite3.Row
        cursor = connection.cursor()
        rows = cursor.execute(
            """
            SELECT
                id,
                address,
                entry_regime,
                entry_lane,
                gate_profile,
                partial_taken,
                partial_count,
                exit_reason,
                entry_qty,
                realized_qty,
                buy_price_usd,
                close_price_usd,
                entry_notional_usd,
                realized_proceeds_usd,
                total_pnl_pct,
                total_pnl_usd,
                highest_pnl_pct,
                max_pnl_pct_seen,
                runner_exit_profile,
                buy_market_cap_usd,
                buy_price_pct_5m,
                buy_txns_last_5m,
                buy_liquidity_is_proxy,
                size_bucket,
                closed_at
            FROM positions
            WHERE closed = 1
            ORDER BY closed_at ASC, id ASC
            """
        ).fetchall()
    except Exception:
        return []
    finally:
        try:
            if connection is not None:
                connection.close()
        except Exception:
            pass

    out: list[dict[str, Any]] = []
    for raw in rows:
        trade_id = raw["id"]
        address = str(raw["address"] or "").strip()
        if not address:
            continue
        closed_at_dt = _parse_timestamp(raw["closed_at"])
        if closed_at_dt is None:
            continue
        buy_price = _to_float(raw["buy_price_usd"])
        close_price = _to_float(raw["close_price_usd"])
        entry_qty = max(_to_int(raw["entry_qty"]), 0)
        realized_qty = max(_to_int(raw["realized_qty"]), 0)
        remaining_qty = max(0, entry_qty - realized_qty)
        actual_total_pnl_pct = _to_float(raw["total_pnl_pct"])
        actual_remaining_leg_pnl_pct = actual_total_pnl_pct
        if buy_price > 0.0 and close_price > 0.0:
            actual_remaining_leg_pnl_pct = ((close_price - buy_price) / buy_price) * 100.0
        out.append(
            {
                "trade_id": int(trade_id) if trade_id is not None else None,
                "row_key": f"db:{trade_id}" if trade_id is not None else f"db:{address}",
                "address": address,
                "closed_at": closed_at_dt,
                "entry_regime": _normalize_regime(raw["entry_regime"]),
                "entry_lane": str(raw["entry_lane"] or ""),
                "gate_profile": str(raw["gate_profile"] or ""),
                "size_bucket": str(raw["size_bucket"] or ""),
                "partial_taken": bool(raw["partial_taken"]),
                "partial_count": _to_int(raw["partial_count"]),
                "exit_reason": str(raw["exit_reason"] or ""),
                "entry_qty": entry_qty,
                "realized_qty": realized_qty,
                "remaining_qty": remaining_qty,
                "buy_price_usd": buy_price,
                "close_price_usd": close_price,
                "peak_price_usd": None,
                "entry_notional_usd": _to_float(raw["entry_notional_usd"]),
                "realized_proceeds_usd": _to_float(raw["realized_proceeds_usd"]),
                "actual_total_pnl_pct": actual_total_pnl_pct,
                "actual_total_pnl_usd": _to_float(raw["total_pnl_usd"]),
                "actual_remaining_leg_pnl_pct": actual_remaining_leg_pnl_pct,
                "peak_pnl_pct": max(_to_float(raw["highest_pnl_pct"]), _to_float(raw["max_pnl_pct_seen"])),
                "runner_exit_profile": str(raw["runner_exit_profile"] or ""),
                "buy_market_cap_usd": _to_float(raw["buy_market_cap_usd"]),
                "buy_price_pct_5m": _to_float(raw["buy_price_pct_5m"]),
                "buy_txns_last_5m": _to_float(raw["buy_txns_last_5m"]),
                "buy_liquidity_is_proxy": int(raw["buy_liquidity_is_proxy"] or 0),
            }
        )
    return out


def _closed_rows_from_paper(portfolio: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for address, raw in portfolio.items():
        if not isinstance(raw, dict):
            continue
        if not bool(raw.get("closed")):
            continue
        closed_at_dt = _parse_timestamp(raw.get("closed_at"))
        if closed_at_dt is None:
            continue
        token = str(raw.get("token_address") or address or "").strip()
        if not token:
            continue
        buy_price = _to_float(raw.get("buy_price_usd"))
        close_price = _to_float(raw.get("close_price_usd"))
        peak_price = _to_float(raw.get("peak_price"), buy_price)
        entry_qty = max(_to_int(raw.get("entry_qty")), _to_int(raw.get("qty_lamports")))
        realized_qty = max(0, _to_int(raw.get("realized_qty")))
        remaining_qty = max(0, entry_qty - realized_qty)
        actual_total_pnl_pct = _to_float(raw.get("total_pnl_pct"), _to_float(raw.get("pnl_pct")))
        actual_total_pnl_usd = _to_float(raw.get("total_pnl_usd"))
        actual_remaining_leg_pnl_pct = actual_total_pnl_pct
        if buy_price > 0.0 and close_price > 0.0:
            actual_remaining_leg_pnl_pct = ((close_price - buy_price) / buy_price) * 100.0
        peak_pnl_pct = 0.0
        if buy_price > 0.0 and peak_price > 0.0:
            peak_pnl_pct = ((peak_price - buy_price) / buy_price) * 100.0
        rows.append(
            {
                "trade_id": None,
                "row_key": f"paper:{token}",
                "address": token,
                "closed_at": closed_at_dt,
                "entry_regime": _normalize_regime(raw.get("entry_regime") or raw.get("discovered_via")),
                "entry_lane": str(raw.get("entry_lane") or ""),
                "gate_profile": str(raw.get("gate_profile") or raw.get("sniper_gate_profile") or ""),
                "size_bucket": str(raw.get("size_bucket") or ""),
                "partial_taken": bool(raw.get("partial_taken")),
                "partial_count": _to_int(raw.get("partial_count")),
                "exit_reason": str(raw.get("exit_reason") or ""),
                "entry_qty": entry_qty,
                "realized_qty": realized_qty,
                "remaining_qty": remaining_qty,
                "buy_price_usd": buy_price,
                "close_price_usd": close_price,
                "peak_price_usd": peak_price,
                "entry_notional_usd": _to_float(raw.get("entry_notional_usd")),
                "realized_proceeds_usd": _to_float(raw.get("realized_proceeds_usd")),
                "actual_total_pnl_pct": actual_total_pnl_pct,
                "actual_total_pnl_usd": actual_total_pnl_usd,
                "actual_remaining_leg_pnl_pct": actual_remaining_leg_pnl_pct,
                "peak_pnl_pct": max(peak_pnl_pct, _to_float(raw.get("max_pnl_pct_seen"))),
                "runner_exit_profile": str(raw.get("runner_exit_profile") or ""),
                "buy_market_cap_usd": _to_float(raw.get("buy_market_cap_usd") or raw.get("market_cap_usd")),
                "buy_price_pct_5m": _to_float(raw.get("buy_price_pct_5m") or raw.get("price_pct_5m")),
                "buy_txns_last_5m": _to_float(raw.get("buy_txns_last_5m") or raw.get("txns_last_5m")),
                "buy_liquidity_is_proxy": _to_int(raw.get("buy_liquidity_is_proxy") or raw.get("liquidity_is_proxy")),
            }
        )
    return rows


def _closed_rows(portfolio: dict[str, Any]) -> list[dict[str, Any]]:
    sqlite_rows = _closed_rows_from_sqlite()
    if not portfolio:
        sqlite_rows.sort(key=lambda row: (row["closed_at"], row["row_key"]))
        return sqlite_rows

    supplemental = _closed_rows_from_paper(portfolio)
    seen_addresses = {str(row["address"]) for row in sqlite_rows}
    merged = list(sqlite_rows)
    for row in supplemental:
        if str(row["address"]) in seen_addresses:
            continue
        merged.append(row)
    merged.sort(key=lambda row: (row["closed_at"], row["row_key"]))
    return merged


def _evaluate_candidate(row: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result.update(
        {
            "candidate_targeted": False,
            "candidate_triggered": False,
            "candidate_trigger_mode": "not_targeted",
            "candidate_threshold_remaining_leg_pct": None,
            "candidate_total_pnl_pct": float(row["actual_total_pnl_pct"]),
            "candidate_total_pnl_usd": float(row["actual_total_pnl_usd"]),
            "candidate_delta_pnl_usd": 0.0,
            "candidate_delta_pnl_pct_points": 0.0,
        }
    )

    if not bool(candidate.get("enabled")):
        result["candidate_trigger_mode"] = "disabled"
        return result

    if str(candidate.get("mode") or "").strip().lower() != "paper_shadow":
        result["candidate_trigger_mode"] = "mode_not_supported"
        return result

    if row["entry_regime"] != candidate["entry_regime"] or not bool(row["partial_taken"]):
        return result

    result["candidate_targeted"] = True
    buy_price = float(row["buy_price_usd"])
    entry_notional = float(row["entry_notional_usd"])
    remaining_qty = int(row["remaining_qty"])
    entry_qty = int(row["entry_qty"])
    realized_qty = int(row["realized_qty"])
    realized_proceeds = float(row["realized_proceeds_usd"])
    peak_pct = float(row["peak_pnl_pct"])
    actual_remaining_leg_pct = float(row["actual_remaining_leg_pnl_pct"])
    lock_floor_pct = float(candidate["lock_floor_pct"])
    giveback_pct = float(candidate["max_giveback_after_partial_pct"])

    if buy_price <= 0.0 or entry_notional <= 0.0 or entry_qty <= 0 or remaining_qty <= 0:
        result["candidate_trigger_mode"] = "insufficient_trade_shape"
        return result

    if peak_pct + _EPSILON < lock_floor_pct:
        result["candidate_trigger_mode"] = "peak_below_lock_floor"
        return result

    protected_remaining_leg_pct = max(lock_floor_pct, peak_pct - giveback_pct)
    result["candidate_threshold_remaining_leg_pct"] = protected_remaining_leg_pct

    if actual_remaining_leg_pct + _EPSILON >= protected_remaining_leg_pct:
        result["candidate_trigger_mode"] = "actual_beats_protection"
        return result

    candidate_close_price = buy_price * (1.0 + (protected_remaining_leg_pct / 100.0))
    totals = summarize_trade(
        entry_qty=entry_qty,
        remaining_qty=remaining_qty,
        buy_price_usd=buy_price,
        entry_notional_usd=entry_notional,
        realized_qty=realized_qty,
        realized_proceeds_usd=realized_proceeds,
        close_price_usd=candidate_close_price,
    )
    candidate_total_pnl_pct = float(totals.total_pnl_pct)
    candidate_total_pnl_usd = float(totals.total_pnl_usd)

    result.update(
        {
            "candidate_triggered": True,
            "candidate_trigger_mode": (
                "giveback_cap" if (peak_pct - giveback_pct) >= lock_floor_pct else "lock_floor"
            ),
            "candidate_total_pnl_pct": candidate_total_pnl_pct,
            "candidate_total_pnl_usd": candidate_total_pnl_usd,
            "candidate_delta_pnl_usd": candidate_total_pnl_usd - float(row["actual_total_pnl_usd"]),
            "candidate_delta_pnl_pct_points": candidate_total_pnl_pct - float(row["actual_total_pnl_pct"]),
        }
    )
    return result


def _runner_profile_policy(profile: str, peak_pct: float) -> tuple[str, str, float, float]:
    peak = max(0.0, float(peak_pct))
    if profile == "prime_runner":
        lock_floor = float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_LOCK_FLOOR_PCT", 25.0) or 25.0)
        max_giveback = float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_PRIME_BASE_MAX_GIVEBACK_PCT", 10.0) or 10.0)
        state = "base"
        if peak >= float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_PEAK_PCT", 80.0) or 80.0):
            lock_floor = float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_LOCK_FLOOR_PCT", 45.0) or 45.0)
            max_giveback = float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_PRIME_STEP_MAX_GIVEBACK_PCT", 15.0) or 15.0)
            state = "step"
        return profile, state, lock_floor, max_giveback

    if profile == "meteor_runner":
        lock_floor = float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_LOCK_FLOOR_PCT", 25.0) or 25.0)
        max_giveback = float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_BASE_MAX_GIVEBACK_PCT", 15.0) or 15.0)
        state = "base"
        if peak >= float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_PEAK_PCT", 100.0) or 100.0):
            lock_floor = float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_LOCK_FLOOR_PCT", 70.0) or 70.0)
            max_giveback = float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP1_MAX_GIVEBACK_PCT", 20.0) or 20.0)
            state = "step1"
        if peak >= float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP2_PEAK_PCT", 250.0) or 250.0):
            lock_floor = max(
                lock_floor,
                float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_METEOR_STEP2_LOCK_FLOOR_PCT", 120.0) or 120.0),
            )
            state = "step2"
        return profile, state, lock_floor, max_giveback

    return (
        "broad_runner",
        "base",
        float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_BROAD_LOCK_FLOOR_PCT", 20.0) or 20.0),
        float(getattr(CFG, "PUMP_EARLY_PROFIT_RUNNER_BROAD_MAX_GIVEBACK_PCT", 5.0) or 5.0),
    )


def _evaluate_runner_candidate(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        "row_key": row["row_key"],
        "address": row["address"],
        "trade_id": row.get("trade_id"),
        "closed_at": row["closed_at"],
        "entry_regime": row["entry_regime"],
        "entry_lane": row.get("entry_lane"),
        "gate_profile": row.get("gate_profile"),
        "partial_taken": bool(row.get("partial_taken")),
        "actual_total_pnl_pct": float(row["actual_total_pnl_pct"]),
        "actual_total_pnl_usd": float(row["actual_total_pnl_usd"]),
        "runner_candidate_targeted": False,
        "runner_candidate_triggered": False,
        "runner_candidate_total_pnl_pct": float(row["actual_total_pnl_pct"]),
        "runner_candidate_total_pnl_usd": float(row["actual_total_pnl_usd"]),
        "runner_candidate_delta_pnl_usd": 0.0,
        "runner_candidate_delta_pnl_pct_points": 0.0,
        "runner_profile": None,
        "runner_profile_state": None,
        "runner_trigger_mode": "not_targeted",
    }

    profile = resolve_runner_exit_profile(row)
    if row["entry_regime"] != "pump_early" or not bool(row.get("partial_taken")) or profile is None:
        return result

    result["runner_candidate_targeted"] = True
    result["runner_profile"] = profile

    buy_price = float(row["buy_price_usd"])
    entry_notional = float(row["entry_notional_usd"])
    remaining_qty = int(row["remaining_qty"])
    entry_qty = int(row["entry_qty"])
    realized_qty = int(row["realized_qty"])
    realized_proceeds = float(row["realized_proceeds_usd"])
    peak_pct = float(row["peak_pnl_pct"])
    actual_remaining_leg_pct = float(row["actual_remaining_leg_pnl_pct"])
    profile_name, profile_state, lock_floor_pct, giveback_pct = _runner_profile_policy(profile, peak_pct)
    result["runner_profile"] = profile_name
    result["runner_profile_state"] = profile_state

    if buy_price <= 0.0 or entry_notional <= 0.0 or entry_qty <= 0 or remaining_qty <= 0:
        result["runner_trigger_mode"] = "insufficient_trade_shape"
        return result
    if peak_pct + _EPSILON < lock_floor_pct:
        result["runner_trigger_mode"] = "peak_below_lock_floor"
        return result

    protected_remaining_leg_pct = max(lock_floor_pct, peak_pct - giveback_pct)
    if actual_remaining_leg_pct + _EPSILON >= protected_remaining_leg_pct:
        result["runner_trigger_mode"] = "actual_beats_runner"
        return result

    candidate_close_price = buy_price * (1.0 + (protected_remaining_leg_pct / 100.0))
    totals = summarize_trade(
        entry_qty=entry_qty,
        remaining_qty=remaining_qty,
        buy_price_usd=buy_price,
        entry_notional_usd=entry_notional,
        realized_qty=realized_qty,
        realized_proceeds_usd=realized_proceeds,
        close_price_usd=candidate_close_price,
    )
    candidate_total_pnl_pct = float(totals.total_pnl_pct)
    candidate_total_pnl_usd = float(totals.total_pnl_usd)
    result.update(
        {
            "runner_candidate_triggered": True,
            "runner_trigger_mode": (
                "giveback_cap" if (peak_pct - giveback_pct) >= lock_floor_pct else "lock_floor"
            ),
            "runner_candidate_total_pnl_pct": candidate_total_pnl_pct,
            "runner_candidate_total_pnl_usd": candidate_total_pnl_usd,
            "runner_candidate_delta_pnl_usd": candidate_total_pnl_usd - float(row["actual_total_pnl_usd"]),
            "runner_candidate_delta_pnl_pct_points": candidate_total_pnl_pct - float(row["actual_total_pnl_pct"]),
        }
    )
    return result


def _simple_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    equity = 0.0
    peak = 0.0
    min_drawdown = 0.0
    for value in values:
        equity += float(value)
        peak = max(peak, equity)
        min_drawdown = min(min_drawdown, equity - peak)
    return float(min_drawdown)


def _summarize_rows(rows: list[dict[str, Any]], *, prefix: str) -> dict[str, Any]:
    if not rows:
        return {
            "count": 0,
            "targeted_count": 0,
            "triggered_count": 0,
            "improved_count": 0,
            "win_rate_pct": None,
            "avg_pnl_pct": None,
            "median_pnl_pct": None,
            "total_pnl_usd": 0.0,
            "simple_max_drawdown_usd": None,
            "simple_max_drawdown_pct_points": None,
        }

    pnl_pct = [float(row[f"{prefix}_total_pnl_pct"]) for row in rows]
    pnl_usd = [float(row[f"{prefix}_total_pnl_usd"]) for row in rows]
    targeted_count = sum(1 for row in rows if bool(row.get("candidate_targeted")))
    triggered_count = sum(1 for row in rows if bool(row.get("candidate_triggered")))
    improved_count = sum(1 for row in rows if float(row.get("candidate_delta_pnl_usd") or 0.0) > 0.0)
    return {
        "count": len(rows),
        "targeted_count": targeted_count,
        "triggered_count": triggered_count,
        "improved_count": improved_count,
        "win_rate_pct": (sum(1 for value in pnl_pct if value > 0.0) / len(pnl_pct)) * 100.0,
        "avg_pnl_pct": sum(pnl_pct) / len(pnl_pct),
        "median_pnl_pct": float(median(pnl_pct)),
        "total_pnl_usd": sum(pnl_usd),
        "simple_max_drawdown_usd": _simple_drawdown(pnl_usd),
        "simple_max_drawdown_pct_points": _simple_drawdown(pnl_pct),
    }


def _metrics_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    actual = _summarize_rows(rows, prefix="actual")
    candidate = _summarize_rows(rows, prefix="candidate")
    delta_total_pnl_usd = float(candidate["total_pnl_usd"] or 0.0) - float(actual["total_pnl_usd"] or 0.0)
    delta_avg_pnl_pct = None
    if actual["avg_pnl_pct"] is not None and candidate["avg_pnl_pct"] is not None:
        delta_avg_pnl_pct = float(candidate["avg_pnl_pct"]) - float(actual["avg_pnl_pct"])
    delta_drawdown_usd = None
    if actual["simple_max_drawdown_usd"] is not None and candidate["simple_max_drawdown_usd"] is not None:
        delta_drawdown_usd = float(candidate["simple_max_drawdown_usd"]) - float(actual["simple_max_drawdown_usd"])
    return {
        "actual": actual,
        "candidate": candidate,
        "delta_total_pnl_usd": delta_total_pnl_usd,
        "delta_avg_pnl_pct": delta_avg_pnl_pct,
        "drawdown_guardrail_passed": bool(
            actual["simple_max_drawdown_usd"] is not None
            and candidate["simple_max_drawdown_usd"] is not None
            and float(candidate["simple_max_drawdown_usd"]) >= float(actual["simple_max_drawdown_usd"])
        ),
        "delta_drawdown_usd": delta_drawdown_usd,
    }


def _runner_metrics_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "count": 0,
            "targeted_count": 0,
            "triggered_count": 0,
            "delta_total_pnl_usd": 0.0,
            "delta_avg_pnl_pct": None,
            "profiles": {},
        }

    actual_rows = [
        {
            "actual_total_pnl_pct": float(row["actual_total_pnl_pct"]),
            "actual_total_pnl_usd": float(row["actual_total_pnl_usd"]),
            "candidate_targeted": bool(row.get("runner_candidate_targeted")),
            "candidate_triggered": bool(row.get("runner_candidate_triggered")),
            "candidate_total_pnl_pct": float(row["runner_candidate_total_pnl_pct"]),
            "candidate_total_pnl_usd": float(row["runner_candidate_total_pnl_usd"]),
            "candidate_delta_pnl_usd": float(row["runner_candidate_delta_pnl_usd"]),
        }
        for row in rows
    ]
    payload = _metrics_block(actual_rows)
    payload["profiles"] = {}
    for profile in ("broad_runner", "prime_runner", "meteor_runner"):
        profile_rows = [row for row in rows if str(row.get("runner_profile") or "") == profile]
        if not profile_rows:
            continue
        payload["profiles"][profile] = _metrics_block(
            [
                {
                    "actual_total_pnl_pct": float(row["actual_total_pnl_pct"]),
                    "actual_total_pnl_usd": float(row["actual_total_pnl_usd"]),
                    "candidate_targeted": bool(row.get("runner_candidate_targeted")),
                    "candidate_triggered": bool(row.get("runner_candidate_triggered")),
                    "candidate_total_pnl_pct": float(row["runner_candidate_total_pnl_pct"]),
                    "candidate_total_pnl_usd": float(row["runner_candidate_total_pnl_usd"]),
                    "candidate_delta_pnl_usd": float(row["runner_candidate_delta_pnl_usd"]),
                }
                for row in profile_rows
            ]
        )
    return payload


def _build_state(closed_rows: list[dict[str, Any]], *, force_reset: bool = False) -> dict[str, Any]:
    state = None if force_reset else _read_json(STATE_PATH)
    if isinstance(state, dict):
        return state

    latest_closed = closed_rows[-1]["closed_at"] if closed_rows else None
    state = {
        "version": 1,
        "started_at_utc": utc_now().isoformat(),
        "baseline_closed_count": len(closed_rows),
        "baseline_latest_closed_at": latest_closed.isoformat() if latest_closed is not None else None,
        "baseline_row_keys": [row["row_key"] for row in closed_rows],
        "candidate": _candidate_config(),
    }
    _write_json(STATE_PATH, state)
    return state


def _recent_deltas(rows: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    recent = sorted(rows, key=lambda row: (row["closed_at"], row["address"]), reverse=True)[:limit]
    return [
        {
            "trade_id": row.get("trade_id"),
            "address": row["address"],
            "closed_at": row["closed_at"].isoformat(),
            "entry_regime": row["entry_regime"],
            "partial_taken": bool(row["partial_taken"]),
            "exit_reason": row["exit_reason"],
            "candidate_targeted": bool(row["candidate_targeted"]),
            "candidate_triggered": bool(row["candidate_triggered"]),
            "candidate_trigger_mode": row["candidate_trigger_mode"],
            "actual_total_pnl_pct": float(row["actual_total_pnl_pct"]),
            "candidate_total_pnl_pct": float(row["candidate_total_pnl_pct"]),
            "actual_total_pnl_usd": float(row["actual_total_pnl_usd"]),
            "candidate_total_pnl_usd": float(row["candidate_total_pnl_usd"]),
            "candidate_delta_pnl_usd": float(row["candidate_delta_pnl_usd"]),
        }
        for row in recent
    ]


def build_snapshot(portfolio: dict[str, Any] | None = None, *, force_reset: bool = False) -> dict[str, Any]:
    loaded_portfolio = _load_portfolio(portfolio)
    closed_rows = _closed_rows(loaded_portfolio)
    state = _build_state(closed_rows, force_reset=force_reset)
    baseline_row_keys = set(str(item) for item in (state.get("baseline_row_keys") or state.get("baseline_addresses") or []))
    candidate = dict(state.get("candidate") or _candidate_config())
    evaluated_rows = [_evaluate_candidate(row, candidate) for row in closed_rows]
    forward_rows = [row for row in evaluated_rows if str(row["row_key"]) not in baseline_row_keys]
    gate_target = max(int(candidate.get("min_new_closes_for_gate") or 50), 1)
    forward_metrics = _metrics_block(forward_rows)
    historical_metrics = _metrics_block(evaluated_rows)
    runner_rows = [_evaluate_runner_candidate(row) for row in closed_rows]
    runner_recent_rows = sorted(runner_rows, key=lambda row: (row["closed_at"], row["address"]), reverse=True)[:12]
    new_close_count = len(forward_rows)

    return {
        "generated_at_utc": utc_now().isoformat(),
        "mode": candidate.get("mode"),
        "notes": {
            "experiment_scope": "paper_shadow_only",
            "live_execution_changed": False,
            "paper_execution_changed": False,
            "threshold_locked_for_experiment": float(candidate.get("ml_threshold_locked") or 0.0),
        },
        "candidate": candidate,
        "start_snapshot": {
            "started_at_utc": state.get("started_at_utc"),
            "baseline_closed_count": int(state.get("baseline_closed_count") or 0),
            "baseline_latest_closed_at": state.get("baseline_latest_closed_at"),
        },
        "historical_context": historical_metrics,
        "forward_window": {
            "new_closed_trades": new_close_count,
            "remaining_new_closes_for_gate": max(gate_target - new_close_count, 0),
            "gate_target_new_closes": gate_target,
            "gate_reached": new_close_count >= gate_target,
            "ready_for_review": bool(
                new_close_count >= gate_target
                and forward_metrics["delta_total_pnl_usd"] > 0.0
                and forward_metrics["drawdown_guardrail_passed"]
            ),
            **forward_metrics,
        },
        "runner_profile_replay": {
            "historical": _runner_metrics_block(runner_rows),
            "recent_window": _runner_metrics_block(runner_recent_rows),
            "recent_deltas": [
                {
                    "trade_id": row.get("trade_id"),
                    "address": row["address"],
                    "closed_at": row["closed_at"].isoformat(),
                    "runner_profile": row.get("runner_profile"),
                    "runner_profile_state": row.get("runner_profile_state"),
                    "runner_candidate_targeted": bool(row.get("runner_candidate_targeted")),
                    "runner_candidate_triggered": bool(row.get("runner_candidate_triggered")),
                    "runner_trigger_mode": row.get("runner_trigger_mode"),
                    "actual_total_pnl_pct": float(row["actual_total_pnl_pct"]),
                    "runner_candidate_total_pnl_pct": float(row["runner_candidate_total_pnl_pct"]),
                    "actual_total_pnl_usd": float(row["actual_total_pnl_usd"]),
                    "runner_candidate_total_pnl_usd": float(row["runner_candidate_total_pnl_usd"]),
                    "runner_candidate_delta_pnl_usd": float(row["runner_candidate_delta_pnl_usd"]),
                }
                for row in runner_recent_rows
            ],
        },
        "latest_closed_trade": _recent_deltas(evaluated_rows, limit=1)[0] if evaluated_rows else None,
        "recent_forward_deltas": _recent_deltas(forward_rows, limit=12),
        "recent_historical_deltas": _recent_deltas(evaluated_rows, limit=12),
        "paths": {
            "state": str(STATE_PATH),
            "snapshot": str(SNAPSHOT_PATH),
            "sqlite_db": str(DB_PATH),
            "paper_portfolio": str(PAPER_PORTFOLIO_PATH),
            "recommended_threshold": str(Path(CFG.AI_THRESHOLD_FILE)),
        },
    }


def refresh_snapshot(portfolio: dict[str, Any] | None = None, *, force_reset: bool = False) -> dict[str, Any] | None:
    if not bool(getattr(CFG, "POST_PARTIAL_EXPERIMENT_ENABLED", True)):
        return None
    with _LOCK:
        snapshot = build_snapshot(portfolio=portfolio, force_reset=force_reset)
        _write_json(SNAPSHOT_PATH, snapshot)
        return snapshot


__all__ = ["SNAPSHOT_PATH", "STATE_PATH", "build_snapshot", "refresh_snapshot"]
