from __future__ import annotations

import bisect
import collections
import datetime as dt
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from analytics.report_utils import write_json
from config.config import PROJECT_ROOT


@dataclass(frozen=True)
class ForwardPaperCriteria:
    amount_sol: float = 0.1
    dex_id: str = "pumpswap"
    max_age_min: float = 60.0
    max_queue_age_min: float = 15.0
    min_liquidity_usd: float = 20_000.0
    min_market_cap_usd: float = 50_000.0
    min_txns_5m: int = 100
    min_score_total: int = 50
    max_price_impact_pct: float = 12.0
    negative_price5m_min_pct: float = -25.0
    negative_price5m_min_txns_5m: int = 1_000
    negative_price5m_min_market_cap_usd: float = 90_000.0
    negative_price5m_max_market_cap_usd: float = 250_000.0


def _parse_time(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _int(value: Any) -> int | None:
    parsed = _float(value)
    return int(parsed) if parsed is not None else None


def _rows(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(row, dict):
                yield row


def _value(row: dict[str, Any], *keys: str) -> Any:
    snapshots = (
        row,
        row.get("feature_snapshot") if isinstance(row.get("feature_snapshot"), dict) else {},
        row.get("features_snapshot") if isinstance(row.get("features_snapshot"), dict) else {},
    )
    for source in snapshots:
        for key in keys:
            value = source.get(key)
            if value is not None and value != "":
                return value
    return None


def _summary(rows: list[dict[str, Any]], *, amount_sol: float) -> dict[str, Any]:
    values = [float(row["pnl_pct"]) for row in rows]
    positives = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    return {
        "outcomes": len(values),
        "win_count": sum(value > 0 for value in values),
        "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100.0, 6) if values else 0.0,
        "avg_pnl_pct": round(sum(values) / len(values), 6) if values else 0.0,
        "sum_pnl_pct_points": round(sum(values), 6),
        "profit_factor": round(positives / losses, 6) if losses > 0 else None,
        "hypothetical_pnl_sol_at_fixed_size_before_costs": round(sum(values) * float(amount_sol) / 100.0, 9),
    }


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lo = math.floor(index)
    hi = math.ceil(index)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - index) + ordered[hi] * (index - lo)


def _base_match(row: dict[str, Any], criteria: ForwardPaperCriteria) -> bool:
    dex_id = str(row.get("dex_id") or "").strip().lower()
    return dex_id == criteria.dex_id and _bool(row.get("has_jupiter_route")) is True and _bool(row.get("cluster_bad")) is not True


def _forward_match(row: dict[str, Any], criteria: ForwardPaperCriteria) -> bool:
    if not _base_match(row, criteria):
        return False
    required = ("age_minutes", "liquidity_usd", "market_cap_usd", "txns_last_5m", "score_total")
    if any(_float(row.get(key)) is None for key in required):
        return False
    queue_age = _float(row.get("queue_age_minutes"))
    impact = _float(row.get("price_impact_pct"))
    if _float(row["age_minutes"]) > criteria.max_age_min:
        return False
    if queue_age is not None and queue_age > criteria.max_queue_age_min:
        return False
    if _float(row["liquidity_usd"]) < criteria.min_liquidity_usd:
        return False
    if _float(row["market_cap_usd"]) < criteria.min_market_cap_usd:
        return False
    if _float(row["txns_last_5m"]) < criteria.min_txns_5m:
        return False
    if _float(row["score_total"]) < criteria.min_score_total:
        return False
    if impact is not None and impact > criteria.max_price_impact_pct:
        return False
    price5m = _float(row.get("price_pct_5m"))
    if price5m is not None and price5m < 0:
        mcap = _float(row["market_cap_usd"])
        return (
            price5m >= criteria.negative_price5m_min_pct
            and _float(row["txns_last_5m"]) >= criteria.negative_price5m_min_txns_5m
            and criteria.negative_price5m_min_market_cap_usd <= mcap <= criteria.negative_price5m_max_market_cap_usd
        )
    return True


def build_causal_shadow_policy_audit(
    root: Path | None = None,
    *,
    run_id: str,
    criteria: ForwardPaperCriteria | None = None,
) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    criteria = criteria or ForwardPaperCriteria()
    metrics = root / "data" / "metrics"
    ledger_path = metrics / "decision_ledger.jsonl"
    outcomes_path = metrics / "candidate_outcomes.normalized.jsonl"

    ledger: dict[str, list[tuple[float, dict[str, Any]]]] = collections.defaultdict(list)
    ledger_rows = 0
    for row in _rows(ledger_path):
        if str(row.get("run_id") or "") != run_id:
            continue
        address = str(row.get("address") or row.get("mint") or "").strip()
        timestamp = _parse_time(row.get("ts_utc") or row.get("timestamp"))
        if not address or timestamp is None:
            continue
        ledger[address].append((timestamp, row))
        ledger_rows += 1
    for values in ledger.values():
        values.sort(key=lambda item: item[0])

    outcomes: list[dict[str, Any]] = []
    unmatched = 0
    for row in _rows(outcomes_path):
        if str(row.get("run_id") or "") != run_id:
            continue
        if row.get("event_type") != "candidate_outcome" or row.get("source") != "research_shadow":
            continue
        pnl_pct = _float(row.get("pnl_pct"))
        opened_at = _parse_time(row.get("opened_at"))
        address = str(row.get("address") or row.get("mint") or "").strip()
        if pnl_pct is None or opened_at is None or not address:
            continue
        candidates = ledger.get(address, [])
        index = bisect.bisect_right([item[0] for item in candidates], opened_at) - 1
        if index < 0:
            unmatched += 1
            continue
        decision_ts, decision = candidates[index]
        outcomes.append(
            {
                "address": address,
                "symbol": _value(decision, "symbol") or row.get("symbol"),
                "opened_at": row.get("opened_at"),
                "day_utc": str(row.get("opened_at"))[:10],
                "pnl_pct": pnl_pct,
                "causal_lag_seconds": max(0.0, opened_at - decision_ts),
                "dex_id": str(_value(decision, "dex_id", "dexId", "dex") or "").strip().lower(),
                "has_jupiter_route": _bool(_value(decision, "has_jupiter_route", "route_ok")),
                "cluster_bad": _bool(_value(decision, "cluster_bad", "helius_cluster_bad")),
                "liquidity_is_proxy": _bool(_value(decision, "liquidity_is_proxy", "liquidity_usd_is_proxy")),
                "observed_optional_snapshot_missing_fields": _int(
                    _value(decision, "snapshot_missing_fields")
                ),
                "age_minutes": _float(_value(decision, "age_minutes", "age_min")),
                "queue_age_minutes": _float(_value(decision, "queue_age_minutes")),
                "liquidity_usd": _float(_value(decision, "liquidity_usd")),
                "market_cap_usd": _float(_value(decision, "market_cap_usd", "mcap")),
                "txns_last_5m": _float(_value(decision, "txns_last_5m", "txns_5m")),
                "score_total": _float(_value(decision, "score_total")),
                "price_impact_pct": _float(_value(decision, "price_impact_pct")),
                "price_pct_5m": _float(_value(decision, "price_pct_5m")),
            }
        )

    base = [row for row in outcomes if _base_match(row, criteria)]
    selected = [row for row in base if _forward_match(row, criteria)]
    selected_by_day = []
    for day, day_rows in sorted(collections.defaultdict(list, {day: [row for row in selected if row["day_utc"] == day] for day in {row["day_utc"] for row in selected}}).items()):
        selected_by_day.append({"day_utc": day, **_summary(day_rows, amount_sol=criteria.amount_sol)})
    lags = [float(row["causal_lag_seconds"]) for row in outcomes]
    unknown_proxy = sum(row.get("liquidity_is_proxy") is None for row in selected)
    optional_missing_values = [
        int(row["observed_optional_snapshot_missing_fields"])
        for row in selected
        if row.get("observed_optional_snapshot_missing_fields") is not None
    ]

    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_id": run_id,
        "sources": [
            str(ledger_path.relative_to(root)),
            str(outcomes_path.relative_to(root)),
        ],
        "criteria": asdict(criteria),
        "join_quality": {
            "ledger_rows_in_run": ledger_rows,
            "matched_terminal_shadow_outcomes": len(outcomes),
            "unmatched_terminal_shadow_outcomes": unmatched,
            "causal_lag_p50_seconds": round(_quantile(lags, 0.5) or 0.0, 6),
            "causal_lag_p90_seconds": round(_quantile(lags, 0.9) or 0.0, 6),
            "causal_lag_max_seconds": round(max(lags), 6) if lags else None,
        },
        "all_terminal_shadows": _summary(outcomes, amount_sol=criteria.amount_sol),
        "pumpswap_route_no_cluster": _summary(base, amount_sol=criteria.amount_sol),
        "forward_paper_candidate": {
            **_summary(selected, amount_sol=criteria.amount_sol),
            "selected_by_day": selected_by_day,
            "unknown_historical_proxy_flags": unknown_proxy,
            "optional_snapshot_missing_fields_diagnostic": {
                "selection_gate": False,
                "meaning": (
                    "snapshot_missing_fields counts optional holder/rug/social enrichments; "
                    "mandatory price/liquidity/market-cap/transaction fields are gated separately"
                ),
                "known_samples": len(optional_missing_values),
                "unknown_samples": len(selected) - len(optional_missing_values),
                "min_observed": min(optional_missing_values) if optional_missing_values else None,
                "max_observed": max(optional_missing_values) if optional_missing_values else None,
            },
            "requires_explicit_non_proxy_forward": True,
            "samples": selected,
        },
        "decision": {
            "forward_paper_candidate": bool(selected),
            "acceptance_grade": False,
            "live_ready": False,
            "status": "PAPER_ONLY_NO_GO_LIVE",
            "reasons": [
                "retrospective post-hoc sample is only seven outcomes",
                "historical explicit false proxy flags were lost by telemetry and cannot be verified",
                "results exclude execution costs, slippage variance, and fill failures",
                "event replay produced no simulated buys and was not acceptance-ready",
            ],
        },
    }


def write_causal_shadow_policy_audit(
    root: Path | None = None,
    *,
    run_id: str,
    criteria: ForwardPaperCriteria | None = None,
) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_causal_shadow_policy_audit(root, run_id=run_id, criteria=criteria)
    write_json(root / "data" / "metrics" / f"causal_shadow_policy_audit_{run_id}.json", report)
    return report


__all__ = [
    "ForwardPaperCriteria",
    "build_causal_shadow_policy_audit",
    "write_causal_shadow_policy_audit",
]
