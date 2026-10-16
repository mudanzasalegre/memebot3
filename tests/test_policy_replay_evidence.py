"""Heuristic policy replay is diagnostic, never prospective net trade proof."""
from copy import deepcopy
import json
import math

import pytest

from api.services import policy as service
from backtest import policy_replay as replay
from test_policy_api_service import _tmp_settings


def metrics(**changes):
    return {'trades':2,'total_pnl':10.,'avg_pnl':5.,'median_pnl':5.,
        'win_rate':50.,'severe_loss_count':0,'runner_capture_ratio':.25,**changes}


def cache(settings,payload):
    path=settings.metrics_dir/'policy_replay.json'
    path.write_text(json.dumps(payload),encoding='utf-8')
    return path


@pytest.mark.parametrize('post',[False,True])
def test_canonical_zero_is_not_replaced_by_a_convenient_positive_alias(post):
    row={'realized_pnl_pct':0.,'total_pnl_pct':1000.,'peak_pnl_pct':1000.}
    stats=(replay._summarize_post_adjustment if post else replay._summarize)([row],
        'baseline_48h' if post else 'current')
    assert stats['trades']==1 and stats['total_pnl']==stats['avg_pnl']==stats['win_rate']==0.


@pytest.mark.parametrize('bad',[True,False,float('nan'),float('inf'),-float('inf'),'bad','',{},[]])
@pytest.mark.parametrize('post',[False,True])
def test_invalid_canonical_return_cannot_fall_back_or_become_a_zero_observation(bad,post):
    rows=[{'realized_pnl_pct':bad,'pnl_pct':1000.},{'pnl_pct':10.,'peak_pnl_pct':100.}]
    stats=(replay._summarize_post_adjustment if post else replay._summarize)(rows,
        'baseline_48h' if post else 'current')
    assert stats['trades']==1 and stats['invalid_return_rows']==1
    assert stats['total_pnl']==stats['avg_pnl']==10.


@pytest.mark.parametrize('post',[False,True])
def test_unknown_returns_and_unknown_peaks_are_not_zero_returns_or_perfect_capture(post):
    rows=[{'closed':True},{'pnl_pct':20.}]
    stats=(replay._summarize_post_adjustment if post else replay._summarize)(rows,
        'baseline_48h' if post else 'current')
    assert stats['trades']==1 and stats['invalid_return_rows']==1
    assert stats['runner_capture_ratio'] is None and stats['runner_capture_denominator']==0


@pytest.mark.parametrize('post',[False,True])
def test_even_median_and_unbounded_finite_tail_mean_do_not_overflow(post):
    summarize=replay._summarize_post_adjustment if post else replay._summarize
    policy='baseline_48h' if post else 'current'
    middle=summarize([{'pnl_pct':-10.},{'pnl_pct':30.}],policy)
    assert middle['median_pnl']==10.
    huge=summarize([{'pnl_pct':1e308},{'pnl_pct':1e308}],policy)
    assert huge['avg_pnl']==huge['median_pnl']==1e308
    assert huge['total_pnl'] is None  # Unrepresentable sum is not zero or infinity.
    mixed=summarize([{'pnl_pct':1e308},{'pnl_pct':1e308},{'pnl_pct':-1e308},{'pnl_pct':-1e308}],policy)
    assert mixed['total_pnl']==mixed['avg_pnl']==mixed['median_pnl']==0.


@pytest.mark.parametrize('post',[False,True])
def test_producer_declares_actual_nonfinancial_grain_and_does_not_render_metadata_as_policy(tmp_path,post):
    report=(replay.write_post_adjustment_policy_replay if post else replay.write_policy_replay)(tmp_path)
    scope=report['_evidence']
    assert scope['diagnostic_only'] is True and scope['financial_acceptance'] is False
    assert scope['can_authorize_trading'] is False
    assert scope['metric_units']['total_pnl']=='unweighted_reported_percentage_points_sum'
    doc=tmp_path/'docs'/('POST_ADJUSTMENT_REPLAY.md' if post else 'POLICY_REPLAY.md')
    assert '| _evidence |' not in doc.read_text()
    assert 'not net financial' in doc.read_text().lower()


