from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analytics.report_utils import boolish, fnum
from config.config import CFG


ACTION_BUY = "buy"
ACTION_DOWNSIZE = "downsize"
ACTION_SHADOW = "shadow"
ACTION_BLOCK = "block"


@dataclass(frozen=True)
class PreEntryRiskDecision:
    allowed: bool
    action: str
    reason: str
    failures: tuple[str, ...]
    risk_flags: tuple[str, ...]
    amount_sol: float
    original_amount_sol: float
    max_amount_sol: float
    normal_size: bool
    force_shadow: bool = False


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _present(row: dict[str, Any], *keys: str) -> bool:
    return _first(row, *keys) is not None


def _cfg_float(cfg: Any, name: str, default: float) -> float:
    return fnum(getattr(cfg, name, default), default)


def _cfg_bool(cfg: Any, name: str, default: bool) -> bool:
    value = getattr(cfg, name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _route_value(row: dict[str, Any]) -> tuple[bool | None, bool]:
    value = _first(row, "has_jupiter_route", "has_route", "route_ok", "route_available", "jupiter_route_ok")
    if value is None:
        return None, False
    return boolish(value, False), True


def _price_impact_pct(row: dict[str, Any]) -> tuple[float, bool]:
    if _present(row, "price_impact_pct", "buy_price_impact_pct", "route_price_impact_pct", "jupiter_price_impact_pct"):
        return fnum(
            _first(row, "price_impact_pct", "buy_price_impact_pct", "route_price_impact_pct", "jupiter_price_impact_pct"),
            0.0,
        ), True
    if _present(row, "price_impact_bps", "buy_price_impact_bps", "jupiter_price_impact_bps"):
        return fnum(_first(row, "price_impact_bps", "buy_price_impact_bps", "jupiter_price_impact_bps"), 0.0) / 100.0, True
    return 0.0, False


def _reason_text(row: dict[str, Any]) -> str:
    return " ".join(
        _norm(_first(row, key))
        for key in (
            "reason",
            "green_sniper_reason",
            "entry_reason",
            "blocked_reason",
            "reject_reason",
            "sniper_research_subprofile_reason",
        )
    )


def _negative_price5m_exception(row: dict[str, Any], *, cfg: Any, price5m: float, route_ok: bool | None, proxy: bool) -> bool:
    if proxy or route_ok is False:
        return False
    impact, impact_present = _price_impact_pct(row)
    max_impact = _cfg_float(cfg, "PRE_ENTRY_RISK_NEG_PRICE5M_EXCEPTION_MAX_PRICE_IMPACT_PCT", 12.0)
    if impact_present and impact > max_impact:
        return False
    min_price5m = _cfg_float(cfg, "PRE_ENTRY_RISK_NEG_PRICE5M_EXCEPTION_MIN_PRICE5M_PCT", -5.0)
    if price5m < min_price5m:
        return False
    liq = fnum(_first(row, "liquidity_usd", "buy_liquidity_usd"), 0.0)
    txns = fnum(_first(row, "txns_last_5m", "txns_5m", "buy_txns_last_5m"), 0.0)
    mcap = fnum(_first(row, "market_cap_usd", "buy_market_cap_usd", "mcap"), 0.0)
    return (
        liq >= _cfg_float(cfg, "PRE_ENTRY_RISK_NEG_PRICE5M_EXCEPTION_MIN_LIQUIDITY_USD", 10_000.0)
        and txns >= _cfg_float(cfg, "PRE_ENTRY_RISK_NEG_PRICE5M_EXCEPTION_MIN_TXNS_5M", 300.0)
        and _cfg_float(cfg, "PRE_ENTRY_RISK_NEG_PRICE5M_EXCEPTION_MIN_MCAP_USD", 20_000.0)
        <= mcap
        <= _cfg_float(cfg, "PRE_ENTRY_RISK_NEG_PRICE5M_EXCEPTION_MAX_MCAP_USD", 80_000.0)
    )


def _decision(
    *,
    allowed: bool,
    action: str,
    reason: str,
    failures: list[str] | tuple[str, ...],
    risk_flags: list[str] | tuple[str, ...],
    amount_sol: float,
    original_amount_sol: float,
    max_amount_sol: float,
    normal_size: bool,
    force_shadow: bool = False,
) -> PreEntryRiskDecision:
    return PreEntryRiskDecision(
        bool(allowed),
        str(action),
        str(reason),
        tuple(dict.fromkeys(failures)),
        tuple(dict.fromkeys(risk_flags)),
        max(0.0, float(amount_sol or 0.0)),
        max(0.0, float(original_amount_sol or 0.0)),
        max(0.0, float(max_amount_sol or 0.0)),
        bool(normal_size),
        bool(force_shadow),
    )


def evaluate_pre_entry_risk(
    row: dict[str, Any],
    *,
    amount_sol: float,
    dry_run: bool = True,
    live: bool = False,
    cfg: Any = CFG,
) -> PreEntryRiskDecision:
    original_amount = max(0.0, float(amount_sol or 0.0))
    micro_cap = max(0.0, _cfg_float(cfg, "PRE_ENTRY_RISK_MICRO_CAP_SOL", _cfg_float(cfg, "MICRO_LANE_HARD_CAP_SOL", 0.01)))
    normal_threshold = max(micro_cap, _cfg_float(cfg, "PRE_ENTRY_RISK_NORMAL_SIZE_SOL", 0.03))
    normal_size = original_amount >= normal_threshold

    if not _cfg_bool(cfg, "PRE_ENTRY_RISK_GUARD_ENABLED", True):
        return _decision(
            allowed=True,
            action=ACTION_BUY,
            reason="pre_entry_risk_disabled",
            failures=(),
            risk_flags=(),
            amount_sol=original_amount,
            original_amount_sol=original_amount,
            max_amount_sol=original_amount,
            normal_size=normal_size,
        )

    risk_flags: list[str] = []
    failures: list[str] = []
    hard_route_flags: list[str] = []

    route_ok, route_present = _route_value(row)
    if route_present and not route_ok:
        hard_route_flags.append("no_route")
    proxy = boolish(_first(row, "liquidity_is_proxy", "liquidity_usd_is_proxy", "buy_liquidity_is_proxy", "route_proxy"), False)
    if proxy:
        hard_route_flags.append("proxy_liquidity")
    impact, impact_present = _price_impact_pct(row)
    max_impact = _cfg_float(cfg, "PRE_ENTRY_RISK_MAX_PRICE_IMPACT_PCT", 12.0 if live else 20.0)
    if impact_present and max_impact > 0 and impact > max_impact:
        hard_route_flags.append("high_price_impact")

    risk_flags.extend(hard_route_flags)

    reason_text = _reason_text(row)
    toxic = (
        boolish(_first(row, "toxic_initial_sell_pressure", "initial_sell_pressure_toxic", "toxic_pressure", "toxic_sell_pressure"), False)
        or "toxic_initial_sell_pressure" in reason_text
        or "toxic_pressure" in reason_text
    )
    if toxic:
        failures.append("toxic_pressure")

    price5m_present = _present(row, "price_pct_5m", "buy_price_pct_5m", "price5m", "price_change_5m")
    price5m = fnum(_first(row, "price_pct_5m", "buy_price_pct_5m", "price5m", "price_change_5m"), 0.0)
    if price5m_present and price5m < 0.0 and _cfg_bool(cfg, "PRE_ENTRY_RISK_BLOCK_NEGATIVE_PRICE5M", True):
        if _negative_price5m_exception(row, cfg=cfg, price5m=price5m, route_ok=route_ok, proxy=proxy):
            risk_flags.append("price5m_negative_exception")
        else:
            failures.append("price5m_negative")

    liq_present = _present(row, "liquidity_usd", "buy_liquidity_usd")
    liq = fnum(_first(row, "liquidity_usd", "buy_liquidity_usd"), 0.0)
    if liq_present and liq < _cfg_float(cfg, "PRE_ENTRY_RISK_MIN_REAL_LIQUIDITY_USD", 10_000.0):
        risk_flags.append("low_real_liquidity")

    mcap_present = _present(row, "market_cap_usd", "buy_market_cap_usd", "mcap")
    mcap = fnum(_first(row, "market_cap_usd", "buy_market_cap_usd", "mcap"), 0.0)
    if mcap_present and mcap < _cfg_float(cfg, "PRE_ENTRY_RISK_MIN_MCAP_USD", 10_000.0):
        risk_flags.append("low_mcap")

    txns_present = _present(row, "txns_last_5m", "txns_5m", "buy_txns_last_5m")
    txns = fnum(_first(row, "txns_last_5m", "txns_5m", "buy_txns_last_5m"), 0.0)
    if txns_present and txns < _cfg_float(cfg, "PRE_ENTRY_RISK_MIN_TXNS_5M", 300.0):
        risk_flags.append("low_txns_5m")

    if (
        price5m_present
        and mcap_present
        and mcap >= _cfg_float(cfg, "PRE_ENTRY_RISK_HIGH_MCAP_USD", 250_000.0)
        and price5m < _cfg_float(cfg, "PRE_ENTRY_RISK_HIGH_MCAP_MIN_PRICE5M_PCT", 10.0)
    ):
        failures.append("high_mcap_no_pump")

    if failures:
        reason = "pre_entry_risk:" + ",".join(failures[:6])
        return _decision(
            allowed=False,
            action=ACTION_BLOCK,
            reason=reason,
            failures=failures,
            risk_flags=risk_flags,
            amount_sol=0.0,
            original_amount_sol=original_amount,
            max_amount_sol=0.0,
            normal_size=normal_size,
        )

    if hard_route_flags and (normal_size or live or len(hard_route_flags) >= 2):
        action = ACTION_SHADOW if dry_run and not live else ACTION_BLOCK
        reason = "pre_entry_risk:" + ",".join(hard_route_flags[:6])
        return _decision(
            allowed=False,
            action=action,
            reason=reason,
            failures=hard_route_flags,
            risk_flags=risk_flags,
            amount_sol=0.0,
            original_amount_sol=original_amount,
            max_amount_sol=micro_cap,
            normal_size=normal_size,
            force_shadow=action == ACTION_SHADOW,
        )

    caution_flags = [flag for flag in risk_flags if flag in {"low_real_liquidity", "low_mcap", "low_txns_5m"}]
    if caution_flags and normal_size and micro_cap > 0.0:
        if dry_run and not live and _cfg_bool(cfg, "PAPER_EXACT_TRADE_SIZE_ENABLED", False):
            return _decision(
                allowed=False, action=ACTION_SHADOW,
                reason="pre_entry_risk_fixed_size_conflict:" + ",".join(caution_flags[:6]),
                failures=caution_flags, risk_flags=risk_flags, amount_sol=0.0,
                original_amount_sol=original_amount, max_amount_sol=micro_cap,
                normal_size=normal_size, force_shadow=True,
            )
        downsized = min(original_amount, micro_cap)
        return _decision(
            allowed=True,
            action=ACTION_DOWNSIZE,
            reason="pre_entry_risk_downsize:" + ",".join(caution_flags[:6]),
            failures=(),
            risk_flags=risk_flags,
            amount_sol=downsized,
            original_amount_sol=original_amount,
            max_amount_sol=micro_cap,
            normal_size=normal_size,
        )

    reason = "pre_entry_risk_ok"
    if risk_flags:
        reason = "pre_entry_risk_micro_exception:" + ",".join(risk_flags[:6])
    return _decision(
        allowed=True,
        action=ACTION_BUY,
        reason=reason,
        failures=(),
        risk_flags=risk_flags,
        amount_sol=original_amount,
        original_amount_sol=original_amount,
        max_amount_sol=original_amount,
        normal_size=normal_size,
    )


def apply_pre_entry_risk_context(row: dict[str, Any], decision: PreEntryRiskDecision) -> dict[str, Any]:
    row["pre_entry_risk_action"] = decision.action
    row["pre_entry_risk_reason"] = decision.reason
    row["pre_entry_risk_failures"] = ",".join(decision.failures)
    row["pre_entry_risk_flags"] = ",".join(decision.risk_flags)
    row["pre_entry_risk_amount_sol"] = float(decision.amount_sol)
    row["pre_entry_risk_original_amount_sol"] = float(decision.original_amount_sol)
    row["pre_entry_risk_max_amount_sol"] = float(decision.max_amount_sol)
    row["pre_entry_risk_normal_size"] = int(bool(decision.normal_size))
    return row


__all__ = [
    "ACTION_BLOCK",
    "ACTION_BUY",
    "ACTION_DOWNSIZE",
    "ACTION_SHADOW",
    "PreEntryRiskDecision",
    "apply_pre_entry_risk_context",
    "evaluate_pre_entry_risk",
]
