from __future__ import annotations

import datetime as dt
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import analytics.research_runtime as research_runtime
from config.config import CFG, PROJECT_ROOT
from utils.time import utc_now


log = logging.getLogger("strategy_runtime")

_VALID_MODES = {"live", "shadow", "off"}
_VALID_DISABLE_ACTIONS = {"shadow", "off", "live"}
_CANDIDATE_TTL_MIN = 240
_MIN_CONFIRM_GAP_S = 5
_SCORECARD_PATH = PROJECT_ROOT / "data" / "metrics" / "research_scorecard.json"
_SHADOW_RECOVERY_EVENTS_PATH = PROJECT_ROOT / "data" / "metrics" / "candidate_outcomes.jsonl"
_SCORECARD_CACHE: dict[str, Any] | None = None
_SCORECARD_MTIME_NS: int | None = None
_SHADOW_RECOVERY_CACHE: list[dict[str, Any]] | None = None
_SHADOW_RECOVERY_MTIME_NS: int | None = None
_SHADOW_RECOVERY_SIZE: int | None = None


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _normalize_regime(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"pump_early", "pumpfun", "pump", "pump_fun"}:
        return "pump_early"
    if raw in {"revival", "revive", "revived"}:
        return "revival"
    return "dex_mature"


def _max_consecutive_losses(values: list[float]) -> int:
    best = 0
    current = 0
    for value in values:
        if float(value) > 0.0:
            current = 0
            continue
        current += 1
        if current > best:
            best = current
    return best


def _normalize_mode(value: Any, default: str = "shadow") -> str:
    raw = str(value or default).strip().lower()
    return raw if raw in _VALID_MODES else default


def _disable_action() -> str:
    raw = str(getattr(CFG, "REGIME_HEALTH_DISABLE_ACTION", "shadow") or "shadow").strip().lower()
    return raw if raw in _VALID_DISABLE_ACTIONS else "shadow"


def _cooldown_live_cap() -> float:
    try:
        return max(0.0, float(getattr(CFG, "REGIME_HEALTH_COOLDOWN_MAX_SIZE_MULTIPLIER", 0.20) or 0.20))
    except Exception:
        return 0.20


