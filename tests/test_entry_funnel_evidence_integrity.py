from __future__ import annotations

import json
from pathlib import Path

import pytest

from analytics.entry_funnel_blockers_report import build_entry_funnel_blockers_report


def _report(tmp_path: Path, rows: list[dict]):
    path = tmp_path / 'data/metrics/runtime_events.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    return build_entry_funnel_blockers_report(tmp_path)


def test_missing_outcome_is_not_zero_return_or_proven_counterfactual(tmp_path):
    report = _report(tmp_path, [{'event_type': 'blocked_before_buy', 'address': 'A',
        'reason': 'rank_below_min,no_route'}])
    row = report['blocker_details']['rank_below_min']
    assert row['avg_shadow_pnl'] is None
    assert row['max_shadow_peak'] is None
    assert row['would_pass_if_relaxed'] is None
    assert row['eligibility_effect_established'] is False
    assert row['recommended_action'] == 'needs_counterfactual_evaluation'
    assert row['sample_tokens'][0]['pnl_pct'] is None
    assert row['sample_tokens'][0]['peak_pct'] is None


@pytest.mark.parametrize('value', [None, '', 'bad', True, False, float('nan'), float('inf'), -float('inf')])
def test_invalid_or_absent_shadow_measurements_remain_unknown(tmp_path, value):
    row = _report(tmp_path, [{'event_type': 'candidate_outcome', 'address': 'A',
        'reason': 'soft_score', 'shadow_kind': 'entry_shadow',
        'shadow_pnl_pct': value, 'observed_peak_after_seen': value}])['blocker_details']['soft_score']
    assert row['avg_shadow_pnl'] is None
    assert row['max_shadow_peak'] is None
    assert row['observed_shadow_pnl_rows'] == 0
    assert row['observed_shadow_peak_rows'] == 0


@pytest.mark.parametrize('value', [0.0, -100.0, 125.0, 1000000.0])
def test_explicit_finite_shadow_values_keep_zero_sign_and_extreme_tail(tmp_path, value):
    row = _report(tmp_path, [{'event_type': 'candidate_outcome', 'address': 'A',
        'reason': 'rank_below_min', 'shadow_kind': 'entry_shadow',
        'shadow_pnl_pct': value, 'observed_peak_after_seen': value}])['blocker_details']['rank_below_min']
    assert row['avg_shadow_pnl'] == value
    assert row['max_shadow_peak'] == value
    assert row['observed_shadow_pnl_rows'] == row['observed_shadow_peak_rows'] == 1
    assert row['would_pass_if_relaxed'] is None
    assert row['financial_evidence'] is False


def test_observed_zero_is_not_replaced_by_other_return_or_peak_alias(tmp_path):
    row = _report(tmp_path, [{'event_type': 'candidate_outcome', 'address': 'A',
        'reason': 'soft_score', 'shadow_kind': 'entry_shadow', 'shadow_pnl_pct': 0.0,
        'pnl_pct': 999.0, 'observed_peak_after_seen': 0.0, 'peak_pnl_pct': 999.0}])['blocker_details']['soft_score']
    assert row['avg_shadow_pnl'] == row['max_shadow_peak'] == 0.0


def test_realized_or_target_values_do_not_masquerade_as_shadow_returns(tmp_path):
    row = _report(tmp_path, [
        {'event_type': 'blocked_before_buy', 'address': 'A', 'reason': 'soft_score',
            'target_total_pnl_pct': 1000.0, 'highest_pnl_pct': 2000.0},
        {'event_type': 'candidate_outcome', 'address': 'B', 'reason': 'soft_score',
            'realized_pnl_pct': 100.0, 'peak_pnl_pct': 1000.0},
    ])['blocker_details']['soft_score']
    assert row['avg_shadow_pnl'] is None
    assert row['max_shadow_peak'] is None
    assert row['observed_shadow_pnl_rows'] == 0
    assert row['recommended_action'] == 'needs_counterfactual_evaluation'


def test_ending_shadow_return_does_not_fabricate_observed_peak(tmp_path):
    row = _report(tmp_path, [{'event_type': 'candidate_outcome', 'address': 'A',
        'reason': 'soft_score', 'shadow_kind': 'entry_shadow', 'shadow_pnl_pct': 15.0}])['blocker_details']['soft_score']
    assert row['avg_shadow_pnl'] == 15.0
    assert row['max_shadow_peak'] is None


