from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SAFETY_CONFIG_PATH = Path(__file__).resolve().with_name("safety.yaml")

TRUTHY_VALUES = {"1", "true", "yes", "y", "on"}
AMOUNT_KEY_MARKERS = ("AMOUNT_SOL", "SIZE_SOL", "MAX_SIZE_SOL", "MAX_SOL")
REQUIRED_SAFE_FLAG_VALUES = {
    "STRATEGY_OPTIMIZATION_LOCK": True,
    "AUTO_PROMOTE_LIVE": False,
    "MODEL_AUTO_PROMOTE": False,
    "ML_AUTO_PROMOTE_LANES": False,
    "LLM_TRADING_ENABLED": False,
    "AUTORESEARCH_LLM_ENABLED": False,
    "AUTORESEARCH_LLM_CAN_EDIT_CODE": False,
    "AUTORESEARCH_LLM_CAN_TOUCH_LIVE": False,
    "AUTORESEARCH_LLM_CAN_CALL_APIS": False,
    "SOCIALS_HOT_PATH_BLOCKING": False,
    "GREEN_SNIPER_REQUIRE_SOCIALS": False,
}


@dataclass(frozen=True)
class SafetyResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    forbidden_changes: list[str] = field(default_factory=list)
    api_budget_risk: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "forbidden_changes": list(self.forbidden_changes),
            "api_budget_risk": self.api_budget_risk,
        }


def _parse_scalar(value: str) -> Any:
    raw = value.strip()
    if not raw:
        return ""
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
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
        if current_key is None:
            continue
        if text.startswith("- "):
            if not isinstance(data.get(current_key), list):
                data[current_key] = []
            data[current_key].append(_parse_scalar(text[2:]))
            continue
        if ":" in text:
            if not isinstance(data.get(current_key), dict):
                data[current_key] = {}
            key, raw_value = text.split(":", 1)
            data[current_key][key.strip()] = _parse_scalar(raw_value)
    return data


def load_safety_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path is not None else SAFETY_CONFIG_PATH
    try:
        import yaml  # type: ignore

        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return payload or {}
    except Exception:
        return _load_simple_yaml(config_path)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in TRUTHY_VALUES


def _float_value(value: Any) -> float | None:
    try:
        return float(str(value).strip().replace("_", ""))
    except (TypeError, ValueError):
        return None


def _candidate_changes(candidate_policy: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    changes = candidate_policy.get("changes", {})
    if changes is None:
        return {}
    if not isinstance(changes, dict):
        errors.append("changes_must_be_object")
        return {}
    return changes


def _amount_cap_for_key(key: str, max_amounts: dict[str, Any]) -> float | None:
    if not any(marker in key for marker in AMOUNT_KEY_MARKERS):
        return None
    if "MOONSHOT_MICRO" in key:
        return _float_value(max_amounts.get("moonshot_micro_max_sol"))
    if "RESEARCH_RANK_CANARY" in key:
        return _float_value(max_amounts.get("rank_canary_max_sol"))
    if "PAPER_EXPLORATION" in key or "PAPER_IDLE" in key:
        return _float_value(max_amounts.get("paper_exploration_max_sol"))
    experimental_markers = (
        "EXPERIMENTAL",
        "SHADOW_FOLLOWUP",
        "BIRTH_PROBE",
        "LATE_MOMENTUM",
        "SNIPER_RESEARCH",
    )
    if any(marker in key for marker in experimental_markers):
        return _float_value(max_amounts.get("experimental_lane_max_sol"))
    return None


def validate_candidate_safety(candidate_policy: dict[str, Any]) -> SafetyResult:
    config = load_safety_config()
    errors: list[str] = []
    warnings: list[str] = []
    forbidden_changes: list[str] = []
    api_budget_risk = False

    if not isinstance(candidate_policy, dict):
        return SafetyResult(
            ok=False,
            errors=["candidate_policy_must_be_object"],
            warnings=[],
            forbidden_changes=[],
            api_budget_risk=False,
        )

    if candidate_policy.get("live_allowed") is True:
        errors.append("live_allowed_must_be_false")
        forbidden_changes.append("live_allowed")

    changes = _candidate_changes(candidate_policy, errors)
    forbidden_env_keys = {str(key).upper() for key in config.get("forbidden_env_keys", [])}
    forbidden_true_flags = {str(key).upper() for key in config.get("forbidden_true_flags", [])}
    protected_api_keys = {str(key).upper() for key in config.get("api_budget_protected_keys", [])}
    max_amounts = config.get("max_amounts") or {}

    for raw_key, value in changes.items():
        key = str(raw_key).strip().upper()
        if not key:
            errors.append("empty_change_key")
            continue

        if key in forbidden_env_keys:
            errors.append(f"forbidden_env_key:{key}")
            forbidden_changes.append(key)

        if key in forbidden_true_flags and _truthy(value):
            errors.append(f"forbidden_true_flag:{key}")
            forbidden_changes.append(key)

        if key in protected_api_keys:
            errors.append(f"api_budget_protected_key:{key}")
            forbidden_changes.append(key)
            api_budget_risk = True

        if key in REQUIRED_SAFE_FLAG_VALUES and _truthy(value) != REQUIRED_SAFE_FLAG_VALUES[key]:
            errors.append(f"required_safe_flag_violation:{key}")
            forbidden_changes.append(key)

        cap = _amount_cap_for_key(key, max_amounts)
        numeric_value = _float_value(value)
        if cap is not None and numeric_value is None:
            errors.append(f"amount_must_be_numeric:{key}")
            continue
        if cap is not None and numeric_value is not None and numeric_value > cap:
            errors.append(f"amount_cap_exceeded:{key}:{numeric_value}>{cap}")

    if not changes:
        warnings.append("no_changes_to_validate")

    return SafetyResult(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        forbidden_changes=sorted(set(forbidden_changes)),
        api_budget_risk=api_budget_risk,
    )


__all__ = ["SafetyResult", "load_safety_config", "validate_candidate_safety"]
