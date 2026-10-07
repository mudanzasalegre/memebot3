from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.config import CFG
from analytics.core_report_scheduler import REQUIRED_CORE_REPORTS
from runtime.provider_health import provider_health_snapshot

CRITICAL_MODEL_WARNINGS = {
    "in_sample_only",
    "not_enough_rows",
    "single_class",
    "low_precision_at_k",
    "unstable_by_lane",
    "not_ready_for_enforcement",
}

AUTORESEARCH_RUNTIME_FALSE_FLAGS = (
    "AUTORESEARCH_LIVE_PROMOTION_ENABLED",
    "AUTORESEARCH_AUTO_LIVE_PROMOTE",
    "AUTORESEARCH_LLM_CAN_TOUCH_LIVE",
)

AUTORESEARCH_PAPER_PROFILE_FALSE_FLAGS = (
    "LIVE_CANARY_ENABLED",
    "GREEN_SNIPER_LIVE_ENABLED",
    "RESEARCH_RANK_CANARY_LIVE_ENABLED",
    "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED",
    "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED",
    "SNIPER_RESEARCH_MICRO_FALLBACK_LIVE_ENABLED",
    "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED",
    "LATE_MOMENTUM_WATCH_LIVE_ENABLED",
    "AUTO_PROMOTE_LIVE",
    "MODEL_AUTO_PROMOTE",
    "ML_AUTO_PROMOTE_LANES",
    "AUTORESEARCH_LIVE_PROMOTION_ENABLED",
    "AUTORESEARCH_AUTO_LIVE_PROMOTE",
    "LLM_TRADING_ENABLED",
    "AUTORESEARCH_LLM_CAN_TOUCH_LIVE",
)

AUTORESEARCH_CANDIDATE_DIRS = (
    "candidates",
    "accepted_replay",
    "accepted_paper",
    "rejected",
    "failed",
    "live_ready_disabled",
)

AUTORESEARCH_PROFILE_REQUIRED_STATUSES = (
    "accepted_replay",
    "needs_paper",
    "paper_forward_started",
    "accepted_paper",
)

AUTORESEARCH_SECRET_MARKERS = (
    "PRIVATE_KEY",
    "WALLET",
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "RPC_URL",
    "HELIUS",
    "BIRDEYE",
    "RUGCHECK",
)

GOLDEN_0707_FIXTURE_RELATIVE_PATH = (
    Path("tests") / "fixtures" / "golden_0707" / "strategy_regression_0707.json"
)

GOLDEN_0707_EXPECTED_AUDIT = {
    "net_closed_pnl_usd": Decimal("-81.33"),
    "profit_factor": Decimal("0.629"),
    "exit_reason_pnl_usd.LIQUIDITY_CRUSH": Decimal("-138.99"),
    "exit_reason_pnl_usd.NO_PUMP_EXIT": Decimal("-55.26"),
    "partial_pnl_usd.no_partial": Decimal("-215.67"),
    "partial_pnl_usd.partial": Decimal("134.35"),
}

GOLDEN_0707_REQUIRED_FALSE_FLAGS = (
    "LIVE_CANARY_ENABLED",
    "GREEN_SNIPER_LIVE_ENABLED",
    "RESEARCH_RANK_CANARY_LIVE_ENABLED",
    "LATE_MOMENTUM_WATCH_LIVE_ENABLED",
    "LIVE_AGGRESSIVE_TRADING_ENABLED",
    "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED",
    "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED",
    "SNIPER_RESEARCH_MICRO_FALLBACK_LIVE_ENABLED",
    "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED",
    "AUTO_PROMOTE_LIVE",
    "MODEL_AUTO_PROMOTE",
    "ML_AUTO_PROMOTE_LANES",
    "ALLOW_LIVE_POLICY_ENFORCE",
    "WALLET_PRESENT",
    "RPC_URL_PRESENT",
    "SECRETS_PRESENT",
)

GOLDEN_0707_FORBIDDEN_FEATURE_FRAGMENTS = (
    "future",
    "exit_reason",
    "total_pnl",
    "closed_at",
    "realized_pnl",
)


def _bool(name: str, default: bool = False) -> bool:
    return bool(getattr(CFG, name, default))


def _float(name: str, default: float = 0.0) -> float:
    try:
        return float(getattr(CFG, name, default))
    except Exception:
        return default


