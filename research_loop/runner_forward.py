"""Prospective, paired paper runner-exit research. Never sends a transaction.

All arms share a real paper entry and its first partial. Subsequent partials
and closes are independently quoted in raw SPL units. Unresolved arms remain
unresolved (not zero-return trades). Only new paper entries consume a selected
manifest; positions already open retain their immutable entry snapshot.
This is a runner-exit experiment, not an evaluation of the complete entry bot.
"""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import hashlib
import json
import logging
import math
import os
import random
import re
import statistics
from pathlib import Path
from typing import Any

from analytics import exit_policy, runner_ladder, runner_price_policy
from config.config import CFG, PROJECT_ROOT
from utils.atomic_json import read_json_strict, write_json_atomic
from runtime.paper_archive import entry_identity
from execution.quote_receipt import capture_summary, valid_summary
from execution import paper_cash_mark as cash
from research_loop.paper_exit_receipt import make_intent, valid_intent, causal_quote, financial_basis

VERSION = "paired_paper_runner_forward_v1"
MIN_TOKENS = 50
ENROLLMENT_HOURS = 24
MAX_SETTLEMENT_HOURS = 48
MAX_EVIDENCE_AGE_HOURS = 48
SELECTION_LIFETIME_DAYS = 7
MAX_ACTIVE = 128
_LOCK = asyncio.Lock()
_VERIFIED_CACHE: dict[str, str] = {}
_ACTIVE_INDEX: dict[str, dict[str, list[Path]]] = {}
log = logging.getLogger("runner_forward")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _time(value: Any) -> dt.datetime | None:
    try:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            return None
        return result.astimezone(dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _positive(value: Any) -> bool:
    result = _number(value)
    return result is not None and result > 0


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = read_json_strict(path)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _write(path: Path, value: dict[str, Any]) -> None:
    write_json_atomic(path, value)


def _directory(root: Path | str | None) -> Path:
    return Path(root or PROJECT_ROOT).resolve() / "data" / "research" / "runner_forward"


def _index(directory: Path) -> dict[str, list[Path]]:
    key = str(directory)
    if key not in _ACTIVE_INDEX:
        tokens: dict[str, list[Path]] = {}
        for path in (directory / "active").glob("*.json"):
            case = _read(path)
            if case and isinstance(case.get("token"), str):
                tokens.setdefault(case["token"], []).append(path)
        if len(_ACTIVE_INDEX) >= 32:
            _ACTIVE_INDEX.pop(next(iter(_ACTIVE_INDEX)))
        _ACTIVE_INDEX[key] = tokens
    return _ACTIVE_INDEX[key]


def _policy_id(policy: dict[str, Any]) -> str:
    return _hash(runner_price_policy.parse_policy(policy))[:20]


def _cohort_name(value: Any) -> bool:
    return re.fullmatch(r"[0-9]{8}_[0-9a-f]{20}", str(value or "")) is not None


def policy_variants(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    baseline = runner_price_policy.parse_policy(policy)
    if baseline is None:
        return {}
    variants = {_policy_id(baseline): baseline}
    for delta in (-5.0, 5.0):
        candidate = {**baseline, "max_price_drawdown_pct": baseline["max_price_drawdown_pct"] + delta}
        if runner_price_policy.parse_policy(candidate) is not None:
            variants[_policy_id(candidate)] = candidate
    return variants


def case_identity(prefix: dict[str, Any]) -> str:
    identity = entry_identity(prefix)
    return _hash(["paper_entry", identity]) if identity else _hash(
        [prefix["run_id"], prefix["token_address"], prefix["opened_at"]])


def prepare_partial_case(entry: dict[str, Any], *,
                     cfg: Any = None, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """Pure preparation: original first-partial state, never a later snapshot."""
    cfg = CFG if cfg is None else cfg
    if getattr(cfg, "PAPER_RUNNER_RESEARCH_ENABLED", False) is not True or entry.get("dry_run") is not True:
        return None
    policy = runner_price_policy.parse_policy(entry.get("runner_trailing_policy"))
    stamp = now or _now()
    opened, run_start = _time(entry.get("opened_at")), _time(entry.get("run_started_at"))
    if (policy is None or entry.get("closed") or entry.get("test_event") or not entry.get("run_id")
            or opened is None or run_start is None or not run_start <= opened <= stamp
            or entry.get("partial_taken") is not True or entry.get("partial_fill_events") != 1):
        return None
    if exit_policy.runner_price_protection_floor_pct(entry, peak=policy["activation_peak_pct"]) is None:
        return None  # Excludes short-lived probes and reversal scalps.
    model = entry.get("execution_cost_model") or {}
    route = entry.get("entry_route_quote") or {}
    if (model.get("version") != "estimated-v1" or model.get("observed_execution") is not False
            or not _positive(entry.get("entry_notional_usd")) or not _positive(entry.get("buy_price_usd"))
            or _number(entry.get("amount_sol")) != .1 or route.get("in_amount") != 100000000
            or not _positive(route.get("out_amount")) or not _positive(route.get("max_impact_pct"))
            or not valid_summary(route, output_mint=entry.get("token_address"), amount=100000000, allow_legacy=True)
            or type(route.get("out_amount")) is not int
            or any(type(entry.get(key)) is not int for key in ("entry_qty", "qty_lamports", "realized_qty"))
            or entry.get("quantity_basis") != "quoted_raw_spl_units"):
        return None
    try:
        entry_qty, remaining, realized = (int(entry[k]) for k in ("entry_qty", "qty_lamports", "realized_qty"))
        numeric_fields = ("realized_proceeds_usd", "realized_proceeds_sol", "estimated_fees_usd", "estimated_fees_sol")
        if (entry_qty <= 0 or remaining <= 0 or realized <= 0 or entry_qty != remaining + realized
                or entry_qty != int(route["out_amount"] / (1 + model["slippage_bps"] / 10000))
                or any(_number(entry.get(k)) is None or float(entry[k]) < 0 for k in numeric_fields)
                or not 0 <= float(model["slippage_bps"]) < 10000
                or not math.isfinite(float(model["fee_sol_per_fill"])) or float(model["fee_sol_per_fill"]) < 0):
            return None
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    token = str(entry.get("token_address") or "").strip()
    from utils.solana_addr import is_valid_base58_32
    if not is_valid_base58_32(token) or type(entry.get("execution_fill_count")) is not int or entry["execution_fill_count"] != 2:
        return None
    case_id = case_identity(entry)
    base_id = _policy_id(policy)
    day = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
    cohort = f"{day.strftime('%Y%m%d')}_{base_id}"
    # Whitelist portfolio fields; no arbitrary payload or credentials are copied.
    keys = {"run_id", "run_started_at", "opened_at", "token_address", "entry_regime", "entry_lane",
            "gate_profile", "runner_exit_profile", "exit_profile", "discovered_via", "buy_price_usd",
            "entry_qty", "qty_lamports", "realized_qty", "realized_proceeds_usd", "realized_proceeds_sol",
            "entry_notional_usd", "amount_sol", "execution_cost_model", "estimated_fees_usd",
            "estimated_fees_sol", "execution_fill_count", "partial_taken", "partial_count", "partial_fill_events",
            "highest_pnl_pct", "max_pnl_pct_seen", "max_adverse_pnl_pct", "partial_ladder_state",
            "first_partial_at", "last_partial_at", "buy_liquidity_usd", "dry_run", "entry_route_quote",
            "quantity_basis", "runner_trailing_policy", "entry_intent_id", "buy_signature",
            "source_position_key", "paper_entry_policy", "first_partial_exit_intent_id"}
    prefix = {key: copy.deepcopy(entry[key]) for key in keys if key in entry}
    variants = policy_variants(policy)
    arms = {}
    for arm_id, parameters in variants.items():
        subject = copy.deepcopy(prefix)
        subject["runner_trailing_policy"] = json.dumps(parameters, sort_keys=True)
        subject.update(closed=False, paper_cash_owner="case:" + _hash([case_id, arm_id]))
        arms[arm_id] = {"parameters": parameters, "subject": subject, "fills": [], "closed": False,
            "cash_last_observed_at": stamp.isoformat(), "cash_observation_count": 0,
            "cash_observation_gap_limit_exceeded": False}
    case = {"version": VERSION, "case_id": case_id, "cohort_id": cohort,
            "cohort_started_at": day.isoformat(), "cohort_ends_at": (day + dt.timedelta(hours=24)).isoformat(),
            "registered_at": stamp.isoformat(), "baseline_id": base_id, "token": token,
            "prefix": prefix, "arms": arms, "last_observed_at": stamp.isoformat(), "quote_failures": 0,
            "observation_count": 0, "observation_gap_limit_exceeded": False,
            "financial_policy_version": cash.VERSION}
    return case


def register_partial(entry: dict[str, Any], *, root: Path | str | None = None,
                     cfg: Any = None, now: dt.datetime | None = None) -> bool:
    """Compatibility entry point; durable production sources are captured with the fill."""
    cfg = CFG if cfg is None else cfg
    stamp = now or _now()
    if prepare_partial_case(entry, cfg=cfg, now=stamp) is None:
        return False
    directory, identity = _directory(root), case_identity(entry)
    if any((directory / state / (identity + ".json")).exists() for state in ("active", "closed", "invalid")):
        return False
    from runtime.runner_enrollment import capture_source, register_source
    return register_source(capture_source(entry, captured_at=stamp), root=root, cfg=cfg, now=stamp)["created"]


def _request(arm: dict[str, Any], price: Any, now: dt.datetime, *, liq_now: float | None = None,
             cash_context=None, cash_valuation=None) -> None:
    if arm["closed"] or arm.get("intent"):
        return
    subject = arm["subject"]
    price = _number(price)
    valid_price = price is not None and price > 0
    pnl = (price / subject["buy_price_usd"] - 1) * 100 if valid_price else None
    if not valid_price and liq_now is not None:
        opened = _time(subject.get("opened_at"))
        if opened is not None and now >= opened:
            # Preserve the independent safeguard before the shared engine's
            # no-price early return; never use a legacy spot peak to activate it.
            peak = subject.get("highest_pnl_pct", 0.) if arm.get("cash_observation_count", 0) > 0 else 0.
            reason = exit_policy.liquidity_crush_reason(subject, exit_policy.effective_exit_policy(subject),
                liq_now=liq_now, age_min=(now - opened).total_seconds() / 60, peak=peak)
            if reason is not None:
                _set_intent(arm, quantity=subject["qty_lamports"], reason=reason, now=now)
                return
    if pnl is not None:
        exit_policy.update_exit_state(subject, pnl_pct=pnl)
    # Retain the real partial ladder; don't compare a hold-all shadow tail with
    # a different incumbent that keeps taking partial profits.
    if pnl is not None and exit_policy.should_take_partial(subject, pnl):
        plan = exit_policy.partial_ladder_plan(subject, pnl)
        fraction = exit_policy.partial_sell_fraction(subject, pnl)
        quantity = min(subject["qty_lamports"], max(1, round(subject["qty_lamports"] * fraction)))
        _set_intent(arm, quantity=quantity, reason="partial_tp", now=now, ladder_plan=plan,
                    cash_valuation=cash_valuation)
        return
    reason = exit_policy.should_exit(subject, price, now, pnl_pct=pnl, liq_now=liq_now, cash_context=cash_context)
    if reason is not None:
        _set_intent(arm, quantity=subject["qty_lamports"], reason=reason, now=now,
                    cash_valuation=cash_valuation)


def _observe_cash(case: dict, arm_id: str, quote: Any, fx: Any, stamp: dt.datetime,
                  *, request_decision: bool = True, slippage_bps: int | None = None,
                  quote_started_at: dt.datetime | None = None) -> bool:
    """Observe one arm's original exact remaining cash, never a market proxy.

    A valuation can create an intent, but cannot execute that newly-created
    intent with the already-received valuation quote. Mutation is atomic.
    """
    try:
        original = case["arms"][arm_id]
        if original.get("closed") or original.get("intent"):
            return False
        arm = copy.deepcopy(original)
        subject, owner = arm["subject"], "case:" + _hash([case["case_id"], arm_id])
        if subject.get("paper_cash_owner") != owner:
            return False
        from fetcher.jupiter_router import routing_quote_slippage_bps
        current = cash.capture(subject, quote, fx, token=case["token"], owner=owner,
            now=stamp, slippage_bps=routing_quote_slippage_bps() if slippage_bps is None else slippage_bps).to_dict()
        received = _time(current["route_quote"]["observation_receipt"]["other"].get("received_at_utc"))
        started = _time(quote_started_at)
        if started is None or received is None or not started <= received <= stamp:
            return False
        previous_at = _time(arm.get("cash_last_observed_at"))
        if previous_at is None or stamp < previous_at:
            return False
        previous_mark = arm.get("cash_last_mark")
        if previous_mark is not None and stamp == previous_at:
            return previous_mark == current  # Conflicting same-instant observations cannot replace a peak.
        remaining_peak = arm.get("cash_peak_mark")
        total_peak = arm.get("cash_total_peak_mark")
        for peak in (remaining_peak, total_peak):
            if peak is not None:
                cash.public_historical_mark(peak, subject, token=case["token"], owner=owner)
                if _time(peak["valued_at"]) > stamp:
                    return False
        if remaining_peak is None or current["values"]["gross_remaining_return_pct"] > remaining_peak["values"]["gross_remaining_return_pct"]:
            remaining_peak = copy.deepcopy(current)
        if total_peak is None or current["values"]["estimated_total_liquidation_net_pnl_usd"] > total_peak["values"]["estimated_total_liquidation_net_pnl_usd"]:
            total_peak = copy.deepcopy(current)
        # Original spot peaks remain diagnostic. They do not acquire cash provenance.
        if not arm.get("cash_observation_count"):
            arm["legacy_market_peak_diagnostic"] = {"role": "unverified_market_peak_only",
                "values": {key: value for key in ("highest_pnl_pct", "max_pnl_pct_seen", "max_adverse_pnl_pct")
                    if type(value := subject.get(key)) in (int, float) and math.isfinite(value)}}
            for key in ("highest_pnl_pct", "max_pnl_pct_seen", "peak_pnl_pct", "max_adverse_pnl_pct"):
                subject[key] = 0.
        subject["highest_pnl_pct"] = max(0., remaining_peak["values"]["gross_remaining_return_pct"])
        subject["max_pnl_pct_seen"] = subject["highest_pnl_pct"]
        arm.update(cash_last_mark=current, cash_peak_mark=remaining_peak, cash_total_peak_mark=total_peak,
            cash_last_observed_at=stamp.isoformat(), cash_observation_count=arm.get("cash_observation_count", 0) + 1,
            cash_observation_gap_limit_exceeded=arm.get("cash_observation_gap_limit_exceeded") is not False
                or (stamp - previous_at).total_seconds() > 300)
        price = cash.checked_price(current, subject, token=case["token"], owner=owner, now=stamp)
        context = cash.protection_context(subject, current, total_peak, token=case["token"], owner=owner, now=stamp)
        if price is None or context.receipt_json is None:
            return False
        if request_decision:
            _request(arm, price, stamp, cash_context=context,
                cash_valuation={"current": current, "remaining_peak": remaining_peak, "total_peak": total_peak,
                                "quote_started_at": started.isoformat()})
        original.clear()
        original.update(arm)
        return True
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return False


def _cash_quoteable(case: dict, arm_id: str, arm: dict) -> bool:
    try:
        if arm.get("closed") or arm.get("intent"):
            return False
        owner = "case:" + _hash([case["case_id"], arm_id])
        cash.basis(arm["subject"], token=case["token"], owner=owner)
        return True
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return False


def _same_cash_generation(first: dict, second: dict) -> bool:
    try:
        return financial_basis(first["subject"]) == financial_basis(second["subject"])
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return False


def _set_intent(arm: dict, **arguments) -> bool:
    try:
        arm["intent"] = make_intent(arm["subject"], **arguments)
        arm.pop("intent_error", None)
        return True
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        arm["intent_error"] = "unknown_original_financial_generation"
        return False


def _apply_quote(case: dict[str, Any], arm: dict[str, Any], quote: Any, sol_usd: float,
                 now: dt.datetime, *, quote_started_at: dt.datetime | None = None, fx_observation=None) -> bool:
    """Apply only a checked original intent; invalid evidence never mutates cash."""
    try:
        updated = copy.deepcopy(arm)
        if not _apply_quote_checked(case, updated, quote, sol_usd, now, quote_started_at=quote_started_at,
                                    fx_observation=fx_observation):
            return False
        arm.clear()
        arm.update(updated)
        return True
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return False


def _apply_quote_checked(case: dict[str, Any], arm: dict[str, Any], quote: Any, sol_usd: float,
                         now: dt.datetime, *, quote_started_at: dt.datetime | None = None, fx_observation=None) -> bool:
    intent = arm.get("intent")
    if not isinstance(intent, dict) or not isinstance(arm.get("fills"), list):
        return False
    quantity = intent["quantity"]
    subject = arm["subject"]
    requested = _time(intent.get("requested_at"))
    if (arm.get("closed") or requested is None or now < requested or not valid_intent(intent, subject)
            or not isinstance(quantity, int) or isinstance(quantity, bool)
            or not 0 < quantity <= subject["qty_lamports"]):
        return False
    from fetcher import jupiter_router
    try:
        receipt = capture_summary(quote, input_mint=case["token"], output_mint=jupiter_router.SOL_MINT,
            amount=quantity, slippage=jupiter_router.routing_quote_slippage_bps(),
            limit=subject["entry_route_quote"]["max_impact_pct"], now=now)
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
    if not causal_quote(intent, receipt, quote_started_at=quote_started_at, filled_at=now):
        return False
    if not _positive(sol_usd):
        return False
    if fx_observation is not None:
        from utils.sol_price import fresh_sol_usd
        if fresh_sol_usd(fx_observation, now=now.timestamp()) != sol_usd:
            return False
    impact, routes = receipt["impact_bps"], receipt["route_count"]
    model = subject["execution_cost_model"]
    proceeds_sol = quote.out_amount / 1e9 * (1 - model["slippage_bps"] / 10000)
    proceeds_usd = proceeds_sol * sol_usd
    if not _positive(proceeds_sol) or not _positive(proceeds_usd):
        return False
    # Exact cash conservation, including the shared entry/first-partial fees.
    subject["qty_lamports"] -= quantity
    subject["realized_qty"] += quantity
    subject["realized_proceeds_sol"] += proceeds_sol
    subject["realized_proceeds_usd"] += proceeds_usd
    subject["estimated_fees_sol"] += model["fee_sol_per_fill"]
    subject["estimated_fees_usd"] += model["fee_sol_per_fill"] * sol_usd
    subject["execution_fill_count"] += 1
    if not all(_number(subject[key]) is not None for key in (
            "realized_proceeds_sol", "realized_proceeds_usd", "estimated_fees_sol", "estimated_fees_usd")):
        return False
    arm["fills"].append({"input_raw_spl": quantity, "output_lamports": quote.out_amount,
                         "route_count": routes,
                         "route_quote": receipt,
                         "impact_bps": impact, "sol_usd": sol_usd, "filled_at": now.isoformat(),
                         "quote_started_at": quote_started_at.isoformat(), "exit_intent": copy.deepcopy(intent),
                         "intent_at": intent["requested_at"], "reason": intent["reason"],
                         "proceeds_sol": proceeds_sol, "proceeds_usd": proceeds_usd,
                         "fee_sol": model["fee_sol_per_fill"], "observed_execution": False})
    if fx_observation is not None:
        arm["fills"][-1]["fx_observation"] = fx_observation.to_dict()
    if subject["qty_lamports"] == 0:
        arm["closed"] = True
        arm["closed_at"] = now.isoformat()
        if "closed" in subject:
            subject.update(closed=True, closed_at=now.isoformat())
        arm["net_pnl_sol"] = subject["realized_proceeds_sol"] - .1 - subject["estimated_fees_sol"]
        arm["net_pnl_usd"] = subject["realized_proceeds_usd"] - subject["entry_notional_usd"] - subject["estimated_fees_usd"]
    else:
        subject["partial_count"] += max(1, int(intent.get("ladder_plan", {}).get("pending_step_count") or 1))
        next_state = intent.get("ladder_plan", {}).get("next_state")
        if isinstance(next_state, dict):
            subject["partial_ladder_state"] = runner_ladder.encode_ladder_state(next_state)
    arm.pop("intent", None)
    return True


def paper_exit_request(arm: dict[str, Any], price: Any, now: dt.datetime, *, liq_now=None) -> None:
    """Shared exit engine; caller owns immutable prefix and observation coverage."""
    _request(arm, price, now, liq_now=liq_now)


def apply_paper_exit_quote(case: dict[str, Any], arm: dict[str, Any], quote: Any,
                          sol_usd: float, now: dt.datetime, *, quote_started_at: dt.datetime | None = None,
                          fx_observation=None) -> bool:
    """Shared exact-quantity quote/cash engine; never signs or sends a swap."""
    return _apply_quote(case, arm, quote, sol_usd, now, quote_started_at=quote_started_at,
                        fx_observation=fx_observation)


def active_tokens(root: Path | str | None = None) -> set[str]:
    return set(_index(_directory(root)))


def has_quote_demand(root: Path | str | None = None) -> bool:
    for path in (_directory(root) / "active").glob("*.json"):
        case = _read(path)
        if case and any(arm.get("intent") or _cash_quoteable(case, arm_id, arm)
                        for arm_id, arm in (case.get("arms") or {}).items()):
            return True
    return False


def _observe_case(case: dict[str, Any], price: Any, stamp: dt.datetime,
                  *, liq_now: float | None = None) -> None:
    previous = _time(case["last_observed_at"])
    if previous is not None and stamp < previous:
        return
    if previous is not None and (stamp - previous).total_seconds() > 300:
        case["observation_gap_limit_exceeded"] = True
    if previous is None or stamp > previous:
        case["last_observed_at"] = stamp.isoformat()
        case["observation_count"] += 1
    for arm in case["arms"].values():
        # Market data can support independent liquidity/time safeguards, not
        # a financial partial, cash peak, runner drawdown or net-profit floor.
        _request(arm, None, stamp, liq_now=liq_now, cash_context=cash.PaperCashProtection())


def observe_market(token: str, price: Any, *, root: Path | str | None = None,
                   cfg: Any = None, now: dt.datetime | None = None, liq_now: Any = None) -> int:
    """Reuse authoritative monitor prices/liquidity without provider requests."""
    cfg = CFG if cfg is None else cfg
    if getattr(cfg, "DRY_RUN", False) is not True or getattr(cfg, "PAPER_RUNNER_RESEARCH_ENABLED", False) is not True:
        return 0
    if not _positive(price):
        return 0
    count, directory, stamp = 0, _directory(root), now or _now()
    for path in list(_index(directory).get(token, [])):
        case = _read(path)
        if case:
            liquidity = _number(liq_now)
            _observe_case(case, price, stamp, liq_now=liquidity if liquidity is not None and liquidity >= 0 else None)
            _write(path, case)
            count += 1
    return count


def observe_quote(token: str, quote: Any, sol_usd: float, *, root: Path | str | None = None,
                  cfg: Any = None, now: dt.datetime | None = None,
                  quote_started_at: dt.datetime | None = None) -> int:
    """Reuse only already-pending, exact-quantity exit probes; never infer intent.

    A quote of a smaller amount cannot prove liquidity for a larger sale. This
    hook doesn't create a policy decision, sign, send, or modify the real trade.
    """
    cfg = CFG if cfg is None else cfg
    if getattr(cfg, "DRY_RUN", False) is not True or getattr(cfg, "PAPER_RUNNER_RESEARCH_ENABLED", False) is not True:
        return 0
    count, directory, stamp = 0, _directory(root), now or _now()
    for path in list(_index(directory).get(token, [])):
        case = _read(path)
        if case:
            changed = False
            for arm in case["arms"].values():
                if arm.get("intent", {}).get("quantity") == getattr(quote, "in_amount", None):
                    if _apply_quote(case, arm, quote, sol_usd, stamp, quote_started_at=quote_started_at):
                        changed = True
                        count += 1
            if changed:
                _write(path, case)
    return count


async def tick(*, root: Path | str | None = None, cfg: Any = None, now: dt.datetime | None = None,
               prices_func=None, quote_func=None, sol_price_func=None, fx_func=None) -> dict[str, Any]:
    """Best-effort secondary workload, after the authoritative position monitor."""
    cfg = CFG if cfg is None else cfg
    if getattr(cfg, "DRY_RUN", False) is not True or getattr(cfg, "PAPER_RUNNER_RESEARCH_ENABLED", False) is not True:
        return {"status": "disabled", "quote_calls": 0}
    async with _LOCK:
        stamp = now or _now()
        directory = _directory(root)
        clock = _read(directory / "clock.json") or {}
        previous = _time(clock.get("last_tick_at"))
        interval = _number(getattr(cfg, "PAPER_RUNNER_RESEARCH_INTERVAL_S", 60))
        interval = max(60.0, interval if interval is not None else 60.0)
        if previous is not None and (stamp - previous).total_seconds() < interval:
            return {"status": "throttled", "quote_calls": 0}
        from analytics.api_budget import provider_status
        if provider_status("jupiter").get("degraded"):
            return {"status": "provider_degraded", "quote_calls": 0}
        _write(directory / "clock.json", {"last_tick_at": stamp.isoformat()})
        cases = []
        for path in sorted((directory / "active").glob("*.json")):
            case = _read(path)
            if case is None or case.get("version") != VERSION:
                continue  # Malformed files remain inspectable; never silently delete.
            cases.append((path, case))
        cases = cases[:MAX_ACTIVE]
        if not cases:
            return {"status": "idle", "quote_calls": 0,
                    "selection": evaluate_completed_cohorts(root=root, cfg=cfg, now=stamp)}
        from fetcher import jupiter_router
        from utils.sol_price import get_sol_usd, get_sol_usd_observation
        quote_func = quote_func or jupiter_router.get_routing_quote
        sol_price_func = sol_price_func or get_sol_usd
        fx_func = fx_func or get_sol_usd_observation
        try:
            # No extra spot request is needed to value a research arm. Retain
            # injected readers for diagnostics and backwards-compatible callers.
            prices = await prices_func(sorted({case["token"] for _, case in cases})) if prices_func else {}
            if not isinstance(prices, dict):
                prices = {}
        except Exception:
            prices = {}  # Keep timeout intents quoteable; never fabricate a price.
        # Market/real-quote hooks can run while the network call is pending.
        # Reload before mutation and persist intents before awaiting a quote.
        cases = [(path, latest) for path, _ in cases if (latest := _read(path)) is not None]
        sampled_at = now or _now()
        for path, case in cases:
            _observe_case(case, prices.get(case["token"]), sampled_at)
            _write(path, case)
        # One raw-quantity quote per minute; share it across exactly identical
        # requests. Persisted FIFO intents account for actual research latency.
        # Pending fills have priority; otherwise service the oldest exact-size
        # valuation. Identical quantities can share cash, never arm ownership.
        requests = []
        for _, case in cases:
            for arm_id, arm in case["arms"].items():
                if arm.get("intent"):
                    requests.append((0, arm["intent"]["requested_at"], case["case_id"], arm_id, case, arm,
                                     arm["intent"]["quantity"]))
                elif _cash_quoteable(case, arm_id, arm):
                    requests.append((1, arm.get("cash_last_observed_at") or case["registered_at"],
                                     case["case_id"], arm_id, case, arm, arm["subject"]["qty_lamports"]))
        requests.sort(key=lambda item: item[:4])
        quote_calls = 0
        from research_loop import entry_gate_forward, forward_budget
        if requests and forward_budget.claim(Path(root or PROJECT_ROOT), "runner_exit",
                other_pending=entry_gate_forward.has_quote_demand(root), now=sampled_at):
            role, _, _, _, selected_case, selected_arm, quantity = requests[0]
            token = selected_case["token"]
            try:
                sol_usd = await sol_price_func() if role == 0 else None
            except Exception:
                sol_usd = None
            if role == 1 or _positive(sol_usd):
                quote_calls = 1
                quote, fx = None, None
                requested_slippage = jupiter_router.routing_quote_slippage_bps()
                try:
                    quote_started_at = now or _now()
                    quote = await quote_func(input_mint=token, output_mint=jupiter_router.SOL_MINT,
                                             amount_lamports=quantity)
                    if role == 0:
                        sol_usd = await sol_price_func()  # recheck FX after network wait
                    else:
                        fx = await fx_func()  # original typed FX, never a relabelled scalar
                except Exception:
                    quote = None
                if (getattr(quote, "other", None) or {}).get("status") == 429:
                    from analytics.api_budget import record_provider_event
                    record_provider_event("jupiter", "429")
                latest_cases = {case["case_id"]: (path, _read(path)) for path, case in cases}
                filled_at = now or _now()
                changed = set()
                for request_role, _, case_id, arm_id, case, arm, requested_quantity in requests:
                    if request_role == role and case["token"] == token and requested_quantity == quantity:
                        _, latest = latest_cases[case_id]
                        current_arm = latest["arms"].get(arm_id) if latest else None
                        if not current_arm or current_arm.get("intent") != arm.get("intent"):
                            continue  # Already filled/replaced by the real-quote hook.
                        success = False
                        if role == 0:
                            success = _positive(sol_usd) and _apply_quote(latest, current_arm, quote, float(sol_usd), filled_at,
                                quote_started_at=quote_started_at)
                        elif _same_cash_generation(current_arm, arm):
                            success = _observe_cash(latest, arm_id, quote, fx, filled_at,
                                slippage_bps=requested_slippage, quote_started_at=quote_started_at)
                        if not success:
                            latest["quote_failures"] += 1
                        changed.add(case_id)
                for case_id in changed:
                    path, latest = latest_cases[case_id]
                    _write(path, latest)
        cases = [(path, latest) for path, _ in cases if (latest := _read(path)) is not None]
        stamp = now or _now()
        for path, case in cases:
            ended = _time(case["cohort_ends_at"])
            state = "active"
            if ended is not None and stamp > ended + dt.timedelta(hours=MAX_SETTLEMENT_HOURS):
                state = "invalid"
                case["invalid_reason"] = "unresolved_quote_or_price_at_settlement_deadline"
            elif all(arm["closed"] for arm in case["arms"].values()):
                state = "closed"
            _write(path, case)
            if state != "active":
                destination = directory / state / path.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, destination)  # Preserve the complete evidence, including unresolved arms.
                _ACTIVE_INDEX.pop(str(directory), None)
        report = evaluate_completed_cohorts(root=root, cfg=cfg, now=stamp)
        return {"status": "observed", "cases": len(cases), "quote_calls": quote_calls, "selection": report}


def _valid_terminal(case: dict[str, Any], arm: dict[str, Any], now: dt.datetime) -> bool:
    try:
        return _valid_terminal_unchecked(case, arm, now)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return False


def validate_paper_cash_terminal(case: dict[str, Any], arm: dict[str, Any], now: dt.datetime) -> bool:
    """Check raw quantity/cash conservation, not strategy or entry eligibility.

    Other paired paper components must separately validate their entry prefix,
    prospective plan, decisions, observation coverage and cohort population.
    """
    return _valid_terminal(case, arm, now)


def _valid_terminal_unchecked(case: dict[str, Any], arm: dict[str, Any], now: dt.datetime) -> bool:
    closed = _time(arm.get("closed_at"))
    registered = _time(case.get("registered_at"))
    subject = arm.get("subject") or {}
    if (arm.get("closed") is not True or closed is None or registered is None
            or not registered <= closed <= now or not arm.get("fills") or subject.get("qty_lamports") != 0
            or _number(arm.get("net_pnl_sol")) is None or _number(arm.get("net_pnl_usd")) is None):
        return False
    deadline = _time(case.get("cohort_ends_at"))
    if deadline is None or closed > deadline + dt.timedelta(hours=MAX_SETTLEMENT_HOURS):
        return False
    prefix = case.get("prefix") or {}
    if (prefix.get("quantity_basis") != "quoted_raw_spl_units" or not _positive(prefix.get("entry_route_quote", {}).get("out_amount"))
            or not _positive(prefix.get("entry_route_quote", {}).get("max_impact_pct"))
            or not valid_summary(prefix.get("entry_route_quote"), output_mint=case.get("token"),
                amount=100000000, not_after=_time(prefix.get("opened_at")), allow_legacy=True)
            or not _positive(prefix.get("entry_notional_usd")) or prefix.get("test_event")):
        return False
    fees_sol, fees_usd = _number(prefix.get("estimated_fees_sol")), _number(prefix.get("estimated_fees_usd"))
    proceeds_sol, proceeds_usd = _number(prefix.get("realized_proceeds_sol")), _number(prefix.get("realized_proceeds_usd"))
    remaining = prefix.get("qty_lamports")
    if any(value is None for value in (fees_sol, fees_usd, proceeds_sol, proceeds_usd)) or not isinstance(remaining, int):
        return False
    previous = registered
    generation = copy.deepcopy(prefix)
    if "paper_cash_owner" in subject:
        if not case.get("case_id") or not arm.get("parameters"):
            return False
        owner = "case:" + _hash([case["case_id"], _policy_id(arm["parameters"])])
        if subject["paper_cash_owner"] != owner:
            return False
        generation.update(paper_cash_owner=owner, closed=False)
    if "runner_trailing_policy" in subject:
        generation["runner_trailing_policy"] = subject["runner_trailing_policy"]
    else:
        generation.pop("runner_trailing_policy", None)
    for fill in arm["fills"]:
        filled, intent = _time(fill.get("filled_at")), _time(fill.get("intent_at"))
        if (filled is None or intent is None or not previous <= intent <= filled <= closed
                or fill.get("observed_execution") is not False or not _positive(fill.get("input_raw_spl"))
                or not _positive(fill.get("output_lamports")) or not _positive(fill.get("sol_usd"))
                or _number(fill.get("impact_bps")) is None
                or not isinstance(fill["input_raw_spl"], int) or not isinstance(fill["output_lamports"], int)):
            return False
        if "fx_observation" in fill:
            from utils.sol_price import SolUsdObservation, fresh_sol_usd
            if fresh_sol_usd(SolUsdObservation(**fill["fx_observation"]), now=filled.timestamp()) != fill["sol_usd"]:
                return False
        receipt = fill.get("route_quote")
        original_intent = fill.get("exit_intent")
        if (not valid_intent(original_intent, generation)
                or original_intent.get("quantity") != fill["input_raw_spl"]
                or original_intent.get("reason") != fill.get("reason")
                or original_intent.get("requested_at") != fill.get("intent_at")
                or not causal_quote(original_intent, receipt,
                    quote_started_at=fill.get("quote_started_at"), filled_at=filled)):
            return False
        if receipt is not None:
            from fetcher.jupiter_router import SOL_MINT
            if (not valid_summary(receipt, input_mint=case["token"], output_mint=SOL_MINT,
                    amount=fill["input_raw_spl"], not_after=filled)
                    or receipt["out_amount"] != fill["output_lamports"]
                    or receipt["route_count"] != fill.get("route_count") or receipt["impact_bps"] != fill["impact_bps"]
                    or receipt["max_impact_pct"] != prefix["entry_route_quote"]["max_impact_pct"]):
                return False
        elif (not isinstance(fill.get("route_count"), int) or isinstance(fill["route_count"], bool)
                or fill["route_count"] <= 0 or abs(fill["impact_bps"]) / 100 > prefix["entry_route_quote"]["max_impact_pct"]):
            return False
        model = prefix["execution_cost_model"]
        if (model.get("version") != "estimated-v1" or model.get("observed_execution") is not False
                or _number(model.get("slippage_bps")) is None or not 0 <= model["slippage_bps"] < 10000
                or _number(model.get("fee_sol_per_fill")) is None or model["fee_sol_per_fill"] < 0):
            return False
        expected_sol = fill["output_lamports"] / 1e9 * (1 - model["slippage_bps"] / 10000)
        expected_usd = expected_sol * fill["sol_usd"]
        if (not math.isclose(fill.get("proceeds_sol", -1), expected_sol, abs_tol=1e-12)
                or not math.isclose(fill.get("proceeds_usd", -1), expected_usd, abs_tol=1e-9)
                or fill.get("fee_sol") != model["fee_sol_per_fill"]):
            return False
        remaining -= fill["input_raw_spl"]
        proceeds_sol += expected_sol
        proceeds_usd += expected_usd
        fees_sol += model["fee_sol_per_fill"]
        fees_usd += model["fee_sol_per_fill"] * fill["sol_usd"]
        generation.update(qty_lamports=remaining,
            realized_qty=generation["realized_qty"] + fill["input_raw_spl"],
            realized_proceeds_sol=proceeds_sol, realized_proceeds_usd=proceeds_usd,
            estimated_fees_sol=fees_sol, estimated_fees_usd=fees_usd,
            execution_fill_count=generation["execution_fill_count"] + 1)
        previous = filled
    return (all(_number(value) is not None for value in (fees_sol, fees_usd, proceeds_sol, proceeds_usd))
            and remaining == 0
            and subject.get("realized_qty") == prefix["entry_qty"]
            and subject.get("execution_fill_count") == prefix["execution_fill_count"] + len(arm["fills"])
            and math.isclose(subject["realized_proceeds_sol"], proceeds_sol, abs_tol=1e-10)
            and math.isclose(subject["realized_proceeds_usd"], proceeds_usd, abs_tol=1e-8)
            and math.isclose(subject["estimated_fees_sol"], fees_sol, abs_tol=1e-10)
            and math.isclose(subject["estimated_fees_usd"], fees_usd, abs_tol=1e-8)
            and math.isclose(arm["net_pnl_sol"], proceeds_sol - .1 - fees_sol, abs_tol=1e-10)
            and math.isclose(arm["net_pnl_usd"], proceeds_usd - prefix["entry_notional_usd"] - fees_usd, abs_tol=1e-8))


def compare_cohort(cases: list[dict[str, Any]], *, now: dt.datetime | None = None) -> dict[str, Any]:
    try:
        return _compare_cohort(cases, now=now)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, ZeroDivisionError):
        return {"version": VERSION, "accepted": False, "reasons": ["malformed_or_nonfinite_cohort"]}


def _valid_cash_coverage(case: dict, arm_id: str, arm: dict) -> bool:
    """Original per-arm estimated cash coverage, not a shared market heartbeat."""
    try:
        observed, closed = _time(arm.get("cash_last_observed_at")), _time(arm.get("closed_at"))
        registered = _time(case.get("registered_at"))
        if (arm.get("cash_observation_gap_limit_exceeded") is not False
                or type(arm.get("cash_observation_count")) is not int or arm["cash_observation_count"] <= 0
                or observed is None or closed is None or registered is None
                or not registered <= observed <= closed or (closed - observed).total_seconds() > 300):
            return False
        owner = "case:" + _hash([case["case_id"], arm_id])
        subject = arm["subject"]
        if subject.get("paper_cash_owner") != owner:
            return False
        last = cash.public_historical_mark(arm["cash_last_mark"], subject, token=case["token"], owner=owner)
        remaining = cash.public_historical_mark(arm["cash_peak_mark"], subject, token=case["token"], owner=owner)
        total = cash.public_historical_mark(arm["cash_total_peak_mark"], subject, token=case["token"], owner=owner)
        return (_time(last["valued_at"]) == observed
            and all(registered <= _time(peak["valued_at"]) <= observed for peak in (remaining, total))
            and remaining["values"]["gross_remaining_return_pct"] + 1e-10 >= last["values"]["gross_remaining_return_pct"]
            and total["values"]["estimated_total_liquidation_net_pnl_usd"] + 1e-10
                >= last["values"]["estimated_total_liquidation_net_pnl_usd"])
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return False


def _compare_cohort(cases: list[dict[str, Any]], *, now: dt.datetime | None = None) -> dict[str, Any]:
    stamp = now or _now()
    reasons = []
    if not cases:
        return {"accepted": False, "reasons": ["empty_cohort"]}
    first = cases[0]
    if not _cohort_name(first.get("cohort_id")):
        reasons.append("invalid_cohort_identity")
    ended = _time(first.get("cohort_ends_at"))
    started = _time(first.get("cohort_started_at"))
    if started is None or ended is None or (ended - started).total_seconds() != ENROLLMENT_HOURS * 3600:
        reasons.append("invalid_enrollment_window")
    if ended is None or stamp < ended:
        reasons.append("enrollment_window_not_finished")
    if len({case.get("cohort_id") for case in cases}) != 1 or len({case.get("baseline_id") for case in cases}) != 1:
        reasons.append("cohort_or_incumbent_mismatch")
    if len({case.get("case_id") for case in cases}) != len(cases):
        reasons.append("duplicate_case")
    arm_ids = set(first.get("arms") or {})
    baseline = first.get("baseline_id")
    if baseline not in arm_ids or len(arm_ids) < 2:
        reasons.append("paired_arms_missing")
    closes = []
    registered_times = []
    for case in cases:
        registered = _time(case.get("registered_at"))
        prefix = case.get("prefix") or {}
        if (registered is None or not case.get("token") or not prefix.get("run_id")
                or prefix.get("dry_run") is not True or prefix.get("amount_sol") != .1
                or prefix.get("entry_route_quote", {}).get("in_amount") != 100000000
                or _time(prefix.get("run_started_at")) is None or _time(prefix.get("opened_at")) is None
                or case.get("version") != VERSION or set(case.get("arms") or {}) != arm_ids or case.get("invalid_reason")):
            reasons.append("invalid_or_incomparable_case")
            continue
        if not _time(prefix["run_started_at"]) <= _time(prefix["opened_at"]) <= registered <= stamp:
            reasons.append("backwards_or_future_case_clock")
        if (started is None or ended is None or not started <= registered < ended
                or case.get("cohort_started_at") != first.get("cohort_started_at")
                or case.get("cohort_ends_at") != first.get("cohort_ends_at")
                or case["case_id"] != case_identity(prefix)):
            reasons.append("case_identity_or_enrollment_window_mismatch")
        if case.get("observation_gap_limit_exceeded") is not False or case.get("observation_count", 0) <= 0:
            reasons.append("incomplete_observation_coverage")
        if case.get("financial_policy_version") != cash.VERSION:
            reasons.append("unknown_financial_policy_basis")
        registered_times.append(registered)
        base_parameters = runner_price_policy.parse_policy(case["arms"].get(baseline, {}).get("parameters"))
        expected_variants = policy_variants(base_parameters or {})
        if set(expected_variants) != arm_ids:
            reasons.append("invalid_policy_variants")
        for arm_id, arm in case["arms"].items():
            if not _valid_cash_coverage(case, arm_id, arm):
                reasons.append("incomplete_arm_cash_coverage")
            for fill in arm.get("fills") or []:
                if (not str(fill.get("reason", "")).startswith("TIMEOUT") and fill.get("reason") != "LIQUIDITY_CRUSH"
                        and not fill.get("exit_intent", {}).get("cash_valuation")):
                    reasons.append("unknown_financial_decision_mark")
            if expected_variants.get(arm_id) != arm.get("parameters") or not _valid_terminal(case, arm, stamp):
                reasons.append("unresolved_or_uncosted_arm")
            else:
                closes.append(_time(arm["closed_at"]))
        same_instant_quotes = {}
        for arm in case["arms"].values():
            for fill in arm.get("fills") or []:
                key = (fill.get("filled_at"), fill.get("input_raw_spl"))
                value = (fill.get("output_lamports"), fill.get("sol_usd"), fill.get("impact_bps"))
                if key in same_instant_quotes and same_instant_quotes[key] != value:
                    reasons.append("conflicting_same_instant_quotes")
                same_instant_quotes[key] = value
    tokens = {case.get("token") for case in cases if case.get("token")}
    if len(tokens) < MIN_TOKENS:
        reasons.append("insufficient_independent_tokens")
    if closes and (stamp - max(closes)).total_seconds() > MAX_EVIDENCE_AGE_HOURS * 3600:
        reasons.append("stale_evidence")
    if closes and registered_times and (max(closes) - min(registered_times)).total_seconds() < 24 * 3600:
        reasons.append("insufficient_observed_window")
    result = {"version": VERSION, "cohort_id": first.get("cohort_id"), "baseline_id": baseline,
              "accepted": False, "reasons": sorted(set(reasons)), "case_count": len(cases),
              "unique_tokens": len(tokens), "generated_at": stamp.isoformat(),
              "comparison": "same_opportunity_paired_prospective_quote_costed_runner_exits",
              "observed_execution": False, "candidates": []}
    if reasons:
        return result
    # Cluster repeats by case-sensitive mint. Bonferroni-adjusted bootstrap
    # lower bounds account for testing both neighbouring challenger policies.
    challengers = sorted(arm_ids - {baseline})
    for candidate in challengers:
        by_token: dict[str, list[float]] = {}
        by_token_usd: dict[str, list[float]] = {}
        candidate_net_sol = candidate_net_usd = baseline_net_sol = baseline_net_usd = 0.0
        for case in cases:
            chosen, incumbent = case["arms"][candidate], case["arms"][baseline]
            by_token.setdefault(case["token"], []).append(chosen["net_pnl_sol"] - incumbent["net_pnl_sol"])
            by_token_usd.setdefault(case["token"], []).append(chosen["net_pnl_usd"] - incumbent["net_pnl_usd"])
            candidate_net_sol += chosen["net_pnl_sol"]
            candidate_net_usd += chosen["net_pnl_usd"]
            baseline_net_sol += incumbent["net_pnl_sol"]
            baseline_net_usd += incumbent["net_pnl_usd"]
        deltas = [statistics.mean(by_token[token]) for token in sorted(by_token)]
        usd_deltas = [statistics.mean(by_token_usd[token]) for token in sorted(by_token_usd)]
        rng = random.Random(731)
        samples = sorted(statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(1000))
        usd_samples = sorted(statistics.mean(rng.choices(usd_deltas, k=len(usd_deltas))) for _ in range(1000))
        index = int(1000 * .05 / len(challengers))
        bound, usd_bound = samples[index], usd_samples[index]
        if any(_number(value) is None for value in (candidate_net_sol, candidate_net_usd, baseline_net_sol,
                                                   baseline_net_usd, bound, usd_bound)):
            result["reasons"] = ["nonfinite_paired_accounting"]
            result["candidates"] = []
            return result
        eligible = candidate_net_sol > 0 and candidate_net_usd > 0 and bound > 0 and usd_bound > 0
        result["candidates"].append({"policy_id": candidate, "parameters": first["arms"][candidate]["parameters"],
                                     "net_pnl_sol": candidate_net_sol, "baseline_net_pnl_sol": baseline_net_sol,
                                     "net_pnl_usd": candidate_net_usd, "baseline_net_pnl_usd": baseline_net_usd,
                                     "paired_token_mean_delta_sol": statistics.mean(deltas),
                                     "paired_bootstrap_lower_delta_sol": bound,
                                     "paired_bootstrap_lower_delta_usd": usd_bound, "eligible": eligible})
    accepted = [candidate for candidate in result["candidates"] if candidate["eligible"]]
    if accepted:
        result["accepted"] = True
        result["selected"] = max(accepted, key=lambda candidate: candidate["paired_bootstrap_lower_delta_sol"])
    else:
        result["reasons"] = ["no_positive_costed_challenger_with_positive_paired_lower_bound"]
    result["case_evidence"] = [{"case_id": case["case_id"], "sha256": _hash(case)}
                               for case in sorted(cases, key=lambda case: case["case_id"])]
    return result


def entry_policy(cfg: Any, *, root: Path | str | None = None, now: dt.datetime | None = None) -> str:
    try:
        return _entry_policy(cfg, root=root, now=now)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, OSError):
        return runner_price_policy.freeze_policy(cfg, dry_run=getattr(cfg, "DRY_RUN", False) is True)


