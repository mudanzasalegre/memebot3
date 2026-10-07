from __future__ import annotations

from pathlib import Path
from typing import Any

from analytics.green_sniper_gate import evaluate_green_sniper
from analytics.report_utils import (
    address_of,
    bought_addresses,
    fnum,
    load_candidate_outcomes,
    metrics_dir,
    write_json,
    write_markdown,
)
from config.config import PROJECT_ROOT
from ml.labels import moonshot_execution_label
from ml.data_contract import (
    SAMPLE_GREEN_SNIPER_REJECT_SHADOW,
    SAMPLE_LATE_MOMENTUM_WATCH_SHADOW,
    SAMPLE_RESEARCH_RANK_SHADOW,
    SAMPLE_SHADOW_CLOSE,
    SAMPLE_TRADE_CLOSE,
    normalize_sample_type,
)


HOT_SEEN_PRICE5M_PCT = 100.0
AVOIDED_LOSER_PNL_PCT = -20.0
CONFIRMED_OUTCOME_SAMPLE_TYPES = {
    SAMPLE_TRADE_CLOSE,
    SAMPLE_SHADOW_CLOSE,
    SAMPLE_GREEN_SNIPER_REJECT_SHADOW,
    SAMPLE_LATE_MOMENTUM_WATCH_SHADOW,
    SAMPLE_RESEARCH_RANK_SHADOW,
}


def _reason(row: dict[str, Any]) -> str:
    candidates = (
        row.get("final_blocking_reason"),
        row.get("rule_that_blocked"),
        row.get("reject_reason"),
        row.get("delay_reason"),
        row.get("shadow_reason"),
        row.get("reason"),
    )
    for value in candidates:
        raw = str(value or "").strip()
        if raw and raw.lower() != "late_funnel":
            return raw
    stage = str(row.get("stage") or "").strip()
    if stage and stage.lower() != "late_funnel":
        return stage
    return "unknown"


