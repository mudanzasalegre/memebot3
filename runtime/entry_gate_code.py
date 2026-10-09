"""Declared, bounded entry-source generations; not execution attestation.

Read known source bytes afresh, never file timestamps or operator artifacts.
This detects incompatible original evidence after source changes. It does not
authenticate a hostile whole-root rewrite or prove which Python was executed.
Historical funded positions retain their original exit contract independently.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

VERSION = "original_entry_gate_sources_v1"
ROOT = Path(__file__).resolve().parents[1]
COMMON = (
    "runtime/entry_gate_code.py", "runtime/paper_entry_policy.py",
    "research_loop/entry_gate_policy.py", "research_loop/entry_gate_forward.py",
    "analytics/token_time.py", "analytics/report_utils.py", "config/config.py",
    "ml/lane_taxonomy.py", "analytics/lane_policy_categories.py",
)
LEAVES = MappingProxyType({
    "rank_canary": ("analytics/research_rank_canary.py",),
    "sniper_subprofile": ("analytics/sniper_research_subprofiles.py",),
    "late_momentum": ("analytics/late_momentum_watch.py", "analytics/liquidity_risk.py"),
    "moonshot": ("analytics/moonshot_micro_lottery.py",),
})


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(_plain(value), sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


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
    identity = {"version": VERSION, "gate": gate, "sources": sources}
    return {**identity, "sha256": _digest(identity)}


def valid_identity(value: Any, *, gate: str) -> bool:
    """Structural original receipt validation, without today's source reads."""
    try:
        if (gate not in LEAVES or not isinstance(value, Mapping)
                or set(value) != {"version", "gate", "sources", "sha256"}
                or value["version"] != VERSION or value["gate"] != gate):
            return False
        sources = value["sources"]
        expected = COMMON + LEAVES[gate]
        if not isinstance(sources, (list, tuple)) or len(sources) != len(expected):
            return False
        for entry, path in zip(sources, expected):
            if (not isinstance(entry, Mapping) or set(entry) != {"path", "sha256"}
                    or entry["path"] != path or not isinstance(entry["sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None):
                return False
        return isinstance(value["sha256"], str) and value["sha256"] == _digest(
            {key: value[key] for key in ("version", "gate", "sources")})
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def matches_current(value: Any, *, gate: str) -> bool:
    try:
        return valid_identity(value, gate=gate) and _plain(value) == snapshot(gate)
    except (OSError, ValueError, TypeError):
        return False