def test_empty_api_workspace_cannot_display_a_passed_policy_replay_gate(tmp_path):
    settings=_tmp_settings(tmp_path)
    row=service.get_policy_safety_envelope(settings).data
    assert row['policy_replay']['candidate_passed'] is False
    assert row['policy_replay']['diagnostic_candidate_passed'] is False
    assert next(g for g in row['gates'] if g['id']=='policy_replay')['status']!='pass'
    assert service.get_policy_replay_envelope(settings).data['best_by_total_pnl'] is None


@pytest.mark.parametrize('claim',[False,True])
def test_positive_cached_diagnostics_and_caller_flags_never_authorize_trading(tmp_path,claim):
    settings=_tmp_settings(tmp_path)
    payload={'current':metrics(),'combined_policy_v2':metrics(total_pnl=20.,runner_capture_ratio=.5),
        '_evidence':{'diagnostic_only':False,'financial_acceptance':claim,'can_authorize_trading':claim}}
    path=cache(settings,payload)
    before=path.read_bytes()
    result=service.get_policy_safety_envelope(settings).data['policy_replay']
    assert result['candidate_passed'] is False and result['diagnostic_candidate_passed'] is True
    assert result['evidence_scope']['financial_acceptance'] is False
    assert result['evidence_scope']['can_authorize_trading'] is False
    assert path.read_bytes()==before
    out=service.get_policy_replay_envelope(settings).data
    assert out['raw']==payload and out['evidence_scope']['diagnostic_only'] is True
    assert all(row['policy'] in replay.POLICIES for row in out['policies'])


@pytest.mark.parametrize('field,value',[
    ('trades',True),('trades',0),('trades',1.5),('trades','2'),
    ('total_pnl',float('inf')),('total_pnl',True),('total_pnl','20'),
    ('severe_loss_count',-1),('severe_loss_count',float('nan')),
    ('runner_capture_ratio',None),('runner_capture_ratio',False),
])
def test_invalid_or_missing_diagnostic_support_cannot_pass(tmp_path,field,value):
    settings=_tmp_settings(tmp_path)
    cache(settings,{'current':metrics(),'combined_policy_v2':metrics(**{field:value})})
    row=service.get_policy_safety_envelope(settings).data['policy_replay']
    assert row['candidate_passed'] is False and row['diagnostic_candidate_passed'] is False


def test_metadata_and_empty_rows_never_become_the_best_policy(tmp_path):
    settings=_tmp_settings(tmp_path)
    cache(settings,{'current':metrics(),'combined_policy_v2':metrics(total_pnl=20.),
        'candidate_config':metrics(total_pnl=1e12),'_evidence':metrics(total_pnl=1e15),
        'risk_guard':metrics(trades=0,total_pnl=1e20)})
    data=service.get_policy_replay_envelope(settings).data
    assert data['best_by_total_pnl']=='combined_policy_v2'
    assert all(row['policy'] not in {'candidate_config','_evidence'} for row in data['policies'])


def test_cached_metrics_cannot_replace_their_policy_identity(tmp_path):
    settings=_tmp_settings(tmp_path)
    cache(settings,{'current':metrics(policy='arbitrary',total_pnl=30.),
        'combined_policy_v2':metrics()})
    data=service.get_policy_replay_envelope(settings).data
    assert data['best_by_total_pnl']=='current'
    assert {row['policy'] for row in data['policies']}=={'current','combined_policy_v2'}