def _outcome_confirmed(row: dict[str, Any]) -> bool:
    raw = str(row.get("outcome_confirmed") or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    return normalize_sample_type(row.get("sample_type")) in CONFIRMED_OUTCOME_SAMPLE_TYPES


def _confirmed_peak(row: dict[str, Any]) -> float | None:
    if not _outcome_confirmed(row):
        return None
    fields = (
        "shadow_max_pnl_pct_seen",
        "max_pnl_pct_seen",
        "max_pnl_pct",
        "peak_pnl_pct",
        "target_total_pnl_pct",
        "shadow_outcome_pnl_pct",
        "pnl_pct",
    )
    values = [fnum(row.get(field), float("nan")) for field in fields if row.get(field) is not None]
    values = [value for value in values if value == value]
    if not values:
        return None
    return max(values)


def _classification(row: dict[str, Any], *, min_pnl_pct: float) -> str:
    confirmed = _confirmed_peak(row)
    if confirmed is not None and confirmed >= min_pnl_pct:
        return "confirmed_missed_winner"
    if confirmed is not None and confirmed <= AVOIDED_LOSER_PNL_PCT:
        return "confirmed_avoided_loser"
    if fnum(row.get("price_pct_5m"), 0.0) >= HOT_SEEN_PRICE5M_PCT:
        return "hot_seen_not_bought"
    return "unresolved_hot_candidate"


def _row_peak_for_dedupe(row: dict[str, Any]) -> float:
    confirmed = _confirmed_peak(row)
    if confirmed is not None:
        return float(confirmed)
    fields = ("shadow_max_pnl_pct_seen", "max_pnl_pct_seen", "max_pnl_pct", "peak_pnl_pct", "target_total_pnl_pct")
    values = [fnum(row.get(field), float("nan")) for field in fields if row.get(field) is not None]
    values = [value for value in values if value == value]
    return max(values) if values else fnum(row.get("price_pct_5m"), 0.0)


def _dedupe_candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []
    for row in rows:
        addr = address_of(row).strip().lower()
        if not addr:
            passthrough.append(row)
            continue
        grouped.setdefault(addr, []).append(row)

    deduped: list[dict[str, Any]] = []
    for group in grouped.values():
        winner = max(
            group,
            key=lambda item: (
                _row_peak_for_dedupe(item),
                fnum(item.get("price_pct_5m"), 0.0),
                1 if _outcome_confirmed(item) else 0,
            ),
        )
        merged = dict(winner)
        for row in group:
            for key, value in row.items():
                if merged.get(key) is None or (isinstance(merged.get(key), str) and not str(merged.get(key)).strip()):
                    merged[key] = value
        deduped.append(merged)
    return deduped + passthrough


def build_missed_pumps(
    root: Path | None = None,
    *,
    min_pnl_pct: float = 100.0,
    include_unconfirmed_hot: bool = True,
) -> list[dict[str, Any]]:
    root = root or PROJECT_ROOT
    bought = bought_addresses(root)
    rows: list[dict[str, Any]] = []
    for row in _dedupe_candidate_rows(load_candidate_outcomes(root)):
        addr = address_of(row)
        if not addr or addr in bought:
            continue
        classification = _classification(row, min_pnl_pct=min_pnl_pct)
        if classification == "unresolved_hot_candidate" and not include_unconfirmed_hot:
            continue
        confirmed_peak = _confirmed_peak(row)
        decision = evaluate_green_sniper(dict(row), dry_run=True, live=False)
        moonshot = moonshot_execution_label(row)
        rows.append(
            {
                "address": addr,
                "symbol": row.get("symbol"),
                "source": row.get("source") or row.get("discovered_via"),
                "first_seen_at": row.get("first_seen_at") or row.get("timestamp") or row.get("ts_utc"),
                "age_at_seen": row.get("age_minutes") or row.get("age_min"),
                "price_pct_5m_at_seen": row.get("price_pct_5m"),
                "txns_5m_at_seen": row.get("txns_last_5m"),
                "liquidity_at_seen": row.get("liquidity_usd"),
                "mcap_at_seen": row.get("market_cap_usd"),
                "route_at_seen": row.get("has_jupiter_route"),
                "reject_reason": row.get("reject_reason"),
                "delay_reason": row.get("delay_reason"),
                "shadow_reason": row.get("shadow_reason") or row.get("reason"),
                "would_green_sniper_pass": decision.action == "buy",
                "rule_that_blocked": _reason(row),
                "green_sniper_reason": decision.reason,
                "classification": classification,
                "theoretical_moonshot": bool(moonshot["theoretical_moonshot"]),
                "executable_moonshot": bool(moonshot["executable_moonshot"]),
                "moonshot_viability": moonshot["moonshot_viability"],
                "moonshot_blocker": moonshot["moonshot_blocker"],
                "moonshot_decision_reason": moonshot["moonshot_decision_reason"],
                "moonshot_route_viability": moonshot["moonshot_route_viability"],
                "moonshot_liquidity_viability": moonshot["moonshot_liquidity_viability"],
                "moonshot_cluster_viability": moonshot["moonshot_cluster_viability"],
                "moonshot_time_to_peak_min": moonshot["moonshot_time_to_peak_min"],
                "moonshot_time_to_peak_source": moonshot["moonshot_time_to_peak_source"],
                "moonshot_peak_pct": moonshot["moonshot_peak_pct"],
                "moonshot_amount_sol": moonshot["moonshot_amount_sol"],
                "moonshot_paper_only": moonshot["moonshot_paper_only"],
                "observed_peak_after_seen_pct": row.get("max_pnl_pct_seen") or row.get("max_pnl_pct") or row.get("peak_pnl_pct"),
                "shadow_outcome_pnl_pct": row.get("shadow_outcome_pnl_pct") or row.get("pnl_pct"),
                "shadow_max_pnl_pct_seen": row.get("shadow_max_pnl_pct_seen") or row.get("max_pnl_pct_seen"),
                "trade_outcome_pnl_pct": row.get("trade_outcome_pnl_pct"),
                "confirmed_later_peak_pct": confirmed_peak,
                "later_max_pnl_pct": confirmed_peak,
                "outcome_confirmed": _outcome_confirmed(row),
            }
        )
    order = {
        "confirmed_missed_winner": 0,
        "hot_seen_not_bought": 1,
        "confirmed_avoided_loser": 2,
        "unresolved_hot_candidate": 3,
    }
    return sorted(
        rows,
        key=lambda item: (
            order.get(str(item["classification"]), 9),
            -fnum(item.get("confirmed_later_peak_pct"), 0.0),
            -fnum(item.get("price_pct_5m_at_seen"), 0.0),
        ),
    )


def write_missed_pumps_report(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or PROJECT_ROOT
    rows = build_missed_pumps(root)
    write_json(metrics_dir(root) / "missed_pumps.json", rows)
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("classification") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    lines = [
        "# Missed Pumps Report",
        "",
        "This report separates hot momentum already visible at evaluation time from confirmed later outcomes. `price_pct_5m_at_seen` is never used as a later peak.",
        "",
        "| Classification | Count |",
        "|---|---:|",
    ]
    for key in ("confirmed_missed_winner", "hot_seen_not_bought", "confirmed_avoided_loser", "unresolved_hot_candidate"):
        lines.append(f"| {key} | {counts.get(key, 0)} |")
    lines.extend(
        [
            "",
            "| Address | Class | Confirmed later peak | Price5m at seen | Rule blocked | Would green sniper pass |",
            "|---|---|---:|---:|---|---|",
        ]
    )
    for row in rows[:100]:
        confirmed = row.get("confirmed_later_peak_pct")
        confirmed_txt = "n/a" if confirmed is None else f"{fnum(confirmed):.2f}%"
        lines.append(
            f"| {str(row['address'])[:10]}... | {row['classification']} | {confirmed_txt} | "
            f"{fnum(row.get('price_pct_5m_at_seen')):.2f}% | {row['rule_that_blocked']} | {row['would_green_sniper_pass']} |"
        )
    moonshot_rows = [row for row in rows if row.get("theoretical_moonshot")]
    if moonshot_rows:
        lines.extend(
            [
                "",
                "## Moonshot executable labels",
                "",
                "| Address | Peak | Executable | Viability | Blocker | Route | Time to peak |",
                "|---|---:|---|---|---|---|---:|",
            ]
        )
        for row in moonshot_rows[:100]:
            time_to_peak = row.get("moonshot_time_to_peak_min")
            time_txt = "n/a" if time_to_peak is None else f"{fnum(time_to_peak):.2f}m"
            lines.append(
                f"| {str(row['address'])[:10]}... | {fnum(row.get('moonshot_peak_pct')):.2f}% | "
                f"{row.get('executable_moonshot')} | {row.get('moonshot_viability')} | "
                f"{row.get('moonshot_blocker')} | {row.get('moonshot_route_viability')} | {time_txt} |"
            )
    write_markdown(root / "docs" / "MISSED_PUMPS_REPORT.md", lines)
    return rows


__all__ = ["build_missed_pumps", "write_missed_pumps_report"]
