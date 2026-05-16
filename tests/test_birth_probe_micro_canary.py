from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import analytics.exit_policy as exit_policy
from analytics.birth_probe_micro_canary import (
    apply_birth_probe_micro_canary_context,
    evaluate_birth_probe_micro_canary,
    reason_group_from_failures,
    summarize_reason_groups,
    write_birth_probe_micro_canary_report,
)


def _cfg(**overrides: object) -> SimpleNamespace:
    base = {
        "BIRTH_PROBE_MICRO_CANARY_ENABLED": True,
        "BIRTH_PROBE_MICRO_CANARY_PAPER_ENABLED": True,
        "BIRTH_PROBE_MICRO_CANARY_LIVE_ENABLED": False,
        "BIRTH_PROBE_MICRO_CANARY_AMOUNT_SOL": 0.01,
        "BIRTH_PROBE_MICRO_CANARY_ALLOWED_REASON_GROUPS": "paper_birth_probe_proxy_low_txns,paper_birth_probe_low_green_proxy_low_txns",
        "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_EV_PCT": 5.0,
        "BIRTH_PROBE_MICRO_CANARY_PNL_CAP_PCT": 1000.0,
        "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_CAPPED_EV_PCT": -1.0,
        "BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_SAMPLES": 50,
        "BIRTH_PROBE_MICRO_CANARY_TIME_STOP_MIN": 3.0,
        "BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_EXIT_MIN": 2.0,
        "BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_MIN_PNL": 5.0,
        "BIRTH_PROBE_MICRO_CANARY_TP1_PCT": 25.0,
        "BIRTH_PROBE_MICRO_CANARY_TP1_FRACTION": 0.50,
        "BIRTH_PROBE_MICRO_CANARY_TP2_PCT": 100.0,
        "BIRTH_PROBE_MICRO_CANARY_TP2_FRACTION": 0.30,
        "BIRTH_PROBE_MICRO_CANARY_TP3_PCT": 300.0,
        "BIRTH_PROBE_MICRO_CANARY_TP3_FRACTION": 0.20,
        "BIRTH_PROBE_MICRO_CANARY_TP4_PCT": 700.0,
        "BIRTH_PROBE_MICRO_CANARY_TP4_FRACTION": 0.15,
        "BIRTH_PROBE_MICRO_CANARY_MOONBAG_FRACTION": 0.20,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_reason_group_maps_proxy_low_txns_and_low_green() -> None:
    assert reason_group_from_failures(["proxy_liquidity_productive_block", "low_txns_5m"]) == "paper_birth_probe_proxy_low_txns"
    assert (
        reason_group_from_failures(["low_green_momentum", "proxy_liquidity_productive_block", "low_txns_5m"])
        == "paper_birth_probe_low_green_proxy_low_txns"
    )


def test_micro_canary_evaluate_requires_good_group_stats() -> None:
    cfg = _cfg()
    stats = {
        "paper_birth_probe_proxy_low_txns": {
            "samples": 50,
            "avg_pnl": 6.0,
            "avg_pnl_capped": 6.0,
            "peak100_count": 3,
            "recommended_micro_enabled": True,
        }
    }
    decision = evaluate_birth_probe_micro_canary(
        {},
        ["proxy_liquidity_productive_block", "low_txns_5m"],
        dry_run=True,
        live=False,
        group_stats=stats,
        cfg=cfg,
    )

    assert decision.allowed
    assert decision.amount_sol == 0.01
    assert decision.lane == "pump_early_birth_probe_micro_canary"


def test_micro_canary_is_paper_only() -> None:
    cfg = _cfg()
    stats = {"paper_birth_probe_proxy_low_txns": {"samples": 50, "avg_pnl": 6.0, "peak100_count": 3, "recommended_micro_enabled": True}}

    decision = evaluate_birth_probe_micro_canary(
        {},
        ["proxy_liquidity_productive_block", "low_txns_5m"],
        dry_run=False,
        live=True,
        group_stats=stats,
        cfg=cfg,
    )

    assert not decision.allowed
    assert decision.reason == "paper_only"


def test_micro_canary_report_recommends_positive_reason_group(tmp_path) -> None:
    rows = []
    for idx in range(50):
        rows.append(
            {
                "reason": "green_sniper:paper_birth_probe:proxy_liquidity_productive_block,low_txns_5m",
                "realized_pnl_pct": 8.0,
                "max_pnl_pct_seen": 120.0 if idx < 3 else 20.0,
            }
        )
    groups = summarize_reason_groups(rows, cfg=_cfg())

    assert groups["paper_birth_probe_proxy_low_txns"]["samples"] == 50
    assert groups["paper_birth_probe_proxy_low_txns"]["avg_pnl_capped"] == 8.0
    assert groups["paper_birth_probe_proxy_low_txns"]["recommended_micro_enabled"] is True


def test_micro_canary_report_caps_outlier_driven_group() -> None:
    rows = [
        {
            "reason": "green_sniper:paper_birth_probe:proxy_liquidity_productive_block,low_txns_5m",
            "realized_pnl_pct": 6000.0,
            "max_pnl_pct_seen": 6000.0,
        }
    ]
    for _ in range(49):
        rows.append(
            {
                "reason": "green_sniper:paper_birth_probe:proxy_liquidity_productive_block,low_txns_5m",
                "realized_pnl_pct": -25.0,
                "max_pnl_pct_seen": 0.0,
            }
        )

    groups = summarize_reason_groups(rows, cfg=_cfg(BIRTH_PROBE_MICRO_CANARY_MIN_GROUP_CAPPED_EV_PCT=0.0))

    group = groups["paper_birth_probe_proxy_low_txns"]
    assert group["avg_pnl"] > 5.0
    assert group["avg_pnl_capped"] < 0.0
    assert group["recommended_micro_enabled"] is False


def test_micro_canary_apply_context_sets_lane() -> None:
    decision = evaluate_birth_probe_micro_canary(
        {},
        ["proxy_liquidity_productive_block", "low_txns_5m"],
        dry_run=True,
        live=False,
        group_stats={
            "paper_birth_probe_proxy_low_txns": {
                "samples": 50,
                "avg_pnl": 6.0,
                "avg_pnl_capped": 6.0,
                "peak100_count": 3,
                "recommended_micro_enabled": True,
            }
        },
        cfg=_cfg(),
    )
    token: dict[str, object] = {}

    apply_birth_probe_micro_canary_context(token, decision)

    assert token["entry_lane"] == "pump_early_birth_probe_micro_canary"
    assert token["gate_profile"] == "birth_probe_micro_canary"
    assert token["birth_probe_micro_canary_amount_sol"] == 0.01


def test_micro_canary_exit_ladder_and_time_stop() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = replace(
        exit_policy.CFG,
        DRY_RUN=True,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=True,
        BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED=True,
        BIRTH_PROBE_MICRO_CANARY_TP1_PCT=25.0,
        BIRTH_PROBE_MICRO_CANARY_TP1_FRACTION=0.50,
        BIRTH_PROBE_MICRO_CANARY_TP2_PCT=100.0,
        BIRTH_PROBE_MICRO_CANARY_TP2_FRACTION=0.30,
        BIRTH_PROBE_MICRO_CANARY_MOONBAG_FRACTION=0.20,
        BIRTH_PROBE_MICRO_CANARY_TIME_STOP_MIN=3.0,
        BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_EXIT_MIN=2.0,
        BIRTH_PROBE_MICRO_CANARY_NO_EXPANSION_MIN_PNL=5.0,
    )
    subject = SimpleNamespace(
        entry_lane="pump_early_birth_probe_micro_canary",
        entry_regime="pump_early",
        dry_run=True,
        entry_qty=1000,
        qty=1000,
        realized_qty=0,
        partial_taken=False,
        opened_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1),
        buy_price_usd=1.0,
        highest_pnl_pct=25.0,
    )
    try:
        assert exit_policy.partial_sell_fraction(subject, 25.0) == pytest.approx(0.50)
        subject.opened_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2, seconds=10)
        subject.highest_pnl_pct = 3.0
        assert exit_policy.should_exit(subject, price_now=1.02, now=dt.datetime.now(dt.timezone.utc), pnl_pct=2.0) == "BIRTH_PROBE_NO_EXPANSION"
    finally:
        exit_policy.CFG = original_cfg


