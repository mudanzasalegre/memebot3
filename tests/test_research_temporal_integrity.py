"""Synthetic decision clocks only; no provider, trade or profit evidence."""
import datetime as dt
from types import SimpleNamespace

import pytest

from analytics import token_time
from analytics.sniper_research_subprofiles import (
    evaluate_sniper_research_subprofile, momentum_trend_missing_strong_reasons,
)
from research_loop import entry_gate_policy as policy
from test_paper_bootstrap import _decision, _row
from test_sniper_research_subprofiles import _cfg
from test_entry_gate_forward import isolated

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("row", [{"age_minutes": 1}, {"age_min": 0}, {"token_age_min": 0},
    {"queue_age_minutes": True}, {"queue_age_minutes": False}, {"queue_age_minutes": -1}])
def test_native_momentum_does_not_forge_queue_strength(row):
    assert "strong_queue_age" not in momentum_trend_missing_strong_reasons(row, cfg=_cfg())


@pytest.mark.parametrize("field", ["age_minutes", "queue_age_minutes"])
@pytest.mark.parametrize("value", [True, False])
def test_native_bootstrap_does_not_accept_boolean_temporal_evidence(field, value):
    result = _decision(row={**_row(), "age_minutes": 1, "queue_age_minutes": 1, field: value},
        cfg=SimpleNamespace(PAPER_BOOTSTRAP_MAX_AGE_MIN=5, PAPER_BOOTSTRAP_MAX_QUEUE_AGE_MIN=5))
    assert not result.allowed


def test_native_bootstrap_accepts_measured_zero_age():
    assert _decision(row={**_row(), "age_minutes": 0}, cfg=SimpleNamespace(PAPER_BOOTSTRAP_MAX_AGE_MIN=5)).allowed


@pytest.mark.parametrize("row", [{}, {"age_minutes": 1}, {"age_min": 0},
    {"token_age_min": 0}, {"queue_age_minutes": True}, {"queue_age_minutes": -1},
    {"queue_age_minutes": float("nan")}, {"queue_age_minutes": float("inf")},
    {"first_seen_at": True}, {"first_seen_at": T0 + dt.timedelta(seconds=1)}])
def test_unknown_queue_does_not_become_strong_momentum(row):
    assert "strong_queue_age" not in momentum_trend_missing_strong_reasons(row, cfg=_cfg(), now=T0)


@pytest.mark.parametrize("row", [{"queue_age_minutes": 0}, {"queue_age_minutes": "0"},
    {"minutes_since_first_seen": 3}, {"first_seen_at": T0 - dt.timedelta(minutes=3)},
    {"first_seen_epoch_s": (T0 - dt.timedelta(minutes=3)).timestamp()}])
def test_observed_queue_is_strong_without_borrowing_birth(row):
    assert "strong_queue_age" in momentum_trend_missing_strong_reasons({"age_minutes": 500, **row}, cfg=_cfg(), now=T0)


@pytest.mark.parametrize("factor", [1, 1000, 1000000, 1000000000])
def test_original_queue_clock_has_priority_and_supports_epoch_units(factor):
    row = {"first_seen_epoch_s": int((T0 - dt.timedelta(minutes=8)).timestamp()) * factor,
           "queue_age_minutes": 0, "age_minutes": 0}
    assert token_time.compute_queue_age_minutes(row, now=T0) == 8


@pytest.mark.parametrize("row", [{"queue_age_minutes": True}, {"queue_age_minutes": []},
    {"queue_age_minutes": -1}, {"first_seen_at": T0 + dt.timedelta(seconds=1), "queue_age_minutes": 0}])
def test_queue_invalid_or_future_evidence_is_not_fabricated_zero(row):
    assert token_time.compute_queue_age_minutes(row, now=T0) is None


