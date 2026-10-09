"""Checked transport for one paired paper entry-gate experiment.

No exported .env profile or scalar replay result is an activation certificate.
This module validates the complete declared cohort, replays the actual gate
decisions, and rechecks quoted net cash. It does NOT collect new counterfactual
quotes, evaluate shared capital constraints, or prove whole-bot profitability.
Missing counterfactual outcomes keep selection disabled.
"""
from __future__ import annotations

import contextlib
import copy
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
COMPARISON_VERSION = "configured_incumbent_challenger_v1"
MIN_TOKENS = 50
MAX_CASES = 128
MAX_AGE_HOURS = 48
_VERIFIED_CACHE: dict[str, tuple[Any, dict[str, Any]]] = {}


def _time(value: Any) -> dt.datetime:
    result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("aware UTC timestamp required")
    return result.astimezone(dt.timezone.utc)


def plan_identity(plan: dict[str, Any]) -> str:
    mutable = {"enrollment_complete", "case_ids", "enrollment_journal", "enrollment_heartbeats"}
    return policy.digest({k: v for k, v in plan.items() if k not in mutable})


def gate_decision(gate: str, features: dict[str, Any], cfg: Any,
                  *, now: dt.datetime | None = None) -> bool | None:
    from research_loop.entry_gate_forward import suppress_capture
    with suppress_capture():
        return _gate_decision(gate, features, cfg, now=now)


def profile_decision(gate: str, features: dict[str, Any], cfg: Any, parameters: dict[str, Any],
                     *, now: dt.datetime | None = None) -> bool | None:
    with policy.baseline_scope():
        if not parameters:
            return gate_decision(gate, features, cfg, now=now)
        with policy.parameter_scope(cfg, parameters, revision="frozen_counterfactual"):
            return gate_decision(gate, features, cfg, now=now)


def incumbent_profile(plan: dict[str, Any], cfg: Any) -> dict[str, float]:
    """The incumbent is registered before outcomes, never chosen afterwards."""
    if not plan.get("comparison_version"):
        return {}  # Prior first-selection evidence compared with configured settings.
    if plan["comparison_version"] != COMPARISON_VERSION:
        raise ValueError("unknown admission comparison")
    snapshot = plan["incumbent"]
    parameters = snapshot["parameters"]
    if not isinstance(parameters, dict):
        raise ValueError("complete incumbent profile required")
    checked = policy.validate_parameters(cfg, parameters, gate=plan["gate"]) if parameters else {}
    manifest = snapshot["manifest"]
    expected = plan["active_manifest_sha256_at_plan"]
    if expected is not None and re.fullmatch(r"[0-9a-f]{64}", str(expected)) is None:
        raise ValueError("invalid predeclared active manifest identity")
    if manifest is None:
        if checked:
            raise ValueError("unproven incumbent parameters")
        return checked
    planned, selected, expires = _time(plan["planned_at"]), _time(manifest["selected_at"]), _time(manifest["expires_at"])
    if (not checked or manifest.get("parameters") != checked or policy.digest(manifest) != expected
            or manifest.get("version") != VERSION or manifest.get("role") != ROLE
            or re.fullmatch(r"[0-9a-f]{20}", str(manifest.get("revision"))) is None
            or not selected <= planned < expires or expires - selected > dt.timedelta(days=7)
            or re.fullmatch(r"[0-9a-f]{64}\.json", str(manifest.get("evidence_name"))) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("evidence_sha256"))) is None):
        raise ValueError("incompatible or nonprospective incumbent")
    return checked


