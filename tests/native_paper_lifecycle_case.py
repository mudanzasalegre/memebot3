"""Isolated synthetic native PAPER case; no operational bot, wallet or provider."""
import asyncio
import datetime as dt
import json
import sys
import time
import traceback
from pathlib import Path
expected = Path(sys.argv.pop(1)).resolve()
options = json.loads(sys.argv.pop(1))
assert Path(__file__).resolve().parents[1] == expected
sys.path.insert(0, str(expected))
if options.get('log'):
    sys.argv.append('--log')
case = options['case']
attempts = []
bootstrap_in_progress = True

def checked_numpy_bootstrap_attempts(items):
    # Nothing is allowed to execute. NumPy1.26's test utilities catch the
    # PermissionError from their optional CPU probe during dependency setup.
    # Preserve that denied attempt separately; never clear an unknown action.
    assert len(items) <= 1, items
    assert all(isinstance(item, dict) and item.get('event') == 'subprocess.Popen'
        and item.get('program') == 'lscpu' and item.get('numpy_testing_probe') is True
        for item in items), items
    return [{'program':'lscpu','source':'numpy.testing._private.utils.check_support_sve',
        'blocked':True} for item in items]

def checked_dependency_bootstrap_attempts(items):
    """Report the exact denied Windows fallback, never allow its execution."""
    windows = [item for item in items if isinstance(item, dict)
        and item.get('stdlib_windows_version_probe') is True]
    if windows:
        assert items[:3] == windows and len(windows) == 3, items
        assert all(item.get('event') == 'subprocess.Popen'
            and item.get('program') == 'cmd.exe' for item in windows), items
        assert [item.get('version_probe_command') for item in windows] == [
            'ver', 'command /c ver', 'cmd /c ver'], items
    remaining = items[len(windows):]
    numpy = checked_numpy_bootstrap_attempts(remaining)
    return numpy, [{'program':'cmd.exe','source':'platform._syscmd_ver',
        'command':item['version_probe_command'],'blocked':True} for item in windows]


def audit(event, args):
    if event in {'socket.getaddrinfo', 'socket.connect'}:
        address = args[0] if event == 'socket.getaddrinfo' else args[1][0]
        if address not in {'127.0.0.1', '::1', 'localhost', None}:
            attempts.append(event)
            raise PermissionError('External network forbidden in isolated integration')
    if event == 'subprocess.Popen':
        frame = sys._getframe()
        numpy_testing_probe = False
        numpy_bootstrap_owner = False
        version_probe_command = None
        platform_module = sys.modules.get('platform')
        numpy_module = sys.modules.get('numpy.testing._private.utils')
        version_code = getattr(getattr(platform_module, '_syscmd_ver', None), '__code__', None)
        while frame is not None:
            if (frame.f_code.co_name == 'check_support_sve'
                    and frame.f_globals.get('__name__') == 'numpy.testing._private.utils'):
                numpy_testing_probe = True
            if (numpy_module is not None and frame.f_globals is vars(numpy_module)
                    and frame.f_code.co_name == '<module>'):
                numpy_bootstrap_owner = True
            if (platform_module is not None and frame.f_globals is vars(platform_module)
                    and frame.f_code is version_code
                    and frame.f_locals.get('cmd') in ('ver', 'command /c ver', 'cmd /c ver')):
                version_probe_command = frame.f_locals['cmd']
            frame = frame.f_back
        # Windows exposes executable=None and the normalized command line;
        # POSIX exposes executable='lscpu'. Recognize only the exact probe.
        program = Path(args[0]).name if isinstance(args[0], str) else None
        if args[0] is None and args[1] == 'lscpu':
            program = 'lscpu'
        version_probe = (bootstrap_in_progress and sys.platform == 'win32'
            and numpy_bootstrap_owner and version_probe_command is not None
            and program == 'cmd.exe' and isinstance(args[0], str)
            and args[1] == f'{args[0]} /c "{version_probe_command}"'
            and args[2] is None and args[3] is None)
        attempts.append({'event':event,'program':program,
            'numpy_testing_probe':numpy_testing_probe,
            'stdlib_windows_version_probe':bool(version_probe),
            'version_probe_command':version_probe_command if version_probe else None,
            'callers':[{'file':Path(frame.filename).name,'function':frame.name}
                       for frame in traceback.extract_stack(limit=8)]})
        raise PermissionError('Child process forbidden in isolated integration')
