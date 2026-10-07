from __future__ import annotations

import datetime as dt

from runtime.policy_overlay import (
    build_policy_overlay_state,
    evaluate_policy_overlay,
)


UTC = dt.timezone.utc


def test_policy_overlay_blocks_recommended_disabled_lane_in_paper() -> None:
    now = dt.datetime(2026, 7, 7, 10, 0, tzinfo=UTC)
    state = {
        "generated_at_utc": now.isoformat(),
        "recommended_changes": {
            "PAPER_EXPLORATION_QUOTA_ENABLED": False,
            "LIVE_CANARY_ENABLED": True,
        },
        "actions": [],
    }
    overlay = build_policy_overlay_state(state, now=now, cooldown_min=30)

    decision = evaluate_policy_overlay(
        {"entry_lane": "pump_early_paper_exploration_micro"},
        dry_run=True,
        live=False,
        overlay_state=overlay,
        now=now + dt.timedelta(minutes=5),
    )

    assert decision.allowed is False
    assert decision.lane == "pump_early_paper_exploration_micro"
    assert "PAPER_EXPLORATION_QUOTA_ENABLED=false" in decision.reason
    assert decision.backoff_s > 0
    assert not any("LIVE" in str(block.get("reason")) for block in overlay["blocked_lanes"])


def test_policy_overlay_live_guard_never_blocks_live() -> None:
    now = dt.datetime(2026, 7, 7, 10, 0, tzinfo=UTC)
    overlay = build_policy_overlay_state(
        {
            "generated_at_utc": now.isoformat(),
            "recommended_changes": {"SHADOW_FOLLOWUP_MICRO_ENABLED": False},
            "actions": [],
        },
        now=now,
        cooldown_min=60,
    )

    decision = evaluate_policy_overlay(
        {"entry_lane": "pump_early_shadow_followup_micro"},
        dry_run=False,
        live=True,
        overlay_state=overlay,
        now=now,
    )

    assert decision.allowed is True
    assert decision.reason == "live_guard_no_runtime_overlay"


def test_policy_overlay_expired_cooldown_allows_lane() -> None:
    now = dt.datetime(2026, 7, 7, 10, 0, tzinfo=UTC)
    overlay = build_policy_overlay_state(
        {
            "generated_at_utc": now.isoformat(),
            "recommended_changes": {"MOONSHOT_MICRO_LOTTERY_ENABLED": False},
            "actions": [],
        },
        now=now,
        cooldown_min=15,
    )

    decision = evaluate_policy_overlay(
        {"entry_lane": "pump_early_moonshot_micro_lottery"},
        dry_run=True,
        live=False,
        overlay_state=overlay,
        now=now + dt.timedelta(minutes=20),
    )

    assert decision.allowed is True
    assert decision.reason == "ok"