def test_momentum_without_trend_cannot_use_birth_as_queue_confirmation():
    row = {"entry_lane": "pump_early_sniper_research", "price_pct_5m": 140,
           "liquidity_usd": 16000, "txns_last_5m": 600, "market_cap_usd": 55000,
           "has_jupiter_route": True, "trend": "unknown", "age_minutes": 1}
    blocked = evaluate_sniper_research_subprofile(row, cfg=_cfg(), now=T0)
    assert not blocked.allowed and blocked.reason == "momentum_ignition_needs_confirmation"
    observed = evaluate_sniper_research_subprofile({**row, "queue_age_minutes": 0}, cfg=_cfg(), now=T0)
    assert observed.allowed
    strong = evaluate_sniper_research_subprofile({**row, "txns_last_5m": 1500}, cfg=_cfg(), now=T0)
    assert strong.allowed  # Other genuinely strong observations still count.


@pytest.mark.parametrize("value", [0, "0"])
def test_bootstrap_measured_zero_is_known_when_age_is_capped(value):
    result = _decision(row={**_row(), "age_minutes": value}, cfg=SimpleNamespace(PAPER_BOOTSTRAP_MAX_AGE_MIN=5), now=T0)
    assert result.allowed


@pytest.mark.parametrize("field", ["age_minutes", "queue_age_minutes"])
@pytest.mark.parametrize("value", [True, False, -1, float("nan"), float("inf"), []])
def test_bootstrap_capped_temporal_field_requires_valid_observation(field, value):
    result = _decision(row={**_row(), "age_minutes": 1, "queue_age_minutes": 1, field: value},
        cfg=SimpleNamespace(PAPER_BOOTSTRAP_MAX_AGE_MIN=5, PAPER_BOOTSTRAP_MAX_QUEUE_AGE_MIN=5), now=T0)
    assert not result.allowed
    assert ("age_missing" if field == "age_minutes" else "queue_age_missing") in result.hard_failures


@pytest.mark.parametrize("factor", [1, 1000, 1000000, 1000000000])
def test_bootstrap_birth_is_measured_at_original_decision_clock(factor):
    row = {**_row(), "created_at": int((T0 - dt.timedelta(minutes=2)).timestamp()) * factor, "age_minutes": 99}
    result = _decision(row=row, cfg=SimpleNamespace(PAPER_BOOTSTRAP_MAX_AGE_MIN=5), now=T0)
    assert result.allowed and row["age_minutes"] == 99


def test_bootstrap_future_birth_cannot_fall_back_to_plausible_measured_age():
    result = _decision(row={**_row(), "created_at": T0 + dt.timedelta(seconds=1), "age_minutes": 1},
        cfg=SimpleNamespace(PAPER_BOOTSTRAP_MAX_AGE_MIN=5), now=T0)
    assert not result.allowed and "age_missing" in result.hard_failures


def test_disabled_bootstrap_age_caps_do_not_create_an_extra_veto():
    assert _decision(cfg=SimpleNamespace(PAPER_BOOTSTRAP_MAX_AGE_MIN=0, PAPER_BOOTSTRAP_MAX_QUEUE_AGE_MIN=0), now=T0).allowed


@pytest.mark.parametrize("gate", ["rank_canary", "sniper_subprofile"])
def test_frozen_component_replay_passes_original_clock_to_actual_gate(monkeypatch, gate):
    from analytics import research_rank_canary as rank, sniper_research_subprofiles as sniper
    seen = []
    module, name = ((rank, "evaluate_research_rank_canary") if gate == "rank_canary"
                    else (sniper, "evaluate_sniper_research_subprofile"))
    def probe(*args, **kwargs):
        seen.append(kwargs.get("now"))
        return SimpleNamespace(allowed=True, reason="synthetic")
    monkeypatch.setattr(module, name, probe)
    assert policy.profile_decision(gate, {}, SimpleNamespace(DRY_RUN=True), {}, now=T0)
    assert seen == [T0]


