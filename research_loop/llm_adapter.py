from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from research_loop.experiment_schema import CandidatePolicyValidationError, validate_candidate_policy
from research_loop.paths import project_root
from research_loop.safety import validate_candidate_safety

STATUS_DISABLED = "disabled"
STATUS_BLOCKED = "blocked"
STATUS_NO_GENERATOR = "no_generator"
STATUS_GENERATED = "generated"
STATUS_REJECTED = "rejected"

LLM_FLAG_DEFAULTS = {
    "AUTORESEARCH_LLM_ENABLED": False,
    "AUTORESEARCH_LLM_CAN_EDIT_CODE": False,
    "AUTORESEARCH_LLM_CAN_TOUCH_LIVE": False,
    "AUTORESEARCH_LLM_CAN_CALL_APIS": False,
}

UNSAFE_LLM_CAPABILITY_FLAGS = (
    "AUTORESEARCH_LLM_CAN_EDIT_CODE",
    "AUTORESEARCH_LLM_CAN_TOUCH_LIVE",
    "AUTORESEARCH_LLM_CAN_CALL_APIS",
)

CandidateGenerator = Callable[[dict[str, Any]], dict[str, Any] | None]


@dataclass(frozen=True)
class LLMAdapterConfig:
    enabled: bool = False
    can_edit_code: bool = False
    can_touch_live: bool = False
    can_call_apis: bool = False

    @property
    def unsafe_capabilities(self) -> list[str]:
        unsafe: list[str] = []
        if self.can_edit_code:
            unsafe.append("AUTORESEARCH_LLM_CAN_EDIT_CODE")
        if self.can_touch_live:
            unsafe.append("AUTORESEARCH_LLM_CAN_TOUCH_LIVE")
        if self.can_call_apis:
            unsafe.append("AUTORESEARCH_LLM_CAN_CALL_APIS")
        return unsafe

    def as_dict(self) -> dict[str, Any]:
        return {
            "AUTORESEARCH_LLM_ENABLED": self.enabled,
            "AUTORESEARCH_LLM_CAN_EDIT_CODE": self.can_edit_code,
            "AUTORESEARCH_LLM_CAN_TOUCH_LIVE": self.can_touch_live,
            "AUTORESEARCH_LLM_CAN_CALL_APIS": self.can_call_apis,
            "unsafe_capabilities": self.unsafe_capabilities,
        }


@dataclass(frozen=True)
class LLMAdapterResult:
    status: str
    candidate_policy: dict[str, Any] | None = None
    output_path: Path | None = None
    config: LLMAdapterConfig = field(default_factory=LLMAdapterConfig)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    generator_called: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidate_policy": self.candidate_policy,
            "output_path": str(self.output_path) if self.output_path else None,
            "config": self.config.as_dict(),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "generator_called": self.generator_called,
        }


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def load_llm_adapter_config(env: Mapping[str, Any] | None = None) -> LLMAdapterConfig:
    source = os.environ if env is None else env
    return LLMAdapterConfig(
        enabled=_truthy(source.get("AUTORESEARCH_LLM_ENABLED", LLM_FLAG_DEFAULTS["AUTORESEARCH_LLM_ENABLED"])),
        can_edit_code=_truthy(
            source.get("AUTORESEARCH_LLM_CAN_EDIT_CODE", LLM_FLAG_DEFAULTS["AUTORESEARCH_LLM_CAN_EDIT_CODE"])
        ),
        can_touch_live=_truthy(
            source.get("AUTORESEARCH_LLM_CAN_TOUCH_LIVE", LLM_FLAG_DEFAULTS["AUTORESEARCH_LLM_CAN_TOUCH_LIVE"])
        ),
        can_call_apis=_truthy(
            source.get("AUTORESEARCH_LLM_CAN_CALL_APIS", LLM_FLAG_DEFAULTS["AUTORESEARCH_LLM_CAN_CALL_APIS"])
        ),
    )


def _read_report_bundle(report_bundle: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(report_bundle, dict):
        return dict(report_bundle)
    payload = json.loads(Path(report_bundle).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("report_bundle_must_be_object")
    return payload


def _default_output_path(root: Path, candidate_policy: dict[str, Any]) -> Path:
    proposal_id = str(candidate_policy.get("proposal_id") or "llm_candidate").strip() or "llm_candidate"
    return root / "strategy_proposals" / "candidates" / f"{proposal_id}.json"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _validate_generated_candidate(candidate_policy: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    try:
        validated = validate_candidate_policy(candidate_policy)
    except CandidatePolicyValidationError as exc:
        return None, [str(exc)]
    safety = validate_candidate_safety(validated.to_dict())
    if not safety.ok:
        errors.extend(safety.errors)
    if errors:
        return None, errors
    return validated.to_dict(), []


def run_llm_adapter(
    report_bundle: str | Path | dict[str, Any],
    *,
    root: str | Path | None = None,
    output_path: str | Path | None = None,
    env: Mapping[str, Any] | None = None,
    generator: CandidateGenerator | None = None,
    write: bool = True,
) -> LLMAdapterResult:
    config = load_llm_adapter_config(env)
    warnings: list[str] = []
    if not config.enabled:
        return LLMAdapterResult(
            status=STATUS_DISABLED,
            config=config,
            warnings=["llm_disabled"],
            generator_called=False,
        )
    if config.unsafe_capabilities:
        return LLMAdapterResult(
            status=STATUS_BLOCKED,
            config=config,
            errors=[f"unsafe_llm_capability:{flag}" for flag in config.unsafe_capabilities],
            generator_called=False,
        )
    if generator is None:
        return LLMAdapterResult(
            status=STATUS_NO_GENERATOR,
            config=config,
            warnings=["llm_generator_not_configured"],
            generator_called=False,
        )

    bundle = _read_report_bundle(report_bundle)
    generated = generator(bundle)
    if not isinstance(generated, dict):
        return LLMAdapterResult(
            status=STATUS_REJECTED,
            config=config,
            warnings=warnings,
            errors=["generated_candidate_must_be_object"],
            generator_called=True,
        )

    candidate_policy, errors = _validate_generated_candidate(generated)
    if candidate_policy is None:
        return LLMAdapterResult(
            status=STATUS_REJECTED,
            config=config,
            warnings=warnings,
            errors=errors,
            generator_called=True,
        )

    resolved_root = project_root(root)
    resolved_output = Path(output_path) if output_path is not None else _default_output_path(resolved_root, candidate_policy)
    if write:
        _write_json(resolved_output, candidate_policy)
    return LLMAdapterResult(
        status=STATUS_GENERATED,
        candidate_policy=candidate_policy,
        output_path=resolved_output,
        config=config,
        warnings=warnings,
        errors=[],
        generator_called=True,
    )


__all__ = [
    "CandidateGenerator",
    "LLMAdapterConfig",
    "LLMAdapterResult",
    "LLM_FLAG_DEFAULTS",
    "STATUS_BLOCKED",
    "STATUS_DISABLED",
    "STATUS_GENERATED",
    "STATUS_NO_GENERATOR",
    "STATUS_REJECTED",
    "UNSAFE_LLM_CAPABILITY_FLAGS",
    "load_llm_adapter_config",
    "run_llm_adapter",
]
