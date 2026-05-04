from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from analytics.report_utils import mcap_bucket, price5m_bucket, rank_bucket
from config.config import PROJECT_ROOT


BASELINE_JSON = Path("data/metrics/post_run_48h_baseline.json")
BASELINE_DOC = Path("docs/POST_RUN_48H_BASELINE.md")
STATE_PATH = Path("data/metrics/post_partial_experiment.state.json")
CANDIDATE_OUTCOMES_PATH = Path("data/metrics/candidate_outcomes.jsonl")
SQLITE_PATH = Path("data/memebotdatabase.db")
PAPER_PORTFOLIO_PATH = Path("data/paper_portfolio.json")

SEVERE_LOSS_PNL_PCT = -25.0


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
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


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
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
        if isinstance(value, str) and not value.strip():
            return default
        return int(float(value))
    except Exception:
        return default


def _boolish(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _parse_timestamp(value: Any) -> str | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except Exception:
            return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _label(value: Any) -> str:
    raw = str(value or "").strip()
    return raw if raw else "null"


def _address(row: dict[str, Any]) -> str:
    return str(row.get("address") or row.get("token_address") or row.get("token_mint") or row.get("mint") or "").strip()


def _candidate_enrichment(root: Path) -> dict[str, dict[str, Any]]:
    keep = {
        "entry_lane",
        "gate_profile",
        "profit_lane_tier",
        "entry_subtype",
        "rank_score",
        "research_rank_score",
        "price_pct_5m",
        "buy_price_pct_5m",
        "market_cap_usd",
        "buy_market_cap_usd",
        "liquidity_usd",
        "buy_liquidity_usd",
        "liquidity_is_proxy",
        "liquidity_usd_is_proxy",
        "buy_liquidity_is_proxy",
        "txns_last_5m",
        "buy_txns_last_5m",
        "has_jupiter_route",
    }
    by_address: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(root / CANDIDATE_OUTCOMES_PATH):
        address = _address(row)
        if not address:
            continue
        current = by_address.setdefault(address, {})
        for key in keep:
            value = row.get(key)
            if value is not None and not (isinstance(value, str) and not value.strip()):
                current[key] = value
    return by_address


def _load_state_baseline_keys(root: Path) -> tuple[set[str], dict[str, Any] | None]:
    state = _read_json(root / STATE_PATH)
    if not isinstance(state, dict):
        return set(), None
    return {str(item) for item in (state.get("baseline_row_keys") or [])}, state


def _sqlite_available(root: Path) -> bool:
    db_path = root / SQLITE_PATH
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='positions'"
            ).fetchone()
        return bool(row)
    except Exception:
        return False


