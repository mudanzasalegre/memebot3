from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analytics import runner_ladder
from config.config import CFG


@dataclass(frozen=True)
class BirdRunnerStep:
    trigger_pct: float
    fraction: float


DEFAULT_BIRD_RUNNER_STEPS = (
    BirdRunnerStep(25.0, 0.25),
    BirdRunnerStep(50.0, 0.25),
    BirdRunnerStep(100.0, 0.20),
    BirdRunnerStep(300.0, 0.15),
    BirdRunnerStep(700.0, 0.07),
    BirdRunnerStep(1000.0, 0.05),
)
DEFAULT_MOONBAG_FRACTION = 0.03


def _cfg_float(cfg: Any, name: str, default: float) -> float:
    try:
        value = getattr(cfg, name, default)
        if value is None:
            return float(default)
        out = float(value)
        if out != out or out == float("inf") or out == float("-inf"):
            return float(default)
        return out
    except Exception:
        return float(default)


def _cfg_bool(cfg: Any, name: str, default: bool) -> bool:
    value = getattr(cfg, name, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def configured_bird_runner_steps(cfg: Any = CFG) -> tuple[BirdRunnerStep, ...]:
    steps = (
        BirdRunnerStep(_cfg_float(cfg, "BIRD_TP1_PCT", 25.0), _cfg_float(cfg, "BIRD_TP1_FRACTION", 0.25)),
        BirdRunnerStep(_cfg_float(cfg, "BIRD_TP2_PCT", 50.0), _cfg_float(cfg, "BIRD_TP2_FRACTION", 0.25)),
        BirdRunnerStep(_cfg_float(cfg, "BIRD_TP3_PCT", 100.0), _cfg_float(cfg, "BIRD_TP3_FRACTION", 0.20)),
        BirdRunnerStep(_cfg_float(cfg, "BIRD_TP4_PCT", 300.0), _cfg_float(cfg, "BIRD_TP4_FRACTION", 0.15)),
        BirdRunnerStep(_cfg_float(cfg, "BIRD_TP5_PCT", 700.0), _cfg_float(cfg, "BIRD_TP5_FRACTION", 0.07)),
        BirdRunnerStep(_cfg_float(cfg, "BIRD_TP6_PCT", 1000.0), _cfg_float(cfg, "BIRD_TP6_FRACTION", 0.05)),
    )
    valid = [step for step in steps if step.trigger_pct > 0 and 0.0 < step.fraction < 1.0]
    return tuple(sorted(valid, key=lambda step: step.trigger_pct))


def configured_moonbag_fraction(cfg: Any = CFG) -> float:
    return max(0.0, min(0.95, _cfg_float(cfg, "BIRD_MOONBAG_FRACTION", DEFAULT_MOONBAG_FRACTION)))


def bird_runner_multi_partial_enabled(*, dry_run: bool, cfg: Any = CFG) -> bool:
    if not _cfg_bool(cfg, "BIRD_RUNNER_MULTI_PARTIAL_ENABLED", True):
        return False
    if dry_run:
        return _cfg_bool(cfg, "BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED", True)
    return _cfg_bool(cfg, "BIRD_RUNNER_MULTI_PARTIAL_LIVE_ENABLED", False)


def runner_giveback_emergency_enabled(*, dry_run: bool, cfg: Any = CFG) -> bool:
    if not _cfg_bool(cfg, "RUNNER_GIVEBACK_EMERGENCY_ENABLED", True):
        return False
    if dry_run:
        return _cfg_bool(cfg, "RUNNER_GIVEBACK_EMERGENCY_PAPER_ENABLED", True)
    return _cfg_bool(cfg, "RUNNER_GIVEBACK_EMERGENCY_LIVE_ENABLED", False)


def secured_fraction_at_peak(
    peak_pct: float,
    steps: tuple[BirdRunnerStep, ...] = DEFAULT_BIRD_RUNNER_STEPS,
    *,
    moonbag_fraction: float = DEFAULT_MOONBAG_FRACTION,
) -> float:
    secured = sum(step.fraction for step in steps if float(peak_pct) >= step.trigger_pct)
    return min(1.0 - max(0.0, float(moonbag_fraction)), max(0.0, secured))


def target_secured_fraction(pnl_pct: float, *, cfg: Any = CFG) -> float:
    return secured_fraction_at_peak(
        float(pnl_pct),
        configured_bird_runner_steps(cfg),
        moonbag_fraction=configured_moonbag_fraction(cfg),
    )


def pending_partial_plan(
    *,
    pnl_pct: float,
    entry_qty: int,
    remaining_qty: int,
    realized_qty: int = 0,
    cfg: Any = CFG,
    steps: tuple[BirdRunnerStep, ...] | None = None,
    moonbag_fraction: float | None = None,
    state: Any = None,
) -> dict[str, Any]:
    entry = max(0, int(entry_qty or 0))
    remaining = max(0, int(remaining_qty or 0))
    if entry <= 0:
        entry = remaining + max(0, int(realized_qty or 0))
    if entry <= 0 or remaining <= 0:
        return {
            "target_secured_fraction": 0.0,
            "already_secured_fraction": 0.0,
            "pending_entry_fraction": 0.0,
            "sell_fraction_of_remaining": 0.0,
            "triggered_steps": [],
        }

    already_qty = max(0, int(realized_qty or 0))
    already_fraction = max(0.0, min(1.0, already_qty / float(entry)))
    active_steps = configured_bird_runner_steps(cfg) if steps is None else steps
    active_moonbag = configured_moonbag_fraction(cfg) if moonbag_fraction is None else float(moonbag_fraction)
    converted_steps = tuple(
        runner_ladder.RunnerLadderStep(f"tp{idx}", step.trigger_pct, step.fraction)
        for idx, step in enumerate(active_steps, start=1)
    )
    plan = runner_ladder.plan_ladder_partials(
        pnl_pct=float(pnl_pct),
        entry_qty=entry,
        remaining_qty=remaining,
        realized_qty=already_qty,
        state=state,
        steps=converted_steps,
        moonbag_fraction=active_moonbag,
    )
    if already_fraction > float(plan.get("already_secured_fraction") or 0.0):
        plan["already_secured_fraction"] = round(already_fraction, 6)
    return plan


def runner_giveback_emergency_reason(
    *,
    peak_pct: float,
    pnl_pct: float,
    dry_run: bool,
    cfg: Any = CFG,
) -> str | None:
    if not runner_giveback_emergency_enabled(dry_run=dry_run, cfg=cfg):
        return None
    peak = float(peak_pct or 0.0)
    current = float(pnl_pct or 0.0)
    giveback = max(0.0, peak - current)
    thresholds = (
        (2000.0, _cfg_float(cfg, "RUNNER_GIVEBACK_PEAK_2000_MAX_GIVEBACK", 450.0)),
        (1000.0, _cfg_float(cfg, "RUNNER_GIVEBACK_PEAK_1000_MAX_GIVEBACK", 220.0)),
        (700.0, _cfg_float(cfg, "RUNNER_GIVEBACK_PEAK_700_MAX_GIVEBACK", 120.0)),
        (300.0, _cfg_float(cfg, "RUNNER_GIVEBACK_PEAK_300_MAX_GIVEBACK", 60.0)),
        (100.0, _cfg_float(cfg, "RUNNER_GIVEBACK_PEAK_100_MAX_GIVEBACK", 25.0)),
    )
    for min_peak, max_giveback in thresholds:
        if peak >= min_peak:
            if giveback >= max(0.0, float(max_giveback)):
                return "RUNNER_GIVEBACK_EMERGENCY"
            return None
    return None


def simulate_bird_runner_capture(peak_pct: float, final_pnl_pct: float, *, cfg: Any = CFG) -> dict[str, float]:
    steps = configured_bird_runner_steps(cfg)
    moonbag_floor = configured_moonbag_fraction(cfg)
    secured = secured_fraction_at_peak(peak_pct, steps, moonbag_fraction=moonbag_floor)
    moonbag = max(0.0, 1.0 - secured)
    realized = secured * max(0.0, float(peak_pct)) + moonbag * float(final_pnl_pct)
    peak = max(0.0, float(peak_pct))
    giveback = max(0.0, peak - float(final_pnl_pct))
    capture_ratio = realized / peak if peak > 0 else 0.0
    return {
        "peak_pct": float(peak_pct),
        "final_pnl_pct": float(final_pnl_pct),
        "secured_fraction": round(secured, 4),
        "moonbag_fraction": round(moonbag, 4),
        "simulated_realized_pnl_pct": round(realized, 4),
        "capture_ratio": round(capture_ratio, 4),
        "giveback_pct": round(giveback, 4),
        "partials_triggered": float(sum(1 for step in steps if float(peak_pct) >= step.trigger_pct)),
        "emergency_sell": 1.0
        if runner_giveback_emergency_reason(peak_pct=float(peak_pct), pnl_pct=float(final_pnl_pct), dry_run=True, cfg=cfg)
        else 0.0,
    }


__all__ = [
    "BirdRunnerStep",
    "DEFAULT_BIRD_RUNNER_STEPS",
    "DEFAULT_MOONBAG_FRACTION",
    "bird_runner_multi_partial_enabled",
    "configured_bird_runner_steps",
    "configured_moonbag_fraction",
    "pending_partial_plan",
    "runner_giveback_emergency_enabled",
    "runner_giveback_emergency_reason",
    "secured_fraction_at_peak",
    "simulate_bird_runner_capture",
    "target_secured_fraction",
]
