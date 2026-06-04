from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OBJECTIVE_CONFIG_PATH = Path(__file__).resolve().with_name("objectives.yaml")


@dataclass(frozen=True)
class ObjectiveResult:
    score: float
    hard_gate_passed: bool
    metric_deltas: dict[str, float]
    rejection_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    accepted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "hard_gate_passed": self.hard_gate_passed,
            "metric_deltas": dict(self.metric_deltas),
            "rejection_reasons": list(self.rejection_reasons),
            "warnings": list(self.warnings),
            "accepted": self.accepted,
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
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metric_delta(baseline_metrics: dict[str, Any], candidate_metrics: dict[str, Any], key: str) -> float | None:
    baseline = _metric_value(baseline_metrics, key)
    candidate = _metric_value(candidate_metrics, key)
    if baseline is None or candidate is None:
        return None
    return candidate - baseline


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
        baseline_api = sum(_metric_value(baseline_metrics, key) or 0.0 for key in api_429_sources)
        candidate_api = sum(_metric_value(candidate_metrics, key) or 0.0 for key in api_429_sources)
        if baseline_api or candidate_api:
            deltas["api_429_count"] = candidate_api - baseline_api
    return deltas


def _gate_metric_key(gate_key: str, metric_deltas: dict[str, float]) -> str:
    metric_key = gate_key.removesuffix("_delta_min").removesuffix("_delta_max")
    if metric_key == "max_drawdown" and "max_drawdown_proxy" in metric_deltas:
        return "max_drawdown_proxy"
    return metric_key


def calculate_objective_score(
    baseline_metrics: dict[str, Any],
    candidate_metrics: dict[str, Any],
    objective_config: dict[str, Any] | None = None,
) -> ObjectiveResult:
    config = objective_config or load_objective_config()
    objective = config.get("objective") or {}
    penalties = config.get("penalties") or {}
    hard_gates = config.get("hard_gates") or {}

    metric_deltas = _all_numeric_deltas(baseline_metrics, candidate_metrics)
    warnings: list[str] = []
    rejection_reasons: list[str] = []
    score = 0.0

    for weight_key, raw_weight in objective.items():
        metric_key = _weighted_metric_key(str(weight_key))
        delta = metric_deltas.get(metric_key)
        if delta is None:
            warnings.append(f"missing_objective_metric:{metric_key}")
            continue
        score += delta * float(raw_weight)

    for penalty_key, raw_penalty in penalties.items():
        metric_key = _penalty_metric_key(str(penalty_key), metric_deltas)
        delta = metric_deltas.get(metric_key)
        if delta is None:
            warnings.append(f"missing_penalty_metric:{metric_key}")
            continue
        if delta > 0:
            score -= delta * float(raw_penalty)

    for gate_key, raw_limit in hard_gates.items():
        if gate_key == "live_allowed_default":
            continue
        gate_name = str(gate_key)
        metric_key = _gate_metric_key(gate_name, metric_deltas)
        delta = metric_deltas.get(metric_key)
        if delta is None:
            warnings.append(f"missing_hard_gate_metric:{metric_key}")
            continue
        limit = float(raw_limit)
        if gate_name.endswith("_delta_min") and delta < limit:
            rejection_reasons.append(f"hard_gate:{metric_key}_delta<{limit}")
        elif gate_name.endswith("_delta_max") and delta > limit:
            rejection_reasons.append(f"hard_gate:{metric_key}_delta>{limit}")

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
    )


__all__ = ["ObjectiveResult", "calculate_objective_score", "load_objective_config"]