@pytest.mark.parametrize('bad',[float('nan'),float('inf'),-float('inf')])
@pytest.mark.parametrize('endpoint',['safety','replay'])
def test_native_http_serializes_invalid_cached_numbers_without_approval(tmp_path,bad,endpoint):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import policy as router
    settings=_tmp_settings(tmp_path)
    cache(settings,{'current':metrics(),'combined_policy_v2':metrics(total_pnl=bad)})
    app=FastAPI()
    app.include_router(router.router,prefix='/api/v1')
    app.dependency_overrides[router.get_settings]=lambda:settings
    with TestClient(app,raise_server_exceptions=False) as client:
        response=client.get('/api/v1/policy/'+endpoint)
    assert response.status_code==200
    data=response.json()['data']
    if endpoint=='safety':
        assert data['policy_replay']['diagnostic_candidate_passed'] is False
        assert data['policy_replay']['candidate_passed'] is False
    else:
        assert data['best_by_total_pnl']=='current'
        assert data['raw']['combined_policy_v2']['total_pnl'] is None


def test_native_loader_keeps_canonical_zero_and_reports_unknown_return_rows(tmp_path):
    path=tmp_path/'data/metrics/candidate_outcomes.jsonl'
    path.parent.mkdir(parents=True)
    rows=[{'address':'ZERO','realized_pnl_pct':0.,'pnl_pct':1000.},
        {'address':'UNKNOWN','realized_pnl_pct':'bad','pnl_pct':1000.}]
    path.write_text('\n'.join(json.dumps(row) for row in rows),encoding='utf-8')
    report=replay.build_policy_replay(tmp_path)
    assert report['current']['trades']==1 and report['current']['total_pnl']==0.
    assert report['current']['invalid_return_rows']==1
    assert report['_evidence']['input_rows']==2
    assert report['_evidence']['valid_return_rows']==report['_evidence']['invalid_return_rows']==1


def test_no_selected_observations_have_only_a_decision_score_not_a_financial_zero():
    from types import SimpleNamespace
    result=replay._shadow_followup_candidate_summary([],SimpleNamespace())
    assert result['objective_score']==0.
    assert result['shadow_followup_total_pnl'] is None
    assert result['shadow_followup_avg_pnl'] is None


def test_selected_unknown_outcome_has_no_objective_score(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(replay,'evaluate_shadow_followup_micro',lambda *a,**k:
        SimpleNamespace(allowed=True,failures=[],route_proxy=False))
    result=replay._shadow_followup_candidate_summary([{'realized_pnl_pct':'bad'}],SimpleNamespace())
    assert result['allowed_shadow_followup']==result['allowed_with_unknown_return']==1
    assert result['simulated_buys']==0 and result['objective_score'] is None


@pytest.mark.parametrize('post',[False,True])
def test_canonical_zero_peak_is_unknown_capture_not_a_positive_alias(post):
    summarize=replay._summarize_post_adjustment if post else replay._summarize
    stats=summarize([{'pnl_pct':20.,'max_pnl_pct_seen':0.,'peak_pnl_pct':1000.}],
        'baseline_48h' if post else 'current')
    assert stats['runner_capture_denominator']==0 and stats['runner_capture_ratio'] is None


def test_extreme_reported_runner_upside_is_not_clipped():
    stats=replay._summarize([{'pnl_pct':1e100,'peak_pnl_pct':1e100}],'current')
    assert stats['total_pnl']==stats['avg_pnl']==1e100
    assert stats['runner_capture_ratio']==1.


@pytest.mark.parametrize('bad',[{},[],True,'invalid'])
def test_native_http_route_keeps_malformed_policy_metrics_non_authorizing(tmp_path,bad):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import policy as router
    settings=_tmp_settings(tmp_path)
    cache(settings,{'current':metrics(),'combined_policy_v2':bad})
    app=FastAPI()
    app.include_router(router.router,prefix='/api/v1')
    app.dependency_overrides[router.get_settings]=lambda:settings
    with TestClient(app,raise_server_exceptions=False) as client:
        response=client.get('/api/v1/policy/safety')
    assert response.status_code==200
    assert response.json()['data']['policy_replay']['candidate_passed'] is False


def test_native_forward_screen_does_not_pass_an_empty_replay(tmp_path):
    from analytics.paper_forward import evaluate_paper_forward
    row=evaluate_paper_forward({},root=tmp_path)
    assert row['replay_passed'] is False and row['passed'] is False
    assert row['replay_evidence_scope']['financial_acceptance'] is False