@pytest.mark.parametrize('blocker', ['rank_below_min', 'proxy_liquidity', 'momentum_ignition_needs_confirmation'])
def test_large_peak_does_not_recommend_probe_or_paper_without_full_evaluation(tmp_path, blocker):
    row = _report(tmp_path, [{'event_type': 'candidate_outcome', 'address': 'A',
        'reason': blocker + ',no_route', 'shadow_kind': 'entry_shadow',
        'shadow_pnl_pct': 500.0, 'observed_peak_after_seen': 1000000.0}])['blocker_details'][blocker]
    assert row['recommended_action'] == 'needs_counterfactual_evaluation'
    assert row['would_pass_if_relaxed'] is None
    assert row['financial_evidence'] is False


def test_row_and_token_grains_are_explicit_and_missing_identity_is_not_a_token(tmp_path):
    row = _report(tmp_path, [
        {'event_type': 'blocked_before_buy', 'address': 'A', 'reason': 'soft_score'},
        {'event_type': 'blocked_before_buy', 'address': 'A', 'reason': 'soft_score'},
        {'event_type': 'blocked_before_buy', 'reason': 'soft_score'},
    ])['blocker_details']['soft_score']
    assert row['count'] == row['distinct_tokens'] == 1
    assert row['matched_rows'] == 3
    assert row['unidentified_rows'] == 1


@pytest.mark.parametrize('reason', ['not_rank_below_min', 'rank_below_minimum', 'rank_below_min_disabled'])
def test_blocker_match_is_not_an_arbitrary_substring(tmp_path, reason):
    row = _report(tmp_path, [{'event_type': 'blocked_before_buy', 'address': 'A',
        'reason': reason}])['blocker_details']['rank_below_min']
    assert row['count'] == 0


def test_empty_population_keeps_missing_measures_and_zero_population(tmp_path):
    row = _report(tmp_path, [])['blocker_details']['soft_score']
    assert row['count'] == row['matched_rows'] == row['distinct_tokens'] == 0
    assert row['avg_shadow_pnl'] is None
    assert row['max_shadow_peak'] is None
    assert row['recommended_action'] == 'needs_more_data'


@pytest.mark.parametrize('blocker', ['untagged_buy_blocked', 'toxic_initial_sell_pressure'])
def test_known_safety_block_is_never_relaxed_by_peak(tmp_path, blocker):
    row = _report(tmp_path, [{'event_type': 'candidate_outcome', 'address': 'A',
        'reason': blocker, 'shadow_kind': 'entry_shadow',
        'observed_peak_after_seen': 1000000.0}])['blocker_details'][blocker]
    assert row['recommended_action'] == 'keep_block'
    assert row['would_pass_if_relaxed'] is None


def test_actual_birth_snapshot_and_signal_blockers_are_not_invisible(tmp_path):
    report = _report(tmp_path, [
        {'event_type': 'blocked_before_buy', 'address': 'A', 'reason': 'entry_observation:missing_age'},
        {'event_type': 'blocked_before_buy', 'address': 'B', 'reason': 'entry_observation:snapshot_unavailable'},
        {'event_type': 'blocked_before_buy', 'address': 'C', 'reason': 'green_sniper:low_green_momentum,low_txns_5m'},
    ])
    for key in ('missing_age', 'snapshot_unavailable', 'low_green_momentum', 'low_txns_5m'):
        assert report['blocker_details'][key]['distinct_tokens'] == 1
    assert report['blocking_observation_coverage'] == {
        'runtime_blocked_rows': 3, 'runtime_blocked_distinct_tokens': 3,
        'recognized_rows': 3, 'unrecognized_rows': 0,
        'qualification': 'Reason groups overlap; recognized does not mean causally explained or financially evaluated.',
    }


def test_unknown_blocker_is_retained_as_unexplained_coverage_not_discarded(tmp_path):
    report = _report(tmp_path, [
        {'event_type': 'blocked_before_buy', 'address': 'A', 'reason': 'new_unrecognized_gate'},
        {'event_type': 'blocked_before_buy', 'address': 'A', 'reason': 'entry_observation:missing_age'},
    ])
    coverage = report['blocking_observation_coverage']
    assert coverage['runtime_blocked_rows'] == 2
    assert coverage['runtime_blocked_distinct_tokens'] == 1
    assert coverage['recognized_rows'] == coverage['unrecognized_rows'] == 1
