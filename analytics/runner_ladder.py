from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.config import CFG, PROJECT_ROOT


LADDER_VERSION = "bird_runner_ladder_v2"


@dataclass(frozen=True)
class RunnerLadderStep:
    step_id: str
    trigger_pct: float
    fraction: float


DEFAULT_RUNNER_LADDER_STEPS = (
    RunnerLadderStep("tp1", 25.0, 0.25),
    RunnerLadderStep("tp2", 50.0, 0.25),
    RunnerLadderStep("tp3", 100.0, 0.20),
    RunnerLadderStep("tp4", 300.0, 0.15),
    RunnerLadderStep("tp5", 700.0, 0.07),
    RunnerLadderStep("tp6", 1000.0, 0.05),
)
DEFAULT_MOONBAG_FRACTION = 0.03


def _cfg_float(cfg: Any, name: str, default: float) -> float:
    try:
        value = getattr(cfg, name, default)
        if value is None:
            return float(default)
        out = float(value)
        if out != out or out in (float("inf"), float("-inf")):
            return float(default)
        return out
    except Exception:
        return float(default)


def _cfg_bool(cfg: Any, name: str, default: bool) -> bool:
    value = getattr(cfg, name, default)
    if isinstance(value, bool):
        return value
    raw = str(value if value is not None else default).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def configured_runner_ladder_steps(cfg: Any = CFG) -> tuple[RunnerLadderStep, ...]:
    defaults = DEFAULT_RUNNER_LADDER_STEPS
    steps: list[RunnerLadderStep] = []
    for idx, default in enumerate(defaults, start=1):
        trigger = _cfg_float(cfg, f"BIRD_TP{idx}_PCT", default.trigger_pct)
        fraction = _cfg_float(cfg, f"BIRD_TP{idx}_FRACTION", default.fraction)
        if trigger > 0.0 and 0.0 < fraction < 1.0:
            steps.append(RunnerLadderStep(f"tp{idx}", trigger, fraction))
    return tuple(sorted(steps, key=lambda step: step.trigger_pct))


def configured_moonbag_fraction(cfg: Any = CFG) -> float:
    return max(0.0, min(0.95, _cfg_float(cfg, "BIRD_MOONBAG_FRACTION", DEFAULT_MOONBAG_FRACTION)))


def initial_ladder_state() -> dict[str, Any]:
    return {
        "version": LADDER_VERSION,
        "executed_steps": [],
        "sold_fraction": 0.0,
        "last_updated_at_utc": None,
    }


