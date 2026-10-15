"""Native cohort eligibility, not a recovered original choice or buy permission."""
from copy import deepcopy
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ml import train
from ml.data_contract import apply_data_contract
from ml.lane_taxonomy import (LANE_PUMP_EARLY_PROFIT as PROFIT,
    LANE_PUMP_EARLY_PRIME as PRIME, LANE_PUMP_EARLY_METEOR as METEOR,
    LANE_PUMP_EARLY_BREAKOUT as BREAKOUT)
from features.token_clock_semantics import (capture_clock_proof, checked_row_clock,
    PROOF_COLUMN as CLOCK_PROOF)
from features.auxiliary_semantics import PROOF_COLUMN as ENTRY_PROOF


def row(**changes):
    return {'address':'synthetic-original-token','mint':'synthetic-original-token',
        'timestamp':'2026-10-01T12:00:00+00:00','entry_regime':'pump_early','dex_id':'pumpswap',
        'has_jupiter_route':1,'liquidity_usd':10000.,'score_total':42.,'age_minutes':8.,
        'price_impact_pct':4.,'market_cap_usd':20000.,'price_pct_5m':7.,
        'txns_last_5m':700.,'volume_24h_usd':80000.,'liquidity_is_proxy':0,
        'entry_lane':None, **changes}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    cfg = SimpleNamespace(ML_TRAIN_ENTRY_LANE_ALLOWLIST=PROFIT,
        ML_TRAIN_ALLOW_MISSING_ENTRY_LANE=True, ML_TRAIN_DEX_ALLOWLIST='pumpswap',
        PUMP_EARLY_METEOR_PRIME_ENABLED=False, PUMP_EARLY_BREAKOUT_PROBE_ENABLED=True)
    monkeypatch.setattr(train,'CFG',cfg)
    return cfg


def mask(frame,allowed=PROFIT,**kwargs):
    return train._productive_lane_mask(frame,entry_lane_allowlist=allowed,**kwargs)


@pytest.mark.parametrize(('allowed','price5m','expected'), [
    (METEOR,7.,False),(BREAKOUT,7.,False),(PROFIT,60.,False),
    (PROFIT,7.,True),(BREAKOUT,60.,True),('unrelated_strategy',7.,False)])
def test_fallback_uses_only_allowed_eligibility_families(allowed,price5m,expected):
    frame = pd.DataFrame([row(price_pct_5m=price5m)])
    before = frame.copy(deep=True)
    result,meta = mask(frame,allowed)
    assert result.tolist() == [expected]
    assert meta['rows_missing_lane_metadata_reconstructed'] == int(expected)
    pd.testing.assert_frame_equal(frame,before)


@pytest.mark.parametrize('unknown',[False,True])
def test_original_checked_age_cannot_be_replaced_by_mutable_alias(unknown):
    source = row(age_minutes=None if unknown else 8.)
    source[CLOCK_PROOF] = json.dumps(capture_clock_proof(source,{} if unknown else {'age_minutes':8.}))
    source['age_min'] = 8. if unknown else 300.
    before = deepcopy(source)
    assert checked_row_clock(source) is not None
    result,meta = mask(pd.DataFrame([source]))
    assert result.tolist() == [not unknown]
    assert meta['fallback_current_clock_rows'] == 1 and meta['fallback_legacy_age_rows'] == 0
    assert source == before


def test_mutated_declared_age_is_unusable_not_legacy():
    source = row()
    source[CLOCK_PROOF] = json.dumps(capture_clock_proof(source,{'age_minutes':8.}))
    source['age_minutes'] = 20.
    assert checked_row_clock(source) is None
    result,meta = mask(pd.DataFrame([source]))
    assert result.tolist() == [False] and meta['fallback_declared_clock_unusable_rows'] == 1


@pytest.mark.parametrize('missing',[None,'','unknown','None','nan','<NA>'],
    ids=['null','empty','unknown','none-marker','nan-marker','pandas-marker'])
def test_actual_contract_missing_markers_reach_configured_reconstruction(missing):
    frame = apply_data_contract(pd.DataFrame([row(entry_lane=missing)]))
    before = frame.copy(deep=True)
    result,meta = mask(frame)
    assert result.tolist() == [True] and meta['rows_missing_lane_metadata_reconstructed'] == 1
    denied,meta = mask(frame,allow_missing_entry_lane=False)
    assert denied.tolist() == [False] and meta['fallback_rows'] == 0
    pd.testing.assert_frame_equal(frame,before)


