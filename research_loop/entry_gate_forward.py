"""Prospective sampled entry-gate research, never a transaction sender.

Registration precedes the gate result and any future quote. Both arms share
one costed virtual position and frozen configuration/code identity. Sampling
is declared up front and independent of future returns. This component does
not evaluate downstream admission guards or shared portfolio capital.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import copy
import datetime as dt
import hashlib
import functools
import logging
import math
import os
import re
from pathlib import Path
from typing import Any

from config.config import CFG, PROJECT_ROOT
from runtime import paper_entry_policy as policy
from research_loop import entry_gate_policy as evaluator, forward_budget as storage

COLLECTOR = "sampled_entry_gate_collector_v1"
SCHEDULE = "entry_components_round_robin_v1"
MAX_IDLE_GATE_WAIT_S = 300
_CAPTURE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("entry_gate_capture", default=None)
_SUPPRESS: contextvars.ContextVar[bool] = contextvars.ContextVar("entry_gate_capture_suppressed", default=False)
_TASKS: dict[str, asyncio.Task] = {}
_LOCK = asyncio.Lock()
_CODE_ID: str | None = None
log = logging.getLogger("entry_gate_forward")
_PRIVATE = re.compile(r"secret|password|private|api.?key|authorization|wallet|credential", re.I)
_OUTCOME = re.compile(r"(^target_|^label|pnl|realized|closed_at|exit_reason|peak_after|outcome)", re.I)


def _best_effort(default):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception as exc:
                log.warning("Secondary entry research unavailable: %s", type(exc).__name__)
                return default()
        return wrapped
    return decorate


def directory(root: Path | str | None = None) -> Path:
    project = Path(root or PROJECT_ROOT).resolve()
    result = (project / "data/research/entry_gate_forward").resolve()
    if not result.is_relative_to(project):
        raise ValueError("research directory escapes project")
    return result


def exit_rule_id() -> str:
    """A changed exit configuration/code invalidates, never retunes, a trial."""
    from analytics import exit_policy, runner_ladder, bird_runner_exit, runner_price_policy
    global _CODE_ID
    modules = (exit_policy, runner_ladder, bird_runner_exit, runner_price_policy)
    if _CODE_ID is None:
        _CODE_ID = policy.digest([hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in modules])
    configurations = []
    for module in modules:
        cfg = getattr(module, "CFG", CFG)
        configurations.append({k: v for k, v in vars(cfg).items()
            if not _PRIVATE.search(k) and isinstance(v, (bool, int, float, str))})
    return policy.digest([_CODE_ID, configurations, getattr(exit_policy, "_REGIME_OVERRIDES", {})])


@contextlib.contextmanager
def suppress_capture():
    token = _SUPPRESS.set(True)
    try:
        yield
    finally:
        _SUPPRESS.reset(token)


@contextlib.contextmanager
def capture_scope(cfg: Any, *, root: Path | str, run_context: dict[str, Any] | None = None,
                  allow_test_capture: bool = False, submit_tasks: bool = True):
    from utils.runtime_context import get_runtime_context
    ctx = run_context if run_context is not None else get_runtime_context()
    started = storage.time(ctx.get("started_at"))
    enabled = (getattr(cfg, "DRY_RUN", False) is True
        and getattr(cfg, "PAPER_ENTRY_RESEARCH_ENABLED", False) is True
        and bool(ctx.get("run_id")) and ctx.get("test_event") is not True
        and started is not None and started <= dt.datetime.now(dt.timezone.utc)
        and (allow_test_capture or not os.getenv("PYTEST_CURRENT_TEST"))
        and (allow_test_capture or not os.getenv("MEMEBOT_DISABLE_RUNTIME_AUDIT")))
    value = {"cfg": cfg, "root": Path(root).resolve(), "run_id": str(ctx.get("run_id")),
             "run_started_at": started.isoformat() if started else None,
             "submit_tasks": submit_tasks} if enabled else None
    token = _CAPTURE.set(value)
    try:
        yield
    finally:
        _CAPTURE.reset(token)


def _features(row: dict[str, Any]) -> dict[str, Any]:
    if len(row) > 1024:
        raise ValueError("oversized gate snapshot")
    result = {}
    for key, value in row.items():
        if not isinstance(key, str) or _PRIVATE.search(key) or _OUTCOME.search(key):
            continue
        if value is None or isinstance(value, (bool, int, float, str)):
            if isinstance(value, str) and len(value) > 512:
                continue
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("nonfinite predecision feature")
            result[key] = value
    return result


def proposals(cfg: Any) -> list[tuple[str, dict[str, float]]]:
    """Deterministic neighbors, not unearned profitability scores."""
    definitions = [
        ("rank_canary", {"RESEARCH_RANK_CANARY_MIN_SCORE": -5,
                         "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_RANK_SCORE": -5}),
        ("rank_canary", {"RESEARCH_RANK_CANARY_PRIORITY_MAX_PRICE5M": 50,
                         "RESEARCH_RANK_CANARY_PAPER_NORMAL_MAX_PRICE5M": 50}),
        ("sniper_subprofile", {"SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M": 100}),
        ("sniper_subprofile", {"SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M": -200}),
        ("late_momentum", {"LATE_MOMENTUM_WATCH_MAX_PRICE5M": 250}),
        ("late_momentum", {"LATE_MOMENTUM_WATCH_MIN_PRICE5M": -50}),
        ("moonshot", {"MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M": -50}),
        ("rank_canary", {"RESEARCH_RANK_CANARY_MIN_SCORE": 5}),
        ("sniper_subprofile", {"SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M": 200}),
    ]
    result = []
    for gate, deltas in definitions:
        if gate == "late_momentum" and not (
                getattr(cfg, "LATE_MOMENTUM_WATCH_BUY_ENABLED", False) is True
                and getattr(cfg, "LATE_MOMENTUM_WATCH_PAPER_CANARY_ENABLED", False) is True):
            continue
        try:
            values = {k: policy.number(getattr(cfg, k)) + delta for k, delta in deltas.items()}
            result.append((gate, policy.validate_parameters(cfg, values, gate=gate)))
        except (ValueError, TypeError, AttributeError):
            continue
    return result


def _plan_id(plan: dict[str, Any]) -> str:
    return evaluator.plan_identity(plan)


def incumbent_neighbors(cfg: Any, parameters: dict[str, float]) -> list[tuple[str, dict[str, float]]]:
    """Predeclared adjacent complete profiles, including revalidation/reset.

    A different component cannot replace it using incomparable gate outcomes;
    each component now has its own manifest and local proposal cursor.
    """
    gate = policy.THRESHOLDS[next(iter(parameters))].gate
    candidates = [dict(parameters)]
    for key in sorted(parameters):
        reset = dict(parameters)
        reset.pop(key)
        candidates.append(reset)
        rule = policy.THRESHOLDS[key]
        for delta in (-rule.max_step, rule.max_step):
            neighbor = dict(parameters)
            value = neighbor[key] + delta
            if value == policy.number(getattr(cfg, key)):
                neighbor.pop(key)
            else:
                neighbor[key] = value
            candidates.append(neighbor)
    candidates.extend(values for component, values in proposals(cfg) if component == gate)
    seen, result = set(), []
    for candidate in candidates:
        try:
            checked = policy.validate_transition(cfg, parameters, candidate, gate=gate)
            identity = policy.digest(checked)
            if identity not in seen:
                seen.add(identity)
                result.append((gate, checked))
        except (ValueError, TypeError, AttributeError):
            continue
    return result


def _cursor(base: Path) -> dict[str, Any] | None:
    path = base / "proposal_cursor.json"
    value = storage.read(path)
    if value is None:
        return None if path.exists() else {"index": 0, "component_indices": {}}
    index, components = value.get("index"), value.get("component_indices", {})
    if (not isinstance(index, int) or isinstance(index, bool) or index < 0
            or value.get("version", SCHEDULE) != SCHEDULE or not isinstance(components, dict)
            or any(gate not in policy.PREFIXES or not isinstance(n, int) or isinstance(n, bool) or n < 0
                   for gate, n in components.items())
            or (value.get("waiting_since_at") is not None and storage.time(value["waiting_since_at"]) is None)):
        return None
    return {**value, "component_indices": dict(components)}


def _plan(root: Path, cfg: Any, ctx: dict[str, Any], gate: str, stamp: dt.datetime) -> tuple[str, dict[str, Any]] | None:
    base = directory(root)
    pointer_path = base / "open_plan.json"
    pointer = storage.read(pointer_path)
    if pointer_path.exists() and (not pointer or "plan_id" not in pointer):
        return None
    if pointer:
        if re.fullmatch(r"[0-9a-f]{64}", str(pointer.get("plan_id"))) is None:
            return None
        plan = storage.read(base / "plans" / f"{pointer['plan_id']}.json")
        if plan is None or _plan_id(plan) != pointer["plan_id"]:
            return None  # Never reset a corrupt/incomplete registered population.
        if (plan["run_id"] != ctx["run_id"] or stamp >= storage.time(plan["cohort_ends_at"])
                or plan["configured_hash"] != policy.configured_hash(cfg, plan["gate"])):
            return None
        return (pointer["plan_id"], plan) if plan["gate"] == gate else None
    grouped: dict[str, list[dict[str, float]]] = {}
    for component, values in proposals(cfg):
        grouped.setdefault(component, []).append(values)
    if gate not in grouped:
        return None
    state = _cursor(base)
    if state is None:
        return None
    gates = list(grouped)
    selected_gate = gates[state["index"] % len(gates)]
    wait = storage.time(state.get("waiting_since_at"))
    if wait and stamp < wait:
        return None
    if selected_gate != gate:
        # A rare/absent lane may defer peers, but cannot stall the collector
        # forever. Rotation is time/event-based, never outcome-based.
        if wait is None:
            storage.write(base / "proposal_cursor.json", {**state, "version": SCHEDULE,
                "waiting_since_at": stamp.isoformat()})
            return None
        if (stamp - wait).total_seconds() < MAX_IDLE_GATE_WAIT_S:
            return None
        state = {**state, "version": SCHEDULE, "index": state["index"] + 1,
                 "waiting_since_at": stamp.isoformat()}
        storage.write(base / "proposal_cursor.json", state)
        selected_gate = gates[state["index"] % len(gates)]
        if selected_gate != gate:
            return None
    active_path = evaluator.selection_path(root, gate)
    active = storage.read(active_path)
    if active_path.exists() and (not active or active.get("version") != evaluator.VERSION
            or active.get("role") != evaluator.ROLE
            or re.fullmatch(r"[0-9a-f]{20}", str(active.get("revision"))) is None):
        return None
    selected = evaluator.load_selection(cfg, root=root, now=stamp, gate=gate)
    incumbent = dict(selected["parameters"]) if selected else {}
    choices = incumbent_neighbors(cfg, incumbent) if incumbent else [(gate, values) for values in grouped[gate]]
    if not choices:
        return None
    component_index = state["component_indices"].get(gate, 0)
    index = component_index % len(choices)
    selected_gate, parameters = choices[index]
    if selected_gate != gate:
        return None
    interval = min(1800., max(600., policy.number(getattr(cfg, "PAPER_ENTRY_RESEARCH_SAMPLE_S", 900))))
    from research_loop.runner_forward import entry_policy
    exit_configuration = exit_rule_id()
    frozen_runner = entry_policy(cfg, root=root, now=stamp)
    if selected:
        if (active.get("parameters") != incumbent or active.get("revision") != selected["revision"]
                or active.get("evidence_sha256") != selected["evidence_sha256"]):
            return None
        history_path = base / "history" / f"{active['revision']}.json"
        if history_path.exists() and storage.read(history_path) != active:
            return None
        storage.write(history_path, active)  # Original checked predecessor, before future outcomes.
    plan = {"version": evaluator.VERSION, "role": evaluator.ROLE, "collector_version": COLLECTOR,
        "proposal_schedule": {"version": SCHEDULE, "index": state["index"], "component_index": component_index},
        "comparison_version": evaluator.COMPARISON_VERSION,
        "incumbent": {"parameters": incumbent, "manifest": copy.deepcopy(active) if selected else None},
        "active_manifest_sha256_at_plan": policy.digest(active) if active else None,
        "gate": gate, "parameters": parameters, "configured_hash": policy.configured_hash(cfg, gate),
        "run_id": ctx["run_id"], "run_started_at": ctx["run_started_at"], "planned_at": stamp.isoformat(),
        "cohort_started_at": stamp.isoformat(), "cohort_ends_at": (stamp + dt.timedelta(hours=24)).isoformat(),
        "exit_configuration_id": exit_configuration, "runner_exit_policy": frozen_runner,
        "exit_rule_id": policy.digest([exit_configuration, frozen_runner]),
        "sampling": {"method": "first_eligible_after_interval_and_shared_quote_slot",
        "interval_s": interval, "max_cases": evaluator.MAX_CASES, "future_outcome_used": False}}
    identity = _plan_id(plan)
    storage.write(base / "plans" / f"{identity}.json", plan)  # Before any case or future quote.
    storage.write(base / "open_plan.json", {"plan_id": identity})
    storage.write(base / "heartbeats" / f"{identity}.json", {"times": [stamp.isoformat()]})
    return identity, plan


@_best_effort(set)
def active_tokens(root: Path | str | None = None) -> set[str]:
    return {case["token"] for path in (directory(root) / "active").glob("*.json")
            if (case := storage.read(path)) and isinstance(case.get("token"), str)}


@_best_effort(lambda: False)
def has_quote_demand(root: Path | str | None = None) -> bool:
    for path in (directory(root) / "active").glob("*.json"):
        case = storage.read(path)
        if case and (case.get("cash") or {}).get("terminal", {}).get("intent"):
            return True
    return False


def capture_gate(gate: str, row: dict[str, Any], cfg: Any, *, now: dt.datetime | None = None) -> str | None:
    context = _CAPTURE.get()
    if context is None or _SUPPRESS.get() or context["cfg"] is not cfg:
        return None
    try:
        from utils.solana_addr import is_valid_base58_32
        from research_loop import runner_forward
        stamp, root = now or dt.datetime.now(dt.timezone.utc), context["root"]
        if storage.time(context["run_started_at"]) > stamp:
            return None
        features = _features(row)
        mint = str(features.get("address") or features.get("mint") or "")
        if not is_valid_base58_32(mint):
            return None
        features["address"] = mint
        chosen = _plan(root, cfg, context, gate, stamp)
        if chosen is None:
            return None
        plan_id, plan = chosen
        base = directory(root)
        journal_path = base / "journals" / f"{plan_id}.json"
        journal = storage.read(journal_path)
        if journal is None and journal_path.exists():
            return None
        journal = journal or {"events": []}
        events = journal["events"]
        if (len(events) >= evaluator.MAX_CASES or any(e["token"] == mint for e in events)
                or (events and (stamp - storage.time(events[-1]["captured_at"])).total_seconds() < plan["sampling"]["interval_s"])
                or len(active_tokens(root) | runner_forward.active_tokens(root)) >= 128):
            return None  # Predeclared sample/capacity selection, not outcome censoring.
        with suppress_capture(), policy.baseline_scope():
            incumbent_parameters = evaluator.incumbent_profile(plan, cfg)
            baseline = evaluator.profile_decision(gate, features, cfg, {})
            challenger = evaluator.profile_decision(gate, features, cfg, plan["parameters"])
            incumbent_buy = evaluator.profile_decision(gate, features, cfg, incumbent_parameters)
        if baseline is None or challenger is None or incumbent_buy is None:
            return None
        case_id = policy.digest([plan_id, mint, stamp.isoformat(), features])
        if not storage.claim(root, "entry_gate", other_pending=runner_forward.has_quote_demand(root), now=stamp, request_id=case_id):
            return None
        event = {"sequence": len(events), "case_id": case_id, "token": mint, "captured_at": stamp.isoformat(),
                 "features_sha256": policy.digest(features), "previous_sha256": events[-1]["sha256"] if events else plan_id}
        event["sha256"] = policy.digest(event)
        events.append(event)
        storage.write(base / "journals" / f"{plan_id}.json", journal)  # Registration before case/quote.
        case = {"collector_version": COLLECTOR, "plan_id": plan_id, "case_id": case_id, "token": mint,
            "decision_at": stamp.isoformat(), "features": features, "baseline_buy": baseline, "challenger_buy": challenger,
            "incumbent_buy": incumbent_buy,
            "exit_rule_id": plan["exit_rule_id"], "cash_rule": "one_common_frozen_entry_and_exit_for_all_gate_arms",
            "cash": None, "outcomes_complete": not (baseline or challenger or incumbent_buy), "observation_count": 0,
            "observation_gap_limit_exceeded": False, "last_observed_at": stamp.isoformat()}
        state = "active" if baseline or challenger or incumbent_buy else "closed"
        storage.write(base / state / f"{case_id}.json", case)
        if state == "active" and context["submit_tasks"]:
            task = asyncio.get_running_loop().create_task(fill_entry(case_id, root=root, cfg=cfg))
            _TASKS[case_id] = task
            def completed(task, key=case_id):
                if _TASKS.get(key) is task:
                    _TASKS.pop(key, None)
                if not task.cancelled() and task.exception() is not None:
                    log.warning("Secondary entry dispatcher unavailable: %s", type(task.exception()).__name__)
            task.add_done_callback(completed)
        return case_id
    except Exception as exc:
        log.warning("Entry research registration unavailable: %s", type(exc).__name__)
        return None  # Research must not abort a primary trading decision.


def _archive(base: Path, path: Path, case: dict[str, Any], state: str) -> None:
    if state not in {"invalid", "closed"} or re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is None:
        raise ValueError("invalid archive identity")
    destination = base / state / path.name
    if not all(p.resolve().is_relative_to(base) for p in (path, destination)):
        raise ValueError("archive path escapes research directory")
    if destination.exists() and storage.read(destination) != case:
        raise ValueError("conflicting archived outcome must be preserved")
    storage.write(path, case)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, destination)


async def fill_entry(case_id: str, *, root: Path | str, cfg: Any = None, now: dt.datetime | None = None,
                     prices_func=None, quote_func=None, sol_price_func=None) -> bool:
    cfg = CFG if cfg is None else cfg
    if getattr(cfg, "DRY_RUN", False) is not True or re.fullmatch(r"[0-9a-f]{64}", str(case_id)) is None:
        return False
    base = directory(root)
    path = base / "active" / f"{case_id}.json"
    case = storage.read(path)
    if not case or case.get("cash") is not None or case.get("entry_quote_dispatched"):
        return False
    stamp = now or dt.datetime.now(dt.timezone.utc)
    if not 0 <= (stamp - storage.time(case["decision_at"])).total_seconds() <= 30:
        case["invalid_reason"] = "entry_quote_deadline_exceeded"
        _archive(base, path, case, "invalid")
        return False
    reservation = storage.read(Path(root).resolve() / "data/research/paired_forward_budget.json") or {}
    if reservation.get("owner") != "entry_gate" or reservation.get("request_id") != case_id:
        return False
    case["entry_quote_dispatched"] = True
    storage.write(path, case)  # A duplicate/restarted dispatcher cannot send another entry quote.
    from analytics.api_budget import provider_status, record_provider_event
    from fetcher import jupiter_price, jupiter_router
    from utils.sol_price import get_sol_usd
    from trader.papertrading import _cost_model, quote_impact_limit_pct
    from analytics import runner_ladder
    from research_loop import runner_forward
    if provider_status("jupiter").get("degraded"):
        case["invalid_reason"] = "entry_provider_degraded"
        _archive(base, path, case, "invalid")
        return False
    async def fresh_prices(tokens):
        return await jupiter_price.get_many_usd_prices(tokens, force_refresh=True)
    prices_func = prices_func or fresh_prices
    quote_func = quote_func or jupiter_router.get_routing_quote
    sol_price_func = sol_price_func or get_sol_usd
    try:
        prices, sol_usd = await asyncio.gather(prices_func([case["token"]]), sol_price_func())
        price, sol_usd = policy.number(prices.get(case["token"])), policy.number(sol_usd)
        model = _cost_model()
        if price <= 0 or sol_usd <= 0 or model is None:
            raise ValueError("unknown entry price or costs")
        quote = await quote_func(input_mint=jupiter_router.SOL_MINT, output_mint=case["token"], amount_lamports=100000000)
        sol_usd = policy.number(await sol_price_func())
        if sol_usd <= 0:
            raise ValueError("entry FX expired or unavailable after quote")
        if (getattr(quote, "other", None) or {}).get("status") == 429:
            record_provider_event("jupiter", "429")
        limit = policy.number(quote_impact_limit_pct(cfg))
        from execution.quote_receipt import capture_summary
        filled = now or dt.datetime.now(dt.timezone.utc)
        route = capture_summary(quote, input_mint=jupiter_router.SOL_MINT, output_mint=case["token"],
            amount=100000000, slippage=jupiter_router.routing_quote_slippage_bps(), limit=limit, now=filled)
        output = route["out_amount"]
        if limit <= 0:
            raise ValueError("invalid exact 0.1 SOL entry quote")
        latest = storage.read(path)
        if not latest or latest.get("cash") is not None:
            return False
        if (filled - storage.time(case["decision_at"])).total_seconds() > 30:
            raise ValueError("entry quote too late")
        plan = storage.read(base / "plans" / f"{case['plan_id']}.json")
        if not plan or plan["exit_configuration_id"] != exit_rule_id():
            raise ValueError("exit rule changed")
        quantity = int(output / (1 + model["slippage_bps"] / 10000))
        if quantity <= 0:
            raise ValueError("empty raw SPL entry")
        lanes = {"rank_canary": "pump_early_research_rank_canary", "sniper_subprofile": "pump_early_sniper_research",
                 "late_momentum": "pump_early_late_momentum_watch", "moonshot": "pump_early_moonshot_micro_lottery"}
        prefix = {"dry_run": True, "run_id": plan["run_id"], "run_started_at": plan["run_started_at"],
            "opened_at": filled.isoformat(), "amount_sol": .1, "entry_sol_usd": sol_usd, "entry_notional_usd": .1 * sol_usd,
            "buy_price_usd": price * (1 + model["slippage_bps"] / 10000), "entry_qty": quantity,
            "qty_lamports": quantity, "realized_qty": 0, "realized_proceeds_sol": 0., "realized_proceeds_usd": 0.,
            "execution_fill_count": 1, "estimated_fees_sol": model["fee_sol_per_fill"],
            "estimated_fees_usd": model["fee_sol_per_fill"] * sol_usd, "execution_cost_model": model,
            "entry_route_quote": route,
            "quantity_basis": "quoted_raw_spl_units", "entry_regime": "pump_early", "entry_lane": lanes[plan["gate"]],
            "buy_liquidity_usd": case["features"].get("liquidity_usd"), "partial_taken": False,
            "partial_count": 0, "partial_fill_events": 0, "highest_pnl_pct": 0., "max_pnl_pct_seen": 0.,
            "runner_trailing_policy": plan["runner_exit_policy"]}
        prefix["partial_ladder_state"] = runner_ladder.encode_ladder_state(runner_ladder.initial_ladder_state())
        latest["cash"] = {"prefix": prefix, "terminal": {"subject": copy.deepcopy(prefix), "fills": [], "closed": False}}
        latest["last_observed_at"] = filled.isoformat()
        latest["observation_count"] = 1
        storage.write(path, latest)
        return True
    except Exception as exc:
        latest = storage.read(path)
        if latest and latest.get("cash") is None:
            latest["invalid_reason"] = "entry_quote_or_price_unresolved"
            _archive(base, path, latest, "invalid")
        log.debug("Entry research quote unavailable: %s", type(exc).__name__)
        return False


def _observe(case: dict[str, Any], price: Any, stamp: dt.datetime, plan: dict[str, Any], *, liq_now=None) -> None:
    from research_loop.runner_forward import paper_exit_request
    previous = storage.time(case["last_observed_at"])
    if previous is None or stamp < previous:
        return
    configuration_unchanged = plan["exit_configuration_id"] == exit_rule_id()
    if (stamp - previous).total_seconds() > 300 or not configuration_unchanged:
        case["observation_gap_limit_exceeded"] = True
    try:
        price = policy.number(price)
    except (TypeError, ValueError):
        price = None
    if price is not None and price > 0 and stamp > previous:
        case["last_observed_at"] = stamp.isoformat()
        case["observation_count"] += 1
    if configuration_unchanged:
        paper_exit_request(case["cash"]["terminal"], price, stamp, liq_now=liq_now)


@_best_effort(lambda: 0)
def observe_market(token: str, price: Any, *, root: Path | str, cfg: Any = None,
                   now: dt.datetime | None = None, liq_now=None) -> int:
    cfg = CFG if cfg is None else cfg
    if getattr(cfg, "DRY_RUN", False) is not True or getattr(cfg, "PAPER_ENTRY_RESEARCH_ENABLED", False) is not True:
        return 0
    count, base, stamp = 0, directory(root), now or dt.datetime.now(dt.timezone.utc)
    for path in (base / "active").glob("*.json"):
        case = storage.read(path)
        if case and case["token"] == token and case.get("cash"):
            plan = storage.read(base / "plans" / f"{case['plan_id']}.json")
            if plan:
                _observe(case, price, stamp, plan, liq_now=liq_now)
                storage.write(path, case)
                count += 1
    return count


@_best_effort(lambda: 0)
def observe_quote(token: str, quote: Any, sol_usd: float, *, root: Path | str, cfg: Any = None,
                  now: dt.datetime | None = None) -> int:
    from research_loop.runner_forward import apply_paper_exit_quote
    cfg = CFG if cfg is None else cfg
    if getattr(cfg, "DRY_RUN", False) is not True or getattr(cfg, "PAPER_ENTRY_RESEARCH_ENABLED", False) is not True:
        return 0
    count, base, stamp = 0, directory(root), now or dt.datetime.now(dt.timezone.utc)
    for path in (base / "active").glob("*.json"):
        case = storage.read(path)
        if case and case["token"] == token and case.get("cash"):
            terminal = case["cash"]["terminal"]
            if terminal.get("intent", {}).get("quantity") == getattr(quote, "in_amount", None):
                cash_case = {"prefix": case["cash"]["prefix"], "token": case["token"]}
                if apply_paper_exit_quote(cash_case, terminal, quote, sol_usd, stamp):
                    count += 1
                    if terminal["closed"]:
                        case["outcomes_complete"] = True
                        _archive(base, path, case, "closed")
                    else:
                        storage.write(path, case)
    return count


def evaluate_plan(root: Path | str, cfg: Any, plan_id: str, *, now: dt.datetime) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{64}", str(plan_id)) is None:
        return {"status": "invalid_plan_identity"}
    base = directory(root)
    plan = storage.read(base / "plans" / f"{plan_id}.json")
    if not plan or now < storage.time(plan["cohort_ends_at"]):
        return {"status": "enrolling"}
    journal = storage.read(base / "journals" / f"{plan_id}.json") or {"events": []}
    beats = storage.read(base / "heartbeats" / f"{plan_id}.json") or {"times": []}
    completed = {**plan, "enrollment_complete": True, "case_ids": [event["case_id"] for event in journal["events"]],
                 "enrollment_journal": journal["events"], "enrollment_heartbeats": beats["times"]}
    cases = []
    for case_id in completed["case_ids"]:
        case = storage.read(base / "closed" / f"{case_id}.json")
        if case is None:
            if (base / "invalid" / f"{case_id}.json").exists():
                return {"status": "rejected_uncosted_enrollment", "accepted": False}
            return {"status": "awaiting_settled_outcomes"}
        cases.append(case)
    with suppress_capture():
        report = evaluator.compare_cohort(completed, cases, cfg, now=now)
    bundle = {"plan": completed, "evaluation": report}
    name = policy.digest(bundle) + ".json"
    storage.write(base / "evaluations" / name, bundle)
    previous_path = evaluator.selection_path(root, plan["gate"])
    previous = storage.read(previous_path)
    if previous_path.exists() and (not previous or re.fullmatch(r"[0-9a-f]{20}", str(previous.get("revision"))) is None):
        return {"status": "retained_corrupt_manifest", "accepted": False}
    if getattr(cfg, "PAPER_ENTRY_GATE_AUTO_APPLY", False) is not True:
        return {"status": "evaluated_auto_apply_disabled", "accepted": report["accepted"], "evaluation": name}
    if plan.get("comparison_version"):
        current_identity = policy.digest(previous) if previous else None
        if current_identity != plan["active_manifest_sha256_at_plan"]:
            return {"status": "retained_changed_incumbent", "accepted": False, "evaluation": name}
    if report["accepted"]:
        if previous and not plan.get("comparison_version"):
            return {"status": "retained_incumbent_requires_direct_comparison"}
        revision = policy.digest([plan_id, now.isoformat()])[:20]
        manifest = {"version": evaluator.VERSION, "role": evaluator.ROLE, "revision": revision,
            "selected_at": now.isoformat(), "expires_at": (now + dt.timedelta(days=7)).isoformat(),
            "evidence_name": name, "evidence_sha256": policy.digest(bundle), "parameters": report["parameters"],
            "action": report["selection_action"], "previous_manifest_sha256": policy.digest(previous) if previous else None}
        if previous:
            history_path = base / "history" / f"{previous['revision']}.json"
            if history_path.exists() and storage.read(history_path) != previous:
                return {"status": "retained_conflicting_history", "accepted": False, "evaluation": name}
            storage.write(history_path, previous)
        storage.write(base / "history" / f"{revision}.json", manifest)
        storage.write(evaluator.selection_path(root, plan["gate"], for_write=True), manifest)
        return {"status": "selected", "action": manifest["action"], "accepted": True, "evaluation": name}
    rollback_parameters = report.get("incumbent_parameters") if plan.get("comparison_version") else plan["parameters"]
    if report.get("rollback_to_configured") and previous and previous.get("parameters") == rollback_parameters:
        retirement = policy.digest([previous, name])
        storage.write(base / "rollbacks" / f"{retirement}.json", {"action": "rollback_to_configured",
            "previous_manifest": previous, "evidence_name": name, "evidence_sha256": policy.digest(bundle)})
        destination = base / "history" / f"{previous['revision']}_retired.json"
        if not destination.resolve().is_relative_to(base):
            return {"status": "invalid_history_scope", "accepted": False}
        if destination.exists() and storage.read(destination) != previous:
            return {"status": "retained_conflicting_history", "accepted": False, "evaluation": name}
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Keep the namespace after retirement, including legacy retirement,
        # so an older root manifest can never become active again.
        evaluator.selection_path(root, plan["gate"], for_write=True).parent.mkdir(parents=True, exist_ok=True)
        os.replace(previous_path, destination)
        return {"status": "rolled_back_to_configured", "evaluation": name}
    return {"status": "evaluated", "accepted": report["accepted"], "evaluation": name}


async def tick(*, root: Path | str, cfg: Any = None, now: dt.datetime | None = None,
               prices_func=None, quote_func=None, sol_price_func=None) -> dict[str, Any]:
    cfg = CFG if cfg is None else cfg
    if getattr(cfg, "DRY_RUN", False) is not True or getattr(cfg, "PAPER_ENTRY_RESEARCH_ENABLED", False) is not True:
        return {"status": "disabled", "quote_calls": 0}
    async with _LOCK:
        from research_loop import runner_forward
        from analytics.api_budget import provider_status, record_provider_event
        from fetcher import jupiter_price, jupiter_router
        from utils.sol_price import get_sol_usd
        base, stamp = directory(root), now or dt.datetime.now(dt.timezone.utc)
        pointer = storage.read(base / "open_plan.json")
        if not pointer:
            return {"status": "idle", "quote_calls": 0}
        plan_id = pointer["plan_id"]
        if re.fullmatch(r"[0-9a-f]{64}", str(plan_id)) is None:
            return {"status": "invalid_plan_identity", "quote_calls": 0}
        plan = storage.read(base / "plans" / f"{plan_id}.json")
        if not plan or _plan_id(plan) != plan_id:
            return {"status": "corrupt_plan", "quote_calls": 0}
        beats_path = base / "heartbeats" / f"{plan_id}.json"
        beats = storage.read(beats_path)
        if beats is None:
            return {"status": "corrupt_heartbeat", "quote_calls": 0}
        if beats["times"] and stamp < storage.time(beats["times"][-1]):
            return {"status": "backwards_clock", "quote_calls": 0}
        if stamp <= storage.time(plan["cohort_ends_at"]) + dt.timedelta(minutes=5):
            if not beats["times"] or (stamp - storage.time(beats["times"][-1])).total_seconds() >= 60:
                beats["times"].append(stamp.isoformat())
                storage.write(beats_path, beats)
        clock_path = base / "tick_clock.json"
        clock = storage.read(clock_path)
        if clock is None and clock_path.exists():
            return {"status": "corrupt_clock", "quote_calls": 0}
        clock = clock or {}
        previous = storage.time(clock.get("last_tick_at"))
        if clock_path.exists() and previous is None:
            return {"status": "corrupt_clock", "quote_calls": 0}
        if previous and (stamp - previous).total_seconds() < 60:
            return {"status": "throttled", "quote_calls": 0}
        storage.write(base / "tick_clock.json", {"last_tick_at": stamp.isoformat()})
        paths = sorted((base / "active").glob("*.json"))[:evaluator.MAX_CASES]
        cases = [(p, c) for p in paths if (c := storage.read(p)) is not None]
        async def fresh_prices(tokens):
            return await jupiter_price.get_many_usd_prices(tokens, force_refresh=True)
        prices_func = prices_func or fresh_prices
        quote_func = quote_func or jupiter_router.get_routing_quote
        sol_price_func = sol_price_func or get_sol_usd
        try:
            prices = {} if provider_status("jupiter").get("degraded") else await prices_func(sorted({c["token"] for _, c in cases if c.get("cash")}))
        except Exception:
            prices = {}
        requests = []
        for path, _ in cases:
            case = storage.read(path)
            if not case:
                continue
            if not case.get("cash"):
                if (stamp - storage.time(case["decision_at"])).total_seconds() > 30 and case["case_id"] not in _TASKS:
                    case["invalid_reason"] = "entry_quote_unresolved_after_restart"
                    _archive(base, path, case, "invalid")
                continue
            _observe(case, prices.get(case["token"]), stamp, plan)
            terminal = case["cash"]["terminal"]
            storage.write(path, case)  # Intent persists before any network await.
            if terminal.get("intent"):
                requests.append((terminal["intent"]["requested_at"], case["case_id"], case["token"], terminal["intent"]["quantity"]))
        requests.sort()
        quote_calls = 0
        if (requests and not provider_status("jupiter").get("degraded")
                and storage.claim(root, "entry_gate", other_pending=runner_forward.has_quote_demand(root), now=stamp)):
            _, _, mint, quantity = requests[0]
            try:
                sol_usd = await sol_price_func()
                quote_calls = 1
                quote = await quote_func(input_mint=mint, output_mint=jupiter_router.SOL_MINT, amount_lamports=quantity)
                sol_usd = await sol_price_func()  # recheck FX after network wait
                if (getattr(quote, "other", None) or {}).get("status") == 429:
                    record_provider_event("jupiter", "429")
                observe_quote(mint, quote, sol_usd, root=root, cfg=cfg, now=now)
            except Exception:
                pass  # Pending quantities stay unknown, not filled or zero-return.
        deadline = storage.time(plan["cohort_ends_at"]) + dt.timedelta(hours=48)
        for path in list((base / "active").glob("*.json")):
            case = storage.read(path)
            if case and case["plan_id"] == plan_id and stamp > deadline:
                case["invalid_reason"] = "settlement_deadline_exceeded"
                _archive(base, path, case, "invalid")
        result = evaluate_plan(root, cfg, plan_id, now=stamp)
        if stamp >= storage.time(plan["cohort_ends_at"]) and not any(
                (c := storage.read(p)) and c.get("plan_id") == plan_id for p in (base / "active").glob("*.json")):
            state = _cursor(base)
            if state is None:
                return {"status": "corrupt_schedule", "quote_calls": quote_calls}
            scheduled = plan.get("proposal_schedule")
            local = state["component_indices"].get(plan["gate"], 0)
            if scheduled:
                before = (scheduled["index"], scheduled["component_index"])
                after = (before[0] + 1, before[1] + 1)
                current = (state["index"], local)
                if scheduled.get("version") != SCHEDULE or current not in (before, after):
                    return {"status": "changed_schedule", "quote_calls": quote_calls}
                advance = current == before
            else:
                advance = True  # Older registered cohorts keep their original proof.
            if advance:
                state["component_indices"][plan["gate"]] = local + 1
                storage.write(base / "proposal_cursor.json", {**state, "version": SCHEDULE,
                    "index": state["index"] + 1, "waiting_since_at": stamp.isoformat()})
            storage.write(base / "completed" / f"{plan_id}.json", result)
            (base / "open_plan.json").unlink(missing_ok=True)
        return {"status": "observed", "quote_calls": quote_calls, "cases": len(cases), "selection": result}


async def stop_background_tasks() -> None:
    tasks = list(_TASKS.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _TASKS.clear()
