"""Whole native guarded entry/monitor/close/export, never an operational launch."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tools.sync_publishing_checkout import EXCLUDED_PARTS, SOURCE_DIRS, allowed_source


HERE = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get('MEMEBOT_TEST_SOURCE_ROOT', str(HERE.parent))).resolve()
SUPPORT = {'tests/quote_fixtures.py', 'tests/native_paper_lifecycle_case.py'}


def native_case(tmp_path, *, case='positive', momentum=5000., monitor=False, log=False, run_id=None):
    assert (SOURCE/'run_bot.py').is_file() and (SOURCE/'config/config.py').is_file()
    isolated = (tmp_path/'owned-source-only').resolve()
    assert isolated.is_relative_to(tmp_path.resolve()) and isolated != SOURCE
    isolated.mkdir()
    copied = 0
    for directory, children, names in os.walk(SOURCE):
        rel_dir = Path(directory).relative_to(SOURCE)
        children[:] = [name for name in children if name not in EXCLUDED_PARTS
            and (rel_dir.parts or name in SOURCE_DIRS)
            and (rel_dir.parts+(name,)) != ('ml','models')]
        for name in names:
            relative = rel_dir/name
            if (not allowed_source(relative) or relative.suffix=='.env' or relative.name=='.env.example'
                    or relative.parts[0] in {'docs','tests','.github','assets','ui'}):
                continue
            target = isolated/relative
            assert target.resolve().is_relative_to(isolated)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE/relative, target)
            copied += 1
    # Test helpers are explicit; even original-C reproduction uses real C runtime code.
    for relative in SUPPORT:
        target = isolated/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE.parent/relative, target)
    keep = {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','LOCALAPPDATA',
        'APPDATA','HOMEDRIVE','HOMEPATH','PROGRAMDATA','PROGRAMFILES','COMSPEC',
        'PATHEXT','NUMBER_OF_PROCESSORS','PROCESSOR_ARCHITECTURE'}
    env = {key:value for key,value in os.environ.items() if key.upper() in keep}
    env.update(DRY_RUN='true',ML_GATE_MODE='shadow',PYTHONDONTWRITEBYTECODE='1',
        PAPER_SNIPER_MODE='true',PAPER_BOOTSTRAP_BLOCK_CLUSTER_BAD='true',
        DEX_WHITELIST='raydium,orca,meteora,pumpswap,pumpfun',
        PUMP_EARLY_EXECUTION_MODE='live',DEX_MATURE_EXECUTION_MODE='live',
        REVIVAL_EXECUTION_MODE='live',SOCIALS_ENABLED='false',
        MPLCONFIGDIR=str(isolated/'cache/matplotlib'))
    if run_id is not None:
        env['MEMEBOT_RUN_ID'] = run_id
    if case in {'shadow','off'}:
        env['PAPER_SNIPER_MODE'] = 'false'
        env['PUMP_EARLY_EXECUTION_MODE'] = case
    if case == 'whitelist':
        env['DEX_WHITELIST'] = 'raydium,orca,meteora'
    options = dict(case=case,momentum=momentum,monitor=monitor,log=log)
    result = subprocess.run([sys.executable,str(isolated/'tests/native_paper_lifecycle_case.py'),
        str(isolated),json.dumps(options)],cwd=isolated,env=env,
        capture_output=True,text=True,timeout=90)
    assert result.returncode==0, result.stdout+'\n'+result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    assert len(rows)==1, result.stdout
    row = rows[0]
    assert row['dry_run'] is True and row['exact_paper_size']==.1
    assert row['operator_files_copied']==0 and row['external_attempts']==[]
    assert row['scoped_modules']>=160
    assert 'Unclosed client session' not in result.stderr
    row['copied_sources'] = copied
    row['runtime_source_sha256'] = hashlib.sha256((SOURCE/'run_bot.py').read_bytes()).hexdigest()
    return row


@pytest.mark.parametrize('momentum',[500.,25000.,1000000.])
def test_full_native_entry_keeps_extreme_signal_exact_size_and_original_identity(tmp_path,momentum):
    row = native_case(tmp_path,momentum=momentum)
    assert len(row['positions'])==1 and row['positions'][0]['closed'] is False
    assert row['entry_signal']==momentum and row['entry_identity_matches'] is True
    assert row['positions'][0]['amount_sol']==.1 and row['positions'][0]['dry_run'] is True
    assert row['entry_run_id']  # Optional file logging must not erase trade lineage.
    assert re.fullmatch(r'\d{8}-[0-9a-f]{23}',row['entry_run_id'])
    assert row['stats']['actual_paper_buys']==1 and row['stats']['actual_paper_buy_attempts']==1
    assert row['pending_buys']==row['pending_sells']==0


@pytest.mark.parametrize('log',[False,True])
def test_full_native_extreme_runner_close_and_idempotent_net_learning(tmp_path,log):
    row = native_case(tmp_path,momentum=1000000.,monitor=True,log=log)
    path = row['progression']
    assert len(path)==8
    assert all(not tick['closed'] and tick['qty']>0 for tick in path[:-1])
    assert path[-2]['peak']>900000 and path[-2]['partials']==6
    assert path[-2]['qty']/row['entry_qty']==pytest.approx(.03,abs=2e-7)
    assert path[-1]['closed'] is True and path[-1]['qty']==0
    assert row['positions'][0]['closed'] is True and row['stats']['sold']==1
    assert row['frozen_signal']==1000000.
    assert row['net_label_pct']<row['gross_label_pct']
    assert row['first_publish']['status']=='written'
    assert row['second_publish']['status']=='already_written'
    assert row['first_publish']['feature_timestamp']==row['second_publish']['feature_timestamp']
    assert row['export_files'] and row['export_rows']==1
    assert [event['event'] for event in row['sql_events']].count('partial_fill')==4
    assert [event['event'] for event in row['sql_events']].count('close')==1
    assert row['pending_buys']==row['pending_sells']==0


def test_native_no_log_preserves_an_explicit_launcher_identity(tmp_path):
    row = native_case(tmp_path,run_id='synthetic-owned-fixture')
    assert row['entry_run_id']=='synthetic-owned-fixture'


@pytest.mark.parametrize('case',['missing_route','missing_cluster','bad_cluster','stale_price',
    'missing_fx','shadow','off','whitelist'])
def test_full_native_missing_evidence_and_explicit_vetoes_never_open_a_position(tmp_path,case):
    row = native_case(tmp_path,case=case)
    assert row['positions']==[] and row['stats']['actual_paper_buys']==0
    assert row['pending_buys']==row['pending_sells']==0
    if case in {'missing_route','stale_price'}:
        assert row['stats']['actual_paper_buy_attempts']==0
        reasons = {e['reason'] for e in row['events'] if e['reason']}
        expected = 'route_unverified' if case=='missing_route' else 'jupiter_price_missing'
        assert any(expected in reason for reason in reasons), reasons
    if case=='missing_fx':
        assert row['stats']['actual_paper_buy_attempts']==1 and 'fx' in row['boundary_calls']
    if case in {'shadow','off','whitelist'}:
        assert row['stats']['actual_paper_buy_attempts']==0
