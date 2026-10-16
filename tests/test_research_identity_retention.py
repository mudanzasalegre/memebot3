"""Immutable original identities in temporary native management paths only."""
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_loop import batch_runner,candidate_generator,replay_runner,sandbox
from research_loop import checkpoint,scoreboard
from test_autoresearch_batch_runner import _candidate
from test_autoresearch_cycle_ownership import blocked_cycle,files


def frozen(monkeypatch,module):
    class Frozen(dt.datetime):
        @classmethod
        def now(cls,tz=None):return cls(2026,10,16,5,0,0,tzinfo=dt.timezone.utc)
    monkeypatch.setattr(module,'dt',SimpleNamespace(datetime=Frozen,timezone=dt.timezone))


def batch(root,**kw):
    return batch_runner.run_research_batch(root=root,space_name='moonshot_micro',n=0,candidates=[],
        baseline_metrics={'diagnostic':kw.pop('diagnostic','first')},write_candidates=False,**kw)


def replay_setup(monkeypatch,failed=False):
    calls=[]
    def report(*a,**kw):
        calls.append(True)
        if failed:raise RuntimeError('original-synthetic-replay-failure')
        return {}
    monkeypatch.setattr(replay_runner,'_build_event_replay_report',report)
    monkeypatch.setattr(replay_runner,'_copy_snapshot',lambda *a,**kw:[])
    return calls


def test_default_batches_in_one_second_preserve_distinct_original_baselines(tmp_path,monkeypatch):
    frozen(monkeypatch,batch_runner)
    first=batch(tmp_path);before=files(first.batch_dir)
    second=batch(tmp_path,diagnostic='second')
    assert first.batch_id!=second.batch_id
    assert files(first.batch_dir)==before


@pytest.mark.parametrize('occupied',['empty','evidence'])
def test_explicit_batch_identity_is_exclusive_before_any_catalog_or_report_write(tmp_path,occupied):
    directory=tmp_path/'data/research_runs/batches/explicit';directory.mkdir(parents=True)
    if occupied=='evidence':(directory/'baseline_metrics.json').write_text('{"original":true}')
    before=files(tmp_path)
    with pytest.raises(RuntimeError,match='batch_identity_already_used'):batch(tmp_path,batch_id='explicit')
    assert files(tmp_path)==before


def test_default_sandboxes_in_one_second_have_distinct_execution_identity(tmp_path,monkeypatch):
    frozen(monkeypatch,sandbox)
    first=sandbox.create_candidate_sandbox(_candidate('proposal'),root=tmp_path);before=files(first.run_dir)
    second=sandbox.create_candidate_sandbox(_candidate('proposal'),root=tmp_path)
    assert first.run_id!=second.run_id and files(first.run_dir)==before


@pytest.mark.parametrize('run_id',['explicit','explicit/'])
def test_reused_explicit_sandbox_identity_never_overwrites_original_inputs(tmp_path,run_id):
    first=sandbox.create_candidate_sandbox(_candidate('proposal'),root=tmp_path,run_id='explicit')
    before=files(first.run_dir)
    with pytest.raises(RuntimeError,match='research_run_identity_already_used'):
        sandbox.create_candidate_sandbox(_candidate('other',{'MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL':'90'}),root=tmp_path,run_id=run_id)
    assert files(first.run_dir)==before


@pytest.mark.parametrize('failed',[False,True])
def test_completed_or_failed_replay_cannot_reuse_original_snapshot(tmp_path,monkeypatch,failed):
    calls=replay_setup(monkeypatch,failed)
    original=sandbox.create_candidate_sandbox(_candidate('proposal'),root=tmp_path)
    replay_runner.run_research_replay_from_sandbox(original,root=tmp_path,regenerate=False)
    before=files(original.run_dir)
    with pytest.raises(RuntimeError,match='replay_identity_already_used'):
        replay_runner.run_research_replay_from_sandbox(original,root=tmp_path,regenerate=False)
    assert len(calls)==1 and files(original.run_dir)==before


