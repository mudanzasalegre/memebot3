from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


TRUE_TEXT = {"1", "true", "yes", "y", "on"}
FALSE_TEXT = {"0", "false", "no", "n", "off"}

LIVE_CANARY_PROTOCOL_ID = "manual_canary_v1"
LIVE_CANARY_APPROVAL_TTL_HOURS = 24
LIVE_CANARY_MAX_OPEN_LIMIT = 1
LIVE_CANARY_MAX_DAILY_BUYS_LIMIT = 3
LIVE_CANARY_DAILY_LOSS_CAP_SOL_LIMIT = 0.05
LIVE_CANARY_SIZE_SOL_LIMIT = 0.01
LIVE_CANARY_RANK_SIZE_SOL_LIMIT = 0.02

DEFAULT_MIN_PAPER_CLOSED_TRADES = 25
DEFAULT_MIN_PAPER_NET_PNL_USD = 0.0
DEFAULT_MIN_PAPER_PROFIT_FACTOR = 1.0

LIVE_PROFILE_FALSE_FLAGS = (
    "AUTO_PROMOTE_LIVE",
    "MODEL_AUTO_PROMOTE",
    "ML_AUTO_PROMOTE_LANES",
    "AUTORESEARCH_LIVE_PROMOTION_ENABLED",
    "AUTORESEARCH_AUTO_LIVE_PROMOTE",
    "AUTORESEARCH_LLM_CAN_TOUCH_LIVE",
    "LLM_TRADING_ENABLED",
    "ALLOW_LIVE_POLICY_ENFORCE",
)

LIVE_PROFILE_TRUE_FLAGS = (
    "LIVE_REQUIRE_ROUTE",
    "LIVE_REQUIRE_PROVIDER_HEALTH",
    "REQUIRE_ENTRY_LANE_FOR_BUY",
    "LIVE_CANARY_ROLLBACK_ON_LIQUIDITY_CRUSH",
    "LIVE_CANARY_ROLLBACK_ON_DAILY_LOSS_CAP",
    "LIVE_CANARY_ROLLBACK_ON_PROVIDER_CRITICAL",
)

LIVE_FLAG_NAMES = (
    "LIVE_CANARY_ENABLED",
    "GREEN_SNIPER_LIVE_ENABLED",
    "RESEARCH_RANK_CANARY_LIVE_ENABLED",
    "LATE_MOMENTUM_WATCH_LIVE_ENABLED",
    "LIVE_AGGRESSIVE_TRADING_ENABLED",
    "MOONSHOT_MICRO_LOTTERY_LIVE_ENABLED",
    "SHADOW_FOLLOWUP_MICRO_LIVE_ENABLED",
    "SNIPER_RESEARCH_MICRO_FALLBACK_LIVE_ENABLED",
    "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED",
)


@dataclass(frozen=True)
class LiveCanaryGate:
    id: str
    passed: bool
    detail: str
    value: Any = None
    required: Any = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = "pass" if self.passed else "block"
        return payload


@dataclass(frozen=True)
class LiveCanaryGuardResult:
    passed: bool
    gates: tuple[LiveCanaryGate, ...]
    errors: tuple[str, ...]
    mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "mode": self.mode,
            "gates": [gate.to_dict() for gate in self.gates],
            "errors": list(self.errors),
        }


class LiveCanaryGuardError(RuntimeError):
    pass


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUE_TEXT


def _falsey(value: Any) -> bool:
    return str(value or "").strip().lower() in FALSE_TEXT


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(str(value).strip())
    except Exception:
        return default


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def _parse_dt(value: Any) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def load_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def live_flags_declared(values: dict[str, Any]) -> bool:
    if _falsey(values.get("DRY_RUN")):
        return True
    return any(_truthy(values.get(name)) for name in LIVE_FLAG_NAMES)


def _gate(gate_id: str, passed: bool, detail: str, *, value: Any = None, required: Any = None) -> LiveCanaryGate:
    return LiveCanaryGate(gate_id, bool(passed), detail, value, required)


def _cap_gate(
    values: dict[str, Any],
    name: str,
    *,
    minimum: float,
    maximum: float,
    integer: bool = False,
    required_if_missing: bool = True,
) -> LiveCanaryGate:
    raw = values.get(name)
    numeric = _int(raw) if integer else _float(raw)
    if numeric is None:
        return _gate(
            f"profile.{name}",
            not required_if_missing,
            f"{name} is missing or non-numeric",
            value=raw,
            required=f"{minimum:g}..{maximum:g}",
        )
    return _gate(
        f"profile.{name}",
        minimum <= float(numeric) <= maximum,
        f"{name} must be finite and between {minimum:g} and {maximum:g}",
        value=numeric,
        required=f"{minimum:g}..{maximum:g}",
    )


