"""Actual checked regressions must never masquerade as risk probabilities."""
import json
from pathlib import Path
import shutil

import pytest

from analytics import model_runtime_common as runtime, risk_model_runtime, risk_predict
from ml.family_training import train_classifier_family,train_regressor_family
from net_financial_fixtures import net_frame
from test_model_uncertainty_integrity import _frame


@pytest.fixture(scope='module')
def regression_artifact(tmp_path_factory):
    root=tmp_path_factory.mktemp('risk-regression-not-probability')
    directory=root/'ml/models/risk'
    report=train_regressor_family(family='risk',targets=['severe_loss_30'],
        feature_set_name='risk_features',frame=net_frame(_frame()),output_dir=directory)
    assert report['targets']['severe_loss_30']['regression_validation_ready']
    return root,directory/'severe_loss_30.pkl'


def configure(monkeypatch,root):
    monkeypatch.setattr(runtime,'PROJECT_ROOT',root)
    monkeypatch.setattr(risk_predict,'MODEL_PATH',root/'ml/risk_model.pkl')
    monkeypatch.setattr(risk_predict,'META_PATH',root/'ml/risk_model.meta.json')


def test_family_risk_reader_rejects_native_accepted_regression_even_in_probability_range(regression_artifact,monkeypatch):
    root,path=regression_artifact
    configure(monkeypatch,root)
    metadata=json.loads(path.with_suffix('.meta.json').read_text())
    assert metadata['prediction_kind']=='regression_pct_points'
    generic=runtime.predict_model('risk','severe_loss_30',{'price_pct_5m':5.})
    assert generic is not None and 0<=generic<=1
    assert risk_predict.predict_risk({'price_pct_5m':5.}) is None


def test_all_risk_heads_reject_regression_instead_of_reporting_high_probability(regression_artifact,monkeypatch):
    root,_=regression_artifact
    configure(monkeypatch,root)
    result=risk_model_runtime.predict_severe_loss_risk({'price_pct_5m':5.})
    assert result['risk_proba_30'] is None
    assert result['risk_level']=='unknown' and result['risk_reason']=='model_missing'


def test_configured_compatibility_risk_cannot_bypass_the_probability_kind(tmp_path,regression_artifact,monkeypatch):
    _,source=regression_artifact
    configure(monkeypatch,tmp_path)
    target=risk_predict.MODEL_PATH
    target.parent.mkdir(parents=True)
    shutil.copy2(source,target)
    metadata=json.loads(source.with_suffix('.meta.json').read_text())
    metadata.update(target='severe_loss_configured',severe_loss_pct=-30.)
    target.with_suffix('.meta.json').write_text(json.dumps(metadata))
    generic=runtime.predict_artifact(target,{'price_pct_5m':5.},expected_metadata={
        'family':'risk','target':'severe_loss_configured','severe_loss_pct':-30.})
    assert generic is not None and 0<=generic<=1
    assert risk_predict.predict_risk({'price_pct_5m':5.}) is None


@pytest.fixture(scope='module')
def probability_artifact(tmp_path_factory):
    root=tmp_path_factory.mktemp('checked-calibrated-risk')
    directory=root/'ml/models/risk'
    report=train_classifier_family(family='risk',targets=['severe_loss_30'],
        feature_set_name='risk_features',frame=net_frame(_frame()),output_dir=directory)
    assert report['targets']['severe_loss_30']['probability_validation_ready']
    return root,directory/'severe_loss_30.pkl'


@pytest.mark.parametrize('price',[5.,80.])
def test_checked_classifier_is_still_usable_without_primary(probability_artifact,monkeypatch,price):
    root,_=probability_artifact
    configure(monkeypatch,root)
    value=risk_predict.predict_risk({'price_pct_5m':price})
    assert value is not None and 0<=value<=1
    assert value==runtime.predict_model('risk','severe_loss_30',{'price_pct_5m':price})
    assert risk_model_runtime.predict_severe_loss_risk({'price_pct_5m':price})['risk_proba_30']==value
    from test_risk_veto_readiness import decision
    result=decision(monkeypatch,primary=False,probability=value)
    assert not result.activation_ready and result.risk_veto_enforced is (value>=.7)


def test_checked_classifier_snapshot_survives_reload_only_until_scope_end(probability_artifact,monkeypatch,tmp_path):
    from analytics.inference_scope import inference_scope
    _,source=probability_artifact
    target=tmp_path/'ml/models/risk/severe_loss_30.pkl'
    target.parent.mkdir(parents=True)
    shutil.copy2(source,target)
    shutil.copy2(source.with_suffix('.meta.json'),target.with_suffix('.meta.json'))
    configure(monkeypatch,tmp_path)
    vec={'price_pct_5m':5.}
    with inference_scope():
        before=risk_predict.predict_risk(vec)
        assert before is not None
        target.write_bytes(b'corrupt replacement in owned temporary fixture')
        runtime.invalidate_model_cache(target)
        assert risk_predict.predict_risk(vec)==before
        assert risk_model_runtime.predict_severe_loss_risk(vec)['risk_proba_30']==before
    assert risk_predict.predict_risk(vec) is None


def test_strict_reader_requires_probability_kind_for_every_risk_head(monkeypatch):
    seen=[]
    monkeypatch.setattr(runtime,'_load_family',lambda family,target,**kwargs:
        (Path(target),None,[],{'family':family,'target':target}))
    monkeypatch.setattr(runtime,'_predict_snapshot',lambda *args,**kwargs:seen.append(kwargs['expected_metadata']))
    for target in ('severe_loss_30','severe_loss_50','liquidity_crush_loss','toxic_exit_loss'):
        assert runtime.predict_probability_model('risk',target,{}) is None
    assert seen==[{'family':'risk','target':target,'prediction_kind':'calibrated_binary_probability'}
        for target in ('severe_loss_30','severe_loss_50','liquidity_crush_loss','toxic_exit_loss')]
