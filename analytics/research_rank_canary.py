from __future__ import annotations

import json
import statistics
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.config import CFG, PROJECT_ROOT
from analytics.lane_policy_categories import POLICY_RESEARCH_RANK_CANARY
from analytics.report_utils import (
    fnum,
    is_severe_exit,
    load_candidate_outcomes,
    load_paper_positions,
    load_sqlite_positions,
    metrics_dir,
    write_json,
)
from ml.lane_taxonomy import LANE_RESEARCH_RANK_CANARY, LANE_RESEARCH_SNIPER, normalize_entry_lane


AUDIT_PATH = PROJECT_ROOT / "data" / "metrics" / "research_rank_canary_audit.json"
_AUDIT_LOCK = threading.Lock()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _score_threshold(value: Any, default: float = 0.647) -> float:
    return normalize_score(value, default)[1]


def normalize_score(value: Any, default: float = 0.0) -> tuple[float, float, str]:
    raw = _float(value, default)
    if 0.0 < raw <= 1.0:
        return raw, raw * 100.0, "0_1"
    return raw, raw, "0_100"


def _field_float(token: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = token.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return _float(value, default)
    return default


@dataclass(frozen=True)
class ResearchRankCanaryDecision:
    allowed: bool
    entry_lane: str
    reason: str
    rank_score: float
    min_score: float
    amount_sol: float
    rank_score_raw: float = 0.0
    rank_score_scale: str = "0_100"
    min_score_raw: float = 0.0
    min_score_scale: str = "0_100"
    shadow_as_own_lane: bool = False
    executable: bool = True


def _read_audit() -> dict[str, Any]:
    try:
        payload = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _record_audit(token: dict[str, Any], decision: ResearchRankCanaryDecision, *, dry_run: bool, live: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    reason = str(decision.reason or "unknown")
    address = str(token.get("address") or token.get("token_address") or "")
    payload = {
        "updated_at_utc": now,
        "total_evaluations": 0,
        "evaluated": 0,
        "allowed": 0,
        "rejected": 0,
        "bought_as_own_lane": 0,
        "shadow_as_own_lane": 0,
        "mixed_lane_detected": 0,
        "reasons": {},
        "blocked_by_reason": {},
        "last_decision": {},
        "config": {
            "enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_LIVE_ENABLED", False)),
            "min_score_raw": decision.min_score_raw,
            "min_score_normalized": decision.min_score,
            "min_score_scale": decision.min_score_scale,
            "min_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_PRICE5M", 40.0), 40.0),
            "max_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_PRICE5M", 100.0), 100.0),
            "force_own_lane": bool(getattr(CFG, "RESEARCH_RANK_CANARY_FORCE_OWN_LANE", True)),
            "shadow_if_not_executable": bool(getattr(CFG, "RESEARCH_RANK_CANARY_SHADOW_IF_NOT_EXECUTABLE", True)),
            "require_route_paper": bool(getattr(CFG, "RESEARCH_RANK_CANARY_REQUIRE_ROUTE_PAPER", True)),
        },
    }
    try:
        with _AUDIT_LOCK:
            payload.update(_read_audit())
            payload["updated_at_utc"] = now
            payload["total_evaluations"] = int(payload.get("total_evaluations") or 0) + 1
            payload["evaluated"] = int(payload.get("evaluated") or 0) + 1
            if decision.allowed:
                payload["allowed"] = int(payload.get("allowed") or 0) + 1
            else:
                payload["rejected"] = int(payload.get("rejected") or 0) + 1
            reasons = payload.get("reasons")
            if not isinstance(reasons, dict):
                reasons = {}
            reasons[reason] = int(reasons.get(reason) or 0) + 1
            payload["reasons"] = reasons
            blocked = payload.get("blocked_by_reason")
            if not isinstance(blocked, dict):
                blocked = {}
            if not decision.allowed:
                blocked[reason] = int(blocked.get(reason) or 0) + 1
            payload["blocked_by_reason"] = blocked
            payload["config"] = {
                "enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_ENABLED", True)),
                "paper_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_ENABLED", True)),
                "live_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_LIVE_ENABLED", False)),
                "min_score_raw": decision.min_score_raw,
                "min_score_normalized": decision.min_score,
                "min_score_scale": decision.min_score_scale,
                "min_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_PRICE5M", 40.0), 40.0),
                "max_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_PRICE5M", 100.0), 100.0),
                "force_own_lane": bool(getattr(CFG, "RESEARCH_RANK_CANARY_FORCE_OWN_LANE", True)),
                "shadow_if_not_executable": bool(
                    getattr(CFG, "RESEARCH_RANK_CANARY_SHADOW_IF_NOT_EXECUTABLE", True)
                ),
                "require_route_paper": bool(getattr(CFG, "RESEARCH_RANK_CANARY_REQUIRE_ROUTE_PAPER", True)),
            }
            payload["last_decision"] = {
                "ts_utc": now,
                "address": address,
                "allowed": bool(decision.allowed),
                "reason": reason,
                "dry_run": bool(dry_run),
                "live": bool(live),
                "entry_lane": str(token.get("entry_lane") or ""),
                "rank_score_raw": decision.rank_score_raw,
                "rank_score_normalized": decision.rank_score,
                "rank_score_scale": decision.rank_score_scale,
                "min_score_raw": decision.min_score_raw,
                "min_score_normalized": decision.min_score,
                "min_score_scale": decision.min_score_scale,
                "shadow_as_own_lane": bool(decision.shadow_as_own_lane),
                "executable": bool(decision.executable),
                "price_pct_5m": _field_float(token, "price_pct_5m", "buy_price_pct_5m", default=0.0),
                "market_cap_usd": _field_float(token, "market_cap_usd", "buy_market_cap_usd", default=0.0),
                "txns_last_5m": _field_float(token, "txns_last_5m", "buy_txns_last_5m", default=0.0),
                "liquidity_usd": _field_float(token, "liquidity_usd", "buy_liquidity_usd", default=0.0),
                "has_jupiter_route": _bool(token.get("has_jupiter_route")),
                "liquidity_is_proxy": _bool(
                    token.get("liquidity_is_proxy")
                    or token.get("liquidity_usd_is_proxy")
                    or token.get("buy_liquidity_is_proxy")
                ),
            }
            AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            AUDIT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        return


