"""Explicit family inputs obey the same source boundary, before any fitting."""
import json

import pandas as pd
import pytest

from ml import family_training as family
from ml.financial_targets import checked_financial_frame
from test_financial_cohort_sources import financial


@pytest.mark.parametrize('field,physical', [('age_minutes', 300.), ('score_total', 0.),
    ('entry_lane', 'out_of_scope'), ('dex_id', 'orca'), ('entry_regime', 'dex_mature')])
def test_explicit_family_loader_restores_inputs_before_early_family_work(field, physical):
    frame = financial()
    expected = frame[field].iloc[0]
    proof = frame.outcome_execution_proof.iloc[0]
    frame[field] = physical
    before = frame.copy(deep=True)
    loaded = family.load_training_frame(frame)
    assert loaded[field].tolist() == [expected]
    assert loaded.outcome_execution_proof.tolist() == [proof]
    assert loaded.attrs['cohort_source_preparation']['restored_rows'] == 1
    pd.testing.assert_frame_equal(frame, before)


def conflicting(fault):
    good = financial(); bad = good.copy(deep=True)
    key, value = {'net': ('target_total_pnl_pct', 999.),
        'future_t0': ('timestamp', '2099-01-01T00:00:00Z'),
        'closed': ('outcome_closed_at', '2099-01-01T00:00:00Z'),
        'source_hash': ('outcome_source_sha256', '0' * 64),
        'source_body': ('outcome_execution_proof', '{}')}[fault]
    bad[key] = value
    return pd.concat([good, bad], ignore_index=True)


@pytest.mark.parametrize('fault', ['net', 'future_t0', 'closed', 'source_hash', 'source_body'])
def test_label_and_timing_removal_cannot_hide_a_conflicting_copy(fault):
    frame = conflicting(fault)
    frame.attrs['cohort_source_preparation'] = {'ready': True, 'rejected_rows': 0}
    before = frame.copy(deep=True)
    loaded = family.load_training_frame(frame)
    assert loaded.empty and checked_financial_frame(family._settled_training_frame(loaded))[0].empty
    proof = loaded.attrs['cohort_source_preparation']
    assert proof['rejected_rows'] == 2
    assert proof['financial_validation']['conflicting_trade_ids'] == frame.iloc[[0]].outcome_trade_id.tolist()
    pd.testing.assert_frame_equal(frame, before)