sys.addaudithook(audit)
# Bootstrap only the installed scientific testing dependency under the SAME
# no-network/no-child guard. No native project module has executed yet.
if options.get('bootstrap_windows_fallback') and sys.platform == 'win32':
    import platform as _bootstrap_platform
    _bootstrap_original_wmi = getattr(_bootstrap_platform, '_wmi_query', None)
    def _bootstrap_unavailable_wmi(*args, **kwargs):
        raise OSError('Owned test: WMI unavailable')
    _bootstrap_platform._wmi_query = _bootstrap_unavailable_wmi
    _bootstrap_platform._uname_cache = None
import numpy.testing
dependency_probes, platform_probes = checked_dependency_bootstrap_attempts(attempts)
bootstrap_in_progress = False
if options.get('bootstrap_windows_fallback') and sys.platform == 'win32':
    if _bootstrap_original_wmi is None:
        del _bootstrap_platform._wmi_query
    else:
        _bootstrap_platform._wmi_query = _bootstrap_original_wmi
attempts.clear()
from config.config import CFG, PROJECT_ROOT
assert PROJECT_ROOT.resolve() == expected and CFG.DRY_RUN is True
assert not (expected / '.env').exists()
import run_bot
from db.database import DB_PATH, engine
assert DB_PATH.resolve().is_relative_to(expected)
assert Path(run_bot.__file__).resolve().is_relative_to(expected)
scoped = []
for name, module in list(sys.modules.items()):
    if name.split('.')[0] in {'analytics','runtime','features','fetcher','ml','trader','config','db','execution','utils','research_loop'}:
        file = getattr(module, '__file__', None)
        if file:
            assert Path(file).resolve().is_relative_to(expected), (name, file)
            scoped.append(name)
assert not attempts, attempts
sys.path.insert(0, str(expected / 'tests'))
from quote_fixtures import SOL, v1_quote
from solders.pubkey import Pubkey
TOKEN = str(Pubkey.new_unique())
from fetcher import rugcheck, helius_cluster, jupiter_price, jupiter_router
from fetcher.jupiter_price_v3 import parse_price_payload
from utils import price_service, sol_price
from utils.sol_price import SolUsdObservation
from utils.market_observation import stamp_market_observation
from utils.auxiliary_observation import observation
from analytics.token_birth import FIELD
from sqlalchemy import select
from db.models import Position, TradeEvent
from features import token_clock_semantics as native_clock
clock_failures = []
original_clock_capture = native_clock.capture_clock_proof
def diagnose_original_clock_capture(vector, token):
    try:
        return original_clock_capture(vector, token)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        row = vector.to_dict() if hasattr(vector, 'to_dict') else dict(vector)
        from analytics.token_time import _to_datetime
        stamp = _to_datetime(row.get('timestamp'))
        inputs = native_clock._inputs(token)
        clock_failures.append({'error':str(exc),'type':type(exc).__name__,
            'timestamp':stamp.isoformat() if stamp else None,
            'vector_ages':{name:row.get(name) for name in native_clock.AGE_FEATURES},
            'expected_ages':native_clock._ages(inputs,stamp) if stamp else None,
            'inputs':inputs,'birth_received_at':token.get(FIELD,{}).get('received_at')})
        raise
native_clock.capture_clock_proof = diagnose_original_clock_capture
calls = []
factor = 1.
stream_ready = not options.get('stream_reconsideration', False)
assert type(stream_ready) is bool
born = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=3)
birth = {'version':'birdeye_mint_creation_receipt_v1','source':'birdeye','chain':'solana',
    'address':TOKEN,'created_at':born.isoformat(),'slot':123,'tx_hash':'1'*64,
    'received_at':time.time()-.01,'basis':'provider_reported_mint_creation_not_independent_chain_verification'}
