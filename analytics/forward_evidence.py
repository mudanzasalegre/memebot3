"""Prospective paper evidence, kept separate from retrospective policy replay."""
from __future__ import annotations

import datetime as dt
import math
import hashlib
import random
import statistics
from pathlib import Path
from typing import Any

from analytics.current_run import parse_time
from analytics.report_utils import load_paper_positions, load_sqlite_positions, read_jsonl


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _true(value: Any) -> bool:
    return value is True or (type(value) is int and value == 1) or (isinstance(value, str) and value.lower() in {"true", "1"})


def _costed_close(row: dict[str, Any]) -> tuple[float, float, float, float, bool] | None:
    """Check estimated cash accounting, not merely the presence of a cost label."""
    model = row.get("execution_cost_model")
    if (not isinstance(model, dict) or model.get("version") != "estimated-v1"
            or model.get("observed_execution") is not False):
        return None
    names = ("net_total_pnl_usd", "net_total_pnl_pct", "total_pnl_usd", "entry_notional_usd",
             "estimated_fees_usd", "estimated_fees_sol", "execution_fill_count", "net_total_pnl_sol")
    values = [_finite(row.get(name)) for name in names]
    if any(value is None for value in values):
        return None
    pnl, pct, gross, notional, fees, fees_sol, fills, net_sol = values
    amount = _finite(row.get("amount_sol", row.get("buy_amount_sol")))
    qty = _finite(row.get("entry_qty"))
    remaining = _finite(row.get("qty_lamports", row.get("qty")))
    slippage, fee_per_fill = _finite(model.get("slippage_bps")), _finite(model.get("fee_sol_per_fill"))
    if (notional <= 0 or amount is None or amount <= 0 or qty is None or qty <= 0 or remaining != 0
            or fills < 2 or fills != int(fills) or fees < 0 or fees_sol < 0
            or slippage is None or not 0 <= slippage < 10000 or fee_per_fill is None or fee_per_fill < 0):
        return None
    if not (math.isclose(pnl, gross - fees, rel_tol=1e-7, abs_tol=1e-8)
            and math.isclose(pct, 100 * pnl / notional, rel_tol=1e-7, abs_tol=1e-7)
            and math.isclose(fees_sol, fills * fee_per_fill, rel_tol=1e-7, abs_tol=1e-10)):
        return None
    if fees_sol > 0 and fees <= 0:
        return None
    if row.get("total_proceeds_sol") is not None:
        proceeds_sol = _finite(row.get("total_proceeds_sol"))
        if proceeds_sol is None or not math.isclose(net_sol, proceeds_sol - amount - fees_sol,
                                                   rel_tol=1e-7, abs_tol=1e-10):
            return None
    quote = row.get("entry_route_quote")
    executable = False
    if isinstance(quote, dict):
        incoming, outgoing = _finite(quote.get("in_amount")), _finite(quote.get("out_amount"))
        impact, limit = _finite(quote.get("impact_bps")), _finite(quote.get("max_impact_pct"))
        executable = (incoming == int(amount * 1e9) and outgoing is not None and outgoing > 0
                      and impact is not None and limit is not None and limit >= 0 and abs(impact) / 100 <= limit
                      and qty == int(outgoing / (1 + slippage / 10000))
                      and row.get("quantity_basis") == "quoted_raw_spl_units"
                      and row.get("price_source_close") == "jupiter_reverse_quote")
    return pnl, pct, net_sol, amount, bool(executable)


