from __future__ import annotations

import collections
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analytics.report_utils import (
    address_of,
    boolish,
    fnum,
    load_candidate_outcomes,
    load_deduped_positions,
    load_runtime_events,
    metrics_dir,
    write_json,
)
from config.config import CFG, PROJECT_ROOT
from ml.lane_taxonomy import LANE_PAPER_BOOTSTRAP_MICRO


POLICY_PAPER_BOOTSTRAP = "paper_bootstrap"
REPORT_JSON = "paper_bootstrap_report.json"


@dataclass(frozen=True)
class PaperBootstrapDecision:
    allowed: bool
    reason: str
    amount_sol: float
    lane: str = LANE_PAPER_BOOTSTRAP_MICRO
    trigger_stage: str = ""
    trigger_reason: str = ""
    hard_failures: tuple[str, ...] = ()
    risk_notes: tuple[str, ...] = ()
    model_cold: bool = False


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _int_cfg(cfg: Any, name: str, default: int) -> int:
    value = getattr(cfg, name, default)
    if value in (None, ""):
        return int(default)
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _float_cfg(cfg: Any, name: str, default: float) -> float:
    value = getattr(cfg, name, default)
    if value in (None, ""):
        return float(default)
    try:
        return float(value)
    except Exception:
        return float(default)