def _load_sqlite_rows(root: Path, enrichment: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    db_path = root / SQLITE_PATH
    rows: list[dict[str, Any]] = []
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        raw_rows = conn.execute("SELECT * FROM positions WHERE closed = 1 ORDER BY closed_at ASC, id ASC").fetchall()
    for raw in raw_rows:
        row = dict(raw)
        address = _address(row)
        rows.append(_normalize_trade(row, enrichment.get(address, {}), source="sqlite"))
    return rows


def _load_paper_rows(root: Path, enrichment: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    payload = _read_json(root / PAPER_PORTFOLIO_PATH)
    raw_rows = payload.get("positions") if isinstance(payload, dict) and "positions" in payload else payload
    if isinstance(raw_rows, dict):
        iterable: Iterable[tuple[str, Any]] = raw_rows.items()
    elif isinstance(raw_rows, list):
        iterable = [(str(idx), row) for idx, row in enumerate(raw_rows)]
    else:
        iterable = []
    rows: list[dict[str, Any]] = []
    for key, raw in iterable:
        if not isinstance(raw, dict) or not _boolish(raw.get("closed")):
            continue
        address = _address(raw) or str(key)
        row = dict(raw)
        row.setdefault("address", address)
        rows.append(_normalize_trade(row, enrichment.get(address, {}), source="paper"))
    rows.sort(key=lambda item: (str(item.get("closed_at") or ""), str(item.get("row_key") or "")))
    return rows


def _normalize_trade(row: dict[str, Any], enrich: dict[str, Any], *, source: str) -> dict[str, Any]:
    address = _address(row)
    trade_id = row.get("id") if source == "sqlite" else row.get("trade_id")
    row_key = f"db:{trade_id}" if source == "sqlite" and trade_id is not None else f"paper:{address}"
    pnl_pct = _to_float(_first_present(row.get("total_pnl_pct"), row.get("realized_pnl_pct"), row.get("pnl_pct")), 0.0) or 0.0
    peak_pct = _to_float(
        _first_present(row.get("max_pnl_pct_seen"), row.get("highest_pnl_pct"), row.get("peak_pnl_pct")),
        pnl_pct,
    )
    price5m = _to_float(_first_present(row.get("buy_price_pct_5m"), row.get("price_pct_5m"), enrich.get("buy_price_pct_5m"), enrich.get("price_pct_5m")))
    mcap = _to_float(_first_present(row.get("buy_market_cap_usd"), row.get("market_cap_usd"), enrich.get("buy_market_cap_usd"), enrich.get("market_cap_usd")))
    liquidity = _to_float(_first_present(row.get("buy_liquidity_usd"), row.get("liquidity_usd"), enrich.get("buy_liquidity_usd"), enrich.get("liquidity_usd")))
    rank = _to_float(_first_present(row.get("rank_score"), row.get("research_rank_score"), enrich.get("rank_score"), enrich.get("research_rank_score")))
    proxy = _boolish(_first_present(row.get("buy_liquidity_is_proxy"), row.get("liquidity_is_proxy"), enrich.get("buy_liquidity_is_proxy"), enrich.get("liquidity_is_proxy"), enrich.get("liquidity_usd_is_proxy")))
    entry_lane = _first_present(row.get("entry_lane"), enrich.get("entry_lane"))
    sublane = _first_present(row.get("profit_lane_tier"), enrich.get("profit_lane_tier"), row.get("entry_subtype"), enrich.get("entry_subtype"), entry_lane)
    return {
        "source": source,
        "row_key": row_key,
        "trade_id": trade_id,
        "address": address,
        "symbol": row.get("symbol"),
        "opened_at": _parse_timestamp(row.get("opened_at")),
        "closed_at": _parse_timestamp(row.get("closed_at")),
        "entry_regime": _label(_first_present(row.get("entry_regime"), row.get("discovered_via"))),
        "entry_lane": _label(entry_lane),
        "gate_profile": _label(_first_present(row.get("gate_profile"), enrich.get("gate_profile"))),
        "research_sublane": _label(sublane),
        "exit_reason": _label(_first_present(row.get("exit_reason"), row.get("reason"))),
        "pnl_pct": pnl_pct,
        "pnl_usd": _to_float(_first_present(row.get("total_pnl_usd"), row.get("realized_pnl_usd")), 0.0) or 0.0,
        "peak_pnl_pct": peak_pct if peak_pct is not None else pnl_pct,
        "price5m": price5m,
        "mcap_usd": mcap,
        "rank_score": rank,
        "liquidity_usd": liquidity,
        "liquidity_proxy": "proxy" if proxy is True else "real" if proxy is False else "unknown",
        "txns_5m": _to_float(_first_present(row.get("buy_txns_last_5m"), row.get("txns_last_5m"), enrich.get("buy_txns_last_5m"), enrich.get("txns_last_5m"))),
        "has_jupiter_route": _boolish(_first_present(row.get("has_jupiter_route"), enrich.get("has_jupiter_route"))),
    }


def _liquidity_bucket(value: Any) -> str:
    liquidity = _to_float(value)
    if liquidity is None or liquidity <= 0:
        return "liquidity_missing"
    if liquidity < 2_000:
        return "liquidity_<2k"
    if liquidity < 5_000:
        return "liquidity_2k_5k"
    if liquidity < 10_000:
        return "liquidity_5k_10k"
    if liquidity < 25_000:
        return "liquidity_10k_25k"
    return "liquidity_25k+"


def _median(values: list[float]) -> float | None:
    return round(float(statistics.median(values)), 3) if values else None


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(row["pnl_pct"]) for row in rows]
    pnl_usd = [float(row["pnl_usd"]) for row in rows]
    return {
        "count": len(rows),
        "win_rate_pct": round((sum(1 for value in pnls if value > 0.0) / len(pnls) * 100.0), 3) if pnls else None,
        "avg_pnl_pct": round((sum(pnls) / len(pnls)), 3) if pnls else None,
        "median_pnl_pct": _median(pnls),
        "total_pnl_usd": round(sum(pnl_usd), 6),
        "total_pnl_pct_points": round(sum(pnls), 3),
        "severe_loss_count": sum(1 for row in rows if _is_severe(row)),
        "runner_count_50": sum(1 for row in rows if float(row.get("peak_pnl_pct") or row["pnl_pct"]) >= 50.0),
        "runner_count_100": sum(1 for row in rows if float(row.get("peak_pnl_pct") or row["pnl_pct"]) >= 100.0),
        "runner_count_300": sum(1 for row in rows if float(row.get("peak_pnl_pct") or row["pnl_pct"]) >= 300.0),
        "runner_count_500": sum(1 for row in rows if float(row.get("peak_pnl_pct") or row["pnl_pct"]) >= 500.0),
    }


def _is_severe(row: dict[str, Any]) -> bool:
    return float(row.get("pnl_pct") or 0.0) <= SEVERE_LOSS_PNL_PCT


def _group(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_label(row.get(field))].append(row)
    return {key: _summary(value) for key, value in sorted(grouped.items())}


def _bucket_group(rows: list[dict[str, Any]], field: str, bucket_fn) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[bucket_fn(row.get(field))].append(row)
    return {key: _summary(value) for key, value in sorted(grouped.items())}


def build_post_run_baseline(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    enrichment = _candidate_enrichment(root)
    sqlite_available = _sqlite_available(root)
    rows = _load_sqlite_rows(root, enrichment) if sqlite_available else _load_paper_rows(root, enrichment)
    baseline_keys, state = _load_state_baseline_keys(root)
    filtered_rows = [row for row in rows if row["row_key"] not in baseline_keys] if baseline_keys else rows
    severe_rows = [row for row in filtered_rows if _is_severe(row)]

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {
            "primary": "sqlite" if sqlite_available else "paper_portfolio",
            "sqlite_available": sqlite_available,
            "sqlite_path": str(root / SQLITE_PATH),
            "paper_portfolio_path": str(root / PAPER_PORTFOLIO_PATH),
            "candidate_outcomes_path": str(root / CANDIDATE_OUTCOMES_PATH),
            "state_path": str(root / STATE_PATH),
        },
        "window": {
            "baseline_row_keys_excluded": sorted(baseline_keys),
            "baseline_closed_count": int((state or {}).get("baseline_closed_count") or len(baseline_keys)),
            "baseline_latest_closed_at": (state or {}).get("baseline_latest_closed_at"),
            "raw_closed_count": len(rows),
            "included_closed_count": len(filtered_rows),
        },
        "global": _summary(filtered_rows),
        "by_entry_lane": _group(filtered_rows, "entry_lane"),
        "by_research_sublane": _group(filtered_rows, "research_sublane"),
        "by_exit_reason": _group(filtered_rows, "exit_reason"),
        "by_price5m_bucket": _bucket_group(filtered_rows, "price5m", price5m_bucket),
        "by_mcap_bucket": _bucket_group(filtered_rows, "mcap_usd", mcap_bucket),
        "by_rank_bucket": _bucket_group(filtered_rows, "rank_score", rank_bucket),
        "by_liquidity_bucket": _bucket_group(filtered_rows, "liquidity_usd", _liquidity_bucket),
        "by_liquidity_proxy": _group(filtered_rows, "liquidity_proxy"),
        "severe_losses": [
            {
                "trade_id": row.get("trade_id"),
                "row_key": row.get("row_key"),
                "symbol": row.get("symbol"),
                "address": row.get("address"),
                "entry_lane": row.get("entry_lane"),
                "research_sublane": row.get("research_sublane"),
                "exit_reason": row.get("exit_reason"),
                "pnl_pct": round(float(row.get("pnl_pct") or 0.0), 3),
                "peak_pnl_pct": round(float(row.get("peak_pnl_pct") or 0.0), 3),
                "mcap_usd": row.get("mcap_usd"),
                "price5m": row.get("price5m"),
                "rank_score": row.get("rank_score"),
                "liquidity_usd": row.get("liquidity_usd"),
                "liquidity_proxy": row.get("liquidity_proxy"),
            }
            for row in severe_rows
        ],
    }


def _append_group_table(lines: list[str], title: str, rows: dict[str, dict[str, Any]]) -> None:
    lines.extend(["", f"## {title}", "", "| Bucket | Count | Win rate | Avg PnL | Median PnL | Severe | Runners >=100 |", "|---|---:|---:|---:|---:|---:|---:|"])
    for key, stats in rows.items():
        lines.append(
            "| {key} | {count} | {win_rate} | {avg} | {median} | {severe} | {r100} |".format(
                key=key,
                count=stats.get("count"),
                win_rate=_fmt_pct(stats.get("win_rate_pct")),
                avg=_fmt_pct(stats.get("avg_pnl_pct")),
                median=_fmt_pct(stats.get("median_pnl_pct")),
                severe=stats.get("severe_loss_count"),
                r100=stats.get("runner_count_100"),
            )
        )


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.2f}%"


