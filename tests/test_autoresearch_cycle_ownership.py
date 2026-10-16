"""Actual management paths, temporary roots and fake work; no model/strategy fit."""
import ast
import asyncio
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from research_loop import scheduler
from test_entry_observation import run_function


def files(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob('*') if p.is_file() and not p.name.endswith('.guard')}


@contextmanager
def blocked_cycle(root,monkeypatch):
    started,release=threading.Event(),threading.Event();errors=[];results=[]
    original=scheduler._runtime_event
    def event(*args,**kwargs):
        if args[1]==scheduler.EVENT_AUTORESEARCH_CYCLE_START and threading.current_thread().name=='synthetic-autoresearch-owner':
            started.set();assert release.wait(8)
        return original(*args,**kwargs)
    monkeypatch.setattr(scheduler,'_runtime_event',event)
    def work():
        try:results.append(scheduler.run_autoresearch_cycle(root=root,config={'enabled':False}))
        except BaseException as e:errors.append(e)
    owner=threading.Thread(target=work,name='synthetic-autoresearch-owner');owner.start()
    try:
        assert started.wait(4);yield
    finally:
        release.set();owner.join(6);assert not owner.is_alive()
        assert not errors,errors
        assert len(results)==1 and results[0].status=='disabled'


@pytest.mark.parametrize('operation',['cycle','demotion'])
@pytest.mark.parametrize('process',['thread','spawned'])
def test_nonowner_cannot_mutate_any_cycle_artifact(tmp_path,monkeypatch,operation,process):
    with blocked_cycle(tmp_path,monkeypatch):
        before=files(tmp_path)
        if process=='thread':
            try:
                if operation=='cycle':scheduler.run_autoresearch_cycle(root=tmp_path,config={'enabled':False})
                else:scheduler.evaluate_paper_profitability_for_demotion(root=tmp_path)
            except RuntimeError as e:result=str(e)
            else:result=None
        else:
            code="""import json,sys
from research_loop import scheduler
try:
 if sys.argv[2]=='cycle':scheduler.run_autoresearch_cycle(root=sys.argv[1],config={'enabled':False})
 else:scheduler.evaluate_paper_profitability_for_demotion(root=sys.argv[1])
except RuntimeError as e:result=str(e)
else:result=None
print(json.dumps({'error':result}))
"""
            child=subprocess.run([sys.executable,'-c',code,str(tmp_path),operation],capture_output=True,text=True,timeout=6)
            assert child.returncode==0,child.stderr
            result=json.loads(child.stdout)['error']
        assert result=='autoresearch_already_running', 'Another process/thread entered the active mutation scope'
        assert files(tmp_path)==before, 'Nonowner changed original cycle/report/runtime state'


@pytest.mark.asyncio
@pytest.mark.parametrize('cancellations',[1,3])
async def test_runtime_control_cancellation_drains_native_cycle_before_releasing_owner(tmp_path,monkeypatch,cancellations):
    started,release,finished=threading.Event(),threading.Event(),threading.Event()
    def work(**kwargs):
        started.set()
        try:assert release.wait(6);return SimpleNamespace(as_dict=lambda:{'status':'disabled'})
        finally:finished.set()
    monkeypatch.setattr(scheduler,'run_autoresearch_cycle',work)
    function=run_function('_run_autoresearch_once_from_control')
    ns={'asyncio':asyncio,'_autoresearch_lock':asyncio.Lock(),'PROJECT_ROOT':tmp_path}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'run_bot.py','exec'),ns)
    task=asyncio.create_task(ns[function.name]({}))
    try:
        for _ in range(1000):
            if started.is_set():break
            await asyncio.sleep(.001)
        assert started.is_set()
        for _ in range(cancellations):
            task.cancel();await asyncio.sleep(.01)
            assert not task.done(), 'Cancellation detached AutoResearch writes from the control owner'
            assert ns['_autoresearch_lock'].locked()
    finally:
        release.set();await asyncio.gather(task,return_exceptions=True);assert finished.wait(2)
    assert not ns['_autoresearch_lock'].locked() and task.cancelled()


