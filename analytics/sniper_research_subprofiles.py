from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analytics.report_utils import (
    address_of,
    boolish,
    fnum,
    load_candidate_outcomes,
    load_paper_positions,
    load_runtime_events,
    load_sqlite_positions,
    metrics_dir,
    write_json,
    write_markdown,
)
from config.config import CFG, PROJECT_ROOT


SUBPROFILE_MOMENTUM_IGNITION = "sniper_research_momentum_ignition"
SUBPROFILE_DEEP_REVERSAL = "sniper_research_deep_reversal"


@dataclass(frozen=True)
class SniperResearchSubprofileDecision:
    allowed: bool
    subprofile: str | None
    reason: str
    failures: tuple[str, ...]


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _field_float(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    return fnum(_first(row, *keys), default)


def _dex_id(row: dict[str, Any]) -> str:
    return _norm(_first(row, "buy_dex_id", "dex_id", "dexId"))


def _route_ok(row: dict[str, Any]) -> bool:
    return boolish(_first(row, "has_jupiter_route", "route_ok", "route_available"), False)


def _proxy_liquidity(row: dict[str, Any]) -> bool:
    return boolish(_first(row, "buy_liquidity_is_proxy", "liquidity_is_proxy", "liquidity_usd_is_proxy"), False)


def _is_sniper_research(row: dict[str, Any]) -> bool:
    return str(_first(row, "entry_lane", "lane") or "").strip().lower() == "pump_early_sniper_research"


def _second_tick_confirmed(row: dict[str, Any]) -> bool:
    if boolish(_first(row, "second_tick_improved", "second_tick_price_improved", "tick2_improved"), False):
        return True
    tick1 = _first(row, "first_tick_price_pct_5m", "tick1_price_pct_5m", "first_tick_price5m")
    tick2 = _first(row, "second_tick_price_pct_5m", "tick2_price_pct_5m", "second_tick_price5m")
    return tick1 is not None and tick2 is not None and fnum(tick2, 0.0) > fnum(tick1, 0.0)


def _has_trend_data(row: dict[str, Any]) -> bool:
    trend = str(_first(row, "trend", "buy_trend", "trend_signal") or "").strip().lower()
    if trend and trend not in {"unknown", "none", "missing", "nan", "null"}:
        return True
    fallback = _first(row, "trend_fallback_used", "trend_missing", "missing_trend")
    return fallback is not None and not boolish(fallback, False)


def _top10_holder_share_pct(row: dict[str, Any]) -> float:
    return _field_float(
        row,
        "helius_top10_share_pct",
        "helius_top10_holder_share_pct",
        "helius_top10_owner_share_pct",
        "top10_share_pct",
        "top10_holder_share_pct",
        "holders_top10_share_pct",
        "top10_pct",
    )


def _toxic_initial_sell_pressure(row: dict[str, Any]) -> bool:
    if boolish(_first(row, "toxic_initial_sell_pressure", "initial_sell_pressure_toxic"), False):
        return True
    reason = str(_first(row, "toxic_initial_sell_pressure_reason", "green_sniper_reason", "reason") or "").lower()
    return "toxic_initial_sell_pressure" in reason or ">70% ventas iniciales" in reason


def _momentum_ignition_failures(row: dict[str, Any], *, cfg: Any = CFG) -> list[str]:
    failures: list[str] = []
    price5m = _field_float(row, "buy_price_pct_5m", "price_pct_5m", "price5m")
    min_price = float(getattr(cfg, "SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M", 100) or 100)
    max_price = float(getattr(cfg, "SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M", 150) or 150)
    liq = _field_float(row, "buy_liquidity_usd", "liquidity_usd")
    min_liq = float(getattr(cfg, "SNIPER_RESEARCH_MOMENTUM_MIN_LIQUIDITY_USD", 15_000.0) or 15_000.0)
    txns = _field_float(row, "buy_txns_last_5m", "txns_last_5m", "txns_5m")
    min_txns = float(getattr(cfg, "SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M", 500) or 500)
    mcap = _field_float(row, "buy_market_cap_usd", "market_cap_usd", "mcap")
    min_mcap = float(getattr(cfg, "SNIPER_RESEARCH_MOMENTUM_MIN_MCAP_USD", 15_000.0) or 15_000.0)
    max_mcap = float(getattr(cfg, "SNIPER_RESEARCH_MOMENTUM_MAX_MCAP_USD", 70_000.0) or 70_000.0)
    top10_max = float(getattr(cfg, "SNIPER_RESEARCH_MOMENTUM_MAX_TOP10_SHARE_PCT", 40.0) or 40.0)
    if price5m < min_price or price5m > max_price:
        failures.append(f"momentum:price5m_not_{min_price:g}_{max_price:g}")
    if liq < min_liq:
        failures.append(f"momentum:liq<{min_liq:g}")
    if txns < min_txns:
        failures.append(f"momentum:txns5m<{min_txns:g}")
    if mcap < min_mcap or mcap > max_mcap:
        failures.append(f"momentum:mcap_not_{min_mcap:g}_{max_mcap:g}")
    if not _route_ok(row):
        failures.append("momentum:route_required")
    if boolish(_first(row, "cluster_bad", "helius_cluster_bad"), False):
        failures.append("momentum:cluster_bad")
    top10_share = _top10_holder_share_pct(row)
    if top10_share > top10_max:
        failures.append(f"momentum:helius_top10_share>{top10_max:g}")
    if not _has_trend_data(row) and not _second_tick_confirmed(row):
        failures.append("momentum:trend_missing_without_second_tick")
    if _toxic_initial_sell_pressure(row):
        failures.append("momentum:toxic_initial_sell_pressure")
    return failures


def _deep_reversal_failures(row: dict[str, Any], *, cfg: Any = CFG) -> list[str]:
    failures: list[str] = []
    price5m = _field_float(row, "buy_price_pct_5m", "price_pct_5m", "price5m")
    min_price = float(getattr(cfg, "SNIPER_RESEARCH_DEEP_REVERSAL_MIN_PRICE5M", -90.0) or -90.0)
    max_price = float(getattr(cfg, "SNIPER_RESEARCH_DEEP_REVERSAL_MAX_PRICE5M", -50.0) or -50.0)
    txns = _field_float(row, "buy_txns_last_5m", "txns_last_5m", "txns_5m")
    min_txns = float(getattr(cfg, "SNIPER_RESEARCH_DEEP_REVERSAL_MIN_TXNS_5M", 500) or 500)
    mcap = _field_float(row, "buy_market_cap_usd", "market_cap_usd", "mcap")
    max_mcap = float(getattr(cfg, "SNIPER_RESEARCH_DEEP_REVERSAL_MAX_MCAP_USD", 25_000.0) or 25_000.0)
    if price5m < min_price or price5m > max_price:
        failures.append(f"deep_reversal:price5m_not_{min_price:g}_{max_price:g}")
    if txns < min_txns:
        failures.append(f"deep_reversal:txns5m<{min_txns:g}")
    if mcap <= 0 or mcap >= max_mcap:
        failures.append(f"deep_reversal:mcap>={max_mcap:g}")
    if not _route_ok(row):
        failures.append("deep_reversal:route_required")
    return failures


def evaluate_sniper_research_subprofile(
    row: dict[str, Any],
    *,
    cfg: Any = CFG,
) -> SniperResearchSubprofileDecision:
    if not bool(getattr(cfg, "SNIPER_RESEARCH_SUBPROFILES_ENABLED", True)):
        return SniperResearchSubprofileDecision(True, None, "subprofiles_disabled", ())
    if not _is_sniper_research(row):
        return SniperResearchSubprofileDecision(True, None, "not_sniper_research", ())

    failures: list[str] = []
    if bool(getattr(cfg, "SNIPER_RESEARCH_MOMENTUM_IGNITION_ENABLED", True)):
        momentum_failures = _momentum_ignition_failures(row, cfg=cfg)
        if not momentum_failures:
            return SniperResearchSubprofileDecision(True, SUBPROFILE_MOMENTUM_IGNITION, SUBPROFILE_MOMENTUM_IGNITION, ())
        price5m = _field_float(row, "buy_price_pct_5m", "price_pct_5m", "price5m")
        if price5m >= float(getattr(cfg, "SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M", 100) or 100):
            compact_momentum = tuple(dict.fromkeys(momentum_failures))
            return SniperResearchSubprofileDecision(
                False,
                None,
                "momentum_ignition_toxic_filter:" + ",".join(compact_momentum[:10]),
                compact_momentum,
            )
        failures.extend(momentum_failures)
    if bool(getattr(cfg, "SNIPER_RESEARCH_DEEP_REVERSAL_ENABLED", True)):
        deep_failures = _deep_reversal_failures(row, cfg=cfg)
        if not deep_failures:
            return SniperResearchSubprofileDecision(True, SUBPROFILE_DEEP_REVERSAL, SUBPROFILE_DEEP_REVERSAL, ())
        failures.extend(deep_failures)

    compact = tuple(dict.fromkeys(failures or ["no_subprofile_enabled"]))
    return SniperResearchSubprofileDecision(
        False,
        None,
        "sniper_research_subprofile_not_matched:" + ",".join(compact[:10]),
        compact,
    )


def apply_sniper_research_subprofile_context(
    row: dict[str, Any],
    decision: SniperResearchSubprofileDecision,
) -> dict[str, Any]:
    if decision.subprofile:
        row["sniper_research_subprofile"] = decision.subprofile
        row["entry_subprofile"] = decision.subprofile
        row["sniper_research_subprofile_reason"] = decision.reason
        row["sniper_research_subprofile_failures"] = ""
        row["green_sniper_reason"] = decision.reason
        if decision.subprofile == SUBPROFILE_DEEP_REVERSAL:
            row["exit_profile"] = "sniper_deep_reversal_defensive"
    else:
        row["sniper_research_subprofile_shadow"] = 1
        row["sniper_research_subprofile_reason"] = decision.reason
        row["sniper_research_subprofile_failures"] = ",".join(decision.failures)
        row["green_sniper_reason"] = decision.reason
    return row


def _pnl(row: dict[str, Any]) -> float:
    return fnum(_first(row, "realized_pnl_pct", "total_pnl_pct", "pnl_pct", "target_total_pnl_pct"), 0.0)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [_pnl(row) for row in rows]
    if not rows:
        return {"rows": 0, "win_rate_pct": 0.0, "avg_pnl_pct": 0.0, "median_pnl_pct": 0.0}
    return {
        "rows": len(rows),
        "win_rate_pct": round(100.0 * sum(1 for value in pnls if value > 0.0) / len(pnls), 3),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        "median_pnl_pct": round(statistics.median(pnls), 3),
        "total_pnl_pct_points": round(sum(pnls), 3),
    }


def build_sniper_research_subprofile_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = (
        load_runtime_events(root)
        + load_candidate_outcomes(root)
        + load_paper_positions(root)
        + load_sqlite_positions(root)
    )
    sniper_rows = [row for row in rows if _is_sniper_research(row)]
    by_subprofile: dict[str, list[dict[str, Any]]] = {}
    failures: dict[str, int] = {}
    for row in sniper_rows:
        decision = evaluate_sniper_research_subprofile(row)
        key = decision.subprofile or "shadow_subprofile_not_matched"
        by_subprofile.setdefault(key, []).append(row)
        if not decision.allowed:
            for failure in decision.failures:
                failures[failure] = failures.get(failure, 0) + 1
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "enabled": bool(getattr(CFG, "SNIPER_RESEARCH_SUBPROFILES_ENABLED", True)),
            "momentum_ignition_enabled": bool(getattr(CFG, "SNIPER_RESEARCH_MOMENTUM_IGNITION_ENABLED", True)),
            "deep_reversal_enabled": bool(getattr(CFG, "SNIPER_RESEARCH_DEEP_REVERSAL_ENABLED", True)),
            "momentum": {
                "price5m_min": float(getattr(CFG, "SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M", 100.0) or 100.0),
                "price5m_max": float(getattr(CFG, "SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M", 150.0) or 150.0),
                "min_liquidity_usd": float(getattr(CFG, "SNIPER_RESEARCH_MOMENTUM_MIN_LIQUIDITY_USD", 15_000.0) or 15_000.0),
                "min_txns_5m": int(getattr(CFG, "SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M", 500) or 500),
                "min_mcap_usd": float(getattr(CFG, "SNIPER_RESEARCH_MOMENTUM_MIN_MCAP_USD", 15_000.0) or 15_000.0),
                "max_mcap_usd": float(getattr(CFG, "SNIPER_RESEARCH_MOMENTUM_MAX_MCAP_USD", 70_000.0) or 70_000.0),
                "max_top10_share_pct": float(getattr(CFG, "SNIPER_RESEARCH_MOMENTUM_MAX_TOP10_SHARE_PCT", 40.0) or 40.0),
            },
            "deep_reversal": {
                "price5m_min": float(getattr(CFG, "SNIPER_RESEARCH_DEEP_REVERSAL_MIN_PRICE5M", -90.0) or -90.0),
                "price5m_max": float(getattr(CFG, "SNIPER_RESEARCH_DEEP_REVERSAL_MAX_PRICE5M", -50.0) or -50.0),
                "min_txns_5m": int(getattr(CFG, "SNIPER_RESEARCH_DEEP_REVERSAL_MIN_TXNS_5M", 500) or 500),
                "max_mcap_usd": float(getattr(CFG, "SNIPER_RESEARCH_DEEP_REVERSAL_MAX_MCAP_USD", 25_000.0) or 25_000.0),
            },
        },
        "summary": {
            "sniper_research_rows": len(sniper_rows),
            "matched_rows": sum(len(rows_) for key, rows_ in by_subprofile.items() if key != "shadow_subprofile_not_matched"),
            "shadow_rows": len(by_subprofile.get("shadow_subprofile_not_matched", [])),
        },
        "by_subprofile": {key: _summary(value) for key, value in sorted(by_subprofile.items())},
        "failure_counts": dict(sorted(failures.items(), key=lambda item: item[1], reverse=True)),
        "preview": [
            {
                "address": address_of(row),
                "subprofile": (evaluate_sniper_research_subprofile(row).subprofile or "shadow"),
                "reason": evaluate_sniper_research_subprofile(row).reason,
                "pnl_pct": _pnl(row),
                "entry_lane": row.get("entry_lane"),
                "gate_profile": row.get("gate_profile") or row.get("sniper_gate_profile"),
            }
            for row in sniper_rows[:100]
        ],
    }


