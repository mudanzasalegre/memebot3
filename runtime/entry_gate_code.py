"""Declared sources and bounded loaded-callable agreement.

Read known source bytes afresh, never file timestamps or operator artifacts.
This detects incompatible original evidence after source changes. It does not
authenticate a hostile whole-root rewrite or a complete process/input graph.
Historical funded positions retain their original exit contract independently.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

LEGACY_VERSION = "original_entry_gate_sources_v1"
VERSION = "original_entry_gate_sources_loaded_v2"
REFERENCE_VERSION = "original_entry_gate_identity_reference_v1"
ROOT = Path(__file__).resolve().parents[1]
LEGACY_COMMON = (
    "runtime/entry_gate_code.py", "runtime/paper_entry_policy.py",
    "research_loop/entry_gate_policy.py", "research_loop/entry_gate_forward.py",
    "analytics/token_time.py", "analytics/report_utils.py", "config/config.py",
    "ml/lane_taxonomy.py", "analytics/lane_policy_categories.py",
)
COMMON = LEGACY_COMMON + ("runtime/loaded_gate_code.py",)
LEAVES = MappingProxyType({
    "rank_canary": ("analytics/research_rank_canary.py",),
    "sniper_subprofile": ("analytics/sniper_research_subprofiles.py",),
    "late_momentum": ("analytics/late_momentum_watch.py", "analytics/liquidity_risk.py"),
    "moonshot": ("analytics/moonshot_micro_lottery.py",),
})


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("unsupported original receipt value")


def _digest(value: Any) -> str:
    # Native JSON handles ordinary nested dict/list/tuple values. Only custom
    # mappings need conversion; avoid a Python traversal of every scalar.
    return hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(",", ":"), allow_nan=False, default=_json_default).encode()).hexdigest()


def freeze(value: Any) -> Any:
    """Detached immutable provenance, including nested source lists."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze(item) for item in value)
    return value


def snapshot(gate: str) -> dict[str, Any]:
    if gate not in LEAVES:
        raise ValueError("unknown entry source component")
    sources = []
    for relative in COMMON + LEAVES[gate]:
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT) or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("entry source unavailable or outside bounded scope")
        content = path.read_bytes().replace(b"\r\n", b"\n")
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("oversized entry source")
        sources.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
    from runtime import loaded_gate_code
    loaded = loaded_gate_code.snapshot(gate, root=ROOT, sources=sources, leaves=LEAVES)
    identity = {"version": VERSION, "gate": gate, "sources": sources, "loaded": loaded}
    return {**identity, "sha256": _digest(identity)}


def valid_identity(value: Any, *, gate: str) -> bool:
    """Structural original receipt validation, without today's source reads."""
    try:
        if gate not in LEAVES or not isinstance(value, Mapping):
            return False
        legacy = value.get("version") == LEGACY_VERSION
        expected_keys = {"version", "gate", "sources", "sha256"} | (set() if legacy else {"loaded"})
        if (set(value) != expected_keys or value.get("version") not in {LEGACY_VERSION, VERSION}
                or value["gate"] != gate):
            return False
        sources = value["sources"]
        expected = (LEGACY_COMMON if legacy else COMMON) + LEAVES[gate]
        if not isinstance(sources, (list, tuple)) or len(sources) != len(expected):
            return False
        for entry, path in zip(sources, expected):
            if (not isinstance(entry, Mapping) or set(entry) != {"path", "sha256"}
                    or entry["path"] != path or not isinstance(entry["sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None):
                return False
        if not legacy:
            from runtime import loaded_gate_code
            if not loaded_gate_code.valid(_plain(value["loaded"]), gate=gate, allowed=set(expected)):
                return False
        keys = ("version", "gate", "sources") if legacy else ("version", "gate", "sources", "loaded")
        return isinstance(value["sha256"], str) and value["sha256"] == _digest({key: value[key] for key in keys})
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def reference(value: Any, *, gate: str) -> dict[str, str]:
    """Bound a compact journal reference to the complete original plan receipt.

    This never upgrades legacy evidence or proves current execution: consumers
    must separately validate the original plan receipt and current generation.
    """
    if not valid_identity(value, gate=gate):
        raise ValueError("missing original gate identity")
    return {"version": REFERENCE_VERSION, "gate": gate, "sha256": value["sha256"]}


def matches_current(value: Any, *, gate: str) -> bool:
    try:
        return valid_identity(value, gate=gate) and _plain(value) == snapshot(gate)
    except (OSError, ValueError, TypeError, ImportError, AttributeError, SyntaxError):
        return False