def _state_from_any(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        state = dict(value)
    elif isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
            state = dict(loaded) if isinstance(loaded, dict) else {}
        except Exception:
            state = {}
    else:
        state = {}
    out = initial_ladder_state()
    out.update({key: value for key, value in state.items() if key in out or key in {"executions"}})
    executed = out.get("executed_steps")
    if not isinstance(executed, list):
        executed = []
    out["executed_steps"] = [str(item) for item in executed]
    try:
        out["sold_fraction"] = max(0.0, min(1.0, float(out.get("sold_fraction") or 0.0)))
    except Exception:
        out["sold_fraction"] = 0.0
    return out


def state_from_subject(subject: Any) -> dict[str, Any]:
    raw = subject.get("partial_ladder_state") if isinstance(subject, dict) else getattr(subject, "partial_ladder_state", None)
    return _state_from_any(raw)


def encode_ladder_state(state: dict[str, Any]) -> str:
    return json.dumps(_state_from_any(state), sort_keys=True, separators=(",", ":"))


def _infer_executed_steps(
    *,
    realized_fraction: float,
    steps: tuple[RunnerLadderStep, ...],
    moonbag_fraction: float,
) -> list[str]:
    cap = max(0.0, 1.0 - max(0.0, moonbag_fraction))
    total = 0.0
    executed: list[str] = []
    for step in steps:
        next_total = min(cap, total + max(0.0, float(step.fraction)))
        if realized_fraction + 1e-9 >= next_total:
            executed.append(step.step_id)
            total = next_total
        else:
            break
    return executed


def plan_ladder_partials(
    *,
    pnl_pct: float,
    entry_qty: int,
    remaining_qty: int,
    realized_qty: int = 0,
    state: Any = None,
    steps: tuple[RunnerLadderStep, ...] | None = None,
    moonbag_fraction: float | None = None,
) -> dict[str, Any]:
    entry = max(0, int(entry_qty or 0))
    remaining = max(0, int(remaining_qty or 0))
    realized = max(0, int(realized_qty or 0))
    if entry <= 0:
        entry = remaining + realized
    active_steps = configured_runner_ladder_steps(CFG) if steps is None else steps
    active_moonbag = configured_moonbag_fraction(CFG) if moonbag_fraction is None else float(moonbag_fraction)
    cap = max(0.0, min(1.0, 1.0 - max(0.0, active_moonbag)))
    normalized_state = _state_from_any(state)
    realized_fraction = max(0.0, min(1.0, realized / float(entry))) if entry > 0 else 0.0
    executed_steps = list(dict.fromkeys(normalized_state.get("executed_steps") or []))
    inferred = _infer_executed_steps(
        realized_fraction=realized_fraction,
        steps=active_steps,
        moonbag_fraction=active_moonbag,
    )
    for step_id in inferred:
        if step_id not in executed_steps:
            executed_steps.append(step_id)

    if entry <= 0 or remaining <= 0:
        normalized_state["executed_steps"] = executed_steps
        normalized_state["sold_fraction"] = max(float(normalized_state.get("sold_fraction") or 0.0), realized_fraction)
        return {
            "enabled": True,
            "version": LADDER_VERSION,
            "target_secured_fraction": 0.0,
            "already_secured_fraction": round(realized_fraction, 6),
            "pending_entry_fraction": 0.0,
            "sell_fraction_of_remaining": 0.0,
            "sell_qty": 0,
            "triggered_steps": [],
            "pending_steps": [],
            "pending_step_count": 0,
            "state": normalized_state,
            "next_state": normalized_state,
        }

    triggered = [step for step in active_steps if float(pnl_pct) >= step.trigger_pct]
    pending_steps = [step for step in triggered if step.step_id not in set(executed_steps)]
    target_fraction = min(cap, sum(max(0.0, float(step.fraction)) for step in triggered))
    already_secured = max(realized_fraction, float(normalized_state.get("sold_fraction") or 0.0))
    pending_entry_fraction = max(0.0, target_fraction - already_secured)
    sell_qty = min(remaining, int(round(entry * pending_entry_fraction)))
    sell_fraction = max(0.0, min(1.0, sell_qty / float(remaining))) if remaining > 0 else 0.0

    next_state = dict(normalized_state)
    next_executed = list(executed_steps)
    if sell_qty > 0:
        for step in pending_steps:
            if step.step_id not in next_executed:
                next_executed.append(step.step_id)
        next_state["executed_steps"] = next_executed
        next_state["sold_fraction"] = round(max(already_secured, target_fraction), 6)
        next_state["last_updated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    else:
        next_state["executed_steps"] = next_executed
        next_state["sold_fraction"] = round(already_secured, 6)

    return {
        "enabled": True,
        "version": LADDER_VERSION,
        "target_secured_fraction": round(target_fraction, 6),
        "already_secured_fraction": round(already_secured, 6),
        "pending_entry_fraction": round(pending_entry_fraction, 6),
        "sell_fraction_of_remaining": round(sell_fraction, 6),
        "sell_qty": int(sell_qty),
        "triggered_steps": [
            {"step_id": step.step_id, "trigger_pct": step.trigger_pct, "fraction": step.fraction}
            for step in triggered
        ],
        "pending_steps": [
            {"step_id": step.step_id, "trigger_pct": step.trigger_pct, "fraction": step.fraction}
            for step in pending_steps
        ],
        "pending_step_count": len(pending_steps) if sell_qty > 0 else 0,
        "state": normalized_state,
        "next_state": next_state,
    }


def dynamic_runner_floor_pct(peak_pct: float, *, cfg: Any = CFG) -> float | None:
    if not _cfg_bool(cfg, "DYNAMIC_RUNNER_FLOOR_ENABLED", True):
        return None
    peak = float(peak_pct or 0.0)
    thresholds = (
        (2000.0, _cfg_float(cfg, "RUNNER_FLOOR_PEAK_2000", 800.0)),
        (1000.0, _cfg_float(cfg, "RUNNER_FLOOR_PEAK_1000", 500.0)),
        (700.0, _cfg_float(cfg, "RUNNER_FLOOR_PEAK_700", 350.0)),
        (300.0, _cfg_float(cfg, "RUNNER_FLOOR_PEAK_300", 150.0)),
        (100.0, _cfg_float(cfg, "RUNNER_FLOOR_PEAK_100", 50.0)),
    )
    for min_peak, floor in thresholds:
        if peak >= min_peak:
            return max(0.0, float(floor))
    return None


def dynamic_runner_floor_reason(*, peak_pct: float, pnl_pct: float, cfg: Any = CFG) -> str | None:
    floor = dynamic_runner_floor_pct(peak_pct, cfg=cfg)
    if floor is None:
        return None
    if float(pnl_pct) <= floor:
        return "DYNAMIC_RUNNER_FLOOR"
    return None


def state_file(root: Path | None = None) -> Path:
    return (root or PROJECT_ROOT) / "data" / "metrics" / "runner_ladder_state.json"


def read_state_store(root: Path | None = None) -> dict[str, Any]:
    path = state_file(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"positions": {}}
    return payload if isinstance(payload, dict) else {"positions": {}}


def write_position_state(position_key: str, state: dict[str, Any], *, root: Path | None = None) -> None:
    if not position_key:
        return
    store = read_state_store(root)
    positions = store.get("positions")
    if not isinstance(positions, dict):
        positions = {}
    positions[str(position_key)] = _state_from_any(state)
    store["positions"] = positions
    store["updated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    path = state_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, sort_keys=True, default=str), encoding="utf-8")


__all__ = [
    "DEFAULT_MOONBAG_FRACTION",
    "DEFAULT_RUNNER_LADDER_STEPS",
    "LADDER_VERSION",
    "RunnerLadderStep",
    "configured_moonbag_fraction",
    "configured_runner_ladder_steps",
    "dynamic_runner_floor_pct",
    "dynamic_runner_floor_reason",
    "encode_ladder_state",
    "initial_ladder_state",
    "plan_ladder_partials",
    "read_state_store",
    "state_from_subject",
    "write_position_state",
]