def test_unknown_allowlist_is_not_an_empty_unrestricted_scope():
    result,meta = mask(pd.DataFrame([row()]),'unknown')
    assert result.tolist() == [False] and meta['entry_lane_allowlist'] == ['unknown']


@pytest.mark.parametrize('mcap,expected',[(20000.,True),(25000.,False),(50000.,False)])
def test_prime_only_fallback_requires_prime_geometry(mcap,expected):
    result,_ = mask(pd.DataFrame([row(market_cap_usd=mcap)]),PRIME)
    assert result.tolist() == [expected]


@pytest.mark.parametrize('lane,allowed,expected',[(PROFIT,PROFIT,True),
    (PROFIT,METEOR,False),('pumpswap_profit',PROFIT,True),(PROFIT,'pumpswap_profit',True)])
def test_explicit_original_lane_is_not_reclassified(lane,allowed,expected,monkeypatch):
    def forbidden(*args,**kwargs): raise AssertionError('Explicit metadata needs no fallback or clock reads')
    monkeypatch.setattr(train,'_productive_lane_fallback_mask',forbidden)
    frame = pd.DataFrame([row(entry_lane=lane,age_minutes=None)])
    result,meta = mask(frame,allowed)
    assert result.tolist() == [expected] and meta['rows_missing_lane_metadata_reconstructed'] == 0


@pytest.mark.parametrize('bad',[False,{},[],'invalid','['*1100+'0'+']'*1100],
    ids=['boolean','empty-object','array','malformed','deeply-nested'])
def test_invalid_declared_clock_does_not_use_legacy_age(bad):
    source = row(**{CLOCK_PROOF:bad})
    result,meta = mask(pd.DataFrame([source]))
    assert result.tolist() == [False] and meta['fallback_declared_clock_unusable_rows'] == 1


def test_duplicate_external_indices_preserve_positional_admission_and_original_frame():
    frame = pd.DataFrame([row(entry_lane=METEOR),row(),row(age_minutes=60.)],index=[4,4,4])
    before = frame.copy(deep=True)
    result,meta = mask(frame)
    assert result.tolist() == [False,True,False] and result.index.tolist() == [4,4,4]
    assert meta['rows_missing_lane_metadata'] == 2 and meta['fallback_legacy_age_rows'] == 2
    pd.testing.assert_frame_equal(frame,before)


def test_empty_config_preserves_deliberate_unrestricted_scope():
    result,meta = mask(pd.DataFrame([row()]),'')
    assert result.tolist() == [True] and meta['entry_lane_allowlist'] == []


@pytest.mark.parametrize('kind',['csv','parquet'])
def test_native_partition_load_and_outcome_filter_only_admit_the_allowed_family(tmp_path,monkeypatch,kind):
    source = pd.DataFrame([row(label=1,sample_type='shadow_close',target_total_pnl_pct=100.)])
    path = tmp_path / f'features_202610.{kind}'
    if kind == 'csv': source.to_csv(path,index=False)
    else: source.to_parquet(path,index=False)
    monkeypatch.setattr(train,'DATA_DIR',tmp_path)
    loaded = train._load_dataset()
    assert loaded.entry_lane.tolist() == ['unknown'] and loaded.timestamp.notna().all()
    expected,meta = train._filter_outcome_training_rows(loaded,entry_lane_allowlist=PROFIT)
    assert expected.address.tolist() == ['synthetic-original-token'] and meta['eligible_rows'] == 1
    denied,_ = train._filter_outcome_training_rows(loaded,entry_lane_allowlist=METEOR)
    assert denied.empty and loaded.entry_lane.tolist() == ['unknown']


def test_fixed_parquet_projection_uses_original_age_at_the_exact_threshold(configured):
    source = row(age_minutes=8.1)
    source[CLOCK_PROOF] = json.dumps(capture_clock_proof(source,{'age_minutes':8.1}))
    source['age_minutes'] = float(np.float32(8.1))
    assert source['age_minutes'] > 8.1 and checked_row_clock(source) is not None
    configured.PUMP_EARLY_PROFIT_MAX_AGE_MIN = 8.1
    result,meta = mask(pd.DataFrame([source]))
    assert result.tolist() == [True] and meta['fallback_current_clock_rows'] == 1


