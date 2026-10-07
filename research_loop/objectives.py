from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import math
from typing import Any

OBJECTIVE_CONFIG_PATH = Path(__file__).resolve().with_name("objectives.yaml")
METRIC_SCOPE_CURRENT_RUN = "current_run"
METRIC_SCOPE_HISTORICAL = "historical"
METRIC_SCOPE_COMBINED = "combined"
METRIC_SCOPES = {METRIC_SCOPE_CURRENT_RUN, METRIC_SCOPE_HISTORICAL, METRIC_SCOPE_COMBINED}
LOWER_IS_BETTER_METRICS = {
    "adverse_tick_count",
    "api_429_count",
    "birdeye_429_count",
    "gecko_429_count",
    "giveback_pct",
    "idle_no_buy_hours",
    "jupiter_rate_limit_count",
    "liquidity_crush_count",
    "max_drawdown_proxy",
    "missed_peak100_count",
    "missed_peak500_count",
    "missed_peak1000_count",
    "no_pump_exit_count",
    "overtrading_count",
    "provider_degraded_minutes",
    "severe_loss_count",
    "stop_loss_count",
}


@dataclass(frozen=True)
class ObjectiveResult:
    score: float
    hard_gate_passed: bool
    metric_deltas: dict[str, float]
    rejection_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    accepted: bool = False
    optimized_metric: str | None = None
    metric_scope: str = METRIC_SCOPE_COMBINED
    optimized_metric_delta: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "hard_gate_passed": self.hard_gate_passed,
            "metric_deltas": dict(self.metric_deltas),
            "rejection_reasons": list(self.rejection_reasons),
            "warnings": list(self.warnings),
            "accepted": self.accepted,
            "optimized_metric": self.optimized_metric,
            "metric_scope": self.metric_scope,
            "optimized_metric_delta": self.optimized_metric_delta,
        }


def _parse_scalar(value: str) -> Any:
    raw = value.strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if "." in raw:
            return float(raw.replace("_", ""))
        return int(raw.replace("_", ""))
    except ValueError:
        return raw


def _load_simple_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        text = raw_line.strip()
        if indent == 0:
            if ":" not in text:
                continue
            key, raw_value = text.split(":", 1)
            key = key.strip()
            raw_value = raw_value.strip()
            if raw_value:
                data[key] = _parse_scalar(raw_value)
                current_key = None
            else:
                data[key] = {}
                current_key = key
            continue
        if current_key is None or ":" not in text:
            continue
        if not isinstance(data.get(current_key), dict):
            data[current_key] = {}
        key, raw_value = text.split(":", 1)
        data[current_key][key.strip()] = _parse_scalar(raw_value)
    return data


def load_objective_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path is not None else OBJECTIVE_CONFIG_PATH
    try:
        import yaml  # type: ignore

        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return payload or {}
    except Exception:
        return _load_simple_yaml(config_path)


