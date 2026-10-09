from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import json
from types import SimpleNamespace

from analytics.green_sniper_restricted_report import restricted_failures
from analytics.lane_policy_categories import (
    POLICY_GREEN_SNIPER_PURE,
    POLICY_GREEN_SNIPER_RESTRICTED_BUY,
    POLICY_GREEN_SNIPER_SHADOW,
    POLICY_LATE_MOMENTUM_WATCH,
    classify_policy_category,
)
from analytics.pumpswap_prime_strict import evaluate_pumpswap_prime_strict, is_pumpswap_prime
from analytics.pumpswap_rebound_prime import evaluate_pumpswap_rebound_prime
from analytics.report_utils import fnum, is_severe_exit, load_candidate_outcomes, load_paper_positions, load_sqlite_positions, metrics_dir, write_json, write_markdown
from analytics.shadow_followup_micro import evaluate_shadow_followup_micro
from analytics.token_time import historical_age_snapshot
from config.config import PROJECT_ROOT
from analytics.report_utils import is_closed_trade, load_deduped_positions


POLICIES = (
    "current",
    "rules_only",
    "fix_missed_only",
    "risk_guard",
    "risk_guard_v2",
    "liq_guard",
    "risk_model_only",
    "rank_canary",
    "research_rank_canary",
    "score_recalibrated",
    "ev_model_only",
    "runner_model_only",
    "pumpswap_prime_strict",
    "pumpswap_rebound_prime",
    "late_momentum_watch",
    "continuation_model",
    "early_dump",
    "early_dump_cut",
    "post_partial_protected",
    "combined_v1",
    "combined_policy_v1",
    "combined_policy_v2",
)

POST_ADJUSTMENT_POLICIES = (
    "baseline_48h",
    "research_rank_priority",
    "green_sniper_shadow_first",
    "green_sniper_restricted",
    "late_momentum_research_only",
    "post_partial_protected",
    "early_dump_candidates",
    "combined_adjusted_v1",
)

SHADOW_FOLLOWUP_REPLAY_KEYS = {
    "SHADOW_FOLLOWUP_MICRO_ENABLED",
    "SHADOW_FOLLOWUP_MICRO_PAPER_ENABLED",
    "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED",
    "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL",
    "SHADOW_FOLLOWUP_MICRO_MAX_OPEN",
    "SHADOW_FOLLOWUP_MICRO_MAX_DAILY_BUYS",
    "SHADOW_FOLLOWUP_TRIGGER_PNL_3M",
    "SHADOW_FOLLOWUP_TRIGGER_PNL_6M",
}


def _base_pnl(row: dict[str, Any]) -> float:
    return fnum(row.get("realized_pnl_pct") or row.get("total_pnl_pct") or row.get("pnl_pct") or row.get("target_total_pnl_pct"), 0.0)


def _parse_env_scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _read_env_values(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}
    env_path = Path(path)
    if not env_path.exists() or not env_path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip().upper()
        if key:
            values[key] = _parse_env_scalar(raw_value)
    return values


def _read_policy_changes(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}
    policy_path = Path(path)
    if not policy_path.exists():
        return {}
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        return {}
    return {str(key).strip().upper(): str(value) for key, value in changes.items() if str(key).strip()}


