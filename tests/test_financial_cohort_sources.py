"""Synthetic source-only cohort regressions; no provider, operator fit or run."""
from copy import deepcopy
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from features.token_clock_semantics import capture_clock_proof, PROOF_COLUMN as CLOCK_PROOF
from features.valuation_semantics import PROOF_COLUMN as VALUATION_PROOF
from ml import train
from ml.cohort_sources import prepare_cohort_sources, cohort_input_view
from ml.financial_targets import checked_financial_frame, checked_net_return
from net_financial_fixtures import net_frame, mint_for


PROFIT = 'pump_early_pumpswap_profit'


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    cfg = SimpleNamespace(ML_TRAIN_ENTRY_LANE_ALLOWLIST=PROFIT,
        ML_TRAIN_DEX_ALLOWLIST='pumpswap', ML_TRAIN_ALLOW_MISSING_ENTRY_LANE=True,
        ML_BOOTSTRAP_ENTRY_LANE_ALLOWLIST=PROFIT, ML_BOOTSTRAP_DEX_ALLOWLIST='pumpswap',
        PUMP_EARLY_METEOR_PRIME_ENABLED=False, PUMP_EARLY_BREAKOUT_PROBE_ENABLED=True)
    monkeypatch.setattr(train, 'CFG', cfg)
    return cfg


def financial(*, clock=True, **changes):
    row = {'address': mint_for('original-financial-cohort'),
        'timestamp': '2026-10-01T12:00:00+00:00', 'entry_regime': 'pump_early',
        'entry_lane': PROFIT, 'dex_id': 'pumpswap', 'has_jupiter_route': 1,
        'liquidity_usd': 10000., 'score_total': 42., 'age_minutes': 8.,
        'price_impact_pct': 4., 'market_cap_usd': 20000., 'price_pct_5m': 7.,
        'txns_last_5m': 700., 'volume_24h_usd': 80000., 'liquidity_is_proxy': 0,
        'target_total_pnl_pct': 100., 'label': 1, **changes}
    row['mint'] = row['address']
    if clock:
        row[CLOCK_PROOF] = capture_clock_proof(row, {'age_minutes': row['age_minutes']})
    return net_frame(pd.DataFrame([row]))


def filtered(frame):
    return train._filter_outcome_training_rows(frame,
        entry_lane_allowlist=PROFIT, dex_allowlist='pumpswap')


@pytest.mark.parametrize(('field', 'original', 'physical', 'expected'), [
    ('entry_lane', PROFIT, 'out_of_scope', 1),
    ('entry_lane', 'pump_early_birth_probe', PROFIT, 0),
    ('dex_id', 'pumpswap', 'orca', 1), ('dex_id', 'orca', 'pumpswap', 0),
    ('entry_regime', 'pump_early', 'dex_mature', 1),
    ('entry_regime', 'dex_mature', 'pump_early', 0),
    ('age_minutes', 8., 300., 1), ('score_total', 0., 42., 0)])
def test_original_inputs_determine_the_shared_financial_cohort(field, original, physical, expected):
    changes = {field: original}
    if field in {'age_minutes', 'score_total'}:
        changes['entry_lane'] = None
    frame = financial(**changes)
    frame[field] = physical
    assert checked_net_return(frame.iloc[0]) is not None
    before = frame.copy(deep=True)
    selected, meta = filtered(frame)
    assert len(selected) == expected
    assert meta['cohort_source_preparation']['restored_rows'] == 1
    if expected:
        assert selected[field].tolist() == [original]
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.parametrize(('canonical', 'alias', 'physical'), [
    ('dex_id', 'dexId', 'pumpswap'), ('dex_id', 'buy_dex_id', 'pumpswap'),
    ('entry_lane', 'size_bucket', PROFIT), ('volume_24h_usd', 'volume_usd_24h', 80000.)])
def test_unowned_physical_aliases_cannot_fill_original_unknowns(canonical, alias, physical):
    changes = {canonical: None}
    if canonical == 'entry_lane':
        changes['score_total'] = 0.
    if canonical == 'volume_24h_usd':
        changes.update(entry_lane=None, price_pct_5m=60.)
    frame = financial(**changes)
    assert filtered(frame)[0].empty
    frame[alias] = physical
    assert filtered(frame)[0].empty
    prepared, _ = prepare_cohort_sources(frame)
    assert prepared[alias].tolist() == [physical]  # Diagnostic copy not destroyed.
    assert cohort_input_view(prepared)[alias].isna().all()


@pytest.mark.parametrize('bad_field', ['target_total_pnl_pct', 'timestamp', 'address',
    'outcome_source_sha256', 'outcome_execution_proof'])