def test_original_bootstrap_replay_uses_event_time_not_today(tmp_path):
    from backtest.event_replay import build_event_replay
    from test_event_replay import _write_jsonl
    metrics = tmp_path / "data/metrics"
    _write_jsonl(metrics / "candidate_outcomes.jsonl", [
        {**_row(), "txns_last_5m": 600, "ts_utc": T0.isoformat(), "created_at": (T0 - dt.timedelta(minutes=2)).isoformat(),
         "first_seen_at": (T0 - dt.timedelta(minutes=1)).isoformat(),
         "event_type": "candidate_decision", "decision_action": "bought",
         "entry_lane": "pump_early_paper_bootstrap_micro"},
        {"address": _row()["address"], "event_type": "candidate_outcome",
         "ts_utc": (T0 + dt.timedelta(minutes=5)).isoformat(), "pnl_pct": 10},
    ])
    _write_jsonl(metrics / "runtime_events.jsonl", [])
    report = build_event_replay(tmp_path, candidate_config={
        "PAPER_BOOTSTRAP_MAX_AGE_MIN": 5, "PAPER_BOOTSTRAP_MAX_QUEUE_AGE_MIN": 5,
        "PAPER_BOOTSTRAP_MAX_OPEN": 0, "PAPER_BOOTSTRAP_MAX_DAILY_BUYS": 0,
        "PAPER_BOOTSTRAP_MAX_HOURLY_BUYS": 0, "PAPER_BOOTSTRAP_MIN_SECONDS_BETWEEN_BUYS": 0,
    }, replay_assumptions={"EVENT_REPLAY_LATENCY_SECONDS": 0, "EVENT_REPLAY_SLIPPAGE_BPS": 0})
    assert report["metrics"]["simulated_buys"] == 1


def test_prospective_registration_passes_one_frozen_clock_to_all_arms(tmp_path, monkeypatch):
    from test_entry_gate_forward import capture, config
    from research_loop import entry_gate_forward as bank
    real, seen = policy.profile_decision, []
    def probe(*args, **kwargs):
        seen.append(kwargs.get("now"))
        return real(*args, **kwargs)
    monkeypatch.setattr(policy, "profile_decision", probe)
    identity = capture(tmp_path, config(), now=T0)
    assert identity and seen == [T0, T0, T0]
    assert bank.directory(tmp_path).is_relative_to(tmp_path)


def test_actual_component_decision_is_reproducible_at_t0_not_wall_clock():
    row = {"entry_lane": "pump_early_sniper_research", "price_pct_5m": 140,
           "liquidity_usd": 16000, "txns_last_5m": 600, "market_cap_usd": 55000,
           "has_jupiter_route": True, "trend": "unknown",
           "first_seen_at": (T0 - dt.timedelta(minutes=3)).isoformat()}
    assert policy.profile_decision("sniper_subprofile", row, _cfg(), {}, now=T0)
    assert not policy.profile_decision("sniper_subprofile", row, _cfg(), {}, now=T0 + dt.timedelta(days=1))
    assert policy.profile_decision("sniper_subprofile", row, _cfg(), {}, now=T0)


def test_original_complete_cohort_revalidates_each_case_at_its_t0(tmp_path, monkeypatch):
    from test_entry_gate_forward import complete, config
    from research_loop import entry_gate_forward as bank, forward_budget as store
    cfg = config(SNIPER_RESEARCH_MOMENTUM_MIN_PRICE5M=100,
        SNIPER_RESEARCH_MOMENTUM_MAX_PRICE5M=150, SNIPER_RESEARCH_MOMENTUM_MIN_TXNS_5M=500,
        SNIPER_RESEARCH_MOMENTUM_MIN_LIQUIDITY_USD=15000, SNIPER_RESEARCH_MOMENTUM_MAX_MCAP_USD=70000)
    base = bank.directory(tmp_path)
    store.write(base / "proposal_cursor.json", {"index": 1, "component_indices": {}})
    identity, now = complete(tmp_path, cfg, gate="sniper_subprofile", features_func=lambda i: {
        "price_pct_5m": 200 if i < 30 else 120, "trend": "unknown", "liquidity_usd": 16000,
        "market_cap_usd": 55000, "first_seen_at": (T0 + dt.timedelta(minutes=15*i-3)).isoformat(),
    })
    real, seen = policy.profile_decision, []
    def probe(*args, **kwargs):
        seen.append(kwargs.get("now"))
        return real(*args, **kwargs)
    monkeypatch.setattr(policy, "profile_decision", probe)
    result = bank.evaluate_plan(tmp_path, cfg, identity, now=now)
    assert result["accepted"]  # Synthetic costed fixture, not actual profitability.
    assert seen == [T0 + dt.timedelta(minutes=15*i) for i in range(50) for _ in range(3)]
