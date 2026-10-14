"""Managed candidate telemetry never enters disk IO on the entry loop."""
import ast
import asyncio
import datetime as dt
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from analytics import research_runtime as research


def wrapper(name):
    node = next(item for item in ast.parse(Path('run_bot.py').read_text(encoding='utf-8')).body
                if isinstance(item, ast.FunctionDef) and item.name == name)
    namespace = {'research_runtime': research, '_record_blocked_before_buy_metric': Mock(),
        '_stats': {},
        'log': SimpleNamespace(debug=Mock())}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    return namespace[name]


@pytest.mark.parametrize('kind', ['stage', 'decision'])
def test_unmanaged_bot_wrapper_cannot_fall_back_to_inline_disk(monkeypatch, kind):
    monkeypatch.setattr(research, '_SEEN', {})
    monkeypatch.setattr(research, 'CFG', SimpleNamespace(RESEARCH_STAGE_LOGGING_ENABLED=True,
        RESEARCH_LANE_ENABLED=True, RESEARCH_DECISION_DEDUP_TTL_S=1))
    disk = Mock(side_effect=AssertionError('Disk writer entered entry loop'))
    monkeypatch.setattr(research, '_write_event', disk)
    monkeypatch.setattr(research, 'append_decision', Mock())
    function = wrapper('_research_' + kind)
    kwargs = {'stage': 'synthetic'}
    if kind == 'decision': kwargs.update(action='wait', reason='synthetic')
    function({'address': 'synthetic-candidate'}, **kwargs)
    disk.assert_not_called()


def prepared(*, kind='candidate_decision', address='synthetic-candidate'):
    return {'row': research._build_event_row(kind, address, observed_at=dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc),
        decision_action='wait', reason='synthetic', stage='entry', entry_regime='pump_early', score_total=40),
        'ledger': kind == 'candidate_decision'}


def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(research, 'METRICS_DIR', tmp_path)
    monkeypatch.setattr(research, 'RESEARCH_EVENTS_PATH', tmp_path / 'events.jsonl')
    from features import decision_store
    monkeypatch.setattr(decision_store, 'DECISION_LEDGER_PATH', tmp_path / 'decisions.jsonl')
    monkeypatch.setattr(research, '_SEEN', {})
    monkeypatch.setattr(research, '_CANDIDATE_OWNERS', {})
    monkeypatch.setattr(research, '_LIVE_CONTEXT', {})


async def close_installed(writer):
    try: await writer.close()
    finally:
        if writer.snapshot()['closed']: research.remove_candidate_writer(writer)


@pytest.mark.asyncio
async def test_native_event_and_ledger_use_original_context_and_time_off_loop(tmp_path, monkeypatch):
    from runtime.candidate_events import CandidateEventWriter
    from utils import runtime_context
    paths(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime_context, '_CONTEXT', {'run_id': 'original', 'started_at': '2026-10-14T00:00:00Z', 'test_event': False})
    owner = threading.get_ident()
    observed_threads = []
    actual = research.publish_candidate_event
    def publish(job):
        observed_threads.append(threading.get_ident())
        return actual(job)
    writer = CandidateEventWriter(publish, on_result=research.finish_candidate_admission)
    writer.start()
    research.install_candidate_writer(writer)
    try:
        call = wrapper('_research_decision')
        token = {'address': 'synthetic-candidate', 'entry_regime': 'pump_early', 'score_total': 40,
            'entry_decision': {'original': [1, 2]}}
        call(token, action='wait', reason='synthetic', stage='entry')
        assert call.__globals__['_stats']['candidate_event_admitted'] == 1
        assert call.__globals__['_stats']['candidate_event_persisted'] == 0
        token['score_total'] = 99
        token['entry_decision']['original'].append(3)
        runtime_context._CONTEXT['run_id'] = 'later'
        await close_installed(writer)
    finally:
        if research._CANDIDATE_WRITER is writer: await close_installed(writer)
    event = json.loads((tmp_path / 'events.jsonl').read_text().splitlines()[0])
    ledger = json.loads((tmp_path / 'decisions.jsonl').read_text().splitlines()[0])
    assert event['run_id'] == ledger['run_id'] == 'original'
    assert event['score_total'] == ledger['score_total'] == 40
    assert event['entry_decision']['original'] == [1, 2]
    assert event['ts_utc'] == ledger['ts_utc'] and event['timestamp'] == ledger['timestamp']
    assert observed_threads and owner not in observed_threads
    assert writer.snapshot()['persisted'] == 1 and writer.snapshot()['pending'] == 0