def collect_forward_evidence(root: Path, *, run_id: str | None, started_at: Any,
                             config_hash: str | None = None, profile: str | None = None) -> dict[str, Any]:
    started = parse_time(started_at)
    now = dt.datetime.now(dt.timezone.utc)
    reasons = []
    if not run_id:
        reasons.append("forward_run_id_missing")
    if started is None:
        reasons.append("prospective_start_missing")
    if not config_hash and not profile:
        reasons.append("candidate_identity_missing")
    if started is not None and started > now:
        reasons.append("prospective_start_in_future")
    identity_invalid = bool(reasons)
    rows = (read_jsonl(root / "data" / "paper_closed_trades.jsonl") + load_paper_positions(root)
            + load_sqlite_positions(root))
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        opened, closed = parse_time(row.get("opened_at")), parse_time(row.get("closed_at"))
        if identity_invalid or not _true(row.get("dry_run")) or _true(row.get("test_event")) or row.get("run_id") == "SMOKE":
            continue
        if row.get("run_id") != run_id or opened is None or opened < started or opened > now:
            continue
        if config_hash and row.get("config_hash") != config_hash:
            continue
        if profile and row.get("config_profile") != profile:
            continue
        # Solana addresses are case sensitive. Open time identifies repeat buys.
        address = row.get("token_address") or row.get("address")
        if not isinstance(address, str) or not address.strip():
            reasons.append("forward_trade_identity_missing")
            continue
        grouped.setdefault((address, opened.isoformat()), []).append(row)
    selected = {}
    open_positions = set()
    uncosted = 0
    for key, snapshots in grouped.items():
        terminal = [row for row in snapshots if _true(row.get("closed"))]
        if not terminal:
            open_positions.add(key)
            continue
        verified = []
        for row in terminal:
            closed, opened = parse_time(row.get("closed_at")), parse_time(row.get("opened_at"))
            values = _costed_close(row)
            if closed is not None and opened is not None and opened <= closed <= now and values is not None:
                verified.append((closed, *values, row))
        if not verified:
            uncosted += 1  # one trade, not one JSON/SQLite/portfolio copy
            continue
        first = verified[0]
        if any(other[0] != first[0] or any(not math.isclose(other[i], first[i], rel_tol=1e-7, abs_tol=1e-8)
                                          for i in range(1, 5)) for other in verified[1:]):
            reasons.append("conflicting_costed_trade_records")
            uncosted += 1
            continue
        selected[key] = max(verified, key=lambda item: item[5])
        # A terminal close supersedes older open/partial snapshots of that trade.
    ordered = sorted(selected.values(), key=lambda item: item[0])
    buys_by_day: dict[str, int] = {}
    for key in grouped:
        day = key[1][:10]
        buys_by_day[day] = buys_by_day.get(day, 0) + 1
    pnls = [item[1] for item in ordered]
    pcts = [item[2] for item in ordered]
    net_sols = [item[3] for item in ordered]
    amounts = [item[4] for item in ordered]
    executable = sum(item[5] for item in ordered)
    token_returns: dict[str, list[float]] = {}
    for key, item in selected.items():
        token_returns.setdefault(key[0], []).append(item[2])
    cluster_means = [statistics.mean(returns) for returns in token_returns.values()]
    bootstrap_lower = None
    if len(cluster_means) >= 2:
        seed = hashlib.sha256(repr(sorted(selected)).encode()).digest()
        rng = random.Random(seed)
        samples = sorted(statistics.mean(rng.choices(cluster_means, k=len(cluster_means))) for _ in range(1000))
        bootstrap_lower = samples[24]  # deterministic 2.5th percentile diagnostic
    peak_returns = []
    for item in ordered:
        record = item[6]
        peak_return = next((_finite(record[name]) for name in ("max_pnl_pct_seen", "highest_pnl_pct", "peak_pnl_pct")
                            if name in record and _finite(record[name]) is not None), None)
        if peak_return is not None and peak_return >= 100:
            peak_returns.append((peak_return, item[2]))
    wins, losses = sum(max(p, 0) for p in pnls), -sum(min(p, 0) for p in pnls)
    cumulative = peak = drawdown = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    latest = ordered[-1][0] if ordered else None
    observed_start = min((parse_time(key[1]) for key in selected), default=None)
    lower = statistics.mean(pcts) - 1.96 * statistics.stdev(pcts) / math.sqrt(len(pcts)) if len(pcts) > 1 else None
    return {
        "evidence_rejections": sorted(set(reasons)), "run_id": run_id, "cost_basis": "estimated_paper_fills_not_live",
        "config_hash": config_hash, "profile": profile,
        "window_started_at": started.isoformat() if started else None,
        "evidence_schema": "cost_checked_terminal_trades_v2",
        "closed_trades": len(pnls), "uncosted_records": uncosted,
        "open_positions": len(open_positions),
        "max_daily_buys_observed": max(buys_by_day.values(), default=0),
        "quote_backed_closed_trades": executable,
        "non_quote_backed_closed_trades": len(ordered) - executable,
        "elapsed_hours": max(0, (latest - observed_start).total_seconds() / 3600) if latest and observed_start else 0,
        "observed_trade_window_started_at": observed_start.isoformat() if observed_start else None,
        "last_closed_at": latest.isoformat() if latest else None,
        "last_close_age_hours": max(0, (now - latest).total_seconds() / 3600) if latest else None,
        "total_pnl_usd": sum(pnls) if pnls else None,
        "total_pnl_sol": sum(net_sols) if net_sols else None,
        "committed_capital_sol": sum(amounts) if amounts else None,
        "net_return_on_committed_capital_pct": 100 * sum(net_sols) / sum(amounts) if amounts else None,
        "runner_capture_ratio": statistics.mean(net / peak for peak, net in peak_returns) if peak_returns else None,
        "runner_capture_denominator": len(peak_returns),
        "moonshot_peak100_capture": sum(peak >= 100 and net > 0 for peak, net in peak_returns),
        "moonshot_peak500_capture": sum(peak >= 500 and net > 0 for peak, net in peak_returns),
        "moonshot_peak1000_capture": sum(peak >= 1000 and net > 0 for peak, net in peak_returns),
        "avg_pnl_pct": statistics.mean(pcts) if pcts else None,
        "median_pnl_pct": statistics.median(pcts) if pcts else None,
        "severe_loss_count": sum(item[2] <= -25 or str(item[6].get("exit_reason", "")).upper() in {
            "LIQUIDITY_CRUSH", "STOP_LOSS", "EARLY_DROP", "ADVERSE_TICK", "EARLY_DUMP_CUT"} for item in ordered),
        "liquidity_crush_count": sum(str(item[6].get("exit_reason", "")).upper() == "LIQUIDITY_CRUSH" for item in ordered),
        "adverse_tick_count": sum(str(item[6].get("exit_reason", "")).upper() == "ADVERSE_TICK" for item in ordered),
        "win_rate_pct": 100 * sum(p > 0 for p in pnls) / len(pnls) if pnls else None,
        "profit_factor": wins / losses if losses else None,
        "loss_usd": losses, "win_usd": wins,
        "mean_return_lower_95_normal_approx_pct": lower,
        "token_cluster_mean_return_lower_bootstrap_diagnostic_pct": bootstrap_lower,
        "distinct_closed_tokens": len(cluster_means),
        "bootstrap_replicates": 1000 if bootstrap_lower is not None else 0,
        "max_drawdown_proxy": drawdown,
    }