def validate_live_canary_profile_values(
    values: dict[str, Any],
    *,
    label: str = "live_profile",
    require_approval: bool = False,
    now: dt.datetime | None = None,
) -> LiveCanaryGuardResult:
    if not live_flags_declared(values):
        return LiveCanaryGuardResult(True, tuple(), tuple(), "paper")

    now = now or dt.datetime.now(dt.timezone.utc)
    gates: list[LiveCanaryGate] = []
    gates.append(
        _gate(
            "profile.protocol",
            str(values.get("LIVE_CANARY_PROTOCOL") or "").strip() == LIVE_CANARY_PROTOCOL_ID,
            f"{label} must declare LIVE_CANARY_PROTOCOL={LIVE_CANARY_PROTOCOL_ID}",
            value=values.get("LIVE_CANARY_PROTOCOL"),
            required=LIVE_CANARY_PROTOCOL_ID,
        )
    )
    gates.append(
        _gate(
            "profile.live_canary_enabled",
            _truthy(values.get("LIVE_CANARY_ENABLED")),
            "LIVE_CANARY_ENABLED must be true in explicit live-canary profiles",
            value=values.get("LIVE_CANARY_ENABLED"),
            required="true",
        )
    )
    gates.append(
        _gate(
            "profile.strategy_lock_lifted",
            _falsey(values.get("STRATEGY_OPTIMIZATION_LOCK")),
            "STRATEGY_OPTIMIZATION_LOCK must be false only inside the explicit live profile",
            value=values.get("STRATEGY_OPTIMIZATION_LOCK"),
            required="false",
        )
    )
    gates.append(
        _gate(
            "profile.dry_run",
            _falsey(values.get("DRY_RUN")),
            "DRY_RUN must be false only inside the explicit live profile",
            value=values.get("DRY_RUN"),
            required="false",
        )
    )

    for name in LIVE_PROFILE_FALSE_FLAGS:
        gates.append(
            _gate(
                f"profile.{name}",
                name not in values or _falsey(values.get(name)),
                f"{name} must remain false for manual-only canary",
                value=values.get(name),
                required="false",
            )
        )
    for name in LIVE_PROFILE_TRUE_FLAGS:
        gates.append(
            _gate(
                f"profile.{name}",
                _truthy(values.get(name)),
                f"{name} must be true for live-canary rollback and route safety",
                value=values.get(name),
                required="true",
            )
        )

    gates.extend(
        [
            _cap_gate(values, "LIVE_CANARY_MAX_OPEN", minimum=1, maximum=LIVE_CANARY_MAX_OPEN_LIMIT, integer=True),
            _cap_gate(
                values,
                "LIVE_CANARY_MAX_DAILY_BUYS",
                minimum=1,
                maximum=LIVE_CANARY_MAX_DAILY_BUYS_LIMIT,
                integer=True,
            ),
            _cap_gate(
                values,
                "LIVE_CANARY_DAILY_LOSS_CAP_SOL",
                minimum=0.000001,
                maximum=LIVE_CANARY_DAILY_LOSS_CAP_SOL_LIMIT,
            ),
            _cap_gate(values, "LIVE_CANARY_SIZE_SOL", minimum=0.000001, maximum=LIVE_CANARY_SIZE_SOL_LIMIT),
        ]
    )

    if _truthy(values.get("GREEN_SNIPER_LIVE_ENABLED")):
        gates.extend(
            [
                _cap_gate(
                    values,
                    "GREEN_SNIPER_LIVE_MAX_OPEN",
                    minimum=1,
                    maximum=LIVE_CANARY_MAX_OPEN_LIMIT,
                    integer=True,
                ),
                _cap_gate(
                    values,
                    "GREEN_SNIPER_LIVE_MAX_DAILY_BUYS",
                    minimum=1,
                    maximum=LIVE_CANARY_MAX_DAILY_BUYS_LIMIT,
                    integer=True,
                ),
                _cap_gate(
                    values,
                    "GREEN_SNIPER_LIVE_MAX_DAILY_LOSS_SOL",
                    minimum=0.000001,
                    maximum=LIVE_CANARY_DAILY_LOSS_CAP_SOL_LIMIT,
                ),
                _cap_gate(
                    values,
                    "GREEN_SNIPER_LIVE_SIZE_SOL",
                    minimum=0.000001,
                    maximum=LIVE_CANARY_SIZE_SOL_LIMIT,
                ),
            ]
        )

    if _truthy(values.get("RESEARCH_RANK_CANARY_LIVE_ENABLED")):
        gates.extend(
            [
                _cap_gate(
                    values,
                    "RESEARCH_RANK_CANARY_MAX_OPEN",
                    minimum=1,
                    maximum=LIVE_CANARY_MAX_OPEN_LIMIT,
                    integer=True,
                ),
                _cap_gate(
                    values,
                    "RESEARCH_RANK_CANARY_MAX_DAILY_BUYS",
                    minimum=1,
                    maximum=LIVE_CANARY_MAX_DAILY_BUYS_LIMIT,
                    integer=True,
                ),
                _cap_gate(
                    values,
                    "RESEARCH_RANK_CANARY_PRIORITY_SIZE_SOL",
                    minimum=0.000001,
                    maximum=LIVE_CANARY_RANK_SIZE_SOL_LIMIT,
                ),
            ]
        )

    is_template = _truthy(values.get("LIVE_CANARY_PROFILE_TEMPLATE"))
    manual_flag_ok = _truthy(values.get("LIVE_CANARY_MANUAL_APPROVAL"))
    approval_time = _parse_dt(values.get("LIVE_CANARY_APPROVED_AT_UTC"))
    approval_age_h = None
    if approval_time is not None:
        approval_age_h = max(0.0, (now - approval_time).total_seconds() / 3600.0)
    if require_approval:
        gates.extend(
            [
                _gate(
                    "manual.profile_not_template",
                    not is_template,
                    "Runtime live start must use a generated approval profile, not a checked-in template",
                    value=is_template,
                    required=False,
                ),
                _gate(
                    "manual.approval_flag",
                    manual_flag_ok,
                    "LIVE_CANARY_MANUAL_APPROVAL must be true for runtime live start",
                    value=values.get("LIVE_CANARY_MANUAL_APPROVAL"),
                    required="true",
                ),
                _gate(
                    "manual.approved_by",
                    bool(str(values.get("LIVE_CANARY_APPROVED_BY") or "").strip()),
                    "LIVE_CANARY_APPROVED_BY must identify the operator",
                    value=values.get("LIVE_CANARY_APPROVED_BY"),
                    required="non-empty",
                ),
                _gate(
                    "manual.approval_id",
                    bool(str(values.get("LIVE_CANARY_APPROVAL_ID") or "").strip()),
                    "LIVE_CANARY_APPROVAL_ID must identify the manual approval event",
                    value=values.get("LIVE_CANARY_APPROVAL_ID"),
                    required="non-empty",
                ),
                _gate(
                    "manual.approval_freshness",
                    approval_age_h is not None and approval_age_h <= LIVE_CANARY_APPROVAL_TTL_HOURS,
                    "Manual approval must be timestamped and less than 24h old",
                    value=None if approval_age_h is None else round(approval_age_h, 3),
                    required=f"<= {LIVE_CANARY_APPROVAL_TTL_HOURS}h",
                ),
            ]
        )

    errors = tuple(gate.detail for gate in gates if not gate.passed)
    return LiveCanaryGuardResult(not errors, tuple(gates), errors, "live_canary" if not errors else "blocked")