def render_post_run_baseline_markdown(report: dict[str, Any]) -> str:
    global_stats = report["global"]
    window = report["window"]
    source = report["source"]
    lines = [
        "# Post-run 48h Baseline",
        "",
        f"- Generated at UTC: `{report['generated_at_utc']}`",
        f"- Source: `{source['primary']}`",
        f"- Raw closed trades: `{window['raw_closed_count']}`",
        f"- Included closed trades: `{window['included_closed_count']}`",
        f"- Excluded baseline keys: `{', '.join(window['baseline_row_keys_excluded']) if window['baseline_row_keys_excluded'] else 'none'}`",
        "",
        "## Global",
        "",
        f"- Closed trades: `{global_stats['count']}`",
        f"- Win rate: `{_fmt_pct(global_stats['win_rate_pct'])}`",
        f"- Avg PnL: `{_fmt_pct(global_stats['avg_pnl_pct'])}`",
        f"- Median PnL: `{_fmt_pct(global_stats['median_pnl_pct'])}`",
        f"- Total PnL USD: `{global_stats['total_pnl_usd']:.6f}`",
        f"- Severe losses: `{global_stats['severe_loss_count']}`",
        f"- Runners >=50/>=100/>=300/>=500: `{global_stats['runner_count_50']}/{global_stats['runner_count_100']}/{global_stats['runner_count_300']}/{global_stats['runner_count_500']}`",
    ]
    _append_group_table(lines, "By Entry Lane", report["by_entry_lane"])
    _append_group_table(lines, "By Research Sublane", report["by_research_sublane"])
    _append_group_table(lines, "By Exit Reason", report["by_exit_reason"])
    _append_group_table(lines, "By Price5m Bucket", report["by_price5m_bucket"])
    _append_group_table(lines, "By Mcap Bucket", report["by_mcap_bucket"])
    _append_group_table(lines, "By Rank Bucket", report["by_rank_bucket"])
    _append_group_table(lines, "By Liquidity Bucket", report["by_liquidity_bucket"])
    _append_group_table(lines, "By Liquidity Proxy", report["by_liquidity_proxy"])
    lines.extend(["", "## Severe Losses", "", "| Trade | Symbol | Lane | Sublane | Exit | PnL | Peak | Mcap | Price5m | Rank | Liquidity |", "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|"])
    for row in report["severe_losses"]:
        lines.append(
            "| {trade} | {symbol} | {lane} | {sublane} | {exit} | {pnl:.2f}% | {peak:.2f}% | {mcap} | {price5m} | {rank} | {liq} |".format(
                trade=row.get("trade_id") or row.get("row_key"),
                symbol=row.get("symbol") or row.get("address"),
                lane=row.get("entry_lane"),
                sublane=row.get("research_sublane"),
                exit=row.get("exit_reason"),
                pnl=float(row.get("pnl_pct") or 0.0),
                peak=float(row.get("peak_pnl_pct") or 0.0),
                mcap=_fmt_num(row.get("mcap_usd")),
                price5m=_fmt_num(row.get("price5m")),
                rank=_fmt_num(row.get("rank_score")),
                liq=_fmt_num(row.get("liquidity_usd")),
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def _fmt_num(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.3f}"
    except Exception:
        return str(value)


def write_post_run_baseline(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_post_run_baseline(root)
    json_path = root / BASELINE_JSON
    doc_path = root / BASELINE_DOC
    json_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    doc_path.write_text(render_post_run_baseline_markdown(report), encoding="utf-8")
    return report


__all__ = [
    "BASELINE_DOC",
    "BASELINE_JSON",
    "build_post_run_baseline",
    "render_post_run_baseline_markdown",
    "write_post_run_baseline",
]
