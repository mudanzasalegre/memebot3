"""Observation cannot veto native admission; active risk remains protected."""
from types import SimpleNamespace

import pytest

from analytics import ml_policy
from test_entry_inference_coherence import load_helpers
from test_ml_policy import _cfg


def decide(monkeypatch, *, mode, lane='pump_early_pumpswap_profit', proba=.1,
           risk=.99, base=True, paper=False, **overrides):
    values=dict(ML_GATE_MODE=mode,ML_RISK_VETO_ENABLED=True,ML_RISK_SHADOW_ONLY=False,
        GREEN_SNIPER_ML_BLOCK_ENABLED=True,GREEN_SNIPER_RISK_CAN_VETO_LIVE=True)
    values.update(overrides)
    monkeypatch.setattr(ml_policy,'CFG',_cfg(**values))
    return ml_policy.decide_ml_action(token={'entry_lane':lane},feature_row={},
        proba=proba,risk_proba=risk,base_rules_passed=base,dry_run=paper,live=not paper,
        entry_model_activation_ready=True,
        entry_model_metadata={'activation_ready':True,'ai_threshold_recommended':.5})


@pytest.mark.parametrize('mode',['off','shadow'])
@pytest.mark.parametrize('lane',['pump_early_pumpswap_profit','pump_early_green_candle_sniper'])
@pytest.mark.parametrize('paper',[False,True])
def test_global_observation_never_becomes_a_native_admission_veto(monkeypatch,mode,lane,paper):
    decision=decide(monkeypatch,mode=mode,lane=lane,paper=paper)
    assert decision.allow_buy and not decision.enforce and not decision.risk_veto_enforced
    assert decision.mode==mode
    namespace,_=load_helpers()
    assert namespace['_entry_ml_allowed'](decision,paper=paper,bypass=False)
    assert decision.proba==.1 and decision.risk_proba==.99


@pytest.mark.parametrize('paper',[False,True])
def test_active_green_generic_risk_veto_reports_same_rejection_as_native_guard(monkeypatch,paper):
    decision=decide(monkeypatch,mode='lane_aware',lane='pump_early_green_candle_sniper',
        paper=paper,proba=.9,GREEN_SNIPER_RISK_CAN_VETO_LIVE=False)
    namespace,_=load_helpers()
    assert not namespace['_entry_ml_allowed'](decision,paper=paper,bypass=True)
    assert decision.risk_veto_enforced and not decision.allow_buy
    assert decision.reason=='risk_veto'


@pytest.mark.parametrize('mode',['legacy','enforce','lane_aware','sizing_only','risk_veto_only'])
@pytest.mark.parametrize('lane',['pump_early_pumpswap_profit','pump_early_green_candle_sniper'])
@pytest.mark.parametrize('paper',[False,True])
def test_active_risk_veto_is_still_not_bypassed_by_paper_exploration(monkeypatch,mode,lane,paper):
    decision=decide(monkeypatch,mode=mode,lane=lane,paper=paper)
    assert decision.risk_veto_enforced
    namespace,_=load_helpers()
    assert not namespace['_entry_ml_allowed'](decision,paper=paper,bypass=True)


@pytest.mark.parametrize('mode',['off','shadow'])
@pytest.mark.parametrize('lane',['pump_early_pumpswap_profit','pump_early_green_candle_sniper'])
@pytest.mark.parametrize('paper',[False,True])
def test_observation_mode_does_not_turn_failed_base_rules_into_permission(monkeypatch,mode,lane,paper):
    decision=decide(monkeypatch,mode=mode,lane=lane,paper=paper,base=False)
    assert not decision.allow_buy and not decision.enforce and not decision.risk_veto_enforced
