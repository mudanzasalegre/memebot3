from __future__ import annotations

from pathlib import Path
from typing import Any

from analytics.paper_forward import evaluate_paper_forward, write_paper_forward_report


def evaluate_candidate_policy_forward(candidate_policy: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    return evaluate_paper_forward(candidate_policy, root=root)


def write_candidate_policy_forward_report(candidate_policy: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    return write_paper_forward_report(candidate_policy, root=root)


__all__ = ["evaluate_candidate_policy_forward", "write_candidate_policy_forward_report"]