def forward_acceptance(evidence: dict[str, Any], *, min_closed: int = 50, min_hours: float = 24) -> dict[str, Any]:
    reasons = list(evidence.get("evidence_rejections") or [])
    if evidence.get("uncosted_records"):
        reasons.append("unknown_execution_costs")
    if evidence.get("open_positions"):
        reasons.append("unsettled_paper_positions")
    if evidence.get("non_quote_backed_closed_trades"):
        reasons.append("paper_fills_without_exact_quote_evidence")
    if evidence.get("evidence_schema") != "cost_checked_terminal_trades_v2":
        reasons.append("forward_cost_validation_missing")
    closed = _finite(evidence.get("closed_trades")) or 0
    if evidence.get("quote_backed_closed_trades") != closed:
        reasons.append("paper_exact_quote_coverage_incomplete")
    if closed < min_closed:
        reasons.append("insufficient_forward_trades")
    if (_finite(evidence.get("elapsed_hours")) or 0) < min_hours:
        reasons.append("insufficient_observed_window")
    if (_finite(evidence.get("total_pnl_usd")) or 0) <= 0:
        reasons.append("nonpositive_net_pnl")
    lower = _finite(evidence.get("mean_return_lower_95_normal_approx_pct"))
    if lower is None or lower <= 0:
        reasons.append("positive_expectancy_not_established")
    cluster_lower = _finite(evidence.get("token_cluster_mean_return_lower_bootstrap_diagnostic_pct"))
    if cluster_lower is None or cluster_lower <= 0 or (_finite(evidence.get("distinct_closed_tokens")) or 0) < 20:
        reasons.append("token_cluster_expectancy_not_established")
    age = _finite(evidence.get("last_close_age_hours"))
    if age is None or age > 24 or age < 0:
        reasons.append("stale_forward_trade_evidence")
    return {"passed": not reasons, "rejection_reasons": reasons,
            "min_closed_trades": min_closed, "min_observed_hours": min_hours,
            "limitations": ["Estimated costs do not prove executable live fills.",
                            "The normal-approximation bound is a diagnostic, not a guarantee for heavy-tailed returns.",
                            "Token-cluster bootstrap is a diagnostic; it does not remove market-regime or time dependence.",
                            "Live activation still requires manual approval and a separate execution-risk review."]}
