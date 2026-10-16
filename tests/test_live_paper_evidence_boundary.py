"""LIVE readiness must reconstruct original PAPER cash, not trust summaries."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.services import live_promotion as service
from runtime import live_canary_guard as guard


def settings(root):
    return SimpleNamespace(project_root=root,data_dir=root/'data',metrics_dir=root/'data/metrics',
        runtime_dir=root/'data/runtime',db_path=root/'data/memebotdatabase.db')


def legacy_green(root):
    payloads={'metrics/current_run_summary.json':{'buys':30,'closed_trades':30,'total_pnl_usd':12.5,'profit_factor':1.2},
        'research_runs/scoreboard.json':{'entries':[{'run_id':'ar_ok','status':'accepted_replay',
            'objective_score':12.5,'evaluated_at_utc':'2026-06-04T20:00:00+00:00'}]},
        'research_runs/api_budget.json':{'birdeye_429_count':0,'gecko_429_count':0,
            'jupiter_rate_limit_count':0,'provider_degraded_minutes':0}}
    for name,value in payloads.items():
        path=root/'data'/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value),encoding='utf-8')


def test_native_ui_preflight_rejects_green_summary_without_original_net_fills(tmp_path):
    legacy_green(tmp_path)
    result=service.build_live_promotion_preflight(settings(tmp_path),
        runtime_snapshot={'wallet_sol':1.,'dry_run':True},manual_approval=True,approved_by='operator')
    assert result['passed'] is False


def test_native_startup_guard_rejects_the_same_unproved_legacy_summary(tmp_path):
    legacy_green(tmp_path)
    values=service.build_live_start_profile_values(approved_by='operator',approval_id='synthetic-approval')
    result=guard.evaluate_live_start(root=tmp_path,cfg=SimpleNamespace(),profile_values=values)
    assert result.passed is False


def test_profile_writer_cannot_trust_a_bare_passed_flag_or_create_a_live_profile(tmp_path):
    destination=settings(tmp_path).runtime_dir/service.LIVE_PROFILE_NAME
    with pytest.raises(RuntimeError):
        service.write_live_start_profile(settings(tmp_path),{'passed':True},approved_by='operator')
    assert not destination.exists()


def test_positive_cached_forward_flag_cannot_pass_policy_gate_without_original_fills(tmp_path):
    from test_policy_api_service import _tmp_settings
    from api.services.policy import get_policy_safety_envelope
    options=_tmp_settings(tmp_path)
    (options.metrics_dir/'paper_forward_report.json').write_text(json.dumps({'passed':True}),encoding='utf-8')
    result=get_policy_safety_envelope(options).data
    assert next(row for row in result['gates'] if row['id']=='paper_forward')['status']!='pass'


def healthy(root):
    from test_live_promotion_preflight import _healthy_artifacts
    _healthy_artifacts(root)


def preflight(root):
    from live_paper_fixtures import snapshot
    return service.build_live_promotion_preflight(settings(root),runtime_snapshot=snapshot(),
        manual_approval=True,approved_by='operator')


def test_checked_financial_cohort_flows_through_profile_write_and_native_start_guard(tmp_path):
    from runtime.live_paper_evidence import BINDING_ENV_FIELDS
    healthy(tmp_path)
    checked=preflight(tmp_path)
    assert checked['passed'] is True
    path=service.write_live_start_profile(settings(tmp_path),checked,approved_by='operator')
    values=guard.load_env_values(path)
    assert all(values[name]==checked['paper_sample']['binding'][key] for key,name in BINDING_ENV_FIELDS.items())
    result=guard.evaluate_live_start(root=tmp_path,cfg=SimpleNamespace(),profile_values=values)
    assert result.passed is True, result.errors
    assert values['AUTO_PROMOTE_LIVE']=='false'


@pytest.mark.parametrize('mode',['removed','bad_cash','new_valid_cash','candidate_changed','stale_wallet'])
def test_writer_rechecks_sources_and_preserves_destination_when_evidence_changes(tmp_path,mode):
    import datetime as dt
    import sqlite3
    from live_paper_fixtures import write_cohort
    healthy(tmp_path)
    checked=preflight(tmp_path)
    assert checked['passed'] is True
    destination=settings(tmp_path).runtime_dir/service.LIVE_PROFILE_NAME
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_bytes(b'previous-owned-profile')
    closed=tmp_path/'data/paper_closed_trades.jsonl'
    if mode=='removed':
        closed.unlink()
    elif mode=='bad_cash':
        rows=[json.loads(line) for line in closed.read_text().splitlines()]
        rows[0]['net_total_pnl_usd']=999
        closed.write_text('\n'.join(json.dumps(row) for row in rows),encoding='utf-8')
    elif mode=='new_valid_cash':
        write_cohort(tmp_path,net=20.)
        assert preflight(tmp_path)['passed'] is True
    elif mode=='candidate_changed':
        board=tmp_path/'data/research_runs/scoreboard.json'
        value=json.loads(board.read_text())
        value[0]['proposal_id']='a-different-proposal'
        board.write_text(json.dumps(value),encoding='utf-8')
    else:
        with sqlite3.connect(settings(tmp_path).db_path) as db:
            db.execute('UPDATE bot_runtime_state SET updated_at=?',((dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=1)).isoformat(),))
    with pytest.raises(RuntimeError):
        service.write_live_start_profile(settings(tmp_path),checked,approved_by='operator')
    assert destination.read_bytes()==b'previous-owned-profile'


def test_native_start_rechecks_frozen_cash_even_after_another_valid_cohort_replaces_it(tmp_path):
    from live_paper_fixtures import write_cohort
    healthy(tmp_path)
    path=service.write_live_start_profile(settings(tmp_path),preflight(tmp_path),approved_by='operator')
    values=guard.load_env_values(path)
    write_cohort(tmp_path,net=20.)
    result=guard.evaluate_live_start(root=tmp_path,cfg=SimpleNamespace(),profile_values=values)
    assert result.passed is False
    original=next(row for row in result.gates if row.id=='sample.original_cash_evidence')
    assert 'paper_original_cash_cohort_changed' in original.value


def test_runtime_binding_does_not_depend_on_mutable_report_after_approval(tmp_path):
    healthy(tmp_path)
    path=service.write_live_start_profile(settings(tmp_path),preflight(tmp_path),approved_by='operator')
    (tmp_path/'data/metrics/paper_forward_report.json').unlink()
    result=guard.evaluate_live_start(root=tmp_path,cfg=SimpleNamespace(),profile_values=guard.load_env_values(path))
    assert result.passed is True, result.errors


@pytest.mark.parametrize('claim',[True,False,'false',None])
def test_cached_approval_claims_cannot_replace_or_poison_original_financial_cohort(tmp_path,claim):
    from runtime.live_paper_evidence import inspect_paper_evidence
    from live_paper_fixtures import write_cohort
    report=write_cohort(tmp_path)
    report.update(passed=claim,forward_acceptance={'passed':claim})
    report['forward_evidence'].update(total_pnl_usd=1e30,closed_trades=1000000)
    (tmp_path/'data/metrics/paper_forward_report.json').write_text(json.dumps(report),encoding='utf-8')
    sample=inspect_paper_evidence(tmp_path)
    assert sample['passed'] is True
    assert sample['evidence']['closed_trades']==50
    assert sample['evidence']['total_pnl_usd']==pytest.approx(50.)
    assert sample['can_authorize_trading'] is False


@pytest.mark.parametrize('field',['proposal_id','run_id','started_at','config_hash','profile','cash_sha256'])
def test_incomplete_frozen_identity_cannot_pass_startup(tmp_path,field):
    from runtime.live_paper_evidence import inspect_paper_evidence
    from live_paper_fixtures import write_cohort
    write_cohort(tmp_path)
    binding=inspect_paper_evidence(tmp_path)['binding']
    binding[field]=None
    assert inspect_paper_evidence(tmp_path,binding=binding)['passed'] is False


@pytest.mark.parametrize('age_hours',[-1.,1.,48.,72.])
def test_wallet_snapshot_must_be_fresh_not_just_positive(tmp_path,age_hours):
    import datetime as dt
    healthy(tmp_path)
    value={'wallet_sol':1.,'dry_run':True,'updated_at':(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=age_hours)).isoformat()}
    result=service.build_live_promotion_preflight(settings(tmp_path),runtime_snapshot=value,
        manual_approval=True,approved_by='operator')
    assert result['passed'] is False
    assert next(row for row in result['gates'] if row['id']=='wallet_sol')['status']=='block'


@pytest.mark.parametrize('age_hours',[-1.,25.])
def test_future_and_stale_manual_approval_are_not_current_authority(age_hours):
    import datetime as dt
    now=dt.datetime.now(dt.timezone.utc)
    values=service.build_live_start_profile_values(approved_by='operator')
    values['LIVE_CANARY_APPROVED_AT_UTC']=(now-dt.timedelta(hours=age_hours)).isoformat()
    result=guard.validate_live_canary_profile_values(values,require_approval=True,now=now)
    assert result.passed is False


@pytest.mark.parametrize('value',[True,1.5,float('inf'),float('nan'),'1.5'])
def test_profile_integer_caps_do_not_truncate_invalid_values(value):
    values=service.build_live_start_profile_values(approved_by='operator')
    values['LIVE_CANARY_MAX_OPEN']=value
    result=guard.validate_live_canary_profile_values(values,require_approval=True)
    assert result.passed is False


@pytest.mark.parametrize('value',[True,'false',[],[{}]])
def test_policy_http_route_handles_malformed_cached_forward_report_without_approval(tmp_path,value):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import policy as router
    from test_policy_api_service import _tmp_settings
    options=_tmp_settings(tmp_path)
    (options.metrics_dir/'paper_forward_report.json').write_text(json.dumps(value),encoding='utf-8')
    app=FastAPI()
    app.include_router(router.router,prefix='/api/v1')
    app.dependency_overrides[router.get_settings]=lambda:options
    with TestClient(app,raise_server_exceptions=False) as client:
        response=client.get('/api/v1/policy/safety')
    assert response.status_code==200
    assert next(row for row in response.json()['data']['gates'] if row['id']=='paper_forward')['status']!='pass'


def test_uncapped_extreme_net_paper_cohort_is_not_rejected_for_its_upside(tmp_path):
    from live_paper_fixtures import write_cohort
    from runtime.live_paper_evidence import inspect_paper_evidence
    write_cohort(tmp_path,net=10000.)
    sample=inspect_paper_evidence(tmp_path)
    assert sample['passed'] is True
    assert sample['evidence']['avg_pnl_pct']==pytest.approx(10000.)


def test_atomic_profile_publication_failure_preserves_previous_file(monkeypatch,tmp_path):
    healthy(tmp_path)
    checked=preflight(tmp_path)
    destination=settings(tmp_path).runtime_dir/service.LIVE_PROFILE_NAME
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_bytes(b'previous-owned-profile')
    def fail(*args): raise OSError('synthetic atomic publication failure')
    monkeypatch.setattr(service.os,'replace',fail)
    with pytest.raises(OSError,match='synthetic atomic'):
        service.write_live_start_profile(settings(tmp_path),checked,approved_by='operator')
    assert destination.read_bytes()==b'previous-owned-profile'
    assert list(destination.parent.glob('*.tmp'))==[]


@pytest.mark.parametrize('field,value',[('approved_by','operator\nDRY_RUN=0'),('approval_id','id\nLIVE_CANARY_MAX_OPEN=999')])
def test_profile_identity_cannot_inject_other_environment_assignments(tmp_path,field,value):
    checked={'passed':True,'approved_by':value if field=='approved_by' else 'operator'}
    kwargs={'approved_by':'operator',field:value}
    with pytest.raises(RuntimeError):
        service.write_live_start_profile(settings(tmp_path),checked,**kwargs)
    assert not (settings(tmp_path).runtime_dir/service.LIVE_PROFILE_NAME).exists()


@pytest.mark.parametrize('clock',['missing','old','future'])
def test_fresh_process_cannot_launder_a_missing_old_or_future_wallet_observation(tmp_path,clock):
    import datetime as dt
    from live_paper_fixtures import snapshot
    healthy(tmp_path)
    value=snapshot()
    if clock=='missing': value.pop('wallet_checked_at')
    else:
        seconds=61 if clock=='old' else -1
        value['wallet_checked_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=seconds)).isoformat()
    result=service.build_live_promotion_preflight(settings(tmp_path),runtime_snapshot=value,
        manual_approval=True,approved_by='operator')
    assert result['passed'] is False
    assert next(row for row in result['gates'] if row['id']=='wallet_sol')['status']=='block'


def test_original_wallet_clock_remains_usable_across_the_native_30_second_poll(tmp_path):
    import datetime as dt
    from live_paper_fixtures import snapshot
    healthy(tmp_path)
    value=snapshot()
    value['wallet_checked_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=30)).isoformat()
    result=service.build_live_promotion_preflight(settings(tmp_path),runtime_snapshot=value,
        manual_approval=True,approved_by='operator')
    assert result['passed'] is True
