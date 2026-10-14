"""A missing entry price cannot be an observed zero-return peak."""
import ast
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid
import json

import pytest


def native_open(root):
    now = dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc)
    namespace = {'DRY_RUN': False, 'REAL_SHADOW_SIM': True, 'uuid': uuid, 'PROJECT_ROOT': root,
        '_shadow_positions': {}, '_pending_ai_vectors': {}, 'utc_now': lambda: now,
        'price_service': SimpleNamespace(get_price_usd=AsyncMock(return_value=None), get_price=AsyncMock(return_value=None)),
        '_runner_profile_for_subject': lambda token: {}, '_fmt': str,
        'research_runtime': SimpleNamespace(record_shadow_open=Mock(return_value=True),
            restore_shadow_context=Mock(), score_candidate=Mock(return_value={})),
        'log': SimpleNamespace(info=Mock(), debug=Mock()), 'Optional': __import__('typing').Optional,
        'Dict': dict}
    node = next(item for item in ast.parse(Path('run_bot.py').read_text(encoding='utf-8')).body
                if isinstance(item, ast.AsyncFunctionDef) and item.name == '_open_shadow')
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    return namespace


@pytest.mark.asyncio
@pytest.mark.parametrize('price', [None, 0., -1., float('nan'), float('inf'), True])
async def test_native_unpriced_shadow_does_not_invent_zero_peak(tmp_path, price):
    namespace = native_open(tmp_path)
    await namespace['_open_shadow']('synthetic-unpriced', {'address':'synthetic-unpriced'}, price_hint=price, force=True)
    state = namespace['_shadow_positions']['synthetic-unpriced']
    assert state['buy_price_usd'] is None
    assert state['max_pnl_pct_seen'] is None and state['peak_price_usd'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('price', [.01, 1., 100000.])
async def test_positive_entry_reference_has_actual_zero_baseline(tmp_path, price):
    namespace = native_open(tmp_path)
    await namespace['_open_shadow']('synthetic-priced', {'address':'synthetic-priced'}, price_hint=price, force=True)
    state = namespace['_shadow_positions']['synthetic-priced']
    assert state['buy_price_usd'] == state['peak_price_usd'] == price
    assert state['max_pnl_pct_seen'] == 0.


@pytest.mark.asyncio
@pytest.mark.parametrize('close_price', [None, 0., -1., float('nan'), float('inf'), True])
async def test_invalid_close_price_cannot_produce_return_or_erase_known_peak(tmp_path, close_price):
    from test_shadow_close_recovery import tick_namespace
    namespace, state = tick_namespace(tmp_path, Mock(return_value=True))
    state['highest_pnl_pct'] = state['max_pnl_pct_seen'] = 25000.
    namespace['price_service'].get_price.return_value = {'price_usd':close_price}
    await namespace['_tick_shadows']()
    frozen = state['close_observation']
    assert frozen['close']['label'] is None and frozen['close']['pnl_pct'] is None
    assert frozen['close']['extra']['max_pnl_pct_seen'] == 25000.


@pytest.mark.asyncio
async def test_actual_unpriced_open_close_parquet_and_labels_preserve_unknown(tmp_path, monkeypatch):
    import pandas as pd
    from test_shadow_close_recovery import tick_namespace, native_paths
    from ml.label_builder import attach_labels, RUNNER_THRESHOLDS
    store, research = native_paths(tmp_path, monkeypatch)
    namespace = native_open(tmp_path)
    original = dt.datetime(2026, 10, 13, tzinfo=dt.timezone.utc)
    namespace['utc_now'] = lambda: original
    vector = {'address':'synthetic-unpriced', 'timestamp':original, 'entry_regime':'pump_early',
        'entry_lane':'pump_early_sniper_research', 'dex_id':'pumpfun'}
    await namespace['_open_shadow']('synthetic-unpriced', vector, force=True)
    state = namespace['_shadow_positions']['synthetic-unpriced']
    tick, _ = tick_namespace(tmp_path, Mock(wraps=store.append))
    tick['_shadow_positions'] = {'synthetic-unpriced':state}
    tick['research_runtime'] = research
    await tick['_tick_shadows']()
    frame = pd.read_parquet(next(store.DATA_DIR.glob('features_*.parquet')))
    assert len(frame) == 1 and frame.label.isna().all() and frame.target_total_pnl_pct.isna().all()
    assert frame.max_pnl_pct_seen.isna().all()
    labels = attach_labels(frame)
    assert all(labels[f'runner_{threshold}'].isna().all() for threshold in RUNNER_THRESHOLDS)
    assert labels.ev_peak_adjusted.isna().all() and labels.ev_realized_clipped.isna().all()
    assert labels.capture_ratio.isna().all()
    event = json.loads(research.RESEARCH_EVENTS_PATH.read_text().splitlines()[0])
    assert event['max_pnl_pct_seen'] is None and event['pnl_pct'] is None and event['label'] is None
    assert not tick['_shadow_positions'] and tick['_stats']['appended_shadow'] == 1


@pytest.mark.asyncio
async def test_missing_hint_can_use_actual_positive_fallback_reference(tmp_path):
    namespace = native_open(tmp_path)
    namespace['price_service'].get_price_usd.return_value = 0.
    namespace['price_service'].get_price.return_value = {'price_usd':.5}
    await namespace['_open_shadow']('synthetic-fallback', {'address':'synthetic-fallback'}, price_hint=-1., force=True)
    state = namespace['_shadow_positions']['synthetic-fallback']
    assert state['buy_price_usd'] == state['peak_price_usd'] == .5 and state['max_pnl_pct_seen'] == 0.


@pytest.mark.asyncio
@pytest.mark.parametrize('peak', [25000., 100000000.])
async def test_valid_reference_keeps_uncapped_original_peak(tmp_path, peak):
    from test_shadow_close_recovery import tick_namespace
    namespace, state = tick_namespace(tmp_path, Mock(return_value=True))
    state['highest_pnl_pct'] = state['max_pnl_pct_seen'] = peak
    namespace['price_service'].get_price.return_value = None
    await namespace['_tick_shadows']()
    assert state['close_observation']['close']['extra']['max_pnl_pct_seen'] == peak