def validate_paper_sample(
    current_summary: dict[str, Any],
    *,
    min_closed_trades: int = DEFAULT_MIN_PAPER_CLOSED_TRADES,
    min_net_pnl_usd: float = DEFAULT_MIN_PAPER_NET_PNL_USD,
    min_profit_factor: float = DEFAULT_MIN_PAPER_PROFIT_FACTOR,
) -> LiveCanaryGuardResult:
    closed_trades = _int(current_summary.get("closed_trades") or current_summary.get("closed_positions"), 0) or 0
    buys = _int(current_summary.get("buys") or current_summary.get("buy_count"), 0) or 0
    total_pnl_usd = _float(current_summary.get("total_pnl_usd"))
    profit_factor = _float(current_summary.get("profit_factor"))
    gates = [
        _gate(
            "sample.closed_trades",
            closed_trades >= min_closed_trades,
            "Paper sample must contain enough closed trades before live canary",
            value={"closed_trades": closed_trades, "buys": buys},
            required={"min_closed_trades": min_closed_trades},
        ),
        _gate(
            "sample.net_pnl",
            total_pnl_usd is not None and total_pnl_usd > min_net_pnl_usd,
            "Paper sample must have positive net closed PnL before live canary",
            value=total_pnl_usd,
            required=f"> {min_net_pnl_usd:g}",
        ),
        _gate(
            "sample.profit_factor",
            profit_factor is not None and profit_factor >= min_profit_factor,
            "Paper sample must meet the profit-factor floor before live canary",
            value=profit_factor,
            required=f">= {min_profit_factor:g}",
        ),
    ]
    errors = tuple(gate.detail for gate in gates if not gate.passed)
    return LiveCanaryGuardResult(not errors, tuple(gates), errors, "paper_sample" if not errors else "blocked")


