from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionContext:
    dry_run: bool
    live: bool
    balance_sol: float | None = None
    rpc_ok: bool = True
    model_degraded: bool = False


__all__ = ["ExecutionContext"]
