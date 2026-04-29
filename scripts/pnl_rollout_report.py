from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.config import CFG, DB_URI, PROJECT_ROOT  # noqa: E402


CUTOFF_UTC = dt.datetime(2026, 4, 15, 4, 2, 52, tzinfo=dt.timezone.utc)
CUTOFF_LABEL = "15 Apr 2026 06:02:52 CEST"
RESEARCH_EVENTS_PATH = PROJECT_ROOT / "data" / "metrics" / "candidate_outcomes.jsonl"


def _db_path_from_uri(db_uri: str) -> Path:
    if db_uri.startswith("sqlite+aiosqlite:///"):
        return Path(db_uri.replace("sqlite+aiosqlite:///", "", 1))
    return Path(db_uri)


def _normalize_regime(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"pump_early", "pumpfun", "pump", "pump_fun"}:
        return "pump_early"
    if raw in {"revival", "revive", "revived"}:
        return "revival"
    return "dex_mature"


def _parse_utc(raw: Any) -> dt.datetime | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _load_closed_positions(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                entry_regime,
                total_pnl_pct,
                closed_at,
                exit_reason,
                size_bucket
            FROM positions
            WHERE closed = 1
              AND total_pnl_pct IS NOT NULL
            ORDER BY closed_at ASC
            """
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "entry_regime": _normalize_regime(row["entry_regime"]),
                "total_pnl_pct": float(row["total_pnl_pct"]),
                "closed_at": _parse_utc(row["closed_at"]),
                "exit_reason": str(row["exit_reason"] or ""),
                "size_bucket": str(row["size_bucket"] or ""),
            }
        )
    return out


def _loss_streak(values: list[float]) -> int:
    best = 0
    current = 0
    for value in values:
        if value > 0:
            current = 0
            continue
        current += 1
        best = max(best, current)
    return best


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(row["total_pnl_pct"]) for row in rows]
    liq_crush = sum(1 for row in rows if str(row.get("exit_reason") or "").upper() == "LIQUIDITY_CRUSH")
    return {
        "closed_trades": len(rows),
        "avg_pnl_pct": (sum(pnls) / len(pnls)) if pnls else None,
        "median_pnl_pct": statistics.median(pnls) if pnls else None,
        "win_rate_pct": (sum(1 for value in pnls if value > 0) / len(pnls) * 100.0) if pnls else None,
        "liq_crush_rate_pct": (liq_crush / len(rows) * 100.0) if rows else None,
        "liq_crush_count": liq_crush,
        "max_loss_streak": _loss_streak(pnls),
    }


def _candidate_rows() -> list[dict[str, Any]]:
    rows = _load_jsonl(RESEARCH_EVENTS_PATH)
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if _normalize_regime(row.get("regime") or row.get("entry_regime")) != "pump_early":
            continue
        address = str(row.get("address") or "").strip()
        if not address:
            continue
        if str(row.get("event_type") or "") not in {"candidate_stage", "candidate_decision"}:
            continue
        if str(row.get("stage") or "") not in {"late_funnel", "strategy", "entry_quality", "soft_score"}:
            continue
        latest[address] = row
    return list(latest.values())


def _passes_current_gate(row: dict[str, Any]) -> bool:
    return (
        bool(_to_int(row.get("has_jupiter_route")))
        and _to_float(row.get("age_minutes")) >= 8.0
        and _to_float(row.get("liquidity_usd")) >= 10_000.0
        and _to_float(row.get("market_cap_usd")) >= 20_000.0
        and _to_float(row.get("market_cap_usd")) <= 125_000.0
        and _to_int(row.get("score_total")) >= 50
        and _to_float(row.get("price_impact_pct")) <= 10.0
        and _to_int(row.get("snapshot_missing_fields")) <= 3
    )


def _passes_sniper_core(row: dict[str, Any]) -> bool:
    return (
        bool(_to_int(row.get("has_jupiter_route")))
        and float(CFG.PUMP_EARLY_SNIPER_MIN_AGE_MIN)
        <= _to_float(row.get("age_minutes"))
        <= float(CFG.PUMP_EARLY_SNIPER_MAX_AGE_MIN)
        and _to_float(row.get("liquidity_usd")) >= float(CFG.PUMP_EARLY_SNIPER_MIN_LIQUIDITY_USD)
        and float(CFG.PUMP_EARLY_SNIPER_MIN_MARKET_CAP_USD)
        <= _to_float(row.get("market_cap_usd"))
        <= float(CFG.PUMP_EARLY_SNIPER_MAX_MARKET_CAP_USD)
        and _to_int(row.get("score_total")) >= int(CFG.PUMP_EARLY_SNIPER_MIN_SCORE_TOTAL)
        and _to_float(row.get("rank_score")) >= float(CFG.PUMP_EARLY_SNIPER_MIN_RANK_SCORE)
        and _to_int(row.get("txns_last_5m")) >= int(CFG.PUMP_EARLY_SNIPER_MIN_TXNS_5M)
        and float(CFG.PUMP_EARLY_SNIPER_MIN_PRICE_PCT_5M)
        <= _to_float(row.get("price_pct_5m"), -999.0)
        <= float(CFG.PUMP_EARLY_SNIPER_MAX_PRICE_PCT_5M)
        and _to_float(row.get("price_impact_pct")) <= float(CFG.PUMP_EARLY_SNIPER_MAX_PRICE_IMPACT_PCT)
        and _to_int(row.get("snapshot_missing_fields")) <= int(CFG.PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS)
    )


def _passes_sniper_micro(row: dict[str, Any]) -> bool:
    return (
        bool(_to_int(row.get("has_jupiter_route")))
        and float(CFG.PUMP_EARLY_SNIPER_MIN_AGE_MIN)
        <= _to_float(row.get("age_minutes"))
        <= float(CFG.PUMP_EARLY_SNIPER_MAX_AGE_MIN)
        and _to_float(row.get("liquidity_usd")) >= float(CFG.PUMP_EARLY_SNIPER_MICRO_MIN_LIQUIDITY_USD)
        and _to_float(row.get("volume_24h_usd")) >= float(CFG.PUMP_EARLY_SNIPER_MICRO_MIN_VOLUME_USD_24H)
        and _to_float(row.get("market_cap_usd")) <= float(CFG.PUMP_EARLY_SNIPER_MICRO_MAX_MARKET_CAP_USD)
        and _to_int(row.get("score_total")) >= int(CFG.PUMP_EARLY_SNIPER_MICRO_MIN_SCORE_TOTAL)
        and _to_float(row.get("rank_score")) >= float(CFG.PUMP_EARLY_SNIPER_MICRO_MIN_RANK_SCORE)
        and _to_int(row.get("txns_last_5m")) >= int(CFG.PUMP_EARLY_SNIPER_MICRO_MIN_TXNS_5M)
        and _to_float(row.get("price_pct_5m"), -999.0) >= float(CFG.PUMP_EARLY_SNIPER_MICRO_MIN_PRICE_PCT_5M)
        and _to_float(row.get("price_impact_pct")) <= float(CFG.PUMP_EARLY_SNIPER_MICRO_MAX_PRICE_IMPACT_PCT)
        and _to_int(row.get("snapshot_missing_fields")) <= int(CFG.PUMP_EARLY_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS)
    )


def _is_proxy_liquidity(row: dict[str, Any]) -> bool:
    try:
        return int(row.get("liquidity_is_proxy") or row.get("liquidity_usd_is_proxy") or 0) != 0
    except Exception:
        raw = str(row.get("liquidity_is_proxy") or row.get("liquidity_usd_is_proxy") or "").strip().lower()
        return raw in {"true", "yes", "on"}


def _norm_dex(row: dict[str, Any]) -> str:
    return str(row.get("dex_id") or row.get("dexId") or "").strip().lower().replace("_", "").replace("-", "")


def _price5m_blocked(value: float) -> bool:
    raw = str(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES", "0:25,50:100") or "0:25,50:100")
    for item in raw.split(","):
        if ":" not in item:
            continue
        try:
            lo_raw, hi_raw = item.split(":", 1)
            lo = float(lo_raw)
            hi = float(hi_raw)
        except Exception:
            continue
        if min(lo, hi) <= value <= max(lo, hi):
            return True
    return False


def _passes_profit_shape_guard(row: dict[str, Any]) -> bool:
    if not bool(getattr(CFG, "PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED", True)):
        return True
    price5m_raw = row.get("price_pct_5m")
    price5m = None if price5m_raw is None or price5m_raw == "" else _to_float(price5m_raw)
    txns5m = _to_int(row.get("txns_last_5m"))
    liquidity = _to_float(row.get("liquidity_usd"))
    mcap = _to_float(row.get("market_cap_usd"))
    volume24h = max(_to_float(row.get("volume_24h_usd")), _to_float(row.get("volume_usd_24h")))

    if mcap >= float(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD", 500_000.0) or 500_000.0):
        return False
    if (
        price5m is not None
        and price5m >= float(getattr(CFG, "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_PCT", 300.0) or 300.0)
        and mcap >= float(getattr(CFG, "PUMP_EARLY_PROFIT_EXTREME_PRICE5M_MIN_MCAP_USD", 100_000.0) or 100_000.0)
    ):
        return False
    if (
        price5m is not None
        and price5m <= float(getattr(CFG, "PUMP_EARLY_PROFIT_DEEP_NEG_PRICE5M_PCT", -40.0))
        and txns5m < int(getattr(CFG, "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_TXNS_5M", 1_500) or 1_500)
        and volume24h < float(getattr(CFG, "PUMP_EARLY_PROFIT_DEEP_NEG_MIN_VOLUME_USD_24H", 150_000.0) or 150_000.0)
    ):
        return False
    if (
        float(getattr(CFG, "PUMP_EARLY_PROFIT_DEAD_VOLUME_MIN_USD_24H", 15_000.0) or 15_000.0)
        <= volume24h
        < float(getattr(CFG, "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_USD_24H", 30_000.0) or 30_000.0)
        and txns5m < int(getattr(CFG, "PUMP_EARLY_PROFIT_DEAD_VOLUME_MAX_TXNS_5M", 1_000) or 1_000)
    ):
        return False
    if (
        price5m is not None
        and float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_PRICE5M_MIN_PCT", 100.0) or 100.0)
        <= price5m
        <= float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_PRICE5M_MAX_PCT", 180.0) or 180.0)
        and mcap >= float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MCAP_MIN_USD", 50_000.0) or 50_000.0)
        and (
            liquidity < float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MIN_LIQUIDITY_USD", 20_000.0) or 20_000.0)
            or txns5m < int(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MIN_TXNS_5M", 600) or 600)
            or volume24h < float(getattr(CFG, "PUMP_EARLY_PROFIT_HOT_MIN_VOLUME_USD_24H", 50_000.0) or 50_000.0)
        )
    ):
        return False
    if (
        price5m is not None
        and volume24h < float(
            getattr(CFG, "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_VOLUME_USD_24H", 15_000.0) or 15_000.0
        )
        and txns5m < int(getattr(CFG, "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_TXNS_5M", 500) or 500)
        and price5m < float(
            getattr(CFG, "PUMP_EARLY_PROFIT_LOW_VOLUME_NO_MOMENTUM_MAX_PRICE5M_PCT", 50.0) or 50.0
        )
    ):
        return False
    if (
        price5m is not None
        and mcap < 25_000.0
        and 25.0 <= price5m < 50.0
        and txns5m < int(getattr(CFG, "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_TXNS_5M", 350) or 350)
        and volume24h < float(
            getattr(CFG, "PUMP_EARLY_PROFIT_PRIME_MID_MOMENTUM_MIN_VOLUME_USD_24H", 100_000.0) or 100_000.0
        )
    ):
        return False
    if (
        price5m is not None
        and mcap >= float(getattr(CFG, "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD", 100_000.0) or 100_000.0)
        and float(getattr(CFG, "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT", 40.0) or 40.0)
        <= price5m
        < float(getattr(CFG, "PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT", 50.0) or 50.0)
    ):
        return False
    return True


def _passes_pumpswap_profit_raw(row: dict[str, Any]) -> bool:
    mcap = _to_float(row.get("market_cap_usd"))
    price5m = _to_float(row.get("price_pct_5m"), -9999.0)
    return (
        bool(_to_int(row.get("has_jupiter_route")))
        and _norm_dex(row) == "pumpswap"
        and not _is_proxy_liquidity(row)
        and _to_float(row.get("liquidity_usd")) >= float(getattr(CFG, "PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD", 5_000.0))
        and _to_int(row.get("score_total")) >= int(getattr(CFG, "PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL", 35))
        and float(getattr(CFG, "PUMP_EARLY_PROFIT_MIN_AGE_MIN", 3.0))
        <= _to_float(row.get("age_minutes"))
        <= float(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_AGE_MIN", 30.0))
        and _to_float(row.get("price_impact_pct")) <= float(getattr(CFG, "PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT", 10.0))
        and not (
            float(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD", 25_000.0))
            <= mcap
            <= float(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD", 50_000.0))
        )
        and not _price5m_blocked(price5m)
    )


def _passes_pumpswap_profit(row: dict[str, Any]) -> bool:
    return _passes_pumpswap_meteor(row) or (_passes_pumpswap_profit_raw(row) and _passes_profit_shape_guard(row))


def _passes_pumpswap_prime(row: dict[str, Any]) -> bool:
    return (
        _passes_pumpswap_profit(row)
        and _to_float(row.get("market_cap_usd")) < 25_000.0
        and _to_float(row.get("liquidity_usd")) <= 25_000.0
    )


def _passes_pumpswap_meteor(row: dict[str, Any]) -> bool:
    return (
        bool(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_ENABLED", True))
        and bool(_to_int(row.get("has_jupiter_route")))
        and _norm_dex(row) == "pumpswap"
        and not _is_proxy_liquidity(row)
        and float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_LIQUIDITY_USD", 4_000.0))
        <= _to_float(row.get("liquidity_usd"))
        <= float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_LIQUIDITY_USD", 30_000.0))
        and float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_MARKET_CAP_USD", 5_000.0))
        <= _to_float(row.get("market_cap_usd"))
        <= float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_MARKET_CAP_USD", 30_000.0))
        and float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_PRICE_PCT_5M", 110.0))
        <= _to_float(row.get("price_pct_5m"), -9999.0)
        <= float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_PCT_5M", 300.0))
        and _to_int(row.get("txns_last_5m")) >= int(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_TXNS_5M", 220))
        and _to_int(row.get("score_total")) >= int(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_SCORE_TOTAL", 30))
        and float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_AGE_MIN", 3.0))
        <= _to_float(row.get("age_minutes"))
        <= float(getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_AGE_MIN", 18.0))
        and _to_float(row.get("price_impact_pct")) <= float(
            getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MAX_PRICE_IMPACT_PCT", 12.0)
        )
        and _to_float(row.get("volume_24h_usd") or row.get("volume_usd_24h")) >= float(
            getattr(CFG, "PUMP_EARLY_METEOR_PRIME_MIN_VOLUME_USD_24H", 8_000.0)
        )
    )


def _gate_replay_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    current = [row for row in rows if _passes_current_gate(row)]
    core = [row for row in rows if _passes_sniper_core(row)]
    micro = [row for row in rows if _passes_sniper_micro(row)]
    profit_raw = [row for row in rows if _passes_pumpswap_profit_raw(row)]
    profit = [row for row in rows if _passes_pumpswap_profit(row)]
    prime = [row for row in rows if _passes_pumpswap_prime(row)]
    meteor = [row for row in rows if _passes_pumpswap_meteor(row)]
    sniper_any = {
        str(row.get("address") or idx): row
        for idx, row in enumerate([*core, *micro])
    }
    return {
        "pump_candidates": len(rows),
        "current_gate_pass": len(current),
        "sniper_core_pass": len(core),
        "sniper_micro_pass": len(micro),
        "sniper_any_pass": len(sniper_any),
        "pumpswap_profit_raw_pass": len(profit_raw),
        "pumpswap_profit_pass": len(profit),
        "pumpswap_prime_pass": len(prime),
        "pumpswap_meteor_pass": len(meteor),
    }


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}%"


def _readiness(summary: dict[str, Any], *, min_closed: int, avg_floor: float, median_floor: float, win_rate_floor: float, liq_crush_cap: float) -> dict[str, Any]:
    checks = {
        "closed_trades": bool(int(summary.get("closed_trades") or 0) >= min_closed),
        "avg_pnl_pct": bool((summary.get("avg_pnl_pct") or -10_000.0) >= avg_floor),
        "median_pnl_pct": bool((summary.get("median_pnl_pct") or -10_000.0) >= median_floor),
        "win_rate_pct": bool((summary.get("win_rate_pct") or 0.0) >= win_rate_floor),
        "liq_crush_rate_pct": bool((summary.get("liq_crush_rate_pct") or 10_000.0) <= liq_crush_cap),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
    }


def _render_line(title: str, summary: dict[str, Any]) -> str:
    return (
        f"- {title}: closed={summary['closed_trades']} "
        f"avg={_fmt_pct(summary.get('avg_pnl_pct'))} "
        f"median={_fmt_pct(summary.get('median_pnl_pct'))} "
        f"win={_fmt_pct(summary.get('win_rate_pct'))} "
        f"liq_crush={_fmt_pct(summary.get('liq_crush_rate_pct'))} "
        f"loss_streak={int(summary.get('max_loss_streak') or 0)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera reporte determinista de rollout PnL para paper y live_canary.")
    parser.add_argument(
        "--write-docs",
        default="docs/ROLLOUT_REPORT.md",
        help="Ruta markdown de salida. Usa cadena vacia para no escribir.",
    )
    args = parser.parse_args()

    db_path = _db_path_from_uri(DB_URI)
    rows = _load_closed_positions(db_path)
    full_rows = list(rows)
    post_cutoff_rows = [row for row in rows if row.get("closed_at") and row["closed_at"] >= CUTOFF_UTC]
    pump_rows = [row for row in rows if row.get("entry_regime") == "pump_early"]
    pump_post_cutoff_rows = [row for row in post_cutoff_rows if row.get("entry_regime") == "pump_early"]
    canary_rows = [row for row in rows if str(row.get("size_bucket") or "").strip().lower() == "recovery"]
    profit_rows = [
        row
        for row in rows
        if str(row.get("size_bucket") or "").strip().lower() in {"pumpswap_profit", "pumpswap_prime"}
    ]
    prime_rows = [
        row
        for row in rows
        if str(row.get("size_bucket") or "").strip().lower() == "pumpswap_prime"
    ]

    full_summary = _summarize(full_rows)
    post_cutoff_summary = _summarize(post_cutoff_rows)
    pump_summary = _summarize(pump_rows)
    pump_post_cutoff_summary = _summarize(pump_post_cutoff_rows)
    canary_first_10_summary = _summarize(canary_rows[:10])
    canary_first_25_summary = _summarize(canary_rows[:25])
    profit_summary = _summarize(profit_rows)
    prime_summary = _summarize(prime_rows)

    paper_readiness = _readiness(
        profit_summary if profit_rows else pump_summary,
        min_closed=50,
        avg_floor=4.0,
        median_floor=-3.0,
        win_rate_floor=45.0,
        liq_crush_cap=5.0,
    )
    live_canary_start = {
        "passed": (
            int(canary_first_10_summary.get("closed_trades") or 0) >= 10
            and (canary_first_10_summary.get("avg_pnl_pct") or -10_000.0) >= 1.0
            and int(canary_first_10_summary.get("liq_crush_count") or 99) == 0
            and int(canary_first_10_summary.get("max_loss_streak") or 99) <= 2
        )
    }
    live_canary_promote = {
        "passed": (
            int(canary_first_25_summary.get("closed_trades") or 0) >= 25
            and (canary_first_25_summary.get("avg_pnl_pct") or -10_000.0) >= 2.5
            and (canary_first_25_summary.get("win_rate_pct") or 0.0) >= 45.0
            and int(canary_first_25_summary.get("liq_crush_count") or 99) == 0
            and int(canary_first_25_summary.get("max_loss_streak") or 99) <= 2
        )
    }

    scorecard = _load_json(PROJECT_ROOT / "data" / "metrics" / "research_scorecard.json") or {}
    thresholds = _load_json(PROJECT_ROOT / "data" / "metrics" / "research_thresholds.json") or {}
    gate_replay = _gate_replay_summary(_candidate_rows())

    lines = [
        "# Rollout Report",
        "",
        f"- DB: `{db_path}`",
        f"- Closed slice cutoff: `{CUTOFF_UTC.isoformat()}` ({CUTOFF_LABEL})",
        "",
        "## History",
        "",
        _render_line("All closed history", full_summary),
        _render_line("Post-cutoff closed history", post_cutoff_summary),
        _render_line("pump_early only", pump_summary),
        _render_line("pump_early post-cutoff", pump_post_cutoff_summary),
        _render_line("pump_early_pumpswap_profit", profit_summary),
        _render_line("pump_early_pumpswap_prime", prime_summary),
        "",
        "## Readiness",
        "",
        f"- Paper readiness: `{paper_readiness['passed']}` checks={json.dumps(paper_readiness['checks'], sort_keys=True)}",
        f"- Live canary start readiness: `{live_canary_start['passed']}` first10={json.dumps(canary_first_10_summary, sort_keys=True, default=str)}",
        f"- Live canary promotion readiness: `{live_canary_promote['passed']}` first25={json.dumps(canary_first_25_summary, sort_keys=True, default=str)}",
        "",
        "## Gate Replay",
        "",
        f"- Pump candidates with late/reject context: `{gate_replay['pump_candidates']}`",
        f"- Current conservative gate pass: `{gate_replay['current_gate_pass']}`",
        f"- Sniper core pass: `{gate_replay['sniper_core_pass']}`",
        f"- Sniper micro-momentum pass: `{gate_replay['sniper_micro_pass']}`",
        f"- Sniper any pass: `{gate_replay['sniper_any_pass']}`",
        f"- Pumpswap profit raw pass: `{gate_replay['pumpswap_profit_raw_pass']}`",
        f"- Pumpswap profit pass: `{gate_replay['pumpswap_profit_pass']}`",
        f"- Pumpswap prime pass: `{gate_replay['pumpswap_prime_pass']}`",
        f"- Pumpswap meteor-prime pass: `{gate_replay['pumpswap_meteor_pass']}`",
        "",
        "## Research",
        "",
        f"- Scorecard generated: `{scorecard.get('generated_at_utc')}`",
        f"- Thresholds generated: `{thresholds.get('generated_at_utc')}`",
        f"- Scorecard live_closed: `{scorecard.get('live_closed')}`",
        f"- Threshold regimes: `{','.join(sorted((thresholds.get('regimes') or {}).keys())) or '(none)'}`",
        "",
    ]

    markdown = "\n".join(lines)
    print(markdown)

    target = str(args.write_docs or "").strip()
    if target:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
