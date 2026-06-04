from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Any, Iterable

from research_loop.safety import load_safety_config, validate_candidate_safety

SEARCH_SPACE_CONFIG_PATH = Path(__file__).resolve().with_name("search_space.yaml")

SPACE_ALIASES = {
    "runner_exit": "runner_ladder",
    "shadow_followup": "shadow_followup_micro",
}

SPACE_METADATA: dict[str, dict[str, Any]] = {
    "rank_canary": {
        "target_lanes": ["pump_early_sniper_research", "research_rank_canary"],
        "hypothesis": "Improve rank canary quality by tuning priority thresholds and paper size.",
        "expected_effect": {
            "increase_pnl": True,
            "increase_win_rate": True,
            "increase_moonshot_capture": False,
            "reduce_severe_losses": True,
        },
        "risk_notes": ["paper only", "rank canary caps enforced"],
    },
    "shadow_followup_micro": {
        "target_lanes": ["shadow_followup_micro"],
        "hypothesis": "Improve shadow follow-up conversion by tuning trigger and micro sizing.",
        "expected_effect": {
            "increase_pnl": True,
            "increase_win_rate": True,
            "increase_moonshot_capture": True,
            "reduce_severe_losses": True,
        },
        "risk_notes": ["paper only", "micro amount capped"],
    },
    "moonshot_micro": {
        "target_lanes": ["pump_early_moonshot_micro_lottery"],
        "hypothesis": "Improve moonshot tail capture with confirmed micro entries.",
        "expected_effect": {
            "increase_pnl": True,
            "increase_win_rate": False,
            "increase_moonshot_capture": True,
            "reduce_severe_losses": True,
        },
        "risk_notes": ["paper only", "moonshot amount capped"],
    },
    "runner_ladder": {
        "target_lanes": ["runner_exit", "pump_early_pumpswap_profit"],
        "hypothesis": "Improve runner capture by tuning partial take-profit ladder and floors.",
        "expected_effect": {
            "increase_pnl": True,
            "increase_win_rate": False,
            "increase_moonshot_capture": True,
            "reduce_severe_losses": False,
        },
        "risk_notes": ["paper only", "exit parameters only"],
    },
    "sniper_momentum": {
        "target_lanes": ["pump_early_sniper_research"],
        "hypothesis": "Improve sniper research entry quality by tuning momentum thresholds.",
        "expected_effect": {
            "increase_pnl": True,
            "increase_win_rate": True,
            "increase_moonshot_capture": False,
            "reduce_severe_losses": True,
        },
        "risk_notes": ["paper only", "no API cadence changes"],
    },
    "paper_exploration": {
        "target_lanes": ["paper_exploration"],
        "hypothesis": "Reduce idle periods with capped paper exploration while preserving safety.",
        "expected_effect": {
            "increase_pnl": True,
            "increase_win_rate": False,
            "increase_moonshot_capture": True,
            "reduce_severe_losses": False,
        },
        "risk_notes": ["paper only", "idle exploration cap enforced"],
    },
}

LIVE_OR_SECRET_MARKERS = (
    "LIVE",
    "PRIVATE",
    "WALLET",
    "SECRET",
    "PASSWORD",
    "RPC_URL",
    "API_KEY",
)


@dataclass(frozen=True)
class SearchSpace:
    name: str
    parameters: dict[str, list[Any]]
    target_lanes: list[str] = field(default_factory=list)
    hypothesis: str = ""
    expected_effect: dict[str, Any] = field(default_factory=dict)
    risk_notes: list[str] = field(default_factory=list)

    def keys(self) -> list[str]:
        return list(self.parameters)

    def total_combinations(self) -> int:
        total = 1
        for values in self.parameters.values():
            total *= len(values)
        return total


@dataclass(frozen=True)
class SearchSpaceValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if "." in value:
            return float(value.replace("_", ""))
        return int(value.replace("_", ""))
    except ValueError:
        return value


def _parse_inline_list(raw: str) -> list[Any]:
    text = raw.strip()
    if not text.startswith("[") or not text.endswith("]"):
        return [_parse_scalar(text)]
    body = text[1:-1].strip()
    if not body:
        return []
    return [_parse_scalar(part) for part in body.split(",")]


