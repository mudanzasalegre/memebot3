"""Cold source-only native supervisor with fictitious provider boundaries."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import time

spec = importlib.util.spec_from_file_location('native_provider_bootstrap',
    Path(__file__).with_name('native_paper_lifecycle_case.py'))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
r = b.run_bot
root = b.expected
assert b.options.get('supervised_runtime') is True and b.case == 'rpc_birth'
assert r.CFG.CORE_REPORTS_AUTO_REGEN_ENABLED and r.CFG.CORE_REPORTS_REGEN_ON_STARTUP
assert not r.CFG.ML_RETRAIN_IN_MAIN_LOOP


def guard_rows():
    path = root/'native-worker-guard.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def exercise():
    from runtime.local_stop import LocalStopRequested, run_with_local_stop
    from runtime.owned_dispatch import pending_dispatch_count
    from db.models import BotRuntimeState
    from sqlalchemy import select
    children, publications = [], []
    original_spawn = asyncio.create_subprocess_exec
    async def observed_spawn(*args, **kwargs):
        child = await original_spawn(*args, **kwargs)
        children.append(child)
        return child
    asyncio.create_subprocess_exec = observed_spawn
    original_publish = r._publish_runtime_state_once
    async def observed_publish():
        if r._runtime_process_state == 'stopped':
            publications.append([child.returncode for child in children])
        return await original_publish()
    r._publish_runtime_state_once = observed_publish

    delivered = False
    async def pump_feed():
        nonlocal delivered
        b.calls.append('pump_feed')
        if delivered: return []
        delivered = True
        return [b.entry_token]
    async def dex_feed():
        b.calls.append('dex_feed')
        return []
    async def balance():
        b.calls.append('wallet_balance')
        return 10.
    r.pumpfun.get_latest_pumpfun = pump_feed
    r.fetch_candidate_pairs = dex_feed
    r.get_sol_balance = balance
    veto = b.options.get('supervised_veto')
    if veto == 'missing_route':
        async def no_route(*args, **kwargs):
            b.calls.append('quote')
            return None
        b.jupiter_router.get_routing_quote = no_route

    r._acquire_process_lock()
    binding = r._PROCESS_STOP_BINDING
    runner = asyncio.create_task(run_with_local_stop(r._runner, binding), name='fixture-owned-native-runtime')
    deadline = time.monotonic()+100
    async def until(predicate):
        while not predicate():
            if runner.done(): runner.result()
            if time.monotonic() >= deadline:
                raise AssertionError('native_supervised_diagnostic='+json.dumps({
                    'runtime_state':r._runtime_process_state, 'error':r._runtime_last_error,
                    'stats':r._stats, 'calls':b.calls, 'guard':guard_rows()}, default=str))
            await asyncio.sleep(.01)
    async def position():
        async with r.SessionLocal() as session:
            return (await session.execute(select(b.Position))).scalars().first()
    report = {'progression':[], 'supervised_runtime':True}
    failure = None
    try:
        await until(lambda: r._runtime_process_state == 'running' and delivered)
        tasks = {task.get_name():task for task in asyncio.all_tasks()}
        expected = {'entry-service','dex-discovery','pump-discovery','position-monitor',
            'shadow-monitor','shadow-opening','periodic-labeler','control-commands',
            'runtime-state','costed-trade-export'}
        assert expected <= set(tasks) and 'retrain' not in tasks
        report['native_services'] = sorted(expected)
        def evaluated():
            task = tasks['entry-service']
            pending = task.get_coro().cr_await
            return r._stats['raw_discovered'] >= 1 and getattr(pending, '__qualname__', '') == 'sleep'
        await until(evaluated)
        p = await position()
        if veto:
            assert p is None and r._stats['actual_paper_buys'] == 0
            assert 'quote' in b.calls
        else:
            assert p is not None and p.buy_amount_sol == .1 and p.dry_run is True
            assert r._stats['actual_paper_buys'] == 1
            if b.options.get('monitor'):
                for factor in (3., 6., 11., 51., 101., 1001., 10001., 1001.):
                    b.factor = factor
                    while True:
                        current = await position()
                        # The synthetic SELL quote applies the same1% impact
                        # as the existing provider fixture, not its spot mark.
                        expected_peak = (.01*factor*.99/p.buy_price_usd-1)*100
                        terminal = (factor == 1001. and report['progression']
                            and report['progression'][-1]['factor'] == 10001.)
                        if current.closed if terminal else (current.highest_pnl_pct or 0) >= expected_peak-1:
                            break
                        if time.monotonic() >= deadline: await until(lambda: False)
                        await asyncio.sleep(.02)
                    report['progression'].append({'factor':factor,'qty':current.qty,
                        'closed':current.closed,'peak':current.highest_pnl_pct,'partials':current.partial_count})
                assert current.closed and report['progression'][-2]['qty'] > 0
                from features import store
                import pyarrow.parquet as parquet
                while not list(store.DATA_DIR.glob('*.parquet')):
                    if time.monotonic() >= deadline: raise AssertionError('Native export loop did not publish closed trade')
                    await asyncio.sleep(.05)
                # The real native exporter uses a typed financial sample tag;
                # inspect its exact retained record, not a handmade target.
                all_rows = [row for path in store.DATA_DIR.glob('*.parquet') for row in parquet.read_table(path).to_pylist()]
                financial = [row for row in all_rows if row.get('outcome_trade_id')]
                assert len(financial) == 1
                report['export_rows'] = len(financial)
                report['frozen_signal'] = financial[0]['price_pct_5m']
                report['net_label_pct'] = financial[0]['target_total_pnl_pct']
        await until(lambda: len(children)==1 and any(row['event']=='worker_started' for row in guard_rows()))
        background = next(task for task in r._background_tasks if task.get_name() == 'core-reports-startup')
        binding.request()
        await until(lambda: background.cancelling() or runner.done())
        await asyncio.sleep(.2)
        report['premature_report_owner_done'] = background.done() and children[0].returncode is None
        (root/'release-report-child').touch()
        try:
            await asyncio.wait_for(asyncio.shield(runner), 60)
        except LocalStopRequested:
            binding.complete()
        report['completion_confirmed'] = binding.completed()
        report['final_publication_children'] = publications
        async with r.SessionLocal() as session:
            state = await session.get(BotRuntimeState, r._RUNTIME_STATE_BOT_ID)
            report['persisted_runtime_state'] = state.process_state
            rows = (await session.execute(select(b.Position))).scalars().all()
            report['positions'] = [{'amount_sol':row.buy_amount_sol,'dry_run':row.dry_run,
                'closed':row.closed,'qty':row.qty} for row in rows]
        report['pending_buys'] = len(r._BUY_RECOVERY.pending_addresses)
        report['pending_sells'] = len(r._SELL_RECOVERY.pending_addresses)
        report['pending_dispatch'] = pending_dispatch_count()
        report['runtime_last_error'] = r._runtime_last_error
    except BaseException as exc:
        failure = exc
    finally:
        (root/'release-report-child').touch()
        if not runner.done():
            binding.request()
            try: await asyncio.wait_for(asyncio.shield(runner), 60)
            except LocalStopRequested: pass
        # Exact owned test children are always drained even if old production
        # cleanup returned too early. Never discover or stop a foreign PID.
        for child in children:
            if child.returncode is None:
                await asyncio.wait_for(child.communicate(), 60)
        report['report_child_returncodes'] = [child.returncode for child in children]
        report['worker_guard'] = guard_rows()
        r._release_process_lock()
        await b.engine.dispose()
        asyncio.create_subprocess_exec = original_spawn
    if failure is not None: raise failure
    report['remaining_tasks'] = sorted(task.get_name() for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done())
    report['live_signer_loaded'] = 'trader.sol_signer' in sys.modules
    assert report['remaining_tasks'] == [], report
    assert report['live_signer_loaded'] is False
    assert report['completion_confirmed'] and report['persisted_runtime_state'] == 'stopped'
    assert report['final_publication_children'] == [[0]], report
    assert report['premature_report_owner_done'] is False, report
    assert report['report_child_returncodes'] == [0], report
    assert report['runtime_last_error'] is None, report
    assert b.allowed_report_children == ['tools/regenerate_core_reports.py']
    assert {row['event'] for row in report['worker_guard']} <= {'worker_started','worker_released','worker_exit'}
    report['scope'] = 'Actual cold source-only native supervisor/services/report subprocess/local stop with mocked provider boundaries; no operator data, financial wallet, model fitting or AutoResearch. Not operational provider or profitability acceptance.'
    return report


b.emit_report(asyncio.run(exercise()))
