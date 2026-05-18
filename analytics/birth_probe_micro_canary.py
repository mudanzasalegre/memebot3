from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from analytics.report_utils import (
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
from ml.lane_taxonomy import LANE_BIRTH_PROBE_MICRO_CANARY


REPORT_PATH = PROJECT_ROOT / "data" / "metrics" / "birth_probe_micro_canary_report.json"
_GROUP_STATS_CACHE: dict[str, Any] = {"key": None, "groups": None}


@dataclass(frozen=True)
class BirthProbeMicroCanaryDecision:
    allowed: bool
    reason: str
    reason_group: str
    amount_sol: float
    lane: str = LANE_BIRTH_PROBE_MICRO_CANARY
    stats: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _csv(value: Any) -> set[str]:
    return {part.strip() for part in str(value or "").split(",") if part.strip()}


def reason_group_from_failures(failures: Iterable[str] | str) -> str:
    if isinstance(failures, str):
        raw = failures
        if "paper_birth_probe:" in raw:
            raw = raw.split("paper_birth_probe:", 1)[-1]
        else:
            raw = raw.split(":", 1)[-1]
        parts = {part.strip() for part in raw.split(",") if part.strip()}
    else:
        parts = {str(part).strip() for part in failures if str(part).strip()}
    has_proxy = bool(parts & {"proxy_liquidity_productive_block", "proxy_liquidity_paper_disabled", "proxy_liquidity"})
    has_low_txns = "low_txns_5m" in parts
    has_low_green = "low_green_momentum" in parts
    has_missing = bool(parts & {"missing_price_pct_5m", "missing_price", "missing_mcap"})
    has_weak_ratio = "weak_buy_sell_ratio" in parts
    if has_low_green and has_proxy and has_low_txns:
        return "paper_birth_probe_low_green_proxy_low_txns"
    if has_proxy and has_low_txns:
        return "paper_birth_probe_proxy_low_txns"
    if has_proxy:
        return "paper_birth_probe_proxy"
    if has_low_green and has_low_txns:
        return "paper_birth_probe_low_green_low_txns"
    if has_missing:
        return "paper_birth_probe_missing_fields"
    if has_weak_ratio:
        return "paper_birth_probe_weak_ratio"
    return "paper_birth_probe_other"


def _row_reason_group(row: dict[str, Any]) -> str:
    explicit = str(row.get("reason_group") or "").strip()
    if explicit:
        return explicit
    reason = str(row.get("reason") or row.get("green_sniper_reason") or row.get("reject_reason") or "")
    return reason_group_from_failures(reason)


def _is_birth_probe_row(row: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("reason", "green_sniper_reason", "gate_profile", "sniper_gate_profile", "entry_subtype", "entry_lane")
    ).lower()
    return "birth_probe" in haystack


def _pnl(row: dict[str, Any]) -> float:
    return fnum(
        row.get("realized_pnl_pct")
        or row.get("total_pnl_pct")
        or row.get("pnl_pct")
        or row.get("target_total_pnl_pct")
        or row.get("unrealized_pnl_pct"),
        0.0,
    )


def _peak(row: dict[str, Any], pnl: float) -> float:
    return max(
        pnl,
        fnum(row.get("highest_pnl_pct"), 0.0),
        fnum(row.get("max_pnl_pct_seen"), 0.0),
        fnum(row.get("max_pnl_seen"), 0.0),
        fnum(row.get("peak_pnl_pct"), 0.0),
        fnum(row.get("later_peak_pct"), 0.0),
        fnum(row.get("confirmed_peak"), 0.0),
    )


def _report_path(root: Path) -> Path:
    return metrics_dir(root) / "birth_probe_micro_canary_report.json"


def _source_mtime_ns(root: Path) -> int:
    candidates = (
        metrics_dir(root) / "candidate_outcomes.jsonl",
        metrics_dir(root) / "runtime_events.jsonl",
        root / "data" / "paper_portfolio.json",
        root / "data" / "research_portfolio.json",
        root / "data" / "memebotdatabase.db",
    )
    mtimes: list[int] = []
    for path in candidates:
        try:
            mtimes.append(path.stat().st_mtime_ns)
        except OSError:
            continue
    return max(mtimes or [0])


def _load_research_portfolio(root: Path) -> list[dict[str, Any]]:
    path = root / "data" / "research_portfolio.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    rows = payload.get("positions") if isinstance(payload, dict) else payload
    if isinstance(rows, dict):
        rows = list(rows.values())
    return [row for row in rows or [] if isinstance(row, dict)]


def _collect_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.extend(load_candidate_outcomes(root))
    rows.extend(load_paper_positions(root))
    rows.extend(load_sqlite_positions(root))
    rows.extend(load_runtime_events(root))
    rows.extend(_load_research_portfolio(root))
    return rows


def summarize_reason_groups(rows: Iterable[dict[str, Any]], *, cfg: Any = CFG) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not _is_birth_probe_row(row):
            continue
        group = _row_reason_group(row)
        pnl = _pnl(row)
        peak = _peak(row, pnl)
        item = {
            "pnl": pnl,
            "peak": peak,
            "severe": pnl <= -25.0,
        }
        buckets.setdefault(group, []).append(item)

    min_samples = int(getattr(cfg, "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_SAMPLES", 50) or 50)
    min_ev = float(getattr(cfg, "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_EV_PCT", 5.0) or 5.0)
    pnl_cap_raw = getattr(cfg, "BIRTH_PROBE_MICRO_CANARY_PNL_CAP_PCT", 1000.0)
    min_capped_ev_raw = getattr(cfg, "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_CAPPED_EV_PCT", -1.0)
    pnl_cap = max(1.0, float(1000.0 if pnl_cap_raw is None else pnl_cap_raw))
    min_capped_ev = float(-1.0 if min_capped_ev_raw is None else min_capped_ev_raw)
    out: dict[str, dict[str, Any]] = {}
    for group, items in sorted(buckets.items()):
        pnls = [float(item["pnl"]) for item in items]
        capped_pnls = [max(-100.0, min(pnl_cap, pnl)) for pnl in pnls]
        peaks = [float(item["peak"]) for item in items]
        samples = len(items)
        avg = sum(pnls) / samples if samples else 0.0
        avg_capped = sum(capped_pnls) / samples if samples else 0.0
        median = sorted(pnls)[samples // 2] if samples else 0.0
        severe = sum(1 for item in items if item["severe"])
        stats = {
            "reason_group": group,
            "samples": samples,
            "avg_pnl": round(avg, 4),
            "avg_pnl_capped": round(avg_capped, 4),
            "pnl_cap_pct": round(pnl_cap, 4),
            "median_pnl": round(median, 4),
            "win_rate": round(sum(1 for pnl in pnls if pnl > 0) / samples * 100.0, 4) if samples else 0.0,
            "peak100_count": sum(1 for peak in peaks if peak >= 100.0),
            "peak500_count": sum(1 for peak in peaks if peak >= 500.0),
            "peak1000_count": sum(1 for peak in peaks if peak >= 1000.0),
            "severe_loss_count": severe,
            "severe_loss_rate": round(severe / samples * 100.0, 4) if samples else 0.0,
        }
        stats["recommended_micro_enabled"] = bool(
            samples >= min_samples
            and avg > min_ev
            and avg_capped >= min_capped_ev
            and int(stats["peak100_count"]) >= 3
            and float(stats["severe_loss_rate"]) <= 50.0
        )
        stats["recommended_exit_profile"] = "birth_probe_micro_ladder"
        out[group] = stats
    return out


def load_reason_group_stats(root: Path | None = None) -> dict[str, dict[str, Any]]:
    root = root or PROJECT_ROOT
    report_path = _report_path(root)
    source_mtime_ns = _source_mtime_ns(root)
    try:
        report_mtime_ns = report_path.stat().st_mtime_ns
    except OSError:
        report_mtime_ns = 0
    cache_key = (str(root.resolve()), int(report_mtime_ns), int(source_mtime_ns))
    if _GROUP_STATS_CACHE.get("key") == cache_key and isinstance(_GROUP_STATS_CACHE.get("groups"), dict):
        return dict(_GROUP_STATS_CACHE["groups"])
    if report_path.exists() and report_mtime_ns >= source_mtime_ns:
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            groups = payload.get("reason_groups")
            if isinstance(groups, dict) and groups:
                out = {str(key): value for key, value in groups.items() if isinstance(value, dict)}
                _GROUP_STATS_CACHE["key"] = cache_key
                _GROUP_STATS_CACHE["groups"] = out
                return dict(out)
        except Exception:
            pass
    report = build_birth_probe_micro_canary_report(root)
    write_json(report_path, report)
    groups = report.get("reason_groups") if isinstance(report, dict) else {}
    out = {str(key): value for key, value in groups.items() if isinstance(value, dict)} if isinstance(groups, dict) else {}
    try:
        report_mtime_ns = report_path.stat().st_mtime_ns
    except OSError:
        report_mtime_ns = 0
    _GROUP_STATS_CACHE["key"] = (str(root.resolve()), int(report_mtime_ns), int(source_mtime_ns))
    _GROUP_STATS_CACHE["groups"] = out
    return dict(out)


def evaluate_birth_probe_micro_canary(
    token: dict[str, Any],
    failures: Iterable[str] | str,
    *,
    dry_run: bool,
    live: bool,
    group_stats: dict[str, dict[str, Any]] | None = None,
    cfg: Any = CFG,
) -> BirthProbeMicroCanaryDecision:
    group = reason_group_from_failures(failures)
    amount = max(0.0, float(getattr(cfg, "BIRTH_PROBE_MICRO_CANARY_AMOUNT_SOL", 0.01) or 0.01))
    def decision(allowed: bool, reason: str, stats: dict[str, Any] | None = None) -> BirthProbeMicroCanaryDecision:
        return BirthProbeMicroCanaryDecision(allowed, reason, group, amount, stats=stats)

    if not _bool(getattr(cfg, "BIRTH_PROBE_MICRO_CANARY_ENABLED", True), True):
        return decision(False, "disabled")
    if live or not dry_run:
        return decision(False, "paper_only")
    if not _bool(getattr(cfg, "BIRTH_PROBE_MICRO_CANARY_PAPER_ENABLED", True), True):
        return decision(False, "paper_disabled")
    if _bool(getattr(cfg, "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED", False), False):
        return decision(False, "live_flag_must_be_false")
    allowed_groups = _csv(getattr(cfg, "BIRTH_PROBE_MICRO_CANARY_ALLOWED_REASON_GROUPS", ""))
    if group not in allowed_groups:
        return decision(False, "reason_group_not_allowed")
    stats_by_group = group_stats if group_stats is not None else load_reason_group_stats(PROJECT_ROOT)
    recommended_groups = {
        str(name): value
        for name, value in (stats_by_group or {}).items()
        if isinstance(value, dict) and _bool(value.get("recommended_micro_enabled"), False)
    }
    if not recommended_groups:
        return decision(False, "no_recommended_groups")
    stats = stats_by_group.get(group) if isinstance(stats_by_group, dict) else None
    if not isinstance(stats, dict):
        return decision(False, "reason_group_stats_missing")
    min_samples = int(getattr(cfg, "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_SAMPLES", 50) or 50)
    min_ev = float(getattr(cfg, "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_EV_PCT", 5.0) or 5.0)
    min_capped_ev_raw = getattr(cfg, "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_CAPPED_EV_PCT", -1.0)
    min_capped_ev = float(-1.0 if min_capped_ev_raw is None else min_capped_ev_raw)
    if int(stats.get("samples") or 0) < min_samples:
        return decision(False, "group_samples_below_min", stats)
    if float(stats.get("avg_pnl") or 0.0) <= min_ev:
        return decision(False, "group_ev_below_min", stats)
    if "avg_pnl_capped" in stats and float(stats.get("avg_pnl_capped") or 0.0) < min_capped_ev:
        return decision(False, "group_capped_ev_below_min", stats)
    if int(stats.get("peak100_count") or 0) < 3:
        return decision(False, "group_peak100_below_min", stats)
    if not _bool(stats.get("recommended_micro_enabled"), False):
        return decision(False, "group_not_recommended", stats)
    return decision(True, "birth_probe_micro_canary", stats)


def apply_birth_probe_micro_canary_context(
    token: dict[str, Any],
    decision: BirthProbeMicroCanaryDecision,
) -> dict[str, Any]:
    token["entry_lane"] = decision.lane
    token["gate_profile"] = "birth_probe_micro_canary"
    token["sniper_gate_profile"] = "birth_probe_micro_canary"
    token["profit_lane_tier"] = decision.lane
    token["lane_policy_category"] = "birth_probe_micro_canary"
    token["entry_subtype"] = "birth_probe_micro_canary"
    token["green_sniper_reason"] = decision.reason
    token["birth_probe_reason_group"] = decision.reason_group
    token["birth_probe_micro_canary_amount_sol"] = decision.amount_sol
    token["birth_probe_micro_canary_enabled"] = int(bool(decision.allowed))
    token["runner_exit_profile"] = "birth_probe_micro_ladder"
    return token


def build_birth_probe_micro_canary_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    groups = summarize_reason_groups(_collect_rows(root), cfg=CFG)
    recommended = {key: value for key, value in groups.items() if value.get("recommended_micro_enabled")}
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "enabled": bool(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_ENABLED", True)),
            "paper_enabled": bool(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_PAPER_ENABLED", True)),
            "live_enabled": bool(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED", False)),
            "amount_sol": float(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_AMOUNT_SOL", 0.01) or 0.01),
            "max_open": int(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_MAX_OPEN", 1) or 1),
            "max_daily_buys": int(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_MAX_DAILY_BUYS", 5) or 5),
            "allowed_reason_groups": sorted(_csv(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_ALLOWED_REASON_GROUPS", ""))),
            "min_group_ev_pct": float(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_EV_PCT", 5.0) or 5.0),
            "pnl_cap_pct": float(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_PNL_CAP_PCT", 1000.0) or 1000.0),
            "min_group_capped_ev_pct": float(
                -1.0
                if getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_CAPPED_EV_PCT", -1.0) is None
                else getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_CAPPED_EV_PCT", -1.0)
            ),
            "min_group_samples": int(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_SAMPLES", 50) or 50),
        },
        "reason_groups": groups,
        "recommended_groups": recommended,
    }


def write_birth_probe_micro_canary_report(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_birth_probe_micro_canary_report(root)
    write_json(metrics_dir(root) / "birth_probe_micro_canary_report.json", report)
    lines = [
        "# Birth Probe Micro Canary",
        "",
        "Paper-only micro-canary for selected birth-probe reason groups. Live remains disabled.",
        "",
        "| Reason group | Samples | Avg PnL | Capped Avg | Win | Peak100 | Peak500 | Severe | Recommended |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for group, stats in report["reason_groups"].items():
        lines.append(
            f"| {group} | {stats['samples']} | {stats['avg_pnl']:.2f}% | "
            f"{float(stats.get('avg_pnl_capped') or 0.0):.2f}% | {stats['win_rate']:.2f}% | "
            f"{stats['peak100_count']} | {stats['peak500_count']} | {stats['severe_loss_count']} | "
            f"{stats['recommended_micro_enabled']} |"
        )
    write_markdown(root / "docs" / "BIRTH_PROBE_MICRO_CANARY.md", lines)
    return report


__all__ = [
    "BirthProbeMicroCanaryDecision",
    "apply_birth_probe_micro_canary_context",
    "build_birth_probe_micro_canary_report",
    "evaluate_birth_probe_micro_canary",
    "reason_group_from_failures",
    "summarize_reason_groups",
    "write_birth_probe_micro_canary_report",
]