def test_explicit_caller_population_is_not_restricted_by_default_productive_allowlists(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Explicit caller population must not acquire default lane/DEX filters')
    monkeypatch.setattr(family, '_filter_outcome_training_rows', forbidden)
    frame = financial(entry_lane='different-original-lane', dex_id='orca', entry_regime='dex_mature')
    loaded = family.load_training_frame(frame)
    assert len(loaded) == 1
    assert loaded.entry_lane.tolist() == ['different-original-lane']
    assert loaded.dex_id.tolist() == ['orca']


def test_valid_duplicates_collapse_but_undeclared_legacy_duplicates_remain_diagnostic():
    good = financial(); good.index = ['repeated']
    legacy = pd.DataFrame([{'address': 'legacy', 'sample_type': 'shadow_close',
        'timestamp': '2026-10-01T11:00:00Z', 'target_total_pnl_pct': 123.,
        'label': 1, 'diagnostic': 'keep'}], index=['repeated'])
    frame = pd.concat([legacy, good, legacy, good])
    before = frame.copy(deep=True)
    loaded = family.load_training_frame(frame)
    assert loaded.index.tolist() == ['repeated'] * 3
    assert loaded.address.tolist() == ['legacy', good.address.iloc[0], 'legacy']
    proof = loaded.attrs['cohort_source_preparation']
    assert proof['legacy_rows'] == 2 and proof['duplicates_removed'] == 1
    assert loaded.loc[loaded.address.eq('legacy'), 'diagnostic'].tolist() == ['keep', 'keep']
    pd.testing.assert_frame_equal(frame, before)


class ObservedBeforeFit(Exception):
    pass


@pytest.mark.parametrize('kind', ['classifier', 'regressor'])
@pytest.mark.parametrize('population', ['changed-age', 'conflict'])
def test_actual_family_entry_prepares_sources_before_semantic_selection_without_fitting(
        monkeypatch, tmp_path, kind, population):
    frame = financial() if population == 'changed-age' else conflicting('future_t0')
    if population == 'changed-age':
        frame['age_minutes'] = 300.
    before = frame.copy(deep=True)
    observed = []
    def stop_before_fit(data, **kwargs):
        observed.append(data.copy(deep=True))
        raise ObservedBeforeFit
    monkeypatch.setattr(family, 'prepare_training_frame', stop_before_fit)
    monkeypatch.setattr(family, '_save_family_model', lambda *a, **k: pytest.fail('No model publication'))
    call = family.train_classifier_family if kind == 'classifier' else family.train_regressor_family
    with pytest.raises(ObservedBeforeFit):
        call(family='risk' if kind == 'classifier' else 'ev',
            targets=['severe_loss_30' if kind == 'classifier' else 'ev_realized'],
            feature_set_name='risk_features' if kind == 'classifier' else 'ev_features',
            frame=frame, output_dir=tmp_path / 'unused-models')
    assert len(observed) == 1
    if population == 'changed-age':
        assert observed[0].age_minutes.tolist() == [8.]
    else:
        assert observed[0].empty
    assert not (tmp_path / 'unused-models').exists()
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize('net', [-150., -3., 500., 1000., 5000., 50000.])
def test_original_net_returns_remain_signed_and_uncapped_in_explicit_source(net):
    frame = financial(target_total_pnl_pct=net)
    loaded = family.load_training_frame(frame)
    assert loaded.target_total_pnl_pct.tolist() == pytest.approx([net])
    assert json.loads(loaded.outcome_execution_proof.iloc[0]) == json.loads(frame.outcome_execution_proof.iloc[0])


def test_empty_explicit_frame_is_a_normal_diagnostic_load():
    frame = pd.DataFrame()
    loaded = family.load_training_frame(frame)
    assert loaded.empty
    assert loaded.attrs['cohort_source_preparation']['source_rows'] == 0
    assert frame.empty and not frame.attrs


@pytest.mark.parametrize('fault', ['net', 'future_t0', 'closed', 'source_hash', 'source_body'])
def test_actual_runner_advisory_cannot_hide_conflicts_before_its_temporal_filter(
        monkeypatch, tmp_path, fault):
    from ml import runner_advisory_learning as runner
    def forbidden(*args, **kwargs):
        pytest.fail('No candidate fitting for an empty checked population')
    monkeypatch.setattr(runner, 'train_classifier_family', forbidden)
    frame = conflicting(fault)
    before = frame.copy(deep=True)
    report = runner.train_runner_advisory(root=tmp_path, frame=frame,
        as_of='2026-10-15T12:00:00Z')
    assert report['status'] == 'insufficient_settled_data'
    assert report['source_rows'] == report['settled_rows'] == 0
    assert report['cohort_source_preparation']['source_rows'] == 2
    assert report['cohort_source_preparation']['rejected_rows'] == 2
    assert not report['updated'] and not report['automatic_live_activation']
    assert not report['buy_permission'] and not report['position_size_change']
    assert not report['exit_policy_change']
    assert not list(tmp_path.rglob('*.pkl'))
    assert not list(tmp_path.rglob('*.joblib'))
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize('kind', ['classifier', 'regressor', 'exit'])
def test_public_family_report_keeps_source_rejections_separate_from_final_population(tmp_path, kind):
    frame = conflicting('net')
    kwargs = {'frame': frame, 'output_dir': tmp_path / 'unused-models'}
    if kind == 'exit':
        report = family.train_exit_classifier(**kwargs)
    elif kind == 'classifier':
        report = family.train_classifier_family(family='risk', targets=['severe_loss_30'],
            feature_set_name='risk_features', **kwargs)
    else:
        report = family.train_regressor_family(family='ev', targets=['ev_realized'],
            feature_set_name='ev_features', **kwargs)
    assert report['status'] == 'skipped' and report['rows'] == 0
    proof = report['cohort_source_preparation']
    assert proof['source_rows'] == proof['declared_rows'] == proof['rejected_rows'] == 2
    assert proof['restored_rows'] == 0
    assert len(proof['financial_validation']['conflicting_trade_ids']) == 1
    assert not (tmp_path / 'unused-models').exists()