@pytest.mark.parametrize('missing',[None,np.nan,pd.NA],ids=['none','nan','pandas-na'])
@pytest.mark.parametrize('storage',['inferred','object','string'])
def test_only_missing_subset_is_examined_and_original_rows_are_not_relabelled(monkeypatch,missing,storage):
    observed = []
    original = train._productive_lane_fallback_mask
    def capture(data,**kwargs):
        observed.extend(data.address.tolist())
        return original(data,**kwargs)
    monkeypatch.setattr(train,'_productive_lane_fallback_mask',capture)
    frame = pd.DataFrame([row(address='explicit',entry_lane=PROFIT),
        row(address='missing',entry_lane=missing)],index=[8,8])
    if storage != 'inferred':
        frame['entry_lane'] = frame['entry_lane'].astype(storage)
    before = frame.copy(deep=True)
    result,meta = mask(frame)
    assert result.tolist() == [True,True] and observed == ['missing']
    pd.testing.assert_frame_equal(frame,before)
    assert meta['fallback_basis'] == 'configured_eligibility_not_recovered_original_choice'


def test_recognized_secondary_metadata_can_fill_an_unknown_primary_lane():
    frame = pd.DataFrame([row(entry_lane='unknown',profit_lane_tier='pumpswap_profit',age_minutes=None)])
    result,meta = mask(frame)
    assert result.tolist() == [True] and meta['rows_missing_lane_metadata'] == 0


def test_union_keeps_each_configured_family_without_double_counting():
    frame = pd.DataFrame([row(),row(price_pct_5m=60.)])
    result,meta = mask(frame,','.join((PROFIT,BREAKOUT)))
    assert result.tolist() == [True,True]
    assert meta['fallback_rows'] == meta['rows_missing_lane_metadata_reconstructed'] == 2


def test_disabled_meteor_is_not_reconstructed_from_standard(configured):
    frame = pd.DataFrame([row(price_pct_5m=150.,txns_last_5m=700.,score_total=42.)])
    result,_ = mask(frame,METEOR)
    assert result.tolist() == [False]
    configured.PUMP_EARLY_METEOR_PRIME_ENABLED = True
    result,meta = mask(frame,METEOR)
    assert result.tolist() == [True] and meta['fallback_meteor_rows'] == 1


def test_empty_frame_is_well_formed():
    result,meta = mask(pd.DataFrame(index=pd.Index([],name='original')))
    assert result.empty and meta['entry_lane_rows'] == meta['fallback_rows'] == 0


def test_embedded_native_entry_clock_is_consumed_without_a_standalone_marker():
    import datetime as dt
    from features.builder import build_feature_vector
    from features.auxiliary_semantics import input_frame
    from runtime.trade_learning import freeze_entry_features
    stamp = dt.datetime(2026,10,1,12,tzinfo=dt.timezone.utc)
    token = {'address':'synthetic-original-token','created_at':stamp-dt.timedelta(minutes=8)}
    vector = build_feature_vector(token,now=stamp)
    proof = freeze_entry_features(vector,address=token['address'],captured_at=stamp)
    source = input_frame(vector).iloc[0].to_dict()
    source.pop(CLOCK_PROOF)
    source[ENTRY_PROOF] = json.dumps(proof)
    source['age_min'] = 300.
    ages,meta = train._cohort_age_series(pd.DataFrame([source]))
    assert ages.tolist() == [8.] and meta['fallback_current_clock_rows'] == 1


def test_declared_legacy_entry_cannot_be_upgraded_to_a_current_original_clock():
    from runtime.trade_learning import freeze_entry_features
    source = row()
    source[ENTRY_PROOF] = json.dumps(freeze_entry_features(source,
        address=source['address'],captured_at=source['timestamp']))
    result,meta = mask(pd.DataFrame([source]))
    assert result.tolist() == [False] and meta['fallback_declared_clock_unusable_rows'] == 1
    # Existing explicit strategy metadata is separate from inferred eligibility.
    source['entry_lane'] = PROFIT
    assert mask(pd.DataFrame([source]))[0].tolist() == [True]


def test_primary_financial_restoration_precedes_cohort_eligibility():
    from net_financial_fixtures import net_frame,mint_for
    from ml.financial_targets import checked_financial_frame
    source = row(address=mint_for('synthetic-original-token'),label=1,target_total_pnl_pct=100.)
    source['mint'] = source['address']
    source[CLOCK_PROOF] = json.dumps(capture_clock_proof(source,{'age_minutes':8.}))
    financial = net_frame(pd.DataFrame([source]))
    financial['age_minutes'] = 300.
    financial['age_min'] = 300.
    restored,proof = checked_financial_frame(financial)
    assert len(restored) == 1 and proof['ready'] and restored.age_minutes.tolist() == [8.]
    result,meta = mask(restored)
    assert result.tolist() == [True] and meta['fallback_current_clock_rows'] == 1
    assert financial.age_minutes.tolist() == [300.], 'Do not rewrite retained source data'
