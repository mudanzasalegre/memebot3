"""Synthetic temporal contracts, not evidence of actual trading profits."""
import datetime as dt
from types import SimpleNamespace

import pytest

from analytics import moonshot_micro_lottery as moon, shadow_followup_micro as shadow, token_time
from research_loop import entry_gate_policy as gates
from test_entry_gate_forward import isolated  # noqa: F401


T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("bad", [None, True, False, -1, "-1"])
def test_native_moonshot_api_cannot_use_unobserved_queue(bad):
    row = hot(age_minutes=2, queue_age_minutes=bad)
    assert not decision(row).allowed


@pytest.mark.parametrize("bad", [True, False, -1, "-1"])
def test_native_shadow_api_rejects_invalid_elapsed_observation(bad):
    assert shadow._trigger({"shadow_pnl_pct": 30, "minutes_since_first_seen": bad}) is None


def test_native_shadow_api_does_not_use_purchase_as_discovery():
    assert shadow._trigger({"shadow_pnl_pct": 30,
                           "opened_at": dt.datetime.now(dt.timezone.utc).isoformat()}) is None


def hot(**changes):
    return {"source": "pumpfun", "market_cap_usd": 80000, "price_pct_5m": 1000,
            "txns_last_5m": 320, "age_minutes": 200, "queue_age_minutes": 2,
            "has_jupiter_route": False, **changes}


def decision(row, **changes):
    return moon.evaluate_moonshot_micro_lottery(row, dry_run=True, live=False,
                                              cfg=SimpleNamespace(), **changes)


@pytest.mark.parametrize("move", [500, 1000, 5000, 10000, 100000])
def test_extreme_fresh_queue_does_not_gain_a_token_birth_veto(move):
    assert decision(hot(price_pct_5m=move), now=T0).allowed


@pytest.mark.parametrize("bad", [None, "", "unknown", True, False, -1, "-1", float("inf"), float("nan"), [], {}])
def test_missing_or_invalid_queue_cannot_borrow_birth_age(bad):
    result = decision(hot(age_minutes=2, queue_age_minutes=bad), now=T0)
    assert not result.allowed and "queue_age_missing" in result.failures


def test_original_stale_queue_dominates_small_measured_age():
    result = decision(hot(age_minutes=2, first_seen_at=(T0-dt.timedelta(minutes=11)).isoformat()), now=T0)
    assert not result.allowed and "age_gt_10m" in result.failures


def test_future_queue_is_unknown_not_newborn():
    result = decision(hot(first_seen_at=(T0+dt.timedelta(seconds=1)).isoformat()), now=T0)
    assert not result.allowed and "queue_age_missing" in result.failures


def test_original_fresh_queue_recovers_signal_without_measured_age():
    assert decision(hot(queue_age_minutes=None, first_seen_at=(T0-dt.timedelta(minutes=2)).isoformat()), now=T0).allowed


@pytest.mark.parametrize("probe", [moon._birth_velocity_probe, moon._late_proxy_momentum_probe, moon._cluster_tail_probe])
def test_birth_probe_cannot_borrow_queue_freshness(probe):
    row = hot(age_minutes=None, price_pct_5m=90, txns_last_5m=33, market_cap_usd=4600,
              volume_24h_usd=1500, reason="paper_birth_probe")
    if probe is moon._late_proxy_momentum_probe:
        row.update(price_pct_5m=685, txns_last_5m=30, market_cap_usd=18700)
    if probe is moon._cluster_tail_probe:
        row.update(cluster_bad=True, price_pct_5m=35, txns_last_5m=25,
                   liquidity_usd=22000, market_cap_usd=97000, volume_24h_usd=33070)
    assert not probe(row, cfg=SimpleNamespace(), now=T0)
    row["created_at"] = (T0-dt.timedelta(minutes=1)).isoformat()
    assert probe(row, cfg=SimpleNamespace(), now=T0)
    row["created_at"] = (T0-dt.timedelta(minutes=20)).isoformat()
    row["age_minutes"] = 1
    assert not probe(row, cfg=SimpleNamespace(), now=T0)


@pytest.mark.parametrize("bad", [None, "", True, False, -1, "-1", float("inf"), float("nan"), [], {}])
def test_shadow_time_trigger_requires_real_nonnegative_observation(bad):
    result = shadow.evaluate_shadow_followup_micro({"shadow_pnl_pct": 30,
        "minutes_since_first_seen": bad, "market_cap_usd": 70000, "has_jupiter_route": True}, now=T0)
    assert not result.allowed and "no_followup_trigger" in result.failures


def test_shadow_first_seen_clock_dominates_stale_measured_residence():
    row = {"shadow_pnl_pct": 30, "minutes_since_first_seen": 2,
           "first_seen_at": (T0-dt.timedelta(minutes=7)).isoformat()}
    assert shadow._trigger(row, now=T0) is None
    row["first_seen_at"] = (T0-dt.timedelta(minutes=2)).isoformat()
    assert shadow._trigger(row, now=T0) == "shadow_pnl_25_within_3m"