def test_invalid_duplicate_rejects_the_whole_identity_before_selection(bad_field):
    good = financial()
    bad = good.copy(deep=True)
    bad[bad_field] = {'target_total_pnl_pct': 999., 'timestamp': '2026-10-01T12:01:00Z',
        'address': mint_for('different-token'), 'outcome_source_sha256': '0' * 64,
        'outcome_execution_proof': '{}'}[bad_field]
    bad['entry_lane'] = 'out_of_scope'
    frame = pd.concat([good, bad], ignore_index=True)
    frame.attrs['cohort_source_preparation'] = {'ready': True, 'validated': True}
    before = frame.copy(deep=True)
    selected, meta = filtered(frame)
    assert selected.empty
    proof = meta['cohort_source_preparation']
    assert proof['rejected_rows'] == 2
    assert proof['financial_validation']['conflicting_trade_ids'] == good.outcome_trade_id.tolist()
    pd.testing.assert_frame_equal(frame, before)


def test_mixed_legacy_rows_keep_order_repeated_indices_and_diagnostic_fields():
    original = financial()
    original.index = ['same']
    legacy = pd.DataFrame([{'address': 'legacy', 'sample_type': 'shadow_close',
        'timestamp': '2026-10-01T12:00:00Z', 'label': 1, 'target_total_pnl_pct': 123.,
        'unowned_diagnostic': 'keep'}], index=['same'])
    frame = pd.concat([legacy, original, original])
    before = frame.copy(deep=True)
    prepared, meta = prepare_cohort_sources(frame)
    assert prepared.index.tolist() == ['same', 'same']
    assert prepared.address.tolist() == ['legacy', original.address.iloc[0]]
    assert prepared.iloc[0].unowned_diagnostic == 'keep'
    assert meta['legacy_rows'] == meta['duplicates_removed'] == meta['restored_rows'] == 1
    assert meta['rejected_rows'] == 0
    assert not any(str(column).startswith('_cohort') for column in prepared)
    pd.testing.assert_frame_equal(frame, before)


def test_unrelated_valid_identity_survives_another_identity_conflict():
    good = financial()
    # net_frame numbers each input batch; use one batch to assign distinct IDs.
    original = json.loads(good.outcome_execution_proof.iloc[0])['entry_features']['vector']
    other = {**deepcopy(original), 'address': mint_for('other-financial-token')}
    raw = [original, other]
    for row in raw:
        row['target_total_pnl_pct'] = 100.
    two = net_frame(pd.DataFrame(raw))
    bad = two.iloc[[0]].copy(); bad['target_total_pnl_pct'] = 999.
    selected, meta = filtered(pd.concat([two, bad], ignore_index=True))
    assert selected.outcome_trade_id.tolist() == two.iloc[[1]].outcome_trade_id.tolist()
    assert meta['cohort_source_preparation']['rejected_rows'] == 2


def test_checked_original_clock_replaces_only_the_detached_marker():
    frame = financial(entry_lane=None)
    source_bytes = frame.outcome_execution_proof.iloc[0]
    original = json.loads(source_bytes)['entry_features']['token_clock']
    frame[CLOCK_PROOF] = 'invalid-detached-marker'
    selected, _ = filtered(frame)
    assert len(selected) == 1
    assert json.loads(selected[CLOCK_PROOF].iloc[0]) == original
    assert selected.outcome_execution_proof.tolist() == [source_bytes]
    assert frame[CLOCK_PROOF].tolist() == ['invalid-detached-marker']


def test_original_legacy_absence_cannot_be_upgraded_by_detached_markers():
    frame = financial(clock=False)
    assert 'token_clock' not in json.loads(frame.outcome_execution_proof.iloc[0])['entry_features']
    frame[CLOCK_PROOF] = 'forged-upgrade'
    frame[VALUATION_PROOF] = 'forged-upgrade'
    checked, _ = checked_financial_frame(frame)
    assert checked[[CLOCK_PROOF, VALUATION_PROOF]].isna().all().all()
    assert filtered(frame)[0].outcome_trade_id.tolist() == frame.outcome_trade_id.tolist()


@pytest.mark.parametrize('require_label', [True, False])
def test_csv_identifiers_preserve_exact_original_text_before_validation(tmp_path, monkeypatch, require_label):
    original = financial()
    path = tmp_path / 'features_202610.csv'
    original.to_csv(path, index=False)
    assert original.outcome_trade_id.iloc[0] == '0' * 31 + '1'
    direct = train._load_one(str(path))
    assert direct.outcome_trade_id.tolist() == original.outcome_trade_id.tolist()
    assert checked_net_return(direct.iloc[0]) is not None
    monkeypatch.setattr(train, 'DATA_DIR', tmp_path)
    loaded = train._load_dataset(require_label=require_label)
    assert len(loaded) == 1 and checked_net_return(loaded.iloc[0]) is not None
    assert filtered(loaded)[0].outcome_trade_id.tolist() == original.outcome_trade_id.tolist()