def _auto_candidate_env_path() -> Path | None:
    raw = str(os.getenv("CONFIG_PROFILE_PATH") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.name.lower() != "candidate.env":
        return None
    return path if path.exists() else None


def _resolve_candidate_inputs(
    *,
    candidate_config: dict[str, Any] | None = None,
    candidate_policy_path: str | Path | None = None,
    candidate_env_path: str | Path | None = None,
) -> tuple[dict[str, str], bool]:
    resolved_env_path = Path(candidate_env_path) if candidate_env_path is not None else _auto_candidate_env_path()
    values = _read_env_values(resolved_env_path)
    if candidate_policy_path is None and resolved_env_path is not None:
        local_policy = resolved_env_path.parent / "candidate_policy.json"
        if local_policy.exists():
            candidate_policy_path = local_policy
    values.update(_read_policy_changes(candidate_policy_path))
    if candidate_config:
        values.update({str(key).strip().upper(): str(value) for key, value in candidate_config.items() if str(key).strip()})
    is_candidate = bool(candidate_config or candidate_policy_path or resolved_env_path)
    return values, is_candidate


def _cfg_namespace(values: dict[str, str]) -> SimpleNamespace:
    defaults = {
        "SHADOW_FOLLOWUP_MICRO_ENABLED": "true",
        "SHADOW_FOLLOWUP_MICRO_PAPER_ENABLED": "true",
        "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED": "false",
        "SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL": "0.003",
        "SHADOW_FOLLOWUP_MICRO_MAX_OPEN": "999999",
        "SHADOW_FOLLOWUP_MICRO_MAX_DAILY_BUYS": "999999",
        "SHADOW_FOLLOWUP_TRIGGER_PNL_3M": "25",
        "SHADOW_FOLLOWUP_TRIGGER_PNL_6M": "50",
    }
    merged = {**defaults, **{key: value for key, value in values.items() if key in SHADOW_FOLLOWUP_REPLAY_KEYS}}
    return SimpleNamespace(**merged)


def _shadow_reason_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(key) or "") for key in ("sample_type", "reason", "action", "shadow_kind", "entry_lane", "gate_profile")).lower()


def _is_shadow_followup_replay_row(row: dict[str, Any], cfg: Any) -> bool:
    reason_text = _shadow_reason_text(row)
    if "shadow" in reason_text or "shadow_followup" in reason_text:
        return True
    return evaluate_shadow_followup_micro(historical_age_snapshot(row), cfg=cfg).reason != "shadow_followup_blocked:no_followup_trigger"


