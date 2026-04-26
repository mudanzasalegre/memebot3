from __future__ import annotations

from typing import Any


def selected_mask(frame, policy: str, threshold: float = 0.5):
    p = frame.get("y_prob")
    if policy in {"rules_only", "ml_shadow"} or p is None:
        return [True] * len(frame)
    if policy in {"lane_aware", "global_enforce", "risk_veto", "ev_sizing"}:
        return p.astype(float).ge(float(threshold))
    raise ValueError(f"unknown policy: {policy}")


__all__ = ["selected_mask"]
