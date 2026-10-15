"""Privacy-bounded birth diagnostics, not model inputs or entry permission."""
import ast
from copy import deepcopy
import json
from pathlib import Path

import pytest

from runtime.token_birth_enrichment import birth_enrichment_diagnostic


@pytest.mark.parametrize('state',[
    {'source':'solana_rpc','status':'creation_budget'},
    {'source':'solana_rpc','status':'cache_hit'},
    {'source':'birdeye','status':'access_denied','rpc_initialization':{'status':'missing_creation_reference'}},
])
def test_known_labels_remain_scoped_to_local_observations(state):
    token={'token_birth_enrichment':state,'mint_creation_reference':{},'token_birth_observation':{}}
    original=deepcopy(token)
    out=birth_enrichment_diagnostic(token)
    assert out['source']==state['source'] and out['status']==state['status']
    assert out['creation_reference_present'] and out['birth_observation_present']
    assert out['basis']=='local_status_and_presence_not_verified_birth_or_buy_permission'
    assert token==original and 'buy_permission' not in out


@pytest.mark.parametrize('value',[None,False,[],{'source':[],'status':{}},
    {'source':'https://node.example.test/?secret=private','status':'private-credential-value'},
    {'source':'birdeye','status':'provider_error','rpc_initialization':{'status':'private-credential-value'}}])
def test_diagnostic_never_serializes_untrusted_metadata_or_provider_strings(value):
    token={'token_birth_enrichment':value,'mint_creation_reference':{'private':'do not serialize'}}
    out=birth_enrichment_diagnostic(token)
    encoded=json.dumps(out)
    assert len(encoded)<500 and 'private' not in encoded and 'https://' not in encoded
    assert set(out)=={'schema_version','source','status','rpc_status','creation_reference_present','birth_observation_present','basis'}


def test_actual_blocked_event_retains_diagnostic_without_changing_dedup_or_features(tmp_path,monkeypatch):
    from utils import runtime_telemetry
    monkeypatch.setattr(runtime_telemetry,'METRICS_DIR',tmp_path)
    target=tmp_path/'runtime_events.jsonl'
    monkeypatch.setattr(runtime_telemetry,'RUNTIME_EVENTS_PATH',target)
    tree=ast.parse(Path('run_bot.py').read_text(encoding='utf-8'))
    node=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='_record_blocked_before_buy_metric')
    namespace={'_blocked_before_buy_seen':set(),'_stats':{'blocked_before_buy':0,'shadow_only':0},
        '_paper_bootstrap_active':lambda token:False,'record_runtime_event':runtime_telemetry.record_runtime_event}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'run_bot.py','exec'),namespace)
    token={'address':'synthetic-diagnostic-token','token_birth_enrichment':{
        'source':'birdeye','status':'access_denied','rpc_initialization':{'status':'creation_budget'}}}
    for _ in range(2):
        namespace['_record_blocked_before_buy_metric'](token,action='wait',reason='entry_observation:missing_age',stage='entry_snapshot')
    rows=[json.loads(line) for line in target.read_text().splitlines()]
    assert len(rows)==1 and namespace['_stats']['blocked_before_buy']==1
    diagnostic=rows[0]['birth_enrichment_diagnostic']
    assert diagnostic['status']=='access_denied' and diagnostic['rpc_status']=='creation_budget'
    assert not diagnostic['birth_observation_present'] and not diagnostic['creation_reference_present']
    assert 'birth_enrichment_diagnostic' not in rows[0]['feature_snapshot']
    assert rows[0]['reason']=='entry_observation:missing_age'
