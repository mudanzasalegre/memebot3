from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterable

from analytics.bird_runner_exit import simulate_bird_runner_capture
from analytics.birth_probe_micro_canary import (
    build_birth_probe_micro_canary_report,
    reason_group_from_failures,
    write_birth_probe_micro_canary_report,
)
from analytics.post_partial_protection_report import (
    build_post_partial_protection_report,
    write_post_partial_protection_report,
)
from analytics.pumpswap_prime_strict import (
    build_pumpswap_prime_strict_report,
    evaluate_pumpswap_prime_strict,
    is_pumpswap_prime,
    write_pumpswap_prime_strict_report,
)
from analytics.pumpswap_rebound_prime import (
    build_pumpswap_rebound_prime_report,
    evaluate_pumpswap_rebound_prime,
    write_pumpswap_rebound_prime_report,
)
from analytics.report_utils import (
    address_of,
    fnum,
    is_severe_exit,
    load_candidate_outcomes,
    load_paper_positions,
    load_sqlite_positions,
    metrics_dir,
    write_json,
    write_markdown,
)
from analytics.runner_capture_ladder_report import write_runner_capture_ladder_report
from config.config import CFG, PROJECT_ROOT


REPORT_JSON = "post_hotfix_strategy_preview.json"
REPORT_MD = "POST_HOTFIX_STRATEGY_PREVIEW.md"


def _boolish(value: Any, default: bool = False) -> bool:
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


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _pnl(row: dict[str, Any]) -> float:
    return fnum(
        _first(
            row,
            "realized_pnl_pct",
            "actual_total_pnl_pct",
            "total_pnl_pct",
            "pnl_pct",
            "target_total_pnl_pct",
            "unrealized_pnl_pct",
        ),
        0.0,
    )


def _peak(row: dict[str, Any]) -> float:
    pnl = _pnl(row)
    return max(
        pnl,
        fnum(row.get("highest_pnl_pct"), 0.0),
        fnum(row.get("max_pnl_pct_seen"), 0.0),
        fnum(row.get("max_pnl_seen"), 0.0),
        fnum(row.get("peak_pnl_pct"), 0.0),
        fnum(row.get("max_pnl_pct"), 0.0),
        fnum(row.get("later_peak_pct"), 0.0),
        fnum(row.get("confirmed_peak"), 0.0),
    )


def _closed_position_rows(root: Path) -> list[dict[str, Any]]:
    rows = load_paper_positions(root) + load_sqlite_positions(root)
    out: list[dict[str, Any]] = []
    for row in rows:
        if _boolish(row.get("closed"), False) or _first(row, "exit_reason", "closed_at", "total_pnl_pct", "realized_pnl_pct") is not None:
            out.append(row)
    return out


def _all_outcome_rows(root: Path) -> list[dict[str, Any]]:
    return load_candidate_outcomes(root) + _closed_position_rows(root)


def _summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(rows)
    pnls = [_pnl(row) for row in items]
    if not items:
        return {
            "count": 0,
            "win_rate_pct": 0.0,
            "avg_pnl_pct": 0.0,
            "total_pnl_pct_points": 0.0,
            "severe_loss_count": 0,
            "runner_100_count": 0,
            "runner_500_count": 0,
            "runner_1000_count": 0,
        }
    peaks = [_peak(row) for row in items]
    return {
        "count": len(items),
        "win_rate_pct": round(100.0 * sum(1 for value in pnls if value > 0) / len(pnls), 4),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 4),
        "total_pnl_pct_points": round(sum(pnls), 4),
        "severe_loss_count": sum(1 for row, pnl in zip(items, pnls) if is_severe_exit(row) or pnl <= -25.0),
        "runner_100_count": sum(1 for peak in peaks if peak >= 100.0),
        "runner_500_count": sum(1 for peak in peaks if peak >= 500.0),
        "runner_1000_count": sum(1 for peak in peaks if peak >= 1000.0),
    }