def _gate_decision(gate: str, features: dict[str, Any], cfg: Any,
                   *, now: dt.datetime | None = None) -> bool | None:
    """The real component, without audit writes or provider requests."""
    for key in ("rank_score", "price_pct_5m", "txns_last_5m", "liquidity_usd",
                "market_cap_usd", "price_impact_pct", "age_minutes"):
        if features.get(key) is not None:
            policy.number(features[key])
    if gate == "rank_canary":
        from analytics.research_rank_canary import evaluate_research_rank_canary
        decision = evaluate_research_rank_canary(
            features, {"rank_score": features.get("rank_score")}, dry_run=True, live=False,
            cfg=cfg, record_audit=False, now=now)
        return None if decision.reason == "not_research_sniper" else decision.allowed
    if gate == "sniper_subprofile":
        from analytics.sniper_research_subprofiles import evaluate_sniper_research_subprofile
        decision = evaluate_sniper_research_subprofile(features, cfg=cfg, now=now)
        if decision.reason in {"not_sniper_research", "subprofiles_disabled"}:
            return None
        return decision.allowed
    if gate == "late_momentum":
        from analytics.late_momentum_watch import evaluate_late_momentum_watch
        decision = evaluate_late_momentum_watch(features, dry_run=True, live=False, cfg=cfg, now=now)
        return decision.action == "buy"
    if gate == "moonshot":
        from analytics.moonshot_micro_lottery import evaluate_moonshot_micro_lottery
        return evaluate_moonshot_micro_lottery(features, dry_run=True, live=False, cfg=cfg, now=now).allowed
    raise ValueError("unsupported entry gate")