def _int(name: str, default: int = 0) -> int:
    try:
        return int(getattr(CFG, name, default))
    except Exception:
        return default


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _truthy_text(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _falsey_text(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "n", "off"}


def _golden_0707_fixture_path() -> Path:
    return ROOT / GOLDEN_0707_FIXTURE_RELATIVE_PATH


def _golden_0707_required_for_root() -> bool:
    return (ROOT / ".github").exists() and (ROOT / "scripts" / "strategy_quality_gate.py").exists()


def _decimal_or_error(value: object, errors: list[str], label: str) -> Decimal | None:
    try:
        if isinstance(value, bool):
            raise InvalidOperation
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        errors.append(f"golden_0707 invalid decimal {label}: {value!r}")
        return None


def _int_or_error(value: object, errors: list[str], label: str) -> int | None:
    try:
        if isinstance(value, bool):
            raise ValueError
        return int(value)
    except (ValueError, TypeError):
        errors.append(f"golden_0707 invalid integer {label}: {value!r}")
        return None


def _fmt_decimal(value: Decimal) -> str:
    return format(value, "f")


def _expect_decimal_close(
    errors: list[str],
    label: str,
    actual: Decimal | None,
    expected: Decimal,
    *,
    tolerance: Decimal = Decimal("0.001"),
) -> None:
    if actual is None:
        return
    if abs(actual - expected) > tolerance:
        errors.append(
            f"golden_0707 {label} expected {_fmt_decimal(expected)} got {_fmt_decimal(actual)}"
        )


def _expect_int_equal(errors: list[str], label: str, actual: int | None, expected: int | None) -> None:
    if actual is None or expected is None:
        return
    if actual != expected:
        errors.append(f"golden_0707 {label} expected {expected} got {actual}")


def _parse_fixture_ts(value: object, errors: list[str], label: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        errors.append(f"golden_0707 missing timestamp {label}")
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"golden_0707 invalid timestamp {label}: {text}")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _validate_golden_0707_audit_summary(payload: dict[str, object], errors: list[str]) -> dict[str, Decimal]:
    audit = payload.get("audit_summary")
    if not isinstance(audit, dict):
        errors.append("golden_0707 audit_summary must be an object")
        return {}

    expected_by_key: dict[str, Decimal] = {}
    for key, expected in GOLDEN_0707_EXPECTED_AUDIT.items():
        if "." in key:
            parent_key, child_key = key.split(".", 1)
            parent = audit.get(parent_key)
            if not isinstance(parent, dict):
                errors.append(f"golden_0707 audit_summary.{parent_key} must be an object")
                continue
            actual = _decimal_or_error(parent.get(child_key), errors, f"audit_summary.{key}")
        else:
            actual = _decimal_or_error(audit.get(key), errors, f"audit_summary.{key}")
        _expect_decimal_close(errors, f"audit_summary.{key}", actual, expected)
        expected_by_key[key] = expected

    expected_closed = _int_or_error(audit.get("closed_trades"), errors, "audit_summary.closed_trades")
    if expected_closed is not None and expected_closed <= 0:
        errors.append("golden_0707 audit_summary.closed_trades must be > 0")
    if expected_closed is not None:
        expected_by_key["closed_trades"] = Decimal(expected_closed)
    return expected_by_key


def _validate_golden_0707_sizing_rows(payload: dict[str, object], errors: list[str]) -> None:
    sizing_cases = payload.get("sizing_cases")
    if not isinstance(sizing_cases, list) or not sizing_cases:
        errors.append("golden_0707 sizing_cases must be a non-empty list")
        return

    for idx, case in enumerate(sizing_cases):
        if not isinstance(case, dict):
            errors.append(f"golden_0707 sizing_cases[{idx}] must be an object")
            continue
        lane = str(case.get("lane") or "")
        is_micro = bool(case.get("micro_lane")) or "micro" in lane.lower()
        amount = _decimal_or_error(case.get("resolved_amount_sol"), errors, f"sizing_cases[{idx}].resolved_amount_sol")
        max_allowed = _decimal_or_error(case.get("max_allowed_sol"), errors, f"sizing_cases[{idx}].max_allowed_sol")
        if not is_micro:
            continue
        if amount is not None and amount >= Decimal("0.1"):
            errors.append(
                f"golden_0707 micro sizing regression {case.get('case_id') or idx}: resolved_amount_sol={_fmt_decimal(amount)}"
            )
        if amount is not None and max_allowed is not None and amount > max_allowed:
            errors.append(
                f"golden_0707 micro sizing cap exceeded {case.get('case_id') or idx}: "
                f"{_fmt_decimal(amount)} > {_fmt_decimal(max_allowed)}"
            )
        if max_allowed is not None and max_allowed > Decimal("0.02"):
            errors.append(
                f"golden_0707 micro sizing max_allowed_sol must stay <=0.02 for {case.get('case_id') or idx}"
            )


def _validate_golden_0707_closed_trades(
    payload: dict[str, object],
    errors: list[str],
    expected: dict[str, Decimal],
) -> None:
    rows = payload.get("closed_trades")
    if not isinstance(rows, list) or not rows:
        errors.append("golden_0707 closed_trades must be a non-empty list")
        return

    expected_count = int(expected["closed_trades"]) if "closed_trades" in expected else None
    if expected_count is not None and len(rows) != expected_count:
        errors.append(f"golden_0707 closed_trades expected {expected_count} rows got {len(rows)}")

    seen_keys: set[str] = set()
    total_pnl = Decimal("0")
    exit_totals: dict[str, Decimal] = {}
    partial_totals = {"partial": Decimal("0"), "no_partial": Decimal("0")}

    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"golden_0707 closed_trades[{idx}] must be an object")
            continue
        source_key = str(row.get("source_position_key") or "").strip()
        if not source_key:
            errors.append(f"golden_0707 closed_trades[{idx}] missing source_position_key")
        elif source_key in seen_keys:
            errors.append(f"golden_0707 duplicate source_position_key {source_key}")
        else:
            seen_keys.add(source_key)
        if row.get("closed") is not True:
            errors.append(f"golden_0707 closed_trades[{idx}] must be closed=true")

        lane = str(row.get("entry_lane") or "")
        if "micro" in lane.lower():
            amount = _decimal_or_error(row.get("entry_amount_sol"), errors, f"closed_trades[{idx}].entry_amount_sol")
            max_allowed = _decimal_or_error(row.get("micro_lane_max_sol"), errors, f"closed_trades[{idx}].micro_lane_max_sol")
            if amount is not None and amount >= Decimal("0.1"):
                errors.append(
                    f"golden_0707 micro sizing regression closed trade {source_key or idx}: "
                    f"entry_amount_sol={_fmt_decimal(amount)}"
                )
            if amount is not None and max_allowed is not None and amount > max_allowed:
                errors.append(
                    f"golden_0707 closed trade micro cap exceeded {source_key or idx}: "
                    f"{_fmt_decimal(amount)} > {_fmt_decimal(max_allowed)}"
                )

        pnl = _decimal_or_error(row.get("total_pnl_usd"), errors, f"closed_trades[{idx}].total_pnl_usd")
        if pnl is None:
            continue
        total_pnl += pnl
        exit_reason = str(row.get("exit_reason") or "UNKNOWN").strip().upper()
        exit_totals[exit_reason] = exit_totals.get(exit_reason, Decimal("0")) + pnl
        partial_key = "partial" if row.get("partial_taken") is True else "no_partial"
        partial_totals[partial_key] += pnl

    _expect_decimal_close(
        errors,
        "closed_trades.net_closed_pnl_usd",
        total_pnl,
        expected.get("net_closed_pnl_usd", Decimal("0")),
        tolerance=Decimal("0.01"),
    )
    _expect_decimal_close(
        errors,
        "closed_trades.exit_reason_pnl_usd.LIQUIDITY_CRUSH",
        exit_totals.get("LIQUIDITY_CRUSH"),
        expected.get("exit_reason_pnl_usd.LIQUIDITY_CRUSH", Decimal("0")),
    )
    _expect_decimal_close(
        errors,
        "closed_trades.exit_reason_pnl_usd.NO_PUMP_EXIT",
        exit_totals.get("NO_PUMP_EXIT"),
        expected.get("exit_reason_pnl_usd.NO_PUMP_EXIT", Decimal("0")),
    )
    _expect_decimal_close(
        errors,
        "closed_trades.partial_pnl_usd.no_partial",
        partial_totals["no_partial"],
        expected.get("partial_pnl_usd.no_partial", Decimal("0")),
        tolerance=Decimal("0.01"),
    )
    _expect_decimal_close(
        errors,
        "closed_trades.partial_pnl_usd.partial",
        partial_totals["partial"],
        expected.get("partial_pnl_usd.partial", Decimal("0")),
    )