def _safe_report(callable_obj: Any, root: Path) -> dict[str, Any]:
    try:
        payload = callable_obj(root)
    except Exception as exc:
        return {"error": str(exc)}
    return payload if isinstance(payload, dict) else {"payload": payload}


def _strict_preview(current_rows: list[dict[str, Any]]) -> dict[str, Any]:
    prime_rows = [row for row in current_rows if is_pumpswap_prime(row)]
    blocked = [row for row in prime_rows if not evaluate_pumpswap_prime_strict(row).allowed]
    passed = [row for row in prime_rows if evaluate_pumpswap_prime_strict(row).allowed]
    blocked_pnl = sum(_pnl(row) for row in blocked)
    return {
        "previous_prime_current": _summary(prime_rows),
        "strict_passed_current": _summary(passed),
        "strict_blocked_current": _summary(blocked),
        "expected_total_pnl_delta_pct_points": round(-blocked_pnl, 4),
        "expected_severe_loss_delta": -_summary(blocked)["severe_loss_count"],
        "runner_missed_by_blocking": {
            "peak_100_count": sum(1 for row in blocked if _peak(row) >= 100.0),
            "peak_500_count": sum(1 for row in blocked if _peak(row) >= 500.0),
            "peak_1000_count": sum(1 for row in blocked if _peak(row) >= 1000.0),
        },
    }


def _rebound_preview(current_rows: list[dict[str, Any]], outcome_rows: list[dict[str, Any]]) -> dict[str, Any]:
    bought = {address_of(row) for row in current_rows if address_of(row)}
    candidates = [row for row in outcome_rows if evaluate_pumpswap_rebound_prime(row).allowed]
    incremental = [row for row in candidates if not address_of(row) or address_of(row) not in bought]
    stats = _summary(incremental)
    return {
        "all_candidates": _summary(candidates),
        "incremental_candidates": stats,
        "expected_total_pnl_delta_pct_points": stats["total_pnl_pct_points"],
        "expected_severe_loss_delta": stats["severe_loss_count"],
        "expected_missed_peak_capture": {
            "peak_100_count": stats["runner_100_count"],
            "peak_500_count": stats["runner_500_count"],
            "peak_1000_count": stats["runner_1000_count"],
        },
    }