def test_sequential_cycles_in_the_same_clock_second_preserve_both_original_reports(tmp_path,monkeypatch):
    class Frozen(dt.datetime):
        @classmethod
        def now(cls,tz=None):return cls(2026,10,16,4,0,0,tzinfo=dt.timezone.utc)
    monkeypatch.setattr(scheduler,'dt',SimpleNamespace(datetime=Frozen,timezone=dt.timezone,timedelta=dt.timedelta))
    first=scheduler.run_autoresearch_cycle(root=tmp_path,config={'enabled':False})
    original=first.cycle_report_path.read_bytes()
    second=scheduler.run_autoresearch_cycle(root=tmp_path,config={'enabled':False})
    assert first.cycle_id!=second.cycle_id, 'Clock-only identity overwrote the first original cycle'
    assert first.cycle_report_path.read_bytes()==original and second.cycle_report_path.is_file()


def test_cli_nonowner_does_not_publish_start_error_or_stop_for_someone_elses_cycle(tmp_path,monkeypatch):
    from tools import run_autoresearch_loop as cli
    monkeypatch.setenv('AUTORESEARCH_ENABLED','false')
    monkeypatch.setattr(sys,'argv',['run_autoresearch_loop.py','--root',str(tmp_path),'--once','--no-demotion'])
    with blocked_cycle(tmp_path,monkeypatch):
        before=files(tmp_path)
        assert cli.main()==2, 'A second CLI acted as a new runtime owner'
        assert files(tmp_path)==before


@pytest.mark.parametrize('fault',['start','report','bundle'])
def test_owned_cycle_exception_releases_for_the_next_checked_attempt(tmp_path,monkeypatch,fault):
    if fault=='start':
        original=scheduler._runtime_event
        def broken(*args,**kw):raise OSError('synthetic-start')
        name='_runtime_event'
    elif fault=='report':
        original=scheduler._write_cycle_report
        def broken(*args,**kw):raise OSError('synthetic-report')
        name='_write_cycle_report'
    else:
        original=scheduler.build_report_bundle
        def broken(*args,**kw):raise OSError('synthetic-bundle')
        name='build_report_bundle'
        monkeypatch.setattr(scheduler,'_research_data_warnings',lambda *a:[])
    monkeypatch.setattr(scheduler,name,broken)
    with pytest.raises(OSError,match='synthetic'):
        scheduler.run_autoresearch_cycle(root=tmp_path,config={'enabled':fault=='bundle','profitability_demotion_enabled':False})
    monkeypatch.setattr(scheduler,name,original)
    assert scheduler.run_autoresearch_cycle(root=tmp_path,config={'enabled':False}).status=='disabled'