@pytest.mark.parametrize('kind', ['csv', 'parquet'])
@pytest.mark.parametrize('require_label', [True, False])
def test_cross_partition_conflict_is_checked_before_label_drop(tmp_path, monkeypatch, kind, require_label):
    good = financial()
    bad = good.copy(); bad['target_total_pnl_pct'] = 999.; bad['label'] = np.nan
    for name, frame in [('202609', good), ('202610', bad)]:
        frame = frame.copy()
        frame[CLOCK_PROOF] = frame[CLOCK_PROOF].map(json.dumps)
        path = tmp_path / f'features_{name}.{kind}'
        if kind == 'csv':
            frame.to_csv(path, index=False)
        else:
            frame.to_parquet(path, index=False)
    monkeypatch.setattr(train, 'DATA_DIR', tmp_path)
    loaded = train._load_dataset(require_label=require_label)
    assert loaded.empty
    report = loaded.attrs['cohort_source_preparation']
    assert report['rejected_rows'] == 2
    assert report['financial_validation']['conflicting_trade_ids'] == good.outcome_trade_id.tolist()


@pytest.mark.parametrize('family', ['ev', 'risk', 'default-family-loader'])
def test_native_default_consumers_receive_original_scoped_rows_without_fitting(
        monkeypatch, tmp_path, configured, family):
    good = financial(); good['dex_id'] = 'orca'
    outside = financial(dex_id='orca'); outside['dex_id'] = 'pumpswap'
    # These represent different declarations even though fixture batches reused
    # one ID. Consume them independently to verify both directions at entry.
    from ml import family_training, train_ev, train_risk
    module = train_ev if family == 'ev' else train_risk if family == 'risk' else family_training
    monkeypatch.setattr(module, 'CFG', configured)
    monkeypatch.setattr(module, '_save_family_model', lambda *a, **k: pytest.fail('No artifact publication'))
    captured = []
    def no_fit(**kwargs):
        captured.append(kwargs['frame'].copy())
        return {'targets': {}}
    if family == 'ev':
        monkeypatch.setattr(module, 'train_regressor_family', no_fit)
        invoke = module.train_ev_model
    elif family == 'risk':
        monkeypatch.setattr(module, 'train_classifier_family', no_fit)
        monkeypatch.setattr(module, 'THRESHOLDS_JSON', tmp_path / 'diagnostics.json')
        invoke = module.train_risk_model
    else:
        monkeypatch.setattr(module, 'enrich_outcome_targets', lambda frame, root: frame)
        invoke = module.load_training_frame
    monkeypatch.setattr(module, '_load_dataset', lambda **kwargs: good.copy(deep=True))
    result = invoke()
    received = result if family == 'default-family-loader' else captured[-1]
    assert received.dex_id.tolist() == ['pumpswap']
    monkeypatch.setattr(module, '_load_dataset', lambda **kwargs: outside.copy(deep=True))
    if family == 'default-family-loader':
        assert invoke().empty
    else:
        with pytest.raises(ValueError, match='requires outcome rows'):
            invoke()
        assert len(captured) == 1


def test_undeclared_legacy_and_empty_frames_are_not_rewritten():
    for frame in (pd.DataFrame(), pd.DataFrame([{'label': 1, 'address': 'legacy', 'alias': 'keep'}])):
        before = frame.copy(deep=True)
        prepared, report = prepare_cohort_sources(frame)
        pd.testing.assert_frame_equal(prepared, before)
        assert report['declared_rows'] == 0 and report['legacy_rows'] == len(frame)


def test_ambiguous_duplicate_columns_fail_closed():
    frame = pd.DataFrame([['a', 'b']], columns=['address', 'address'])
    with pytest.raises(ValueError, match='columns must be unique'):
        prepare_cohort_sources(frame)


@pytest.mark.parametrize('net', [-150., -3., 500., 1000., 5000., 50000.])
def test_source_preparation_preserves_signed_uncapped_net_targets(net):
    frame = financial(target_total_pnl_pct=net)
    selected, _ = filtered(frame)
    assert selected.target_total_pnl_pct.tolist() == pytest.approx([net])
    assert checked_net_return(selected.iloc[0]) == pytest.approx(net)