def _validate_golden_0707_ledger(
    payload: dict[str, object],
    errors: list[str],
    expected: dict[str, Decimal],
) -> None:
    ledger = payload.get("ledger_reconciliation")
    if not isinstance(ledger, dict):
        errors.append("golden_0707 ledger_reconciliation must be an object")
        return
    db_rows = _int_or_error(ledger.get("db_closed_rows"), errors, "ledger_reconciliation.db_closed_rows")
    report_rows = _int_or_error(ledger.get("report_closed_rows"), errors, "ledger_reconciliation.report_closed_rows")
    expected_rows = int(expected["closed_trades"]) if "closed_trades" in expected else None
    _expect_int_equal(errors, "ledger_reconciliation.db_closed_rows", db_rows, expected_rows)
    _expect_int_equal(errors, "ledger_reconciliation.report_closed_rows", report_rows, expected_rows)
    _expect_int_equal(errors, "ledger_reconciliation.db_vs_report_closed_rows", db_rows, report_rows)

    db_total = _decimal_or_error(ledger.get("db_total_pnl_usd"), errors, "ledger_reconciliation.db_total_pnl_usd")
    report_total = _decimal_or_error(
        ledger.get("report_total_pnl_usd"),
        errors,
        "ledger_reconciliation.report_total_pnl_usd",
    )
    expected_total = expected.get("net_closed_pnl_usd", Decimal("0"))
    _expect_decimal_close(errors, "ledger_reconciliation.db_total_pnl_usd", db_total, expected_total)
    _expect_decimal_close(errors, "ledger_reconciliation.report_total_pnl_usd", report_total, expected_total)
    if db_total is not None and report_total is not None:
        _expect_decimal_close(errors, "ledger_reconciliation.db_vs_report_total_pnl_usd", db_total, report_total)


def _validate_golden_0707_safety(payload: dict[str, object], errors: list[str]) -> None:
    safety = payload.get("safety")
    if not isinstance(safety, dict):
        errors.append("golden_0707 safety must be an object")
        return
    if safety.get("DRY_RUN") is not True:
        errors.append("golden_0707 safety requires DRY_RUN=true")
    if safety.get("STRATEGY_OPTIMIZATION_LOCK") is not True:
        errors.append("golden_0707 safety requires STRATEGY_OPTIMIZATION_LOCK=true")
    for flag in GOLDEN_0707_REQUIRED_FALSE_FLAGS:
        if safety.get(flag) is not False:
            errors.append(f"golden_0707 safety requires {flag}=false")


def _validate_golden_0707_threshold(payload: dict[str, object], errors: list[str]) -> None:
    threshold = payload.get("threshold_gate")
    if not isinstance(threshold, dict):
        errors.append("golden_0707 threshold_gate must be an object")
        return
    if threshold.get("objective") != "expected_pnl_precision_floor":
        errors.append("golden_0707 threshold_gate.objective must be expected_pnl_precision_floor")
    if threshold.get("fallback_objective_used") is not False:
        errors.append("golden_0707 threshold_gate must not fallback to expected_pnl")
    floor = _decimal_or_error(threshold.get("precision_floor"), errors, "threshold_gate.precision_floor")
    picked = _decimal_or_error(threshold.get("precision_at_picked"), errors, "threshold_gate.precision_at_picked")
    if floor is None or picked is None:
        return
    if picked >= floor:
        errors.append("golden_0707 threshold fixture must keep a missed precision-floor sentinel")
    if picked < floor and threshold.get("activation_ready") is not False:
        errors.append("golden_0707 threshold_gate must remain inactive when precision floor is missed")
    if picked < floor and str(threshold.get("mode_recommended") or "").lower() != "shadow":
        errors.append("golden_0707 threshold_gate must recommend shadow when precision floor is missed")


def _validate_golden_0707_replay(payload: dict[str, object], errors: list[str]) -> None:
    cases = payload.get("replay_cases")
    if not isinstance(cases, list) or not cases:
        errors.append("golden_0707 replay_cases must be a non-empty list")
        return
    for idx, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"golden_0707 replay_cases[{idx}] must be an object")
            continue
        label = str(case.get("case_id") or idx)
        if case.get("lookahead_used") is not False:
            errors.append(f"golden_0707 replay no-lookahead regression {label}: lookahead_used must be false")
        decision_ts = _parse_fixture_ts(case.get("decision_ts"), errors, f"replay_cases[{idx}].decision_ts")
        feature_ts = _parse_fixture_ts(case.get("feature_max_ts"), errors, f"replay_cases[{idx}].feature_max_ts")
        label_ts = _parse_fixture_ts(case.get("label_ts"), errors, f"replay_cases[{idx}].label_ts")
        if decision_ts and feature_ts and feature_ts > decision_ts:
            errors.append(f"golden_0707 replay no-lookahead regression {label}: feature_max_ts > decision_ts")
        if decision_ts and label_ts and label_ts <= decision_ts:
            errors.append(f"golden_0707 replay no-lookahead regression {label}: label_ts <= decision_ts")

        feature_columns = case.get("feature_columns")
        if not isinstance(feature_columns, list):
            errors.append(f"golden_0707 replay_cases[{idx}].feature_columns must be a list")
            continue
        for column in feature_columns:
            lowered = str(column).lower()
            if any(fragment in lowered for fragment in GOLDEN_0707_FORBIDDEN_FEATURE_FRAGMENTS):
                errors.append(f"golden_0707 replay forbidden feature column {label}: {column}")


