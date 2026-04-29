from __future__ import annotations

from types import SimpleNamespace

import analytics.green_sniper_sizing as sizing


def test_paper_uses_fixed_tiers(monkeypatch) -> None:
    monkeypatch.setattr(
        sizing,
        "CFG",
        SimpleNamespace(
            GREEN_SNIPER_SIZE_HOT_SOL=0.10,
            GREEN_SNIPER_SIZE_CORE_SOL=0.06,
            GREEN_SNIPER_SIZE_MICRO_SOL=0.03,
            GREEN_SNIPER_SIZE_MODE="fixed_tiers",
            GREEN_SNIPER_ML_RISK_REDUCE_SIZE=True,
            GREEN_SNIPER_ML_EV_SIZE_UP_PAPER=False,
            GREEN_SNIPER_SOCIALS_CAN_INCREASE_SIZE_PAPER=True,
            GREEN_SNIPER_SOCIALS_CAN_INCREASE_SIZE_LIVE=False,
            GREEN_SNIPER_SOCIALS_CAN_DECREASE_SIZE=True,
            GREEN_SNIPER_SOCIALS_MAX_SIZE_BONUS_TIER=1,
            GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_REDUCE_SIZE=True,
        ),
    )
    decision = sizing.compute_green_sniper_sizing({}, dry_run=True, live=False, size_hint="hot")
    assert decision.amount_sol == 0.10


def test_live_uses_canary_fixed(monkeypatch) -> None:
    monkeypatch.setattr(
        sizing,
        "CFG",
        SimpleNamespace(
            GREEN_SNIPER_LIVE_SIZE_SOL=0.01,
            GREEN_SNIPER_LIVE_SIZE_MODE="canary_fixed",
            GREEN_SNIPER_LIVE_ADVANCED_ENABLED=False,
            GREEN_SNIPER_SOCIALS_CAN_DECREASE_SIZE=True,
            GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_REDUCE_SIZE=True,
        ),
    )
    decision = sizing.compute_green_sniper_sizing({}, dry_run=False, live=True, size_hint="hot")
    assert decision.amount_sol == 0.01


def test_social_present_can_promote_one_paper_tier(monkeypatch) -> None:
    monkeypatch.setattr(
        sizing,
        "CFG",
        SimpleNamespace(
            GREEN_SNIPER_SIZE_HOT_SOL=0.10,
            GREEN_SNIPER_SIZE_CORE_SOL=0.06,
            GREEN_SNIPER_SIZE_MICRO_SOL=0.03,
            GREEN_SNIPER_SIZE_MODE="fixed_tiers",
            GREEN_SNIPER_ML_RISK_REDUCE_SIZE=False,
            GREEN_SNIPER_ML_EV_SIZE_UP_PAPER=False,
            GREEN_SNIPER_SOCIALS_CAN_INCREASE_SIZE_PAPER=True,
            GREEN_SNIPER_SOCIALS_MAX_SIZE_BONUS_TIER=1,
            GREEN_SNIPER_SOCIALS_CAN_DECREASE_SIZE=True,
            GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_REDUCE_SIZE=True,
        ),
    )
    decision = sizing.compute_green_sniper_sizing(
        {"social_status": "present", "social_ok": True, "social_link_count": 1},
        dry_run=True,
        live=False,
        size_hint="micro",
    )
    assert decision.size_hint == "core"
    assert decision.amount_sol == 0.06


def test_social_present_does_not_increase_live_size(monkeypatch) -> None:
    monkeypatch.setattr(
        sizing,
        "CFG",
        SimpleNamespace(
            GREEN_SNIPER_LIVE_SIZE_SOL=0.01,
            GREEN_SNIPER_LIVE_SIZE_MODE="canary_fixed",
            GREEN_SNIPER_LIVE_ADVANCED_ENABLED=False,
            GREEN_SNIPER_SOCIALS_CAN_DECREASE_SIZE=True,
            GREEN_SNIPER_SOCIALS_SUSPICIOUS_CAN_REDUCE_SIZE=True,
        ),
    )
    decision = sizing.compute_green_sniper_sizing(
        {"social_status": "present", "social_ok": True, "social_link_count": 1},
        dry_run=False,
        live=True,
        size_hint="hot",
    )
    assert decision.amount_sol == 0.01