@pytest.mark.parametrize('artifact',['report_snapshot','replay_metrics.json','replay_result.json','candidate_diff.md'])
def test_partial_replay_identity_is_retained_before_regeneration(tmp_path,monkeypatch,artifact):
    calls=replay_setup(monkeypatch)
    original=sandbox.create_candidate_sandbox(_candidate('proposal'),root=tmp_path)
    target=original.run_dir/artifact
    if artifact=='report_snapshot':target.mkdir()
    else:target.write_text('original')
    before=files(original.run_dir);regenerated=[]
    with pytest.raises(RuntimeError,match='replay_identity_already_used'):
        replay_runner.run_research_replay_from_sandbox(original,root=tmp_path,
            regenerate_func=lambda root:regenerated.append(root) or {})
    assert not calls and not regenerated and files(original.run_dir)==before


@pytest.mark.parametrize('artifact',['baseline_snapshot','baseline_metrics.json','baseline_report.json'])
def test_original_batch_baseline_rejects_reuse_before_regeneration(tmp_path,artifact):
    directory=tmp_path/'data/research_runs/batches/explicit';directory.mkdir(parents=True)
    target=directory/artifact
    if artifact=='baseline_snapshot':target.mkdir()
    else:target.write_text('original')
    before=files(tmp_path);regenerated=[]
    with pytest.raises(RuntimeError,match='baseline_identity_already_used'):
        batch_runner.build_batch_baseline(root=tmp_path,batch_dir=directory,regenerate=True,
            regenerate_func=lambda root:regenerated.append(root) or {})
    assert not regenerated and files(tmp_path)==before


@pytest.mark.parametrize('field',['changes','hypothesis','risk_notes'])
def test_catalog_conflict_cannot_replace_original_proposal_identity(tmp_path,field):
    original=_candidate('proposal');path=candidate_generator.write_candidate_policies([original],root=tmp_path)[0]
    before=path.read_bytes()
    changed={**original,field:({'MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL':'90'} if field=='changes' else ['changed'] if field=='risk_notes' else 'changed')}
    with pytest.raises(RuntimeError,match='candidate_identity_already_used'):
        candidate_generator.write_candidate_policies([changed],root=tmp_path)
    assert path.read_bytes()==before


def test_equivalent_catalog_retry_preserves_original_creation_receipt(tmp_path):
    original=_candidate('proposal');path=candidate_generator.write_candidate_policies([original],root=tmp_path)[0]
    before=path.read_bytes()
    retried={**original,'created_at_utc':'2026-10-16T00:00:00+00:00'}
    assert candidate_generator.write_candidate_policies([retried],root=tmp_path)==[path]
    assert path.read_bytes()==before


def test_all_catalog_conflicts_are_checked_before_writing_any_new_proposal(tmp_path):
    candidate_generator.write_candidate_policies([_candidate('original')],root=tmp_path)
    before=files(tmp_path)
    with pytest.raises(RuntimeError,match='candidate_identity_already_used'):
        candidate_generator.write_candidate_policies([_candidate('new'),_candidate('original',
            {'MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL':'90'})],root=tmp_path)
    assert files(tmp_path)==before


@pytest.mark.parametrize('kind',['traversal','absolute'])
def test_catalog_identity_cannot_select_a_different_destination(tmp_path,monkeypatch,kind):
    tmp_path.mkdir(exist_ok=True);monkeypatch.chdir(tmp_path)
    identity='../escaped-fixture' if kind=='traversal' else str(tmp_path/'escaped-fixture')
    with pytest.raises(RuntimeError,match='candidate_identity_invalid'):
        candidate_generator.write_candidate_policies([_candidate(identity)],root=tmp_path)
    assert not (tmp_path/'escaped-fixture.json').exists()
    assert not (tmp_path/'strategy_proposals/escaped-fixture.json').exists()


@pytest.mark.parametrize('operation',['catalog','sandbox'])
def test_standalone_input_writer_cannot_bypass_active_native_cycle(tmp_path,monkeypatch,operation):
    with blocked_cycle(tmp_path,monkeypatch):
        before=files(tmp_path)
        with pytest.raises(RuntimeError,match='autoresearch_already_running'):
            if operation=='catalog':candidate_generator.write_candidate_policies([_candidate('proposal')],root=tmp_path)
            else:sandbox.create_candidate_sandbox(_candidate('proposal'),root=tmp_path)
        assert files(tmp_path)==before