@pytest.mark.parametrize('crash',[False,True])
def test_os_owner_release_after_actual_child_exit_does_not_trust_old_marker_age(tmp_path,crash):
    code="""import sys
from research_loop.cycle_ownership import autoresearch_session
with autoresearch_session(sys.argv[1]):
 print('owned',flush=True)
 sys.stdin.readline()
"""
    child=subprocess.Popen([sys.executable,'-c',code,str(tmp_path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='owned'
        with pytest.raises(RuntimeError,match='autoresearch_already_running'):
            scheduler.run_autoresearch_cycle(root=tmp_path,config={'enabled':False})
        if crash:child.terminate()
        else:child.stdin.write('release\n');child.stdin.flush()
        child.communicate(timeout=5)
        assert child.poll() is not None
        assert scheduler.run_autoresearch_cycle(root=tmp_path,config={'enabled':False}).status=='disabled'
    finally:
        if child.poll() is None:child.terminate();child.communicate(timeout=5)


def test_other_root_and_training_lock_are_independent_from_autoresearch(tmp_path,monkeypatch):
    from ml import training_lock
    left,right=tmp_path/'left',tmp_path/'right'
    training=left/'data/metrics/training.lock'
    assert training_lock.acquire_lock(training)
    try:
        with blocked_cycle(left,monkeypatch):
            assert scheduler.run_autoresearch_cycle(root=right,config={'enabled':False}).status=='disabled'
    finally:training_lock.release_lock(training)


def test_same_thread_nested_cycle_is_not_permitted_to_overwrite_its_owner(tmp_path,monkeypatch):
    original=scheduler._runtime_event;calls=[]
    def event(*args,**kwargs):
        if args[1]==scheduler.EVENT_AUTORESEARCH_CYCLE_START:
            with pytest.raises(RuntimeError,match='autoresearch_already_running'):
                scheduler.run_autoresearch_cycle(root=tmp_path,config={'enabled':False})
            calls.append('rejected')
        return original(*args,**kwargs)
    monkeypatch.setattr(scheduler,'_runtime_event',event)
    assert scheduler.run_autoresearch_cycle(root=tmp_path,config={'enabled':False}).status=='disabled'
    assert calls==['rejected']


def test_forced_identity_collision_fails_before_any_existing_report_or_event_is_changed(tmp_path,monkeypatch):
    monkeypatch.setattr(scheduler,'_cycle_id',lambda:'autoresearch_fixed_synthetic_identity')
    scheduler.run_autoresearch_cycle(root=tmp_path,config={'enabled':False})
    before=files(tmp_path)
    with pytest.raises(scheduler.AutoResearchSchedulerError,match='cycle_identity_already_used'):
        scheduler.run_autoresearch_cycle(root=tmp_path,config={'enabled':False})
    assert files(tmp_path)==before


@pytest.mark.asyncio
async def test_native_control_rejects_process_busy_without_reporting_completed_or_failed():
    from research_loop.cycle_ownership import AutoResearchAlreadyRunning
    async def work(*args):raise AutoResearchAlreadyRunning()
    function=run_function('_execute_control_command')
    ns={'_autoresearch_lock':asyncio.Lock(),'_run_autoresearch_once_from_control':work,
        'COMMAND_STATUS_REJECTED':'rejected'}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'run_bot.py','exec'),ns)
    assert await ns[function.name]({'command_type':'run_autoresearch'})==(
        'rejected',{'reason':'autoresearch_already_running'},'autoresearch_already_running')


def test_relative_root_is_frozen_to_the_owned_folder_before_a_callback_changes_cwd(tmp_path,monkeypatch):
    left,right=tmp_path/'original',tmp_path/'different'
    left.mkdir();right.mkdir();monkeypatch.chdir(left)
    original=scheduler._runtime_event
    def event(*args,**kwargs):
        if args[1]==scheduler.EVENT_AUTORESEARCH_CYCLE_START:monkeypatch.chdir(right)
        return original(*args,**kwargs)
    monkeypatch.setattr(scheduler,'_runtime_event',event)
    result=scheduler.run_autoresearch_cycle(root=Path('.'),config={'enabled':False})
    assert result.cycle_report_path.is_absolute() and result.cycle_report_path.is_relative_to(left)
    assert not (right/'data').exists(), 'Work escaped the root whose OS ownership was retained'


@pytest.mark.parametrize('target',['absolute','traversal'])
def test_standalone_demotion_cannot_write_a_run_outside_its_owned_project(tmp_path,monkeypatch,target):
    root=tmp_path/'owned';foreign=tmp_path/'foreign';foreign.mkdir()
    root.mkdir();monkeypatch.chdir(root)
    (foreign/'sentinel.txt').write_text('original',encoding='utf-8')
    before=files(foreign)
    supplied=foreign if target=='absolute' else Path('../foreign')
    with pytest.raises(scheduler.AutoResearchSchedulerError,match='paper_run_directory_outside_owned_root'):
        scheduler.evaluate_paper_profitability_for_demotion(root=root,run_id_or_dir=supplied)
    assert files(foreign)==before


def test_demotion_project_relative_path_is_bound_to_root_not_the_callers_cwd(tmp_path,monkeypatch):
    root,elsewhere=tmp_path/'owned',tmp_path/'elsewhere';root.mkdir();elsewhere.mkdir()
    relative=Path('data/research_runs/paper_forward/synthetic_missing_state')
    (root/relative).mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    result=scheduler.evaluate_paper_profitability_for_demotion(root=root,run_id_or_dir=relative)
    assert result.status=='missing_paper_state'
    assert result.demotion_report_path.is_absolute() and result.demotion_report_path.is_relative_to(root)
    assert not (elsewhere/'data').exists()
