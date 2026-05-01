from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BirdRunnerStep:
    trigger_pct: float
    fraction: float


DEFAULT_BIRD_RUNNER_STEPS = (
    BirdRunnerStep(25.0, 0.25),
    BirdRunnerStep(50.0, 0.25),
    BirdRunnerStep(100.0, 0.20),
    BirdRunnerStep(300.0, 0.15),
)
DEFAULT_MOONBAG_FRACTION = 0.15


def secured_fraction_at_peak(peak_pct: float, steps: tuple[BirdRunnerStep, ...] = DEFAULT_BIRD_RUNNER_STEPS) -> float:
    secured = sum(step.fraction for step in steps if float(peak_pct) >= step.trigger_pct)
    return min(1.0 - DEFAULT_MOONBAG_FRACTION, max(0.0, secured))


def simulate_bird_runner_capture(peak_pct: float, final_pnl_pct: float) -> dict[str, float]:
    secured = secured_fraction_at_peak(peak_pct)
    moonbag = max(0.0, 1.0 - secured)
    realized = secured * max(0.0, float(peak_pct)) + moonbag * float(final_pnl_pct)
    return {
        "peak_pct": float(peak_pct),
        "final_pnl_pct": float(final_pnl_pct),
        "secured_fraction": round(secured, 4),
        "moonbag_fraction": round(moonbag, 4),
        "simulated_realized_pnl_pct": round(realized, 4),
    }


__all__ = ["BirdRunnerStep", "DEFAULT_BIRD_RUNNER_STEPS", "DEFAULT_MOONBAG_FRACTION", "secured_fraction_at_peak", "simulate_bird_runner_capture"]