def _validate_golden_0707_payload(
    payload: object,
    errors: list[str],
    *,
    source_label: str = "golden_0707",
) -> None:
    if not isinstance(payload, dict):
        errors.append(f"golden_0707 fixture must be an object: {source_label}")
        return
    if payload.get("fixture_id") != "golden_0707_strategy_regression_v1":
        errors.append("golden_0707 fixture_id must be golden_0707_strategy_regression_v1")
    expected = _validate_golden_0707_audit_summary(payload, errors)
    _validate_golden_0707_sizing_rows(payload, errors)
    _validate_golden_0707_closed_trades(payload, errors, expected)
    _validate_golden_0707_ledger(payload, errors, expected)
    _validate_golden_0707_safety(payload, errors)
    _validate_golden_0707_threshold(payload, errors)
    _validate_golden_0707_replay(payload, errors)


def _validate_golden_0707_fixture(errors: list[str]) -> None:
    path = _golden_0707_fixture_path()
    if not path.exists():
        errors.append(f"golden_0707 fixture missing: {GOLDEN_0707_FIXTURE_RELATIVE_PATH.as_posix()}")
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"golden_0707 fixture cannot be parsed: {exc}")
        return
    _validate_golden_0707_payload(payload, errors, source_label=str(path))


def _validate_autoresearch_runtime_flags(errors: list[str]) -> None:
    for name in AUTORESEARCH_RUNTIME_FALSE_FLAGS:
        if _bool(name, False):
            errors.append(f"{name} must remain false")


def _validate_paper_rank_research_profile(errors: list[str]) -> None:
    profile = ROOT / "config" / "profiles" / "paper_rank_research_v1.env"
    if not profile.exists():
        if _bool("PAPER_RANK_RESEARCH_PROFILE_REQUIRED", False):
            errors.append("paper_rank_research_v1 profile is required")
        return
    values = _parse_env_file(profile)
    required_true = (
        "DRY_RUN",
        "PAPER_SNIPER_MODE",
        "STRATEGY_OPTIMIZATION_LOCK",
        "RESEARCH_RANK_CANARY_ENABLED",
        "RESEARCH_RANK_CANARY_PAPER_ENABLED",
        "RESEARCH_RANK_CANARY_PREFER_REAL_LIQUIDITY",
        "GREEN_SNIPER_BUY_RESTRICTED_ENABLED",
        "LATE_MOMENTUM_WATCH_RESEARCH_ENABLED",
        "POST_PARTIAL_PROTECTION_ENABLED",
        "POST_PARTIAL_PROTECTION_PAPER_ENABLED",
    )
    required_false = (
        "LIVE_CANARY_ENABLED",
        "AUTO_PROMOTE_LIVE",
        "MODEL_AUTO_PROMOTE",
        "ML_AUTO_PROMOTE_LANES",
        "ML_ALLOW_RESEARCH_LIVE",
        "ML_ALLOW_UNKNOWN_LIVE",
        "ALLOW_LIVE_POLICY_ENFORCE",
        "RESEARCH_RANK_CANARY_LIVE_ENABLED",
        "GREEN_SNIPER_LIVE_ENABLED",
        "LATE_MOMENTUM_WATCH_BUY_ENABLED",
        "LATE_MOMENTUM_WATCH_AUTORESEARCH_ENABLED",
        "LATE_MOMENTUM_WATCH_LIVE_ENABLED",
        "POST_PARTIAL_PROTECTION_LIVE_ENABLED",
        "SOCIALS_HOT_PATH_BLOCKING",
        "GREEN_SNIPER_REQUIRE_SOCIALS",
    )
    for name in required_true:
        if not _truthy_text(values.get(name)):
            errors.append(f"paper_rank_research_v1 requires {name}=true")
    for name in required_false:
        if not _falsey_text(values.get(name)):
            errors.append(f"paper_rank_research_v1 requires {name}=false")
    if values.get("GREEN_SNIPER_POLICY_MODE", "").strip().lower() != "shadow":
        errors.append("paper_rank_research_v1 requires GREEN_SNIPER_POLICY_MODE=shadow")
    if values.get("RESEARCH_RANK_CANARY_MIN_SCORE", "").strip() not in {"0.647", "64.7", "64.81"}:
        errors.append("paper_rank_research_v1 requires RESEARCH_RANK_CANARY_MIN_SCORE=64.81")
    if values.get("RESEARCH_RANK_CANARY_MIN_PRICE5M", "").strip() != "40":
        errors.append("paper_rank_research_v1 requires RESEARCH_RANK_CANARY_MIN_PRICE5M=40")


def _model_enforcement_requested() -> bool:
    mode = str(getattr(CFG, "ML_GATE_MODE", "shadow") or "shadow").strip().lower()
    if mode in {"legacy", "enforce"}:
        return True
    if mode == "lane_aware":
        lane_modes = (
            str(getattr(CFG, "ML_RESEARCH_MODE", "shadow") or "shadow").strip().lower(),
            str(getattr(CFG, "ML_LIVE_PROFIT_MODE", "sizing_only") or "sizing_only").strip().lower(),
            str(getattr(CFG, "ML_UNKNOWN_LANE_MODE", "shadow") or "shadow").strip().lower(),
        )
        if "enforce" in lane_modes:
            return True
    return bool(
        _bool("GREEN_SNIPER_ML_BLOCK_ENABLED", False)
        or _bool("ML_GREEN_SNIPER_BLOCK_ENABLED", False)
        or (_bool("ML_RISK_VETO_ENABLED", False) and not _bool("ML_RISK_SHADOW_ONLY", True))
    )


