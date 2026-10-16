"""Standalone management mutations use the native cycle owner, never a bot run."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from research_loop import batch_runner, checkpoint, evaluator, replay_runner, scoreboard, scheduler, runtime_state
from test_autoresearch_cycle_ownership import blocked_cycle, files

OPERATIONS=['upsert','write','record','evaluate','checkpoint','batch','replay',
    'runtime_write','runtime_update','event','runtime_start','cycle_start','cycle_complete','runtime_error']


def operation(name, root):
    run=root/'data/research_runs/runs/fixture'
    if name=='upsert':return lambda:scoreboard.upsert_scoreboard_entry({'run_id':'new','proposal_id':'p'},root=root)
    if name=='write':return lambda:scoreboard.write_scoreboard([{'run_id':'new'}],root=root)
    if name=='record':return lambda:scoreboard.record_evaluation(run_id='new',candidate_policy={},
        evaluation_result=evaluator.EvaluationResult(status='failed',accepted=False),root=root)
    if name=='evaluate':return lambda:scoreboard.record_run_evaluation(run,root=root)
    if name=='checkpoint':return lambda:checkpoint.save_checkpoint(checkpoint.empty_checkpoint(),root=root)
    if name=='batch':return lambda:batch_runner.run_research_batch(space_name='moonshot_micro',n=0,
        candidates=[],baseline_metrics={},write_candidates=False,root=root)
    if name=='replay':return lambda:replay_runner.run_research_replay_from_sandbox(run,root=root,regenerate=False)
    if name=='runtime_write':return lambda:runtime_state.write_runtime_state(root,{'last_status':'other'})
    if name=='runtime_update':return lambda:runtime_state.update_runtime_state(root,{'last_status':'other'})
    if name=='event':return lambda:runtime_state.append_event(root,'SYNTHETIC_NONOWNER')
    if name=='runtime_start':return lambda:runtime_state.record_runtime_start(root,{},once=True,interval_hours=1)
    if name=='cycle_start':return lambda:runtime_state.record_cycle_start(root,{})
    if name=='cycle_complete':return lambda:runtime_state.record_cycle_completion(root,{}, {},next_cycle_at_utc=None)
    if name=='runtime_error':return lambda:runtime_state.record_runtime_error(root,'nonowner')
    raise AssertionError(name)


@pytest.mark.parametrize('name',OPERATIONS)
def test_standalone_writer_rejects_active_native_cycle_before_mutating(tmp_path,monkeypatch,name):
    monkeypatch.setattr(replay_runner,'_build_event_replay_report',lambda *a,**kw:{})
    with blocked_cycle(tmp_path,monkeypatch):
        before=files(tmp_path)
        try:operation(name,tmp_path)()
        except RuntimeError as e:error=str(e)
        else:error=None
        assert error=='autoresearch_already_running'
        assert files(tmp_path)==before


@pytest.mark.parametrize('module',[scoreboard,checkpoint,batch_runner,replay_runner,scheduler])
def test_failed_native_json_write_preserves_original_document(tmp_path,monkeypatch,module):
    target=tmp_path/'original.json';original=b'{"original":"retained"}'
    target.write_bytes(original)
    real_write=Path.write_text;real_fdopen=os.fdopen
    def broken_write(path,text,*args,**kw):
        if path==target:
            with path.open('w',encoding='utf-8') as f:f.write(text[:5])
            raise OSError('synthetic-partial-write')
        return real_write(path,text,*args,**kw)
    class BrokenHandle:
        def __init__(self,handle):self.handle=handle
        def __enter__(self):self.handle.__enter__();return self
        def __exit__(self,*args):return self.handle.__exit__(*args)
        def write(self,text):self.handle.write(text[:5]);raise OSError('synthetic-partial-write')
        def __getattr__(self,name):return getattr(self.handle,name)
    monkeypatch.setattr(Path,'write_text',broken_write)
    monkeypatch.setattr(os,'fdopen',lambda *a,**kw:BrokenHandle(real_fdopen(*a,**kw)))
    with pytest.raises(OSError,match='synthetic-partial-write'):
        module._write_json(target,{'replacement':'must-not-be-partial'})
    assert target.read_bytes()==original
    assert set(tmp_path.iterdir())=={target}


def test_scoreboard_read_modify_write_owner_prevents_lost_independent_entry(tmp_path,monkeypatch):
    entered,release=threading.Event(),threading.Event();errors=[]
    original=scoreboard.load_scoreboard
    def paused(root=None):
        rows=original(root)
        if threading.current_thread().name=='scoreboard-first':
            entered.set();assert release.wait(8)
        return rows
    monkeypatch.setattr(scoreboard,'load_scoreboard',paused)
    def first():
        try:scoreboard.upsert_scoreboard_entry({'run_id':'first','proposal_id':'p'},root=tmp_path)
        except BaseException as e:errors.append(e)
    thread=threading.Thread(target=first,name='scoreboard-first');thread.start()
    try:
        assert entered.wait(4)
        try:scoreboard.upsert_scoreboard_entry({'run_id':'second','proposal_id':'p'},root=tmp_path)
        except RuntimeError as e:error=str(e)
        else:error=None
    finally:
        release.set();thread.join(6);assert not thread.is_alive()
    assert not errors
    assert error=='autoresearch_already_running', 'Unowned overwrite left: '+repr(original(tmp_path))
    scoreboard.upsert_scoreboard_entry({'run_id':'second','proposal_id':'p'},root=tmp_path)
    assert {x['run_id'] for x in original(tmp_path)}=={'first','second'}


@pytest.mark.parametrize('name',OPERATIONS)
def test_spawned_standalone_writer_cannot_enter_owned_root(tmp_path,monkeypatch,name):
    code="""import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[3])
