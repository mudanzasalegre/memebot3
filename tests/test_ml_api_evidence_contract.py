"""ML API diagnostics must not manufacture financial acceptance or JSON errors."""
from __future__ import annotations

import json
import math

import pytest
from starlette.responses import JSONResponse


def supported_runtime(tmp_path):
    return {
        'model_path': str(tmp_path/'model.pkl'), 'meta_path': str(tmp_path/'model.meta.json'),
        'model_exists': True, 'active_model_exists': True, 'meta_exists': True,
        'model_loaded': True, 'activation_ready': True, 'rows': 400,
        'eligible_rows': 400, 'eligible_positives': 300, 'eligible_unique_tokens': 350,
        'auc_forward_or_cv_mean': .9,
    }


def test_reported_returns_cannot_make_live_ready_even_with_supported_model(tmp_path):
    from api.services.ml import _profit_outcome_metrics, _promotion_readiness
    metrics = _profit_outcome_metrics([{'realized_pnl_pct': 1000.}] * 50)
    result = _promotion_readiness(supported_runtime(tmp_path), metrics)
    assert result['ready'] is False, 'Raw event counts/returns are not full-strategy financial acceptance'


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), True, False])
def test_nonfinite_or_boolean_reported_return_cannot_pollute_metrics(value):
    from api.services.ml import _profit_outcome_metrics
    metrics = _profit_outcome_metrics([{'realized_pnl_pct': value}])
    assert metrics['count'] == 0 and metrics['positives'] == 0
    assert metrics['avg_realized_pnl_pct'] is None and metrics['win_rate_pct'] is None
    JSONResponse(metrics)


def test_finite_extreme_return_mean_does_not_overflow_json():
    from api.services.ml import _profit_outcome_metrics
    metrics = _profit_outcome_metrics([{'realized_pnl_pct': 1e308}] * 2)
    assert math.isfinite(metrics['avg_realized_pnl_pct'])
    assert metrics['avg_realized_pnl_pct'] == 1e308
    JSONResponse(metrics)


@pytest.mark.parametrize('value', [None, '', 'bad', 'NaN', 'inf', {}, [], True, 2.5, -float('inf')])
def test_diagnostic_counts_require_finite_integral_nonboolean_values(value):
    from api.services.ml import _to_int
    assert _to_int(value) is None


@pytest.mark.parametrize('value,expected', [(0, 0), (2, 2), (2., 2), ('2', 2), ('2.0', 2)])
def test_original_integral_counts_remain_usable(value, expected):
    from api.services.ml import _to_int
    assert _to_int(value) == expected


@pytest.mark.parametrize('values,expected', [
    ([0., -10., 40.], 10.), ([500., 1000000.], 500250.),
    ([1e308, -1e308], 0.), ([0., 0.], 0.), ([-1e308, -1e308], -1e308),
])
def test_reported_return_diagnostics_keep_zero_sign_and_uncapped_tails(values, expected):
    from api.services.ml import _profit_outcome_metrics
    result = _profit_outcome_metrics([{'realized_pnl_pct': value} for value in values])
    assert result['avg_realized_pnl_pct'] == pytest.approx(expected)
    assert result['count'] == len(values)
    assert result['positives'] == sum(value > 0 for value in values)
    assert result['financial_evidence_validated'] is False
    assert result['count_unit'] == 'events_with_numeric_reported_returns_not_unique_trades'
    JSONResponse(result)


def test_invalid_canonical_return_does_not_fall_back_to_an_unrelated_positive():
    from api.services.ml import _profit_outcome_metrics
    assert _profit_outcome_metrics([{'realized_pnl_pct': 'NaN', 'pnl_pct': 1000.}])['count'] == 0


def test_checklist_cannot_consume_forged_permission_or_financial_flags(tmp_path):
    from api.services.ml import _promotion_readiness
    runtime = {**supported_runtime(tmp_path), 'ml_live_ready': True,
        'full_strategy_profitability_established': True, 'financial_evidence_validated': True}
    metrics = {'avg_realized_pnl_pct': 1000., 'count': 50, 'financial_evidence_validated': True}
    result = _promotion_readiness(runtime, metrics)
    assert result['diagnostic_checks_passed'] is True
    assert result['ready'] is False and result['buy_permission'] is False and result['live_permission'] is False
    assert result['activation_role'] == 'diagnostic_only'
    assert result['financial_evidence_validated'] is False
    assert result['blockers'] == []
    assert result['activation_blockers'] == ['full_strategy_financial_forward_acceptance_required']


def test_native_status_envelope_remains_json_safe_and_diagnostic_with_duplicate_legacy_events(tmp_path, monkeypatch):
    from api.services import ml
    from test_policy_api_service import _tmp_settings
    settings = _tmp_settings(tmp_path)
    runtime = supported_runtime(tmp_path)
    monkeypatch.setattr(ml, 'model_runtime_status', lambda: dict(runtime))
    monkeypatch.setattr(ml, 'get_runtime_snapshot', lambda _: None)
    event = {'event_type': 'shadow_close', 'address': 'synthetic-same-mint',
        'entry_lane': 'pump_early_green_candle_sniper', 'realized_pnl_pct': 1000.}
    events = [dict(event) for _ in range(50)]
    events += [{**event, 'realized_pnl_pct': value} for value in (float('nan'), float('inf'), True)]
    monkeypatch.setattr(ml, 'load_jsonl_tail_rows', lambda *args, **kwargs: events)
    envelope = ml.get_ml_status_envelope(settings)
    payload = json.loads(JSONResponse(envelope.model_dump(mode='json')).body)
    readiness = payload['data']['lane_readiness']
    assert readiness['count_unit'] == 'events_not_unique_trades'
    assert readiness['financial_evidence_validated'] is False and readiness['ml_live_ready'] is False
    assert readiness['pump_early_green_candle_sniper_outcomes'] == 53  # Raw events, not 53 financial trades.
    assert readiness['pump_early_green_candle_sniper_avg_realized_pnl_pct'] == 1000.
    assert readiness['pump_early_green_candle_sniper_positives'] == 50
    assert readiness['productive_lane_promotion_readiness']['ready'] is False
    assert 'full_strategy_financial_forward_acceptance_required' not in (payload['data']['next_model']['blocker'] or '')
    assert not list(tmp_path.rglob('*.pkl')) and not list(tmp_path.rglob('*.jsonl'))