def test_shadow_opened_clock_and_future_first_seen_cannot_fake_observation():
    assert shadow._trigger({"shadow_pnl_pct": 30, "opened_at": T0.isoformat()}, now=T0) is None
    assert shadow._trigger({"shadow_pnl_pct": 30, "minutes_since_first_seen": 2,
        "first_seen_at": (T0+dt.timedelta(seconds=1)).isoformat()}, now=T0) is None


def test_shadow_observed_birth_age_at_first_seen_is_not_current_age():
    row = {"observed_peak_after_seen": 60, "created_at": (T0-dt.timedelta(minutes=20)).isoformat(),
           "first_seen_at": (T0-dt.timedelta(minutes=19)).isoformat()}
    assert shadow._trigger(row, now=T0) == "observed_peak_after_seen_50"


@pytest.mark.parametrize("gate", ["moonshot", "late_momentum"])
def test_actual_research_components_receive_case_clock(monkeypatch, gate):
    from analytics import late_momentum_watch as late
    module, name = (moon, "evaluate_moonshot_micro_lottery") if gate == "moonshot" else (late, "evaluate_late_momentum_watch")
    seen = []
    def probe(*args, **kwargs):
        seen.append(kwargs.get("now"))
        return SimpleNamespace(allowed=True, action="buy")
    monkeypatch.setattr(module, name, probe)
    assert gates.profile_decision(gate, {}, SimpleNamespace(DRY_RUN=True), {}, now=T0)
    assert seen == [T0]


def test_historical_view_freezes_ages_without_mutating_original():
    row = hot(ts_utc=T0.isoformat(), created_at=(T0-dt.timedelta(minutes=200)).isoformat(),
              first_seen_at=(T0-dt.timedelta(minutes=2)).isoformat(), queue_age_minutes=500)
    before = dict(row)
    view = token_time.historical_age_snapshot(row)
    assert view["age_minutes"] == 200 and view["queue_age_minutes"] == 2
    assert "created_at" not in view and "first_seen_at" not in view and row == before
    assert decision(view).allowed


@pytest.mark.parametrize("not_decision_clock", ["opened_at", "updated_at_utc", "closed_at"])
def test_missing_event_clock_does_not_recompute_historical_ages_today(not_decision_clock):
    row = hot(created_at=(T0-dt.timedelta(minutes=1)).isoformat(),
              first_seen_at=(T0-dt.timedelta(minutes=1)).isoformat(), **{not_decision_clock: T0.isoformat()})
    view = token_time.historical_age_snapshot(row)
    assert view["age_minutes"] is None and view["queue_age_minutes"] is None
    assert not decision(view).allowed


def test_original_moonshot_event_replay_uses_event_time(tmp_path, monkeypatch):
    from backtest.event_replay import build_event_replay
    from test_event_replay import _write_jsonl
    metrics = tmp_path / "data/metrics"
    row = hot(address="So11111111111111111111111111111111111111112", age_minutes=None, queue_age_minutes=None,
              has_jupiter_route=True,
              ts_utc=T0.isoformat(), created_at=(T0-dt.timedelta(minutes=200)).isoformat(),
              first_seen_at=(T0-dt.timedelta(minutes=2)).isoformat(), event_type="candidate_decision",
              decision_action="bought", entry_lane="pump_early_moonshot_micro_lottery")
    _write_jsonl(metrics / "candidate_outcomes.jsonl", [row, {"address": row["address"],
        "event_type": "candidate_outcome", "ts_utc": (T0+dt.timedelta(minutes=5)).isoformat(), "pnl_pct": 10}])
    _write_jsonl(metrics / "runtime_events.jsonl", [])
    real, observed = moon.evaluate_moonshot_micro_lottery, []
    def probe(row, **kwargs):
        result = real(row, **kwargs)
        observed.append((kwargs.get("now"), result.reason))
        return result
    monkeypatch.setattr(moon, "evaluate_moonshot_micro_lottery", probe)
    monkeypatch.setattr("backtest.event_replay.evaluate_moonshot_micro_lottery", probe)
    report = build_event_replay(tmp_path, replay_assumptions={"EVENT_REPLAY_LATENCY_SECONDS": 0,
                                                          "EVENT_REPLAY_SLIPPAGE_BPS": 0})
    assert report["metrics"]["simulated_buys"] == 1, (observed, report["metrics"])
    assert observed == [(T0, "confirmed_moonshot_buy")]


def test_historical_moonshot_label_uses_original_decision_clock():
    from ml.labels import moonshot_execution_label
    row = hot(ts_utc=T0.isoformat(), created_at=(T0-dt.timedelta(minutes=200)).isoformat(),
              first_seen_at=(T0-dt.timedelta(minutes=2)).isoformat(), queue_age_minutes=None)
    assert moonshot_execution_label(row, cfg=SimpleNamespace())["executable_moonshot"]