def test_micro_canary_moonshot_ladder_preserves_runner_tail() -> None:
    original_cfg = exit_policy.CFG
    exit_policy.CFG = replace(
        exit_policy.CFG,
        DRY_RUN=True,
        BIRD_RUNNER_MULTI_PARTIAL_ENABLED=True,
        BIRD_RUNNER_MULTI_PARTIAL_PAPER_ENABLED=True,
        BIRTH_PROBE_MICRO_CANARY_TP1_PCT=25.0,
        BIRTH_PROBE_MICRO_CANARY_TP1_FRACTION=0.15,
        BIRTH_PROBE_MICRO_CANARY_TP2_PCT=100.0,
        BIRTH_PROBE_MICRO_CANARY_TP2_FRACTION=0.20,
        BIRTH_PROBE_MICRO_CANARY_TP3_PCT=300.0,
        BIRTH_PROBE_MICRO_CANARY_TP3_FRACTION=0.20,
        BIRTH_PROBE_MICRO_CANARY_TP4_PCT=700.0,
        BIRTH_PROBE_MICRO_CANARY_TP4_FRACTION=0.15,
        BIRTH_PROBE_MICRO_CANARY_MOONBAG_FRACTION=0.30,
    )
    subject = SimpleNamespace(
        entry_lane="pump_early_birth_probe_micro_canary",
        entry_regime="pump_early",
        dry_run=True,
        entry_qty=1000,
        qty=1000,
        realized_qty=0,
        partial_taken=False,
        highest_pnl_pct=700.0,
    )
    try:
        plan = exit_policy.partial_ladder_plan(subject, 700.0)
        assert plan["target_secured_fraction"] == pytest.approx(0.70)
        assert exit_policy.partial_sell_fraction(subject, 700.0) == pytest.approx(0.70)
    finally:
        exit_policy.CFG = original_cfg


def test_micro_canary_report_writes_files(tmp_path) -> None:
    metrics = tmp_path / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "candidate_outcomes.jsonl").write_text(
        json.dumps(
            {
                "reason": "green_sniper:paper_birth_probe:proxy_liquidity_productive_block,low_txns_5m",
                "realized_pnl_pct": 12.0,
                "max_pnl_pct_seen": 150.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = write_birth_probe_micro_canary_report(tmp_path)

    assert "reason_groups" in report
    assert (metrics / "birth_probe_micro_canary_report.json").exists()
    assert (tmp_path / "docs" / "BIRTH_PROBE_MICRO_CANARY.md").exists()