def _parse_utc(value: Any) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _load_scorecard() -> dict[str, Any] | None:
    global _SCORECARD_CACHE, _SCORECARD_MTIME_NS

    if not bool(getattr(CFG, "STRATEGY_SCORECARD_OVERRIDE_ENABLED", True)):
        return None

    try:
        stat = _SCORECARD_PATH.stat()
    except OSError:
        return None

    mtime_ns = int(getattr(stat, "st_mtime_ns", 0) or 0)
    if _SCORECARD_CACHE is not None and _SCORECARD_MTIME_NS == mtime_ns:
        return _SCORECARD_CACHE

    try:
        payload = json.loads(_SCORECARD_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    _SCORECARD_CACHE = payload
    _SCORECARD_MTIME_NS = mtime_ns
    return payload


def _scorecard_regime_signal(regime: str, now: dt.datetime | None = None) -> dict[str, Any] | None:
    payload = _load_scorecard()
    if not payload:
        return None

    now = now or utc_now()
    generated_at = _parse_utc(payload.get("generated_at_utc"))
    max_age_min = max(0.0, float(getattr(CFG, "STRATEGY_SCORECARD_MAX_AGE_MIN", 240.0) or 240.0))
    if generated_at is None:
        return None
    if max_age_min > 0 and (now - generated_at).total_seconds() / 60.0 > max_age_min:
        return None

    rows = payload.get("outcomes_by_regime")
    if not isinstance(rows, list):
        return None

    target: dict[str, Any] | None = None
    for row in rows:
        if isinstance(row, dict) and _normalize_regime(row.get("group")) == _normalize_regime(regime):
            target = row
            break
    if not target:
        return None

    try:
        count = int(target.get("count") or 0)
    except Exception:
        count = 0
    min_outcomes = max(1, int(getattr(CFG, "STRATEGY_SCORECARD_MIN_OUTCOMES", 12) or 12))
    if count < min_outcomes:
        return None

    avg_pnl_pct = _to_float(target.get("avg_pnl_pct"))
    if avg_pnl_pct is None:
        return None

    return {
        "count": count,
        "avg_pnl_pct": float(avg_pnl_pct),
        "win_rate_pct": _to_float(target.get("win_rate_pct")),
        "generated_at": generated_at,
    }


def _load_shadow_recovery_events() -> list[dict[str, Any]]:
    global _SHADOW_RECOVERY_CACHE, _SHADOW_RECOVERY_MTIME_NS, _SHADOW_RECOVERY_SIZE

    try:
        stat = _SHADOW_RECOVERY_EVENTS_PATH.stat()
    except OSError:
        return []

    mtime_ns = int(getattr(stat, "st_mtime_ns", 0) or 0)
    size = int(getattr(stat, "st_size", 0) or 0)
    if (
        _SHADOW_RECOVERY_CACHE is not None
        and _SHADOW_RECOVERY_MTIME_NS == mtime_ns
        and _SHADOW_RECOVERY_SIZE == size
    ):
        return _SHADOW_RECOVERY_CACHE

    rows: list[dict[str, Any]] = []
    try:
        with _SHADOW_RECOVERY_EVENTS_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except Exception:
        return _SHADOW_RECOVERY_CACHE or []

    _SHADOW_RECOVERY_CACHE = rows
    _SHADOW_RECOVERY_MTIME_NS = mtime_ns
    _SHADOW_RECOVERY_SIZE = size
    return rows


def _row_pnl_pct(row: dict[str, Any]) -> float | None:
    for key in ("pnl_pct", "target_total_pnl_pct", "realized_pnl_pct", "total_pnl_pct"):
        value = _to_float(row.get(key))
        if value is not None:
            return float(value)
    return None


def _cfg_float(name: str, default: float) -> float:
    value = _to_float(getattr(CFG, name, default))
    return float(default if value is None else value)


def _cfg_int(name: str, default: int) -> int:
    value = _to_int(getattr(CFG, name, default))
    return int(default if value is None else value)


def _row_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None:
            return float(value)
    return None


def _range_spec(spec: Any) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    for raw in str(spec or "").split(","):
        chunk = raw.strip()
        if not chunk:
            continue
        try:
            lo_raw, hi_raw = chunk.replace("-", ":").split(":", 1)
            lo = float(lo_raw)
            hi = float(hi_raw)
        except Exception:
            continue
        ranges.append((min(lo, hi), max(lo, hi)))
    return ranges


def _value_in_ranges(value: float, spec: Any) -> bool:
    for lo, hi in _range_spec(spec):
        if lo <= value <= hi:
            return True
    return False


def _bucket_intersects_ranges(bucket: str, spec: Any) -> bool:
    intervals = {
        "<0": (-1_000_000.0, 0.0),
        "0_25": (0.0, 25.0),
        "25_50": (25.0, 50.0),
        "50_100": (50.0, 100.0),
        "100_180": (100.0, 180.0),
        ">=180": (180.0, 1_000_000.0),
    }
    normalized = bucket.strip().lower().replace("price5m_", "")
    if normalized not in intervals:
        return False
    lo, hi = intervals[normalized]
    for block_lo, block_hi in _range_spec(spec):
        if max(lo, block_lo) <= min(hi, block_hi):
            return True
    return False


def _shadow_price5m_bucket_blocked(row: dict[str, Any]) -> bool:
    price_pct_5m = _row_float(row, "price_pct_5m", "buy_price_pct_5m")
    ranges = getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES", "25:999")
    if price_pct_5m is not None and _value_in_ranges(price_pct_5m, ranges):
        return True
    bucket = str(row.get("price5m_bucket") or row.get("buy_price5m_bucket") or "").strip().lower()
    return _bucket_intersects_ranges(bucket, ranges)


def _shadow_row_matches_current_profit_gate(row: dict[str, Any]) -> bool:
    lane = str(row.get("entry_lane") or "").strip()
    profile = str(row.get("gate_profile") or row.get("sniper_gate_profile") or "").strip()
    if lane and lane != "pump_early_pumpswap_profit":
        return False
    if profile == "pumpswap_profit_research":
        return False

    dex_id = str(row.get("dex_id") or row.get("buy_dex_id") or "").strip().lower()
    venue_is_pumpswap = _truthy_int(row.get("venue_is_pumpswap")) == 1 or dex_id == "pumpswap"
    if not venue_is_pumpswap:
        return False

    liquidity = _row_float(row, "liquidity_usd", "buy_liquidity_usd")
    market_cap = _row_float(row, "market_cap_usd", "buy_market_cap_usd", "mcap")
    age_min = _row_float(row, "age_minutes", "age_min")
    score_total = _cfg_int("PUMP_EARLY_PROFIT_MIN_SCORE_TOTAL", 35)
    row_score = _to_int(row.get("score_total") or row.get("entry_score_total")) or 0
    if liquidity is None or liquidity < _cfg_float("PUMP_EARLY_PROFIT_MIN_LIQUIDITY_USD", 5_000.0):
        return False
    if market_cap is None or market_cap <= 0:
        return False
    if age_min is None:
        return False
    if age_min < _cfg_float("PUMP_EARLY_PROFIT_MIN_AGE_MIN", 3.0):
        return False
    if age_min > _cfg_float("PUMP_EARLY_PROFIT_MAX_AGE_MIN", 30.0):
        return False
    if row_score < score_total:
        return False

    proxy_flag = row.get("liquidity_is_proxy")
    if proxy_flag is None:
        proxy_flag = row.get("liquidity_usd_is_proxy")
    if proxy_flag is None:
        proxy_flag = row.get("buy_liquidity_is_proxy")
    if bool(getattr(CFG, "PUMP_EARLY_PROFIT_REQUIRE_REAL_LIQUIDITY", True)) and (
        _truthy_int(proxy_flag) == 1 or abs(float(liquidity) - 1_500.0) <= 1.0
    ):
        return False

    impact = _row_float(row, "price_impact_pct", "buy_price_impact_pct")
    if impact is not None and impact > _cfg_float("PUMP_EARLY_PROFIT_MAX_PRICE_IMPACT_PCT", 10.0):
        return False

    block_min = _cfg_float("PUMP_EARLY_PROFIT_BLOCK_MCAP_MIN_USD", 0.0)
    block_max = _cfg_float("PUMP_EARLY_PROFIT_BLOCK_MCAP_MAX_USD", 0.0)
    if block_min > 0 and block_max > 0 and block_min <= market_cap <= block_max:
        return False

    if _shadow_price5m_bucket_blocked(row):
        return False

    max_mcap = _cfg_float("PUMP_EARLY_PROFIT_MAX_MARKET_CAP_USD", 200_000.0)
    if bool(getattr(CFG, "PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED", True)) and max_mcap > 0 and market_cap >= max_mcap:
        return False

    price_pct_5m = _row_float(row, "price_pct_5m", "buy_price_pct_5m")
    price5m_bucket = str(row.get("price5m_bucket") or "").strip().lower()
    high_mid_min_mcap = _cfg_float("PUMP_EARLY_PROFIT_HIGH_MCAP_MID_MIN_MCAP_USD", 100_000.0)
    high_mid_lo = _cfg_float("PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MIN_PCT", 40.0)
    high_mid_hi = _cfg_float("PUMP_EARLY_PROFIT_HIGH_MCAP_MID_PRICE5M_MAX_PCT", 50.0)
    high_mid_price = (
        high_mid_lo <= price_pct_5m < high_mid_hi
        if price_pct_5m is not None
        else price5m_bucket == "25_50"
    )
    if (
        bool(getattr(CFG, "PUMP_EARLY_PROFIT_SHAPE_GUARD_ENABLED", True))
        and market_cap >= high_mid_min_mcap
        and high_mid_price
    ):
        return False

    return True


def _shadow_row_is_productive_recovery(row: dict[str, Any]) -> bool:
    if str(row.get("event_type") or "") != "candidate_outcome":
        return False
    if str(row.get("source") or "") != "research_shadow":
        return False
    if str(row.get("shadow_kind") or "") != "execution":
        return False
    if _normalize_regime(row.get("regime")) != "pump_early":
        return False

    reason = str(row.get("reason") or "").strip()
    return reason == "strategy:recovery_not_ready" and _shadow_row_matches_current_profit_gate(row)


def _shadow_recovery_signal(regime: str, now: dt.datetime | None = None) -> dict[str, Any]:
    if _normalize_regime(regime) != "pump_early":
        return {"ready": False}
    if not bool(getattr(CFG, "PUMP_EARLY_SHADOW_RECOVERY_ENABLED", True)):
        return {"ready": False}

    now = now or utc_now()
    max_age_h = max(0.0, float(getattr(CFG, "PUMP_EARLY_SHADOW_RECOVERY_MAX_AGE_H", 36.0) or 36.0))
    window = max(1, int(getattr(CFG, "PUMP_EARLY_SHADOW_RECOVERY_WINDOW", 8) or 8))
    min_trades = max(1, int(getattr(CFG, "PUMP_EARLY_SHADOW_RECOVERY_MIN_TRADES", window) or window))
    min_avg = float(getattr(CFG, "PUMP_EARLY_SHADOW_RECOVERY_MIN_AVG_PNL_PCT", 5.0) or 5.0)
    min_win_rate = float(getattr(CFG, "PUMP_EARLY_SHADOW_RECOVERY_MIN_WIN_RATE_PCT", 45.0) or 45.0)
    max_severe = max(0, int(getattr(CFG, "PUMP_EARLY_SHADOW_RECOVERY_MAX_SEVERE_EXITS", 2) or 2))
    max_liq = max(0, int(getattr(CFG, "PUMP_EARLY_SHADOW_RECOVERY_MAX_LIQ_CRUSH", 1) or 1))
    max_loss_streak = max(0, int(getattr(CFG, "PUMP_EARLY_SHADOW_RECOVERY_MAX_CONSECUTIVE_LOSSES", 3) or 3))

    candidates: list[tuple[dt.datetime, dict[str, Any], float]] = []
    cutoff = now - dt.timedelta(hours=max_age_h) if max_age_h > 0 else None
    for row in _load_shadow_recovery_events():
        if not _shadow_row_is_productive_recovery(row):
            continue
        ts = _parse_utc(row.get("ts_utc") or row.get("timestamp") or row.get("ts"))
        if ts is None:
            continue
        if cutoff is not None and ts < cutoff:
            continue
        pnl = _row_pnl_pct(row)
        if pnl is None:
            continue
        candidates.append((ts, row, float(pnl)))

    candidates.sort(key=lambda item: item[0])
    selected = candidates[-window:]
    count = len(selected)
    if count < min_trades:
        return {
            "ready": False,
            "source": "shadow_productive_recovery",
            "count": count,
            "min_trades": min_trades,
            "window": window,
        }

    pnls = [pnl for _, _, pnl in selected]
    avg_pnl = sum(pnls) / len(pnls)
    win_rate_pct = sum(1 for value in pnls if value > 0.0) / len(pnls) * 100.0
    severe_count = sum(1 for _, row, pnl in selected if _is_severe_exit(str(row.get("exit_reason") or ""), pnl))
    liq_count = sum(1 for _, row, _ in selected if str(row.get("exit_reason") or "").strip().upper() == "LIQUIDITY_CRUSH")
    loss_streak = _max_consecutive_losses(pnls)
    ready = (
        avg_pnl >= min_avg
        and win_rate_pct >= min_win_rate
        and severe_count <= max_severe
        and liq_count <= max_liq
        and loss_streak <= max_loss_streak
    )
    return {
        "ready": bool(ready),
        "source": "shadow_productive_recovery",
        "count": count,
        "window": window,
        "avg_pnl_pct": float(avg_pnl),
        "win_rate_pct": float(win_rate_pct),
        "severe_exit_count": int(severe_count),
        "liq_crush_count": int(liq_count),
        "recent_max_consecutive_losses": int(loss_streak),
        "min_avg_pnl_pct": min_avg,
        "min_win_rate_pct": min_win_rate,
        "max_severe_exits": max_severe,
        "max_liq_crush": max_liq,
        "max_consecutive_losses_allowed": max_loss_streak,
        "first_event_at": selected[0][0],
        "last_event_at": selected[-1][0],
    }


def _recovery_signal(regime: str, now: dt.datetime | None = None) -> dict[str, Any]:
    resolved_regime = _normalize_regime(regime)
    rank_gate = research_runtime.load_live_rank_gate(resolved_regime, now=now)
    scorecard_signal = _scorecard_regime_signal(resolved_regime, now=now)
    outcomes = int((scorecard_signal or {}).get("count") or 0)
    win_rate_pct = _to_float((scorecard_signal or {}).get("win_rate_pct"))
    avg_pnl_pct = _to_float((scorecard_signal or {}).get("avg_pnl_pct"))
    selected_rows = int(rank_gate.get("selected_rows_at_picked") or 0)
    avg_realized = _to_float(rank_gate.get("avg_realized_pnl_pct_at_picked"))
    min_win_rate_pct = float(getattr(CFG, "PUMP_EARLY_RECOVERY_MIN_WIN_RATE_PCT", 42.0) or 42.0)
    ready = (
        resolved_regime == "pump_early"
        and bool(scorecard_signal)
        and not bool(rank_gate.get("stale"))
        and bool(rank_gate.get("activation_ready"))
        and outcomes >= 60
        and selected_rows >= 20
        and avg_realized is not None
        and float(avg_realized) >= 3.0
        and win_rate_pct is not None
        and float(win_rate_pct) >= min_win_rate_pct
    )
    return {
        "ready": bool(ready),
        "outcomes": outcomes,
        "selected_rows_at_picked": selected_rows,
        "avg_realized_pnl_pct_at_picked": avg_realized,
        "win_rate_pct": win_rate_pct,
        "min_win_rate_pct": min_win_rate_pct,
        "avg_pnl_pct": avg_pnl_pct,
        "rank_gate": rank_gate,
    }


def _recent_recovery_signal(
    regime: str,
    pnls: list[float],
    severe_exits: list[bool],
    liq_crush_exits: list[bool],
) -> dict[str, Any]:
    if _normalize_regime(regime) != "pump_early":
        return {"ready": False}
    if not bool(getattr(CFG, "PUMP_EARLY_RECOVERY_RECENT_OVERRIDE_ENABLED", True)):
        return {"ready": False}

    window = max(
        1,
        int(
            getattr(
                CFG,
                "PUMP_EARLY_PROFIT_RECOVERY_RECENT_TRADES",
                getattr(CFG, "PUMP_EARLY_RECOVERY_RECENT_TRADES", 8),
            )
            or 8
        ),
    )
    if len(pnls) < window:
        return {"ready": False, "window": window, "closed": len(pnls)}

    recent_pnls = [float(x) for x in pnls[-window:]]
    recent_severe = list(severe_exits[-window:])
    recent_liq = list(liq_crush_exits[-window:])
    avg_pnl = sum(recent_pnls) / len(recent_pnls)
    win_rate_pct = (sum(1 for x in recent_pnls if x > 0.0) / len(recent_pnls)) * 100.0
    min_avg = float(
        getattr(
            CFG,
            "PUMP_EARLY_PROFIT_RECOVERY_RECENT_MIN_AVG_PNL_PCT",
            getattr(CFG, "PUMP_EARLY_RECOVERY_RECENT_MIN_AVG_PNL_PCT", 5.0),
        )
        or 5.0
    )
    max_consecutive_losses = max(
        0,
        int(
            getattr(
                CFG,
                "PUMP_EARLY_PROFIT_RECOVERY_RECENT_MAX_CONSECUTIVE_LOSSES",
                getattr(CFG, "PUMP_EARLY_RECOVERY_RECENT_MAX_CONSECUTIVE_LOSSES", 2),
            )
            or 2
        ),
    )
    recent_loss_streak = _max_consecutive_losses(recent_pnls)
    ready = (
        avg_pnl >= min_avg
        and not any(recent_liq)
        and recent_loss_streak <= max_consecutive_losses
    )
    return {
        "ready": bool(ready),
        "source": "recent_clean_productive_window",
        "window": window,
        "avg_pnl_pct": avg_pnl,
        "win_rate_pct": win_rate_pct,
        "severe_exit_count": sum(1 for flag in recent_severe if flag),
        "liq_crush_count": sum(1 for flag in recent_liq if flag),
        "min_avg_pnl_pct": min_avg,
        "recent_max_consecutive_losses": recent_loss_streak,
        "max_consecutive_losses_allowed": max_consecutive_losses,
    }


@dataclass
class CandidateState:
    address: str
    regime: str
    first_seen: dt.datetime
    last_seen: dt.datetime
    confirmations: int = 0
    last_liquidity_usd: float | None = None
    last_has_route: bool | None = None
    last_age_min: float | None = None


@dataclass
class RegimeHealth:
    trade_pnls_pct: deque[float] = field(
        default_factory=lambda: deque(maxlen=max(1, int(getattr(CFG, "REGIME_HEALTH_WINDOW_TRADES", 20) or 20)))
    )
    trade_wins: deque[bool] = field(
        default_factory=lambda: deque(maxlen=max(1, int(getattr(CFG, "REGIME_HEALTH_WINDOW_TRADES", 20) or 20)))
    )
    exec_success: deque[bool] = field(
        default_factory=lambda: deque(maxlen=max(1, int(getattr(CFG, "REGIME_HEALTH_WINDOW_EVENTS", 40) or 40)))
    )
    price_coverage: deque[bool] = field(
        default_factory=lambda: deque(maxlen=max(1, int(getattr(CFG, "REGIME_HEALTH_WINDOW_EVENTS", 40) or 40)))
    )
    consecutive_losses: int = 0
    cooldown_until: dt.datetime | None = None
    last_disable_reason: str | None = None
    severe_exits: deque[bool] = field(
        default_factory=lambda: deque(maxlen=max(1, int(getattr(CFG, "REGIME_HEALTH_WINDOW_TRADES", 20) or 20)))
    )
    liq_crush_exits: deque[bool] = field(
        default_factory=lambda: deque(maxlen=max(1, int(getattr(CFG, "REGIME_HEALTH_WINDOW_TRADES", 20) or 20)))
    )
    recovery_trade_pnls_pct: deque[float] = field(default_factory=lambda: deque(maxlen=25))
    recovery_trade_wins: deque[bool] = field(default_factory=lambda: deque(maxlen=25))
    recovery_severe_exits: deque[bool] = field(default_factory=lambda: deque(maxlen=25))
    recovery_liq_crush_exits: deque[bool] = field(default_factory=lambda: deque(maxlen=25))
    recovery_consecutive_losses: int = 0
    recovery_armed: bool = False
    canary_active: bool = False
    last_auto_demote_at: dt.datetime | None = None
    last_auto_recover_at: dt.datetime | None = None


@dataclass
class BucketHealth:
    trade_pnls_pct: deque[float] = field(default_factory=lambda: deque(maxlen=20))
    severe_exits: deque[bool] = field(default_factory=lambda: deque(maxlen=20))
    consecutive_losses: int = 0
    cooldown_until: dt.datetime | None = None
    last_disable_reason: str | None = None
    last_auto_demote_at: dt.datetime | None = None


@dataclass(frozen=True)
class StrategyDecision:
    regime: str
    requested_mode: str
    effective_mode: str
    effective_execution_state: str
    health_state: str
    action: str
    reason: str
    confirmations: int
    confirmations_required: int
    requeue_backoff_s: int
    size_cap_multiplier: float | None = None


_CANDIDATES: dict[str, CandidateState] = {}
_HEALTH: dict[str, RegimeHealth] = {
    "pump_early": RegimeHealth(),
    "dex_mature": RegimeHealth(),
    "revival": RegimeHealth(),
}
_BUCKET_HEALTH: dict[str, BucketHealth] = {}


_SEVERE_EXIT_REASONS = {"LIQUIDITY_CRUSH", "STOP_LOSS", "EARLY_DROP", "ADVERSE_TICK"}


def _policy_for_regime(regime: str) -> dict[str, float | int | str | bool]:
    regime_key = _normalize_regime(regime)
    default_mode = _normalize_mode(getattr(CFG, "STRATEGY_REGIME_MODE_DEFAULT", "shadow"), "shadow")

    if _paper_aggressive_enabled():
        return _paper_aggressive_policy(regime_key)
    if _live_aggressive_enabled():
        return _live_aggressive_policy(regime_key)

    if regime_key == "pump_early":
        return {
            "mode": _normalize_mode(getattr(CFG, "PUMP_EARLY_EXECUTION_MODE", default_mode), default_mode),
            "confirmations": max(1, int(getattr(CFG, "PUMP_EARLY_CONFIRM_SNAPSHOTS", 3) or 3)),
            "backoff_s": max(10, int(getattr(CFG, "PUMP_EARLY_CONFIRM_BACKOFF_S", 30) or 30)),
            "min_age_min": max(0.0, float(getattr(CFG, "PUMP_EARLY_CONFIRM_MIN_AGE_MIN", 1.0) or 1.0)),
            "recovery_cap": max(
                0.0,
                float(getattr(CFG, "PUMP_EARLY_RECOVERY_MAX_SIZE_MULTIPLIER", 0.20) or 0.20),
            ),
        }
    if regime_key == "revival":
        return {
            "mode": _normalize_mode(getattr(CFG, "REVIVAL_EXECUTION_MODE", default_mode), default_mode),
            "confirmations": max(1, int(getattr(CFG, "REVIVAL_CONFIRM_SNAPSHOTS", 2) or 2)),
            "backoff_s": max(10, int(getattr(CFG, "REVIVAL_CONFIRM_BACKOFF_S", 60) or 60)),
            "min_age_min": max(0.0, float(getattr(CFG, "REVIVAL_CONFIRM_MIN_AGE_MIN", 8.0) or 8.0)),
            "recovery_cap": max(
                0.0,
                float(getattr(CFG, "REVIVAL_RECOVERY_MAX_SIZE_MULTIPLIER", 0.30) or 0.30),
            ),
        }
    return {
        "mode": _normalize_mode(getattr(CFG, "DEX_MATURE_EXECUTION_MODE", "live"), "live"),
        "confirmations": max(1, int(getattr(CFG, "DEX_MATURE_CONFIRM_SNAPSHOTS", 2) or 2)),
        "backoff_s": max(10, int(getattr(CFG, "DEX_MATURE_CONFIRM_BACKOFF_S", 45) or 45)),
        "min_age_min": max(0.0, float(getattr(CFG, "DEX_MATURE_CONFIRM_MIN_AGE_MIN", 3.0) or 3.0)),
        "recovery_cap": max(
            0.0,
            float(getattr(CFG, "DEX_MATURE_RECOVERY_MAX_SIZE_MULTIPLIER", 0.50) or 0.50),
        ),
    }


def _sniper_enabled(regime: str) -> bool:
    return _normalize_regime(regime) == "pump_early" and bool(getattr(CFG, "PUMP_EARLY_SNIPER_ENABLED", True))


def _sniper_paper_continue_on_health(regime: str) -> bool:
    return (
        _sniper_enabled(regime)
        and bool(getattr(CFG, "DRY_RUN", False))
        and bool(getattr(CFG, "PUMP_EARLY_SNIPER_PAPER_CONTINUE_ON_HEALTH", False))
        and not bool(getattr(CFG, "PAPER_PNL_STRICT_HEALTH", True))
    )


def _sniper_paper_recovery_cap() -> float:
    try:
        return max(0.0, float(getattr(CFG, "PUMP_EARLY_SNIPER_PAPER_RECOVERY_SIZE_CAP", 0.20) or 0.20))
    except Exception:
        return 0.20


def _paper_aggressive_enabled() -> bool:
    return bool(getattr(CFG, "DRY_RUN", False)) and bool(
        getattr(CFG, "PAPER_AGGRESSIVE_TRADING_ENABLED", False)
    )


def _live_aggressive_enabled() -> bool:
    return (not bool(getattr(CFG, "DRY_RUN", False))) and bool(
        getattr(CFG, "LIVE_AGGRESSIVE_TRADING_ENABLED", False)
    )


def _live_aggressive_recovery_cap() -> float:
    try:
        return max(0.0, float(getattr(CFG, "LIVE_AGGRESSIVE_HEALTH_SIZE_CAP_MULTIPLIER", 0.10) or 0.10))
    except Exception:
        return 0.10


def _live_aggressive_continue_on_health(regime: str) -> bool:
    _ = regime
    return (
        _live_aggressive_enabled()
        and bool(getattr(CFG, "LIVE_AGGRESSIVE_CONTINUE_ON_HEALTH", True))
    )


def _paper_sniper_continue_on_health(token: dict[str, Any], regime: str) -> bool:
    lane = str(token.get("entry_lane") or "").strip().lower()
    profile = str(token.get("gate_profile") or token.get("sniper_gate_profile") or "").strip().lower()
    return (
        _normalize_regime(regime) == "pump_early"
        and bool(getattr(CFG, "DRY_RUN", False))
        and bool(getattr(CFG, "PAPER_SNIPER_MODE", False))
        and bool(getattr(CFG, "PAPER_SNIPER_CONTINUE_ON_HEALTH", True))
        and bool(getattr(CFG, "PAPER_SNIPER_IGNORE_REGIME_COOLDOWN", True))
        and (lane == "pump_early_green_candle_sniper" or profile.startswith("green_sniper"))
    )


def _regime_cooldown_minutes() -> int:
    return max(1, int(getattr(CFG, "REGIME_HEALTH_COOLDOWN_MIN", 120) or 120))


def _recovery_demote_min_trades() -> int:
    return max(1, int(getattr(CFG, "PUMP_EARLY_RECOVERY_DEMOTE_MIN_TRADES", 3) or 3))


def _paper_aggressive_policy(regime_key: str) -> dict[str, float | int | str | bool]:
    if regime_key == "pump_early":
        recovery_cap = float(getattr(CFG, "PUMP_EARLY_RECOVERY_MAX_SIZE_MULTIPLIER", 0.20) or 0.20)
    elif regime_key == "revival":
        recovery_cap = float(getattr(CFG, "REVIVAL_RECOVERY_MAX_SIZE_MULTIPLIER", 0.30) or 0.30)
    else:
        recovery_cap = float(getattr(CFG, "DEX_MATURE_RECOVERY_MAX_SIZE_MULTIPLIER", 0.50) or 0.50)
    return {
        "mode": "live",
        "confirmations": max(1, int(getattr(CFG, "PAPER_AGGRESSIVE_CONFIRM_SNAPSHOTS", 1) or 1)),
        "backoff_s": max(5, int(getattr(CFG, "PAPER_AGGRESSIVE_CONFIRM_BACKOFF_S", 10) or 10)),
        "min_age_min": max(0.0, float(getattr(CFG, "PAPER_AGGRESSIVE_MIN_AGE_MIN", 0.05) or 0.0)),
        "recovery_cap": max(0.0, recovery_cap),
    }


def _live_aggressive_policy(regime_key: str) -> dict[str, float | int | str | bool]:
    if regime_key == "pump_early":
        recovery_cap = _live_aggressive_recovery_cap()
    elif regime_key == "revival":
        recovery_cap = float(getattr(CFG, "REVIVAL_RECOVERY_MAX_SIZE_MULTIPLIER", 0.30) or 0.30)
    else:
        recovery_cap = float(getattr(CFG, "DEX_MATURE_RECOVERY_MAX_SIZE_MULTIPLIER", 0.50) or 0.50)
    return {
        "mode": "live",
        "confirmations": max(1, int(getattr(CFG, "LIVE_AGGRESSIVE_CONFIRM_SNAPSHOTS", 1) or 1)),
        "backoff_s": max(5, int(getattr(CFG, "LIVE_AGGRESSIVE_CONFIRM_BACKOFF_S", 10) or 10)),
        "min_age_min": max(0.0, float(getattr(CFG, "LIVE_AGGRESSIVE_MIN_AGE_MIN", 0.05) or 0.0)),
        "recovery_cap": max(0.0, recovery_cap),
    }


def _to_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except Exception:
        return None


def _sniper_fast_confirm(token: dict[str, Any], has_route: bool | None) -> bool:
    if not _sniper_enabled(token.get("entry_regime") or token.get("discovered_via")):
        return False
    route_ok = bool(has_route) if has_route is not None else bool(_to_int(token.get("has_jupiter_route")) or 0)
    if not route_ok:
        return False
    age_min = _to_float(token.get("age_minutes") or token.get("age_min")) or 0.0
    txns_5m = _to_int(token.get("txns_last_5m")) or 0
    price_pct_5m = _to_float(token.get("price_pct_5m"))
    return (
        age_min >= float(getattr(CFG, "PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_AGE_MIN", 3.0) or 3.0)
        and txns_5m >= int(getattr(CFG, "PUMP_EARLY_SNIPER_FAST_CONFIRM_MIN_TXNS_5M", 40) or 40)
        and price_pct_5m is not None
    )


def _truthy_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        raw = str(value or "").strip().lower()
        return 1 if raw in {"true", "yes", "on"} else 0


def _bucket_key(
    *,
    regime: str,
    entry_lane: Any = None,
    dex_id: Any = None,
    liquidity_proxy_flag: Any = None,
    mcap_bucket: Any = None,
    price5m_bucket: Any = None,
    gate_profile: Any = None,
) -> str:
    if _normalize_regime(regime) != "pump_early":
        return ""
    lane = str(entry_lane or "").strip().lower()
    profile = str(gate_profile or "").strip().lower()
    if not lane and not profile:
        return ""
    dex = str(dex_id or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "") or "unknown"
    proxy = "proxy" if _truthy_int(liquidity_proxy_flag) else "real"
    mcap = str(mcap_bucket or "unknown").strip().lower() or "unknown"
    price5m = str(price5m_bucket or "unknown").strip().lower() or "unknown"
    return f"{lane or 'unknown'}|{profile or 'unknown'}|{dex}|{proxy}|{mcap}|{price5m}"


def _bucket_key_from_token(token: dict[str, Any], regime: str) -> str:
    return _bucket_key(
        regime=regime,
        entry_lane=token.get("entry_lane"),
        dex_id=token.get("dex_id") or token.get("dexId"),
        liquidity_proxy_flag=token.get("liquidity_is_proxy") or token.get("liquidity_usd_is_proxy"),
        mcap_bucket=token.get("mcap_bucket"),
        price5m_bucket=token.get("price5m_bucket"),
        gate_profile=token.get("gate_profile") or token.get("sniper_gate_profile"),
    )


def _is_severe_exit(exit_reason: str | None, pnl_pct: float) -> bool:
    reason = str(exit_reason or "").strip().upper()
    return reason in _SEVERE_EXIT_REASONS or float(pnl_pct) <= -25.0


def _is_breakout_lane(entry_lane: Any = None, gate_profile: Any = None, size_bucket: Any = None) -> bool:
    lane = str(entry_lane or "").strip().lower()
    profile = str(gate_profile or "").strip().lower()
    bucket = str(size_bucket or "").strip().lower()
    return (
        lane == "pump_early_pumpswap_breakout_probe"
        or profile.startswith("pumpswap_breakout")
        or bucket == "pumpswap_breakout"
    )


def _record_bucket_close(
    *,
    regime: str,
    pnl_pct: float,
    exit_reason: str | None,
    entry_lane: Any = None,
    dex_id: Any = None,
    liquidity_proxy_flag: Any = None,
    mcap_bucket: Any = None,
    price5m_bucket: Any = None,
    gate_profile: Any = None,
) -> None:
    key = _bucket_key(
        regime=regime,
        entry_lane=entry_lane,
        dex_id=dex_id,
        liquidity_proxy_flag=liquidity_proxy_flag,
        mcap_bucket=mcap_bucket,
        price5m_bucket=price5m_bucket,
        gate_profile=gate_profile,
    )
    if not key:
        return
    now = utc_now()
    health = _BUCKET_HEALTH.setdefault(key, BucketHealth())
    health.trade_pnls_pct.append(float(pnl_pct))
    won = float(pnl_pct) > 0.0
    health.consecutive_losses = 0 if won else health.consecutive_losses + 1
    health.severe_exits.append(_is_severe_exit(exit_reason, float(pnl_pct)))

    pnls = list(health.trade_pnls_pct)
    last8 = pnls[-8:]
    last20 = pnls[-20:]
    severe20 = list(health.severe_exits)[-20:]
    reasons: list[str] = []
    if len(last8) >= 8 and (sum(last8) / len(last8)) <= -5.0:
        reasons.append("bucket_expectancy_8")
    if len(last20) >= 20 and (sum(last20) / len(last20)) <= 0.0:
        reasons.append("bucket_expectancy_20")
    if int(health.consecutive_losses) >= 4:
        reasons.append("bucket_loss_streak")
    if sum(1 for flag in severe20 if flag) >= 2:
        reasons.append("bucket_severe")
    if str(exit_reason or "").strip().upper() == "LIQUIDITY_CRUSH" and len(pnls) <= 10:
        reasons.append("bucket_liq_crush_canary")
    if reasons:
        health.last_disable_reason = ",".join(reasons)
        health.cooldown_until = now + dt.timedelta(minutes=_regime_cooldown_minutes())
        health.last_auto_demote_at = now


def _bucket_disable_reason(token: dict[str, Any], regime: str, now: dt.datetime) -> str | None:
    key = _bucket_key_from_token(token, regime)
    if not key:
        return None
    health = _BUCKET_HEALTH.get(key)
    if not health:
        return None
    if health.cooldown_until and now < health.cooldown_until:
        return f"bucket:{health.last_disable_reason or 'cooldown'}"
    if health.cooldown_until and now >= health.cooldown_until:
        health.cooldown_until = None
    return None


def _health_snapshot(regime: str, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    health = _HEALTH[_normalize_regime(regime)]
    pnls = list(health.trade_pnls_pct)
    wins = list(health.trade_wins)
    exec_events = list(health.exec_success)
    price_events = list(health.price_coverage)
    severe_exits = list(health.severe_exits)
    liq_crush_exits = list(health.liq_crush_exits)
    recovery_pnls = list(health.recovery_trade_pnls_pct)
    recovery_wins = list(health.recovery_trade_wins)
    recovery_severe = list(health.recovery_severe_exits)
    recovery_liq_crush = list(health.recovery_liq_crush_exits)

    trade_count = len(pnls)
    avg_pnl_pct = (sum(pnls) / trade_count) if trade_count else None
    win_rate = (sum(1 for x in wins if x) / len(wins)) if wins else None
    exec_rate = (sum(1 for x in exec_events if x) / len(exec_events)) if exec_events else None
    price_rate = (sum(1 for x in price_events if x) / len(price_events)) if price_events else None
    sniper_health = _sniper_enabled(regime)
    short_window_size = (
        max(1, int(getattr(CFG, "PUMP_EARLY_SNIPER_DEMOTE_WINDOW_TRADES", 8) or 8))
        if sniper_health
        else 6
    )
    short_window = pnls[-short_window_size:]
    short_avg_pnl_pct = (sum(short_window) / len(short_window)) if short_window else None
    severe_exit_count = sum(1 for flag in severe_exits if flag)
    liq_crush_count = sum(1 for flag in liq_crush_exits if flag)
    recovery_trade_count = len(recovery_pnls)
    recovery_avg_pnl_pct = (sum(recovery_pnls) / recovery_trade_count) if recovery_trade_count else None
    recovery_win_rate = (sum(1 for x in recovery_wins if x) / len(recovery_wins)) if recovery_wins else None
    recovery_signal = _recovery_signal(regime, now=now)
    recent_recovery_signal = _recent_recovery_signal(regime, pnls, severe_exits, liq_crush_exits)
    shadow_recovery_signal = _shadow_recovery_signal(regime, now=now)
    recent_ready = bool(recent_recovery_signal.get("ready"))
    shadow_ready = bool(shadow_recovery_signal.get("ready"))
    recovery_ready = bool(recovery_signal.get("ready")) or recent_ready or shadow_ready
    recovery_basis = (
        dict(recent_recovery_signal)
        if recent_ready
        else dict(shadow_recovery_signal)
        if shadow_ready
        else dict(recovery_signal)
        if bool(recovery_signal.get("ready"))
        else {}
    )

    min_trades = max(1, int(getattr(CFG, "REGIME_HEALTH_MIN_TRADES", 6) or 6))
    enough_trades = trade_count >= min_trades
    enough_exec = len(exec_events) >= max(4, min_trades // 2)
    enough_price = len(price_events) >= max(6, min_trades)

    state = "normal"
    disable_reason = health.last_disable_reason

    if health.cooldown_until and now < health.cooldown_until:
        state = "cooldown"
    else:
        if health.cooldown_until and now >= health.cooldown_until:
            health.cooldown_until = None
        if health.recovery_armed and not health.canary_active:
            if recovery_ready:
                health.canary_active = True
                health.last_auto_recover_at = now
                state = "recovery"
            else:
                state = "shadow_wait"
        else:
            bad_expectancy = (
                enough_trades
                and avg_pnl_pct is not None
                and avg_pnl_pct <= float(getattr(CFG, "REGIME_HEALTH_DISABLE_EXPECTANCY_PCT", -5.0) or -5.0)
            )
            short_floor = (
                float(getattr(CFG, "PUMP_EARLY_SNIPER_DEMOTE_AVG_PNL_PCT", -5.0) or -5.0)
                if sniper_health
                else -2.0
            )
            loss_streak_limit = (
                int(getattr(CFG, "PUMP_EARLY_SNIPER_DEMOTE_LOSS_STREAK", 4) or 4)
                if sniper_health
                else min(3, int(getattr(CFG, "REGIME_HEALTH_MAX_CONSECUTIVE_LOSSES", 4) or 4))
            )
            bad_short_expectancy = (
                len(short_window) >= short_window_size
                and short_avg_pnl_pct is not None
                and short_avg_pnl_pct <= short_floor
            )
            bad_loss_streak = health.consecutive_losses >= loss_streak_limit
            bad_exec = (
                enough_exec
                and exec_rate is not None
                and exec_rate < float(getattr(CFG, "REGIME_HEALTH_MIN_EXEC_SUCCESS_RATE", 0.70) or 0.70)
            )
            bad_price = (
                enough_price
                and price_rate is not None
                and price_rate < float(getattr(CFG, "REGIME_HEALTH_MIN_PRICE_COVERAGE_RATE", 0.70) or 0.70)
            )
            canary_liq_window = int(getattr(CFG, "PUMP_EARLY_SNIPER_DEMOTE_LIQ_CRUSH_FIRST_CLOSES", 10) or 10)
            rolling_liq_limit = int(getattr(CFG, "PUMP_EARLY_SNIPER_DEMOTE_LIQ_CRUSH_ROLLING", 2) or 2)
            bad_liq_crush_canary = bool(
                sniper_health
                and liq_crush_count >= 1
                and trade_count <= max(1, canary_liq_window)
            ) or bool(health.canary_active and any(recovery_liq_crush[-1:]))
            ignore_old_liq_crush = (
                bool(getattr(CFG, "PUMP_EARLY_RECOVERY_RECENT_IGNORE_OLD_LIQ_CRUSH", True))
                and (bool(recent_recovery_signal.get("ready")) or bool(shadow_recovery_signal.get("ready")))
            )
            bad_liq_crush_normal = bool(
                (not health.canary_active)
                and not ignore_old_liq_crush
                and liq_crush_count >= max(1, rolling_liq_limit)
            )
            if health.canary_active:
                recovery_min_trades = _recovery_demote_min_trades()
                recent_recovery_pnls = [float(x) for x in recovery_pnls[-recovery_min_trades:]]
                recovery_avg = (
                    sum(recent_recovery_pnls) / len(recent_recovery_pnls)
                    if len(recent_recovery_pnls) >= recovery_min_trades
                    else None
                )
                bad_expectancy = False
                bad_short_expectancy = recovery_avg is not None and recovery_avg <= short_floor
                bad_loss_streak = (
                    len(recovery_pnls) >= recovery_min_trades
                    and health.recovery_consecutive_losses >= min(loss_streak_limit, recovery_min_trades)
                )
                bad_exec = False
                bad_price = False
                bad_liq_crush_canary = any(recovery_liq_crush[-1:])
                bad_liq_crush_normal = False

            if bad_expectancy or bad_short_expectancy or bad_loss_streak or bad_exec or bad_price or bad_liq_crush_canary or bad_liq_crush_normal:
                reasons = []
                if bad_expectancy:
                    reasons.append("expectancy")
                if bad_short_expectancy:
                    reasons.append(f"expectancy_{short_window_size}")
                if bad_loss_streak:
                    reasons.append("loss_streak")
                if bad_exec:
                    reasons.append("exec")
                if bad_price:
                    reasons.append("price")
                if bad_liq_crush_canary:
                    reasons.append("liq_crush_canary")
                if bad_liq_crush_normal:
                    reasons.append("liq_crush")
                disable_reason = ",".join(reasons) or "health"
                health.last_disable_reason = disable_reason
                health.cooldown_until = now + dt.timedelta(minutes=_regime_cooldown_minutes())
                health.recovery_armed = True
                health.canary_active = False
                health.recovery_trade_pnls_pct.clear()
                health.recovery_trade_wins.clear()
                health.recovery_severe_exits.clear()
                health.recovery_consecutive_losses = 0
                health.last_auto_demote_at = now
                state = "cooldown"
            else:
                promotion_ready = (
                    health.canary_active
                    and recovery_trade_count >= 10
                    and recovery_avg_pnl_pct is not None
                    and float(recovery_avg_pnl_pct) >= 1.0
                    and int(health.recovery_consecutive_losses) <= 2
                    and not any(recovery_severe[-10:])
                )
                if promotion_ready:
                    health.canary_active = False
                    health.recovery_armed = False
                    disable_reason = None
                    health.last_disable_reason = None
                    state = "normal"
                elif health.canary_active:
                    state = "recovery"
                elif (
                    enough_trades
                    and avg_pnl_pct is not None
                    and avg_pnl_pct < float(getattr(CFG, "REGIME_HEALTH_RECOVERY_EXPECTANCY_PCT", 1.0) or 1.0)
                ):
                    state = "recovery"
                else:
                    disable_reason = None
                    health.last_disable_reason = None

    return {
        "state": state,
        "trade_count": trade_count,
        "avg_pnl_pct": avg_pnl_pct,
        "short_avg_pnl_pct": short_avg_pnl_pct,
        "win_rate": win_rate,
        "exec_rate": exec_rate,
        "price_rate": price_rate,
        "consecutive_losses": int(health.consecutive_losses),
        "cooldown_until": health.cooldown_until,
        "disable_reason": disable_reason,
        "severe_exit_count": int(severe_exit_count),
        "liq_crush_count": int(liq_crush_count),
        "recovery_trade_count": int(recovery_trade_count),
        "recovery_avg_pnl_pct": recovery_avg_pnl_pct,
        "recovery_win_rate": recovery_win_rate,
        "recovery_ready": bool(recovery_ready),
        "recovery_basis": recovery_basis,
        "recovery_signal": {**recovery_signal, "recent_override": recent_recovery_signal},
        "shadow_recovery_signal": shadow_recovery_signal,
        "current_gate_rebased": bool(
            _normalize_regime(regime) == "pump_early"
            and bool(getattr(CFG, "PUMP_EARLY_PROFIT_HEALTH_REBASE_CURRENT_GATE", True))
        ),
        "canary_active": bool(health.canary_active),
        "last_auto_demote_at": health.last_auto_demote_at,
        "last_auto_recover_at": health.last_auto_recover_at,
    }


def _cleanup_candidates(now: dt.datetime | None = None) -> None:
    now = now or utc_now()
    ttl = dt.timedelta(minutes=_CANDIDATE_TTL_MIN)
    stale = [
        address
        for address, state in _CANDIDATES.items()
        if now - state.last_seen > ttl
    ]
    for address in stale:
        _CANDIDATES.pop(address, None)


def clear_candidate(address: str) -> None:
    _CANDIDATES.pop(str(address), None)


def evaluate_candidate(
    token: dict[str, Any],
    *,
    regime: str | None = None,
    has_route: bool | None = None,
    now: dt.datetime | None = None,
) -> StrategyDecision:
    now = now or utc_now()
    _cleanup_candidates(now)

    address = str(token.get("address") or "").strip()
    resolved_regime = _normalize_regime(regime or token.get("entry_regime") or token.get("discovered_via"))
    policy = _policy_for_regime(resolved_regime)
    requested_mode = str(policy["mode"])
    health = _health_snapshot(resolved_regime, now)
    effective_mode = requested_mode
    effective_execution_state = "shadow" if requested_mode != "off" else "off"
    size_cap_multiplier: float | None = None
    mode_override_reason: str | None = None
    paper_continue = _sniper_paper_continue_on_health(resolved_regime) or _paper_sniper_continue_on_health(
        token,
        resolved_regime,
    )
    paper_aggressive_continue = _paper_aggressive_enabled()
    live_aggressive_continue = _live_aggressive_continue_on_health(resolved_regime)
    bucket_disable_reason = _bucket_disable_reason(token, resolved_regime, now)

    if requested_mode == "live":
        scorecard_signal = _scorecard_regime_signal(resolved_regime, now)
        demote_avg_pnl_pct = float(
            getattr(CFG, "STRATEGY_SCORECARD_DEMOTE_MAX_AVG_PNL_PCT", -1.0) or -1.0
        )
        if bucket_disable_reason:
            mode_override_reason = bucket_disable_reason
            effective_mode = "shadow"
            effective_execution_state = "shadow"
        elif scorecard_signal and float(scorecard_signal["avg_pnl_pct"]) <= demote_avg_pnl_pct:
            mode_override_reason = "scorecard_negative"
            if paper_aggressive_continue:
                effective_mode = "live"
                effective_execution_state = "paper_aggressive"
                mode_override_reason = "paper_aggressive_scorecard_negative"
            elif paper_continue:
                effective_mode = "live"
                effective_execution_state = "paper_recovery"
                size_cap_multiplier = _sniper_paper_recovery_cap()
                mode_override_reason = "paper_scorecard_negative"
            elif live_aggressive_continue:
                effective_mode = "live"
                effective_execution_state = "live_aggressive_recovery"
                size_cap_multiplier = _live_aggressive_recovery_cap()
                mode_override_reason = "live_aggressive_scorecard_negative"
            else:
                effective_mode = "shadow"
                effective_execution_state = "shadow"
        elif health["state"] == "cooldown":
            mode_override_reason = str(health.get("disable_reason") or "cooldown")
            if paper_aggressive_continue:
                effective_mode = "live"
                effective_execution_state = "paper_aggressive"
                mode_override_reason = f"paper_aggressive_{mode_override_reason}"
            elif paper_continue:
                effective_mode = "live"
                effective_execution_state = "paper_recovery"
                size_cap_multiplier = _sniper_paper_recovery_cap()
                mode_override_reason = f"paper_{mode_override_reason}"
            elif live_aggressive_continue:
                effective_mode = "live"
                effective_execution_state = "live_aggressive_recovery"
                size_cap_multiplier = _live_aggressive_recovery_cap()
                mode_override_reason = f"live_aggressive_{mode_override_reason}"
            else:
                effective_mode = "shadow"
                effective_execution_state = "shadow"
        elif health["state"] == "shadow_wait":
            mode_override_reason = "recovery_not_ready"
            if paper_aggressive_continue:
                effective_mode = "live"
                effective_execution_state = "paper_aggressive"
                mode_override_reason = "paper_aggressive_recovery_not_ready"
            elif paper_continue:
                effective_mode = "live"
                effective_execution_state = "paper_recovery"
                size_cap_multiplier = _sniper_paper_recovery_cap()
                mode_override_reason = "paper_recovery_not_ready"
            elif live_aggressive_continue:
                effective_mode = "live"
                effective_execution_state = "live_aggressive_recovery"
                size_cap_multiplier = _live_aggressive_recovery_cap()
                mode_override_reason = "live_aggressive_recovery_not_ready"
            else:
                effective_mode = "shadow"
                effective_execution_state = "shadow"
        elif health["state"] == "recovery":
            effective_mode = "live"
            if paper_continue:
                effective_execution_state = "paper_recovery"
                size_cap_multiplier = _sniper_paper_recovery_cap()
            else:
                effective_execution_state = "recovery"
                size_cap_multiplier = min(
                    float(policy["recovery_cap"]),
                    float(getattr(CFG, "SIZE_MIN_MULTIPLIER", 0.10) or 0.10),
                    float(getattr(CFG, "REGIME_RECOVERY_MAX_SIZE_MULTIPLIER", 0.10) or 0.10),
                )
        else:
            effective_mode = "live"
            effective_execution_state = "live"
            if _sniper_enabled(resolved_regime) and not bool(getattr(CFG, "DRY_RUN", False)):
                initial_closes = int(getattr(CFG, "PUMP_EARLY_SNIPER_CANARY_INITIAL_CLOSES", 10) or 10)
                if int(health.get("trade_count") or 0) < max(1, initial_closes):
                    effective_execution_state = "live_canary"
                    size_cap_multiplier = float(
                        getattr(CFG, "PUMP_EARLY_SNIPER_CANARY_INITIAL_SIZE_CAP", 0.20) or 0.20
                    )
    elif requested_mode == "shadow":
        effective_mode = "shadow"
        effective_execution_state = "shadow"

    if effective_mode == "off":
        clear_candidate(address)
        return StrategyDecision(
            regime=resolved_regime,
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            effective_execution_state="off",
            health_state=str(health["state"]),
            action="off",
            reason="regime_off",
            confirmations=0,
            confirmations_required=max(1, int(policy["confirmations"])),
            requeue_backoff_s=max(10, int(policy["backoff_s"])),
            size_cap_multiplier=None,
        )

    confirmations_required = max(
        1,
        int(policy["confirmations"])
        if bool(getattr(CFG, "STRATEGY_CONFIRMATION_ENABLED", True))
        else int(getattr(CFG, "STRATEGY_CONFIRM_DEFAULT_SNAPSHOTS", 1) or 1),
    )
    backoff_s = max(10, int(policy["backoff_s"]))
    if resolved_regime == "pump_early" and _sniper_fast_confirm(token, has_route):
        confirmations_required = 1
        backoff_s = max(5, int(getattr(CFG, "PUMP_EARLY_SNIPER_FAST_CONFIRM_BACKOFF_S", 10) or 10))
    min_age_min = max(0.0, float(policy["min_age_min"]))
    age_min = _to_float(token.get("age_min")) or 0.0
    liquidity_usd = _to_float(token.get("liquidity_usd"))

    if age_min < min_age_min:
        state = _CANDIDATES.get(address)
        confirmations = int(state.confirmations if state else 0)
        return StrategyDecision(
            regime=resolved_regime,
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            effective_execution_state=effective_execution_state,
            health_state=str(health["state"]),
            action="wait",
            reason="confirm_age",
            confirmations=confirmations,
            confirmations_required=confirmations_required,
            requeue_backoff_s=backoff_s,
            size_cap_multiplier=size_cap_multiplier,
        )

    route_required = bool(getattr(CFG, "STRATEGY_CONFIRM_REQUIRE_ROUTE", True))
    if route_required and has_route is False:
        clear_candidate(address)
        return StrategyDecision(
            regime=resolved_regime,
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            effective_execution_state=effective_execution_state,
            health_state=str(health["state"]),
            action="wait",
            reason="confirm_route",
            confirmations=0,
            confirmations_required=confirmations_required,
            requeue_backoff_s=backoff_s,
            size_cap_multiplier=size_cap_multiplier,
        )

    if confirmations_required <= 1:
        clear_candidate(address)
        return StrategyDecision(
            regime=resolved_regime,
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            effective_execution_state=effective_execution_state,
            health_state=str(health["state"]),
            action="shadow" if effective_mode == "shadow" else "live",
            reason=str(mode_override_reason or "confirm_ok"),
            confirmations=1,
            confirmations_required=1,
            requeue_backoff_s=backoff_s,
            size_cap_multiplier=size_cap_multiplier,
        )

    state = _CANDIDATES.get(address)
    if state is None or state.regime != resolved_regime:
        state = CandidateState(
            address=address,
            regime=resolved_regime,
            first_seen=now,
            last_seen=now,
            confirmations=1,
            last_liquidity_usd=liquidity_usd,
            last_has_route=has_route,
            last_age_min=age_min,
        )
        _CANDIDATES[address] = state
    else:
        stable = True
        if has_route is False:
            stable = False
        if state.last_age_min is not None and age_min + 1e-9 < state.last_age_min:
            stable = False
        if (
            liquidity_usd is not None
            and state.last_liquidity_usd is not None
            and state.last_liquidity_usd > 0
        ):
            min_liq = state.last_liquidity_usd * (
                1.0 - float(getattr(CFG, "STRATEGY_CONFIRM_LIQUIDITY_DROP_PCT", 20.0) or 20.0) / 100.0
            )
            if liquidity_usd < min_liq:
                stable = False

        if not stable:
            state.confirmations = 1
        else:
            gap_s = max(_MIN_CONFIRM_GAP_S, min(backoff_s, 30))
            if (now - state.last_seen).total_seconds() >= gap_s:
                state.confirmations += 1

        state.last_seen = now
        state.last_liquidity_usd = liquidity_usd
        state.last_has_route = has_route
        state.last_age_min = age_min

    if state.confirmations < confirmations_required:
        return StrategyDecision(
            regime=resolved_regime,
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            effective_execution_state=effective_execution_state,
            health_state=str(health["state"]),
            action="wait",
            reason="confirm_snapshots",
            confirmations=int(state.confirmations),
            confirmations_required=confirmations_required,
            requeue_backoff_s=backoff_s,
            size_cap_multiplier=size_cap_multiplier,
        )

    clear_candidate(address)
    return StrategyDecision(
        regime=resolved_regime,
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        effective_execution_state=effective_execution_state,
        health_state=str(health["state"]),
        action="shadow" if effective_mode == "shadow" else "live",
        reason=str(mode_override_reason or "confirm_ok"),
        confirmations=int(state.confirmations),
        confirmations_required=confirmations_required,
        requeue_backoff_s=backoff_s,
        size_cap_multiplier=size_cap_multiplier,
    )


def record_trade_close(
    regime: str,
    total_pnl_pct: float | None,
    *,
    exit_reason: str | None = None,
    execution_state: str | None = None,
    entry_lane: str | None = None,
    dex_id: str | None = None,
    liquidity_proxy_flag: bool | int | None = None,
    mcap_bucket: str | None = None,
    price5m_bucket: str | None = None,
    gate_profile: str | None = None,
) -> None:
    if total_pnl_pct is None:
        return
    resolved_regime = _normalize_regime(regime)
    pnl_pct = float(total_pnl_pct)
    _record_bucket_close(
        regime=resolved_regime,
        pnl_pct=pnl_pct,
        exit_reason=exit_reason,
        entry_lane=entry_lane,
        dex_id=dex_id,
        liquidity_proxy_flag=liquidity_proxy_flag,
        mcap_bucket=mcap_bucket,
        price5m_bucket=price5m_bucket,
        gate_profile=gate_profile,
    )
    if (
        resolved_regime == "pump_early"
        and bool(getattr(CFG, "PUMP_EARLY_BREAKOUT_HEALTH_ISOLATED", True))
        and _is_breakout_lane(entry_lane=entry_lane, gate_profile=gate_profile)
    ):
        return

    health = _HEALTH[resolved_regime]
    health.trade_pnls_pct.append(pnl_pct)
    won = pnl_pct > 0.0
    health.trade_wins.append(won)
    health.consecutive_losses = 0 if won else (health.consecutive_losses + 1)
    severe_exit = _is_severe_exit(exit_reason, pnl_pct)
    liq_crush_exit = str(exit_reason or "").strip().upper() == "LIQUIDITY_CRUSH"
    health.severe_exits.append(bool(severe_exit))
    health.liq_crush_exits.append(bool(liq_crush_exit))

    if str(execution_state or "").strip().lower() in {"recovery", "paper_recovery"}:
        health.recovery_trade_pnls_pct.append(pnl_pct)
        health.recovery_trade_wins.append(won)
        health.recovery_severe_exits.append(bool(severe_exit))
        health.recovery_liq_crush_exits.append(bool(liq_crush_exit))
        health.recovery_consecutive_losses = 0 if won else (health.recovery_consecutive_losses + 1)


def record_execution(regime: str, ok: bool) -> None:
    health = _HEALTH[_normalize_regime(regime)]
    health.exec_success.append(bool(ok))


def record_monitor_coverage(regime: str, has_price: bool) -> None:
    health = _HEALTH[_normalize_regime(regime)]
    health.price_coverage.append(bool(has_price))


def bootstrap_closed_trades(rows: list[tuple[Any, ...]]) -> None:
    for health in _HEALTH.values():
        health.trade_pnls_pct.clear()
        health.trade_wins.clear()
        health.severe_exits.clear()
        health.liq_crush_exits.clear()
        health.recovery_trade_pnls_pct.clear()
        health.recovery_trade_wins.clear()
        health.recovery_severe_exits.clear()
        health.recovery_liq_crush_exits.clear()
        health.consecutive_losses = 0
        health.recovery_consecutive_losses = 0
        health.cooldown_until = None
        health.last_disable_reason = None
        health.recovery_armed = False
        health.canary_active = False
        health.last_auto_demote_at = None
        health.last_auto_recover_at = None
    _BUCKET_HEALTH.clear()

    ordered = sorted(
        rows,
        key=lambda row: row[2] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
    )
    for row in ordered:
        regime = row[0] if len(row) > 0 else "dex_mature"
        total_pnl_pct = row[1] if len(row) > 1 else None
        exit_reason = row[3] if len(row) > 3 else None
        size_bucket = row[4] if len(row) > 4 else None
        entry_lane = row[5] if len(row) > 5 else None
        dex_id = row[6] if len(row) > 6 else None
        liquidity_proxy_flag = row[7] if len(row) > 7 else None
        mcap_bucket = row[8] if len(row) > 8 else None
        price5m_bucket = row[9] if len(row) > 9 else None
        gate_profile = row[10] if len(row) > 10 else None
        execution_state = "recovery" if str(size_bucket or "").strip().lower() == "recovery" else None
        bucket = str(size_bucket or "").strip().lower()
        entry_lane = entry_lane or (
            "pump_early_pumpswap_breakout_probe"
            if bucket == "pumpswap_breakout"
            else
            "pump_early_pumpswap_profit"
            if bucket in {"pumpswap_profit", "pumpswap_prime", "pumpswap_meteor"}
            else None
        )
        gate_profile = gate_profile or (
            "pumpswap_breakout_probe" if bucket == "pumpswap_breakout" else
            "pumpswap_meteor_prime" if bucket == "pumpswap_meteor" else
            "pumpswap_profit_prime" if bucket == "pumpswap_prime" else "pumpswap_profit_broad" if bucket == "pumpswap_profit" else None
        )
        record_trade_close(
            regime,
            total_pnl_pct,
            exit_reason=exit_reason,
            execution_state=execution_state,
            entry_lane=entry_lane,
            dex_id=dex_id,
            liquidity_proxy_flag=liquidity_proxy_flag,
            mcap_bucket=mcap_bucket,
            price5m_bucket=price5m_bucket,
            gate_profile=gate_profile,
        )


def describe_strategy_policy() -> dict[str, Any]:
    return {
        regime: _policy_for_regime(regime)
        for regime in ("pump_early", "dex_mature", "revival")
    }


def describe_bucket_health(now: dt.datetime | None = None) -> dict[str, dict[str, Any]]:
    now = now or utc_now()
    out: dict[str, dict[str, Any]] = {}
    for key, health in _BUCKET_HEALTH.items():
        pnls = list(health.trade_pnls_pct)
        trade_count = len(pnls)
        avg_pnl_pct = (sum(pnls) / trade_count) if trade_count else None
        parts = key.split("|")
        cooldown_active = bool(health.cooldown_until and now < health.cooldown_until)
        out[key] = {
            "entry_lane": parts[0] if len(parts) > 0 else None,
            "gate_profile": parts[1] if len(parts) > 1 else None,
            "dex_id": parts[2] if len(parts) > 2 else None,
            "liquidity_proxy_flag": parts[3] if len(parts) > 3 else None,
            "mcap_bucket": parts[4] if len(parts) > 4 else None,
            "price5m_bucket": parts[5] if len(parts) > 5 else None,
            "trade_count": trade_count,
            "avg_pnl_pct": avg_pnl_pct,
            "consecutive_losses": int(health.consecutive_losses),
            "severe_exit_count": sum(1 for flag in health.severe_exits if flag),
            "cooldown_until": health.cooldown_until,
            "blocked": cooldown_active,
            "last_disable_reason": health.last_disable_reason,
            "last_auto_demote_at": health.last_auto_demote_at,
        }
    return out


def describe_regime_health(now: dt.datetime | None = None) -> dict[str, dict[str, Any]]:
    now = now or utc_now()
    out: dict[str, dict[str, Any]] = {}
    for regime in ("pump_early", "dex_mature", "revival"):
        snap = _health_snapshot(regime, now)
        policy = _policy_for_regime(regime)
        requested_mode = str(policy["mode"])
        if requested_mode == "off":
            effective_execution_state = "off"
        elif requested_mode != "live":
            effective_execution_state = "shadow"
        elif _paper_aggressive_enabled() and snap["state"] in {"cooldown", "shadow_wait", "recovery"}:
            effective_execution_state = "paper_aggressive"
        elif _sniper_paper_continue_on_health(regime) and snap["state"] in {"cooldown", "shadow_wait", "recovery"}:
            effective_execution_state = "paper_recovery"
        elif _live_aggressive_continue_on_health(regime) and snap["state"] in {"cooldown", "shadow_wait", "recovery"}:
            effective_execution_state = "live_aggressive_recovery"
        elif snap["state"] == "recovery":
            effective_execution_state = "recovery"
        elif snap["state"] in {"cooldown", "shadow_wait"}:
            effective_execution_state = "shadow"
        elif (
            _sniper_enabled(regime)
            and not bool(getattr(CFG, "DRY_RUN", False))
            and int(snap.get("trade_count") or 0) < int(getattr(CFG, "PUMP_EARLY_SNIPER_CANARY_INITIAL_CLOSES", 10) or 10)
        ):
            effective_execution_state = "live_canary"
        else:
            effective_execution_state = "live"
        out[regime] = {
            "requested_mode": requested_mode,
            "effective_execution_state": effective_execution_state,
            "health_state": snap["state"],
            "trade_count": snap["trade_count"],
            "avg_pnl_pct": snap["avg_pnl_pct"],
            "short_avg_pnl_pct": snap.get("short_avg_pnl_pct"),
            "win_rate": snap["win_rate"],
            "exec_rate": snap["exec_rate"],
            "price_rate": snap["price_rate"],
            "consecutive_losses": snap["consecutive_losses"],
            "cooldown_until": snap["cooldown_until"],
            "disable_reason": snap["disable_reason"],
            "last_disable_reason": snap.get("disable_reason"),
            "size_cap_multiplier": (
                _sniper_paper_recovery_cap()
                if effective_execution_state == "paper_recovery"
                else float(policy["recovery_cap"])
                if effective_execution_state == "recovery"
                else (
                    float(getattr(CFG, "PUMP_EARLY_SNIPER_CANARY_INITIAL_SIZE_CAP", 0.20) or 0.20)
                    if effective_execution_state == "live_canary"
                    else None
                )
            ),
            "severe_exit_count": snap.get("severe_exit_count"),
            "liq_crush_count": snap.get("liq_crush_count"),
            "recovery_trade_count": snap.get("recovery_trade_count"),
            "recovery_avg_pnl_pct": snap.get("recovery_avg_pnl_pct"),
            "recovery_ready": snap.get("recovery_ready"),
            "recovery_basis": snap.get("recovery_basis") or {},
            "shadow_recovery_signal": snap.get("shadow_recovery_signal") or {},
            "current_gate_rebased": bool(snap.get("current_gate_rebased")),
            "last_auto_demote_at": snap.get("last_auto_demote_at"),
            "last_auto_recover_at": snap.get("last_auto_recover_at"),
        }
    return out


__all__ = [
    "StrategyDecision",
    "bootstrap_closed_trades",
    "clear_candidate",
    "describe_bucket_health",
    "describe_regime_health",
    "describe_strategy_policy",
    "evaluate_candidate",
    "record_execution",
    "record_monitor_coverage",
    "record_trade_close",
]