@pytest.mark.asyncio
async def test_capacity_includes_active_and_drops_do_not_claim_dedup_or_buy_context(tmp_path, monkeypatch):
    from runtime.candidate_events import CandidateEventWriter
    paths(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def publish(job):
        entered.set()
        assert release.wait(10)
        return True
    writer = CandidateEventWriter(publish, capacity=1, on_result=research.finish_candidate_admission)
    writer.start()
    research.install_candidate_writer(writer)
    try:
        research.record_candidate_stage({'address': 'active'}, stage='entry')
        for _ in range(200):
            if entered.is_set(): break
            await asyncio.sleep(.005)
        assert entered.is_set()
        call = wrapper('_research_decision')
        call({'address': 'synthetic-buy', 'score_total': 81}, action='bought', reason='actual', stage='execution')
        key = 'decision:synthetic-buy:bought:actual:execution'
        assert key not in research._SEEN and key not in research._CANDIDATE_OWNERS
        assert research._LIVE_CONTEXT['synthetic-buy']['score_total'] == 81
        assert call.__globals__['_stats']['candidate_event_dropped'] == 1
        assert writer.snapshot()['pending'] == 1 and writer.snapshot()['admitted'] == 1
    finally:
        release.set()
        await close_installed(writer)


@pytest.mark.asyncio
async def test_dedup_failure_releases_only_original_owner_and_zero_ttl_claims_nothing(tmp_path, monkeypatch):
    from runtime.candidate_events import CandidateEventWriter
    paths(tmp_path, monkeypatch)
    writer = CandidateEventWriter(lambda job: True, on_result=research.finish_candidate_admission)
    writer.start()
    research.install_candidate_writer(writer)
    try:
        token = {'address': 'same'}
        research.record_candidate_decision(token, action='wait', reason='x', stage='entry', dedup_ttl_s=1)
        key = 'decision:same:wait:x:entry'
        older = research._CANDIDATE_OWNERS[key]
        monkeypatch.setattr(research.time, 'monotonic', lambda: research._SEEN[key] + 2.)
        research.record_candidate_decision(token, action='wait', reason='x', stage='entry', dedup_ttl_s=1)
        newer = research._CANDIDATE_OWNERS[key]
        research.finish_candidate_admission((key, older), False)
        assert research._CANDIDATE_OWNERS[key] == newer and key in research._SEEN
        research.record_candidate_decision(token, action='wait', reason='x', stage='entry', dedup_ttl_s=0)
        research.finish_candidate_admission((key, None), False)
        assert research._CANDIDATE_OWNERS[key] == newer and key in research._SEEN
    finally:
        await close_installed(writer)


@pytest.mark.asyncio
@pytest.mark.parametrize('result', [False, None, 1, 'error'])
async def test_unconfirmed_or_failed_jobs_do_not_become_persisted_and_worker_continues(result):
    from runtime.candidate_events import CandidateEventWriter
    results, calls = [], []
    def publish(job):
        calls.append(job)
        if len(calls) == 1:
            if result == 'error': raise OSError('synthetic disk error')
            return result
        return True
    writer = CandidateEventWriter(publish, on_result=lambda *args: results.append(args))
    writer.start()
    assert writer.submit(prepared(), context=('first', None))
    assert writer.submit(prepared(address='second'), context=('second', None))
    await writer.close()
    assert writer.snapshot()['failed'] == writer.snapshot()['persisted'] == 1
    assert results[0][1] is False and results[1][1] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['outcome', 'financial', 'oversized', 'deep', 'large_collection', 'huge_integer'])
async def test_invalid_or_non_diagnostic_records_are_not_admitted(invalid):
    from runtime.candidate_events import CandidateEventWriter
    writer = CandidateEventWriter(lambda job: True, max_row_bytes=8192)
    writer.start()
    try:
        job = prepared()
        if invalid == 'outcome': job['row']['event_type'] = 'candidate_outcome'
        elif invalid == 'financial': job['row']['outcome_trade_id'] = 'not-a-diagnostic'
        elif invalid == 'oversized': job['row']['symbol'] = 'x' * 9000
        elif invalid == 'large_collection': job['row']['list'] = list(range(300))
        elif invalid == 'huge_integer': job['row']['large'] = 1 << 5000
        else:
            nested = {}
            job['row']['nested'] = nested
            for _ in range(20): nested['next'] = {}; nested = nested['next']
        assert writer.submit(job) is False
        assert writer.snapshot()['admitted'] == 0 and writer.snapshot()['dropped'] == 1
    finally: await writer.close()