def _bool_cfg(cfg: Any, name: str, default: bool) -> bool:
    value = getattr(cfg, name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _cap_reached(count: int, cap: int) -> bool:
    return cap > 0 and count >= cap


def _is_solana_address(address: Any) -> bool:
    raw = str(address or "").strip()
    return bool(raw) and not raw.startswith("0x") and 30 <= len(raw) <= 50


def _activity_score(row: dict[str, Any]) -> float:
    return max(
        fnum(_first(row, "liquidity_usd", "buy_liquidity_usd"), 0.0),
        fnum(_first(row, "volume_24h_usd", "buy_volume_24h_usd"), 0.0),
        fnum(_first(row, "txns_last_5m", "txns_5m", "buy_txns_last_5m"), 0.0),
        abs(fnum(_first(row, "price_pct_5m", "buy_price_pct_5m"), 0.0)),
    )


def _hard_failures(row: dict[str, Any], *, cfg: Any) -> tuple[str, ...]:
    failures: list[str] = []
    address = _first(row, "address", "token_address", "mint", "baseMint")
    chain = _norm(_first(row, "chainId", "chain", "chainIdShort"))
    reason_text = " ".join(
        _norm(_first(row, key))
        for key in (
            "reason",
            "green_sniper_reason",
            "entry_reason",
            "reject_reason",
            "blocked_reason",
            "sniper_research_subprofile_reason",
        )
    )

    if chain and chain not in {"solana", "sol"}:
        failures.append("non_solana_chain")
    if not _is_solana_address(address):
        failures.append("invalid_solana_address")
    if "banned_creator" in reason_text or boolish(_first(row, "banned_creator"), False):
        failures.append("banned_creator")
    if "toxic_initial_sell_pressure" in reason_text or boolish(
        _first(row, "toxic_initial_sell_pressure"),
        False,
    ):
        failures.append("toxic_initial_sell_pressure")
    if _bool_cfg(cfg, "PAPER_BOOTSTRAP_BLOCK_CLUSTER_BAD", False) and (
        "cluster_bad" in reason_text or boolish(_first(row, "cluster_bad", "helius_cluster_bad"), False)
    ):
        failures.append("cluster_bad")
    if _activity_score(row) <= 0.0:
        failures.append("no_activity_signal")

    max_impact = _float_cfg(cfg, "PAPER_BOOTSTRAP_MAX_PRICE_IMPACT_PCT", 40.0)
    price_impact = fnum(_first(row, "price_impact_pct", "buy_price_impact_pct"), 0.0)
    if max_impact > 0 and price_impact > max_impact:
        failures.append("price_impact_too_high")

    max_rug = _float_cfg(cfg, "PAPER_BOOTSTRAP_MAX_RUG_SCORE", 95.0)
    rug_score = fnum(_first(row, "rug_score"), -1.0)
    if max_rug > 0 and rug_score >= max_rug:
        failures.append("rug_score_too_high")

    return tuple(dict.fromkeys(failures))


def _risk_notes(row: dict[str, Any]) -> tuple[str, ...]:
    notes: list[str] = []
    reason_text = " ".join(
        _norm(_first(row, key))
        for key in ("reason", "green_sniper_reason", "entry_reason", "blocked_reason")
    )
    if "cluster_bad" in reason_text or boolish(_first(row, "cluster_bad", "helius_cluster_bad"), False):
        notes.append("cluster_bad_allowed_for_paper")
    if boolish(_first(row, "liquidity_is_proxy", "liquidity_usd_is_proxy", "buy_liquidity_is_proxy"), False):
        notes.append("proxy_liquidity_allowed_for_paper")
    if fnum(_first(row, "price_usd", "buy_price_usd"), 0.0) <= 0.0:
        notes.append("missing_price_bootstrap")
    return tuple(dict.fromkeys(notes))


def _field_present(row: dict[str, Any], *keys: str) -> bool:
    return _first(row, *keys) is not None


def _quality_failures(row: dict[str, Any], *, cfg: Any) -> tuple[str, ...]:
    if not _bool_cfg(cfg, "PAPER_BOOTSTRAP_QUALITY_GATES_ENABLED", True):
        return ()

    failures: list[str] = []
    if _bool_cfg(cfg, "PAPER_BOOTSTRAP_REQUIRE_ROUTE", True):
        route_value = _first(row, "has_jupiter_route", "route_ok", "route_available")
        if route_value is not None and not boolish(route_value, False):
            failures.append("no_jupiter_route")

    if _bool_cfg(cfg, "PAPER_BOOTSTRAP_REQUIRE_REAL_LIQUIDITY", True) and boolish(
        _first(row, "liquidity_is_proxy", "liquidity_usd_is_proxy", "buy_liquidity_is_proxy"),
        False,
    ):
        failures.append("proxy_liquidity")

    min_liq = _float_cfg(cfg, "PAPER_BOOTSTRAP_MIN_LIQUIDITY_USD", 1_500.0)
    if min_liq > 0 and fnum(_first(row, "liquidity_usd", "buy_liquidity_usd"), 0.0) < min_liq:
        failures.append("liquidity_below_min")

    min_mcap = _float_cfg(cfg, "PAPER_BOOTSTRAP_MIN_MARKET_CAP_USD", 2_000.0)
    if min_mcap > 0 and fnum(_first(row, "market_cap_usd", "buy_market_cap_usd", "mcap"), 0.0) < min_mcap:
        failures.append("mcap_below_min")

    min_txns = _int_cfg(cfg, "PAPER_BOOTSTRAP_MIN_TXNS_5M", 25)
    if min_txns > 0 and fnum(_first(row, "txns_last_5m", "txns_5m", "buy_txns_last_5m"), 0.0) < min_txns:
        failures.append("txns5m_below_min")

    min_score = _int_cfg(cfg, "PAPER_BOOTSTRAP_MIN_SCORE_TOTAL", 30)
    if min_score > 0 and fnum(_first(row, "score_total", "entry_score_total"), 0.0) < min_score:
        failures.append("score_below_min")

    max_missing = _int_cfg(cfg, "PAPER_BOOTSTRAP_MAX_SNAPSHOT_MISSING_FIELDS", 2)
    if max_missing >= 0:
        missing = 0
        for keys in (
            ("price_usd", "buy_price_usd"),
            ("liquidity_usd", "buy_liquidity_usd"),
            ("market_cap_usd", "buy_market_cap_usd", "mcap"),
            ("txns_last_5m", "txns_5m", "buy_txns_last_5m"),
        ):
            if not _field_present(row, *keys):
                missing += 1
        if missing > max_missing:
            failures.append(f"snapshot_missing>{max_missing}")

    return tuple(dict.fromkeys(failures))


def should_allow_paper_bootstrap(
    row: dict[str, Any],
    *,
    dry_run: bool,
    live: bool,
    open_count: int,
    daily_buys: int,
    hourly_buys: int,
    seconds_since_last_buy: float,
    closed_trades: int,
    model_loaded: bool,
    model_rows: int,
    trigger_stage: str,
    trigger_reason: str,
    cfg: Any = CFG,
) -> PaperBootstrapDecision:
    amount = min(
        max(0.0, _float_cfg(cfg, "PAPER_BOOTSTRAP_AMOUNT_SOL", 0.1)),
        max(0.0001, _float_cfg(cfg, "PAPER_BOOTSTRAP_MAX_AMOUNT_SOL", 0.1)),
    )
    model_rows_ready = int(model_rows or 0) >= _int_cfg(cfg, "PAPER_BOOTSTRAP_MODEL_ROWS_READY", 200)
    model_ready = bool(model_loaded) and model_rows_ready
    historical_data_ready = int(closed_trades or 0) >= _int_cfg(cfg, "PAPER_BOOTSTRAP_MIN_CLOSED_TRADES_READY", 50)
    model_cold = not (model_ready or model_rows_ready or historical_data_ready)

    def decision(
        allowed: bool,
        reason: str,
        *,
        hard_failures: tuple[str, ...] = (),
        risk_notes: tuple[str, ...] = (),
    ) -> PaperBootstrapDecision:
        return PaperBootstrapDecision(
            bool(allowed),
            str(reason),
            amount,
            trigger_stage=str(trigger_stage or ""),
            trigger_reason=str(trigger_reason or ""),
            hard_failures=hard_failures,
            risk_notes=risk_notes,
            model_cold=bool(model_cold),
        )

    if not _bool_cfg(cfg, "PAPER_BOOTSTRAP_ENABLED", True):
        return decision(False, "paper_bootstrap_disabled")
    if live or not dry_run:
        return decision(False, "paper_bootstrap_paper_only")
    if _bool_cfg(cfg, "PAPER_BOOTSTRAP_REQUIRE_COLD_START", False) and not model_cold:
        return decision(False, "paper_bootstrap_cold_start_complete")

    failures = _hard_failures(row, cfg=cfg) + _quality_failures(row, cfg=cfg)
    if failures:
        return decision(False, "paper_bootstrap_hard_risk", hard_failures=failures)

    if _cap_reached(open_count, _int_cfg(cfg, "PAPER_BOOTSTRAP_MAX_OPEN", 0)):
        return decision(False, "paper_bootstrap_open_cap")
    if _cap_reached(daily_buys, _int_cfg(cfg, "PAPER_BOOTSTRAP_MAX_DAILY_BUYS", 0)):
        return decision(False, "paper_bootstrap_daily_cap")
    if _cap_reached(hourly_buys, _int_cfg(cfg, "PAPER_BOOTSTRAP_MAX_HOURLY_BUYS", 0)):
        return decision(False, "paper_bootstrap_hourly_cap")
    cooldown = _float_cfg(cfg, "PAPER_BOOTSTRAP_MIN_SECONDS_BETWEEN_BUYS", 0.0)
    if cooldown > 0 and seconds_since_last_buy < cooldown:
        return decision(False, "paper_bootstrap_cooldown")

    return decision(True, POLICY_PAPER_BOOTSTRAP, risk_notes=_risk_notes(row))


def apply_paper_bootstrap_context(row: dict[str, Any], decision: PaperBootstrapDecision) -> dict[str, Any]:
    row["entry_lane"] = decision.lane
    row["gate_profile"] = POLICY_PAPER_BOOTSTRAP
    row["sniper_gate_profile"] = POLICY_PAPER_BOOTSTRAP
    row["live_profit_gate_profile"] = POLICY_PAPER_BOOTSTRAP
    row["profit_lane_tier"] = decision.lane
    row["lane_policy_category"] = POLICY_PAPER_BOOTSTRAP
    row["paper_bootstrap"] = 1
    row["paper_bootstrap_reason"] = decision.reason
    row["paper_bootstrap_trigger_stage"] = decision.trigger_stage
    row["paper_bootstrap_trigger_reason"] = decision.trigger_reason
    row["paper_bootstrap_amount_sol"] = float(decision.amount_sol)
    row["paper_bootstrap_model_cold"] = int(bool(decision.model_cold))
    row["paper_bootstrap_risk_notes"] = ",".join(decision.risk_notes)
    row["require_jupiter_for_buy"] = int(_bool_cfg(CFG, "PAPER_BOOTSTRAP_REQUIRE_ROUTE", True))
    row["green_sniper_reason"] = POLICY_PAPER_BOOTSTRAP
    row["entry_reason"] = POLICY_PAPER_BOOTSTRAP
    row["sniper_research_subprofile_reason"] = POLICY_PAPER_BOOTSTRAP
    row["live_profit_gate_failed_count"] = 0
    row["live_profit_gate_failures"] = ""
    row["runner_exit_profile"] = POLICY_PAPER_BOOTSTRAP
    return row


def _event(row: dict[str, Any]) -> str:
    return _norm(_first(row, "event_type", "event", "action", "decision_action"))


def _lane(row: dict[str, Any]) -> str:
    return _norm(_first(row, "entry_lane", "lane", "profit_lane_tier"))


def _reason(row: dict[str, Any]) -> str:
    return _norm(
        _first(
            row,
            "reason",
            "paper_bootstrap_reason",
            "entry_reason",
            "blocked_reason",
            "reject_reason",
        )
    )


def _is_bootstrap(row: dict[str, Any]) -> bool:
    return (
        boolish(row.get("paper_bootstrap"), False)
        or _lane(row) == LANE_PAPER_BOOTSTRAP_MICRO
        or POLICY_PAPER_BOOTSTRAP in _reason(row)
        or _norm(_first(row, "gate_profile", "sniper_gate_profile")) == POLICY_PAPER_BOOTSTRAP
    )


def _position_opened(row: dict[str, Any]) -> bool:
    return _first(row, "opened_at", "entry_price_usd", "buy_price_usd", "buy_amount_sol") is not None


def _unique_count(rows: list[dict[str, Any]]) -> int:
    addresses = {address_of(row) for row in rows if address_of(row)}
    return len(addresses) if addresses else len(rows)


def build_paper_bootstrap_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    runtime_rows = load_runtime_events(root)
    outcome_rows = load_candidate_outcomes(root)
    position_rows = load_deduped_positions(root)
    rows = runtime_rows + outcome_rows + position_rows
    bootstrap_rows = [row for row in rows if _is_bootstrap(row)]
    attempts = [row for row in runtime_rows if _event(row) == "actual_paper_buy_attempt" and _is_bootstrap(row)]
    actual_buy_events = [row for row in runtime_rows if _event(row) == "actual_paper_buy" and _is_bootstrap(row)]
    legacy_buy_events = [
        row for row in runtime_rows if _event(row) in {"buy", "bought", "paper_buy", "buy_ok"} and _is_bootstrap(row)
    ]
    position_buys = [row for row in position_rows if _is_bootstrap(row) and _position_opened(row)]
    buy_count = max(_unique_count(actual_buy_events), _unique_count(legacy_buy_events), _unique_count(position_buys))
    blocked = [row for row in rows if _event(row) in {"paper_bootstrap_blocked", "blocked_before_buy"} and _is_bootstrap(row)]
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "enabled": _bool_cfg(CFG, "PAPER_BOOTSTRAP_ENABLED", True),
            "amount_sol": _float_cfg(CFG, "PAPER_BOOTSTRAP_AMOUNT_SOL", 0.1),
            "max_open": _int_cfg(CFG, "PAPER_BOOTSTRAP_MAX_OPEN", 0),
            "max_daily_buys": _int_cfg(CFG, "PAPER_BOOTSTRAP_MAX_DAILY_BUYS", 0),
            "max_hourly_buys": _int_cfg(CFG, "PAPER_BOOTSTRAP_MAX_HOURLY_BUYS", 0),
            "require_cold_start": _bool_cfg(CFG, "PAPER_BOOTSTRAP_REQUIRE_COLD_START", False),
            "quality_gates_enabled": _bool_cfg(CFG, "PAPER_BOOTSTRAP_QUALITY_GATES_ENABLED", True),
            "require_route": _bool_cfg(CFG, "PAPER_BOOTSTRAP_REQUIRE_ROUTE", True),
            "require_real_liquidity": _bool_cfg(CFG, "PAPER_BOOTSTRAP_REQUIRE_REAL_LIQUIDITY", True),
            "min_liquidity_usd": _float_cfg(CFG, "PAPER_BOOTSTRAP_MIN_LIQUIDITY_USD", 1_500.0),
            "min_market_cap_usd": _float_cfg(CFG, "PAPER_BOOTSTRAP_MIN_MARKET_CAP_USD", 2_000.0),
            "min_txns_5m": _int_cfg(CFG, "PAPER_BOOTSTRAP_MIN_TXNS_5M", 25),
            "min_score_total": _int_cfg(CFG, "PAPER_BOOTSTRAP_MIN_SCORE_TOTAL", 30),
        },
        "rows": len(bootstrap_rows),
        "actual_paper_buy_attempts": len(attempts),
        "actual_paper_buys": buy_count,
        "blocked_before_buy": len(blocked),
        "blocked_by_reason": dict(collections.Counter(_reason(row) or "unknown" for row in blocked).most_common(20)),
        "risk_notes": dict(
            collections.Counter(
                note
                for row in bootstrap_rows
                for note in str(row.get("paper_bootstrap_risk_notes") or "").split(",")
                if note
            ).most_common(20)
        ),
        "samples": [
            {
                "address": address_of(row),
                "event": _event(row),
                "reason": _reason(row),
                "lane": _lane(row),
            }
            for row in bootstrap_rows[:50]
        ],
    }


def write_paper_bootstrap_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_paper_bootstrap_report(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    return report


__all__ = [
    "POLICY_PAPER_BOOTSTRAP",
    "PaperBootstrapDecision",
    "apply_paper_bootstrap_context",
    "build_paper_bootstrap_report",
    "should_allow_paper_bootstrap",
    "write_paper_bootstrap_report",
]