def write_sniper_research_subprofile_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_sniper_research_subprofile_report(root)
    write_json(metrics_dir(root) / "sniper_research_subprofile_report.json", report)
    lines = [
        "# Sniper Research Subprofiles",
        "",
        "Only matched sniper research subprofiles are eligible for paper buys; all other sniper research candidates stay in shadow.",
        "",
        "| Subprofile | Rows | Win | Avg PnL | Median PnL |",
        "|---|---:|---:|---:|---:|",
    ]
    for key, stats in report["by_subprofile"].items():
        lines.append(
            f"| {key} | {stats['rows']} | {stats['win_rate_pct']:.2f}% | "
            f"{stats['avg_pnl_pct']:.2f}% | {stats['median_pnl_pct']:.2f}% |"
        )
    lines.extend(["", "## Top Failures", ""])
    for failure, count in report["failure_counts"].items():
        lines.append(f"- `{failure}`: {count}")
    write_markdown(root / "docs" / "SNIPER_RESEARCH_SUBPROFILES.md", lines)
    return report


__all__ = [
    "SUBPROFILE_DEEP_REVERSAL",
    "SUBPROFILE_MOMENTUM_IGNITION",
    "SniperResearchSubprofileDecision",
    "apply_sniper_research_subprofile_context",
    "build_sniper_research_subprofile_report",
    "evaluate_sniper_research_subprofile",
    "write_sniper_research_subprofile_report",
]