entry_token = {'address':TOKEN,'chain':'solana','created_at':born,
    FIELD:birth,'discovered_via':'dex','dex_id':'pumpswap'}
if case.startswith('rpc_birth'):
    from test_rpc_mint_initialization import fixture
    from fetcher import pumpfun
    from runtime import token_birth_enrichment
    from runtime.rpc_token_birth import RpcTokenBirthResolver
    from fetcher.mint_initialization import get_mint_initialization
    from utils import solana_execution as rpc
    result, TOKEN, signature, reference_received = fixture(cpi=True,lookup=True)
    if options.get('failure') == 'rpc_unavailable': result = None
    if case == 'rpc_birth_unknown': result = None
    elif case == 'rpc_birth_failed': result['meta']['err'] = {'InstructionError':[0,'InvalidArgument']}
    elif case == 'rpc_birth_future': result['blockTime'] = int(time.time()) + 3600
    entry_token = pumpfun._parse_event({'mint':TOKEN,'signature':signature,'txType':'create','slot':123})
    # Keep the discovered reference, use the existing fresh Pumpswap route.
    entry_token.update(dex_id='pumpswap')
    class CreationStream:
        def __init__(self, value): self.raw = value
        async def read(self,bound):
            chunk,self.raw = self.raw[:bound],self.raw[bound:]
            return chunk
    class CreationReply:
        status = 200
        def __init__(self,payload):
            self.content = CreationStream(json.dumps({'jsonrpc':'2.0','id':payload['id'],'result':result}).encode())
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
    class CreationSession:
        def __init__(self,**kwargs): assert kwargs['timeout'].total == 3
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        def post(self,url,**kwargs):
            calls.append('creation_rpc')
            payload = kwargs['json']
            assert url == 'https://synthetic-node.example.test' and kwargs['allow_redirects'] is False
            assert payload['method'] == 'getTransaction' and payload['params'][0] == signature
            return CreationReply(payload)
    rpc.aiohttp.ClientSession = CreationSession
    token_birth_enrichment.GLOBAL_RPC_TOKEN_BIRTH_RESOLVER = RpcTokenBirthResolver(
        lookup=get_mint_initialization,endpoint=lambda:'https://synthetic-node.example.test',degraded=lambda:False)
clock_trace = {}
if options.get('coarse_clock'):
    assert case == 'rpc_birth'
    from runtime import entry_clock
    original_capture_causal_utc = entry_clock.capture_causal_utc
    async def coarse_capture(*, minimum_epoch, utc, **kwargs):
        # This deliberately lagging UTC callback is a synthetic clock source,
        # not a production timestamp repair. All receipt bytes stay original.
        first = dt.datetime.fromtimestamp(minimum_epoch,dt.timezone.utc)
        if first.timestamp() >= minimum_epoch:
            first -= dt.timedelta(microseconds=1)
        observed = []
        def lagging_then_real():
            value = first if not observed else utc()
            observed.append(value)
            return value
        result = await original_capture_causal_utc(minimum_epoch=minimum_epoch,utc=lagging_then_real,**kwargs)
        clock_trace.update(first_epoch=first.timestamp(),minimum_epoch=minimum_epoch,
            captured_epoch=result.timestamp() if result is not None else None,
            reads=len(observed),returned_actual_observation=result is not None and any(result is item for item in observed))
        return result
    entry_clock.capture_causal_utc = coarse_capture
if case == 'clock_ahead':
    from runtime import entry_clock
    original_capture = entry_clock.capture_causal_utc
    async def ahead_capture(*, minimum_epoch, utc, **kwargs):
        return await original_capture(minimum_epoch=minimum_epoch,
            utc=lambda:utc()+dt.timedelta(seconds=5),**kwargs)
    entry_clock.capture_causal_utc = ahead_capture