def _entry_policy(cfg: Any, *, root: Path | str | None = None, now: dt.datetime | None = None) -> str:
    base = runner_price_policy.freeze_policy(cfg, dry_run=getattr(cfg, "DRY_RUN", False) is True)
    if (getattr(cfg, "DRY_RUN", False) is not True
            or getattr(cfg, "PAPER_RUNNER_RESEARCH_ENABLED", False) is not True
            or getattr(cfg, "PAPER_RUNNER_RESEARCH_AUTO_APPLY", False) is not True):
        return base
    directory = _directory(root)
    manifest = _read(directory / "active_policy.json") or {}
    stamp = now or _now()
    expires, selected_at = _time(manifest.get("expires_at")), _time(manifest.get("selected_at"))
    policy = runner_price_policy.parse_policy(manifest.get("parameters"))
    base_parameters = runner_price_policy.parse_policy(base)
    evidence_name = str(manifest.get("evidence_name") or "")
    if (policy is None or base_parameters is None or manifest.get("version") != VERSION
            or manifest.get("role") != "paper_runner_exit_only" or manifest.get("configured_policy_id") != _policy_id(base_parameters)
            or expires is None or selected_at is None or not selected_at <= stamp < expires
            or Path(evidence_name).name != evidence_name or not evidence_name.endswith(".json")):
        return base
    evidence = _read(directory / "evaluations" / evidence_name)
    if (not evidence or _hash(evidence) != manifest.get("evidence_sha256") or evidence.get("accepted") is not True
            or evidence.get("version") != VERSION or not _cohort_name(evidence.get("cohort_id"))
            or evidence.get("selected", {}).get("parameters") != policy or evidence.get("observed_execution") is not False
            or evidence.get("unique_tokens", 0) < MIN_TOKENS):
        return base
    # A scalar report is not sufficient evidence. Recheck its conserved cash
    # records and exact cohort; stale, edited or missing records disable it.
    records = evidence.get("case_evidence")
    if not isinstance(records, list) or not MIN_TOKENS <= len(records) <= 4096:
        return base
    paths = []
    for record in records:
        if not isinstance(record, dict) or re.fullmatch(r"[0-9a-f]{64}", str(record.get("case_id") or "")) is None:
            return base
        path = directory / "closed" / f"{record['case_id']}.json"
        metadata = path.stat()
        paths.append((path, record, metadata.st_mtime_ns, metadata.st_size))
    from runtime.runner_enrollment import population_matches, source_paths, source_metadata
    source_stats = [source_metadata(path) for path in source_paths(Path(root or PROJECT_ROOT).resolve())]
    signature = _hash([manifest, evidence, [(str(p), mtime, size) for p, _, mtime, size in paths], source_stats])
    if _VERIFIED_CACHE.get(str(directory)) == signature:
        return json.dumps({**policy, "selection_revision": manifest.get("revision"),
                           "selection_evidence_sha256": manifest["evidence_sha256"]}, sort_keys=True)
    cases = []
    for path, record, _, _ in paths:
        case = _read(path)
        if not case or _hash(case) != record.get("sha256"):
            return base
        cases.append(case)
    verified = compare_cohort(cases, now=selected_at)
    if (not verified.get("accepted") or verified.get("selected") != evidence.get("selected")
            or not population_matches(Path(root or PROJECT_ROOT).resolve(), evidence["cohort_id"], cases)):
        return base
    if len(_VERIFIED_CACHE) >= 32:
        _VERIFIED_CACHE.pop(next(iter(_VERIFIED_CACHE)))
    _VERIFIED_CACHE[str(directory)] = signature
    return json.dumps({**policy, "selection_revision": manifest.get("revision"),
                       "selection_evidence_sha256": manifest["evidence_sha256"]}, sort_keys=True)