def latest_accepted_candidate(scoreboard: Any) -> dict[str, Any] | None:
    entries = scoreboard.get("entries") if isinstance(scoreboard, dict) else scoreboard
    if not isinstance(entries, list):
        return None
    accepted = [
        row
        for row in entries
        if isinstance(row, dict)
        and str(row.get("status") or "").strip().lower() in {"accepted_replay", "accepted_paper"}
        and (_float(row.get("objective_score"), 0.0) or 0.0) > 0.0
    ]
    if not accepted:
        return None
    return sorted(
        accepted,
        key=lambda row: str(row.get("evaluated_at_utc") or row.get("created_at_utc") or row.get("run_id") or ""),
        reverse=True,
    )[0]


def evaluate_live_start(
    *,
    root: Path,
    cfg: Any,
    profile_values: dict[str, Any] | None = None,
    profile_label: str = "runtime",
    now: dt.datetime | None = None,
) -> LiveCanaryGuardResult:
    values = profile_values
    if values is None:
        raw_profile = str(os.getenv("CONFIG_PROFILE_PATH") or "").strip()
        values = load_env_values(Path(raw_profile)) if raw_profile else dict(os.environ)
        profile_label = raw_profile or "environment"

    profile_result = validate_live_canary_profile_values(values, label=profile_label, require_approval=True, now=now)
    min_closed = _int(getattr(cfg, "LIVE_PROMOTION_MIN_PAPER_CLOSED_TRADES", None), DEFAULT_MIN_PAPER_CLOSED_TRADES)
    min_pnl = _float(getattr(cfg, "LIVE_PROMOTION_MIN_NET_PNL_USD", None), DEFAULT_MIN_PAPER_NET_PNL_USD)
    min_pf = _float(getattr(cfg, "LIVE_PROMOTION_MIN_PROFIT_FACTOR", None), DEFAULT_MIN_PAPER_PROFIT_FACTOR)
    metrics_dir = root / "data" / "metrics"
    current_summary = read_json_object(metrics_dir / "current_run_summary.json")
    sample_result = validate_paper_sample(
        current_summary,
        min_closed_trades=int(min_closed or DEFAULT_MIN_PAPER_CLOSED_TRADES),
        min_net_pnl_usd=float(min_pnl if min_pnl is not None else DEFAULT_MIN_PAPER_NET_PNL_USD),
        min_profit_factor=float(min_pf if min_pf is not None else DEFAULT_MIN_PAPER_PROFIT_FACTOR),
    )
    scoreboard = read_json_object(root / "data" / "research_runs" / "scoreboard.json")
    accepted = latest_accepted_candidate(scoreboard)
    candidate_gate = _gate(
        "sample.accepted_candidate",
        accepted is not None,
        "AutoResearch must have an accepted positive replay or paper candidate before live canary",
        value={"run_id": accepted.get("run_id"), "objective_score": accepted.get("objective_score")} if accepted else None,
        required="accepted_replay_or_accepted_paper_with_positive_score",
    )
    gates = [*profile_result.gates, *sample_result.gates, candidate_gate]
    errors = tuple(gate.detail for gate in gates if not gate.passed)
    return LiveCanaryGuardResult(not errors, tuple(gates), errors, "live_canary" if not errors else "blocked")


def ensure_live_start_allowed(*, root: Path, cfg: Any) -> None:
    result = evaluate_live_start(root=root, cfg=cfg)
    if not result.passed:
        blocked = ",".join(gate.id for gate in result.gates if not gate.passed) or "unknown"
        raise LiveCanaryGuardError(f"live canary guard blocked startup: {blocked}")


__all__ = [
    "DEFAULT_MIN_PAPER_CLOSED_TRADES",
    "DEFAULT_MIN_PAPER_NET_PNL_USD",
    "DEFAULT_MIN_PAPER_PROFIT_FACTOR",
    "LIVE_CANARY_PROTOCOL_ID",
    "LiveCanaryGate",
    "LiveCanaryGuardError",
    "LiveCanaryGuardResult",
    "ensure_live_start_allowed",
    "evaluate_live_start",
    "latest_accepted_candidate",
    "live_flags_declared",
    "load_env_values",
    "read_json_object",
    "validate_live_canary_profile_values",
    "validate_paper_sample",
]
