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
from analytics.policy_replay_semantics import (
    finite_number, reported_value, linear_sum, moments, rounded, replay_scope,
)


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


def _base_pnl(row: dict[str, Any]) -> float | None:
    return reported_value(row, ("realized_pnl_pct","total_pnl_pct","pnl_pct","target_total_pnl_pct"))


def _peak(row: dict[str, Any], default: float | None=None) -> float | None:
    value=reported_value(row,("max_pnl_pct_seen","peak_pnl_pct","max_pnl_pct"))
    return default if value is None else value


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
    valid_items=[(row,decision) for row,decision in allowed_items if _base_pnl(row) is not None]
    pnls = [_base_pnl(row) for row, _decision in valid_items]
    severe = sum(1 for row, pnl in zip((row for row, _decision in valid_items), pnls) if is_severe_exit(row) or pnl <= -25.0)
    liquidity_crush = sum(1 for row, _decision in allowed_items if str(row.get("exit_reason") or row.get("reason") or "").upper() == "LIQUIDITY_CRUSH")
    adverse_tick = sum(1 for row, _decision in allowed_items if str(row.get("exit_reason") or row.get("reason") or "").upper() == "ADVERSE_TICK")
    risk_blocked = sum(
        1
        for _row, decision in blocked_items
        if any(failure != "no_followup_trigger" for failure in decision.failures)
    )
    stats=moments(pnls)
    total,avg,median=(stats[key] for key in ('total_pnl','avg_pnl','median_pnl'))
    win_rate = round(100.0 * sum(1 for pnl in pnls if pnl > 0) / len(pnls), 3) if pnls else 0.0
    # Selecting no observations has a known decision-only penalty. It does not
    # turn unknown financial returns into zero. Selecting rows whose return is
    # unknown, or an unrepresentable aggregate, has no return-based score.
    if not allowed_items:
        objective_score = rounded(-.5 * risk_blocked)
    elif len(valid_items) != len(allowed_items) or total is None or avg is None:
        objective_score = None
    else:
        objective_score = rounded(linear_sum([(total,1.),(avg,2.),(win_rate,.05),
            (severe,-40.),(liquidity_crush,-35.),(adverse_tick,-20.),(risk_blocked,-.5)]))
    return {
        "allowed_shadow_followup": len(allowed_items),
        "simulated_buys": len(valid_items),
        "allowed_with_unknown_return": len(allowed_items)-len(valid_items),
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
    effect = _shadow_followup_candidate_summary(shadow_rows, cfg)
    allowed=[row for row in shadow_rows if evaluate_shadow_followup_micro(historical_age_snapshot(row),cfg=cfg).allowed]
    merged = _summarize(base_rows+allowed, "current")
    merged.update(effect)
    return merged


def _simulate(row: dict[str, Any], policy: str) -> float | None:
    pnl = _base_pnl(row)
    if pnl is None:
        return None
    reason = str(row.get("exit_reason") or row.get("reason") or "").upper()
    peak = _peak(row,pnl)
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
        peak = _peak(row,pnl)
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


def _simulate_post_adjustment(row: dict[str, Any], policy: str) -> float | None:
    pnl = _base_pnl(row)
    if pnl is None:
        return None
    reason = str(row.get("exit_reason") or row.get("reason") or "").upper()
    peak = _peak(row,pnl)
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


def _summarize_scenarios(rows: list[dict[str, Any]], policy: str, *, post: bool=False) -> dict[str, Any]:
    simulate=_simulate_post_adjustment if post else _simulate
    items=[(row,pnl) for row in rows if (pnl:=finite_number(simulate(row,policy))) is not None]
    pnls=[pnl for _,pnl in items]
    if not pnls:
        return {'trades':0,'invalid_return_rows':len(rows),
            'runner_capture_ratio':None,'runner_capture_denominator':0}
    def severe(row,pnl): return pnl<=-25 or (not post and is_severe_exit(row))
    grouped={}
    for row,pnl in items:
        grouped.setdefault(classify_policy_category(row),[]).append((row,pnl))
    by_category={category:{'trades':len(group),**moments([pnl for _,pnl in group]),
        'win_rate':round(100.*sum(pnl>0 for _,pnl in group)/len(group),3),
        'severe_loss_count':sum(severe(row,pnl) for row,pnl in group)}
        for category,group in sorted(grouped.items())}
    captures=[]
    for row,pnl in items:
        peak=_peak(row)
        if peak is not None and peak>0:
            ratio=finite_number(max(pnl,0.)/peak)
            if ratio is not None: captures.append(ratio)
    capture=linear_sum((value,1./len(captures)) for value in captures) if captures else None
    result={'trades':len(items),'invalid_return_rows':len(rows)-len(items),**moments(pnls),
        'win_rate':round(100.*sum(value>0 for value in pnls)/len(pnls),3),
        'severe_loss_count':sum(severe(row,pnl) for row,pnl in items),
        'max_drawdown_proxy':round(min(0.,min(pnls)),3),
        'runner_capture_ratio':rounded(capture,4),'runner_capture_denominator':len(captures),
        'lane_policy_category_breakdown':by_category}
    if post:
        result['delta_total_pnl_vs_baseline']=None
    else:
        result.update({
            'missed_confirmed_winners':sum(row.get('classification')=='confirmed_missed_winner' for row,_ in items),
            'avoided_losers':sum(row.get('classification')=='confirmed_avoided_loser' for row,_ in items),
            'adverse_tick_count':sum(str(row.get('exit_reason') or row.get('reason')).upper()=='ADVERSE_TICK' for row,_ in items),
            'liq_crush_count':sum(str(row.get('exit_reason') or row.get('reason')).upper()=='LIQUIDITY_CRUSH' for row,_ in items)})
    return result


def _summarize(rows: list[dict[str, Any]], policy: str) -> dict[str, Any]:
    return _summarize_scenarios(rows,policy)


def _summarize_post_adjustment(rows: list[dict[str, Any]], policy: str) -> dict[str, Any]:
    return _summarize_scenarios(rows,policy,post=True)


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
    report['_evidence']=replay_scope(input_rows=len(rows))
    report['_evidence'].update(valid_return_rows=sum(_base_pnl(row) is not None for row in rows),
        invalid_return_rows=sum(_base_pnl(row) is None for row in rows))
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
    baseline_total = finite_number((policies.get("baseline_48h") or {}).get("total_pnl"))
    baseline_severe = int((policies.get("baseline_48h") or {}).get("severe_loss_count") or 0)
    for stats in policies.values():
        total=finite_number(stats.get('total_pnl'))
        stats["delta_total_pnl_vs_baseline"] = (rounded(linear_sum([(total,1.),(baseline_total,-1.)]))
            if total is not None and baseline_total is not None else None)
        stats["delta_severe_loss_vs_baseline"] = int(stats.get("severe_loss_count") or 0) - baseline_severe
    return {
        '_evidence':replay_scope(input_rows=len(rows)),
        "baseline_reference": _baseline_reference(root),
        "policies": policies,
    }


def _display(value: Any,digits: int=2) -> str:
    number=finite_number(value)
    return f'{number:.{digits}f}' if number is not None else 'unknown'


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
    lines = ["# Policy Replay", "", "Diagnostic scenarios, not net financial evidence or trading approval.",
        "Total is an unweighted sum of reported percentage points, not portfolio return.", "",
        "| Policy | Outcome rows | Positive rows | Avg reported return | Sum (pp) | Severe | Capture proxy |", "|---|---:|---:|---:|---:|---:|---:|"]
    for key in POLICIES:
        stats=report[key]
        lines.append(
            f"| {key} | {stats.get('trades', 0)} | {_display(stats.get('win_rate'))}% | {_display(stats.get('avg_pnl'))}% | "
            f"{_display(stats.get('total_pnl'))} | {stats.get('severe_loss_count', 0)} | {_display(stats.get('runner_capture_ratio'),3)} |"
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
        "Diagnostic scenarios, not net financial evidence or trading approval.",
        "Total and delta are unweighted reported percentage-point sums, not portfolio returns.",
        "",
        "| Policy | Outcome rows | Positive rows | Avg reported return | Sum (pp) | Delta (pp) | Severe | Delta Severe | Capture proxy |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, stats in report["policies"].items():
        lines.append(
            f"| {key} | {stats.get('trades', 0)} | {_display(stats.get('win_rate'))}% | "
            f"{_display(stats.get('avg_pnl'))}% | {_display(stats.get('total_pnl'))} | "
            f"{_display(stats.get('delta_total_pnl_vs_baseline'))} | {stats.get('severe_loss_count', 0)} | "
            f"{stats.get('delta_severe_loss_vs_baseline', 0)} | {_display(stats.get('runner_capture_ratio'),3)} |"
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