def _metric_value(metrics: dict[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _metric_delta(baseline_metrics: dict[str, Any], candidate_metrics: dict[str, Any], key: str) -> float | None:
    baseline = _metric_value(baseline_metrics, key)
    candidate = _metric_value(candidate_metrics, key)
    if baseline is None or candidate is None:
        return None
    delta = candidate - baseline
    return delta if math.isfinite(delta) else None


def _weighted_metric_key(weight_key: str) -> str:
    return weight_key.removesuffix("_weight")


def _penalty_metric_key(penalty_key: str, metric_deltas: dict[str, float]) -> str:
    base = penalty_key.removesuffix("_penalty")
    aliases = {
        "severe_loss": "severe_loss_count",
        "liquidity_crush": "liquidity_crush_count",
        "adverse_tick": "adverse_tick_count",
        "no_pump_exit": "no_pump_exit_count",
        "max_drawdown": "max_drawdown_proxy",
        "api_429": "api_429_count",
        "provider_degraded": "provider_degraded_minutes",
        "overtrading": "overtrading_count",
        "idle_no_buy": "idle_no_buy_hours",
    }
    candidates = [base, aliases.get(base, ""), f"{base}_count"]
    for key in candidates:
        if key and key in metric_deltas:
            return key
    return aliases.get(base, base)


def _all_numeric_deltas(baseline_metrics: dict[str, Any], candidate_metrics: dict[str, Any]) -> dict[str, float]:
    deltas: dict[str, float] = {}
    for key in sorted(set(baseline_metrics) | set(candidate_metrics)):
        delta = _metric_delta(baseline_metrics, candidate_metrics, key)
        if delta is not None:
            deltas[key] = delta
    if "api_429_count" not in deltas:
        api_429_sources = ("gecko_429_count", "birdeye_429_count", "jupiter_rate_limit_count")
        baseline_parts = [_metric_value(baseline_metrics, key) for key in api_429_sources]
        candidate_parts = [_metric_value(candidate_metrics, key) for key in api_429_sources]
        if all(value is not None for value in baseline_parts + candidate_parts):
            delta = sum(candidate_parts) - sum(baseline_parts)
            if math.isfinite(delta):
                deltas["api_429_count"] = delta
    return deltas


def _gate_metric_key(gate_key: str, metric_deltas: dict[str, float]) -> str:
    metric_key = gate_key.removesuffix("_delta_min").removesuffix("_delta_max")
    if metric_key == "max_drawdown" and "max_drawdown_proxy" in metric_deltas:
        return "max_drawdown_proxy"
    return metric_key


def metric_delta_is_worse(metric: str, delta: float) -> bool:
    if metric in LOWER_IS_BETTER_METRICS:
        return delta > 0.0
    return delta < 0.0


def calculate_objective_score(
    baseline_metrics: dict[str, Any],
    candidate_metrics: dict[str, Any],
    objective_config: dict[str, Any] | None = None,
    *,
    optimized_metric: str | None = None,
    metric_scope: str = METRIC_SCOPE_COMBINED,
) -> ObjectiveResult:
    config = objective_config or load_objective_config()
    objective = config.get("objective") or {}
    penalties = config.get("penalties") or {}
    hard_gates = config.get("hard_gates") or {}

    metric_deltas = _all_numeric_deltas(baseline_metrics, candidate_metrics)
    warnings: list[str] = []
    rejection_reasons: list[str] = []
    score = 0.0
    selected_metric = str(optimized_metric or "").strip() or None
    selected_scope = str(metric_scope or METRIC_SCOPE_COMBINED).strip() or METRIC_SCOPE_COMBINED
    optimized_metric_delta = metric_deltas.get(selected_metric) if selected_metric else None
    if selected_metric and optimized_metric_delta is None:
        warnings.append(f"missing_optimized_metric:{selected_metric}")

    for weight_key, raw_weight in objective.items():
        metric_key = _weighted_metric_key(str(weight_key))
        delta = metric_deltas.get(metric_key)
        if delta is None:
            warnings.append(f"missing_objective_metric:{metric_key}")
            continue
        weight = _metric_value(objective, weight_key)
        if weight is None:
            rejection_reasons.append(f"invalid_objective_weight:{weight_key}")
            continue
        score += delta * weight

    for penalty_key, raw_penalty in penalties.items():
        metric_key = _penalty_metric_key(str(penalty_key), metric_deltas)
        delta = metric_deltas.get(metric_key)
        if delta is None:
            warnings.append(f"missing_penalty_metric:{metric_key}")
            continue
        penalty = _metric_value(penalties, penalty_key)
        if penalty is None or penalty < 0:
            rejection_reasons.append(f"invalid_objective_penalty:{penalty_key}")
            continue
        if delta > 0:
            score -= delta * penalty

    for gate_key, raw_limit in hard_gates.items():
        if gate_key == "live_allowed_default":
            continue
        gate_name = str(gate_key)
        metric_key = _gate_metric_key(gate_name, metric_deltas)
        delta = metric_deltas.get(metric_key)
        if delta is None:
            warnings.append(f"missing_hard_gate_metric:{metric_key}")
            rejection_reasons.append(f"missing_hard_gate_metric:{metric_key}")
            continue
        limit = _metric_value(hard_gates, gate_key)
        if limit is None:
            rejection_reasons.append(f"invalid_hard_gate_limit:{gate_name}")
            continue
        if not gate_name.endswith(("_delta_min", "_delta_max")):
            rejection_reasons.append(f"invalid_hard_gate_name:{gate_name}")
            continue
        if gate_name.endswith("_delta_min") and delta < limit:
            rejection_reasons.append(f"hard_gate:{metric_key}_delta<{limit}")
        elif gate_name.endswith("_delta_max") and delta > limit:
            rejection_reasons.append(f"hard_gate:{metric_key}_delta>{limit}")

    if not math.isfinite(score):
        rejection_reasons.append("nonfinite_objective_score")
        score = 0.0
    hard_gate_passed = not rejection_reasons
    if hard_gate_passed and score <= 0:
        rejection_reasons.append("objective_score_not_positive")

    return ObjectiveResult(
        score=score,
        hard_gate_passed=hard_gate_passed,
        metric_deltas=metric_deltas,
        rejection_reasons=rejection_reasons,
        warnings=warnings,
        accepted=hard_gate_passed and score > 0,
        optimized_metric=selected_metric,
        metric_scope=selected_scope,
        optimized_metric_delta=optimized_metric_delta,
    )


__all__ = [
    "LOWER_IS_BETTER_METRICS",
    "METRIC_SCOPE_COMBINED",
    "METRIC_SCOPE_CURRENT_RUN",
    "METRIC_SCOPE_HISTORICAL",
    "METRIC_SCOPES",
    "ObjectiveResult",
    "calculate_objective_score",
    "load_objective_config",
    "metric_delta_is_worse",
]