def _entry_cash(case: dict[str, Any], plan: dict[str, Any], now: dt.datetime) -> tuple[float, float]:
    from research_loop import runner_forward, entry_gate_forward
    from execution import paper_cash_mark as cash
    from analytics.runner_price_policy import parse_policy
    from utils.sol_price import SolUsdObservation, fresh_sol_usd
    from execution.quote_receipt import valid_summary
    from execution.quote_observation import impact_within_limit
    from fetcher.jupiter_router import SOL_MINT
    prefix, terminal = case["cash"]["prefix"], case["cash"]["terminal"]
    decision_at = _time(case["decision_at"])
    route, model = prefix["entry_route_quote"], prefix["execution_cost_model"]
    quantity = prefix["entry_qty"]
    slippage, fee = policy.number(model["slippage_bps"]), policy.number(model["fee_sol_per_fill"])
    entry_sol_usd = policy.number(prefix["entry_sol_usd"])
    opened = _time(prefix["opened_at"])
    original_fx = fresh_sol_usd(SolUsdObservation(**prefix["entry_fx_observation"]), now=opened.timestamp())
    received = _time(route["observation_receipt"]["other"]["received_at_utc"])
    parameters = parse_policy(plan["runner_exit_policy"])
    if (prefix.get("dry_run") is not True or prefix.get("test_event")
            or plan.get("financial_policy_version") != cash.VERSION or case.get("financial_policy_version") != cash.VERSION
            or prefix.get("closed") is not False or prefix.get("token_address") != case["token"]
            or original_fx is None or original_fx != entry_sol_usd
            or not decision_at <= _time(prefix["entry_quote_started_at"]) <= received <= opened
            or parameters is None or terminal.get("parameters") != parameters
            or parse_policy(prefix.get("runner_trailing_policy")) != parameters
            or prefix.get("run_id") != plan["run_id"]
            or prefix.get("run_started_at") != plan["run_started_at"]
            or not decision_at <= _time(prefix["opened_at"]) <= decision_at + dt.timedelta(seconds=30)
            or prefix.get("amount_sol") != .1 or route.get("in_amount") != 100000000
            or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0
            or not isinstance(route.get("out_amount"), int) or route["out_amount"] <= 0
            or not valid_summary(route, input_mint=SOL_MINT, output_mint=case["token"],
                amount=100000000, not_after=_time(prefix["opened_at"]))
            or isinstance(route["out_amount"], bool)
            or policy.number(route["max_impact_pct"]) <= 0
            or not impact_within_limit(policy.number(route["impact_bps"]), policy.number(route["max_impact_pct"]),
                protocol=route.get("protocol", "metis_v1"))
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
            or case.get("cash_rule") != ("one_common_frozen_entry_and_exit_for_all_gate_arms"
                if plan.get("comparison_version") else "one_common_frozen_entry_and_exit_for_both_gate_arms")):
        raise ValueError("incomplete or incomparable counterfactual coverage")
    row, arm_id = entry_gate_forward.cash_case(case, cohort_ends_at=plan["cohort_ends_at"])
    if not runner_forward._valid_cash_coverage(row, arm_id, terminal):
        raise ValueError("missing own original cash observation coverage")
    for fill in terminal.get("fills") or []:
        if (not isinstance(fill.get("fx_observation"), dict)
                or (not str(fill.get("reason", "")).startswith("TIMEOUT")
                    and fill.get("reason") != "LIQUIDITY_CRUSH" and not fill.get("exit_intent", {}).get("cash_valuation"))):
            raise ValueError("unknown original FX or financial decision mark")
    if not runner_forward.validate_paper_cash_terminal(row, terminal, now):
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
        from execution.paper_cash_mark import VERSION as CASH_VERSION
        if plan.get("financial_policy_version") != CASH_VERSION:
            raise ValueError("unknown original financial policy contract")
        gate = plan["gate"]
        incumbent_parameters = incumbent_profile(plan, cfg)
        candidate = (policy.validate_transition(cfg, incumbent_parameters, plan["parameters"], gate=gate)
            if plan.get("comparison_version") else policy.validate_parameters(cfg, plan["parameters"], gate=gate))
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
        plan_id = plan_identity(plan)
        collector_validated = False
        if plan.get("collector_version"):
            from research_loop.entry_gate_forward import COLLECTOR
            if plan["collector_version"] != COLLECTOR:
                raise ValueError("unknown collector")
            previous = plan_id
            events = plan["enrollment_journal"]
            if len(events) != len(cases):
                raise ValueError("missing original registration")
            for i, (event, case) in enumerate(zip(events, cases)):
                if (event["sequence"] != i or event["previous_sha256"] != previous
                        or event["case_id"] != case["case_id"] or event["token"] != case["token"]
                        or event["captured_at"] != case["decision_at"]
                        or event["features_sha256"] != policy.digest(case["features"])
                        or event["sha256"] != policy.digest({k: v for k, v in event.items() if k != "sha256"})):
                    raise ValueError("altered enrollment journal")
                previous = event["sha256"]
            sampling = plan["sampling"]
            if (sampling.get("method") != "first_eligible_after_interval_and_shared_quote_slot"
                    or sampling.get("future_outcome_used") is not False
                    or not 600 <= policy.number(sampling["interval_s"]) <= 1800
                    or sampling["max_cases"] != MAX_CASES
                    or any((_time(b["captured_at"]) - _time(a["captured_at"])).total_seconds() < sampling["interval_s"]
                           for a, b in zip(events, events[1:]))):
                raise ValueError("invalid predeclared sampler")
            beats = [_time(value) for value in plan["enrollment_heartbeats"]]
            if (not beats or beats[0] != start or beats[-1] < end or beats[-1] > end + dt.timedelta(minutes=5)
                    or any(not 0 < (b - a).total_seconds() <= 300 for a, b in zip(beats, beats[1:]))):
                raise ValueError("incomplete full-window collector uptime")
            collector_validated = True
        ids, tokens, paired_sol, paired_usd, selected_sol, selected_usd, closes = [], set(), [], [], [], [], []
        incumbent_delta_sol, incumbent_delta_usd, direct_sol, direct_usd = [], [], [], []
        changed = changed_incumbent = changed_direct = 0
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
            baseline = profile_decision(gate, case["features"], cfg, {}, now=decision_at)
            challenger = profile_decision(gate, case["features"], cfg, candidate, now=decision_at)
            incumbent = profile_decision(gate, case["features"], cfg, incumbent_parameters, now=decision_at)
            if (baseline is None or challenger is None or incumbent is None or case.get("baseline_buy") is not baseline
                    or case.get("challenger_buy") is not challenger
                    or (plan.get("comparison_version") and case.get("incumbent_buy") is not incumbent)):
                raise ValueError("gate decision or component applicability mismatch")
            if baseline or challenger or incumbent:
                sol, usd = _entry_cash(case, plan, stamp)
                closes.append(_time(case["cash"]["terminal"]["closed_at"]))
            else:
                if case.get("cash") is not None:
                    raise ValueError("all arms skip but cash was attributed")
                sol = usd = 0.0  # Explicit policy skip, never a missing buy/quote.
            changed += baseline != challenger
            changed_incumbent += baseline != incumbent
            changed_direct += incumbent != challenger
            paired_sol.append(sol * (int(challenger) - int(baseline)))
            paired_usd.append(usd * (int(challenger) - int(baseline)))
            incumbent_delta_sol.append(sol * (int(incumbent) - int(baseline)))
            incumbent_delta_usd.append(usd * (int(incumbent) - int(baseline)))
            direct_sol.append(sol * (int(challenger) - int(incumbent)))
            direct_usd.append(usd * (int(challenger) - int(incumbent)))
            selected_sol.append(sol if challenger else 0.0)
            selected_usd.append(usd if challenger else 0.0)
        if (sorted(ids) != sorted(plan["case_ids"]) or max(changed, changed_incumbent, changed_direct) < 10 or not closes
                or (not collector_validated and max(closes) - min(_time(c["decision_at"]) for c in cases) < dt.timedelta(hours=24))
                or stamp - max(closes) > dt.timedelta(hours=MAX_AGE_HOURS)):
            raise ValueError("missing population, effective differences, duration or freshness")
        rng = random.Random(1907)
        boot_sol, boot_usd, boot_direct_sol, boot_direct_usd, boot_incumbent_sol, boot_incumbent_usd = [], [], [], [], [], []
        for _ in range(2000):
            sampled = [rng.randrange(len(cases)) for _ in cases]
            boot_sol.append(statistics.fmean(paired_sol[i] for i in sampled))
            boot_usd.append(statistics.fmean(paired_usd[i] for i in sampled))
            boot_direct_sol.append(statistics.fmean(direct_sol[i] for i in sampled))
            boot_direct_usd.append(statistics.fmean(direct_usd[i] for i in sampled))
            boot_incumbent_sol.append(statistics.fmean(incumbent_delta_sol[i] for i in sampled))
            boot_incumbent_usd.append(statistics.fmean(incumbent_delta_usd[i] for i in sampled))
        successor = bool(incumbent_parameters) and incumbent_parameters != candidate
        # Four simultaneous lower comparisons for successors; two for first
        # selection or revalidation. These diagnostics are not a profit promise.
        bound = 24 if successor else 49
        lower_sol, lower_usd = sorted(boot_sol)[bound], sorted(boot_usd)[bound]
        upper_sol, upper_usd = sorted(boot_sol)[-50], sorted(boot_usd)[-50]
        direct_lower_sol, direct_lower_usd = sorted(boot_direct_sol)[bound], sorted(boot_direct_usd)[bound]
        incumbent_upper_sol, incumbent_upper_usd = sorted(boot_incumbent_sol)[-50], sorted(boot_incumbent_usd)[-50]
        baseline_sol = sum(s - d for s, d in zip(selected_sol, paired_sol))
        baseline_usd = sum(s - d for s, d in zip(selected_usd, paired_usd))
        accepted = (changed >= 10 and lower_sol > 0 and lower_usd > 0 and sum(selected_sol) > 0 and sum(selected_usd) > 0
            and (not successor or (changed_direct >= 10 and direct_lower_sol > 0 and direct_lower_usd > 0)))
        rollback = (changed_incumbent >= 10 and incumbent_upper_sol < 0 and incumbent_upper_usd < 0
                    and baseline_sol > 0 and baseline_usd > 0)
        if not plan.get("comparison_version"):
            rollback = upper_sol < 0 and upper_usd < 0 and baseline_sol > 0 and baseline_usd > 0
        result.update(accepted=accepted, reasons=[] if accepted else ["no_positive_costed_paired_improvement"],
            plan_id=plan_id, gate=gate, parameters=candidate, unique_tokens=len(tokens), changed_decisions=changed,
            net_pnl_sol=sum(selected_sol), net_pnl_usd=sum(selected_usd),
            paired_lower_mean_sol=lower_sol, paired_lower_mean_usd=lower_usd,
            paired_upper_mean_sol=upper_sol, paired_upper_mean_usd=upper_usd,
            comparison_version=plan.get("comparison_version"), incumbent_parameters=incumbent_parameters,
            changed_incumbent_vs_configured=changed_incumbent, changed_challenger_vs_incumbent=changed_direct,
            challenger_vs_incumbent_lower_mean_sol=direct_lower_sol,
            challenger_vs_incumbent_lower_mean_usd=direct_lower_usd,
            incumbent_vs_configured_upper_mean_sol=incumbent_upper_sol,
            incumbent_vs_configured_upper_mean_usd=incumbent_upper_usd,
            incumbent_net_pnl_sol=baseline_sol + sum(incumbent_delta_sol),
            incumbent_net_pnl_usd=baseline_usd + sum(incumbent_delta_usd),
            effective_transition_changes=len({k for k in incumbent_parameters.keys() | candidate.keys()
                if incumbent_parameters.get(k, getattr(cfg, k)) != candidate.get(k, getattr(cfg, k))}),
            selection_action="successor" if successor else ("revalidation" if incumbent_parameters else "first_selection"),
            rollback_to_configured=rollback,
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


def _manifest_times(manifest: dict[str, Any]) -> tuple[dt.datetime, dt.datetime]:
    selected, expires = _time(manifest["selected_at"]), _time(manifest["expires_at"])
    if (manifest.get("version") != VERSION or manifest.get("role") != ROLE
            or not dt.timedelta(0) < expires - selected <= dt.timedelta(days=7)
            or re.fullmatch(r"[0-9a-f]{20}", str(manifest.get("revision"))) is None):
        raise ValueError("invalid paper manifest")
    return selected, expires


def _evidence_sources(directory: Path, manifest: dict[str, Any]):
    """Bounded original evidence, not a scalar accepted flag or recursive chain."""
    _manifest_times(manifest)
    name = str(manifest["evidence_name"])
    if re.fullmatch(r"[0-9a-f]{64}\.json", name) is None:
        raise ValueError("invalid evidence name")
    bundle = _read(directory / "evaluations" / name, inside=directory)
    if policy.digest(bundle) != manifest.get("evidence_sha256"):
        raise ValueError("changed evidence bundle")
    plan = bundle["plan"]
    from research_loop.entry_gate_forward import COLLECTOR, exit_rule_id
    if plan.get("collector_version") != COLLECTOR or plan.get("exit_configuration_id") != exit_rule_id():
        raise ValueError("incompatible collector or exits")
    identity = plan_identity(plan)
    original = _read(directory / "plans" / f"{identity}.json", inside=directory)
    journal = _read(directory / "journals" / f"{identity}.json", inside=directory)
    beats = _read(directory / "heartbeats" / f"{identity}.json", inside=directory)
    if (plan_identity(original) != identity or original.get("enrollment_complete") is True
            or journal.get("events") != plan.get("enrollment_journal")
            or beats.get("times") != plan.get("enrollment_heartbeats")):
        raise ValueError("changed original enrollment")
    if len(plan["case_ids"]) != len(set(plan["case_ids"])) or not MIN_TOKENS <= len(plan["case_ids"]) <= MAX_CASES:
        raise ValueError("invalid original population")
    paths = []
    for case_id in plan["case_ids"]:
        if re.fullmatch(r"[0-9a-f]{64}", str(case_id)) is None:
            raise ValueError("invalid case path")
        path = directory / "closed" / f"{case_id}.json"
        if not path.resolve().is_relative_to(directory):
            raise ValueError("case path escapes research directory")
        metadata = path.stat()
        paths.append((path, metadata.st_mtime_ns, metadata.st_size))
    return bundle, paths


def _replay_manifest(cfg: Any, directory: Path, manifest: dict[str, Any], bundle: dict[str, Any], paths):
    cases = [_read(path, inside=directory) for path, _, _ in paths]
    verified = compare_cohort(bundle["plan"], cases, cfg, now=_time(manifest["selected_at"]))
    if (not verified["accepted"] or verified != bundle["evaluation"]
            or manifest.get("parameters") != verified["parameters"]):
        raise ValueError("manifest has no matching complete accepted cohort")
    return verified


def selection_path(root: Path | str, gate: str, *, for_write: bool = False) -> Path:
    """Known component namespace, with read-only compatibility for old manifests.

    An existing namespace with no active manifest means configured fallback:
    retirement must never resurrect an older legacy manifest.
    """
    if gate not in policy.PREFIXES:
        raise ValueError("unsupported entry component")
    project = Path(root).resolve()
    directory = (project / "data" / "research" / "entry_gate_forward").resolve()
    namespace = directory / "policies" / gate
    if not directory.is_relative_to(project) or not namespace.resolve().is_relative_to(directory):
        raise ValueError("entry component path escapes project")
    path = namespace / "active_policy.json"
    if not path.resolve().is_relative_to(directory):
        raise ValueError("entry manifest path escapes project")
    if for_write or namespace.exists():
        return path
    legacy = directory / "active_policy.json"
    try:
        manifest = _read(legacy, inside=directory)
        keys = manifest["parameters"]
        if keys and all(policy.THRESHOLDS[key].gate == gate for key in keys):
            return legacy
    except (OSError, KeyError, TypeError, ValueError):
        pass
    return path


def load_selection(cfg: Any, *, root: Path | str, now: dt.datetime | None = None,
                   gate: str | None = None) -> dict[str, Any] | None:
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
        # The no-gate API is legacy/single-selection compatibility. Production
        # composition always requests explicit independent components.
        if gate is None:
            available = load_selections(cfg, root=root, now=stamp)
            return next(iter(available.values())) if len(available) == 1 else None
        path = selection_path(project, gate)
        manifest = _read(path, inside=directory)
        selected_at, expires = _manifest_times(manifest)
        if not selected_at <= stamp < expires:
            return None
        bundle, paths = _evidence_sources(directory, manifest)
        plan = bundle["plan"]
        if plan["gate"] != gate:
            return None
        anchor = (plan.get("incumbent") or {}).get("manifest")
        anchor_bundle, anchor_paths = None, []
        if anchor is not None:
            incumbent_profile(plan, cfg)
            original_anchor = _read(directory / "history" / f"{anchor['revision']}.json", inside=directory)
            if original_anchor != anchor:
                return None
            anchor_bundle, anchor_paths = _evidence_sources(directory, anchor)
        signature = policy.digest([manifest, bundle, policy.configured_hash(cfg, plan["gate"]),
            anchor, anchor_bundle, [(str(path), mtime, size) for path, mtime, size in paths + anchor_paths]])
        cache_key = str(path)
        cached = _VERIFIED_CACHE.get(cache_key)
        # Even unchanged metadata gets a full source recheck within five seconds.
        if cached and cached[0] == signature and 0 <= (stamp - _time(cached[1]["verified_at"])).total_seconds() < 5:
            return copy.deepcopy({k: v for k, v in cached[1].items() if k != "verified_at"})
        if anchor is not None:
            _replay_manifest(cfg, directory, anchor, anchor_bundle, anchor_paths)
        # Every candidate is independently checked against configured settings
        # on its own fresh cohort, as well as the frozen immediate incumbent.
        # Replaying arbitrarily many historic ancestors is not needed to prove
        # that comparison and would create an unbounded entry-time workload.
        verified = _replay_manifest(cfg, directory, manifest, bundle, paths)
        selection = {"parameters": verified["parameters"], "revision": manifest["revision"],
                     "evidence_sha256": manifest["evidence_sha256"]}
        if len(_VERIFIED_CACHE) >= 16:
            _VERIFIED_CACHE.pop(next(iter(_VERIFIED_CACHE)))
        _VERIFIED_CACHE[cache_key] = (signature, {**copy.deepcopy(selection), "verified_at": stamp.isoformat()})
        return selection
    except (OSError, KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return None


def load_selections(cfg: Any, *, root: Path | str,
                    now: dt.datetime | None = None) -> dict[str, dict[str, Any]]:
    """A corrupt/expired component falls back without disabling valid peers."""
    stamp = now or dt.datetime.now(dt.timezone.utc)
    return {gate: selection for gate in policy.PREFIXES
            if (selection := load_selection(cfg, root=root, now=stamp, gate=gate)) is not None}


@contextlib.contextmanager
def selected_scope(cfg: Any, *, root: Path | str):
    selections = load_selections(cfg, root=root)
    if not selections:
        with policy.baseline_scope():
            yield
    elif len(selections) == 1:
        # Preserve the old single-component telemetry schema.
        with policy.parameter_scope(cfg, **next(iter(selections.values()))):
            yield
    else:
        with policy.composition_scope(cfg, selections):
            yield