def _record_context_application(token: dict[str, Any], *, shadow: bool) -> None:
    try:
        with _AUDIT_LOCK:
            payload = _read_audit()
            if shadow:
                payload["shadow_as_own_lane"] = int(payload.get("shadow_as_own_lane") or 0) + 1
            else:
                payload["bought_as_own_lane"] = int(payload.get("bought_as_own_lane") or 0) + 1
            mixed = (
                str(token.get("entry_lane") or "") != LANE_RESEARCH_RANK_CANARY
                or str(token.get("gate_profile") or "") != "research_rank_canary"
                or str(token.get("profit_lane_tier") or "") != LANE_RESEARCH_RANK_CANARY
            )
            if mixed:
                payload["mixed_lane_detected"] = int(payload.get("mixed_lane_detected") or 0) + 1
            payload["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            AUDIT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        return


def _record_event(token: dict[str, Any], decision: ResearchRankCanaryDecision, *, dry_run: bool, live: bool) -> None:
    address = str(token.get("address") or token.get("token_address") or "")
    if not address:
        return
    try:
        from utils.runtime_telemetry import record_runtime_event

        record_runtime_event(
            "research_rank_canary_eval",
            address,
            allowed=bool(decision.allowed),
            reason=str(decision.reason),
            dry_run=bool(dry_run),
            live=bool(live),
            entry_lane=str(token.get("entry_lane") or ""),
            rank_score_raw=float(decision.rank_score_raw),
            rank_score_normalized=float(decision.rank_score),
            rank_score_scale=str(decision.rank_score_scale),
            min_score_raw=float(decision.min_score_raw),
            min_score_normalized=float(decision.min_score),
            min_score_scale=str(decision.min_score_scale),
        )
    except Exception:
        return


def evaluate_research_rank_canary(
    token: dict[str, Any],
    rank_info: dict[str, Any] | None,
    *,
    dry_run: bool,
    live: bool,
) -> ResearchRankCanaryDecision:
    min_score_raw, min_score, min_score_scale = normalize_score(
        getattr(CFG, "RESEARCH_RANK_CANARY_MIN_SCORE", 64.81),
        64.81,
    )
    amount = _float(getattr(CFG, "RESEARCH_RANK_CANARY_SIZE_SOL", 0.01), 0.01)
    if dry_run:
        amount = max(amount, _float(getattr(CFG, "MIN_BUY_SOL", amount), amount))
    rank_raw_value = (
        (rank_info or {}).get("rank_score")
        or (rank_info or {}).get("research_rank_score")
        or token.get("rank_score")
        or token.get("research_rank_score")
    )
    rank_score_raw, rank_score, rank_score_scale = normalize_score(rank_raw_value, 0.0)

    def decision(
        allowed: bool,
        reason: str,
        *,
        shadow_as_own_lane: bool = False,
        executable: bool = True,
    ) -> ResearchRankCanaryDecision:
        out = ResearchRankCanaryDecision(
            allowed,
            LANE_RESEARCH_RANK_CANARY,
            reason,
            rank_score,
            min_score,
            amount,
            rank_score_raw,
            rank_score_scale,
            min_score_raw,
            min_score_scale,
            shadow_as_own_lane,
            executable,
        )
        _record_audit(token, out, dry_run=dry_run, live=live)
        _record_event(token, out, dry_run=dry_run, live=live)
        return out

    def not_executable(reason: str) -> ResearchRankCanaryDecision:
        if bool(getattr(CFG, "RESEARCH_RANK_CANARY_SHADOW_IF_NOT_EXECUTABLE", True)):
            return decision(
                False,
                f"research_rank_canary_not_executable:{reason}",
                shadow_as_own_lane=True,
                executable=False,
            )
        return decision(False, reason, executable=False)

    if not bool(getattr(CFG, "RESEARCH_RANK_CANARY_ENABLED", True)):
        return decision(False, "disabled")
    if normalize_entry_lane(token.get("entry_lane")) != LANE_RESEARCH_SNIPER:
        return decision(False, "not_research_sniper")
    if live and not bool(getattr(CFG, "RESEARCH_RANK_CANARY_LIVE_ENABLED", False)):
        return decision(False, "live_disabled")
    if dry_run and not bool(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_ENABLED", True)):
        return decision(False, "paper_disabled")
    if rank_score < min_score:
        return decision(False, "rank_below_min")
    price5m = _field_float(token, "price_pct_5m", "buy_price_pct_5m", default=0.0)
    min_price5m = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_PRICE5M", 40.0), 40.0)
    max_price5m = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_PRICE5M", 100.0), 100.0)
    if price5m < min_price5m:
        return decision(False, "price5m_below_min", shadow_as_own_lane=True, executable=False)
    if price5m > max_price5m:
        return decision(False, "price5m_out_of_band")
    liq = _field_float(token, "liquidity_usd", "buy_liquidity_usd", default=0.0)
    if 40.0 <= price5m < 50.0:
        low_band_min_rank = _float(getattr(CFG, "RESEARCH_RANK_CANARY_LOW_BAND_MIN_RANK_SCORE", 70.0), 70.0)
        low_band_min_liq = _float(
            getattr(CFG, "RESEARCH_RANK_CANARY_LOW_BAND_MIN_LIQUIDITY_USD", 20_000.0),
            20_000.0,
        )
        if rank_score < low_band_min_rank and liq < low_band_min_liq:
            return decision(False, "price5m_40_50_requires_rank70_or_liq20k", shadow_as_own_lane=True, executable=False)
    mcap = _field_float(token, "market_cap_usd", "buy_market_cap_usd", default=0.0)
    min_mcap = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_MCAP_USD", 25_000.0), 25_000.0)
    max_mcap = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_MCAP_USD", 100_000.0), 100_000.0)
    if mcap < min_mcap or mcap > max_mcap:
        return decision(False, "mcap_out_of_band")
    txns = _field_float(token, "txns_last_5m", "buy_txns_last_5m", default=0.0)
    min_txns = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_TXNS_5M", 300), 300.0)
    if txns < min_txns:
        return decision(False, "txns_below_min")
    min_liq = _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_LIQUIDITY_USD", 2000.0), 2000.0)
    if liq < min_liq:
        return not_executable("liquidity_below_min")
    proxy = _bool(token.get("liquidity_is_proxy") or token.get("liquidity_usd_is_proxy") or token.get("buy_liquidity_is_proxy"))
    if proxy and bool(getattr(CFG, "RESEARCH_RANK_CANARY_PREFER_REAL_LIQUIDITY", True)):
        return decision(False, "proxy_liquidity")
    if dry_run and bool(getattr(CFG, "RESEARCH_RANK_CANARY_REQUIRE_ROUTE_PAPER", True)) and not _bool(token.get("has_jupiter_route")):
        return not_executable("no_route_paper")
    if live and bool(getattr(CFG, "RESEARCH_RANK_CANARY_REQUIRE_ROUTE_LIVE", True)) and not _bool(token.get("has_jupiter_route")):
        return not_executable("no_route_live")
    return decision(True, "research_rank_canary")


def apply_research_rank_canary_context(
    token: dict[str, Any],
    decision: ResearchRankCanaryDecision,
    *,
    record_audit: bool = True,
) -> dict[str, Any]:
    token["entry_lane"] = decision.entry_lane
    token["gate_profile"] = "research_rank_canary"
    token["profit_lane_tier"] = decision.entry_lane
    token["lane_policy_category"] = POLICY_RESEARCH_RANK_CANARY
    token["research_rank_canary_rank_score"] = decision.rank_score
    token["research_rank_canary_rank_score_raw"] = decision.rank_score_raw
    token["research_rank_canary_rank_score_scale"] = decision.rank_score_scale
    token["research_rank_canary_min_score"] = decision.min_score
    token["research_rank_canary_min_score_raw"] = decision.min_score_raw
    token["research_rank_canary_min_score_scale"] = decision.min_score_scale
    token["research_rank_canary_amount_sol"] = decision.amount_sol
    token["green_sniper_reason"] = decision.reason
    token["live_profit_gate_failed_count"] = 0
    token["live_profit_gate_failures"] = ""
    token["live_profit_gate_profile"] = "research_rank_canary"
    token["sniper_gate_profile"] = "research_rank_canary"
    if record_audit:
        _record_context_application(token, shadow=False)
    return token


def apply_research_rank_canary_shadow_context(token: dict[str, Any], decision: ResearchRankCanaryDecision) -> dict[str, Any]:
    apply_research_rank_canary_context(token, decision, record_audit=False)
    token["green_sniper_reason"] = decision.reason
    token["research_rank_canary_shadow"] = 1
    token["research_rank_canary_not_executable"] = int(not decision.executable)
    _record_context_application(token, shadow=True)
    return token


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _pnl(row: dict[str, Any]) -> float:
    return fnum(_first(row, "realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct"), 0.0)


def _is_rank_canary_row(row: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(_first(row, key) or "")
        for key in (
            "entry_lane",
            "gate_profile",
            "sniper_gate_profile",
            "profit_lane_tier",
            "lane_policy_category",
            "reason",
            "green_sniper_reason",
        )
    ).lower()
    return "research_rank_canary" in haystack


def _price5m(row: dict[str, Any]) -> float:
    return fnum(_first(row, "buy_price_pct_5m", "price_pct_5m", "price5m"), 0.0)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_pnl(row) for row in rows]
    if not rows:
        return {
            "rows": 0,
            "win_rate_pct": 0.0,
            "avg_pnl_pct": 0.0,
            "median_pnl_pct": 0.0,
            "total_pnl_pct_points": 0.0,
            "severe_loss_count": 0,
        }
    return {
        "rows": len(rows),
        "win_rate_pct": round(100.0 * sum(1 for value in pnls if value > 0.0) / len(pnls), 3),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        "median_pnl_pct": round(statistics.median(pnls), 3),
        "total_pnl_pct_points": round(sum(pnls), 3),
        "severe_loss_count": sum(1 for row, pnl in zip(rows, pnls) if is_severe_exit(row) or pnl <= -25.0),
    }


def build_research_rank_canary_audit_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    runtime_audit = _read_audit() if root == PROJECT_ROOT else {}
    if isinstance(runtime_audit.get("runtime_audit"), dict):
        runtime_audit = runtime_audit["runtime_audit"]
    rows = load_candidate_outcomes(root) + load_paper_positions(root) + load_sqlite_positions(root)
    rank_rows = [row for row in rows if _is_rank_canary_row(row)]
    band_25_40 = [row for row in rank_rows if 25.0 <= _price5m(row) < 40.0]
    band_40_50 = [row for row in rank_rows if 40.0 <= _price5m(row) < 50.0]
    band_50_100 = [row for row in rank_rows if 50.0 <= _price5m(row) <= 100.0]
    own_lane = [
        row
        for row in rank_rows
        if normalize_entry_lane(_first(row, "entry_lane")) == LANE_RESEARCH_RANK_CANARY
        and str(_first(row, "gate_profile", "sniper_gate_profile") or "").strip().lower() == "research_rank_canary"
    ]
    mixed_lane = [
        row
        for row in rank_rows
        if row not in own_lane
        or str(_first(row, "profit_lane_tier") or "").strip().lower() == "pump_early_pumpswap_prime"
        or str(_first(row, "gate_profile", "sniper_gate_profile") or "").strip().lower() == "pumpswap_profit_prime"
    ]
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "evaluated": int(runtime_audit.get("evaluated") or runtime_audit.get("total_evaluations") or 0),
        "allowed": int(runtime_audit.get("allowed") or 0),
        "rejected": int(runtime_audit.get("rejected") or 0),
        "bought_as_own_lane": int(runtime_audit.get("bought_as_own_lane") or 0),
        "shadow_as_own_lane": int(runtime_audit.get("shadow_as_own_lane") or 0),
        "mixed_lane_detected": int(runtime_audit.get("mixed_lane_detected") or 0) + len(mixed_lane),
        "blocked_by_reason": runtime_audit.get("blocked_by_reason") if isinstance(runtime_audit.get("blocked_by_reason"), dict) else {},
        "runtime_audit": runtime_audit,
        "config": {
            "enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "RESEARCH_RANK_CANARY_LIVE_ENABLED", False)),
            "min_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MIN_PRICE5M", 40.0), 40.0),
            "max_price5m": _float(getattr(CFG, "RESEARCH_RANK_CANARY_MAX_PRICE5M", 100.0), 100.0),
            "low_band_min_rank_score": _float(getattr(CFG, "RESEARCH_RANK_CANARY_LOW_BAND_MIN_RANK_SCORE", 70.0), 70.0),
            "low_band_min_liquidity_usd": _float(
                getattr(CFG, "RESEARCH_RANK_CANARY_LOW_BAND_MIN_LIQUIDITY_USD", 20_000.0),
                20_000.0,
            ),
            "force_own_lane": bool(getattr(CFG, "RESEARCH_RANK_CANARY_FORCE_OWN_LANE", True)),
        },
        "price5m_band_comparison": {
            "price5m_25_40_shadow_candidate": _summary(band_25_40),
            "price5m_40_50_conditional": _summary(band_40_50),
            "price5m_50_100_executable": _summary(band_50_100),
        },
        "lane_mix_audit": {
            "rank_canary_rows": len(rank_rows),
            "own_lane_rows": len(own_lane),
            "mixed_lane_rows": len(mixed_lane),
            "pumpswap_profit_prime_leak_rows": sum(
                1
                for row in rank_rows
                if str(_first(row, "profit_lane_tier") or "").strip().lower() == "pump_early_pumpswap_prime"
                or str(_first(row, "gate_profile", "sniper_gate_profile") or "").strip().lower() == "pumpswap_profit_prime"
            ),
        },
    }
    return report


def write_research_rank_canary_audit_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_research_rank_canary_audit_report(root)
    write_json(metrics_dir(root) / "research_rank_canary_audit.json", report)
    return report


__all__ = [
    "AUDIT_PATH",
    "ResearchRankCanaryDecision",
    "apply_research_rank_canary_context",
    "apply_research_rank_canary_shadow_context",
    "build_research_rank_canary_audit_report",
    "evaluate_research_rank_canary",
    "normalize_score",
    "write_research_rank_canary_audit_report",
]
