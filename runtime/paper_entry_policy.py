"""Task-local paper entry thresholds; never mutates the global configuration.

Only the explicitly integrated entry gates call ``entry_config``. Position
monitoring, execution, size, route checks and safety configuration keep their
ordinary configuration. Binding parameters is a simulation primitive, not an
evidence-based promotion: runtime selection belongs to entry_gate_policy.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterator, Mapping


@dataclass(frozen=True)
class Threshold:
    gate: str
    minimum: float
    maximum: float
    max_step: float


# These are admission thresholds, NOT return targets or take-profit ceilings.
# Security flags, impact limits, amounts, quotas and exits are intentionally absent.
THRESHOLDS = MappingProxyType({
    "RESEARCH_RANK_CANARY_MIN_SCORE": Threshold("rank_canary", 40, 90, 5),
    "RESEARCH_RANK_CANARY_PRIORITY_MIN_RANK_SCORE": Threshold("rank_canary", 40, 90, 5),
    "RESEARCH_RANK_CANARY_PRIORITY_MIN_TXNS_5M": Threshold("rank_canary", 100, 3000, 300),
    "RESEARCH_RANK_CANARY_PRIORITY_MIN_LIQUIDITY_USD": Threshold("rank_canary", 8000, 100000, 5000),
    "RESEARCH_RANK_CANARY_PRIORITY_MIN_PRICE5M": Threshold("rank_canary", 0, 500, 20),
    "RESEARCH_RANK_CANARY_PRIORITY_MAX_PRICE5M": Threshold("rank_canary", 50, 100000, 500),
    "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_RANK_SCORE": Threshold("rank_canary", 40, 90, 5),
    "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_TXNS_5M": Threshold("rank_canary", 100, 3000, 300),
    "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_LIQUIDITY_USD": Threshold("rank_canary", 8000, 100000, 5000),
    "RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_PRICE5M": Threshold("rank_canary", 0, 500, 20),
    "RESEARCH_RANK_CANARY_PAPER_NORMAL_MAX_PRICE5M": Threshold("rank_canary", 50, 100000, 500),
    "SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M": Threshold("sniper_subprofile", 20, 500, 30),
    "SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M": Threshold("sniper_subprofile", 50, 100000, 500),
    "SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M": Threshold("sniper_subprofile", 100, 3000, 300),
    "SNIPER_RESEARCH_MOMENTUM_MIN_LIQUIDITY_USD": Threshold("sniper_subprofile", 8000, 100000, 5000),
    "SNIPER_RESEARCH_MOMENTUM_MAX_MCAP_USD": Threshold("sniper_subprofile", 25000, 250000, 50000),
    "SNIPER_RESEARCH_DEEP_REVERSAL_MIN_PRICE5M": Threshold("sniper_subprofile", -99, -30, 20),
    "SNIPER_RESEARCH_DEEP_REVERSAL_MAX_PRICE5M": Threshold("sniper_subprofile", -95, -10, 20),
    "SNIPER_RESEARCH_DEEP_REVERSAL_MIN_TXNS_5M": Threshold("sniper_subprofile", 100, 3000, 300),
    "LATE_MOMENTUM_WATCH_MIN_PRICE5M": Threshold("late_momentum", 100, 2000, 100),
    "LATE_MOMENTUM_WATCH_MAX_PRICE5M": Threshold("late_momentum", 300, 100000, 500),
    "LATE_MOMENTUM_WATCH_MIN_RANK_SCORE": Threshold("late_momentum", 40, 90, 5),
    "LATE_MOMENTUM_WATCH_MIN_TXNS_5M": Threshold("late_momentum", 100, 3000, 300),
    "LATE_MOMENTUM_WATCH_MIN_LIQUIDITY_USD": Threshold("late_momentum", 1500, 50000, 2000),
    "MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M": Threshold("moonshot", 100, 2000, 100),
    "MOONSHOT_MICRO_LOTTERY_MIN_TXNS_5M": Threshold("moonshot", 100, 3000, 300),
    "MOONSHOT_MICRO_LOTTERY_MAX_MCAP_USD": Threshold("moonshot", 25000, 250000, 50000),
})
PREFIXES = {
    "rank_canary": "RESEARCH_RANK_CANARY_",
    "sniper_subprofile": "SNIPER_RESEARCH_",
    "late_momentum": "LATE_MOMENTUM_WATCH_",
    "moonshot": "MOONSHOT_MICRO_LOTTERY_",
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not an admission threshold")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite admission threshold")
    return result


def configured_hash(cfg: Any, gate: str) -> str:
    """Bind evidence to all configuration fields of the evaluated component.

    Only known gate prefixes are exposed; credentials and unrelated global
    configuration are never serialized. This does not certify the full bot.
    """
    prefix = PREFIXES[gate]
    values = {key: value for key, value in vars(cfg).items() if key.startswith(prefix)}
    # Frozen dataclass defaults can live on the class rather than in vars().
    for key in dir(type(cfg)):
        if key.startswith(prefix):
            values[key] = getattr(cfg, key)
    values["DRY_RUN"] = getattr(cfg, "DRY_RUN", False)
    return digest(values)


def validate_parameters(cfg: Any, parameters: Mapping[str, Any], *, gate: str | None = None) -> dict[str, float]:
    if not isinstance(parameters, Mapping) or not 1 <= len(parameters) <= 2:
        raise ValueError("one or two admission thresholds required")
    output: dict[str, float] = {}
    gates: set[str] = set()
    for key, value in parameters.items():
        rule = THRESHOLDS.get(key)
        if rule is None:
            raise ValueError("unsupported admission parameter")
        baseline, candidate = number(getattr(cfg, key)), number(value)
        if (not rule.minimum <= candidate <= rule.maximum
                or abs(candidate - baseline) > rule.max_step or candidate == baseline):
            raise ValueError("unbounded, unchanged or non-adjacent admission parameter")
        output[key] = candidate
        gates.add(rule.gate)
    if len(gates) != 1 or (gate is not None and gates != {gate}):
        raise ValueError("one entry component per paired experiment")
    for minimum, maximum in (
        ("RESEARCH_RANK_CANARY_PRIORITY_MIN_PRICE5M", "RESEARCH_RANK_CANARY_PRIORITY_MAX_PRICE5M"),
        ("RESEARCH_RANK_CANARY_PAPER_NORMAL_MIN_PRICE5M", "RESEARCH_RANK_CANARY_PAPER_NORMAL_MAX_PRICE5M"),
        ("SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M", "SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M"),
        ("SNIPER_RESEARCH_DEEP_REVERSAL_MIN_PRICE5M", "SNIPER_RESEARCH_DEEP_REVERSAL_MAX_PRICE5M"),
        ("LATE_MOMENTUM_WATCH_MIN_PRICE5M", "LATE_MOMENTUM_WATCH_MAX_PRICE5M"),
    ):
        if minimum in output or maximum in output:
            low = number(output.get(minimum, getattr(cfg, minimum)))
            high = number(output.get(maximum, getattr(cfg, maximum)))
            if low >= high:
                raise ValueError("inverted admission price band")
    return output


def validate_transition(cfg: Any, incumbent: Mapping[str, Any], candidate: Mapping[str, Any],
                        *, gate: str) -> dict[str, float]:
    """Check the effective change, including parameters reset to configured values.

    Each complete profile remains inside the original allowlisted envelope.
    Comparing two individually valid profiles is not permission to change four
    fields or jump from one end of that envelope to the other in one trial.
    An empty candidate means the configured baseline, never a scoped binding.
    """
    before = validate_parameters(cfg, incumbent, gate=gate) if incumbent else {}
    after = validate_parameters(cfg, candidate, gate=gate) if candidate else {}
    if not isinstance(incumbent, Mapping) or not isinstance(candidate, Mapping):
        raise ValueError("complete admission profiles required")
    changed = {}
    for key in before.keys() | after.keys():
        old = before.get(key, number(getattr(cfg, key)))
        new = after.get(key, number(getattr(cfg, key)))
        if old != new:
            if abs(new - old) > THRESHOLDS[key].max_step:
                raise ValueError("non-adjacent incumbent transition")
            changed[key] = new
    if len(changed) > 2:
        raise ValueError("at most two effective admission changes per experiment")
    return after


class _EntryConfig:
    __slots__ = ("_base", "_parameters")

    def __init__(self, base: Any, parameters: Mapping[str, float]):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_parameters", MappingProxyType(dict(parameters)))

    def __getattr__(self, key: str) -> Any:
        if key in self._parameters:
            return self._parameters[key]
        return getattr(self._base, key)

    def __setattr__(self, key: str, value: Any) -> None:
        raise AttributeError("entry configuration is immutable")


@dataclass(frozen=True)
class _Binding:
    owner: Any
    cfg: _EntryConfig
    provenance: Mapping[str, Any]


_CURRENT: contextvars.ContextVar[_Binding | None] = contextvars.ContextVar("paper_entry_policy", default=None)


@contextlib.contextmanager
def baseline_scope() -> Iterator[None]:
    token = _CURRENT.set(None)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def entry_config(cfg: Any, *, dry_run: bool | None = None, live: bool = False) -> Any:
    binding = _CURRENT.get()
    if (binding is None or binding.owner is not cfg or getattr(cfg, "DRY_RUN", False) is not True
            or live or dry_run is False):
        return cfg
    return binding.cfg


def snapshot() -> dict[str, Any] | None:
    binding = _CURRENT.get()
    if binding is None:
        return None
    def detached(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: detached(item) for key, item in value.items()}
        return value
    return detached(binding.provenance)


@contextlib.contextmanager
def composition_scope(cfg: Any, selections: Mapping[str, Mapping[str, Any]]) -> Iterator[None]:
    """Combine independently checked components, not a whole-strategy certificate.

    This is a binding primitive like parameter_scope; the production selector
    must recheck every component's original evidence before calling it. Each
    component keeps its own two-parameter envelope and immutable provenance.
    """
    if getattr(cfg, "DRY_RUN", False) is not True or not selections:
        raise ValueError("nonempty paper-only admission composition required")
    parameters, components = {}, {}
    for gate, selection in selections.items():
        if gate not in PREFIXES:
            raise ValueError("unsupported admission component")
        checked = validate_parameters(cfg, selection["parameters"], gate=gate)
        parameters.update(checked)
        components[gate] = MappingProxyType({
            "revision": str(selection["revision"]),
            "evidence_sha256": selection["evidence_sha256"],
            "configured_hash": configured_hash(cfg, gate),
            "parameters": MappingProxyType(checked),
        })
    provenance = MappingProxyType({
        "version": "paper_entry_composition_v1", "role": "paper_entry_components_only",
        "components": MappingProxyType(components), "parameters": MappingProxyType(parameters),
        "full_strategy_profitability_established": False,
    })
    token = _CURRENT.set(_Binding(cfg, _EntryConfig(cfg, parameters), provenance))
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextlib.contextmanager
def parameter_scope(cfg: Any, parameters: Mapping[str, Any], *, revision: str,
                    evidence_sha256: str = "") -> Iterator[None]:
    """Isolated gate simulation. Production callers must verify evidence first."""
    if getattr(cfg, "DRY_RUN", False) is not True:
        raise ValueError("entry adaptation is paper-only")
    checked = validate_parameters(cfg, parameters)
    gate = THRESHOLDS[next(iter(checked))].gate
    provenance = MappingProxyType({
        "version": "paper_entry_thresholds_v1", "role": "paper_entry_gate_only",
        "gate": gate, "revision": str(revision), "configured_hash": configured_hash(cfg, gate),
        "parameters": MappingProxyType(checked), "evidence_sha256": evidence_sha256,
        "full_strategy_profitability_established": False,
    })
    token = _CURRENT.set(_Binding(cfg, _EntryConfig(cfg, checked), provenance))
    try:
        yield
    finally:
        _CURRENT.reset(token)