def snapshot_value(address):
    return stamp_market_observation({'address':address,'chain':'solana','symbol':'SYNTHETIC',
        'dex_id':'pumpswap','price_usd':.01,'liquidity_usd':50000.,'market_cap_usd':100000.,
        'fdv_usd':150000.,'volume_24h_usd':10000000. if stream_ready else 0.,'holders':10000,
        'txns_last_5m':1000 if stream_ready else 1,'txns_last_5m_buys':900 if stream_ready else 1,'txns_last_5m_sells':100 if stream_ready else 1,
        'price_pct_5m':options.get('momentum',5000.) if stream_ready else 0.,'price_pct_1m':500. if stream_ready else 0.,'volume_pct_5m':1000. if stream_ready else 0.,
        'liquidity_usd_is_proxy':False,'liquidity_is_proxy':False,
        'price_source':'dexscreener','price_confidence':'high'}, 'dexscreener')
async def snapshot(address, **kwargs):
    calls.append('snapshot')
    return snapshot_value(address)
async def rug(address):
    calls.append('rug')
    stamp = time.time()-.001
    return observation('rug',address,1,source='rugcheck_report_summary',observed_at=stamp,
        inputs={'score':1,'score_normalised':1})
async def cluster(address):
    calls.append('cluster')
    stamp = time.time()-.001
    if case == 'missing_cluster':
        return observation('cluster',address,None,source='solana_rpc_confirmed',reason='synthetic_unavailable')
    bad = case == 'bad_cluster'
    return observation('cluster',address,bad,source='solana_rpc_confirmed',observed_at=stamp,
        inputs={'accounts':[{'address':f'account-{i}',
            'amount':(9000 if i==0 else 1) if bad else 100,'decimals':6} for i in range(20)],
            'total_supply':10000,'decimals':6,'largest_slot':123,'supply_slot':123,
            'largest_received_at':stamp,'supply_received_at':stamp,'commitment':'confirmed'})
async def quote(**kwargs):
    calls.append('quote')
    from utils.raw_units import sol_to_lamports
    amount = kwargs.get('amount_lamports')
    if amount is None:
        amount = sol_to_lamports(kwargs['amount_sol'])
    if case == 'missing_route':
        return None
    output = int(amount * 10) if kwargs['input_mint']==SOL else int(amount * .1 * factor)
    return v1_quote(kwargs['input_mint'],kwargs['output_mint'],amount,output, impact_bps=10)
async def price_batch(mints):
    calls.append('price_batch')
    stamp = time.time()-(61 if case=='stale_price' else .001)
    return parse_price_payload({mint:{'usdPrice':100. if mint==SOL else .01 * factor,
        'blockId':123,'decimals':9 if mint==SOL else 6} for mint in mints},
        mints, received_at=stamp)
async def fx():
    calls.append('fx')
    stamp = time.time()-.001
    if case == 'missing_fx':
        return SolUsdObservation(status='ERR',reason='synthetic_unavailable')
    return SolUsdObservation(status='OK',price_usd=100.,received_at=stamp,market_updated_at=stamp)
price_service.get_entry_snapshot = snapshot
async def market_price(address, **kwargs):
    calls.append('market_price')
    if options.get('shadow_feed'):
        return snapshot_value(address)
    return stamp_market_observation({'address':address,'price_usd':.01 * factor,
        'liquidity_usd':50000.,'price_source':'dexscreener'},'dexscreener')