def test_historical_shadow_report_does_not_expire_original_signal(monkeypatch, tmp_path):
    row = {"shadow_pnl_pct": 30, "ts_utc": T0.isoformat(), "sample_type": "shadow",
           "first_seen_at": (T0-dt.timedelta(minutes=2)).isoformat(), "market_cap_usd": 70000,
           "has_jupiter_route": True}
    monkeypatch.setattr(shadow, "load_runtime_events", lambda _: [row])
    monkeypatch.setattr(shadow, "load_candidate_outcomes", lambda _: [])
    assert shadow.build_shadow_followup_micro_report(tmp_path)["micro_triggers"] == 1


@pytest.mark.parametrize("queue_age", [0, 10, "0", "10"])
def test_valid_zero_and_queue_boundary_remain_eligible(queue_age):
    assert decision(hot(queue_age_minutes=queue_age), now=T0).allowed


@pytest.mark.parametrize("scale", [1, 1000, 1000000, 1000000000])
def test_original_epoch_units_survive_actual_replay_feature_projection(scale):
    from backtest.event_replay import _entry_visible_row
    row = hot(first_seen_epoch_s=(T0-dt.timedelta(minutes=2)).timestamp()*scale,
              pairCreatedAtMs=(T0-dt.timedelta(minutes=200)).timestamp()*1000,
              queue_age_minutes=None, age_minutes=None)
    visible, ignored = _entry_visible_row(row)
    assert ignored == 0 and decision(visible, now=T0).allowed
    assert token_time.compute_age_minutes(visible, now=T0) == 200


def test_special_confirmed_birth_probe_keeps_its_distinct_age_policy():
    row = hot(age_minutes=1, queue_age_minutes=None, price_pct_5m=90, txns_last_5m=33,
              market_cap_usd=4600, volume_24h_usd=1500, reason="paper_birth_probe",
              observed_shadow_move_pct=80)
    assert decision(row, now=T0).allowed


def test_queue_measurement_cannot_manufacture_shadow_residence():
    assert shadow._trigger({"shadow_pnl_pct": 30, "queue_age_minutes": 2}, now=T0) is None


def test_future_shadow_clock_cannot_be_overridden_by_measured_birth_at_seen():
    assert shadow._trigger({"observed_peak_after_seen": 60, "age_at_seen": 1,
        "first_seen_at": (T0+dt.timedelta(seconds=1)).isoformat()}, now=T0) is None


def test_original_birth_at_seen_dominates_conflicting_measurement():
    row = {"observed_peak_after_seen": 60, "age_at_seen": 1,
           "created_at": (T0-dt.timedelta(minutes=20)).isoformat(),
           "first_seen_at": (T0-dt.timedelta(minutes=1)).isoformat()}
    assert shadow._trigger(row, now=T0) is None


def test_historical_missing_clock_preserves_genuine_measured_age_at_seen():
    view = token_time.historical_age_snapshot({"age_at_seen": 1, "observed_peak_after_seen": 60,
        "first_seen_at": T0.isoformat()})
    assert view["queue_age_minutes"] is None and view["age_at_seen"] == 1
    assert shadow._trigger(view) == "observed_peak_after_seen_50"


def test_original_moonshot_costed_cohort_revalidates_all_arms_at_t0(tmp_path, monkeypatch):
    from test_entry_gate_forward import complete, config
    from research_loop import entry_gate_forward as bank, forward_budget as store
    cfg = config(MOONSHOT_MICRO_LOTTERY_MIN_PRICE5M=300,
                 MOONSHOT_MICRO_LOTTERY_MIN_TXNS_5M=80)
    groups = list(dict.fromkeys(gate for gate, _ in bank.proposals(cfg)))
    store.write(bank.directory(tmp_path)/"proposal_cursor.json",
                {"index": groups.index("moonshot"), "component_indices": {}})
    identity, now = complete(tmp_path, cfg, gate="moonshot", features_func=lambda i: {
        "source": "pumpfun", "price_pct_5m": 280 if i < 30 else 350, "age_minutes": 200,
        "first_seen_at": (T0+dt.timedelta(minutes=15*i-2)).isoformat()})
    real, seen = gates.profile_decision, []
    def probe(*args, **kwargs):
        seen.append(kwargs.get("now"))
        return real(*args, **kwargs)
    monkeypatch.setattr(gates, "profile_decision", probe)
    result = bank.evaluate_plan(tmp_path, cfg, identity, now=now)
    assert result["accepted"]  # Synthetic quoted/costed cells, not actual profitability.
    assert seen == [T0+dt.timedelta(minutes=15*i) for i in range(50) for _ in range(3)]
