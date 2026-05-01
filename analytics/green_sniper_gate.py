from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from config.config import CFG
from analytics.green_sniper_score import score_green_sniper
from analytics.green_sniper_risk_guard import evaluate_green_sniper_risk_guard
from analytics.late_momentum_watch import evaluate_late_momentum_watch
from analytics.liquidity_risk import evaluate_liquidity_risk
from analytics.social_signal import (
    SOCIAL_STATUS_PRESENT,
    SOCIAL_STATUS_SUSPICIOUS,
    social_signal_from_token,
)
from analytics.token_time import compute_age_minutes, token_with_age
from ml.lane_taxonomy import LANE_PUMP_EARLY_GREEN_SNIPER


@dataclass(frozen=True)
class GreenSniperDecision:
    action: str  # buy, shadow, delay, reject
    lane: str
    reason: str
    score: float
    size_hint: str
    runner_profile: str
    reject_reasons: tuple[str, ...] = ()
    route_required: bool = False
    proxy_liquidity_used: bool = False
    social_status: str = "unknown"
    social_bonus_applied: float = 0.0
    social_risk_flags: tuple[str, ...] = ()
    paper_birth_probe: bool = False
    gate_profile: str = "green_sniper"
    risk_level: str = "low"
    risk_reasons: tuple[str, ...] = ()
    size_multiplier: float = 1.0
    liquidity_risk_level: str = "low"
    liquidity_risk_reasons: tuple[str, ...] = ()
    route_proxy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value is None:
            return default
        out = float(value)
        if out != out:
            return default
        return out
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _to_bool(value: Any, default: bool = False) -> bool:
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


def _age_minutes(token: dict[str, Any]) -> float:
    return compute_age_minutes(token)