def _runner_ladder_preview(outcome_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in outcome_rows:
        address = address_of(row) or str(id(row))
        if address in seen:
            continue
        seen.add(address)
        final_pnl = _pnl(row)
        peak = _peak(row)
        if peak < 25.0:
            continue
        sim = simulate_bird_runner_capture(peak, final_pnl, cfg=CFG)
        current_capture = final_pnl / peak if peak > 0 else 0.0
        rows.append(
            {
                "address": address_of(row),
                "entry_lane": _first(row, "entry_lane", "profit_lane_tier", "lane") or "unknown",
                "peak_pct": round(peak, 4),
                "final_pnl_pct": round(final_pnl, 4),
                "simulated_realized_pnl_pct": sim["simulated_realized_pnl_pct"],
                "current_capture_ratio": round(max(0.0, current_capture), 4),
                "simulated_capture_ratio": sim["capture_ratio"],
                "delta_pnl_pct_points": round(float(sim["simulated_realized_pnl_pct"]) - final_pnl, 4),
                "emergency_sell": bool(sim["emergency_sell"]),
            }
        )
    if not rows:
        return {
            "rows": 0,
            "expected_total_pnl_delta_pct_points": 0.0,
            "expected_runner_capture_delta": 0.0,
            "emergency_sells": 0,
            "top_improvements": [],
        }
    current_capture_avg = sum(fnum(row["current_capture_ratio"]) for row in rows) / len(rows)
    sim_capture_avg = sum(fnum(row["simulated_capture_ratio"]) for row in rows) / len(rows)
    return {
        "rows": len(rows),
        "expected_total_pnl_delta_pct_points": round(sum(fnum(row["delta_pnl_pct_points"]) for row in rows), 4),
        "expected_runner_capture_delta": round(sim_capture_avg - current_capture_avg, 6),
        "emergency_sells": sum(1 for row in rows if row["emergency_sell"]),
        "top_improvements": sorted(rows, key=lambda row: fnum(row["delta_pnl_pct_points"]), reverse=True)[:25],
    }


def _research_rank_preview(root: Path, outcome_rows: list[dict[str, Any]]) -> dict[str, Any]:
    audit_path = metrics_dir(root) / "research_rank_canary_audit.json"
    audit: dict[str, Any] = {}
    try:
        loaded = json.loads(audit_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            audit = loaded
    except Exception:
        audit = {}
    rows = []
    for row in outcome_rows:
        haystack = " ".join(str(_first(row, key) or "") for key in ("entry_lane", "gate_profile", "profit_lane_tier", "lane_policy_category", "reason"))
        if "research_rank_canary" in haystack:
            rows.append(row)
    mixed = [
        row
        for row in rows
        if str(_first(row, "entry_lane") or "").strip().lower() not in {"pump_early_research_rank_canary", ""}
    ]
    own = [
        row
        for row in rows
        if str(_first(row, "entry_lane") or "").strip().lower() == "pump_early_research_rank_canary"
        or str(_first(row, "gate_profile") or "").strip().lower() == "research_rank_canary"
    ]
    return {
        "audit": {
            "evaluated": int(audit.get("evaluated") or audit.get("total_evaluations") or 0),
            "allowed": int(audit.get("allowed") or 0),
            "bought_as_own_lane": int(audit.get("bought_as_own_lane") or 0),
            "shadow_as_own_lane": int(audit.get("shadow_as_own_lane") or 0),
            "mixed_lane_detected": int(audit.get("mixed_lane_detected") or 0),
            "blocked_by_reason": audit.get("blocked_by_reason") if isinstance(audit.get("blocked_by_reason"), dict) else {},
        },
        "historical_rows": _summary(rows),
        "own_lane_rows": _summary(own),
        "mixed_lane_rows": _summary(mixed),
    }


def _birth_micro_preview(outcome_rows: list[dict[str, Any]], birth_report: dict[str, Any]) -> dict[str, Any]:
    recommended = birth_report.get("recommended_groups") if isinstance(birth_report.get("recommended_groups"), dict) else {}
    recommended_names = set(str(key) for key in recommended)
    candidates = []
    for row in outcome_rows:
        haystack = " ".join(str(_first(row, key) or "") for key in ("reason", "green_sniper_reason", "gate_profile", "entry_subtype", "entry_lane"))
        if "birth_probe" not in haystack.lower():
            continue
        group = str(_first(row, "reason_group") or reason_group_from_failures(haystack))
        if group in recommended_names:
            candidates.append(row)
    stats = _summary(candidates)
    return {
        "recommended_groups": recommended,
        "candidate_rows": stats,
        "expected_missed_peak_capture": {
            "peak_100_count": stats["runner_100_count"],
            "peak_500_count": stats["runner_500_count"],
            "peak_1000_count": stats["runner_1000_count"],
        },
        "expected_total_pnl_delta_pct_points": stats["total_pnl_pct_points"],
        "expected_severe_loss_delta": stats["severe_loss_count"],
        "amount_sol": float(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_AMOUNT_SOL", 0.01) or 0.01),
    }


def build_post_hotfix_strategy_preview(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    current_rows = _closed_position_rows(root)
    outcome_rows = _all_outcome_rows(root)
    strict_report = _safe_report(build_pumpswap_prime_strict_report, root)
    rebound_report = _safe_report(build_pumpswap_rebound_prime_report, root)
    post_partial_report = _safe_report(build_post_partial_protection_report, root)
    birth_report = _safe_report(build_birth_probe_micro_canary_report, root)

    strict = _strict_preview(current_rows)
    rebound = _rebound_preview(current_rows, outcome_rows)
    runner = _runner_ladder_preview(outcome_rows)
    research_rank = _research_rank_preview(root, outcome_rows)
    birth_micro = _birth_micro_preview(outcome_rows, birth_report)

    post_delta = post_partial_report.get("delta") if isinstance(post_partial_report.get("delta"), dict) else {}
    post_total_delta = fnum(post_delta.get("total_pnl"), 0.0) if isinstance(post_delta, dict) else 0.0
    post_severe_delta = int(post_delta.get("severe_losses") or 0) if isinstance(post_delta, dict) else 0
    post_runner_delta = fnum(post_delta.get("runner_capture"), 0.0) if isinstance(post_delta, dict) else 0.0

    expected_total = (
        fnum(strict["expected_total_pnl_delta_pct_points"])
        + fnum(rebound["expected_total_pnl_delta_pct_points"])
        + post_total_delta
        + fnum(runner["expected_total_pnl_delta_pct_points"])
        + fnum(birth_micro["expected_total_pnl_delta_pct_points"])
    )
    expected_severe = (
        int(strict["expected_severe_loss_delta"])
        + int(rebound["expected_severe_loss_delta"])
        + post_severe_delta
        + int(birth_micro["expected_severe_loss_delta"])
    )
    missed_peak_capture = {
        "rebound_peak_100": rebound["expected_missed_peak_capture"]["peak_100_count"],
        "rebound_peak_500": rebound["expected_missed_peak_capture"]["peak_500_count"],
        "rebound_peak_1000": rebound["expected_missed_peak_capture"]["peak_1000_count"],
        "birth_micro_peak_100": birth_micro["expected_missed_peak_capture"]["peak_100_count"],
        "birth_micro_peak_500": birth_micro["expected_missed_peak_capture"]["peak_500_count"],
        "birth_micro_peak_1000": birth_micro["expected_missed_peak_capture"]["peak_1000_count"],
    }
    return {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "profile": "combined_hotfix_v1",
        "safety": {
            "dry_run": bool(getattr(CFG, "DRY_RUN", True)),
            "strategy_optimization_lock": bool(getattr(CFG, "STRATEGY_OPTIMIZATION_LOCK", True)),
            "live_canary_enabled": bool(getattr(CFG, "LIVE_CANARY_ENABLED", False)),
            "auto_promote_live": bool(getattr(CFG, "AUTO_PROMOTE_LIVE", False)),
            "model_auto_promote": bool(getattr(CFG, "MODEL_AUTO_PROMOTE", False)),
            "birth_probe_micro_live_enabled": bool(getattr(CFG, "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED", False)),
            "bird_runner_live_enabled": bool(getattr(CFG, "BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED", False)),
            "runner_giveback_live_enabled": bool(getattr(CFG, "RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED", False)),
        },
        "baseline_current": _summary(current_rows),
        "pumpswap_strict": {
            "preview": strict,
            "report": strict_report,
        },
        "rebound_lane": {
            "preview": rebound,
            "report": rebound_report,
        },
        "post_partial_execution": {
            "report": post_partial_report,
            "expected_total_pnl_delta_pct_points": post_total_delta,
            "expected_severe_loss_delta": post_severe_delta,
            "expected_runner_capture_delta": post_runner_delta,
        },
        "multi_partial_runner": runner,
        "research_rank_own_lane": research_rank,
        "birth_probe_micro_canary": birth_micro,
        "combined_hotfix_v1": {
            "expected_total_pnl_delta_pct_points": round(expected_total, 4),
            "expected_severe_loss_delta": expected_severe,
            "expected_runner_capture_delta": round(post_runner_delta + fnum(runner["expected_runner_capture_delta"]), 6),
            "expected_missed_peak_capture": missed_peak_capture,
            "estimate_note": "Offline additive preview. Entry and exit effects can overlap; use as directional validation before paper forward.",
        },
    }


def write_post_hotfix_strategy_preview(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    write_pumpswap_prime_strict_report(root)
    write_pumpswap_rebound_prime_report(root)
    write_post_partial_protection_report(root)
    write_runner_capture_ladder_report(root)
    write_birth_probe_micro_canary_report(root)
    report = build_post_hotfix_strategy_preview(root)
    write_json(metrics_dir(root) / REPORT_JSON, report)
    combined = report["combined_hotfix_v1"]
    baseline = report["baseline_current"]
    strict = report["pumpswap_strict"]["preview"]
    rebound = report["rebound_lane"]["preview"]
    runner = report["multi_partial_runner"]
    birth = report["birth_probe_micro_canary"]
    rank = report["research_rank_own_lane"]
    lines = [
        "# Post Hotfix Strategy Preview",
        "",
        "Offline preview for `combined_hotfix_v1`. Live remains disabled; this report does not promote models or change wallets.",
        "",
        "## Combined Estimate",
        "",
        f"- Baseline closed rows: `{baseline['count']}`",
        f"- Expected total PnL delta: `{combined['expected_total_pnl_delta_pct_points']:.4f}` pct-points",
        f"- Expected severe loss delta: `{combined['expected_severe_loss_delta']}`",
        f"- Expected runner capture delta: `{combined['expected_runner_capture_delta']:.6f}`",
        f"- Estimate note: {combined['estimate_note']}",
        "",
        "## Entry Changes",
        "",
        "| Surface | Count | Total PnL delta | Severe delta | Peak100 | Peak500 |",
        "|---|---:|---:|---:|---:|---:|",
        f"| Pumpswap strict blocked current | {strict['strict_blocked_current']['count']} | {strict['expected_total_pnl_delta_pct_points']:.4f} | {strict['expected_severe_loss_delta']} | {strict['runner_missed_by_blocking']['peak_100_count']} | {strict['runner_missed_by_blocking']['peak_500_count']} |",
        f"| Rebound incremental candidates | {rebound['incremental_candidates']['count']} | {rebound['expected_total_pnl_delta_pct_points']:.4f} | {rebound['expected_severe_loss_delta']} | {rebound['expected_missed_peak_capture']['peak_100_count']} | {rebound['expected_missed_peak_capture']['peak_500_count']} |",
        f"| Birth micro candidates | {birth['candidate_rows']['count']} | {birth['expected_total_pnl_delta_pct_points']:.4f} | {birth['expected_severe_loss_delta']} | {birth['expected_missed_peak_capture']['peak_100_count']} | {birth['expected_missed_peak_capture']['peak_500_count']} |",
        "",
        "## Exit Changes",
        "",
        f"- Post-partial expected total delta: `{report['post_partial_execution']['expected_total_pnl_delta_pct_points']:.4f}`",
        f"- Multi-partial runner rows: `{runner['rows']}`",
        f"- Multi-partial expected total delta: `{runner['expected_total_pnl_delta_pct_points']:.4f}`",
        f"- Emergency sells simulated: `{runner['emergency_sells']}`",
        "",
        "## Research Rank Lane",
        "",
        f"- Audit evaluated: `{rank['audit']['evaluated']}`",
        f"- Bought as own lane: `{rank['audit']['bought_as_own_lane']}`",
        f"- Shadow as own lane: `{rank['audit']['shadow_as_own_lane']}`",
        f"- Mixed lane detected: `{rank['audit']['mixed_lane_detected']}`",
        "",
        "## Safety",
        "",
    ]
    for name, value in report["safety"].items():
        lines.append(f"- `{name}`: `{value}`")
    write_markdown(root / "docs" / REPORT_MD, lines)
    return report


__all__ = [
    "REPORT_JSON",
    "REPORT_MD",
    "build_post_hotfix_strategy_preview",
    "write_post_hotfix_strategy_preview",
]