@pytest.mark.parametrize('kind',['batch','sandbox'])
def test_forced_default_collision_still_rejects_without_modifying_original(tmp_path,monkeypatch,kind):
    if kind=='batch':
        monkeypatch.setattr(batch_runner,'_default_batch_id',lambda *a:'forced')
        call=lambda:batch(tmp_path)
        reason='batch_identity_already_used'
    else:
        monkeypatch.setattr(sandbox,'_default_run_id',lambda *a:'forced')
        call=lambda:sandbox.create_candidate_sandbox(_candidate('p'),root=tmp_path)
        reason='research_run_identity_already_used'
    call();before=files(tmp_path)
    with pytest.raises(RuntimeError,match=reason):call()
    assert files(tmp_path)==before


@pytest.mark.parametrize('reserved',['CON','NUL','COM1','LPT9.txt'])
def test_catalog_rejects_portability_device_names_before_publishing(tmp_path,reserved):
    with pytest.raises(RuntimeError,match='candidate_identity_invalid'):
        candidate_generator.write_candidate_policies([_candidate(reserved)],root=tmp_path)
    assert not (tmp_path/'strategy_proposals/candidates').exists()


@pytest.mark.parametrize('raw',['not-json','[]','null'])
def test_catalog_never_replaces_unreadable_or_nonobject_original(tmp_path,raw):
    path=tmp_path/'strategy_proposals/candidates/proposal.json';path.parent.mkdir(parents=True)
    path.write_text(raw,encoding='utf-8');before=path.read_bytes()
    with pytest.raises(RuntimeError,match='candidate_identity_already_used'):
        candidate_generator.write_candidate_policies([_candidate('proposal')],root=tmp_path)
    assert path.read_bytes()==before


def test_conflicting_proposals_in_one_input_list_publish_nothing(tmp_path):
    with pytest.raises(RuntimeError,match='candidate_identity_already_used'):
        candidate_generator.write_candidate_policies([_candidate('proposal'),_candidate('proposal',
            {'MOONSHOT_MICRO_LOTTERY_CONFIRMATION_PNL':'90'})],root=tmp_path)
    assert not (tmp_path/'strategy_proposals/candidates').exists()


def test_equivalent_proposals_in_one_input_list_keep_first_creation_receipt(tmp_path):
    first=_candidate('proposal');second={**first,'created_at_utc':'2026-10-16T00:00:00+00:00'}
    paths=candidate_generator.write_candidate_policies([first,second],root=tmp_path)
    assert len(paths)==2 and paths[0]==paths[1]
    assert json.loads(paths[0].read_text(encoding='utf-8'))==first


def test_fresh_batch_attempt_does_not_alias_proposal_or_previous_execution(tmp_path):
    from research_loop.replay_runner import ReplayRunResult
    def replay(run):
        path=run.run_dir/'replay_metrics.json';path.write_text('{}')
        return ReplayRunResult(run.run_id,run.run_dir,'completed',path,run.run_dir/'report_snapshot',{},[],[])
    def run():return batch_runner.run_research_batch(root=tmp_path,space_name='moonshot_micro',n=1,
        candidates=[_candidate('proposal')],baseline_metrics={},replay_func=replay)
    first=run();original=tmp_path/'data/research_runs/runs'/first.results[0].run_id;before=files(original)
    # A deliberately fresh temporary campaign, never an operator reset.
    checkpoint.save_checkpoint(checkpoint.empty_checkpoint(),root=tmp_path)
    second=run()
    assert first.results[0].run_id!=second.results[0].run_id
    assert first.results[0].run_id!='proposal' and second.results[0].run_id!='proposal'
    assert files(original)==before
    entries=scoreboard.load_scoreboard(tmp_path)
    assert {row['run_id'] for row in entries}=={first.results[0].run_id,second.results[0].run_id}