def _shadow_followup_candidate_summary(rows: list[dict[str, Any]], cfg: Any) -> dict[str, Any]:
    decisions = [evaluate_shadow_followup_micro(historical_age_snapshot(row), cfg=cfg) for row in rows]
    allowed_items = [(row, decision) for row, decision in zip(rows, decisions) if decision.allowed]
    blocked_items = [(row, decision) for row, decision in zip(rows, decisions) if not decision.allowed]
    pnls = [_base_pnl(row) for row, _decision in allowed_items]
    severe = sum(1 for row, pnl in zip((row for row, _decision in allowed_items), pnls) if is_severe_exit(row) or pnl <= -25.0)
    liquidity_crush = sum(1 for row, _decision in allowed_items if str(row.get("exit_reason") or row.get("reason") or "").upper() == "LIQUIDITY_CRUSH")
    adverse_tick = sum(1 for row, _decision in allowed_items if str(row.get("exit_reason") or row.get("reason") or "").upper() == "ADVERSE_TICK")
    risk_blocked = sum(
        1
        for _row, decision in blocked_items
        if any(failure != "no_followup_trigger" for failure in decision.failures)
    )
    total = round(sum(pnls), 3)
    avg = round(total / len(pnls), 3) if pnls else 0.0
    median = round(sorted(pnls)[len(pnls) // 2], 3) if pnls else 0.0
    win_rate = round(100.0 * sum(1 for pnl in pnls if pnl > 0) / len(pnls), 3) if pnls else 0.0
    objective_score = round(total + avg * 2.0 + win_rate * 0.05 - severe * 40.0 - liquidity_crush * 35.0 - adverse_tick * 20.0 - risk_blocked * 0.5, 3)
    return {
        "allowed_shadow_followup": len(allowed_items),
        "simulated_buys": len(allowed_items),
        "shadow_followup_total_pnl": total,
        "shadow_followup_avg_pnl": avg,
        "shadow_followup_median_pnl": median,
        "shadow_followup_win_rate": win_rate,
        "shadow_followup_severe_loss_count": severe,
        "shadow_followup_liquidity_crush_count": liquidity_crush,
        "shadow_followup_adverse_tick_count": adverse_tick,
        "shadow_followup_risk_blocked": risk_blocked,
        "shadow_followup_route_proxy": sum(1 for _row, decision in allowed_items if decision.route_proxy),
        "objective_score": objective_score,
    }


def _merge_candidate_current(base_rows: list[dict[str, Any]], shadow_rows: list[dict[str, Any]], cfg: Any) -> dict[str, Any]:
    base = _summarize(base_rows, "current")
    effect = _shadow_followup_candidate_summary(shadow_rows, cfg)
    base_trades = int(base.get("trades") or 0)
    simulated_buys = int(effect["simulated_buys"])
    total_trades = base_trades + simulated_buys
    base_total = fnum(base.get("total_pnl"), 0.0)
    candidate_total = round(base_total + fnum(effect.get("shadow_followup_total_pnl"), 0.0), 3)
    base_wins = round(base_trades * fnum(base.get("win_rate"), 0.0) / 100.0)
    candidate_wins = round(simulated_buys * fnum(effect.get("shadow_followup_win_rate"), 0.0) / 100.0)
    merged = dict(base)
    merged.update(effect)
    merged["trades"] = total_trades
    merged["total_pnl"] = candidate_total
    merged["avg_pnl"] = round(candidate_total / total_trades, 3) if total_trades else 0.0
    if simulated_buys and not base_trades:
        merged["median_pnl"] = effect["shadow_followup_median_pnl"]
    merged["win_rate"] = round(100.0 * (base_wins + candidate_wins) / total_trades, 3) if total_trades else 0.0
    merged["severe_loss_count"] = int(base.get("severe_loss_count") or 0) + int(effect["shadow_followup_severe_loss_count"])
    merged["liq_crush_count"] = int(base.get("liq_crush_count") or 0) + int(effect["shadow_followup_liquidity_crush_count"])
    merged["adverse_tick_count"] = int(base.get("adverse_tick_count") or 0) + int(effect["shadow_followup_adverse_tick_count"])
    return merged


def _simulate(row: dict[str, Any], policy: str) -> float:
    pnl = _base_pnl(row)
    reason = str(row.get("exit_reason") or row.get("reason") or "").upper()
    peak = fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct") or row.get("max_pnl_pct"), pnl)
    combined = policy in {"combined_v1", "combined_policy_v1", "combined_policy_v2"}
    if policy in {"risk_guard", "risk_guard_v2", "risk_model_only"} or combined:
        if reason in {"ADVERSE_TICK", "LIQUIDITY_CRUSH"}:
            return max(pnl, -18.0)
    if policy == "liq_guard" and reason == "LIQUIDITY_CRUSH":
        return max(pnl, -15.0)
    if policy == "risk_model_only":
        return pnl
    if policy == "ev_model_only" and fnum(row.get("ev_pred_pct"), pnl) < 0:
        return 0.0
    if policy == "runner_model_only":
        peak = fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct") or row.get("max_pnl_pct"), pnl)
        return max(pnl, peak * 0.30) if peak >= 100 else pnl
    if policy == "pumpswap_prime_strict":
        if is_pumpswap_prime(row) and not evaluate_pumpswap_prime_strict(row).allowed:
            return 0.0
        return pnl
    if policy == "pumpswap_rebound_prime":
        return pnl if evaluate_pumpswap_rebound_prime(row).allowed else 0.0
    if policy == "continuation_model" and str(row.get("entry_lane") or "") == "pump_early_late_momentum_watch":
        return max(pnl, fnum(row.get("continuation_peak_after_seen_3m"), pnl) * 0.25)
    if policy in {"early_dump", "early_dump_cut"} or combined:
        if pnl < -25 and peak < 15:
            return max(pnl, -12.0)
    if policy in {"post_partial_protected"} or combined:
        if peak >= 100 and pnl > 0:
            capture = 0.40 if policy == "combined_policy_v2" else 0.35
            return max(pnl, peak * capture)
    if policy in {"rank_canary", "research_rank_canary"} and str(row.get("entry_lane") or "").endswith("sniper_research") and fnum(row.get("rank_score"), 0) >= 61:
        return pnl
    return pnl


def _simulate_post_adjustment(row: dict[str, Any], policy: str) -> float:
    pnl = _base_pnl(row)
    reason = str(row.get("exit_reason") or row.get("reason") or "").upper()
    peak = fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct") or row.get("max_pnl_pct"), pnl)
    category = classify_policy_category(row)
    if policy in {"baseline_48h", "research_rank_priority"}:
        return pnl
    if policy == "green_sniper_shadow_first":
        return 0.0 if category in {POLICY_GREEN_SNIPER_PURE, POLICY_GREEN_SNIPER_SHADOW} else pnl
    if policy == "green_sniper_restricted":
        if category in {POLICY_GREEN_SNIPER_PURE, POLICY_GREEN_SNIPER_SHADOW, POLICY_GREEN_SNIPER_RESTRICTED_BUY}:
            return pnl if not restricted_failures(row) else 0.0
        return pnl
    if policy == "late_momentum_research_only":
        return 0.0 if category == POLICY_LATE_MOMENTUM_WATCH else pnl
    if policy == "post_partial_protected":
        if peak >= 35 and pnl > 0:
            return max(pnl, max(20.0, peak - 5.0))
        return pnl
    if policy == "early_dump_candidates":
        if reason == "EARLY_DUMP_CUT" or (pnl < -25 and peak < 15):
            return max(pnl, -12.0)
        return pnl
    if policy == "combined_adjusted_v1":
        if category in {POLICY_GREEN_SNIPER_PURE, POLICY_GREEN_SNIPER_SHADOW, POLICY_GREEN_SNIPER_RESTRICTED_BUY}:
            pnl = pnl if not restricted_failures(row) else 0.0
        if category == POLICY_LATE_MOMENTUM_WATCH:
            pnl = 0.0
        if reason == "EARLY_DUMP_CUT" or (pnl < -25 and peak < 15):
            pnl = max(pnl, -12.0)
        if peak >= 35 and pnl > 0:
            pnl = max(pnl, max(20.0, peak - 5.0))
        return pnl
    return pnl


def _summarize(rows: list[dict[str, Any]], policy: str) -> dict[str, Any]:
    pnls = [_simulate(row, policy) for row in rows]
    if not pnls:
        return {"trades": 0}
    by_category: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[tuple[dict[str, Any], float]]] = {}
    for row, pnl in zip(rows, pnls):
        grouped.setdefault(classify_policy_category(row), []).append((row, pnl))
    for category, items in grouped.items():
        cat_pnls = [pnl for _, pnl in items]
        by_category[category] = {
            "trades": len(cat_pnls),
            "win_rate": round(100.0 * sum(1 for value in cat_pnls if value > 0) / len(cat_pnls), 3),
            "avg_pnl": round(sum(cat_pnls) / len(cat_pnls), 3),
            "severe_loss_count": sum(1 for row, pnl in items if is_severe_exit(row) or pnl <= -25),
        }
    severe = sum(1 for row, pnl in zip(rows, pnls) if is_severe_exit(row) or pnl <= -25)
    return {
        "trades": len(pnls),
        "win_rate": round(100.0 * sum(1 for value in pnls if value > 0) / len(pnls), 3),
        "avg_pnl": round(sum(pnls) / len(pnls), 3),
        "median_pnl": round(sorted(pnls)[len(pnls) // 2], 3),
        "total_pnl": round(sum(pnls), 3),
        "severe_loss_count": severe,
        "missed_confirmed_winners": sum(1 for row in rows if str(row.get("classification") or "") == "confirmed_missed_winner"),
        "avoided_losers": sum(1 for row in rows if str(row.get("classification") or "") == "confirmed_avoided_loser"),
        "max_drawdown_proxy": round(min(0.0, min(pnls)), 3),
        "adverse_tick_count": sum(1 for row in rows if str(row.get("exit_reason") or row.get("reason")).upper() == "ADVERSE_TICK"),
        "liq_crush_count": sum(1 for row in rows if str(row.get("exit_reason") or row.get("reason")).upper() == "LIQUIDITY_CRUSH"),
        "lane_policy_category_breakdown": dict(sorted(by_category.items())),
        "runner_capture_ratio": round(
            sum(max(_simulate(row, policy), 0.0) / max(fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct"), _simulate(row, policy)), 1.0) for row in rows)
            / len(rows),
            4,
        ),
    }


def _summarize_post_adjustment(rows: list[dict[str, Any]], policy: str) -> dict[str, Any]:
    pnls = [_simulate_post_adjustment(row, policy) for row in rows]
    if not pnls:
        return {"trades": 0}
    grouped: dict[str, list[tuple[dict[str, Any], float]]] = {}
    for row, pnl in zip(rows, pnls):
        grouped.setdefault(classify_policy_category(row), []).append((row, pnl))
    by_category: dict[str, dict[str, Any]] = {}
    for category, items in grouped.items():
        cat_pnls = [pnl for _, pnl in items]
        by_category[category] = {
            "trades": len(cat_pnls),
            "win_rate": round(100.0 * sum(1 for value in cat_pnls if value > 0) / len(cat_pnls), 3),
            "avg_pnl": round(sum(cat_pnls) / len(cat_pnls), 3),
            "total_pnl": round(sum(cat_pnls), 3),
            "severe_loss_count": sum(1 for value in cat_pnls if value <= -25),
        }
    return {
        "trades": len(pnls),
        "win_rate": round(100.0 * sum(1 for value in pnls if value > 0) / len(pnls), 3),
        "avg_pnl": round(sum(pnls) / len(pnls), 3),
        "median_pnl": round(sorted(pnls)[len(pnls) // 2], 3),
        "total_pnl": round(sum(pnls), 3),
        "delta_total_pnl_vs_baseline": 0.0,
        "severe_loss_count": sum(1 for value in pnls if value <= -25),
        "max_drawdown_proxy": round(min(0.0, min(pnls)), 3),
        "runner_capture_ratio": round(
            sum(max(pnl, 0.0) / max(fnum(row.get("max_pnl_pct_seen") or row.get("peak_pnl_pct"), pnl), 1.0) for row, pnl in zip(rows, pnls))
            / len(rows),
            4,
        ),
        "lane_policy_category_breakdown": dict(sorted(by_category.items())),
    }


def build_policy_replay(
    root: Path | None = None,
    *,
    candidate_config: dict[str, Any] | None = None,
    candidate_policy_path: str | Path | None = None,
    candidate_env_path: str | Path | None = None,
) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = [row for row in load_candidate_outcomes(root) + load_deduped_positions(root) if is_closed_trade(row)]
    report = {policy: _summarize(rows, policy) for policy in POLICIES}
    resolved_config, is_candidate = _resolve_candidate_inputs(
        candidate_config=candidate_config,
        candidate_policy_path=candidate_policy_path,
        candidate_env_path=candidate_env_path,
    )
    if is_candidate:
        cfg = _cfg_namespace(resolved_config)
        shadow_rows = [row for row in rows if _is_shadow_followup_replay_row(row, cfg)]
        base_rows = [row for row in rows if row not in shadow_rows]
        report["current"] = _merge_candidate_current(base_rows, shadow_rows, cfg)
        report["candidate_config"] = {
            key: resolved_config[key]
            for key in sorted(resolved_config)
            if key in SHADOW_FOLLOWUP_REPLAY_KEYS
        }
        report["candidate_effect"] = {
            "target": "shadow_followup_micro",
            "rows_considered": len(shadow_rows),
            **{
                key: report["current"].get(key)
                for key in (
                    "allowed_shadow_followup",
                    "simulated_buys",
                    "shadow_followup_total_pnl",
                    "shadow_followup_risk_blocked",
                    "objective_score",
                )
            },
        }
    return report


def _baseline_reference(root: Path) -> dict[str, Any]:
    path = metrics_dir(root) / "post_run_48h_baseline.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "source": str(path),
        "window": payload.get("window") or {},
        "global": payload.get("global") or {},
    }


def build_post_adjustment_policy_replay(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    rows = [row for row in load_candidate_outcomes(root) + load_deduped_positions(root) if is_closed_trade(row)]
    policies = {policy: _summarize_post_adjustment(rows, policy) for policy in POST_ADJUSTMENT_POLICIES}
    baseline_total = float((policies.get("baseline_48h") or {}).get("total_pnl") or 0.0)
    baseline_severe = int((policies.get("baseline_48h") or {}).get("severe_loss_count") or 0)
    for stats in policies.values():
        stats["delta_total_pnl_vs_baseline"] = round(float(stats.get("total_pnl") or 0.0) - baseline_total, 3)
        stats["delta_severe_loss_vs_baseline"] = int(stats.get("severe_loss_count") or 0) - baseline_severe
    return {
        "baseline_reference": _baseline_reference(root),
        "policies": policies,
    }


def write_policy_replay(
    root: Path | None = None,
    *,
    candidate_config: dict[str, Any] | None = None,
    candidate_policy_path: str | Path | None = None,
    candidate_env_path: str | Path | None = None,
) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_policy_replay(
        root,
        candidate_config=candidate_config,
        candidate_policy_path=candidate_policy_path,
        candidate_env_path=candidate_env_path,
    )
    write_json(metrics_dir(root) / "policy_replay.json", report)
    lines = ["# Policy Replay", "", "| Policy | Trades | Win rate | Avg PnL | Total PnL | Severe | Runner capture |", "|---|---:|---:|---:|---:|---:|---:|"]
    for key, stats in report.items():
        lines.append(
            f"| {key} | {stats.get('trades', 0)} | {stats.get('win_rate', 0):.2f}% | {stats.get('avg_pnl', 0):.2f}% | "
            f"{stats.get('total_pnl', 0):.2f} | {stats.get('severe_loss_count', 0)} | {stats.get('runner_capture_ratio', 0):.3f} |"
        )
    write_markdown(root / "docs" / "POLICY_REPLAY.md", lines)
    return report


def write_post_adjustment_policy_replay(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    report = build_post_adjustment_policy_replay(root)
    write_json(metrics_dir(root) / "post_adjustment_policy_replay.json", report)
    lines = [
        "# Post-adjustment Policy Replay",
        "",
        "| Policy | Trades | Win rate | Avg PnL | Total PnL | Delta PnL | Severe | Delta Severe | Runner capture |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, stats in report["policies"].items():
        lines.append(
            f"| {key} | {stats.get('trades', 0)} | {stats.get('win_rate', 0):.2f}% | "
            f"{stats.get('avg_pnl', 0):.2f}% | {stats.get('total_pnl', 0):.2f} | "
            f"{stats.get('delta_total_pnl_vs_baseline', 0):.2f} | {stats.get('severe_loss_count', 0)} | "
            f"{stats.get('delta_severe_loss_vs_baseline', 0)} | {stats.get('runner_capture_ratio', 0):.3f} |"
        )
    baseline = report.get("baseline_reference", {}).get("global", {})
    if baseline:
        lines.extend(
            [
                "",
                "## Frozen 48h Baseline Reference",
                "",
                f"- Closed trades: `{baseline.get('count')}`",
                f"- Win rate: `{baseline.get('win_rate_pct')}`",
                f"- Avg PnL: `{baseline.get('avg_pnl_pct')}`",
                f"- Severe losses: `{baseline.get('severe_loss_count')}`",
            ]
        )
    write_markdown(root / "docs" / "POST_ADJUSTMENT_REPLAY.md", lines)
    return report


__all__ = [
    "POLICIES",
    "POST_ADJUSTMENT_POLICIES",
    "build_policy_replay",
    "build_post_adjustment_policy_replay",
    "write_policy_replay",
    "write_post_adjustment_policy_replay",
]