def _critical_warnings_from_payload(payload: object) -> set[str]:
    found: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key in ("critical_warnings", "warnings"):
                items = value.get(key)
                if isinstance(items, list):
                    found.update(str(item) for item in items if str(item) in CRITICAL_MODEL_WARNINGS)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return found


def _validate_model_enforcement_warnings(errors: list[str]) -> None:
    if not _model_enforcement_requested():
        return
    report_paths = (
        ROOT / "data" / "metrics" / "model_training_report.json",
        ROOT / "data" / "metrics" / "risk_model_report.json",
        ROOT / "data" / "metrics" / "ev_model_report.json",
        ROOT / "data" / "metrics" / "runner_model_report.json",
        ROOT / "data" / "metrics" / "continuation_model_report.json",
    )
    existing = [path for path in report_paths if path.exists()]
    if not existing:
        errors.append("model enforcement requires model training reports without critical warnings")
        return
    for path in existing:
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            errors.append(f"model enforcement cannot parse {path.relative_to(ROOT)}")
            continue
        critical = sorted(_critical_warnings_from_payload(payload))
        if critical:
            errors.append(
                f"model enforcement blocked by critical warnings in {path.relative_to(ROOT)}: {','.join(critical)}"
            )


def _looks_like_autoresearch_candidate(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if str(payload.get("proposal_id") or "").startswith("ar_"):
        return True
    return "created_at_utc" in payload and "api_budget_sensitive" in payload and "target_lanes" in payload


def _env_values_upper(path: Path) -> dict[str, str]:
    return {str(key).upper(): str(value) for key, value in _parse_env_file(path).items()}


def _env_has_secret_keys(values: dict[str, str]) -> list[str]:
    matches: list[str] = []
    for key in values:
        upper = key.upper()
        if any(marker in upper for marker in AUTORESEARCH_SECRET_MARKERS):
            matches.append(key)
    return sorted(set(matches))


def _validate_autoresearch_paper_profiles(errors: list[str]) -> None:
    profiles_dir = ROOT / "config" / "profiles"
    profiles = sorted(profiles_dir.glob("paper_research_candidate_*.env")) if profiles_dir.exists() else []
    if not profiles:
        if _autoresearch_paper_profile_required():
            errors.append("autoresearch paper profile missing: config/profiles/paper_research_candidate_*.env")
        return
    for profile in profiles:
        values = _env_values_upper(profile)
        label = profile.relative_to(ROOT)
        if not _truthy_text(values.get("DRY_RUN")):
            errors.append(f"autoresearch paper profile requires DRY_RUN=true: {label}")
        for name in AUTORESEARCH_PAPER_PROFILE_FALSE_FLAGS:
            if name in values and not _falsey_text(values.get(name)):
                errors.append(f"autoresearch paper profile requires {name}=false: {label}")
        secret_keys = _env_has_secret_keys(values)
        if secret_keys:
            errors.append(f"autoresearch paper profile must not contain secrets: {label}:{','.join(secret_keys)}")


def _autoresearch_paper_profile_required() -> bool:
    scoreboard_path = ROOT / "data" / "research_runs" / "scoreboard.json"
    try:
        payload = json.loads(scoreboard_path.read_text(encoding="utf-8", errors="ignore")) if scoreboard_path.exists() else {}
    except Exception:
        payload = {}
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and str(entry.get("status") or "") in AUTORESEARCH_PROFILE_REQUIRED_STATUSES:
                return True

    paper_root = ROOT / "data" / "research_runs" / "paper_forward"
    if paper_root.exists():
        for state_path in paper_root.glob("*/paper_forward_state.json"):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            if isinstance(state, dict) and str(state.get("status") or "") in AUTORESEARCH_PROFILE_REQUIRED_STATUSES:
                return True
    return False


def _validate_autoresearch_scoreboard(errors: list[str]) -> None:
    scoreboard_path = ROOT / "data" / "research_runs" / "scoreboard.json"
    if not scoreboard_path.exists():
        errors.append("autoresearch scoreboard missing: data/research_runs/scoreboard.json")
        return
    try:
        payload = json.loads(scoreboard_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        errors.append("autoresearch scoreboard cannot be parsed")
        return
    if isinstance(payload, dict):
        entries = payload.get("entries")
        if entries is not None and not isinstance(entries, list):
            errors.append("autoresearch scoreboard entries must be a list")
    elif not isinstance(payload, list):
        errors.append("autoresearch scoreboard must be a list or object with entries")


def _validate_autoresearch_api_budget(errors: list[str]) -> None:
    api_budget_path = ROOT / "data" / "research_runs" / "api_budget.json"
    if not api_budget_path.exists():
        errors.append("autoresearch api budget missing: data/research_runs/api_budget.json")
        return
    try:
        payload = json.loads(api_budget_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        errors.append("autoresearch api_budget.json cannot be parsed")
        return
    if not isinstance(payload, dict):
        errors.append("autoresearch api_budget.json must be an object")
        return
    comparison = payload.get("comparison")
    if isinstance(comparison, dict):
        if comparison.get("ok") is False:
            errors.append("autoresearch api budget comparison is not ok")
        reasons = comparison.get("rejection_reasons") or []
        if reasons:
            errors.append("autoresearch api budget comparison has rejections: " + ",".join(str(reason) for reason in reasons))
        deltas = comparison.get("deltas") or {}
        try:
            api_429_delta = float(deltas.get("api_429_count") or 0)
        except Exception:
            api_429_delta = 0.0
        try:
            degraded_delta = float(deltas.get("provider_degraded_minutes") or 0)
        except Exception:
            degraded_delta = 0.0
        if api_429_delta > 0:
            errors.append("autoresearch api budget rejects api_429_count_delta > 0")
        if degraded_delta > 0:
            errors.append("autoresearch api budget rejects provider_degraded_minutes_delta > 0")


def _validate_autoresearch_contract(errors: list[str]) -> None:
    research_root = ROOT / "research_loop"
    schema_path = ROOT / "strategy_proposals" / "schema.autoresearch.json"
    if not research_root.exists() and not schema_path.exists() and not (ROOT / "data" / "research_runs").exists():
        return

    required_files = (
        research_root / "safety.yaml",
        research_root / "safety.py",
        research_root / "objectives.yaml",
        research_root / "objectives.py",
        research_root / "experiment_schema.py",
        schema_path,
    )
    for path in required_files:
        if not path.exists():
            errors.append(f"autoresearch required file missing: {path.relative_to(ROOT)}")

    _validate_autoresearch_scoreboard(errors)
    _validate_autoresearch_api_budget(errors)
    _validate_autoresearch_paper_profiles(errors)

    try:
        from research_loop.experiment_schema import CandidatePolicyValidationError, validate_candidate_policy
    except Exception as exc:
        errors.append(f"autoresearch validator import failed: {exc}")
        return

    proposal_root = ROOT / "strategy_proposals"
    for candidate_dir_name in AUTORESEARCH_CANDIDATE_DIRS:
        candidates_dir = proposal_root / candidate_dir_name
        if not candidates_dir.exists():
            continue
        for path in sorted(candidates_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            if not _looks_like_autoresearch_candidate(payload):
                continue
            try:
                validate_candidate_policy(payload)
            except CandidatePolicyValidationError as exc:
                errors.append(f"autoresearch candidate invalid {path.relative_to(ROOT)}: {exc}")


def _payload_has_test_event(value: object) -> bool:
    if isinstance(value, dict):
        if str(value.get("run_id") or "").strip().upper() == "SMOKE":
            return True
        raw = value.get("test_event")
        if isinstance(raw, bool) and raw:
            return True
        if str(raw or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
        return any(_payload_has_test_event(child) for child in value.values())
    if isinstance(value, list):
        return any(_payload_has_test_event(child) for child in value)
    return False


def checks() -> list[str]:
    errors: list[str] = []
    replay = ROOT / "data" / "metrics" / "policy_replay.json"
    paper_forward = ROOT / "data" / "metrics" / "paper_forward_report.json"
    model_root = ROOT / "ml" / "models"
    _validate_paper_rank_research_profile(errors)
    _validate_model_enforcement_warnings(errors)
    _validate_autoresearch_runtime_flags(errors)
    if _bool("STRATEGY_OPTIMIZATION_LOCK", True):
        if not _bool("DRY_RUN", True):
            errors.append("STRATEGY_OPTIMIZATION_LOCK=true requires DRY_RUN=true")
        blocked_flags = (
            "LIVE_CANARY_ENABLED",
            "GREEN_SNIPER_LIVE_ENABLED",
            "RESEARCH_RANK_CANARY_LIVE_ENABLED",
            "LATE_MOMENTUM_WATCH_LIVE_ENABLED",
            "LIVE_AGGRESSIVE_TRADING_ENABLED",
            "BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED",
            "RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED",
            "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED",
            "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED",
            "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED",
            "SNIPER_RESEARCH_MICRO_FALLBACK_LIVE_ENABLED",
            "AUTO_PROMOTE_LIVE",
            "MODEL_AUTO_PROMOTE",
            "ML_AUTO_PROMOTE_LANES",
            "ML_ALLOW_RESEARCH_LIVE",
            "ML_ALLOW_UNKNOWN_LIVE",
            "ALLOW_LIVE_POLICY_ENFORCE",
        )
        for name in blocked_flags:
            if _bool(name, False):
                errors.append(f"STRATEGY_OPTIMIZATION_LOCK=true blocks {name}=true")
    if _bool("POLICY_REPLAY_REQUIRED", False) and not replay.exists():
        errors.append("POLICY_REPLAY_REQUIRED=true but data/metrics/policy_replay.json is missing")
    if _bool("AUTO_PROMOTE_LIVE", False):
        errors.append("AUTO_PROMOTE_LIVE must remain false")
    if _bool("MODEL_AUTO_PROMOTE", False):
        errors.append("MODEL_AUTO_PROMOTE must remain false")
    if _bool("LATE_MOMENTUM_WATCH_AUTORESEARCH_ENABLED", False):
        errors.append("LATE_MOMENTUM_WATCH_AUTORESEARCH_ENABLED must remain false")
    if not _bool("REQUIRE_ENTRY_LANE_FOR_BUY", True):
        errors.append("REQUIRE_ENTRY_LANE_FOR_BUY must remain true")
    if _bool("ALLOW_UNTAGGED_STANDARD_BUY", False):
        errors.append("ALLOW_UNTAGGED_STANDARD_BUY must remain false")
    if not _bool("PUMPSWAP_PRIME_STRICT_ENABLED", True):
        errors.append("PUMPSWAP_PRIME_STRICT_ENABLED must remain true")
    if _bool("PUMP_EARLY_PROFIT_LANE_ENABLED", False) and not _bool("PUMPSWAP_PRIME_STRICT_ENABLED", True):
        errors.append("PUMP_EARLY_PROFIT_LANE_ENABLED=true requires PUMPSWAP_PRIME_STRICT_ENABLED=true")
    if _bool("PUMPSWAP_PRIME_STRICT_ENABLED", True) and not _bool("PUMPSWAP_PRIME_SHADOW_IF_NOT_STRICT", True):
        errors.append("PUMPSWAP_PRIME_STRICT_ENABLED=true requires PUMPSWAP_PRIME_SHADOW_IF_NOT_STRICT=true")
    if _bool("POST_PARTIAL_PROTECTION_LIVE_ENABLED", False):
        errors.append("POST_PARTIAL_PROTECTION_LIVE_ENABLED must remain false")
    if _bool("BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED", False):
        errors.append("BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED must remain false")
    if _bool("RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED", False):
        errors.append("RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED must remain false")
    if _bool("BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED", False):
        errors.append("BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED must remain false")
    if _bool("MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED", False):
        errors.append("MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED must remain false")
    if _bool("SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED", False):
        errors.append("SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED must remain false")
    if _bool("SNIPER_RESEARCH_MICRO_FALLBACK_LIVE_ENABLED", False):
        errors.append("SNIPER_RESEARCH_MICRO_FALLBACK_LIVE_ENABLED must remain false")
    if _bool("PUMPSWAP_PRIME_STRICT_BUY_ENABLED", False):
        errors.append("PUMPSWAP_PRIME_STRICT_BUY_ENABLED must remain false")
    if not _bool("LANE_SIZING_ENABLED", True):
        errors.append("LANE_SIZING_ENABLED must remain true")
    if _float("DEFAULT_PAPER_BUY_SOL", 0.1) > 0.1:
        errors.append("DEFAULT_PAPER_BUY_SOL must stay <=0.1")
    if _float("MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL", 0.001) > 0.02:
        errors.append("MOONSHOT_MICRO_LOTTERY_AMOUNT_SOL must stay <=0.02")
    if _float("MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_AMOUNT_SOL", 0.001) > 0.002:
        errors.append("MOONSHOT_MICRO_LOTTERY_CLUSTER_TAIL_AMOUNT_SOL must stay <=0.002")
    if _float("MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_AMOUNT_SOL", 0.0005) > 0.0005:
        errors.append("MOONSHOT_MICRO_LOTTERY_RISKY_CLUSTER_AMOUNT_SOL must stay <=0.0005")
    if _int("MOONSHOT_MICRO_LOTTERY_MAX_OPEN", 1) > 1:
        errors.append("MOONSHOT_MICRO_LOTTERY_MAX_OPEN must stay <=1")
    if _float("PAPER_EXPLORATION_AMOUNT_SOL", 0.1) > 0.1:
        errors.append("PAPER_EXPLORATION_AMOUNT_SOL must stay <=0.1")
    if _float("PAPER_IDLE_AMOUNT_SOL", 0.1) > 0.1:
        errors.append("PAPER_IDLE_AMOUNT_SOL must stay <=0.1")
    if _float("RESEARCH_RANK_CANARY_SIZE_SOL", 0.005) > 0.03:
        errors.append("RESEARCH_RANK_CANARY_SIZE_SOL must stay <=0.03")
    if _float("RESEARCH_RANK_CANARY_MAX_SIZE_SOL", 0.03) > 0.03:
        errors.append("RESEARCH_RANK_CANARY_MAX_SIZE_SOL must stay <=0.03")
    if _float("SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL", 0.003) > 0.02:
        errors.append("SHADOW_FOLLOWUP_MICRO_AMOUNT_SOL must stay <=0.02")
    if _float("SNIPER_RESEARCH_MICRO_FALLBACK_AMOUNT_SOL", 0.003) > 0.02:
        errors.append("SNIPER_RESEARCH_MICRO_FALLBACK_AMOUNT_SOL must stay <=0.02")
    if _int("SNIPER_RESEARCH_MICRO_FALLBACK_MAX_OPEN", 1) > 1:
        errors.append("SNIPER_RESEARCH_MICRO_FALLBACK_MAX_OPEN must stay <=1")
    if _float("LATE_MOMENTUM_MICRO_AMOUNT_SOL", 0.003) > 0.02:
        errors.append("LATE_MOMENTUM_MICRO_AMOUNT_SOL must stay <=0.02")
    if _float("RESEARCH_RANK_CANARY_PULLBACK_TAIL_AMOUNT_SOL", 0.005) > 0.005:
        errors.append("RESEARCH_RANK_CANARY_PULLBACK_TAIL_AMOUNT_SOL must stay <=0.005")
    if _bool("STRATEGY_OPTIMIZATION_LOCK", True):
        if _bool("RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED", False):
            if _bool("RESEARCH_RANK_CANARY_LIVE_ENABLED", False):
                errors.append(
                    "STRATEGY_OPTIMIZATION_LOCK=true allows RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED only with live disabled"
                )
            if _float("RESEARCH_RANK_CANARY_SIZE_SOL", 0.005) > 0.005:
                errors.append(
                    "STRATEGY_OPTIMIZATION_LOCK=true allows RESEARCH_RANK_CANARY_NORMAL_BUY_ENABLED only with RESEARCH_RANK_CANARY_SIZE_SOL<=0.005"
                )
        if _bool("RESEARCH_RANK_CANARY_PULLBACK_BUY_ENABLED", False):
            errors.append("STRATEGY_OPTIMIZATION_LOCK=true requires RESEARCH_RANK_CANARY_PULLBACK_BUY_ENABLED=false")
    if _float("BIRD_TP1_PCT", 25.0) <= 0:
        errors.append("partial ladder requires BIRD_TP1_PCT > 0")
    if _bool("STRATEGY_OPTIMIZATION_LOCK", True) and not _bool("RUNNER_TURBO_PAPER_ONLY", True):
        errors.append("STRATEGY_OPTIMIZATION_LOCK=true requires RUNNER_TURBO_PAPER_ONLY=true")
    if _bool("LLM_TRADING_ENABLED", False):
        errors.append("LLM_TRADING_ENABLED must remain false")
    if _bool("SOCIALS_HOT_PATH_BLOCKING", False) or _bool("GREEN_SNIPER_REQUIRE_SOCIALS", False):
        errors.append("socials must not be a hard gate")
    for name in ("GREEN_SNIPER_POLICY_MODE", "LATE_MOMENTUM_POLICY_MODE", "RESEARCH_RANK_POLICY_MODE"):
        if str(getattr(CFG, name, "") or "").strip().lower() == "enforce" and not _bool("ALLOW_LIVE_POLICY_ENFORCE", False):
            errors.append(f"{name}=enforce requires explicit ALLOW_LIVE_POLICY_ENFORCE")
    if _bool("LIVE_CANARY_ENABLED", False):
        if _int("LIVE_CANARY_MAX_OPEN", 0) < 1 or _int("LIVE_CANARY_MAX_OPEN", 0) > 1:
            errors.append("LIVE_CANARY_MAX_OPEN must stay finite and <=1")
        if _int("LIVE_CANARY_MAX_DAILY_BUYS", 0) < 1 or _int("LIVE_CANARY_MAX_DAILY_BUYS", 0) > 3:
            errors.append("LIVE_CANARY_MAX_DAILY_BUYS must stay finite and <=3")
        if _float("LIVE_CANARY_DAILY_LOSS_CAP_SOL", 0.05) <= 0:
            errors.append("LIVE_CANARY_DAILY_LOSS_CAP_SOL is required")
        if not _bool("LIVE_REQUIRE_ROUTE", True):
            errors.append("LIVE_CANARY requires LIVE_REQUIRE_ROUTE=true")
        if not _bool("LIVE_REQUIRE_PROVIDER_HEALTH", True):
            errors.append("LIVE_CANARY requires LIVE_REQUIRE_PROVIDER_HEALTH=true")
        if not _bool("LIVE_CANARY_MANUAL_APPROVAL", False):
            errors.append("LIVE_CANARY requires LIVE_CANARY_MANUAL_APPROVAL=true")
        if not replay.exists():
            errors.append("LIVE_CANARY requires data/metrics/policy_replay.json")
        if not paper_forward.exists():
            errors.append("LIVE_CANARY requires data/metrics/paper_forward_report.json")
        if not model_root.exists():
            errors.append("LIVE_CANARY requires ml/models registry directory")
    if _bool("GREEN_SNIPER_LIVE_ENABLED", False):
        if _bool("DRY_RUN", True):
            errors.append("live canary requires DRY_RUN=0")
        if not _bool("GREEN_SNIPER_REQUIRE_ROUTE_LIVE", True):
            errors.append("live canary requires GREEN_SNIPER_REQUIRE_ROUTE_LIVE=true")
        if _float("GREEN_SNIPER_LIVE_SIZE_SOL", 0.01) > 0.01:
            errors.append("GREEN_SNIPER_LIVE_SIZE_SOL must stay <=0.01 in safe canary")
        if _int("GREEN_SNIPER_LIVE_MAX_OPEN", 1) > 1:
            errors.append("GREEN_SNIPER_LIVE_MAX_OPEN must stay <=1 in safe canary")
        if _float("GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL", 0.0) <= 0:
            errors.append("GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL is required")
        if _int("GREEN_SNIPER_LIVE_MAX_DAILY_BUYS", 0) < 0:
            errors.append("GREEN_SNIPER_LIVE_MAX_DAILY_BUYS must be >=0 (0 means unlimited)")
        provider_health = provider_health_snapshot()
        if provider_health.get("overall_status") == "critical":
            errors.append("provider health critical; live canary must not start")
    if _bool("PAPER_SNIPER_MODE", False):
        if not _bool("GREEN_SNIPER_REJECT_SHADOW_ENABLED", True):
            errors.append("paper sniper requires GREEN_SNIPER_REJECT_SHADOW_ENABLED=true for high-risk shadows")
    missing_core_reports = [
        name
        for name in REQUIRED_CORE_REPORTS
        if not (ROOT / "data" / "metrics" / name).exists()
    ]
    if missing_core_reports and not _bool("CORE_REPORTS_AUTO_REGEN_ENABLED", True):
        errors.append(
            "CORE_REPORTS_AUTO_REGEN_ENABLED=false with missing critical reports: "
            + ",".join(missing_core_reports)
        )
    metrics_root = ROOT / "data" / "metrics"
    current_run_summary = metrics_root / "current_run_summary.json"
    reports_present = metrics_root.exists() and any((metrics_root / name).exists() for name in REQUIRED_CORE_REPORTS)
    if reports_present and not current_run_summary.exists():
        errors.append("data/metrics/current_run_summary.json is missing")
    for name in REQUIRED_CORE_REPORTS:
        path = ROOT / "data" / "metrics" / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        if _payload_has_test_event(payload):
            errors.append(f"{name} includes test_event/SMOKE data by default")
    ranges = str(getattr(CFG, "PUMP_EARLY_PROFIT_BLOCK_PRICE5M_RANGES", "") or "")
    if "25:999" in ranges and _bool("GREEN_SNIPER_ENABLED", True):
        errors.append("price5m 25:999 block contradicts green sniper")
    missed = ROOT / "data" / "metrics" / "missed_pumps.json"
    if missed.exists():
        try:
            payload = json.loads(missed.read_text(encoding="utf-8", errors="ignore"))
            rows = payload
            if isinstance(payload, dict):
                rows = payload.get("data") or payload.get("rows") or []
            if rows and isinstance(rows, list) and "confirmed_later_peak_pct" not in rows[0]:
                errors.append("missed_pumps.json uses legacy schema; regenerate tools/missed_pumps_report.py")
        except Exception:
            errors.append("missed_pumps.json cannot be parsed")
    if _golden_0707_required_for_root():
        _validate_golden_0707_fixture(errors)
    _validate_autoresearch_contract(errors)
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warn-only", action="store_true")
    parser.add_argument("--golden-0707-only", action="store_true")
    args = parser.parse_args()
    errors: list[str] = []
    if args.golden_0707_only:
        _validate_golden_0707_fixture(errors)
    else:
        errors = checks()
    for error in errors:
        print(f"strategy_quality_gate=fail {error}")
    if errors and not args.warn_only:
        raise SystemExit(1)
    print("strategy_quality_gate=ok" if not errors else "strategy_quality_gate=warn")


if __name__ == "__main__":
    main()
