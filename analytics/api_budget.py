from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

try:
    from research_loop.api_budget import (  # noqa: F401
        BUDGET_METRIC_KEYS,
        PROVIDERS,
        ApiBudgetComparison,
        build_api_budget_report,
        compare_api_budget,
        metrics_from_api_budget,
    )
except Exception:  # pragma: no cover - compatibility for partial imports
    BUDGET_METRIC_KEYS = ()
    PROVIDERS = ()
    ApiBudgetComparison = object  # type: ignore

    def build_api_budget_report(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        return {}

    def compare_api_budget(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("research_loop.api_budget is not available")

    def metrics_from_api_budget(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        return {}


_DEFAULT_ENTRY_PROVIDERS = ("jupiter", "birdeye", "dexscreener", "gecko")
_DEGRADE_EVENTS = {
    "429",
    "rate_limit",
    "rate_limited",
    "too_many_requests",
    "provider_degraded",
    "degraded",
    "budget_exhausted",
}
_RECOVERY_EVENTS = {"ok", "recover", "recovered", "healthy", "reset"}


@dataclass(frozen=True)
class ProviderRuntimeStatus:
    provider: str
    status: str
    degraded_until: Optional[float]
    reason: Optional[str]
    event_count: int

    @property
    def degraded(self) -> bool:
        return self.status == "degraded"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "degraded": self.degraded,
            "degraded_until": self.degraded_until,
            "reason": self.reason,
            "event_count": self.event_count,
        }


_DEGRADED_UNTIL: Dict[str, float] = {}
_DEGRADE_REASON: Dict[str, str] = {}
_EVENT_COUNTS: Dict[str, int] = {}


def _now() -> float:
    return time.monotonic()


def _cooldown_s() -> float:
    try:
        return max(1.0, float(os.getenv("PROVIDER_DEGRADE_COOLDOWN_S", "300")))
    except Exception:
        return 300.0


def normalize_provider(provider: object) -> str:
    value = str(provider or "").strip().lower()
    if "jup" in value:
        return "jupiter"
    if "bird" in value:
        return "birdeye"
    if "dex" in value:
        return "dexscreener"
    if "gecko" in value or value == "gt":
        return "gecko"
    if "pump" in value:
        return "pumpfun"
    if "rpc" in value:
        return "rpc"
    return value or "unknown"


def _entry_providers_from_env() -> Tuple[str, ...]:
    raw = os.getenv("PROVIDER_ENTRY_BLOCKERS", "")
    if not raw.strip():
        return _DEFAULT_ENTRY_PROVIDERS
    providers = tuple(
        normalize_provider(item)
        for item in raw.split(",")
        if str(item).strip()
    )
    return providers or _DEFAULT_ENTRY_PROVIDERS


def _active_status(provider: str, now: Optional[float] = None) -> ProviderRuntimeStatus:
    name = normalize_provider(provider)
    current = _now() if now is None else float(now)
    until = _DEGRADED_UNTIL.get(name)
    if until is not None and until <= current:
        _DEGRADED_UNTIL.pop(name, None)
        _DEGRADE_REASON.pop(name, None)
        until = None
    status = "degraded" if until is not None else "ok"
    return ProviderRuntimeStatus(
        provider=name,
        status=status,
        degraded_until=until,
        reason=_DEGRADE_REASON.get(name),
        event_count=int(_EVENT_COUNTS.get(name, 0)),
    )


def provider_status(provider: object, *, now: Optional[float] = None) -> Dict[str, Any]:
    return _active_status(normalize_provider(provider), now=now).as_dict()


def record_provider_event(
    provider: object,
    event: object,
    *,
    now: Optional[float] = None,
    cooldown_s: Optional[float] = None,
) -> Dict[str, Any]:
    name = normalize_provider(provider)
    event_name = str(event or "").strip().lower()
    current = _now() if now is None else float(now)
    _EVENT_COUNTS[name] = int(_EVENT_COUNTS.get(name, 0)) + 1

    if event_name in _RECOVERY_EVENTS:
        _DEGRADED_UNTIL.pop(name, None)
        _DEGRADE_REASON.pop(name, None)
        return _active_status(name, now=current).as_dict()

    if event_name in _DEGRADE_EVENTS:
        ttl = _cooldown_s() if cooldown_s is None else max(1.0, float(cooldown_s))
        _DEGRADED_UNTIL[name] = current + ttl
        _DEGRADE_REASON[name] = event_name

    return _active_status(name, now=current).as_dict()


def provider_entries_allowed(
    required: Optional[Iterable[object]] = None,
    *,
    now: Optional[float] = None,
) -> tuple[bool, Optional[str], Dict[str, Any]]:
    providers = tuple(normalize_provider(p) for p in (required or _entry_providers_from_env()))
    statuses = {provider: _active_status(provider, now=now).as_dict() for provider in providers}
    degraded = [provider for provider, status in statuses.items() if status.get("degraded")]
    if degraded:
        reason = "provider_degraded:" + ",".join(sorted(degraded))
        return False, reason, {"providers": statuses, "degraded": degraded}
    return True, None, {"providers": statuses, "degraded": []}


def provider_exits_allowed(
    required: Optional[Iterable[object]] = None,
    *,
    now: Optional[float] = None,
) -> tuple[bool, Optional[str], Dict[str, Any]]:
    providers = tuple(normalize_provider(p) for p in (required or _entry_providers_from_env()))
    statuses = {provider: _active_status(provider, now=now).as_dict() for provider in providers}
    return True, None, {"providers": statuses, "degraded": [p for p, s in statuses.items() if s.get("degraded")]}


def provider_runtime_snapshot(*, now: Optional[float] = None) -> Dict[str, Any]:
    providers = sorted(set(_entry_providers_from_env()) | set(_DEGRADED_UNTIL) | set(_EVENT_COUNTS))
    statuses = {provider: _active_status(provider, now=now).as_dict() for provider in providers}
    return {
        "status": "degraded" if any(s.get("degraded") for s in statuses.values()) else "ok",
        "providers": statuses,
    }


def reset_provider_circuits() -> None:
    _DEGRADED_UNTIL.clear()
    _DEGRADE_REASON.clear()
    _EVENT_COUNTS.clear()


__all__ = [
    "ApiBudgetComparison",
    "BUDGET_METRIC_KEYS",
    "PROVIDERS",
    "ProviderRuntimeStatus",
    "build_api_budget_report",
    "compare_api_budget",
    "metrics_from_api_budget",
    "normalize_provider",
    "provider_entries_allowed",
    "provider_exits_allowed",
    "provider_runtime_snapshot",
    "provider_status",
    "record_provider_event",
    "reset_provider_circuits",
]