def test_duplicate_checkpoints_retain_separate_skipped_batch_receipts(tmp_path):
    from test_autoresearch_batch_runner import _fake_replay
    def run():return batch_runner.run_research_batch(root=tmp_path,space_name='moonshot_micro',n=1,
        candidates=[_candidate('proposal')],baseline_metrics={},replay_func=_fake_replay())
    first=run();second=run();before=checkpoint.load_checkpoint(tmp_path);third=run();after=checkpoint.load_checkpoint(tmp_path)
    assert second.skipped==third.skipped==1
    assert len(before['runs'])==2 and len(after['runs'])==3
    assert {row['run_id'] for row in before['runs']}.issubset({row['run_id'] for row in after['runs']})
    assert len({first.batch_id,second.batch_id,third.batch_id})==3


def test_failed_sandbox_initialization_is_preserved_and_fresh_attempt_can_proceed(tmp_path,monkeypatch):
    original=sandbox._write_env
    error=OSError('original-env-write-failure')
    def fail(*a):raise error
    monkeypatch.setattr(sandbox,'_write_env',fail)
    with pytest.raises(OSError) as caught:sandbox.create_candidate_sandbox(_candidate('p'),root=tmp_path,run_id='failed')
    assert caught.value is error
    directory=tmp_path/'data/research_runs/runs/failed';before=files(directory)
    monkeypatch.setattr(sandbox,'_write_env',original)
    with pytest.raises(RuntimeError,match='research_run_identity_already_used'):
        sandbox.create_candidate_sandbox(_candidate('p'),root=tmp_path,run_id='failed')
    healthy=sandbox.create_candidate_sandbox(_candidate('p'),root=tmp_path)
    assert healthy.run_dir!=directory and healthy.candidate_env_path.is_file() and files(directory)==before


@pytest.mark.parametrize('tool',['batch','replay'])
def test_cli_reports_reused_identity_as_rejected_without_overwrite(tmp_path,monkeypatch,capsys,tool):
    import sys
    from tools import run_research_batch,run_research_replay
    if tool=='batch':
        batch(tmp_path,batch_id='fixed');module=run_research_batch
        args=['--space','moonshot_micro','--n','0','--batch-id','fixed'];reason='batch_identity_already_used'
    else:
        replay_setup(monkeypatch)
        run=sandbox.create_candidate_sandbox(_candidate('proposal'),root=tmp_path)
        replay_runner.run_research_replay_from_sandbox(run,root=tmp_path,regenerate=False)
        module=run_research_replay;args=['--run-dir',str(run.run_dir),'--no-regenerate'];reason='replay_identity_already_used'
    before=files(tmp_path);monkeypatch.setattr(sys,'argv',[tool,'--root',str(tmp_path),*args])
    assert module.main()==2
    assert capsys.readouterr().err.strip()==reason and files(tmp_path)==before


@pytest.mark.parametrize('field',['hypothesis','optimization_targets','risk_notes'])
def test_generated_identity_includes_complete_experiment_definition(tmp_path,monkeypatch,field):
    from dataclasses import replace
    space=candidate_generator.load_generation_space('moonshot_micro')
    first=candidate_generator.generate_candidate_policies(space_name='moonshot_micro',n=1,seed=42)[0]
    changed={'hypothesis':space.hypothesis+' changed','optimization_targets':['runner_capture_ratio'],
        'risk_notes':list(space.risk_notes)+['new diagnostic context']}[field]
    monkeypatch.setattr(candidate_generator,'load_generation_space',lambda name:replace(space,**{field:changed}))
    second=candidate_generator.generate_candidate_policies(space_name='moonshot_micro',n=1,seed=42)[0]
    assert first['changes']==second['changes'] and first['proposal_id']!=second['proposal_id']
    paths=candidate_generator.write_candidate_policies([first,second],root=tmp_path)
    assert len(set(paths))==2


def test_generated_identity_is_stable_across_creation_times_and_keeps_bandit_space():
    from research_loop.bandit import space_from_proposal_id
    first=candidate_generator.generate_candidate_policies(space_name='moonshot_micro',n=1,seed=42,
        created_at_utc='2026-10-15T00:00:00+00:00')[0]
    second=candidate_generator.generate_candidate_policies(space_name='moonshot_micro',n=1,seed=42,
        created_at_utc='2026-10-16T00:00:00+00:00')[0]
    assert first['proposal_id']==second['proposal_id']
    assert space_from_proposal_id(first['proposal_id'])=='moonshot_micro'
