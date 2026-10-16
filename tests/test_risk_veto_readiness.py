"""Risk-head readiness is independent of primary-entry model readiness."""
import datetime as dt
import inspect
import json
from types import SimpleNamespace

import pytest

from analytics import ml_policy
from test_entry_inference_coherence import load_helpers
from test_ml_policy import _cfg


def decision(monkeypatch, *, primary=False, ready=True, probability=.99,
             mode='risk_veto_only', lane='pump_early_pumpswap_profit', paper=False):
    monkeypatch.setattr(ml_policy,'CFG',_cfg(ML_GATE_MODE=mode,
        ML_RISK_VETO_ENABLED=True,ML_RISK_SHADOW_ONLY=False,
        GREEN_SNIPER_RISK_CAN_VETO_LIVE=True))
    kwargs=dict(token={'entry_lane':lane},feature_row={},proba=.9 if primary else None,
        risk_proba=probability,base_rules_passed=True,dry_run=paper,live=not paper,
        entry_model_activation_ready=primary,
        entry_model_metadata={'activation_ready':True,'ai_threshold_recommended':.5} if primary else {})
    # Run the same semantic regression against the original signature too;
    # absence of the new explicit boundary must not turn red into TypeError.
    if 'risk_prediction_ready' in inspect.signature(ml_policy.decide_ml_action).parameters:
        kwargs['risk_prediction_ready']=ready
    return ml_policy.decide_ml_action(**kwargs)


@pytest.mark.parametrize('mode',['legacy','enforce','lane_aware','sizing_only','risk_veto_only'])
@pytest.mark.parametrize('lane',['pump_early_pumpswap_profit','pump_early_green_candle_sniper'])
@pytest.mark.parametrize('paper',[False,True])
def test_checked_risk_can_veto_without_any_primary_model(monkeypatch,mode,lane,paper):
    result=decision(monkeypatch,mode=mode,lane=lane,paper=paper)
    assert result.risk_veto_enforced and not result.allow_buy
    assert not result.activation_ready and result.proba is None
    namespace,_=load_helpers()
    assert not namespace['_entry_ml_allowed'](result,paper=paper,bypass=True)


@pytest.mark.parametrize('primary',[False,True])
@pytest.mark.parametrize('lane',['pump_early_pumpswap_profit','pump_early_green_candle_sniper'])
@pytest.mark.parametrize('ready',[False,None,1,'true'])
def test_unchecked_risk_scalar_cannot_inherit_primary_authority(monkeypatch,primary,lane,ready):
    result=decision(monkeypatch,primary=primary,lane=lane,ready=ready)
    assert result.risk_veto and not result.risk_veto_enforced and result.allow_buy
    assert result.activation_ready is primary


@pytest.mark.parametrize('primary',[False,True])
@pytest.mark.parametrize('lane',['pump_early_pumpswap_profit','pump_early_green_candle_sniper'])
@pytest.mark.parametrize('mode',['off','shadow'])
def test_checked_independent_risk_stays_advisory_in_global_observation(monkeypatch,primary,lane,mode):
    result=decision(monkeypatch,primary=primary,lane=lane,mode=mode)
    assert result.risk_veto and not result.risk_veto_enforced and result.allow_buy
    assert not result.enforce and result.mode==mode


@pytest.mark.parametrize('value',[None,True,False,float('nan'),float('inf'),float('-inf'),-.1,1.1,'0.99','bad',[],{}])
@pytest.mark.parametrize('primary',[False,True])
def test_malformed_risk_is_unknown_not_healthy_or_enforceable(monkeypatch,value,primary):
    result=decision(monkeypatch,primary=primary,probability=value)
    assert result.risk_proba is None
    assert not result.risk_veto and not result.risk_veto_enforced and result.allow_buy
    json.dumps(result.__dict__,allow_nan=False)


@pytest.mark.parametrize('value,expected',[(0.,False),(.69999,False),(.7,True),(1.,True)])
@pytest.mark.parametrize('primary',[False,True])
def test_checked_probability_boundary_is_preserved(monkeypatch,value,expected,primary):
    result=decision(monkeypatch,primary=primary,probability=value)
    assert result.risk_proba==value and result.risk_veto_enforced is expected
    assert result.allow_buy is not expected and result.activation_ready is primary


@pytest.mark.parametrize('probability',[None,0.,.69999,.7,.99])
@pytest.mark.parametrize('paper',[False,True])
def test_actual_native_scoring_transmits_independent_risk_without_primary(monkeypatch,probability,paper):
    from analytics import ai_predict,exit_model_runtime
    from features.builder import build_feature_vector
    monkeypatch.setattr(ai_predict,'primary_model_selection',lambda:{'status':'unavailable'})
    monkeypatch.setattr(exit_model_runtime,'predict_exit_profile',lambda vec:{'status':'unknown'})
    config=_cfg(ML_GATE_MODE='risk_veto_only',ML_RISK_VETO_ENABLED=True,ML_RISK_SHADOW_ONLY=False)
    monkeypatch.setattr(ml_policy,'CFG',config)
    ns,_=load_helpers()
    ns.update(CFG=config,DRY_RUN=paper,AI_THRESHOLD=.5,
        _runner_profile_for_subject=lambda token:'unchanged',_config_hash=lambda:'fixture',
        build_feature_vector=build_feature_vector,should_buy=lambda vec:None,
        predict_risk=lambda vec:probability,predict_ev=lambda vec:None,
        entry_prediction_state=lambda:{'activation_ready':False,'metadata':{}},
        decide_ml_action=ml_policy.decide_ml_action,
        filters=SimpleNamespace(effective_ai_threshold=lambda *args:.5),
        research_runtime=SimpleNamespace(score_candidate=lambda *args,**kwargs:{}))
    bundle=ns['_score_entry_inputs']({'address':'synthetic','entry_lane':'pump_early_pumpswap_profit'},
        captured_at=dt.datetime(2026,10,16,tzinfo=dt.timezone.utc))
    result=bundle[-1]
    expected=probability is not None and probability>=.7
    assert result.risk_veto_enforced is expected
    assert not result.activation_ready and result.proba is None
    assert ns['_entry_ml_allowed'](result,paper=paper,bypass=True) is not expected


def test_omitted_readiness_is_not_inferred_from_valid_primary(monkeypatch):
    monkeypatch.setattr(ml_policy,'CFG',_cfg(ML_GATE_MODE='risk_veto_only',
        ML_RISK_VETO_ENABLED=True,ML_RISK_SHADOW_ONLY=False))
    result=ml_policy.decide_ml_action(token={'entry_lane':'pump_early_pumpswap_profit'},
        feature_row={},proba=.9,risk_proba=.99,base_rules_passed=True,dry_run=False,live=True,
        entry_model_activation_ready=True,
        entry_model_metadata={'activation_ready':True,'ai_threshold_recommended':.5})
    assert result.activation_ready and result.risk_veto
    assert not result.risk_veto_enforced and result.allow_buy