def _norm_source(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def _buy_sell_ratio(token: dict[str, Any]) -> float:
    buys = _to_float(token.get("txns_last_5m_buys"), None)
    sells = _to_float(token.get("txns_last_5m_sells"), None)
    if buys is None or sells is None:
        return 1.0
    return float(buys) / max(float(sells), 1.0)


def _score(token: dict[str, Any], *, live: bool, has_route: bool, proxy_liquidity: bool) -> float:
    token_for_score = token_with_age(token)
    score = score_green_sniper(token_for_score, has_route=has_route, proxy_liquidity=proxy_liquidity, live=live).score
    social = social_signal_from_token(token)
    if bool(getattr(CFG, "GREEN_SNIPER_SOCIALS_BONUS_ENABLED", True)):
        if social.status == SOCIAL_STATUS_PRESENT:
            score += float(getattr(CFG, "GREEN_SNIPER_SOCIALS_SCORE_BONUS", 5.0) or 5.0)
        elif social.status == SOCIAL_STATUS_SUSPICIOUS:
            score -= float(getattr(CFG, "GREEN_SNIPER_SOCIALS_RISK_PENALTY", 5.0) or 5.0)
    return round(max(0.0, score), 3)


def _size_hint(token: dict[str, Any], score: float) -> str:
    price5m = float(_to_float(token.get("price_pct_5m"), 0.0) or 0.0)
    txns = _to_int(token.get("txns_last_5m"), 0)
    age = _age_minutes(token)
    rank = float(_to_float(token.get("rank_score") or token.get("research_rank_score"), 0.0) or 0.0)
    if score >= 78.0 and rank >= 50.0 and txns >= int(getattr(CFG, "GREEN_SNIPER_HOT_MIN_TXNS_5M", 80) or 80) and 20.0 <= price5m <= 180.0 and age <= 3.0:
        return "hot"
    if score >= 55.0:
        return "core"
    return "micro"


def _runner_profile(size_hint: str, token: dict[str, Any]) -> str:
    _ = token
    if size_hint == "hot":
        return "green_sniper_runner"
    return "green_sniper_runner"


def _paper_birth_probe_allowed(
    failures: list[str],
    *,
    dry_run: bool,
    live: bool,
    source: str,
    age: float,
    liq: float,
    impact: float,
    mcap: float,
) -> bool:
    if live or not dry_run:
        return False
    if not bool(getattr(CFG, "PAPER_SNIPER_MODE", False)):
        return False
    if not bool(getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_ENABLED", True)):
        return False
    if source not in {"pumpfun", "pumpportal"}:
        return False

    max_age = float(getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_AGE_MIN", 3.0) or 3.0)
    min_liq = float(getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_MIN_LIQUIDITY_USD", 1000.0) or 1000.0)
    max_impact = float(getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_MAX_PRICE_IMPACT_PCT", 25.0) or 25.0)
    if age > max_age or liq < min_liq or impact > max_impact:
        return False
    if mcap > 0 and mcap > float(getattr(CFG, "GREEN_SNIPER_MAX_MARKET_CAP_USD", 180000.0) or 180000.0):
        return False

    allowed_missing = {
        "missing_price_pct_5m",
        "missing_price",
        "missing_mcap",
        "proxy_liquidity_paper_disabled",
        "proxy_liquidity_productive_block",
        "low_txns_5m",
        "weak_buy_sell_ratio",
        "low_green_momentum",
    }
    hard_failures = {
        "too_young",
        "too_old",
        "too_late_momentum",
        "high_mcap",
        "proxy_liquidity_live",
        "high_impact",
        "snapshot_missing",
        "no_route",
        "live_disabled",
    }
    return bool(failures) and all(item in allowed_missing for item in failures) and not any(
        item in hard_failures for item in failures
    )


def evaluate_green_sniper(token: dict[str, Any], *, dry_run: bool, live: bool) -> GreenSniperDecision:
    if not bool(getattr(CFG, "GREEN_SNIPER_ENABLED", True)):
        return GreenSniperDecision(
            action="reject",
            lane=LANE_PUMP_EARLY_GREEN_SNIPER,
            reason="disabled",
            score=0.0,
            size_hint="none",
            runner_profile="",
            reject_reasons=("disabled",),
        )

    failures: list[str] = []
    age = _age_minutes(token)
    liq = float(_to_float(token.get("liquidity_usd"), 0.0) or 0.0)
    mcap = float(_to_float(token.get("market_cap_usd"), 0.0) or 0.0)
    price5m = _to_float(token.get("price_pct_5m"), None)
    txns = _to_int(token.get("txns_last_5m"), 0)
    impact = float(_to_float(token.get("price_impact_pct"), 0.0) or 0.0)
    missing = _to_int(token.get("snapshot_missing_fields"), 0)
    has_price = bool(_to_float(token.get("price_usd"), None) or price5m is not None)
    has_route = _to_bool(token.get("has_jupiter_route"), False)
    proxy_liq = _to_bool(token.get("liquidity_is_proxy") or token.get("liquidity_usd_is_proxy"), False)
    source = _norm_source(token.get("discovered_via") or token.get("source"))

    min_age = float(getattr(CFG, "GREEN_SNIPER_LIVE_MIN_AGE_MIN", 0.35) if live else getattr(CFG, "GREEN_SNIPER_MIN_AGE_MIN", 0.15))
    max_age = float(getattr(CFG, "GREEN_SNIPER_LIVE_MAX_AGE_MIN", 6.0) if live else getattr(CFG, "GREEN_SNIPER_MAX_AGE_MIN", 8.0))
    min_liq = float(getattr(CFG, "GREEN_SNIPER_LIVE_MIN_LIQUIDITY_USD", 2500.0) if live else getattr(CFG, "GREEN_SNIPER_MIN_LIQUIDITY_USD", 1200.0))
    max_impact = float(getattr(CFG, "GREEN_SNIPER_LIVE_MAX_PRICE_IMPACT_PCT", 12.0) if live else getattr(CFG, "GREEN_SNIPER_MAX_PRICE_IMPACT_PCT", 20.0))
    min_txns = int(getattr(CFG, "GREEN_SNIPER_LIVE_MIN_TXNS_5M", 60) if live else getattr(CFG, "GREEN_SNIPER_MIN_TXNS_5M", 35))
    route_required = bool(getattr(CFG, "GREEN_SNIPER_REQUIRE_ROUTE_LIVE", True) if live else getattr(CFG, "GREEN_SNIPER_REQUIRE_ROUTE_PAPER", False))

    if live and not bool(getattr(CFG, "GREEN_SNIPER_LIVE_ENABLED", False)):
        failures.append("live_disabled")
    if age < min_age:
        failures.append("too_young")
    if max_age > 0 and age > max_age:
        failures.append("too_old")
    if price5m is None:
        failures.append("missing_price_pct_5m")
    else:
        if bool(getattr(CFG, "LATE_MOMENTUM_WATCH_ENABLED", True)) and price5m >= float(getattr(CFG, "LATE_MOMENTUM_WATCH_MIN_PRICE5M", 300.0) or 300.0):
            late = evaluate_late_momentum_watch(token, dry_run=dry_run, live=live)
            social = social_signal_from_token(token)
            return GreenSniperDecision(
                action=late.action,
                lane=late.lane,
                reason=late.reason,
                score=late.score,
                size_hint="micro",
                runner_profile="green_sniper_runner",
                reject_reasons=late.reject_reasons,
                route_required=route_required,
                proxy_liquidity_used=proxy_liq,
                social_status=social.status,
                social_bonus_applied=0.0,
                social_risk_flags=tuple(social.risk_flags),
                gate_profile="late_momentum_watch",
                route_proxy=late.route_proxy,
            )
        if price5m < float(getattr(CFG, "GREEN_SNIPER_MIN_PRICE_PCT_5M", 20.0)):
            failures.append("low_green_momentum")
        if price5m > float(getattr(CFG, "GREEN_SNIPER_MAX_PRICE_PCT_5M", 280.0)):
            failures.append("too_late_momentum")
    if not has_price:
        failures.append("missing_price")
    if mcap <= 0:
        failures.append("missing_mcap")
    elif mcap < float(getattr(CFG, "GREEN_SNIPER_MIN_MARKET_CAP_USD", 2000.0)):
        failures.append("low_mcap")
    elif mcap > float(getattr(CFG, "GREEN_SNIPER_MAX_MARKET_CAP_USD", 180000.0)):
        failures.append("high_mcap")
    if liq < min_liq:
        failures.append("low_liquidity")
    if (
        dry_run
        and proxy_liq
        and bool(getattr(CFG, "GREEN_SNIPER_BLOCK_PROXY_PRODUCTIVE", True))
    ):
        failures.append("proxy_liquidity_productive_block")
    if live and proxy_liq:
        failures.append("proxy_liquidity_live")
    if dry_run and proxy_liq and not bool(getattr(CFG, "GREEN_SNIPER_ALLOW_PROXY_LIQUIDITY_PAPER", False)):
        failures.append("proxy_liquidity_paper_disabled")
    if txns < min_txns:
        failures.append("low_txns_5m")
    if _buy_sell_ratio(token) < float(getattr(CFG, "GREEN_SNIPER_MIN_BUY_SELL_RATIO", 1.15)):
        failures.append("weak_buy_sell_ratio")
    if impact > max_impact:
        failures.append("high_impact")
    if missing > int(getattr(CFG, "GREEN_SNIPER_MAX_SNAPSHOT_MISSING_FIELDS", 6)):
        failures.append("snapshot_missing")
    if route_required and not has_route:
        failures.append("no_route")

    terminal = {
        "too_old",
        "too_late_momentum",
        "high_mcap",
        "missing_price",
        "proxy_liquidity_live",
        "high_impact",
        "late_momentum_watch",
    }
    score = _score(token, live=live, has_route=has_route, proxy_liquidity=proxy_liq)
    social = social_signal_from_token(token)
    if social.status == SOCIAL_STATUS_PRESENT and bool(getattr(CFG, "GREEN_SNIPER_SOCIALS_BONUS_ENABLED", True)):
        social_bonus = float(getattr(CFG, "GREEN_SNIPER_SOCIALS_SCORE_BONUS", 5.0) or 5.0)
    elif social.status == SOCIAL_STATUS_SUSPICIOUS:
        social_bonus = -float(getattr(CFG, "GREEN_SNIPER_SOCIALS_RISK_PENALTY", 5.0) or 5.0)
    else:
        social_bonus = 0.0
    size_hint = _size_hint(token, score)
    runner_profile = _runner_profile(size_hint, token)
    paper_birth_probe = _paper_birth_probe_allowed(
        failures,
        dry_run=dry_run,
        live=live,
        source=source,
        age=age,
        liq=liq,
        impact=impact,
        mcap=mcap,
    )

    if paper_birth_probe:
        action = "shadow" if bool(getattr(CFG, "GREEN_SNIPER_PAPER_BIRTH_PROBE_SHADOW_FIRST", True)) else "buy"
        reason = "paper_birth_probe:" + ",".join(failures[:8])
        size_hint = "micro"
    elif not failures:
        action = "buy"
        reason = "green_sniper_pass"
    elif source in {"pumpfun", "pumpportal"} or (price5m is not None and price5m >= float(getattr(CFG, "GREEN_SNIPER_MIN_PRICE_PCT_5M", 20.0))):
        action = "delay" if failures == ["too_young"] or failures == ["no_route"] else "shadow"
        if any(reason in terminal for reason in failures):
            action = "reject"
        reason = ",".join(failures[:8])
    else:
        action = "reject"
        reason = ",".join(failures[:8]) or "not_hot"

    risk_decision = evaluate_green_sniper_risk_guard(token, dry_run=dry_run, live=live)
    liq_decision = evaluate_liquidity_risk(token, live=live)
    if bool(getattr(CFG, "GREEN_SNIPER_RISK_GUARD_ENABLED", True)) and risk_decision.risk_level in {"high", "lethal"}:
        if paper_birth_probe and dry_run and not live and action == "shadow":
            pass
        elif risk_decision.risk_level == "lethal":
            action = "reject"
        elif live:
            action = "reject"
        elif action == "buy":
            action = "shadow"
        if not reason or reason in {"green_sniper_pass", "late_momentum_canary"}:
            reason = "risk_guard:" + ",".join(risk_decision.risk_reasons[:6])
    if bool(getattr(CFG, "GREEN_SNIPER_LIQ_GUARD_ENABLED", True)) and liq_decision.risk_level in {"high", "lethal"}:
        if paper_birth_probe and dry_run and not live and action == "shadow":
            pass
        elif live or liq_decision.risk_level == "lethal":
            action = "reject"
        elif action == "buy":
            action = "shadow"
        if not reason or reason == "green_sniper_pass":
            reason = "liquidity_risk:" + ",".join(liq_decision.reasons[:6])

    return GreenSniperDecision(
        action=action,
        lane=LANE_PUMP_EARLY_GREEN_SNIPER,
        reason=reason,
        score=score,
        size_hint=size_hint,
        runner_profile=runner_profile,
        reject_reasons=tuple(failures),
        route_required=route_required,
        proxy_liquidity_used=proxy_liq,
        social_status=social.status,
        social_bonus_applied=social_bonus,
        social_risk_flags=tuple(social.risk_flags),
        paper_birth_probe=paper_birth_probe,
        risk_level=risk_decision.risk_level,
        risk_reasons=tuple(risk_decision.risk_reasons),
        size_multiplier=risk_decision.size_multiplier,
        liquidity_risk_level=liq_decision.risk_level,
        liquidity_risk_reasons=tuple(liq_decision.reasons),
    )


def apply_green_sniper_context(token: dict[str, Any], decision: GreenSniperDecision) -> dict[str, Any]:
    token["entry_lane"] = decision.lane
    token["gate_profile"] = "green_sniper_birth_probe" if decision.paper_birth_probe else decision.gate_profile
    token["sniper_gate_profile"] = token["gate_profile"]
    token["profit_lane_tier"] = decision.lane
    token["runner_exit_profile"] = decision.runner_profile
    token["green_sniper_score"] = decision.score
    token["green_sniper_action"] = decision.action
    token["green_sniper_reason"] = decision.reason
    token["green_sniper_size_hint"] = decision.size_hint
    token["green_sniper_paper_birth_probe"] = int(bool(decision.paper_birth_probe))
    token["green_sniper_risk_level"] = decision.risk_level
    token["green_sniper_risk_reasons"] = ",".join(decision.risk_reasons)
    token["green_sniper_size_multiplier"] = decision.size_multiplier
    token["liquidity_risk_level"] = decision.liquidity_risk_level
    token["liquidity_risk_reasons"] = ",".join(decision.liquidity_risk_reasons)
    token["route_proxy"] = int(bool(decision.route_proxy))
    if decision.paper_birth_probe:
        token["entry_subtype"] = "paper_birth_probe"
    token["social_bonus_applied"] = decision.social_bonus_applied
    token["social_risk_flags"] = ",".join(decision.social_risk_flags)
    token["live_profit_gate_failed_count"] = 0 if decision.action == "buy" else len(decision.reject_reasons)
    token["live_profit_gate_failures"] = ",".join(decision.reject_reasons[:8])
    return token


__all__ = ["GreenSniperDecision", "apply_green_sniper_context", "evaluate_green_sniper"]