@pytest.mark.asyncio
async def test_foreign_thread_cannot_mutate_queue_or_module_dedup(tmp_path, monkeypatch):
    from runtime.candidate_events import CandidateEventWriter
    paths(tmp_path, monkeypatch)
    writer = CandidateEventWriter(lambda job: True)
    writer.start()
    research.install_candidate_writer(writer)
    try:
        before = writer.snapshot()
        assert await asyncio.to_thread(writer.submit, prepared()) is False
        with pytest.raises(RuntimeError, match='owner loop/thread'):
            await asyncio.to_thread(research.record_candidate_stage, {'address': 'foreign'}, stage='entry')
        assert writer.snapshot() == before and not research._SEEN and not research._CANDIDATE_OWNERS
    finally: await close_installed(writer)


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_thread_and_all_admitted_jobs():
    from runtime.candidate_events import CandidateEventWriter
    entered, release = threading.Event(), threading.Event()
    rows = []
    def publish(job):
        entered.set()
        assert release.wait(10)
        rows.append(job)
        return True
    writer = CandidateEventWriter(publish)
    writer.start()
    assert writer.submit(prepared()) and writer.submit(prepared(address='second'))
    for _ in range(200):
        if entered.is_set(): break
        await asyncio.sleep(.005)
    assert entered.is_set()
    closer = asyncio.create_task(writer.close())
    closer.cancel()
    # Cancellation before first coroutine instruction has admitted no close work.
    with pytest.raises(asyncio.CancelledError): await closer
    closer = asyncio.create_task(writer.close())
    await asyncio.sleep(0)
    closer.cancel()
    await asyncio.sleep(0)
    closer.cancel()
    assert not closer.done() and len(rows) == 0
    release.set()
    with pytest.raises(asyncio.CancelledError): await closer
    assert len(rows) == 2 and writer.snapshot()['persisted'] == 2 and writer.snapshot()['closed']


@pytest.mark.asyncio
async def test_bought_context_refreshes_even_when_logging_is_deduplicated(tmp_path, monkeypatch):
    from runtime.candidate_events import CandidateEventWriter
    paths(tmp_path, monkeypatch)
    writer = CandidateEventWriter(lambda job: True, on_result=research.finish_candidate_admission)
    writer.start()
    research.install_candidate_writer(writer)
    try:
        for score in (40, 90):
            research.record_candidate_decision({'address': 'same', 'score_total': score},
                action='bought', reason='actual', stage='execution')
        assert writer.snapshot()['admitted'] == 1 and research._LIVE_CONTEXT['same']['score_total'] == 90
    finally: await close_installed(writer)


@pytest.mark.asyncio
async def test_shared_journal_lock_wait_does_not_enter_candidate_owner_loop(tmp_path, monkeypatch):
    from runtime.candidate_events import CandidateEventWriter
    paths(tmp_path, monkeypatch)
    held, release = threading.Event(), threading.Event()
    def hold():
        with research._LOCK:
            held.set()
            assert release.wait(10)
    holder = threading.Thread(target=hold)
    holder.start()
    writer = CandidateEventWriter(research.publish_candidate_event, on_result=research.finish_candidate_admission)
    writer.start()
    research.install_candidate_writer(writer)
    try:
        for _ in range(200):
            if held.is_set(): break
            await asyncio.sleep(.005)
        assert held.is_set()
        wrapper('_research_stage')({'address': 'synthetic'}, stage='entry')
        progressed = []
        async def entry(): progressed.append(True)
        await asyncio.wait_for(entry(), .5)
        assert progressed and writer.snapshot()['persisted'] == 0
        assert writer.snapshot()['admitted'] == 1
    finally:
        release.set()
        await asyncio.to_thread(holder.join)
        await close_installed(writer)


@pytest.mark.parametrize('options', [
    {'capacity': 0}, {'capacity': 4097}, {'capacity': True}, {'capacity': .5},
    {'max_row_bytes': 1000}, {'max_row_bytes': 2000000}, {'max_row_bytes': True}, {'max_row_bytes': .5}])
def test_invalid_bounds_create_no_owner_or_executor(options):
    from runtime.candidate_events import CandidateEventWriter
    with pytest.raises(ValueError): CandidateEventWriter(lambda job: True, **options)


def test_unstarted_submission_never_creates_a_worker_or_claims_persistence():
    from runtime.candidate_events import CandidateEventWriter
    publish = Mock()
    writer = CandidateEventWriter(publish)
    assert writer.submit(prepared()) is False
    publish.assert_not_called()
    assert writer._task is writer._pool is None
    assert writer.snapshot()['admitted'] == writer.snapshot()['persisted'] == 0


@pytest.mark.asyncio
async def test_callback_error_is_counted_without_stopping_later_writes():
    from runtime.candidate_events import CandidateEventWriter
    def result(*args): raise ValueError('synthetic callback failure')
    writer = CandidateEventWriter(lambda job: True, on_result=result)
    writer.start()
    assert writer.submit(prepared()) and writer.submit(prepared(address='second'))
    await writer.close()
    assert writer.snapshot()['persisted'] == writer.snapshot()['callback_errors'] == 2