def evaluate_completed_cohorts(*, root: Path | str | None = None, cfg: Any = None,
                              now: dt.datetime | None = None) -> dict[str, Any]:
    cfg = CFG if cfg is None else cfg
    stamp = now or _now()
    directory = _directory(root)
    current = runner_price_policy.parse_policy(entry_policy(cfg, root=root, now=stamp))
    if (current is None or getattr(cfg, "DRY_RUN", False) is not True
            or getattr(cfg, "PAPER_RUNNER_RESEARCH_ENABLED", False) is not True):
        return {"status": "disabled"}
    grouped: dict[str, list[dict[str, Any]]] = {}
    incomplete_files = False
    for state in ("active", "closed", "invalid"):
        for path in (directory / state).glob("*.json"):
            case = _read(path)
            if not case or not _cohort_name(case.get("cohort_id")):
                incomplete_files = True
            elif case.get("baseline_id") == _policy_id(current):
                grouped.setdefault(case["cohort_id"], []).append(case)
    selected = None
    for cohort, cases in sorted(grouped.items()):
        report = compare_cohort(cases, now=stamp)
        if incomplete_files or list((directory / "coverage_gaps").glob(f"{cohort}_*.json")):
            report["accepted"] = False
            report["reasons"] = sorted(set(report["reasons"] + ["incomplete_enrollment_coverage"]))
        if report["accepted"]:
            from runtime.runner_enrollment import population_matches
            if not population_matches(Path(root or PROJECT_ROOT).resolve(), cohort, cases):
                report["accepted"] = False
                report["reasons"] = sorted(set(report["reasons"] + ["incomplete_first_partial_population"]))
        _write(directory / "evaluations" / f"{cohort}.json", report)
        if report["accepted"]:
            selected = report
    if selected and getattr(cfg, "PAPER_RUNNER_RESEARCH_AUTO_APPLY", False) is True:
        previous = _read(directory / "active_policy.json")
        configured = runner_price_policy.parse_policy(runner_price_policy.freeze_policy(cfg, dry_run=True))
        policy = selected["selected"]["parameters"]
        # The proposed change is one adjacent step; safety, sizing, stops and
        # holding-time settings cannot be changed by this transport.
        if (configured is not None and abs(policy["max_price_drawdown_pct"] - current["max_price_drawdown_pct"]) <= 5
                and all(policy[k] == current[k] for k in policy if k != "max_price_drawdown_pct")):
            revision = _hash([selected["cohort_id"], _policy_id(policy), stamp.isoformat()])[:20]
            manifest = {"version": VERSION, "role": "paper_runner_exit_only", "revision": revision,
                        "parameters": policy, "previous_parameters": current,
                        "configured_policy_id": _policy_id(configured), "selected_at": stamp.isoformat(),
                        "expires_at": (stamp + dt.timedelta(days=SELECTION_LIFETIME_DAYS)).isoformat(),
                        "evidence_name": f"{selected['cohort_id']}.json", "evidence_sha256": _hash(selected),
                        "action": "rollback" if previous and previous.get("previous_parameters") == policy else "selection"}
            if previous and re.fullmatch(r"[0-9a-f]{20}", str(previous.get("revision") or "")):
                _write(directory / "history" / f"{previous['revision']}.json", previous)
            _write(directory / "active_policy.json", manifest)
            _write(directory / "history" / f"{revision}.json", manifest)
            return {"status": "selected", "revision": revision, "action": manifest["action"]}
    return {"status": "awaiting_comparable_evidence", "cohorts": len(grouped)}