price_service.get_price = market_price
rugcheck.fetch_observation = rug
helius_cluster.fetch_observation = cluster
jupiter_router.get_routing_quote = quote
jupiter_price._fetch_batch_with_status = price_batch
sol_price.get_sol_usd_observation = fx
async def exercise():
    global factor, entry_token, stream_ready
    await run_bot.async_init_db()
    from analytics import research_runtime as candidate_runtime
    from runtime.candidate_events import CandidateEventWriter
    candidate_writer = CandidateEventWriter(candidate_runtime.publish_candidate_event,
        on_result=candidate_runtime.finish_candidate_admission)
    candidate_writer.start()
    candidate_runtime.install_candidate_writer(candidate_writer)
    run_bot._wallet_sol_balance = 10.
    report = {'progression':[]}
    try:
        if case == 'rpc_birth_cached_unreferenced':
            # The only synthetic creation response precedes the new feed. The
            # actual entry receives neither a creation reference nor a clock.
            warmed = await token_birth_enrichment.enrich_token_birth(entry_token)
            cached_original_birth = warmed[FIELD]
            assert warmed['token_birth_enrichment']['status'] == 'resolved'
            assert calls == ['creation_rpc']
            entry_token = {'address':TOKEN,'chain':'solana',
                'discovered_via':'dex','dex_id':'pumpswap'}
            calls.append('cached_unreferenced_entry')
        async with run_bot.SessionLocal() as ses:
            entry_source = 'dex'
            if options.get('stream_reconsideration'):
                from runtime.hot_queue import HotQueue
                queue = HotQueue(persist_events=False)
                clock = [time.time()]
                admission_clock = [time.monotonic()]
                queue._now = lambda: clock[0]
                # Advance the SAME retained local admission owner, not its
                # quota/cooldown parameters or reservation state. Queue wall
                # time alone cannot expire an independent monotonic budget.
                run_bot._PF_PRICE_ADMISSION.clock = lambda: admission_clock[0]
                run_bot.GLOBAL_HOT_QUEUE = queue
                initial = {**entry_token, 'discovered_via':'pumpfun',
                    'price_pct_5m':0., 'txns_last_5m':1}
                assert queue.add(initial)
                first = queue.pop_batch(1)[0]
                if options.get('shadow_feed'):
                    from runtime.shadow_openings import ShadowOpeningQueue
                    ready = asyncio.Event()
                    ready.set()
                    opener = ShadowOpeningQueue(run_bot._open_shadow_now, ready=ready)
                    opener.start()
                    run_bot._SHADOW_OPEN_QUEUE = opener
                await run_bot._evaluate_and_buy_guarded(first,ses,source='hot_queue')
                if options.get('shadow_feed'):
                    await opener.close()
                    run_bot._SHADOW_OPEN_QUEUE = None
                    assert opener.snapshot()['completed'] == 1, opener.snapshot()
                    original = run_bot._shadow_positions[TOKEN]
                    assert native_clock.checked_row_clock(original['vec']) is not None
                    report['shadow_feed'] = {'opening_completed':opener.snapshot()['completed'],
                        'original_shadow_clock_valid':True}
                assert (await ses.execute(select(Position))).scalars().all() == []
                assert run_bot._stats['actual_paper_buy_attempts'] == 0
                assert calls.count('snapshot') == 1 and run_bot._stream_candidate_is_cooled(TOKEN)
                report['stream_reconsideration'] = {'initial_attempts':0,
                    'initial_weak_snapshot_observed':True}
                stream_ready = True
                advance = max(1., float(getattr(CFG,'HOT_QUEUE_RECHECK_INTERVAL_S',15.)),
                    run_bot._PF_PRICE_ADMISSION.cooldown_s, run_bot._PF_PRICE_ADMISSION.window_s) + 1.
                assert advance < 900.
                clock[0] += advance
                admission_clock[0] += advance
                report['stream_reconsideration']['controlled_scheduling_clock_advance_s'] = advance
                if options.get('shadow_feed'):
                    before_monitor_calls = calls.count('market_price')
                    await run_bot._tick_shadows()
                    report['shadow_feed']['monitor_market_calls'] = calls.count('market_price') - before_monitor_calls
                    assert queue.snapshot()['size'] == 1, 'Observed shadow update never reached native scanner'
                else:
                    assert queue.add({**initial,'price_pct_5m':options.get('momentum',5000.),'txns_last_5m':1000})
                entry_token = queue.pop_batch(1)[0]
                if options.get('shadow_feed'):
                    report['shadow_feed']['queued_source'] = entry_token['discovered_via']
                entry_source = 'hot_queue'
            await run_bot._evaluate_and_buy_guarded(entry_token,ses,source=entry_source)
            if options.get('stream_reconsideration'):
                report['stream_reconsideration']['original_cooldown_still_active'] = run_bot._stream_candidate_is_cooled(TOKEN)
            positions = (await ses.execute(select(Position))).scalars().all()
            if case in {'positive','rpc_birth','rpc_birth_cached_unreferenced'}:
                if not (len(positions)==1 and positions[0].buy_amount_sol==.1 and positions[0].dry_run is True):
                    events_path = expected/'data/metrics/runtime_events.jsonl'
                    events = [json.loads(line) for line in events_path.read_text(encoding='utf-8-sig').splitlines()] if events_path.exists() else []
                    from runtime.token_birth_enrichment import token_birth_enrichment_snapshot
                    diagnostic = {'case':case,'position_count':len(positions),
                        'external_attempts':attempts,'operator_files_copied':0,
                        'dry_run':CFG.DRY_RUN,'exact_paper_size':CFG.PAPER_EXACT_TRADE_SIZE_SOL,
                        'position_amounts':[p.buy_amount_sol for p in positions],
                        'position_dry_run':[p.dry_run for p in positions],
                        'boundary_calls':calls,'stats':run_bot._stats,
                        'runtime_last_error':run_bot._runtime_last_error,
                        'clock_validation_failures':clock_failures[-5:],
                        'causal_clock_failures':[e.get('clock_diagnostic') for e in events
                            if e.get('event_type')=='entry_clock_unconfirmed' or e.get('event')=='entry_clock_unconfirmed'],
                        'birth_enrichment':token_birth_enrichment_snapshot(),
                        'decisions':[{'event':e.get('event_type') or e.get('event'),
                            'reason':e.get('reason'),'stage':e.get('stage'),
                            'hard_failures':e.get('hard_failures')} for e in events[-30:]]}
                    raise AssertionError('native_paper_entry_diagnostic='+json.dumps(diagnostic,default=str))
                p = positions[0]
                from trader import papertrading
                primary = papertrading._PORTFOLIO[TOKEN]
                report['entry_qty'] = p.entry_qty
                report['entry_signal'] = p.buy_price_pct_5m
                report['entry_run_id'] = p.run_id
                report['entry_identity_matches'] = p.source_position_key=='buy:'+primary['entry_intent_id']
                if case in {'rpc_birth','rpc_birth_cached_unreferenced'}:
                    from analytics.token_birth import checked_token_birth
                    from utils.atomic_json import read_json_strict
                    from runtime.buy_recovery import BuyRecoveryStore
                    journals = [expected/'data/metrics/buy_recovery'/sub/(primary['entry_intent_id']+'.json')
                        for sub in ('','resolved')]
                    available = [path for path in journals if path.exists()]
                    assert len(available) == 1
                    journal = read_json_strict(available[0])
                    BuyRecoveryStore._validate_row(journal)
                    original_birth = journal['entry_features']['token_clock'][FIELD]
                    report['rpc_birth_initializer_checked'] = checked_token_birth(original_birth,TOKEN) is not None
                    if case == 'rpc_birth_cached_unreferenced':
                        report['cached_original_receipt_retained'] = original_birth == cached_original_birth
                        assert calls.count('creation_rpc') == 1
                assert not run_bot._BUY_RECOVERY.pending_addresses
                # A duplicate actual dispatch cannot submit or persist another fill.
                await run_bot._evaluate_and_buy_guarded(entry_token,ses,source='dex')
                if options.get('monitor'):
                    for factor in (3., 6., 11., 51., 101., 1001., 10001., 1001.):
                        await run_bot._check_positions(ses)
                        await ses.refresh(p)
                        report['progression'].append({'factor':factor,'qty':p.qty,'closed':p.closed,
                            'partials':p.partial_count,'peak':p.highest_pnl_pct,
                            'sql_gross_pnl_usd':p.total_pnl_usd})
                    from runtime.trade_learning import prepare_close, publish_close, validate_source
                    identity = primary['entry_intent_id']
                    prepared = prepare_close(identity, root=expected)
                    report['net_label_pct'] = validate_source(prepared)
                    report['gross_label_pct'] = 100*prepared['trade']['total_pnl_usd']/prepared['trade']['entry_notional_usd']
                    report['frozen_signal'] = prepared['entry_features']['vector']['price_pct_5m']
                    report['first_publish'] = publish_close(identity, root=expected)
                    report['second_publish'] = publish_close(identity, root=expected)
                    from features import store
                    import pyarrow.parquet as parquet
                    assert store.DATA_DIR.resolve().is_relative_to(expected)
                    tables = [parquet.read_table(path) for path in store.DATA_DIR.glob('*.parquet')]
                    report['export_files'] = [path.relative_to(expected).as_posix()
                        for path in store.DATA_DIR.glob('*.parquet')]
                    report['export_rows'] = sum(table.num_rows for table in tables)
                    exported = [row for table in tables for row in table.to_pylist()]
                    assert len(exported)==1 and exported[0]['outcome_trade_id']==identity
                    assert exported[0]['price_pct_5m']==options.get('momentum',5000.)
                    assert abs(exported[0]['target_total_pnl_pct']-report['net_label_pct'])<.001
                    if case in {'rpc_birth','rpc_birth_cached_unreferenced'}:
                        from features.token_clock_semantics import checked_row_clock
                        report['rpc_birth_retained_in_export'] = checked_row_clock(exported[0])[FIELD] == original_birth
                    fills = (await ses.execute(select(TradeEvent).where(TradeEvent.position_id==p.id))).scalars().all()
                    report['sql_events'] = [{'event':event.event_type,'qty':event.qty} for event in fills]
                    assert sum(event.qty for event in fills)==p.entry_qty
            report['positions'] = [{'address':p.address,'closed':p.closed,'amount_sol':p.buy_amount_sol,
                'qty':p.qty,'lane':p.entry_lane,'dry_run':p.dry_run,'run_id':p.run_id} for p in positions]
            report['pending_buys'] = len(run_bot._BUY_RECOVERY.pending_addresses)
            report['pending_sells'] = len(run_bot._SELL_RECOVERY.pending_addresses)
    finally:
        await candidate_writer.close()
        candidate_runtime.remove_candidate_writer(candidate_writer)
        for key in ('admitted', 'persisted', 'failed', 'dropped', 'pending'):
            run_bot._stats['candidate_event_' + key] = candidate_writer.snapshot()[key]
        await jupiter_price.aclose()
        await engine.dispose()
    return report
report = asyncio.run(exercise())
assert not attempts, attempts
events_path = expected/'data/metrics/runtime_events.jsonl'
events = [json.loads(line) for line in events_path.read_text(encoding='utf-8-sig').splitlines()] if events_path.exists() else []
print(json.dumps({**report,'native_complete_entry':True,'boundary_calls':calls,'causal_clock_trace':clock_trace,
    'events':[{'event':e.get('event_type') or e.get('event'),'reason':e.get('reason'),
        'stage':e.get('stage'),'hard_failures':e.get('hard_failures'),
        'clock_diagnostic':e.get('clock_diagnostic')} for e in events],
    'stats':run_bot._stats,'external_attempts':attempts,
    'blocked_dependency_probes':dependency_probes,
    'blocked_platform_probes':platform_probes,
    'isolated_native_import':True,'scoped_modules':len(scoped),
    'operator_files_copied':0,'outbound_network_attempts':len(attempts),
    'dry_run':CFG.DRY_RUN,'exact_paper_size':CFG.PAPER_EXACT_TRADE_SIZE_SOL,
    'scope':'Native source-only PAPER lifecycle; no operational main loop, wallet or providers. Scientific testing bootstrap probes are separately recorded and blocked; the native phase permits no child process or external network.'}))