def _load_simple_search_space_yaml(path: Path) -> dict[str, dict[str, list[Any]]]:
    spaces: dict[str, dict[str, list[Any]]] = {}
    current: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        text = raw_line.strip()
        if indent == 0:
            if not text.endswith(":"):
                continue
            current = text[:-1].strip()
            spaces[current] = {}
            continue
        if current is None or ":" not in text:
            continue
        key, raw_values = text.split(":", 1)
        values = _parse_inline_list(raw_values)
        spaces[current][key.strip()] = values
    return spaces


def _load_raw_search_spaces(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path is not None else SEARCH_SPACE_CONFIG_PATH
    try:
        import yaml  # type: ignore

        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return payload or {}
    except Exception:
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return _load_simple_search_space_yaml(config_path)


def resolve_space_name(name: str) -> str:
    normalized = str(name).strip()
    return SPACE_ALIASES.get(normalized, normalized)


def _space_from_payload(name: str, payload: dict[str, Any]) -> SearchSpace:
    resolved_name = resolve_space_name(name)
    parameters = {str(key): list(value) for key, value in payload.items() if isinstance(value, list)}
    metadata = SPACE_METADATA.get(resolved_name, {})
    return SearchSpace(
        name=resolved_name,
        parameters=parameters,
        target_lanes=list(metadata.get("target_lanes") or [resolved_name]),
        hypothesis=str(metadata.get("hypothesis") or f"Optimize {resolved_name}."),
        expected_effect=dict(metadata.get("expected_effect") or {"increase_pnl": True}),
        risk_notes=list(metadata.get("risk_notes") or ["paper only"]),
    )


def load_search_spaces(path: str | Path | None = None) -> dict[str, SearchSpace]:
    raw = _load_raw_search_spaces(path)
    spaces: dict[str, SearchSpace] = {}
    for name, payload in raw.items():
        if isinstance(payload, dict):
            space = _space_from_payload(str(name), payload)
            spaces[space.name] = space
    return spaces


def get_search_space(name: str, path: str | Path | None = None) -> SearchSpace:
    resolved_name = resolve_space_name(name)
    spaces = load_search_spaces(path)
    if resolved_name not in spaces:
        raise KeyError(f"unknown_search_space:{name}")
    return spaces[resolved_name]


def _live_or_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in LIVE_OR_SECRET_MARKERS)


def validate_search_space(space: SearchSpace) -> SearchSpaceValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    config = load_safety_config()
    forbidden = {str(key).upper() for key in config.get("forbidden_env_keys", [])}
    protected_api = {str(key).upper() for key in config.get("api_budget_protected_keys", [])}

    if not space.parameters:
        errors.append(f"empty_space:{space.name}")

    for key, values in space.parameters.items():
        upper = key.upper()
        if not values:
            errors.append(f"empty_values:{space.name}:{key}")
            continue
        if upper in forbidden or _live_or_secret_key(upper):
            errors.append(f"forbidden_key:{space.name}:{key}")
        if upper in protected_api:
            errors.append(f"api_budget_protected_key:{space.name}:{key}")
        sample_policy = {
            "live_allowed": False,
            "changes": {key: values[0]},
        }
        safety = validate_candidate_safety(sample_policy)
        if not safety.ok:
            errors.extend(f"safety:{space.name}:{error}" for error in safety.errors)

    return SearchSpaceValidationResult(ok=not errors, errors=errors, warnings=warnings)


def validate_search_spaces(spaces: dict[str, SearchSpace] | None = None) -> SearchSpaceValidationResult:
    spaces = spaces or load_search_spaces()
    errors: list[str] = []
    warnings: list[str] = []
    for space in spaces.values():
        result = validate_search_space(space)
        errors.extend(result.errors)
        warnings.extend(result.warnings)
    return SearchSpaceValidationResult(ok=not errors, errors=errors, warnings=warnings)


def iter_grid(space: SearchSpace) -> Iterable[dict[str, Any]]:
    keys = list(space.parameters)
    for values in product(*(space.parameters[key] for key in keys)):
        yield dict(zip(keys, values))


__all__ = [
    "SEARCH_SPACE_CONFIG_PATH",
    "SPACE_ALIASES",
    "SPACE_METADATA",
    "SearchSpace",
    "SearchSpaceValidationResult",
    "get_search_space",
    "iter_grid",
    "load_search_spaces",
    "resolve_space_name",
    "validate_search_space",
    "validate_search_spaces",
]