@pytest.mark.asyncio
async def test_native_ledger_failure_is_explicit_not_false_dual_sink_success(tmp_path, monkeypatch):
    from runtime.candidate_events import CandidateEventWriter
    paths(tmp_path, monkeypatch)
    monkeypatch.setattr(research, 'append_decision', Mock(side_effect=OSError('synthetic ledger failure')))
    writer = CandidateEventWriter(research.publish_candidate_event, on_result=research.finish_candidate_admission)
    writer.start()
    research.install_candidate_writer(writer)
    try:
        research.record_candidate_decision({'address': 'synthetic'}, action='wait', reason='x', stage='entry')
        await close_installed(writer)
    finally:
        if research._CANDIDATE_WRITER is writer: await close_installed(writer)
    assert writer.snapshot()['failed'] == 1 and writer.snapshot()['persisted'] == 0
    assert not research._SEEN and not research._CANDIDATE_OWNERS
    assert len((tmp_path / 'events.jsonl').read_text().splitlines()) == 1
    assert not (tmp_path / 'decisions.jsonl').exists()


@pytest.mark.asyncio
async def test_fatal_writer_drains_pending_counts_original_and_can_be_unregistered():
    from runtime.candidate_events import CandidateEventWriter
    class Fatal(BaseException): pass
    results = []
    def publish(job): raise Fatal('synthetic fatal owner')
    writer = CandidateEventWriter(publish, on_result=lambda *args: results.append(args))
    writer.start()
    assert writer.submit(prepared()) and writer.submit(prepared(address='second'))
    with pytest.raises(Fatal, match='synthetic fatal owner'): await writer.close()
    assert writer.snapshot()['failed'] == 2 and writer.snapshot()['closed']
    assert len(results) == 2 and all(item[1] is False for item in results)


@pytest.mark.asyncio
async def test_actual_runner_finishes_other_cleanup_and_stopped_before_reporting_writer_fatal(monkeypatch):
    from unittest.mock import AsyncMock
    from runtime import social_enrichment_queue
    from research_loop import entry_gate_forward
    class Fatal(BaseException): pass
    def publish(job): raise Fatal('synthetic candidate owner failure')
    monkeypatch.setattr(research, 'publish_candidate_event', publish)
    monkeypatch.setattr(research, '_SEEN', {})
    monkeypatch.setattr(research, '_CANDIDATE_OWNERS', {})
    stops = []
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    async def stop_pump(): stops.append('pump')
    async def stop_social(): stops.append('social')
    async def stop_research(): stops.append('research')
    monkeypatch.setattr(social_enrichment_queue, 'stop_background_tasks', stop_social)
    monkeypatch.setattr(entry_gate_forward, 'stop_background_tasks', stop_research)
    async def forever(*args): await asyncio.Event().wait()
    async def main(*, positions_ready, hot_queue_enabled):
        positions_ready.set()
        research.record_candidate_stage({'address': 'synthetic-fatal'}, stage='entry', queued_only=True)
        await forever()
    async def control():
        while research._CANDIDATE_WRITER is None or not research._CANDIDATE_WRITER._task.done():
            await asyncio.sleep(0)
        raise RuntimeError('trigger orderly shutdown')
    async def stopped():
        assert research._CANDIDATE_WRITER is None and namespace['_POLICY_FEATURE_WRITER'] is None
        assert namespace['_runtime_process_state'] == 'stopped'
        assert set(stops) == {'pump', 'social', 'research'}
        stops.append('stopped')
    namespace = {'asyncio': asyncio, 'CFG': SimpleNamespace(ML_RETRAIN_IN_MAIN_LOOP=False, HOT_QUEUE_ENABLED=False),
        'DRY_RUN': False, 'async_init_db': AsyncMock(), 'SessionLocal': Session,
        '_recover_buy_persistence_outbox': AsyncMock(), '_recover_close_persistence_outbox': AsyncMock(),
        '_refresh_green_live_risk': AsyncMock(), 'store_append': Mock(return_value=True),
        '_policy_feature_write_result': Mock(), 'main_loop': main, '_dex_discovery_loop': forever,
        '_position_monitor_loop': forever, '_shadow_monitor_loop': forever, '_periodic_labeler': forever,
        'control_command_loop': control, 'runtime_state_loop': forever, '_background_tasks': set(),
        'pumpfun': SimpleNamespace(stop_background_tasks=stop_pump), '_stats': {},
        '_note_runtime_error': Mock(), '_publish_runtime_state_once': stopped,
        'log': SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())}
    node = next(item for item in ast.parse(Path('run_bot.py').read_text(encoding='utf-8')).body
                if isinstance(item, ast.AsyncFunctionDef) and item.name == '_runner')
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'run_bot.py', 'exec'), namespace)
    with pytest.raises(Fatal, match='synthetic candidate owner failure'):
        await namespace['_runner']()
    assert stops[-1] == 'stopped' and namespace['_stats']['candidate_event_failed'] == 1