from test_research_state_ownership import operation
try:operation(sys.argv[2],Path(sys.argv[1]).resolve())()
except RuntimeError as e:error=str(e)
else:error=None
print(json.dumps({'error':error}))
"""
    with blocked_cycle(tmp_path,monkeypatch):
        before=files(tmp_path)
        child=subprocess.run([sys.executable,'-c',code,str(tmp_path),name,str(Path(__file__).parent)],
            capture_output=True,text=True,timeout=15,cwd=Path(__file__).resolve().parents[1])
        assert child.returncode==0,child.stderr
        assert json.loads(child.stdout)['error']=='autoresearch_already_running'
        assert files(tmp_path)==before


@pytest.mark.parametrize('name',OPERATIONS)
def test_original_thread_owner_can_borrow_for_nested_mutation(tmp_path,monkeypatch,name):
    from research_loop.cycle_ownership import autoresearch_session,AutoResearchAlreadyRunning
    monkeypatch.setattr(replay_runner,'_build_event_replay_report',lambda *a,**kw:{})
    with autoresearch_session(tmp_path):
        marker=tmp_path/'data/research_runs/autoresearch_cycle.lock'
        before=marker.read_bytes()
        operation(name,tmp_path)()
        assert marker.read_bytes()==before
        with pytest.raises(AutoResearchAlreadyRunning):
            with autoresearch_session(tmp_path):pass
    assert not marker.exists()


def test_another_root_is_independent_and_original_exception_releases_owner(tmp_path,monkeypatch):
    from research_loop.cycle_ownership import autoresearch_session
    other=tmp_path/'other'
    with autoresearch_session(tmp_path):
        scoreboard.upsert_scoreboard_entry({'run_id':'other'},root=other)
    error=OSError('original-load-failure');original=scoreboard.load_scoreboard
    def fail(*a):raise error
    monkeypatch.setattr(scoreboard,'load_scoreboard',fail)
    with pytest.raises(OSError) as caught:scoreboard.upsert_scoreboard_entry({},root=tmp_path)
    assert caught.value is error
    monkeypatch.setattr(scoreboard,'load_scoreboard',original)
    scoreboard.upsert_scoreboard_entry({'run_id':'after'},root=tmp_path)


def test_root_binding_and_original_call_signature_are_preserved(tmp_path,monkeypatch):
    from inspect import signature
    from research_loop.cycle_ownership import owned_mutation
    monkeypatch.chdir(tmp_path)
    def function(value,root=None,*,flag=False):
        monkeypatch.chdir(tmp_path/'moved')
        return value,root,flag
    (tmp_path/'moved').mkdir()
    owned=owned_mutation(function)
    assert signature(owned)==signature(function)
    assert owned('v','owned',flag=True)==('v',tmp_path/'owned',True)
    unknown=tmp_path/'not-created'
    with pytest.raises(TypeError):owned(root=unknown)
    assert not unknown.exists()


@pytest.mark.parametrize('fault',['fdopen','write','flush','fsync','replace'])
def test_atomic_storage_faults_preserve_original_and_remove_unique_temporary(tmp_path,monkeypatch,fault):
    from research_loop import storage
    target=tmp_path/'report.json';target.write_text('{"old":true}',encoding='utf-8')
    original=target.read_bytes();real=os.fdopen
    error=OSError('original-'+fault)
    def fail(*a,**kw):raise error
    class Handle:
        def __init__(self,h):self.h=h
        def __enter__(self):self.h.__enter__();return self
        def __exit__(self,*a):return self.h.__exit__(*a)
        def write(self,text):
            if fault=='write':self.h.write(text[:3]);raise error
            return self.h.write(text)
        def flush(self):
            if fault=='flush':raise error
            return self.h.flush()
        def fileno(self):return self.h.fileno()
    monkeypatch.setattr(storage.os,'fdopen',fail if fault=='fdopen' else lambda *a,**kw:Handle(real(*a,**kw)))
    if fault in {'fsync','replace'}:monkeypatch.setattr(storage.os,fault,fail)
    with pytest.raises(OSError) as caught:storage.write_json_document(target,{'new':True})
    assert caught.value is error and target.read_bytes()==original
    assert set(tmp_path.iterdir())=={target}


def test_reader_sees_complete_original_until_atomic_replacement(tmp_path,monkeypatch):
    from research_loop import storage
    target=tmp_path/'report.json';target.write_text('{"old":true}',encoding='utf-8')
    replace=os.replace;observed=[]
    def commit(source,destination):
        observed.append(json.loads(Path(destination).read_text(encoding='utf-8')))
        assert json.loads(Path(source).read_text(encoding='utf-8'))=={'new':'x'*100000}
        return replace(source,destination)
    monkeypatch.setattr(storage.os,'replace',commit)
    storage.write_json_document(target,{'new':'x'*100000})
    assert observed==[{'old':True}]
    assert json.loads(target.read_text(encoding='utf-8'))=={'new':'x'*100000}
    assert set(tmp_path.iterdir())=={target}


@pytest.mark.parametrize('tool',['batch','replay','scoreboard'])
def test_busy_native_cli_rejects_without_writes_or_rewrite_read(tmp_path,monkeypatch,tool):
    from tools import run_research_batch,run_research_replay,research_scoreboard
    module={'batch':run_research_batch,'replay':run_research_replay,'scoreboard':research_scoreboard}[tool]
    args={'batch':['--space','moonshot_micro','--n','0'],
        'replay':['--run-dir',str(tmp_path/'data/research_runs/runs/fake'),'--no-regenerate'],
        'scoreboard':['--rewrite']}[tool]
    monkeypatch.setattr(sys,'argv',[tool,'--root',str(tmp_path),*args])
    reads=[]
    if tool=='scoreboard':monkeypatch.setattr(module,'load_scoreboard',lambda *a:reads.append(True) or [])
    with blocked_cycle(tmp_path,monkeypatch):
        before=files(tmp_path)
        assert module.main()==2
        assert files(tmp_path)==before and not reads


def test_scoreboard_read_only_cli_does_not_require_mutation_permission(tmp_path,monkeypatch):
    from tools import research_scoreboard
    monkeypatch.setattr(sys,'argv',['scoreboard','--root',str(tmp_path)])
    with blocked_cycle(tmp_path,monkeypatch):
        before=files(tmp_path)
        assert research_scoreboard.main()==0
        assert files(tmp_path)==before


@pytest.mark.parametrize('name',['checkpoint','baseline','replay'])
def test_explicit_destination_cannot_escape_the_declared_owner_root(tmp_path,monkeypatch,name):
    root=tmp_path/'owned';root.mkdir();monkeypatch.chdir(root)
    foreign=tmp_path/'foreign';foreign.mkdir();before=files(foreign)
    monkeypatch.setattr(replay_runner,'_build_event_replay_report',lambda *a,**kw:{})
    if name=='checkpoint':call=lambda:checkpoint.save_checkpoint({},root=root,path=foreign/'checkpoint.json')
    elif name=='baseline':call=lambda:batch_runner.build_batch_baseline(root=root,batch_dir=foreign,regenerate=False)
    else:call=lambda:replay_runner.run_research_replay_from_sandbox(foreign,root=root,regenerate=False)
    with pytest.raises(ValueError,match='research_artifact_outside_owned_root'):call()
    assert files(foreign)==before


@pytest.mark.parametrize('name',['checkpoint','replay'])
def test_project_relative_artifact_is_bound_to_declared_root_not_caller_cwd(tmp_path,monkeypatch,name):
    root=tmp_path/'owned';root.mkdir();caller=tmp_path/'caller';caller.mkdir();monkeypatch.chdir(caller)
    monkeypatch.setattr(replay_runner,'_build_event_replay_report',lambda *a,**kw:{})
    if name=='checkpoint':
        result=checkpoint.save_checkpoint({'runs':[]},root=root,path='relative/checkpoint.json')
        assert result==root/'relative/checkpoint.json' and result.is_file()
    else:
        result=replay_runner.run_research_replay_from_sandbox('relative/run',root=root,regenerate=False)
        assert result.run_dir==root/'relative/run' and result.replay_metrics_path.is_file()
    assert files(caller)=={}
