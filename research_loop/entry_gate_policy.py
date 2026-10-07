"""Checked transport for one paired paper entry-gate experiment.

No exported .env profile or scalar replay result is an activation certificate.
This module validates the complete declared cohort, replays the actual gate
decisions, and rechecks quoted net cash. It does NOT collect new counterfactual
quotes, evaluate shared capital constraints, or prove whole-bot profitability.
Missing counterfactual outcomes keep selection disabled.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import math
import random
import re
import statistics
from pathlib import Path
from typing import Any

from runtime import paper_entry_policy as policy

VERSION = "paired_paper_entry_gate_v1"
ROLE = "paper_entry_gate_only"
MIN_TOKENS = 50
MAX_CASES = 128
MAX_AGE_HOURS = 48
_VERIFIED_CACHE: dict[str, tuple[Any, dict[str, Any]]] = {}


def _time(value: Any) -> dt.datetime:
    result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("aware UTC timestamp required")
    return result.astimezone(dt.timezone.utc)


def gate_decision(gate: str, features: dict[str, Any], cfg: Any) -> bool | None:
    """The real component, without audit writes or provider requests."""
    for key in ("rank_score", "price_pct_5m", "txns_last_5m", "liquidity_usd",
                "market_cap_usd", "price_impact_pct", "age_minutes"):
        if features.get(key) is not None:
            policy.number(features[key])
    if gate == "rank_canary":
        from analytics.research_rank_canary import evaluate_research_rank_canary
        decision = evaluate_research_rank_canary(
            features, {"rank_score": features.get("rank_score")}, dry_run=True, live=False,
            cfg=cfg, record_audit=False)
        return None if decision.reason == "not_research_sniper" else decision.allowed
    if gate == "sniper_subprofile":
        from analytics.sniper_research_subprofiles import evaluate_sniper_research_subprofile
        decision = evaluate_sniper_research_subprofile(features, cfg=cfg)
        if decision.reason in {"not_sniper_research", "subprofiles_disabled"}:
            return None
        return decision.allowed
    if gate == "late_momentum":
        from analytics.late_momentum_watch import evaluate_late_momentum_watch
        decision = evaluate_late_momentum_watch(features, dry_run=True, live=False, cfg=cfg)
        return decision.action == "buy"
    if gate == "moonshot":
        from analytics.moonshot_micro_lottery import evaluate_moonshot_micro_lottery
        return evaluate_moonshot_micro_lottery(features, dry_run=True, live=False, cfg=cfg).allowed
    raise ValueError("unsupported entry gate")


def _entry_cash(case: dict[str, Any], plan: dict[str, Any], now: dt.datetime) -> tuple[float, float]:
    from research_loop.runner_forward import validate_paper_cash_terminal
    prefix, terminal = case["cash"]["prefix"], case["cash"]["terminal"]
    decision_at = _time(case["decision_at"])
    route, model = prefix["entry_route_quote"], prefix["execution_cost_model"]
    quantity = prefix["entry_qty"]
    slippage, fee = policy.number(model["slippage_bps"]), policy.number(model["fee_sol_per_fill"])
    entry_sol_usd = policy.number(prefix["entry_sol_usd"])
    if (prefix.get("dry_run") is not True or prefix.get("test_event")
            or prefix.get("run_id") != plan["run_id"]
            or prefix.get("run_started_at") != plan["run_started_at"]
            or _time(prefix["opened_at"]) != decision_at
            or prefix.get("amount_sol") != .1 or route.get("in_amount") != 100000000
            or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0
            or not isinstance(route.get("out_amount"), int) or route["out_amount"] <= 0
            or not isinstance(route.get("route_count"), int) or route["route_count"] <= 0
            or isinstance(route["out_amount"], bool) or isinstance(route["route_count"], bool)
            or policy.number(route["max_impact_pct"]) <= 0
            or abs(policy.number(route["impact_bps"])) / 100 > policy.number(route["max_impact_pct"])
            or not 0 <= slippage < 10000 or fee < 0 or entry_sol_usd <= 0
            or policy.number(prefix["buy_price_usd"]) <= 0
            or model.get("version") != "estimated-v1" or model.get("observed_execution") is not False
            or quantity != int(route["out_amount"] / (1 + slippage / 10000))
            or prefix.get("qty_lamports") != quantity or prefix.get("realized_qty") != 0
            or prefix.get("realized_proceeds_sol") != 0 or prefix.get("realized_proceeds_usd") != 0
            or prefix.get("execution_fill_count") != 1
            or not math.isclose(policy.number(prefix["entry_notional_usd"]), .1 * entry_sol_usd, abs_tol=1e-9)
            or not math.isclose(policy.number(prefix["estimated_fees_sol"]), fee, abs_tol=1e-12)
            or not math.isclose(policy.number(prefix["estimated_fees_usd"]), fee * entry_sol_usd, abs_tol=1e-9)):
        raise ValueError("invalid exact-entry cash prefix")
    close = _time(terminal["closed_at"])
    if (case.get("observation_gap_limit_exceeded") is not False or case.get("observation_count", 0) < 2
            or case.get("exit_rule_id") != plan["exit_rule_id"] or close > now
            or case.get("cash_rule") != "one_common_frozen_entry_and_exit_for_both_gate_arms"):
        raise ValueError("incomplete or incomparable counterfactual coverage")
    cash_case = {"prefix": prefix, "registered_at": decision_at.isoformat(),
                 "cohort_ends_at": plan["cohort_ends_at"]}
    if not validate_paper_cash_terminal(cash_case, terminal, now):
        raise ValueError("unresolved or nonconserved quoted cash")
    return policy.number(terminal["net_pnl_sol"]), policy.number(terminal["net_pnl_usd"])


def compare_cohort(plan: dict[str, Any], cases: list[dict[str, Any]], cfg: Any,
                   *, now: dt.datetime | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"version": VERSION, "role": ROLE, "accepted": False,
        "observed_execution": False, "full_strategy_profitability_established": False}
    try:
        stamp = now or dt.datetime.now(dt.timezone.utc)
        if getattr(cfg, "DRY_RUN", False) is not True:
            raise ValueError("paper-only component")
        gate = plan["gate"]
        candidate = policy.validate_parameters(cfg, plan["parameters"], gate=gate)
        start, end, planned, run_start = (_time(plan[k]) for k in (
            "cohort_started_at", "cohort_ends_at", "planned_at", "run_started_at"))
        if (plan.get("version") != VERSION or plan.get("role") != ROLE or plan.get("test_event")
                or not plan.get("run_id") or not run_start <= planned <= start < end <= stamp
                or (end - start).total_seconds() != 86400
                or plan.get("configured_hash") != policy.configured_hash(cfg, gate)
                or re.fullmatch(r"[0-9a-f]{64}", str(plan.get("exit_rule_id"))) is None
                or plan.get("enrollment_complete") is not True
                or not MIN_TOKENS <= len(cases) <= MAX_CASES):
            raise ValueError("incomplete, unbound or nonprospective plan")
        plan_id = policy.digest({k: v for k, v in plan.items() if k not in {"enrollment_complete", "case_ids"}})
        ids, tokens, paired_sol, paired_usd, selected_sol, selected_usd, closes = [], set(), [], [], [], [], []
        changed = 0
        for case in cases:
            from utils.solana_addr import is_valid_base58_32
            token, decision_at = case["token"], _time(case["decision_at"])
            if (not isinstance(token, str) or not is_valid_base58_32(token) or token in tokens
                    or case.get("plan_id") != plan_id or case.get("test_event")
                    or not start <= decision_at < end or case.get("outcomes_complete") is not True
                    or case["features"].get("address", token) != token):
                raise ValueError("duplicate, incomplete or out-of-plan case")
            expected_id = policy.digest([plan_id, token, case["decision_at"], case["features"]])
            if case.get("case_id") != expected_id:
                raise ValueError("altered predecision features")
            ids.append(expected_id)
            tokens.add(token)
            with policy.baseline_scope():
                baseline = gate_decision(gate, case["features"], cfg)
                with policy.parameter_scope(cfg, candidate, revision="counterfactual"):
                    challenger = gate_decision(gate, case["features"], cfg)
            if (baseline is None or challenger is None or case.get("baseline_buy") is not baseline
                    or case.get("challenger_buy") is not challenger):
                raise ValueError("gate decision or component applicability mismatch")
            if baseline or challenger:
                sol, usd = _entry_cash(case, plan, stamp)
                closes.append(_time(case["cash"]["terminal"]["closed_at"]))
            else:
                if case.get("cash") is not None:
                    raise ValueError("both arms skip but cash was attributed")
                sol = usd = 0.0  # Explicit policy skip, never a missing buy/quote.
            changed += baseline != challenger
            paired_sol.append(sol * (int(challenger) - int(baseline)))
            paired_usd.append(usd * (int(challenger) - int(baseline)))
            selected_sol.append(sol if challenger else 0.0)
            selected_usd.append(usd if challenger else 0.0)
        if (sorted(ids) != sorted(plan["case_ids"]) or changed < 10 or not closes
                or max(closes) - min(_time(c["decision_at"]) for c in cases) < dt.timedelta(hours=24)
                or stamp - max(closes) > dt.timedelta(hours=MAX_AGE_HOURS)):
            raise ValueError("missing population, effective differences, duration or freshness")
        rng = random.Random(1907)
        boot_sol, boot_usd = [], []
        for _ in range(2000):
            sampled = [rng.randrange(len(cases)) for _ in cases]
            boot_sol.append(statistics.fmean(paired_sol[i] for i in sampled))
            boot_usd.append(statistics.fmean(paired_usd[i] for i in sampled))
        # One predeclared challenger; 2.5% lower marginal bounds for both currencies.
        lower_sol, lower_usd = sorted(boot_sol)[49], sorted(boot_usd)[49]
        accepted = lower_sol > 0 and lower_usd > 0 and sum(selected_sol) > 0 and sum(selected_usd) > 0
        result.update(accepted=accepted, reasons=[] if accepted else ["no_positive_costed_paired_improvement"],
            plan_id=plan_id, gate=gate, parameters=candidate, unique_tokens=len(tokens), changed_decisions=changed,
            net_pnl_sol=sum(selected_sol), net_pnl_usd=sum(selected_usd),
            paired_lower_mean_sol=lower_sol, paired_lower_mean_usd=lower_usd,
            generated_at=stamp.isoformat(),
            case_evidence=[{"case_id": case["case_id"], "sha256": policy.digest(case)} for case in cases])
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, ZeroDivisionError):
        result["reasons"] = ["invalid_or_incomplete_cohort"]
    return result


def _read(path: Path, *, inside: Path) -> dict[str, Any]:
    if not path.resolve().is_relative_to(inside):
        raise ValueError("evidence path escapes the scoped directory")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("oversized evidence object")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


def load_selection(cfg: Any, *, root: Path | str, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """Recheck the original closed cohort; generic profile exports cannot apply."""
    try:
        stamp = now or dt.datetime.now(dt.timezone.utc)
        if (getattr(cfg, "DRY_RUN", False) is not True
                or getattr(cfg, "PAPER_ENTRY_GATE_AUTO_APPLY", True) is not True):
            return None
        project = Path(root).resolve()
        directory = (project / "data" / "research" / "entry_gate_forward").resolve()
        if not directory.is_relative_to(project):
            return None
        manifest = _read(directory / "active_policy.json", inside=directory)
        selected_at, expires = _time(manifest["selected_at"]), _time(manifest["expires_at"])
        if (manifest.get("version") != VERSION or manifest.get("role") != ROLE
                or not selected_at <= stamp < expires
                or expires - selected_at > dt.timedelta(days=7)
                or re.fullmatch(r"[0-9a-f]{20}", str(manifest.get("revision"))) is None):
            return None
        name = str(manifest["evidence_name"])
        if re.fullmatch(r"[0-9a-f]{64}\.json", name) is None:
            return None
        bundle = _read(directory / "evaluations" / name, inside=directory)
        if policy.digest(bundle) != manifest.get("evidence_sha256"):
            return None
        plan, cases = bundle["plan"], []
        if len(plan["case_ids"]) != len(set(plan["case_ids"])) or not MIN_TOKENS <= len(plan["case_ids"]) <= MAX_CASES:
            return None
        paths = []
        for case_id in plan["case_ids"]:
            if re.fullmatch(r"[0-9a-f]{64}", str(case_id)) is None:
                return None
            path = directory / "closed" / f"{case_id}.json"
            metadata = path.stat()
            paths.append((path, metadata.st_mtime_ns, metadata.st_size))
        signature = policy.digest([manifest, bundle, policy.configured_hash(cfg, plan["gate"]),
                                   [(str(path), mtime, size) for path, mtime, size in paths]])
        cached = _VERIFIED_CACHE.get(str(directory))
        # Even unchanged metadata gets a full source recheck within five seconds.
        if cached and cached[0] == signature and 0 <= (stamp - _time(cached[1]["verified_at"])).total_seconds() < 5:
            return {k: v for k, v in cached[1].items() if k != "verified_at"}
        for path, _, _ in paths:
            case = _read(path, inside=directory)
            cases.append(case)
        verified = compare_cohort(plan, cases, cfg, now=selected_at)
        if (not verified["accepted"] or verified != bundle["evaluation"]
                or manifest.get("parameters") != verified["parameters"]):
            return None
        selection = {"parameters": verified["parameters"], "revision": manifest["revision"],
                     "evidence_sha256": manifest["evidence_sha256"]}
        if len(_VERIFIED_CACHE) >= 16:
            _VERIFIED_CACHE.pop(next(iter(_VERIFIED_CACHE)))
        _VERIFIED_CACHE[str(directory)] = (signature, {**selection, "verified_at": stamp.isoformat()})
        return selection
    except (OSError, KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return None


@contextlib.contextmanager
def selected_scope(cfg: Any, *, root: Path | str):
    selection = load_selection(cfg, root=root)
    if selection is None:
        with policy.baseline_scope():
            yield
    else:
        with policy.parameter_scope(cfg, **selection):
            yield
